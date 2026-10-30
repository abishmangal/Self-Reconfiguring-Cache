"""
check_sim.py — Validate sim.py: the three modes, resource accounting, the CLI.

Run:  python check_sim.py          (all)
      python check_sim.py -k oracle (only names containing "oracle")
"""

import contextlib
import io
import os
import sys
import tempfile

import sim
import sim_config as cfg
from check_base import main
from system import build_system
from workload import Workload

SCALED = [("compute", 200_000), ("streaming", 40_000), ("compute", 200_000), ("pointer", 600)]


def test_baseline_matches_a_plain_run():
    w = Workload(SCALED)
    res = sim.run_baseline(w)
    plain = build_system()
    plain.core.run(w.ops())
    assert res.cycles == plain.core.cycle, "attaching the monitor must not change timing"
    assert res.start_config == cfg.BASELINE_CONFIG and res.config_changes == []
    assert res.overhead_cycles == 0
    r = res.resources()
    assert r == {"avg_ways": 8.0, "avg_mshr": 16.0, "pf_on_frac": 0.0}


def test_dynamic_with_the_controller_held_back_equals_baseline():
    w = Workload(SCALED)
    b = sim.run_baseline(w)
    d = sim.run_dynamic(w, hysteresis_m=10 ** 6, hysteresis_m_fast=10 ** 6)
    assert d.reconfig.log == [] and d.overhead_cycles == 0
    assert d.cycles == b.cycles, "the whole control loop is timing-neutral until it acts"
    assert d.controller.trace, "it still evaluated every window"


def test_oracle_switches_are_free_and_follow_the_phases():
    w = Workload(SCALED)
    b, o = sim.run_baseline(w), sim.run_oracle(w)
    start = sim.make_config()
    s = o.system.core.stats
    assert s["reconfig_cycles"] == 0 and s["drain_cycles"] == 0, "no cost is charged"
    assert len(o.schedule) == len(w.phases)
    assert o.schedule[0] == start and o.schedule[2] == start, "nothing to gain in a compute phase"
    assert o.schedule[1]["n_mshr"] == 32, "streaming wants the biggest pool"
    assert o.cycles < 0.9 * b.cycles
    assert o.instructions == b.instructions
    assert [(k, v) for _, k, v in o.config_changes] == [("n_mshr", 32), ("n_mshr", 16)]


def test_oracle_search_is_deterministic_and_cached():
    w = Workload([("streaming", 30_000)])
    a = sim.oracle_schedule(w)
    assert a == sim.oracle_schedule(w)
    assert a[0]["n_mshr"] == 32
    assert sim.best_config_for("compute", sim.make_config(), w.params, w.seed) == sim.make_config(), \
        "a phase where nothing matters keeps the starting configuration"


def test_oracle_bounds_dynamic_and_dynamic_beats_baseline():
    for name, phases, start in (
        ("default", SCALED, None),
        ("conflict", [("compute", 100_000), ("conflict", 100_000), ("compute", 100_000)], {"l2_assoc": 2}),
    ):
        w = Workload(phases)
        b, d, o = sim.run_baseline(w, start), sim.run_dynamic(w, start), sim.run_oracle(w, start)
        closed = (b.cycles - d.cycles) / (b.cycles - o.cycles)
        print(f"[modes] {name}: baseline {b.cycles}, dynamic {d.cycles}, oracle {o.cycles} "
              f"(dynamic closes {100 * closed:.0f}% of the gap)")
        assert o.cycles <= d.cycles < b.cycles
        assert closed > 0.7


def test_resource_use_is_time_weighted():
    start = {"l2_assoc": 8, "n_mshr": 16, "pf_mode": "off", "pf_distance": 0}
    r = sim.resource_use(start, [(100, "n_mshr", 32), (300, "pf_mode", "stride")], 400)
    assert approx(r["avg_mshr"], (16 * 100 + 32 * 300) / 400)
    assert approx(r["pf_on_frac"], 100 / 400) and r["avg_ways"] == 8.0
    r = sim.resource_use(start, [(0, "l2_assoc", 2)], 50)
    assert r["avg_ways"] == 2.0
    assert sim.resource_use(start, [], 0)["avg_ways"] == 0.0


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol * max(1.0, abs(b))


def test_dynamic_result_reports_its_own_overhead_and_resources():
    w = Workload(SCALED)
    d = sim.run_dynamic(w)
    s = d.summary()
    assert s["switches"] == 1 and s["overhead_cycles"] == 416
    assert abs(s["overhead_pct"] - 100 * 416 / d.cycles) < 0.001
    assert 16 < s["avg_mshr"] < 32 and s["avg_ways"] == 8.0
    assert d.reconfig.total_overhead_cycles() == d.overhead_cycles


def test_config_helpers_and_parsing():
    assert sim.make_config({"pf_mode": "stride"}) ["pf_distance"] == cfg.PF_DISTANCE_DEFAULT["stride"]
    assert sim.make_config({"pf_mode": "off", "pf_distance": 7})["pf_distance"] == 0
    for bad in ({"cache_size": 1}, {"pf_mode": "bogus"}, {"pf_mode": "stride", "pf_distance": 3}):
        try:
            sim.make_config(bad)
            raise AssertionError(f"{bad} must be rejected")
        except ValueError:
            pass
    assert sim.parse_phases("compute:100,streaming:50") == [("compute", 100), ("streaming", 50)]
    for bad in ("compute", "bogus:5", "compute:0", "compute:x"):
        try:
            sim.parse_phases(bad)
            raise AssertionError(f"{bad!r} must be rejected")
        except ValueError:
            pass
    assert sim.parse_pf("stride:4") == ("stride", 4) and sim.parse_pf("off") == ("off", 0)
    try:
        sim.run("bogus", Workload([("compute", 100)]))
        raise AssertionError("unknown mode")
    except ValueError:
        pass


def test_command_line_runs_every_mode_and_writes_csv():
    tmp = tempfile.mkdtemp()
    prefix = os.path.join(tmp, "out")
    for mode in (cfg.MODE_BASELINE, cfg.MODE_DYNAMIC, cfg.MODE_ORACLE):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = sim.main(["--mode", mode, "--phases", "compute:60000,streaming:15000,compute:30000",
                             "--csv", prefix, "--mshr", "8", "--pf", "off"])
        assert code == 0 and f"mode {mode}" in buf.getvalue()
    assert os.path.getsize(prefix + "_windows.csv") > 0
    assert os.path.exists(prefix + "_switches.csv") and os.path.exists(prefix + "_trace.csv")
    assert "oracle schedule" in buf.getvalue()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], globals()))
