"""
check_controller.py — Validate controller.py.

Run:  python check_controller.py          (all)
      python check_controller.py -k assoc (only names containing "assoc")

Part 1 drives the controller with hand-built feature vectors and a stub
reconfigurator, so each rule and gate is tested in isolation: when a rule fires,
when it must not (including missing evidence), hysteresis, the window skipped
after a switch, residency, the utility test, ordering, and that the controller
never touches the workload.
Part 2 runs whole simulations and checks the outcomes the design promises.
"""

import ast
import os
import sys
from types import SimpleNamespace

import sim
import sim_config as cfg
from check_base import main
from controller import Controller, ControllerParams, classify
from monitor import Features
from workload import Workload

WINDOW = 10_000


# ----------------------------------------------------------------------
# helpers: feature vectors and a stub reconfigurator
# ----------------------------------------------------------------------
class StubRC:
    def __init__(self):
        self.calls = []

    def estimate(self, knob):
        return {"prefetch": cfg.EST_OVERHEAD_PREFETCH_DIST, "n_mshr": cfg.EST_OVERHEAD_MSHR,
                "assoc": (cfg.L2_SIZE // cfg.LINE_SIZE) * cfg.K_ASSOC_PER_LINE}[knob]

    def apply(self, knob, value, reason=""):
        self.calls.append((knob, value, reason))
        return SimpleNamespace(old=None, new=value)


def ctl(**kw):
    return Controller(None, None, StubRC(), **kw)


def feat(window=0, **kw):
    """A quiet compute window; override fields to build other situations."""
    f = dict(window=window, t_end=(window + 1) * WINDOW, cycles=WINDOW, overhead_cycles=0,
             partial=False, instructions=40_000, ipc=4.0, l2_mpki=0.0, stall_frac=0.0,
             dep_stall_frac=0.0, bw_util=0.0, mshr_occ=0.0, mshr_occ_frac=0.0,
             l2_miss_latency=None, assoc=8, n_mshr=16, pf_mode="off", pf_distance=0,
             pf_issued=0, pf_accuracy=None, pf_coverage=None, pf_late_frac=None,
             pf_unused_evicted=0, stream_samples=0, seq_score=None, stride_conf=None,
             shadow_rate={}, shadow_samples={})
    f.update(kw)
    return Features(**f)


def stream(**kw):      # independent misses, pool full, bus busy but not saturated
    base = dict(stall_frac=1.0, ipc=0.15, l2_mpki=500, bw_util=0.66, mshr_occ=15.7,
                mshr_occ_frac=0.98, stream_samples=700, seq_score=1.0, stride_conf=1.0)
    base.update(kw)
    return feat(**base)


def paced(**kw):       # regular stream, latency exposed, pool mostly idle
    base = dict(stall_frac=0.8, bw_util=0.3, mshr_occ=4.0, mshr_occ_frac=0.25,
                stream_samples=400, seq_score=1.0, stride_conf=1.0)
    base.update(kw)
    return feat(**base)


def pointer(**kw):     # dependent chain
    base = dict(stall_frac=1.0, dep_stall_frac=1.0, bw_util=0.05, mshr_occ=1.0,
                mshr_occ_frac=0.06, stream_samples=46, seq_score=0.0, stride_conf=0.0)
    base.update(kw)
    return feat(**base)


def conflict(**kw):    # thrashing at 2-way; the shadow says 8-way would hold it
    base = dict(assoc=2, stall_frac=1.0, bw_util=0.4, mshr_occ=8, mshr_occ_frac=0.5,
                stream_samples=600, seq_score=None, stride_conf=None,
                shadow_rate={2: 1.0, 4: 1.0, 8: 0.05}, shadow_samples={2: 150, 4: 300, 8: 600})
    base.update(kw)
    return feat(**base)


def rules(f, c=None):
    return {pr.rule: pr for pr in (c or ctl()).propose(f)}


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


# ----------------------------------------------------------------------
# part 1: rules
# ----------------------------------------------------------------------
def test_regime_classification():
    p = ControllerParams()
    assert classify(feat(stall_frac=0.05), p) == "compute"
    assert classify(feat(stall_frac=0.9, dep_stall_frac=0.9), p) == "latency"
    assert classify(feat(stall_frac=0.9), p) == "memory"
    assert classify(feat(stall_frac=0.25), p) == "uncertain"
    assert classify(stream(), p) == classify(stream(bw_util=0.99), p) == "memory", \
        "the label must not move just because the controller changed the bus load"


def test_prefetch_on_rules():
    r = rules(paced())
    assert r["pf-on"].value == ("stride", cfg.PF_DISTANCE_DEFAULT["stride"])
    assert approx(r["pf-on"].saved_per_window, WINDOW * 0.8 * cfg.PF_EXPECTED_HIT_STRIDE)
    assert rules(paced(stride_conf=None))["pf-on"].value[0] == "next-line", \
        "sequential but no stride evidence yet: next-line"
    for why, f in {
        "stream not regular enough": paced(stride_conf=0.5, seq_score=0.5),
        "no spare MSHRs": paced(mshr_occ_frac=0.9),
        "bus saturated": paced(bw_util=0.85),
        "dependent chain": paced(dep_stall_frac=0.6),
        "memory not hurting": paced(stall_frac=0.1),
        "no evidence": paced(stride_conf=None, seq_score=None),
    }.items():
        assert "pf-on" not in rules(f), why
    assert rules(pointer()) == {}, "nothing helps a pointer chase"


def test_prefetch_off_lower_raise_rules():
    on = dict(pf_mode="stride", pf_distance=4)
    r = rules(paced(**on, pf_issued=100, pf_accuracy=0.1, pf_coverage=0.5, pf_late_frac=0.2))
    assert r["pf-off"].fast and r["pf-off"].urgent and r["pf-off"].exempt
    assert r["pf-lower"].value == ("stride", 2) and r["pf-lower"].urgent

    r = rules(paced(**on, pf_issued=100, pf_accuracy=0.35, pf_coverage=0.5))
    assert "pf-off" not in r and r["pf-lower"].value == ("stride", 2)
    assert "pf-off" in rules(pointer(**on)), "both stream scores below PF_IRREGULAR"
    assert "pf-off" in rules(paced(**on, pf_issued=20, pf_accuracy=0.8, pf_coverage=0.05))
    assert "pf-off" not in rules(paced(**on, pf_issued=3, pf_accuracy=0.8, pf_coverage=0.05)), \
        "too few prefetches to judge coverage"
    assert "pf-lower" in rules(paced(**on, bw_util=0.9, pf_issued=100, pf_accuracy=0.6, pf_coverage=0.5))
    assert "pf-lower" not in rules(paced(**on, bw_util=0.9, pf_issued=100, pf_accuracy=0.9,
                                         pf_coverage=0.5, pf_late_frac=0.1))
    assert "pf-lower" not in rules(paced(pf_mode="stride", pf_distance=1, pf_issued=100,
                                         pf_accuracy=0.35, pf_coverage=0.5)), "already at the bottom"

    good = dict(pf_issued=100, pf_accuracy=0.95, pf_coverage=0.8, pf_late_frac=0.9)
    r = rules(paced(**on, **good))
    assert r["pf-raise"].value == ("stride", 8) and r["pf-raise"].saved_per_window > 0
    assert "pf-raise" not in rules(paced(pf_mode="stride", pf_distance=16, **good)), "ladder top"
    assert "pf-raise" not in rules(paced(**on, **{**good, "pf_late_frac": 0.2})), "not late"
    assert "pf-raise" not in rules(paced(**on, bw_util=0.85, **good))
    assert "pf-raise" not in rules(paced(**on, mshr_occ_frac=0.9, **good))
    assert rules(paced(**on, pf_issued=0)).get("pf-off") is None, "no prefetches, no evidence"


def test_mshr_rules():
    r = rules(stream())
    speedup = min(2.0, 1 / 0.66)
    assert r["mshr-raise"].value == 32
    assert approx(r["mshr-raise"].saved_per_window, WINDOW * 1.0 * (1 - 1 / speedup))
    assert "mshr-raise" not in rules(stream(n_mshr=32)), "ladder top"
    assert "mshr-raise" not in rules(stream(dep_stall_frac=0.6)), "misses cannot overlap"
    assert "mshr-raise" not in rules(stream(bw_util=0.85)), "bus is the limit"
    assert "mshr-raise" not in rules(stream(pf_mode="stride", pf_distance=2, pf_issued=100,
                                            pf_accuracy=0.1)), "junk prefetch would flood the pool"
    assert "mshr-raise" in rules(stream(pf_mode="stride", pf_distance=2, pf_issued=100,
                                        pf_accuracy=0.9))

    idle = feat(mshr_occ=1.0, mshr_occ_frac=1 / 16)
    assert "mshr-shrink" not in rules(idle), "shrinking is off by default"
    c2 = ctl(enable_mshr_shrink=True)
    r2 = rules(idle, c2)
    assert r2["mshr-shrink"].value == 8 and r2["mshr-shrink"].exempt
    assert "mshr-shrink" not in rules(feat(mshr_occ=6.0, mshr_occ_frac=0.375), c2), "pool in use"
    assert "mshr-shrink" not in rules(feat(mshr_occ=5.0, mshr_occ_frac=0.29), c2), \
        "smaller pool would not hold twice the occupancy"
    assert "mshr-shrink" not in rules(feat(n_mshr=4, mshr_occ=0.1, mshr_occ_frac=0.02), c2), "ladder bottom"


def test_assoc_rules():
    r = rules(conflict())
    assert r["assoc-better"].value == 8
    assert approx(r["assoc-better"].saved_per_window, WINDOW * 1.0 * (1 - 0.05))
    assert "assoc-better" not in rules(conflict(assoc=8)), "nothing beats 8-way"
    assert "assoc-better" not in rules(conflict(stall_frac=0.1)), "memory not hurting"
    assert "assoc-better" not in rules(conflict(shadow_rate={2: 1.0, 4: None, 8: 0.05})), "missing evidence"
    assert "assoc-better" not in rules(conflict(shadow_rate={2: 1.0, 4: 0.95, 8: 0.93})), "under 10% better"
    assert rules(conflict(shadow_rate={2: 1.0, 4: 0.5, 8: 0.3}))["assoc-better"].value == 8, "the best wins"
    assert rules(conflict(assoc=8, shadow_rate={2: 0.9, 4: 0.5, 8: 0.6}))["assoc-better"].value == 4, \
        "the same test run the other way is the revert rule"
    assert "assoc-better" not in rules(conflict(assoc=8, shadow_rate={2: 0.0, 4: 0.0, 8: 0.0})), "no misses"

    flat = feat(assoc=8, stall_frac=0.1, shadow_rate={2: 0.5, 4: 0.5, 8: 0.5},
                shadow_samples={2: 99, 4: 99, 8: 99})
    assert "assoc-shrink" not in rules(flat), "shrinking is off by default"
    r2 = rules(flat, ctl(enable_assoc_shrink=True))
    assert r2["assoc-shrink"].value == 4 and r2["assoc-shrink"].exempt
    worse = feat(assoc=8, shadow_rate={2: 0.9, 4: 0.55, 8: 0.5}, shadow_samples={2: 99, 4: 99, 8: 99})
    assert "assoc-shrink" not in rules(worse, ctl(enable_assoc_shrink=True)), "shrinking would lose"


# ----------------------------------------------------------------------
# part 1: gates
# ----------------------------------------------------------------------
def test_utility_gate():
    c = ctl(hysteresis_m=1)
    d = c.step(conflict(stall_frac=0.25, shadow_rate={2: 1.0, 4: 1.0, 8: 0.89}))
    assert d.action == "" and "utility" in d.blocked and c.rc.calls == [], \
        "11% fewer misses at 25% stall does not repay a flush"
    c = ctl(hysteresis_m=1)
    c.step(conflict())
    assert c.rc.calls[0][:2] == ("assoc", 8)


def test_hysteresis():
    c = ctl()                                           # M = 3
    done = []
    for w, f in enumerate([stream(window=0), stream(window=1), feat(window=2),
                           stream(window=3), stream(window=4), stream(window=5)]):
        c.step(f)
        done.append(len(c.rc.calls))
    assert done == [0, 0, 0, 0, 0, 1], "a gap resets the streak; three in a row fires"

    c = ctl()                                           # damage control is faster
    on = dict(pf_mode="stride", pf_distance=4)
    c.step(pointer(window=0, **on))
    assert c.rc.calls == []
    c.step(pointer(window=1, **on))
    assert c.rc.calls == [("prefetch", ("off", 0), "pf-off")]


def test_skip_window_and_residency():
    c = ctl(hysteresis_m=1)
    assert c.step(stream(window=0)).action.startswith("n_mshr")
    d1 = c.step(stream(window=1))
    assert d1.skipped and d1.regime is None, "the window after a switch is ignored"
    for w in (2, 3, 4):
        d = c.step(conflict(window=w))
        assert d.action == "" and "residency" in d.blocked
    assert c.step(conflict(window=5)).action.startswith("assoc"), "allowed after MIN_RESIDENCY_K"

    c = ctl(hysteresis_m=1)                             # heavy and prefetch clocks are separate
    c.step(stream(window=0))
    assert c.step(paced(window=2)).action.endswith("(pf-on)")

    c = ctl(hysteresis_m=1, residency_k_pf=5)
    c.step(paced(window=0))
    good = dict(pf_mode="stride", pf_distance=2, pf_issued=100, pf_accuracy=0.95,
                pf_coverage=0.8, pf_late_frac=0.9)
    assert "residency" in c.step(paced(window=2, **good)).blocked
    assert c.step(paced(window=5, **good)).action.endswith("(pf-raise)")


def test_one_change_per_window_urgent_first_then_net_benefit():
    c = ctl(hysteresis_m=1)
    c.step(conflict(window=0, pf_mode="stride", pf_distance=4, pf_issued=50,
                    pf_accuracy=0.1, pf_coverage=0.5))
    assert [x[2] for x in c.rc.calls] == ["pf-off"], \
        "damage control goes first even though an associativity win is ripe too"

    c = ctl(hysteresis_m=1)
    c.step(stream(assoc=2, shadow_rate={2: 1.0, 4: 1.0, 8: 0.05}, shadow_samples={2: 99, 4: 99, 8: 99}))
    assert [x[:2] for x in c.rc.calls] == [("assoc", 8)], \
        "the bigger net win (associativity) is not held up behind the cheap MSHR change"


def test_regime_confirmation_needs_hysteresis():
    mem = dict(stall_frac=0.5, bw_util=0.5, mshr_occ_frac=0.5, mshr_occ=8)
    seq = (["c"] * 3 + ["m", "m", "c"] + ["m"] * 3 + ["l"] * 3)

    def make(tag, w):
        if tag == "c":
            return feat(window=w)
        return feat(window=w, **mem) if tag == "m" else pointer(window=w)

    c = ctl()
    for w, tag in enumerate(seq):
        c.step(make(tag, w))
    assert c.rc.calls == []
    assert c.regime_changes == [(8, "compute", "memory"), (11, "memory", "latency")], \
        "a regime is confirmed only after M windows; a two-window blip is ignored"


def test_on_window_ignores_missing_partial_and_final():
    class FakeMonitor:
        def __init__(self, items):
            self.items = list(items)

        def sample(self, core, final):
            return self.items.pop(0)

    c = Controller(None, FakeMonitor([None, feat(window=0, partial=True), feat(window=1),
                                      feat(window=2)]), StubRC())
    assert c.on_window(None, False) == 0 and not c.trace          # no cycles elapsed
    assert c.on_window(None, False) == 0 and not c.trace          # partial window
    assert c.on_window(None, True) == 0 and not c.trace           # end of run
    assert c.on_window(None, False) == 0 and len(c.trace) == 1


def test_parameters_and_independence_from_the_workload():
    try:
        ctl(bogus=1)
        raise AssertionError("unknown parameter must be rejected")
    except ValueError:
        pass
    assert ControllerParams().hysteresis_m == cfg.HYSTERESIS_M
    assert ControllerParams(hysteresis_m=1).hysteresis_m == 1

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller.py")
    tree = ast.parse(open(path).read())
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "dataclasses", "sim_config"}, imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not any("boundar" in n.lower() or "phase" in n.lower() for n in names), \
        "the controller must never see workload phase information"


# ----------------------------------------------------------------------
# part 2: whole simulations
# ----------------------------------------------------------------------
def windows_of_boundaries(res, workload):
    """Window index in which each phase starts (scoring only)."""
    marks, cum = [], 0
    for f in res.monitor.history:
        cum += f.instructions
        marks.append(cum)
    return [next(i for i, m in enumerate(marks) if m >= b["start_op"]) for b in workload.boundaries]


def test_default_workload_end_to_end():
    w = Workload([("compute", 200_000), ("streaming", 40_000), ("compute", 200_000), ("pointer", 600)])
    b, d = sim.run_baseline(w), sim.run_dynamic(w)
    print(f"[default] baseline {b.cycles}, dynamic {d.cycles} ({100 * (d.cycles / b.cycles - 1):+.1f}%), "
          f"overhead {d.overhead_cycles} cycles, {len(d.reconfig.log)} switch(es)")
    assert d.instructions == b.instructions
    assert d.cycles < 0.95 * b.cycles
    assert [(r.knob, r.new) for r in d.reconfig.log] == [("n_mshr", 32)]
    assert d.overhead_cycles < 0.01 * d.cycles

    changes = d.controller.regime_changes
    assert [(a, z) for _, a, z in changes] == [("compute", "memory"), ("memory", "compute"),
                                               ("compute", "latency")]
    starts = windows_of_boundaries(d, w)[1:]             # streaming, compute, pointer
    delays = [conf - s for (conf, _, _), s in zip(changes, starts)]
    print(f"[default] regime confirmed {delays} windows after each true phase start")
    assert all(0 <= x <= cfg.HYSTERESIS_M + 1 for x in delays)


def test_nothing_to_gain_means_no_switches_and_identical_timing():
    for phases in ([("compute", 150_000)], [("pointer", 800)]):
        w = Workload(phases)
        b, d = sim.run_baseline(w), sim.run_dynamic(w)
        assert d.reconfig.log == [] and d.overhead_cycles == 0
        assert d.cycles == b.cycles, "monitoring and a quiet controller must not change timing"


def test_paced_stream_turns_prefetch_on_and_raises_distance():
    w = Workload([("compute", 80_000), ("streaming", 500_000)],
                 params={"streaming": {"compute_per_mem": 100}})
    b, d = sim.run_baseline(w), sim.run_dynamic(w)
    log = d.reconfig.log
    print(f"[paced] baseline {b.cycles}, dynamic {d.cycles}; " +
          ", ".join(f"{r.new}" for r in log))
    assert log[0].knob == "prefetch" and log[0].new == ("stride", cfg.PF_DISTANCE_DEFAULT["stride"])
    assert sum(r.reason == "pf-raise" for r in log) >= 2
    assert all(r.knob == "prefetch" for r in log), "MSHRs were never the limit here"
    assert d.cycles < 0.6 * b.cycles and d.overhead_cycles < 0.001 * d.cycles


def test_useless_prefetcher_is_switched_off_on_a_pointer_chase():
    w = Workload([("pointer", 1500)])
    start = {"pf_mode": "next-line", "pf_distance": 4}
    b, d = sim.run_baseline(w, start), sim.run_dynamic(w, start)
    assert [(r.knob, r.new, r.reason) for r in d.reconfig.log] == [("prefetch", ("off", 0), "pf-off")]
    assert d.reconfig.log[0].window <= 4, "switched off within a few windows"
    assert d.cycles < 1.05 * b.cycles


def test_conflict_phase_triggers_associativity_switch():
    w = Workload([("compute", 100_000), ("conflict", 100_000), ("compute", 100_000)])
    b, d = sim.run_baseline(w, {"l2_assoc": 2}), sim.run_dynamic(w, {"l2_assoc": 2})
    rec = d.reconfig.log[0]
    print(f"[assoc] baseline@2-way {b.cycles}, dynamic {d.cycles}; switch cost {rec.total_cycles}, "
          f"refetch misses {rec.refetch_misses}")
    assert [(r.knob, r.old, r.new, r.reason) for r in d.reconfig.log] == [("assoc", 2, 8, "assoc-better")]
    assert rec.refetch_misses > 0 and rec.total_cycles >= rec.est_cycles
    assert d.cycles < 0.4 * b.cycles


def test_short_phase_switch_loses_and_long_phase_wins():
    """The cost of adapting is real: a thrashing phase too short to repay the
    flush makes the adaptive run slower than doing nothing."""
    res = {}
    for n in (12_000, 45_000):
        w = Workload([("compute", 100_000), ("conflict", n), ("compute", 100_000)])
        b, d = sim.run_baseline(w, {"l2_assoc": 2}), sim.run_dynamic(w, {"l2_assoc": 2})
        res[n] = d.cycles / b.cycles
        assert [r.knob for r in d.reconfig.log] == ["assoc"]
    print(f"[break-even] dynamic/baseline: 12k-op phase {res[12_000]:.2f}, 45k-op phase {res[45_000]:.2f}")
    assert res[12_000] > 1.0, "short phase: adaptation hurts"
    assert res[45_000] < 0.6, "long phase: adaptation pays"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], globals()))
