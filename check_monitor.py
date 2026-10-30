"""
check_monitor.py — Validate monitor.py.

Run:  python check_monitor.py          (all)
      python check_monitor.py -k shadow (only names containing "shadow")

Checks: counter conservation, per-phase signal signatures, reconfiguration
overhead kept out of the signals, the final partial window, configuration
recorded as in force during the window, shadow tags (ranking, independence of
the real configuration, agreement with real miss rates), None-for-unknown
fields, prefetch effectiveness fields, rebase after a counter reset, CSV.
"""

import csv
import functools
import os
import random
import sys
import tempfile

import sim_config as cfg
from check_base import L2_LINES, _paced, main, mem_ops, run
from monitor import Monitor
from system import build_system
from workload import OP_COMPUTE, Workload

PHASES = (("compute", 200_000), ("streaming", 15_000),
          ("compute", 200_000), ("pointer", 450))


@functools.lru_cache(maxsize=None)
def baseline_run():
    """One monitored run of a scaled-down default workload (shared by tests)."""
    w = Workload(PHASES)
    s = build_system()
    m = Monitor(s)
    s.core.run(w.ops(), on_window=m.on_window)
    s.check_invariants()
    return w, s, m


def phase_windows(w, m, kind):
    """Windows lying entirely inside a phase of this kind. Workload boundaries
    are used here only to SCORE the monitor, never by the monitor."""
    out, cum = [], 0
    for f in m.history:
        lo, cum = cum, cum + f.instructions
        if f.partial:
            continue
        for b in w.boundaries:
            if b["kind"] == kind and b["start_op"] <= lo and cum <= b["end_op"]:
                out.append(f)
    return out


def compute_ops(n):
    return [(OP_COMPUTE, None, False, 0)] * n


# ----------------------------------------------------------------------
def test_counters_conserved():
    w, s, m = baseline_run()
    h = m.history
    assert sum(f.instructions for f in h) == s.core.snapshot()["instructions"]
    total = sum(f.cycles + f.overhead_cycles for f in h) + s.core.stats["stall_end"]
    assert total == s.core.cycle, "every cycle is accounted for exactly once"
    misses = sum(round(f.l2_mpki * f.instructions / 1000) for f in h)
    assert misses == s.l2.stats["misses"]
    assert [f.partial for f in h] == [False] * (len(h) - 1) + [True]
    assert all(0 <= f.bw_util <= 1 and 0 <= f.stall_frac <= 1 for f in h)
    assert [f.window for f in h] == list(range(len(h)))
    print(f"[monitor] {len(h)} windows, total {s.core.cycle} cycles")


def test_phase_signatures():
    w, s, m = baseline_run()
    comp = phase_windows(w, m, "compute")
    stream = phase_windows(w, m, "streaming")
    ptr = phase_windows(w, m, "pointer")
    assert len(comp) >= 4 and len(stream) >= 8 and len(ptr) >= 6

    for f in comp[1:]:                       # skip the cold first window
        assert f.ipc > 3.5 and f.stall_frac < 0.05 and f.l2_mpki < 1 and f.bw_util < 0.05
    for f in stream:
        assert f.stall_frac > 0.9 and f.dep_stall_frac < 0.05
        assert 0.4 < f.bw_util <= 1 and f.mshr_occ_frac > 0.9
        assert 400 < f.l2_mpki < 600
        assert f.seq_score > 0.95 and f.stride_conf > 0.95
        assert f.l2_miss_latency > cfg.DRAM_LATENCY
    for f in ptr:
        assert f.dep_stall_frac > 0.9 and f.stall_frac > 0.9
        assert f.bw_util < 0.1 and f.mshr_occ_frac < 0.2
        assert f.seq_score < 0.05 and f.stride_conf < 0.05

    def mean(fs, name):
        return sum(getattr(f, name) for f in fs) / len(fs)
    print("[phases] phase      ipc  stall  dep   bw   occ  mpki")
    for name, fs in (("compute", comp[1:]), ("streaming", stream), ("pointer", ptr)):
        print(f"[phases] {name:<9} {mean(fs,'ipc'):5.2f} {mean(fs,'stall_frac'):5.2f} "
              f"{mean(fs,'dep_stall_frac'):5.2f} {mean(fs,'bw_util'):5.2f} "
              f"{mean(fs,'mshr_occ_frac'):5.2f} {mean(fs,'l2_mpki'):6.1f}")
    # streaming and pointer chasing look alike in stall_frac; dep_stall_frac and
    # the stream scores are what tell them apart
    assert mean(stream, "dep_stall_frac") < 0.05 < 0.9 < mean(ptr, "dep_stall_frac")


