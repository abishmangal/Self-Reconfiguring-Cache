"""
memory.py — DRAM model with latency and a bandwidth limit.

Two limits:
  1. Base latency per request: DRAM_LATENCY cycles from the moment the
     request wins the bus until the data is available.
  2. Bus occupancy: each line transfer (read or writeback) occupies the bus
     for DRAM_BUS_CYCLES cycles. The bus serializes transfers in the order
     the calls arrive (FIFO in call order, a simplification).

The bandwidth limit is what produces the "more MSHRs is not always
better" negative case: once the bus is saturated, extra outstanding
misses cannot hide latency.

Downstream interface (shared with Cache, so a Cache can sit on top of
either one):
    access(addr, now, is_write, is_prefetch, pc) -> Result
    writeback(addr, now)                         -> None
    drain_time()                                 -> 0 (no MSHRs here)
    retire_all(now)                              -> no-op

Counters are cumulative. The monitor takes differences between snapshots
and never resets them.
"""

from collections import namedtuple

import sim_config as cfg

# Result of a cache/memory access.
#   hit        : True if served without allocating a new miss
#   completion : cycle at which the data is available, or None if the
#                request was refused (MSHRs full)
#   retry_at   : when completion is None, the earliest cycle at which a
#                retry can succeed
Result = namedtuple("Result", "hit completion retry_at")


class Memory:
    COUNTERS = ("reads", "prefetch_reads", "writes", "bus_busy_cycles")

    def __init__(self, latency=None, bus_cycles=None):
        self.latency = cfg.DRAM_LATENCY if latency is None else latency
        self.bus_cycles = cfg.DRAM_BUS_CYCLES if bus_cycles is None else bus_cycles
        if self.latency <= 0 or self.bus_cycles <= 0:
            raise ValueError("latency and bus_cycles must be > 0")
        self.next_free_time = 0
        self.stats = dict.fromkeys(self.COUNTERS, 0)

    # ------------------------------------------------------------------
    # Counters and state
    # ------------------------------------------------------------------
    def reset_counters(self):
        """Zero the counters. Bus timing state is untouched."""
        for k in self.stats:
            self.stats[k] = 0

    def reset_timing(self):
        """Forget bus reservations (start of a new run)."""
        self.next_free_time = 0

    def snapshot(self):
        d = dict(self.stats)
        d["next_free_time"] = self.next_free_time
        return d

    @staticmethod
    def bus_util(busy_delta, elapsed_cycles):
        """Bus utilisation over a window: difference of bus_busy_cycles
        between two snapshots divided by elapsed cycles, clamped to 1.
        (Busy time is booked when a transfer is reserved, so a window can
        briefly book more than its length.)"""
        if elapsed_cycles <= 0:
            return 0.0
        return min(1.0, busy_delta / elapsed_cycles)

    # ------------------------------------------------------------------
    # Downstream interface
    # ------------------------------------------------------------------
    def _reserve(self, now):
        start = now if now > self.next_free_time else self.next_free_time
        self.next_free_time = start + self.bus_cycles
        self.stats["bus_busy_cycles"] += self.bus_cycles
        return start

    def access(self, addr, now, is_write=False, is_prefetch=False, pc=0):
        """Read one line. Never refused. Returns Result(True, completion, None)."""
        start = self._reserve(now)
        self.stats["prefetch_reads" if is_prefetch else "reads"] += 1
        return Result(True, start + self.latency, None)

    def writeback(self, addr, now):
        """Posted write of one line: occupies the bus, nobody waits for it."""
        self._reserve(now)
        self.stats["writes"] += 1

    def drain_time(self):
        return 0

    def retire_all(self, now):
        pass
