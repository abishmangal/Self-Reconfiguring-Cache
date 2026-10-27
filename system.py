"""
system.py — Builds the memory hierarchy (Memory <- L2 <- L1 <- Core) from
sim_config, so tests, experiments and sim.py all construct it the same way.

    s = build_system(l2_assoc=4, l2_mshr=8)
    s.core.run(workload.ops(), on_window=my_callback)
    s.snapshot()            # every counter, for the monitor
    s.check_invariants()    # raises AssertionError if accounting is inconsistent
"""

from dataclasses import dataclass

import sim_config as cfg
from cache import Cache
from core import Core
from memory import Memory


@dataclass
class System:
    memory: Memory
    l2: Cache
    l1: Cache
    core: Core

    def snapshot(self):
        return {
            "cycle": self.core.cycle,
            "core": self.core.snapshot(),
            "l1": self.l1.snapshot(),
            "l2": self.l2.snapshot(),
            "mem": self.memory.snapshot(),
        }

    def reset_counters(self):
        """Zero all counters (e.g. after a warm-up pass). State and time are kept."""
        self.core.reset_counters()
        self.l1.reset_counters()
        self.l2.reset_counters()
        self.memory.reset_counters()

    def check_invariants(self):
        """Counter conservation laws. Valid once the run has ended or been
        drained (no accesses in flight between levels)."""
        c, a, b, m = self.core.stats, self.l1.stats, self.l2.stats, self.memory.stats

        def eq(x, y, what):
            assert x == y, f"invariant violated: {what}: {x} != {y}"

        eq(a["accesses"], c["loads"] + c["stores"], "L1 accesses == core loads + stores")
        eq(a["hits"] + a["misses"], a["accesses"], "L1 hits + misses == accesses")
        eq(b["accesses"], a["misses"], "L2 accesses == L1 misses")
        eq(b["hits"] + b["misses"], b["accesses"], "L2 hits + misses == accesses")
        eq(m["reads"], b["misses"], "memory reads == L2 demand misses")
        eq(m["prefetch_reads"], b["pf_issued"], "memory prefetch reads == L2 prefetches issued")
        eq(a["writebacks_out"], b["writebacks_in"], "L1 writebacks out == L2 writebacks in")
        eq(m["writes"], b["writebacks_out"], "memory writes == L2 writebacks out")


def build_system(l2_assoc=None, l2_mshr=None, l1_mshr=None,
                 dram_latency=None, bus_cycles=None,
                 width=None, rob_size=None, l2_prefetcher=None):
    memory = Memory(latency=dram_latency, bus_cycles=bus_cycles)
    l2 = Cache("L2", cfg.L2_SIZE,
               cfg.L2_ASSOC_DEFAULT if l2_assoc is None else l2_assoc,
               cfg.L2_LATENCY, memory,
               n_mshr=cfg.MSHR_DEFAULT if l2_mshr is None else l2_mshr,
               prefetcher=l2_prefetcher)
    l1 = Cache("L1", cfg.L1_SIZE, cfg.L1_ASSOC, cfg.L1_LATENCY, l2,
               n_mshr=cfg.L1_MSHR if l1_mshr is None else l1_mshr)
    core = Core(l1, width=width, rob_size=rob_size)
    return System(memory=memory, l2=l2, l1=l1, core=core)