def test_unknown_fields_are_none():
    w, s, m = baseline_run()
    for f in phase_windows(w, m, "compute")[1:]:
        assert f.seq_score is None and f.stride_conf is None
        assert f.shadow_valid is False and f.pf_accuracy is None and f.pf_coverage is None
    for f in phase_windows(w, m, "pointer"):
        assert not f.shadow_valid and all(v is None for v in f.shadow_rate.values()), \
            "a few dozen accesses are too few for a shadow estimate"
        assert f.pf_accuracy is None            # mode is off: nothing issued
    for f in phase_windows(w, m, "streaming"):
        assert f.shadow_valid and f.pf_accuracy is None


def test_overhead_stays_out_of_signals():
    ops = compute_ops(40_000)
    s = build_system()
    m = Monitor(s)
    state = {"n": 0}

    def cb(core, final):
        m.sample(core, final)
        state["n"] += 1
        return 500 if state["n"] == 1 else 0

    s.core.run(iter(ops), on_window=cb, window_cycles=1_000)
    h = m.history
    assert h[1].overhead_cycles == 500 and h[1].cycles == 1_000
    assert all(abs(f.ipc - cfg.CORE_WIDTH) < 1e-9 for f in h if not f.partial), \
        "a 500-cycle reconfiguration stall must not lower IPC"
    assert sum(f.overhead_cycles for f in h) == s.core.stats["reconfig_cycles"] == 500

    # a drain is overhead too
    s2 = build_system()
    m2 = Monitor(s2)
    spent = []

    def cb2(core, final):
        m2.sample(core, final)
        if len(m2.history) == 3 and not final:
            spent.append(core.drain())
        return 0

    s2.core.run(iter(mem_ops(range(6_000))), on_window=cb2)
    assert spent and spent[0] > 0
    assert sum(f.overhead_cycles for f in m2.history) == spent[0]
    assert all(f.stall_frac <= 1 for f in m2.history)


def test_final_partial_window_and_tail():
    s = build_system()
    m = Monitor(s)
    s.core.run(iter(mem_ops(range(3_000))), on_window=m.on_window)
    last = m.history[-1]
    assert last.partial and not any(f.partial for f in m.history[:-1])
    assert s.core.stats["stall_end"] > 0, "the run ends with loads still in flight"
    assert sum(f.cycles + f.overhead_cycles for f in m.history) + s.core.stats["stall_end"] == s.core.cycle
    assert m.sample(s.core, True) is None, "no workload cycles since the last sample"


def test_config_recorded_as_in_force_during_window():
    s = build_system()
    m = Monitor(s)

    def cb(core, final):
        f = m.sample(core, final)
        if f is not None and f.window == 2:       # act AFTER sampling window 2
            s.pf.set_config("next-line", 4)
            s.l2.set_n_mshr(8)
            core.drain()
            s.l2.flush_invalidate(core.cycle)
            s.l2.set_assoc(4)
        return 0

    s.core.run(iter(_paced(mem_ops(range(2_000), pc=0x2000), 100)), on_window=cb)
    s.check_invariants()
    h = m.history
    for f in h[:3]:
        assert (f.assoc, f.n_mshr, f.pf_mode, f.pf_distance) == (8, 16, "off", 0)
    for f in h[3:]:
        assert (f.assoc, f.n_mshr, f.pf_mode, f.pf_distance) == (4, 8, "next-line", 4)
    assert any(f.pf_issued > 0 for f in h[3:]) and all(f.pf_issued == 0 for f in h[:3])
    assert sum(f.overhead_cycles for f in h) > 0


# ----------------------------------------------------------------------
# shadow tags
# ----------------------------------------------------------------------
def _shadow_totals(m):
    sh = m.shadow
    return {a: (sh.acc[a], sh.miss[a]) for a in sh.assocs}


