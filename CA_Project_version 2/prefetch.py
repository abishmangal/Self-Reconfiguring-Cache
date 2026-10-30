"""
prefetch.py — The L2's prefetch unit: a stride detector that is ALWAYS
training, plus an issue engine whose mode and distance the controller sets.

    unit = PrefetchUnit()                    # mode "off"
    l2.prefetcher = unit                     # build_system() does this
    unit.set_config("stride", 4)             # runtime change, no data to move

Why the detectors never switch off: the controller must know whether a stream
is prefetchable BEFORE it turns prefetching on. So every demand access the L2
sees trains the stride table and the sequential detector, whatever the mode.
Only the issuing of prefetches is gated by the mode. The monitor reads these
detector counters (see snapshot()).

Modes
    off        : never issues.
    next-line  : on every access to line L, prefetch L+1 .. L+distance.
    stride     : on every access, if that PC's stride is confident, prefetch
                 L+stride*1 .. L+stride*distance.
Distance is the number of lines (or strides) ahead and must be in
sim_config.PF_DISTANCE_LADDER.

The unit is stateless about what it already requested: it proposes targets on
every access and Cache.prefetch() skips lines already present or in flight
(counted as pf_redundant). Targets refused for lack of a free MSHR are counted
as pf_dropped. Accuracy and coverage are measured from the cache counters
(pf_issued, pf_useful, ...), not here.

Detector counters (cumulative, diffed by the monitor):
    obs_accesses   L2 demand accesses seen
    seq_hits       accesses whose previous line (line-1) was among the last
                   RECENT_ADDR_BUFFER lines  -> sequential_score
    stride_trained accesses to a PC whose table entry already holds a stride
    stride_matches ... of which the new stride equals the entry's stride;
                   stride_matches / obs_accesses -> stride_confidence (the
                   fraction of ALL accesses that continue an established
                   stride, so irregular streams score near 0, not "unknown")
"""

import collections
import math

import sim_config as cfg

LINE = cfg.LINE_SIZE


class StrideTable:
    """Small set-associative table keyed by PC. Entry: [pc, last_line, stride,
    confidence, lru]. Strides are in cache lines."""

    def __init__(self, size=None, ways=None, conf_max=None, max_stride=None):
        size = cfg.STRIDE_TABLE_SIZE if size is None else size
        self.ways = cfg.STRIDE_TABLE_WAYS if ways is None else ways
        if size % self.ways:
            raise ValueError("table size must be a multiple of ways")
        self.n_sets = size // self.ways
        self.sets = [[] for _ in range(self.n_sets)]
        self.conf_max = cfg.PF_CONF_MAX if conf_max is None else conf_max
        self.conf_min = math.ceil(cfg.PF_CONFIDENCE_THRESHOLD * self.conf_max)
        self.max_stride = cfg.PF_MAX_STRIDE_LINES if max_stride is None else max_stride
        self._tick = 0

    def _index(self, pc):
        # xor-fold so PCs that differ only in high bits still spread out
        return (pc ^ (pc >> 4) ^ (pc >> 8) ^ (pc >> 12)) % self.n_sets

    def update(self, pc, line):
        """Train on one access. Returns (trained, matched, stride, confident):
          trained   : the entry already had a stride before this access
          matched   : this access continued that stride
          stride    : the entry's stride after the update (0 = none yet)
          confident : confidence >= the threshold after the update
        """
        entries = self.sets[self._index(pc)]
        self._tick += 1
        for e in entries:
            if e[0] == pc:
                break
        else:
            if len(entries) >= self.ways:
                entries.remove(min(entries, key=lambda x: x[4]))
            entries.append([pc, line, 0, 0, self._tick])
            return False, False, 0, False

        e[4] = self._tick
        delta = line - e[1]
        if delta == 0:                      # same line again: no information
            return False, False, e[2], e[3] >= self.conf_min

        trained = e[2] != 0
        matched = trained and delta == e[2]
        if matched:
            if e[3] < self.conf_max:
                e[3] += 1
        else:
            if e[3] > 0:
                e[3] -= 1                   # tolerate a stray access
            if e[3] == 0:                   # lost confidence: adopt the new stride
                e[2] = delta if abs(delta) <= self.max_stride else 0
        e[1] = line
        return trained, matched, e[2], e[3] >= self.conf_min


class PrefetchUnit:
    COUNTERS = ("obs_accesses", "seq_hits", "stride_trained", "stride_matches")

    def __init__(self, mode=None, distance=None, table=None):
        self.table = table if table is not None else StrideTable()
        self.mode = "off"
        self.distance = 0
        self.set_config(cfg.PF_MODE_DEFAULT if mode is None else mode, distance)
        self._recent = collections.deque()
        self._recent_cnt = {}
        self.stats = dict.fromkeys(self.COUNTERS, 0)

    # ------------------------------------------------------------------
    # Runtime control (the "near-zero overhead" reconfiguration)
    # ------------------------------------------------------------------
    @staticmethod
    def resolve(mode, distance=None):
        """Validate a (mode, distance) request and return the normalised pair
        without changing anything ("off" -> distance 0; no distance -> the
        mode's default)."""
        if mode not in cfg.PF_MODES:
            raise ValueError(f"unknown prefetch mode {mode!r}; choose from {cfg.PF_MODES}")
        if mode == "off":
            distance = 0
        elif distance is None:
            distance = cfg.PF_DISTANCE_DEFAULT[mode]
        elif distance not in cfg.PF_DISTANCE_LADDER:
            raise ValueError(f"distance {distance} not in {cfg.PF_DISTANCE_LADDER}")
        return mode, distance

    def set_config(self, mode, distance=None):
        self.mode, self.distance = self.resolve(mode, distance)

    def config(self):
        return self.mode, self.distance

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------
    def reset_counters(self):
        for k in self.stats:
            self.stats[k] = 0

    def snapshot(self):
        d = dict(self.stats)
        d["mode"] = self.mode
        d["distance"] = self.distance
        return d

    # ------------------------------------------------------------------
    # The hook Cache calls on every accepted L2 demand access
    # ------------------------------------------------------------------
    def observe(self, addr, pc, hit, now):
        """Train the detectors; return byte addresses to prefetch (maybe none)."""
        line = addr // LINE
        st = self.stats
        st["obs_accesses"] += 1

        # sequential detector: was the previous line touched recently?
        if (line - 1) in self._recent_cnt:
            st["seq_hits"] += 1
        self._remember(line)

        # stride detector (trains in every mode)
        trained, matched, stride, confident = self.table.update(pc, line)
        if trained:
            st["stride_trained"] += 1
            if matched:
                st["stride_matches"] += 1

        mode = self.mode
        if mode == "off":
            return ()
        d = self.distance
        if mode == "next-line":
            return [(line + k) * LINE for k in range(1, d + 1)]
        # stride
        if not confident or stride == 0:
            return ()
        return [t * LINE for t in (line + stride * k for k in range(1, d + 1)) if t >= 0]

    def _remember(self, line):
        recent, cnt = self._recent, self._recent_cnt
        if len(recent) >= cfg.RECENT_ADDR_BUFFER:
            old = recent.popleft()
            c = cnt[old] - 1
            if c:
                cnt[old] = c
            else:
                del cnt[old]
        recent.append(line)
        cnt[line] = cnt.get(line, 0) + 1
