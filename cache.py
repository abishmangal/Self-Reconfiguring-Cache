"""
cache.py — Set-associative, write-back, write-allocate cache with LRU,
MSHRs, pending-fill tracking and prefetch bookkeeping.

A line is a list [valid, tag, dirty, prefetched, lru, ready]:
    ready = cycle at which the line's data has actually arrived. A line is
            allocated (tag installed) when its miss is issued, but a request
            that finds it before `ready` waits until `ready` (a secondary
            miss merged into the in-flight fill, counted as a late hit).

The L1 (fixed) and L2 (adaptive) use this same class. Every cache has a
downstream (another Cache or Memory) with the same interface:
    access(addr, now, is_write, is_prefetch, pc) -> Result(hit, completion, retry_at)
    writeback(addr, now)
    drain_time(), retire_all(now)

Rules the model follows:
  * A refused access (MSHRs full here or downstream) changes NO state and is
    not counted as an access/miss; the caller waits until retry_at and retries.
  * Fills are read requests downstream; dirty victims are written back
    downstream (posted: they use bus bandwidth, nobody waits for them).
  * The hierarchy is non-inclusive: flushing or evicting from L2 never touches
    L1 contents; a dirty L1 line later written back to L2 is simply allocated.
  * Counters are cumulative. The monitor diffs snapshots; it never resets.

Prefetcher hook (optional, set `cache.prefetcher`): an object with
    observe(addr, pc, hit, now) -> iterable of byte addresses to prefetch
It is called after every accepted demand access. Prefetches use the same
MSHRs and memory bandwidth, but may not take the last `pf_mshr_reserve` MSHRs.

Reconfiguration primitives (policy and overhead accounting live in
reconfig.py):
    flush_invalidate(now) -> (n_valid, n_dirty)   write back dirty, invalidate all
    set_assoc(new_assoc)                          rebuild sets; cache must be empty
    set_n_mshr(n)                                 resize the MSHR table
"""

import heapq

import sim_config as cfg
from memory import Result

# Line field indices
V, TAG, DIRTY, PF, LRU, READY = range(6)

COUNTERS = (
    "accesses", "hits", "misses",       # demand only; accesses = hits + misses
    "late_hits",                         # demand hits on a line still in flight
    "blocked",                           # refused attempts (not counted above)
    "writebacks_out", "writebacks_in",
    "pf_issued", "pf_useful", "pf_late", "pf_unused_evicted", "pf_dropped",
    "miss_latency_sum",                  # sum of (completion - now) over demand misses
    "occ_integral",                      # integral of MSHR occupancy over time
    "flushes",
)