def test_shadow_ranks_associativity_and_ignores_real_config():
    lines = [r + j * (cfg.L2_SIZE // (min(cfg.L2_ASSOC_CHOICES) * cfg.LINE_SIZE))
             for j in range(cfg.CONFLICT_WAYS) for r in range(cfg.CONFLICT_SETS)]
    totals = {}
    for real in (2, 8):
        s = build_system(l2_assoc=real)
        m = Monitor(s)
        s.core.run(iter(mem_ops(lines * 8)), on_window=m.on_window)
        totals[real] = _shadow_totals(m)
    assert totals[2] == totals[8], "the shadow sees the same stream whatever the real assoc is"
    rate = {a: miss / acc for a, (acc, miss) in totals[8].items()}
    print("[shadow] conflict pattern, estimated miss rate: " +
          ", ".join(f"{a}-way {r:.3f}" for a, r in sorted(rate.items())))
    for a in cfg.L2_ASSOC_CHOICES:
        if a < cfg.CONFLICT_WAYS:
            assert rate[a] > 0.95
    assert rate[max(cfg.L2_ASSOC_CHOICES)] < 0.2


def test_shadow_predicts_real_miss_rate():
    """Run at one associativity, read the estimate for the others, compare to
    runs actually made at those associativities."""
    # Conflict layout, but visited in random order: CONFLICT_WAYS lines per set,
    # so the miss rate falls gradually with associativity instead of 0 or 1.
    rng = random.Random(11)
    stride = cfg.L2_SIZE // (min(cfg.L2_ASSOC_CHOICES) * cfg.LINE_SIZE)
    lines = [rng.randrange(cfg.CONFLICT_SETS) + rng.randrange(cfg.CONFLICT_WAYS) * stride
             for _ in range(40_000)]
    ops = mem_ops(lines)
    s = build_system(l2_assoc=8)
    m = Monitor(s)
    s.core.run(iter(ops), on_window=m.on_window)
    est = {a: miss / acc for a, (acc, miss) in _shadow_totals(m).items()}
    print("[shadow] random reuse, estimate vs real miss rate: ", end="")
    for a in cfg.L2_ASSOC_CHOICES:
        r = build_system(l2_assoc=a)
        run(r, ops)
        real = r.l2.miss_rate()
        print(f"{a}-way {est[a]:.3f}/{real:.3f}  ", end="")
        assert abs(est[a] - real) < 0.06, f"{a}-way: estimate {est[a]:.3f} vs real {real:.3f}"
    print()
    assert est[2] > est[4] > est[8] + 0.02, "more ways must show fewer misses on this pattern"


# ----------------------------------------------------------------------
def test_prefetch_effectiveness_fields():
    s = build_system(pf_mode="next-line", pf_distance=4)
    m = Monitor(s)
    s.core.run(iter(_paced(mem_ops(range(L2_LINES // 2), pc=0x2000), 100)), on_window=m.on_window)
    fs = [f for f in m.history if not f.partial and f.pf_issued > 0]
    assert len(fs) > 5
    for f in fs[1:]:
        assert f.pf_accuracy > 0.9 and f.pf_coverage > 0.8 and f.pf_late_frac > 0.9
        assert f.pf_unused_evicted == 0 and f.pf_mode == "next-line" and f.pf_distance == 4


def test_rebase_after_counter_reset():
    s = build_system()
    m = Monitor(s)
    s.core.run(iter(compute_ops(20_000)), on_window=m.on_window, window_cycles=1_000)
    s.reset_counters()
    try:
        m.sample(s.core)
        raise AssertionError("a counter reset must be detected")
    except RuntimeError:
        pass
    m.rebase()
    n = len(m.history)
    s.core.run(iter(compute_ops(20_000)), on_window=m.on_window, window_cycles=1_000)
    assert len(m.history) > n and all(f.ipc > 3.9 for f in m.history[n:-1])


def test_csv_and_running_mean():
    w, s, m = baseline_run()
    path = os.path.join(tempfile.mkdtemp(), "features.csv")
    m.write_csv(path)
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(m.history)
    assert {"ipc", "stall_frac", "shadow_rate_2", "shadow_rate_8", "pf_mode"} <= set(rows[0])
    assert float(rows[0]["ipc"]) == m.history[0].ipc

    mean_ipc = m.running_mean("ipc", n=3)
    last3 = [f.ipc for f in m.history[-3:] if not f.partial]
    assert abs(mean_ipc - sum(last3) / len(last3)) < 1e-12
    assert m.running_mean("pf_accuracy") is None, "no data -> None, not 0"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], globals()))
