"""
reconfig.py — Changes the adaptive L2's configuration safely, charges the
cost to the simulation clock, verifies the result and logs every switch.

    rc = Reconfigurator(system, monitor)          # monitor is optional
    rc.set_prefetch("stride", 4)                  # each returns a SwitchRecord,
    rc.set_n_mshr(32)                             # or None if nothing changed
    rc.set_assoc(4, reason="rule G")
    rc.overhead_table()

Call it from the Core.run on_window callback (between ops, so nothing is being
issued). The clock is advanced here, via core.drain() and core.charge(); the
callback does not need to return the stall.

Sequence for every switch
    validate -> (drain) -> (flush) -> change -> (wait) -> verify -> log
Validation happens first, so a bad request has no side effects.

Cost model (what is charged to the clock)
    prefetch mode/distance : PF_DISTANCE_STALL. A control-register write; no
                             drain, no data moves, in-flight prefetches finish.
    MSHR count             : measured drain + MSHR_DRAIN_STALL.
    associativity          : measured drain + max(walk, writeback wait), where
                             walk = n_lines * K_ASSOC_PER_LINE (visiting every
                             tag) and the writeback wait is how long the bus
                             needs to push the flush's dirty lines to memory.
                             The cache is then empty ("fresh") at the same
                             capacity, with the index/tag split moved.
Not charged at the switch, but a real cost of an associativity change: the
lines the workload has to fetch again afterwards. They are counted separately
as `refetch_misses` (first L2 miss to each line that was resident at the flush).
Lines that were resident but are never touched again cost nothing. A line that
comes back through a prefetch instead of a demand miss is not counted.

The L1 is never touched: the hierarchy is non-inclusive, so a dirty L1 line
written back later is simply allocated in the new L2.

Monitor interplay: no counter is ever reset, so no monitor rebase is needed.
The switch's cycles land in the NEXT window as overhead_cycles and are excluded
from IPC and the stall fractions. That next window is also the one with the
cold-start misses; each record notes its index in `window` so the controller
can skip it for classification (SKIP_WINDOW_AFTER_SWITCH).

Estimates (estimate_overhead) are what the controller may use to decide. They
leave out the measured drain and the refetch misses; the gap between estimate
and real cost is reported by overhead_table().
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import sim_config as cfg
from prefetch import PrefetchUnit

KNOBS = ("assoc", "n_mshr", "prefetch")


@dataclass
class SwitchRecord:
    index: int
    cycle: int                 # clock when the switch began
    window: int | None         # index of the next monitor window (None without a monitor)
    knob: str
    old: object
    new: object
    drain_cycles: int          # waiting for outstanding loads/misses
    stall_cycles: int          # charged on top of the drain
    est_cycles: int            # what the controller would have estimated
    n_valid: int = 0           # lines resident at the flush (assoc only)
    n_dirty: int = 0           # ... of which written back
    refetch_misses: int = 0    # first misses to flushed lines afterwards (assoc only)
    reason: str = ""

    @property
    def total_cycles(self):
        return self.drain_cycles + self.stall_cycles

    def to_row(self):
        d = asdict(self)
        d["total_cycles"] = self.total_cycles
        d["old"], d["new"] = str(self.old), str(self.new)
        return d


def estimate_overhead(system, knob):
    """The controller's cost estimate for one switch of `knob`, from config
    constants only (no measured drain, no refetch misses)."""
    if knob == "prefetch":
        return cfg.EST_OVERHEAD_PREFETCH_DIST
    if knob == "n_mshr":
        return cfg.EST_OVERHEAD_MSHR
    if knob == "assoc":
        return (system.l2.size // cfg.LINE_SIZE) * cfg.K_ASSOC_PER_LINE
    raise ValueError(f"unknown knob {knob!r}; choose from {KNOBS}")


class _RefetchTracker:
    """L2 observer: counts first misses to the lines that a flush discarded."""

    def __init__(self):
        self.lines = set()
        self.record = None

    def arm(self, lines, record):
        self.lines = set(lines)
        self.record = record

    def observe(self, addr, pc, hit, now):
        if self.lines and not hit:
            line = addr // cfg.LINE_SIZE
            if line in self.lines:
                self.lines.discard(line)
                self.record.refetch_misses += 1


class Reconfigurator:
    def __init__(self, system, monitor=None):
        self.sys = system
        self.monitor = monitor
        self.log: list[SwitchRecord] = []
        self._tracker = _RefetchTracker()
        system.l2.observers.append(self._tracker)

    # ------------------------------------------------------------------
    # public switches
    # ------------------------------------------------------------------
    def apply(self, knob, value, reason=""):
        if knob == "assoc":
            return self.set_assoc(value, reason)
        if knob == "n_mshr":
            return self.set_n_mshr(value, reason)
        if knob == "prefetch":
            mode, dist = (value, None) if isinstance(value, str) else value
            return self.set_prefetch(mode, dist, reason)
        raise ValueError(f"unknown knob {knob!r}; choose from {KNOBS}")

    def estimate(self, knob):
        return estimate_overhead(self.sys, knob)

    def set_prefetch(self, mode, distance=None, reason=""):
        pf = self.sys.pf
        new = PrefetchUnit.resolve(mode, distance)          # validates, no side effects
        old = pf.config()
        if new == old:
            return None
        rec = self._begin("prefetch", old, new, reason)
        pf.set_config(*new)
        rec.stall_cycles = self.sys.core.charge(cfg.PF_DISTANCE_STALL)
        if pf.config() != new:
            raise RuntimeError("prefetch change did not take effect")
        return self._commit(rec)

    def set_n_mshr(self, n, reason=""):
        if n not in cfg.MSHR_CHOICES:
            raise ValueError(f"n_mshr {n} not in {cfg.MSHR_CHOICES}")
        l2, core = self.sys.l2, self.sys.core
        old = l2.n_mshr
        if n == old:
            return None
        rec = self._begin("n_mshr", old, n, reason)
        rec.drain_cycles = core.drain()
        l2.set_n_mshr(n)
        rec.stall_cycles = core.charge(cfg.MSHR_DRAIN_STALL)
        if l2.n_mshr != n or l2.mshr:
            raise RuntimeError("MSHR change left the table inconsistent")
        return self._commit(rec)

    def set_assoc(self, assoc, reason=""):
        if assoc not in cfg.L2_ASSOC_CHOICES:
            raise ValueError(f"assoc {assoc} not in {cfg.L2_ASSOC_CHOICES}")
        l2, core, mem = self.sys.l2, self.sys.core, self.sys.memory
        old = l2.assoc
        if assoc == old:
            return None
        rec = self._begin("assoc", old, assoc, reason)

        rec.drain_cycles = core.drain()                  # nothing in flight from here on
        now = core.cycle
        resident = l2.resident_lines()
        writes0 = mem.stats["writes"]
        rec.n_valid, rec.n_dirty = l2.flush_invalidate(now)

        n_lines = l2.size // cfg.LINE_SIZE
        walk = n_lines * cfg.K_ASSOC_PER_LINE
        writeback_wait = max(0, mem.next_free_time - now)
        rec.stall_cycles = core.charge(max(walk, writeback_wait))

        l2.set_assoc(assoc)

        # verify: dirty data reached memory, the cache is empty and consistent
        if mem.stats["writes"] - writes0 != rec.n_dirty:
            raise RuntimeError("flush lost or duplicated dirty lines")
        if (l2.count_valid() or l2.mshr or core.rob or l2.assoc != assoc
                or l2.n_sets * l2.assoc * cfg.LINE_SIZE != l2.size):
            raise RuntimeError("associativity change left the L2 inconsistent")
        if mem.next_free_time > core.cycle:
            raise RuntimeError("flush writebacks still on the bus after the stall")

        self._tracker.arm(resident, rec)
        return self._commit(rec)

    # ------------------------------------------------------------------
    # bookkeeping
    # ------------------------------------------------------------------
    def _begin(self, knob, old, new, reason):
        return SwitchRecord(
            index=len(self.log), cycle=self.sys.core.cycle,
            window=len(self.monitor.history) if self.monitor is not None else None,
            knob=knob, old=old, new=new, drain_cycles=0, stall_cycles=0,
            est_cycles=estimate_overhead(self.sys, knob), reason=reason)

    def _commit(self, rec):
        self.log.append(rec)
        return rec

    def switches(self, knob=None):
        return [r for r in self.log if knob is None or r.knob == knob]

    def total_overhead_cycles(self):
        return sum(r.total_cycles for r in self.log)

    def log_rows(self):
        return [r.to_row() for r in self.log]

    def overhead_table(self, runtime_cycles=None):
        """One row per knob plus a total: switches, cycles charged, estimate,
        refetch misses and share of runtime."""
        runtime = self.sys.core.cycle if runtime_cycles is None else runtime_cycles
        rows = []
        for knob in KNOBS + ("all",):
            recs = self.log if knob == "all" else self.switches(knob)
            total = sum(r.total_cycles for r in recs)
            rows.append({
                "knob": knob,
                "switches": len(recs),
                "drain_cycles": sum(r.drain_cycles for r in recs),
                "stall_cycles": sum(r.stall_cycles for r in recs),
                "total_cycles": total,
                "estimated_cycles": sum(r.est_cycles for r in recs),
                "refetch_misses": sum(r.refetch_misses for r in recs),
                "pct_of_runtime": 100.0 * total / runtime if runtime else 0.0,
            })
        return rows
