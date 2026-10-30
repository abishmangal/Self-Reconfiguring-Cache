"""
check_reconfig.py — Validate reconfig.py.

Run:  python check_reconfig.py          (all)
      python check_reconfig.py -k assoc (only names containing "assoc")

Checks: each knob changes and is charged exactly what the cost model says,
no-ops and bad requests have no side effects, the flush writes back exactly the
dirty lines, switching with misses and prefetches in flight is safe, refetch
misses are counted for the lines that matter, the monitor sees the cost as
overhead, and the log / table conserve cycles.
"""

import sys

import sim_config as cfg
from cache import DIRTY, V
from check_base import L2_LINES, _paced, main, mem_ops, run
from monitor import Monitor
from reconfig import Reconfigurator, estimate_overhead
from system import build_system
from workload import OP_STORE


def dirty_lines(l2):
    return sum(1 for lines in l2.sets for line in lines if line[V] and line[DIRTY])


def fresh(**kw):
    s = build_system(**kw)
    return s, Reconfigurator(s)


# ----------------------------------------------------------------------
# prefetch and MSHR
# ----------------------------------------------------------------------
def test_prefetch_switch_is_cheap_and_touches_no_data():
    s, rc = fresh()
    run(s, mem_ops(range(300)))
    valid, writes, cyc = s.l2.count_valid(), s.memory.stats["writes"], s.core.cycle
    rec = rc.set_prefetch("stride", 4, reason="test")
    assert s.pf.config() == ("stride", 4)
    assert (rec.drain_cycles, rec.stall_cycles) == (0, cfg.PF_DISTANCE_STALL)
    assert rec.est_cycles == rec.total_cycles, "estimate is exact for prefetch"
    assert s.core.cycle == cyc + cfg.PF_DISTANCE_STALL
    assert s.core.stats["reconfig_cycles"] == cfg.PF_DISTANCE_STALL
    assert s.l2.count_valid() == valid and s.memory.stats["writes"] == writes
    assert (rec.old, rec.new, rec.reason) == (("off", 0), ("stride", 4), "test")
    assert rc.set_prefetch("stride") is not None, "a distance change is a switch too"
    assert s.pf.config() == ("stride", cfg.PF_DISTANCE_DEFAULT["stride"])


def test_mshr_switch_idle_and_with_misses_in_flight():
    s, rc = fresh()
    rec = rc.set_n_mshr(32)
    assert s.l2.n_mshr == 32 and (rec.drain_cycles, rec.stall_cycles) == (0, cfg.MSHR_DRAIN_STALL)
    assert rec.total_cycles == rec.est_cycles

    s2, rc2 = fresh()
    s2.core.run(iter(mem_ops(range(300))), max_ops=300)     # stop with misses outstanding
    assert s2.l2.mshr and s2.core.rob
    t_done = max(s2.l1.drain_time(), max(d for _, d in s2.core.rob))
    start = s2.core.cycle
    rec2 = rc2.set_n_mshr(8)
    print(f"[mshr] drain {rec2.drain_cycles} + fixed {rec2.stall_cycles} = {rec2.total_cycles} "
          f"cycles (controller estimate {rec2.est_cycles})")
    assert rec2.drain_cycles == t_done - start > 0
    assert s2.core.cycle == start + rec2.total_cycles
    assert not s2.l2.mshr and not s2.l1.mshr and not s2.core.rob and s2.l2.n_mshr == 8
    assert rec2.total_cycles > rec2.est_cycles, "the estimate leaves out the measured drain"
    run(s2, mem_ops(range(300, 600)))
    s2.check_invariants()


