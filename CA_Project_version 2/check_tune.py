"""
check_tune.py — Validate suites.py, tune.py and the frozen parameters.

Run:  python check_tune.py          (all)
      python check_tune.py -k frozen (only names containing "frozen")

Checks: the tuning and test suites are disjoint, tune.py cannot see the test set,
the search algorithm (on a toy objective), the objective, window-size neutrality,
a miniature end-to-end tuning run, and that frozen_params.json is complete, valid,
consistent with the current constants and not overfitted.
"""

import ast
import json
import math
import os
import sys
import tempfile

import sim
import sim_config as cfg
import tune
from check_base import main
from controller import ControllerParams
from suites import TEST_SEEDS, TEST_SUITE, TUNING_SEEDS, TUNING_SUITE, Entry
from workload import Workload

HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------
# suites
# ----------------------------------------------------------------------
def test_suites_are_disjoint_and_buildable():
    t_names = {e.name for e in TUNING_SUITE}
    e_names = {e.name for e in TEST_SUITE}
    assert not (t_names & e_names) and len(t_names) == len(TUNING_SUITE)
    assert not (set(TUNING_SEEDS) & set(TEST_SEEDS)), "tuning and test seeds must not overlap"
    assert len(TUNING_SUITE) >= 8 and "tune-compute" in t_names, "needs 'do nothing' workloads"
    assert {"test-default", "test-paced-stream", "test-conflict4", "test-conflict2",
            "test-chase-stream", "test-intermediate"} <= e_names
    for e in TUNING_SUITE + TEST_SUITE:
        for seed in (TUNING_SEEDS if e in TUNING_SUITE else TEST_SEEDS)[:1]:
            w = e.workload(seed)
            assert w.total_ops == e.total_ops() > 0
            assert w.footprint_lines                       # builds without error
    default_phases = tuple(cfg.DEFAULT_PHASES)
    assert next(e for e in TEST_SUITE if e.name == "test-default").phases == default_phases
    assert all(e.phases != default_phases for e in TUNING_SUITE)


def test_second_workload_has_intermediate_phases():
    e = next(x for x in TEST_SUITE if x.name == "test-intermediate")
    kinds = [k for k, _ in e.phases]
    assert len(set(kinds)) >= 5 and "conflict" in kinds
    assert e.params["streaming"]["compute_per_mem"] >= 300, "sparse misses -> intermediate stall"
    assert e.start == {"l2_assoc": 4}


def test_tuner_source_never_references_the_test_set():
    tree = ast.parse(open(os.path.join(HERE, "tune.py")).read())
    from_suites = {a.name for n in ast.walk(tree)
                   if isinstance(n, ast.ImportFrom) and n.module == "suites" for a in n.names}
    assert from_suites <= {"TUNING_SEEDS", "TUNING_SUITE"}, from_suites
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not any("TEST" in n for n in names), "tune.py must not touch anything test-related"


# ----------------------------------------------------------------------
# search and objective
# ----------------------------------------------------------------------
def test_geomean_and_defaults():
    assert abs(tune.geomean([1.0, 1.0]) - 1.0) < 1e-12
    assert abs(tune.geomean([0.5, 2.0]) - 1.0) < 1e-12
    assert abs(tune.geomean([0.8]) - 0.8) < 1e-12
    d = tune.default_params()
    assert set(d) == set(tune.GRID)
    for k, values in tune.GRID.items():
        assert d[k] in values, f"default of {k} must be a grid value so the search starts at the defaults"
    assert tune.valid(d)
    assert not tune.valid({**d, "acc_off": 0.5, "acc_low": 0.4})


def test_coordinate_descent_on_a_toy_objective():
    grid = {"a": [1, 2, 3, 4], "b": [10, 20, 30]}
    calls, notes = [], []

    def f(p):
        calls.append(dict(p))
        return 1.0 + (p["a"] - 3) ** 2 + 0.01 * (p["b"] - 20) ** 2 / 100

    best, score, passes, changed, rows, sens = tune.coordinate_descent(
        f, grid, {"a": 1, "b": 10}, epsilon=0.001, max_passes=3, progress=notes.append)
    assert (best["a"], best["b"]) == (3, 20) and abs(score - 1.0) < 1e-12
    assert passes == 2 and not changed, "second pass changes nothing: converged"
    assert any("a 1 -> 3" in n for n in notes) and rows
    assert {v for v, _ in sens["a"]} == {1, 2, 3, 4}, "sensitivity covers every value"

    # a flat objective never moves off the defaults (ties go to the defaults)
    best, _, passes, changed, _, _ = tune.coordinate_descent(lambda p: 1.0, grid, {"a": 2, "b": 20})
    assert best == {"a": 2, "b": 20} and passes == 1 and not changed

    # a gain smaller than epsilon is not taken; a bigger one is
    f2 = lambda p: 1.0 - (0.0005 if p["a"] == 2 else 0.0)
    assert tune.coordinate_descent(f2, {"a": [1, 2]}, {"a": 1}, epsilon=0.001)[0]["a"] == 1
    assert tune.coordinate_descent(f2, {"a": [1, 2]}, {"a": 1}, epsilon=0.0001)[0]["a"] == 2

    # invalid candidates (None) are skipped, the best valid one wins
    f3 = lambda p: None if p["a"] == 3 else 1.0 / p["a"]
    assert tune.coordinate_descent(f3, {"a": [1, 2, 3, 4]}, {"a": 1})[0]["a"] == 4

    # stopping at the pass limit still returns a sweep around the result
    best, _, passes, changed, _, sens = tune.coordinate_descent(
        lambda p: 1.0 / p["a"], {"a": [1, 2, 3]}, {"a": 1}, max_passes=1)
    assert passes == 1 and changed and best["a"] == 3 and len(sens["a"]) == 3