class Cache:
    def __init__(self, name, size, assoc, latency, downstream, n_mshr,
                 prefetcher=None, pf_mshr_reserve=None):
        if downstream is None:
            raise ValueError(f"{name}: a cache needs a downstream")
        if n_mshr < 1:
            raise ValueError(f"{name}: n_mshr must be >= 1")
        self.name = name
        self.size = size
        self.latency = latency
        self.downstream = downstream
        self.n_mshr = n_mshr
        self.prefetcher = prefetcher
        self.pf_mshr_reserve = (cfg.PF_MSHR_RESERVE if pf_mshr_reserve is None
                                else pf_mshr_reserve)
        self._lru_clock = 0
        self.mshr = []          # heap of completion cycles of outstanding misses
        self._occ_t = 0         # time up to which occupancy has been integrated
        self._build(assoc)
        self.stats = dict.fromkeys(COUNTERS, 0)

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------
    def _build(self, assoc):
        if assoc < 1 or self.size % (assoc * cfg.LINE_SIZE) != 0:
            raise ValueError(f"{self.name}: size {self.size} not divisible into "
                             f"{assoc}-way sets of {cfg.LINE_SIZE}B lines")
        self.assoc = assoc
        self.n_sets = self.size // (assoc * cfg.LINE_SIZE)
        self.sets = [[[False, 0, False, False, 0, 0] for _ in range(assoc)]
                     for _ in range(self.n_sets)]

    def _decode(self, addr):
        line = addr // cfg.LINE_SIZE
        return line % self.n_sets, line // self.n_sets

    def _line_addr(self, set_idx, tag):
        return (tag * self.n_sets + set_idx) * cfg.LINE_SIZE

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------
    def reset_counters(self):
        for k in self.stats:
            self.stats[k] = 0

    def snapshot(self):
        d = dict(self.stats)
        d.update(mshr_occupancy=len(self.mshr), n_mshr=self.n_mshr,
                 assoc=self.assoc, n_sets=self.n_sets)
        return d

    def miss_rate(self):
        a = self.stats["accesses"]
        return self.stats["misses"] / a if a else 0.0

    def mshr_occupancy(self):
        return len(self.mshr)

    def count_valid(self):
        return sum(1 for cs in self.sets for line in cs if line[V])

    # ------------------------------------------------------------------
    # MSHRs
    # ------------------------------------------------------------------
    def retire_mshrs(self, now):
        """Free MSHRs whose miss completed by `now`, integrating occupancy
        over time exactly (for the monitor's average outstanding misses)."""
        heap = self.mshr
        t = self._occ_t
        integral = 0
        while heap and heap[0] <= now:
            done = heap[0]
            if done > t:
                integral += len(heap) * (done - t)
                t = done
            heapq.heappop(heap)
        if now > t:
            integral += len(heap) * (now - t)
            t = now
        self._occ_t = t
        self.stats["occ_integral"] += integral

    def drain_time(self):
        """Cycle by which every outstanding miss here and downstream is done."""
        t = max(self.mshr) if self.mshr else 0
        return max(t, self.downstream.drain_time())

    def retire_all(self, now):
        self.retire_mshrs(now)
        self.downstream.retire_all(now)

    def set_n_mshr(self, new_n):
        """Resize the MSHR table. Shrinking below the current occupancy is
        fine: new misses are refused until it drains. reconfig.py drains first."""
        if new_n < 1:
            raise ValueError(f"{self.name}: n_mshr must be >= 1")
        self.n_mshr = new_n

    # ------------------------------------------------------------------
    # Demand access
    # ------------------------------------------------------------------
    def access(self, addr, now, is_write=False, is_prefetch=False, pc=0):
        self.retire_mshrs(now)
        set_idx, tag = self._decode(addr)
        for line in self.sets[set_idx]:
            if line[V] and line[TAG] == tag:
                res = self._hit(line, now, is_write, is_prefetch)
                break
        else:
            res = self._miss(addr, set_idx, tag, now, is_write, is_prefetch)
        if (self.prefetcher is not None and not is_prefetch
                and res.completion is not None):
            self._train_prefetcher(addr, pc, res.hit, now)
        return res

    def _hit(self, line, now, is_write, is_prefetch):
        s = self.stats
        s["accesses"] += 1
        s["hits"] += 1
        ready = line[READY]
        completion = now + self.latency
        pending = ready > now
        if ready > completion:
            completion = ready
        if pending:
            s["late_hits"] += 1
        if not is_prefetch:
            if line[PF]:
                s["pf_useful"] += 1
                if pending:
                    s["pf_late"] += 1
                line[PF] = False
            if is_write:
                line[DIRTY] = True
        self._touch(line)
        return Result(True, completion, None)

    def _miss(self, addr, set_idx, tag, now, is_write, is_prefetch):
        heap = self.mshr
        if len(heap) >= self.n_mshr:
            self.stats["blocked"] += 1
            return Result(False, None, heap[0])

        t_down = now + self.latency          # tag check, then go downstream
        res = self.downstream.access(addr, t_down, False, is_prefetch, 0)
        if res.completion is None:           # downstream refused: nothing changed here
            self.stats["blocked"] += 1
            return Result(False, None, res.retry_at)
        completion = res.completion

        victim = self._victim(self.sets[set_idx], now)
        self._evict(victim, set_idx, t_down)
        self._install(victim, tag, is_write, is_prefetch, completion)
        heapq.heappush(heap, completion)

        s = self.stats
        if is_prefetch:
            s["pf_issued"] += 1
        else:
            s["accesses"] += 1
            s["misses"] += 1
            s["miss_latency_sum"] += completion - now
        return Result(False, completion, None)

    # ------------------------------------------------------------------
    # Lines
    # ------------------------------------------------------------------
    def _touch(self, line):
        self._lru_clock += 1
        line[LRU] = self._lru_clock

    def _victim(self, lines, now):
        """Invalid line first; else LRU among lines whose data has arrived;
        else plain LRU (a still-pending line, only possible in tiny sets)."""
        lru_any = lines[0]
        lru_ready = None
        for line in lines:
            if not line[V]:
                return line
            if line[LRU] < lru_any[LRU]:
                lru_any = line
            if line[READY] <= now and (lru_ready is None or line[LRU] < lru_ready[LRU]):
                lru_ready = line
        return lru_ready if lru_ready is not None else lru_any

    def _evict(self, line, set_idx, now):
        if line[V]:
            if line[DIRTY]:
                self.downstream.writeback(self._line_addr(set_idx, line[TAG]), now)
                self.stats["writebacks_out"] += 1
            if line[PF]:
                self.stats["pf_unused_evicted"] += 1
        line[V] = False

    def _install(self, line, tag, dirty, prefetched, ready):
        line[V] = True
        line[TAG] = tag
        line[DIRTY] = dirty
        line[PF] = prefetched
        line[READY] = ready
        self._touch(line)

    # ------------------------------------------------------------------
    # Writebacks from the level above (no MSHR, not a demand access)
    # ------------------------------------------------------------------
    def writeback(self, addr, now):
        self.stats["writebacks_in"] += 1
        set_idx, tag = self._decode(addr)
        lines = self.sets[set_idx]
        for line in lines:
            if line[V] and line[TAG] == tag:
                line[DIRTY] = True
                self._touch(line)
                return
        victim = self._victim(lines, now)
        self._evict(victim, set_idx, now)
        self._install(victim, tag, True, False, now)

    # ------------------------------------------------------------------
    # Prefetch
    # ------------------------------------------------------------------
    def _train_prefetcher(self, addr, pc, hit, now):
        for paddr in self.prefetcher.observe(addr, pc, hit, now):
            self.prefetch(paddr, now)

    def prefetch(self, addr, now):
        """Try to prefetch one line. Returns True if a fetch was issued."""
        self.retire_mshrs(now)
        set_idx, tag = self._decode(addr)
        for line in self.sets[set_idx]:
            if line[V] and line[TAG] == tag:
                self.stats["pf_dropped"] += 1
                return False
        if len(self.mshr) >= max(0, self.n_mshr - self.pf_mshr_reserve):
            self.stats["pf_dropped"] += 1
            return False
        res = self._miss(addr, set_idx, tag, now, False, True)
        if res.completion is None:
            self.stats["pf_dropped"] += 1
            return False
        return True

    # ------------------------------------------------------------------
    # Reconfiguration primitives
    # ------------------------------------------------------------------
    def flush_invalidate(self, now):
        """Write back every dirty line downstream and invalidate everything.
        Returns (n_valid, n_dirty). MSHRs must already be drained."""
        if self.mshr:
            raise RuntimeError(f"{self.name}: drain outstanding misses before flushing")
        n_valid = n_dirty = 0
        for set_idx, lines in enumerate(self.sets):
            for line in lines:
                if not line[V]:
                    continue
                n_valid += 1
                if line[DIRTY]:
                    n_dirty += 1
                    self.downstream.writeback(self._line_addr(set_idx, line[TAG]), now)
                line[V] = False
        self.stats["writebacks_out"] += n_dirty
        self.stats["flushes"] += 1
        return n_valid, n_dirty

    def set_assoc(self, new_assoc):
        """Rebuild the sets at a new associativity (same total capacity, so
        n_sets changes and the index/tag split moves). The cache must be empty
        and drained: call flush_invalidate() first."""
        if self.mshr:
            raise RuntimeError(f"{self.name}: drain outstanding misses first")
        if self.count_valid():
            raise RuntimeError(f"{self.name}: flush_invalidate() before changing associativity")
        self._build(new_assoc)