# ----------------------------------------------------------------------
# associativity
# ----------------------------------------------------------------------
def test_assoc_switch_cost_and_flush_exactness():
    s, rc = fresh()
    run(s, mem_ops(range(L2_LINES // 2), kind=OP_STORE))
    valid, dirty = s.l2.count_valid(), dirty_lines(s.l2)
    writes0, cyc0 = s.memory.stats["writes"], s.core.cycle
    sets0 = s.l2.n_sets
    # a run ends when its loads are done; store fills may still be in flight,
    # and the drain waits for them
    expected_drain = max(s.l1.drain_time(), cyc0) - cyc0
    rec = rc.set_assoc(4)
    walk = (s.l2.size // cfg.LINE_SIZE) * cfg.K_ASSOC_PER_LINE
    print(f"[assoc] flushed {rec.n_valid} lines ({rec.n_dirty} dirty): charged {rec.stall_cycles} "
          f"cycles (walk {walk})")
    assert (rec.n_valid, rec.n_dirty) == (valid, dirty) and dirty > 0
    assert s.memory.stats["writes"] - writes0 == dirty, "exactly the dirty lines are written back"
    assert rec.stall_cycles == max(walk, dirty * s.memory.bus_cycles) == walk
    assert rec.drain_cycles == expected_drain and rec.est_cycles == walk
    assert s.core.cycle == cyc0 + rec.total_cycles
    assert s.l2.assoc == 4 and s.l2.n_sets == 2 * sets0 and s.l2.count_valid() == 0
    assert s.l2.n_sets * s.l2.assoc * cfg.LINE_SIZE == s.l2.size, "capacity is unchanged"
    s.check_invariants()


def test_assoc_switch_waits_for_writebacks_when_bus_is_slow():
    s, rc = fresh(bus_cycles=2 * cfg.DRAM_BUS_CYCLES)
    run(s, mem_ops(range(L2_LINES), kind=OP_STORE))
    dirty = dirty_lines(s.l2)
    rec = rc.set_assoc(2)
    walk = (s.l2.size // cfg.LINE_SIZE) * cfg.K_ASSOC_PER_LINE
    print(f"[assoc] slow bus: {dirty} dirty lines -> charged {rec.stall_cycles} (walk {walk})")
    assert dirty * s.memory.bus_cycles > walk, "scenario must make the bus the bottleneck"
    assert rec.stall_cycles == dirty * s.memory.bus_cycles
    assert s.memory.next_free_time <= s.core.cycle
    s.check_invariants()


def test_assoc_refetch_misses_count_only_lines_that_come_back():
    n = L2_LINES // 2
    ops = mem_ops(range(n))

    s, rc = fresh()
    run(s, ops * 2)                                   # warm: the working set sits in L2
    rec = rc.set_assoc(4)
    assert rec.n_valid == n
    m0 = s.l2.stats["misses"]
    run(s, ops)
    assert s.l2.stats["misses"] - m0 == n == rec.refetch_misses
    m1 = s.l2.stats["misses"]
    run(s, ops)
    assert s.l2.stats["misses"] == m1 and rec.refetch_misses == n, "warm again: no more misses"

    s2, rc2 = fresh()
    run(s2, ops * 2)
    rec2 = rc2.set_assoc(4)
    run(s2, ops[: n // 2])                            # only half the lines are used again
    assert rec2.refetch_misses == n // 2, "lines never touched again cost nothing"
    s2.check_invariants()


def test_switching_with_misses_and_prefetches_in_flight_is_safe():
    for mode, dist in (("off", None), ("next-line", 8)):
        s = build_system(pf_mode=mode, pf_distance=dist)
        m = Monitor(s)
        rc = Reconfigurator(s, m)

        def cb(core, final):
            f = m.sample(core, final)
            if f is not None and f.window == 2:
                rc.set_assoc(2, reason="mid-stream")
                rc.set_n_mshr(32)
            return 0

        ops = _paced(mem_ops(range(3_000), kind=OP_STORE, pc=0x2000), 30)
        s.core.run(iter(ops), on_window=cb)
        s.check_invariants()
        assert s.core.snapshot()["instructions"] == len(ops), "every instruction still retires"
        assert len(rc.log) == 2 and (s.l2.assoc, s.l2.n_mshr) == (2, 32)
        s.core.drain()                          # fills still in flight at the end of the run
        assert not s.l2.mshr and not s.l1.mshr


# ----------------------------------------------------------------------
# monitor, log, estimates
# ----------------------------------------------------------------------
def test_monitor_reports_switch_cost_as_overhead():
    s = build_system()
    m = Monitor(s)
    rc = Reconfigurator(s, m)

    def cb(core, final):
        f = m.sample(core, final)
        if f is not None and f.window == 2:
            rc.set_assoc(4)
        return 0

    s.core.run(iter(mem_ops(range(3_000))), on_window=cb)
    rec = rc.log[0]
    assert rec.window == 3, "record notes the next window, the one the controller should skip"
    assert m.history[3].overhead_cycles == rec.total_cycles
    assert sum(f.overhead_cycles for f in m.history) == rec.total_cycles
    total = sum(f.cycles + f.overhead_cycles for f in m.history) + s.core.stats["stall_end"]
    assert total == s.core.cycle
    assert m.history[3].assoc == 4 and m.history[2].assoc == 8


def test_log_table_and_cycle_conservation():
    s, rc = fresh()
    s.core.run(iter(mem_ops(range(500))), max_ops=500)
    rc.set_n_mshr(8)
    rc.set_prefetch("next-line", 2)
    rc.set_assoc(4)
    rc.set_prefetch("off")
    rc.set_assoc(8)
    assert [r.index for r in rc.log] == list(range(5))
    assert [r.knob for r in rc.log] == ["n_mshr", "prefetch", "assoc", "prefetch", "assoc"]
    assert rc.total_overhead_cycles() == s.core.stats["reconfig_cycles"] + s.core.stats["drain_cycles"]
    table = {r["knob"]: r for r in rc.overhead_table()}
    assert [table[k]["switches"] for k in ("assoc", "n_mshr", "prefetch", "all")] == [2, 1, 2, 5]
    assert table["all"]["total_cycles"] == rc.total_overhead_cycles()
    assert abs(table["all"]["pct_of_runtime"] - 100 * rc.total_overhead_cycles() / s.core.cycle) < 1e-9
    assert table["assoc"]["total_cycles"] > table["n_mshr"]["total_cycles"] > table["prefetch"]["total_cycles"]
    print("[table] " + "; ".join(f"{k}: {table[k]['switches']} switches, {table[k]['total_cycles']} cycles"
                                 for k in ("prefetch", "n_mshr", "assoc")))
    assert len(rc.log_rows()) == 5 and rc.log_rows()[2]["knob"] == "assoc"


def test_noop_and_invalid_requests_have_no_side_effects():
    s, rc = fresh()
    run(s, mem_ops(range(200)))
    before = (s.core.cycle, dict(s.core.stats), s.l2.count_valid(), s.l2.assoc,
              s.l2.n_mshr, s.pf.config())
    assert rc.set_assoc(cfg.L2_ASSOC_DEFAULT) is None
    assert rc.set_n_mshr(cfg.MSHR_DEFAULT) is None
    assert rc.set_prefetch("off") is None
    for bad in (lambda: rc.set_assoc(3), lambda: rc.set_n_mshr(5), lambda: rc.set_prefetch("bogus"),
                lambda: rc.set_prefetch("stride", 3), lambda: rc.apply("cache_size", 1)):
        try:
            bad()
            raise AssertionError("bad request must raise")
        except ValueError:
            pass
    after = (s.core.cycle, dict(s.core.stats), s.l2.count_valid(), s.l2.assoc,
             s.l2.n_mshr, s.pf.config())
    assert before == after and rc.log == []


def test_estimates_follow_the_config():
    s, rc = fresh()
    assert estimate_overhead(s, "prefetch") == cfg.PF_DISTANCE_STALL
    assert estimate_overhead(s, "n_mshr") == cfg.EST_OVERHEAD_MSHR
    assert estimate_overhead(s, "assoc") == (cfg.L2_SIZE // cfg.LINE_SIZE) * cfg.K_ASSOC_PER_LINE
    assert rc.estimate("assoc") == estimate_overhead(s, "assoc")
    try:
        estimate_overhead(s, "l1_size")
        raise AssertionError("unknown knob")
    except ValueError:
        pass
    assert rc.apply("prefetch", "stride") is not None and s.pf.config()[0] == "stride"
    assert rc.apply("prefetch", ("next-line", 4)) is not None and s.pf.config() == ("next-line", 4)
    assert rc.apply("n_mshr", 4) is not None and rc.apply("assoc", 2) is not None


def test_verification_catches_a_broken_flush():
    s, rc = fresh()
    run(s, mem_ops(range(500), kind=OP_STORE))
    real_flush = s.l2.flush_invalidate

    def lying_flush(now):                 # reports one more dirty line than it wrote
        n_valid, n_dirty = real_flush(now)
        return n_valid, n_dirty + 1

    s.l2.flush_invalidate = lying_flush
    try:
        rc.set_assoc(4)
        raise AssertionError("the dirty-line check must fire")
    except RuntimeError as e:
        assert "dirty" in str(e)
    assert rc.log == [], "a failed switch is not logged as a success"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], globals()))