def test_objective_scores_the_ratio_to_baseline():
    suite = (Entry("t-compute", (("compute", 20_000),)),
             Entry("t-strided", (("compute", 10_000), ("strided", 12_000))))
    ev = tune.Evaluator(suite, 101)
    d = tune.default_params()
    score, det = ev.run(d)
    assert det[0]["ratio"] == 1.0 and det[0]["switches"] == 0, "nothing to gain: nothing happens"
    assert det[1]["ratio"] < 1.0, "a strided stream is helped"
    assert abs(score - tune.geomean([x["ratio"] for x in det])) < 1e-12
    assert det[1]["baseline"] == sim.run_baseline(suite[1].workload(101)).cycles
    f = ev.objective()
    assert f({**d, "acc_off": 0.5, "acc_low": 0.4}) is None, "invalid combinations are rejected"
    assert f(d) == f(dict(d)), "memoised and deterministic"


def test_window_size_does_not_change_timing_without_actions():
    w = Workload([("compute", 80_000)])
    small = sim.run_baseline(w, window_cycles=5_000)
    large = sim.run_baseline(w, window_cycles=20_000)
    assert small.cycles == large.cycles
    full = lambda r: sum(1 for f in r.monitor.history if not f.partial)
    assert full(small) >= 3 * full(large) >= 3, "a 4x smaller window gives about 4x the full windows"
    d = sim.run_dynamic(w, window_cycles=5_000)
    assert d.cycles == small.cycles and d.reconfig.log == []


def test_miniature_end_to_end_tuning_run():
    suite = (Entry("m-compute", (("compute", 15_000),)),
             Entry("m-strided", (("compute", 10_000), ("strided", 12_000))))
    tmp = tempfile.mkdtemp()
    out, log, rep = (os.path.join(tmp, n) for n in ("f.json", "log.csv", "r.md"))
    res = tune.run_tuning(101, 102, passes=2, out_path=out, log_path=log, report_path=rep,
                          suite=suite, grid={"hysteresis_m": [2, 3], "horizon_h": [10, 20]},
                          verbose=False)
    data = json.load(open(out))
    assert set(data["controller"]) == set(tune.GRID) - {"window_cycles"}, "the full set is frozen"
    assert data["window_cycles"] in tune.GRID["window_cycles"]
    assert data["scores"]["tuned"] <= data["scores"]["default"] + 1e-12
    assert data["sim_config_fingerprint"] == tune.config_fingerprint()
    assert data["tuning"]["suite"] == ["m-compute", "m-strided"]
    assert os.path.getsize(log) > 0
    text = open(rep).read()
    assert "Sensitivity" in text and "separate from the test suite" in text and "m-strided" in text
    assert res["frozen"] == data


# ----------------------------------------------------------------------
# the frozen parameters
# ----------------------------------------------------------------------
def _frozen():
    path = os.path.join(HERE, "frozen_params.json")
    assert os.path.exists(path), "run `python tune.py` to create frozen_params.json"
    return json.load(open(path))


def test_frozen_parameters_are_complete_valid_and_current():
    fz = _frozen()
    assert set(fz["controller"]) == set(tune.GRID) - {"window_cycles"}
    for k, v in fz["controller"].items():
        assert v in tune.GRID[k], f"{k}={v} is outside its grid"
    assert fz["window_cycles"] in tune.GRID["window_cycles"]
    ControllerParams(**fz["controller"])                           # accepted by the controller
    assert tune.valid({**fz["controller"], "window_cycles": fz["window_cycles"]})
    assert fz["sim_config_fingerprint"] == tune.config_fingerprint(), \
        "sim_config.py constants changed since tuning: re-run tune.py"
    changed = {k: [d, fz["controller"].get(k, fz["window_cycles"])]
               for k, d in tune.default_params().items()
               if fz["controller"].get(k, fz["window_cycles"]) != d}
    assert fz["changed_from_default"] == changed
    assert fz["tuning"]["suite"] == [e.name for e in TUNING_SUITE]
    assert not set(fz["tuning"]["seeds"]) & set(TEST_SEEDS)
    assert fz["tuning"]["converged"], "the search must have converged before freezing"


def test_frozen_parameters_improve_the_tuning_suite_and_are_not_overfitted():
    s = _frozen()["scores"]
    print(f"[freeze] tuning seed {s['tuning_seed']}: {s['default']:.4f} -> {s['tuned']:.4f}; "
          f"held-out seed {s['check_seed']}: {s['default_check']:.4f} -> {s['tuned_check']:.4f}")
    assert s["tuned"] <= s["default"], "tuning cannot make its own objective worse"
    assert s["tuned"] < 1.0, "net gain over doing nothing on the tuning suite"
    assert s["tuned_check"] <= s["default_check"] * 1.005, \
        "worse than the defaults on a held-out tuning seed: overfitted"


def test_frozen_parameters_load_into_the_simulator():
    fz = sim.load_frozen()
    w = Workload([("compute", 40_000), ("strided", 20_000)])
    d = sim.run_dynamic_frozen(w)
    plain = sim.run_dynamic(w, window_cycles=fz["window_cycles"], **fz["controller"])
    assert d.cycles == plain.cycles
    assert d.controller.p.hysteresis_m == fz["controller"]["hysteresis_m"]
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert sim.main(["--mode", "dynamic", "--frozen", "--phases", "compute:20000,strided:10000"]) == 0
    assert "mode dynamic" in buf.getvalue()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], globals()))
