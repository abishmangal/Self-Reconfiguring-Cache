"""
monitor.py — Runtime monitoring: turns cumulative counters into one feature
vector per window.

    mon = Monitor(system)
    system.core.run(ops, on_window=mon.on_window)      # monitor-only run
    mon.history                                        # list of Features

With a controller, call mon.sample(core, final) from your own on_window
callback and act on the returned Features.

What it reads: only hardware counters (core, caches, memory, the prefetch
unit's detectors) and the shadow tags it watches the L2 access stream with.
It never sees workload phase labels.

Windows are measured in WORKLOAD cycles: cycles spent in reconfiguration
(drain + stall) and in the end-of-run wait are reported separately
(overhead_cycles) and excluded from IPC and the stall fractions, so adaptation
cost never hides inside the signals the controller reads.

Timing contract with Core.run: the callback fires when a window has elapsed;
the monitor samples first, then the controller may reconfigure. So the
n_mshr / assoc / prefetch config recorded in a Features row is the one that
was in force DURING that window.

Features fields that can be undefined are None, never 0, so "no evidence" is
distinguishable from "measured zero": pf_* when nothing was prefetched,
seq_score / stride_conf with fewer than STREAM_MIN_SAMPLES L2 accesses, and
shadow miss rates with fewer than SHADOW_MIN_SAMPLES sampled accesses.

Shadow tags: tag-only LRU copies of the L2 at every candidate associativity,
same capacity, on a fixed random sample of sets, fed with the L2's own demand
access stream. They answer "how many misses would this stream have at 2/4/8
ways?" for the CURRENT access pattern, including the current associativity, so
estimates are compared like for like. They ignore prefetch fills and
in-flight timing, and measure steady-state behaviour (they do not include the
cold-start cost of switching; the controller adds that).
"""

from __future__ import annotations

import csv
import random
from dataclasses import asdict, dataclass, field

import sim_config as cfg
from memory import Memory


# ----------------------------------------------------------------------
# Shadow tags
# ----------------------------------------------------------------------
class ShadowTags:
    def __init__(self, assocs=None, n_sample=None):
        self.assocs = tuple(cfg.L2_ASSOC_CHOICES if assocs is None else assocs)
        n_sample = cfg.SHADOW_SAMPLE_SETS if n_sample is None else n_sample
        self.n_sets = {a: cfg.L2_SIZE // (a * cfg.LINE_SIZE) for a in self.assocs}
        # sampled set index -> LRU list of tags (MRU last). The sample is random
        # (so strided patterns cannot alias with it) but fixed (so runs repeat).
        self.sampled = {}
        for a in self.assocs:
            if n_sample > self.n_sets[a]:
                raise ValueError(f"cannot sample {n_sample} of {self.n_sets[a]} sets")
            picks = random.Random(f"shadow:{a}").sample(range(self.n_sets[a]), n_sample)
            self.sampled[a] = {s: [] for s in picks}
        self.acc = dict.fromkeys(self.assocs, 0)    # cumulative sampled accesses
        self.miss = dict.fromkeys(self.assocs, 0)   # cumulative sampled misses

    def observe(self, addr, pc, hit, now):
        line = addr // cfg.LINE_SIZE
        for a in self.assocs:
            n = self.n_sets[a]
            tags = self.sampled[a].get(line % n)
            if tags is None:
                continue
            tag = line // n
            self.acc[a] += 1
            if tag in tags:
                tags.remove(tag)
                tags.append(tag)
            else:
                self.miss[a] += 1
                tags.append(tag)
                if len(tags) > a:
                    del tags[0]


# ----------------------------------------------------------------------
# Feature vector
# ----------------------------------------------------------------------
@dataclass
class Features:
    window: int                    # index of this sample (0, 1, 2, ...)
    t_end: int                     # core cycle at the end of the window
    cycles: int                    # workload cycles in the window (overhead excluded)
    overhead_cycles: int           # reconfiguration cycles that fell in the window
    partial: bool                  # the final, shorter-than-a-window sample
    instructions: int

    ipc: float
    l2_mpki: float                 # L2 demand misses per 1000 instructions
    stall_frac: float              # (dep + rob + mshr stall cycles) / workload cycles
    dep_stall_frac: float          # dependent-load stall cycles / workload cycles
    bw_util: float                 # DRAM bus busy fraction (reads + writebacks)
    mshr_occ: float                # time-averaged outstanding L2 misses
    mshr_occ_frac: float           # ... / n_mshr
    l2_miss_latency: float | None  # mean cycles from request to data, demand misses

    # configuration in force during the window
    assoc: int
    n_mshr: int
    pf_mode: str | None
    pf_distance: int | None

    # prefetch effectiveness (None when nothing was prefetched)
    pf_issued: int
    pf_accuracy: float | None      # useful / issued (clamped to 1)
    pf_coverage: float | None      # useful / (useful + demand misses)
    pf_late_frac: float | None     # late / useful
    pf_unused_evicted: int         # prefetched lines evicted never used (pollution)

    # access-stream character (None when too few accesses)
    stream_samples: int
    seq_score: float | None        # fraction of accesses continuing a sequential run
    stride_conf: float | None      # fraction of ALL L2 accesses continuing their PC's stride

    # shadow-tag miss rates by associativity (None when too few samples)
    shadow_rate: dict = field(default_factory=dict)
    shadow_samples: dict = field(default_factory=dict)

    @property
    def shadow_valid(self):
        return bool(self.shadow_rate) and all(v is not None for v in self.shadow_rate.values())

    def to_row(self):
        d = asdict(self)
        for key in ("shadow_rate", "shadow_samples"):
            for a, v in d.pop(key).items():
                d[f"{key}_{a}"] = v
        return d


# ----------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------
_CORE_KEYS = ("cycle", "instructions", "stall_dep", "stall_rob", "stall_mshr",
              "stall_end", "drain_cycles", "reconfig_cycles")
_L2_KEYS = ("misses", "miss_latency_sum", "occ_integral", "pf_issued", "pf_useful",
            "pf_late", "pf_unused_evicted")
_PF_KEYS = ("obs_accesses", "seq_hits", "stride_matches")


def _ratio(num, den):
    return num / den if den > 0 else None


class Monitor:
    def __init__(self, system, shadow=True):
        self.sys = system
        self.shadow = ShadowTags() if shadow else None
        if self.shadow is not None:
            system.l2.observers.append(self.shadow)
        self.history = []
        self._window = 0
        self._prev = self._raw()

    # ------------------------------------------------------------------
    def _raw(self):
        s = self.sys
        raw = {}
        core = s.core.snapshot()
        for k in _CORE_KEYS:
            raw["core." + k] = core[k]
        l2 = s.l2.stats
        for k in _L2_KEYS:
            raw["l2." + k] = l2[k]
        raw["mem.bus_busy"] = s.memory.stats["bus_busy_cycles"]
        pf = s.pf.stats if hasattr(s.pf, "stats") else {}
        for k in _PF_KEYS:
            raw["pf." + k] = pf.get(k, 0)
        if self.shadow is not None:
            for a in self.shadow.assocs:
                raw[f"sh.acc.{a}"] = self.shadow.acc[a]
                raw[f"sh.miss.{a}"] = self.shadow.miss[a]
        return raw

    def rebase(self):
        """Take a new baseline. Call after system.reset_counters()."""
        self.sys.l2.retire_mshrs(self.sys.core.cycle)
        self._prev = self._raw()

    # ------------------------------------------------------------------
    def sample(self, core, final=False):
        """Close the current window and return its Features (None if no
        workload cycles elapsed since the last sample)."""
        s = self.sys
        s.l2.retire_mshrs(core.cycle)       # bring the occupancy integral up to date
        cur = self._raw()
        prev = self._prev
        d = {k: cur[k] - prev[k] for k in cur}
        if any(v < 0 for v in d.values()):
            raise RuntimeError("counters went backwards; call monitor.rebase() "
                               "after system.reset_counters()")
        self._prev = cur

        elapsed = d["core.cycle"]
        overhead = d["core.reconfig_cycles"] + d["core.drain_cycles"]
        cycles = elapsed - overhead - d["core.stall_end"]
        if cycles <= 0:
            return None

        instr = d["core.instructions"]
        dep = d["core.stall_dep"]
        stall = dep + d["core.stall_rob"] + d["core.stall_mshr"]
        misses = d["l2.misses"]
        issued, useful = d["l2.pf_issued"], d["l2.pf_useful"]

        acc = _ratio(useful, issued)
        samples = d["pf.obs_accesses"]
        enough = samples >= cfg.STREAM_MIN_SAMPLES

        shadow_rate, shadow_samples = {}, {}
        if self.shadow is not None:
            for a in self.shadow.assocs:
                n = d[f"sh.acc.{a}"]
                shadow_samples[a] = n
                shadow_rate[a] = (d[f"sh.miss.{a}"] / n
                                  if n >= cfg.SHADOW_MIN_SAMPLES else None)

        pf = s.pf
        f = Features(
            window=self._window, t_end=core.cycle, cycles=cycles,
            overhead_cycles=overhead, partial=final, instructions=instr,
            ipc=instr / cycles,
            l2_mpki=1000.0 * misses / instr if instr else 0.0,
            stall_frac=min(1.0, stall / cycles),
            dep_stall_frac=min(1.0, dep / cycles),
            bw_util=Memory.bus_util(d["mem.bus_busy"], cycles),
            mshr_occ=d["l2.occ_integral"] / elapsed,
            mshr_occ_frac=d["l2.occ_integral"] / elapsed / s.l2.n_mshr,
            l2_miss_latency=_ratio(d["l2.miss_latency_sum"], misses),
            assoc=s.l2.assoc, n_mshr=s.l2.n_mshr,
            pf_mode=getattr(pf, "mode", None), pf_distance=getattr(pf, "distance", None),
            pf_issued=issued,
            pf_accuracy=None if acc is None else min(1.0, acc),
            pf_coverage=_ratio(useful, useful + misses),
            pf_late_frac=_ratio(d["l2.pf_late"], useful),
            pf_unused_evicted=d["l2.pf_unused_evicted"],
            stream_samples=samples,
            seq_score=_ratio(d["pf.seq_hits"], samples) if enough else None,
            stride_conf=_ratio(d["pf.stride_matches"], samples) if enough else None,
            shadow_rate=shadow_rate, shadow_samples=shadow_samples,
        )
        self._window += 1
        self.history.append(f)
        return f

    def on_window(self, core, final):
        """Core.run callback for monitor-only runs (adds no stall)."""
        self.sample(core, final)
        return 0

    # ------------------------------------------------------------------
    # Convenience for the controller and for reports
    # ------------------------------------------------------------------
    def last(self):
        return self.history[-1] if self.history else None

    def running_mean(self, name, n=None):
        """Mean of a Features field over the last n samples (default
        AVG_WINDOW), ignoring partial windows and None values; None if no data."""
        n = cfg.AVG_WINDOW if n is None else n
        vals = [getattr(f, name) for f in self.history[-n:] if not f.partial]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def to_rows(self):
        return [f.to_row() for f in self.history]

    def write_csv(self, path):
        rows = self.to_rows()
        if not rows:
            return
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
