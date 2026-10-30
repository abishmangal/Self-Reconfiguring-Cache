"""
experiments.py — Runs every experiment on the TEST suite with the FROZEN controller.

    python experiments.py run --budget 110      # repeat until it says "all done"
    python experiments.py status
    python experiments.py report                # tables, CSVs and figures (analysis.py, plots.py)

Nothing here changes any constant: the parameters come from frozen_params.json and the
workloads from suites.TEST_SUITE (seeds 1-5). Results are cached in results/cache.json
and are valid only for the same sim_config constants and the same frozen file; each
invocation stops cleanly after --budget seconds (exit code 3) and the next one resumes.

Experiments
  main        every test workload x seeds 1-5 x {baseline, dynamic (frozen), dynamic with the
              default constants, static oracle}; per-window series, switch log, overhead
              table and the phase-detection score (observe-only replay, see detection.py)
  breakeven   how long a phase must last for adaptation to pay: associativity (from 4-way
              and from 2-way), MSHR count, prefetch; seeds 1-3
  negative    cases where adaptation or a knob hurts: more MSHRs under a bandwidth limit,
              prefetch pollution (static and with the controller), short phases (breakeven)
  horizon     sensitivity to the horizon H on two workloads; seeds 1-3
The oracle's schedule per workload is searched once (reference seed 1) and reused for
every seed, like a configuration chosen from prior experiments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time

import detection
import sim
import sim_config as cfg
import suites
import tune
from controller import Controller
from monitor import Monitor
from reconfig import Reconfigurator
from system import build_system
from workload import OP_COMPUTE, OP_LOAD, Workload

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")
CACHE_PATH = os.path.join(RESULTS_DIR, "cache.json")

MAIN_MODES = ("baseline", "dynamic", "dynamic_default", "oracle")
SWEEP_SEEDS = (1, 2, 3)
ASSOC_LENGTHS = (6_000, 12_000, 20_000, 30_000, 45_000, 60_000, 100_000)
BREAKEVEN = {("assoc", 4): ASSOC_LENGTHS, ("assoc", 2): ASSOC_LENGTHS,
             ("mshr", 0): (4_000, 8_000, 16_000, 32_000, 64_000),
             ("prefetch", 0): (30_000, 60_000, 120_000, 240_000, 480_000)}
HORIZONS = (5, 10, 20, 40, 80)
HORIZON_ENTRIES = ("test-conflict4", "test-paced-stream")
NEG_BUSES = (8, 16)
NEG_JUNK_MSHR = (4, 8, 16, 32)
SERIES_COLUMNS = ("t_end", "ipc", "stall_frac", "dep_stall_frac", "bw_util", "mshr_occ_frac",
                  "l2_mpki", "assoc", "n_mshr", "pf_mode", "pf_distance")


# ----------------------------------------------------------------------
# cache
# ----------------------------------------------------------------------
class BudgetExhausted(Exception):
    pass


def frozen_hash():
    with open(sim.FROZEN_PATH, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


class Store:
    """Disk cache: key string -> JSON value. Valid only for one (config, frozen file)."""

    def __init__(self, path=CACHE_PATH, meta=None):
        self.path = path
        self.meta = meta if meta is not None else {
            "v": 1, "fingerprint": tune.config_fingerprint(), "frozen": frozen_hash()}
        self.data = {}
        if os.path.exists(path):
            with open(path) as fh:
                blob = json.load(fh)
            if blob.get("meta") == self.meta:
                self.data = blob["data"]

    def __contains__(self, key):
        return key in self.data

    def get(self, key):
        return self.data[key]

    def put(self, key, value):
        self.data[key] = value
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump({"meta": self.meta, "data": self.data}, fh, separators=(",", ":"))


def key(*parts):
    return "|".join(str(p) for p in parts)


# ----------------------------------------------------------------------
# describing a run
# ----------------------------------------------------------------------
class OpsWorkload:
    """A bare op list with just enough of the Workload interface for sim.run_*."""

    def __init__(self, ops):
        self._ops, self.phases, self.params, self.seed, self.boundaries = ops, [], {}, 1, []

    def ops(self):
        return iter(self._ops)


def _series(history):
    full = [f for f in history if not f.partial]
    cols = {c: [(round(v, 4) if isinstance(v, float) else v) for v in (getattr(f, c) for f in full)]
            for c in SERIES_COLUMNS}
    cols["window"] = [f.window for f in full]
    return cols


def describe(res, workload, entry, mode, frozen):
    h = res.monitor.history
    win_instr = [f.instructions for f in h]
    out = {"summary": res.summary(), "win_instr": win_instr, "boundaries": workload.boundaries,
           "phases": [list(p) for p in workload.phases]}
    if mode in ("baseline", "dynamic", "dynamic_default"):
        out["series"] = _series(h)
    if res.reconfig is not None:
        out["switch_log"] = res.reconfig.log_rows()
        out["overhead_table"] = res.reconfig.overhead_table()
        out["config_changes"] = [[c, k, str(v)] for c, k, v in res.config_changes]
        out["regime_changes"] = [list(c) for c in res.controller.regime_changes]
        out["trace"] = {"regime": [d.regime for d in res.controller.trace],
                        "confirmed": [d.confirmed_regime for d in res.controller.trace],
                        "skipped": [d.skipped for d in res.controller.trace]}
    if mode == "oracle":
        out["config_changes"] = [[c, k, str(v)] for c, k, v in res.config_changes]
        out["schedule"] = res.schedule
    if mode == "baseline":                      # observe-only replay with the frozen controller
        regs, changes, ctl = detection.replay_regimes(h, **frozen["controller"])
        start_assoc = entry.start.get("l2_assoc", cfg.L2_ASSOC_DEFAULT)
        expected = detection.expected_sequence(workload.phases, start_assoc)
        out["expected"] = expected
        out["regime_changes"] = [list(c) for c in changes]
        out["trace"] = {"regime": [d.regime for d in ctl.trace],
                        "confirmed": [d.confirmed_regime for d in ctl.trace],
                        "skipped": [d.skipped for d in ctl.trace]}
        out["detection"] = detection.score(win_instr, workload.boundaries, expected, regs, changes)
    return out


# ----------------------------------------------------------------------
# tasks
# ----------------------------------------------------------------------
def get_schedule(store, entry):
    k = key("schedule", entry.name)
    if k not in store:
        ref = suites.TEST_SEEDS[0]
        w = entry.workload(ref)
        store.put(k, sim.oracle_schedule(w, entry.start, seed=ref))
    return store.get(k)


def task_main(store, name, seed, mode):
    entry = suites.by_name(name)
    w, frozen = entry.workload(seed), sim.load_frozen()
    if mode == "baseline":
        res = sim.run_baseline(w, entry.start)
    elif mode == "dynamic":
        res = sim.run_dynamic_frozen(w, entry.start)
    elif mode == "dynamic_default":
        res = sim.run_dynamic(w, entry.start)
    elif mode == "oracle":
        res = sim.run_oracle(w, entry.start, schedule=get_schedule(store, entry))
    else:
        raise ValueError(mode)
    return describe(res, w, entry, mode, frozen)


def breakeven_workload(knob, variant, length, seed):
    if knob == "assoc":
        return Workload([("compute", 100_000), ("conflict", length), ("compute", 100_000)],
                        seed=seed), {"l2_assoc": variant}
    if knob == "mshr":
        return Workload([("compute", 50_000), ("streaming", length), ("compute", 50_000)],
                        seed=seed), {}
    return Workload([("compute", 30_000), ("streaming", length)], seed=seed,
                    params={"streaming": {"compute_per_mem": 100}}), {}


def task_breakeven(store, knob, variant, length, seed):
    w, start = breakeven_workload(knob, variant, length, seed)
    b = sim.run_baseline(w, start)
    d = sim.run_dynamic_frozen(w, start)
    layout = detection.phase_layout([f.instructions for f in b.monitor.history], w.boundaries)
    return {"baseline": b.cycles, "dynamic": d.cycles, "overhead": d.overhead_cycles,
            "switches": [[r.knob, str(r.new), r.window] for r in d.reconfig.log],
            "phase_windows": len(layout[1][1]), "phase_ops": length}


def _streaming_ops():
    return Workload([("streaming", 40_000)])


def _junk_ops():
    """A reusable working set near L2 capacity, spaced so +1..+16 lines are never used:
    every prefetch is junk and evicts a useful line."""
    rng = random.Random(1)
    ops = []
    pad = [(OP_COMPUTE, None, False, 0)] * 20
    for _ in range(30_000):
        ops.append((OP_LOAD, rng.randrange(3300) * 33 * cfg.LINE_SIZE, False, 0x5000))
        ops.extend(pad)
    return OpsWorkload(ops)


def task_negative(store, kind, *args):
    frozen = sim.load_frozen()
    if kind == "mshr_bus":
        bus, mshr = int(args[0]), int(args[1])
        s = build_system(l2_mshr=mshr, bus_cycles=bus)
        s.core.run(_streaming_ops().ops())
        return {"cycles": s.core.cycle, "bus_floor": 20_000 * bus}
    if kind == "controller_bus":
        bus = int(args[0])
        out = {}
        for mode in ("baseline", "dynamic"):
            s = build_system(bus_cycles=bus)
            mon = Monitor(s)
            if mode == "baseline":
                s.core.run(_streaming_ops().ops(), on_window=mon.on_window)
                out[mode] = {"cycles": s.core.cycle, "switches": []}
            else:
                rc = Reconfigurator(s, mon)
                ctl = Controller(s, mon, rc, **frozen["controller"])
                s.core.run(_streaming_ops().ops(), on_window=ctl.on_window,
                           window_cycles=frozen["window_cycles"])
                out[mode] = {"cycles": s.core.cycle,
                             "switches": [[r.knob, str(r.new)] for r in rc.log]}
        return out
    if kind == "junk":
        mshr, mode, dist = int(args[0]), args[1], int(args[2])
        s = build_system(l2_mshr=mshr, pf_mode=mode, pf_distance=dist if mode != "off" else None)
        s.core.run(_junk_ops().ops())
        st = s.l2.stats
        return {"cycles": s.core.cycle, "l2_miss_rate": s.l2.miss_rate(),
                "pf_issued": st["pf_issued"], "pf_unused_evicted": st["pf_unused_evicted"]}
    if kind == "junk_dynamic":
        variant = args[0]
        w = _junk_ops()
        start = {"pf_mode": "next-line", "pf_distance": 4} if variant.endswith("_on") else {}
        res = (sim.run_baseline(w, start) if variant.startswith("baseline")
               else sim.run_dynamic_frozen(w, start))
        return {"cycles": res.cycles, "switches": [[r.knob, str(r.new), r.window]
                                                   for r in (res.reconfig.log if res.reconfig else [])]}
    raise ValueError(kind)


def task_horizon(store, name, h, seed):
    entry, frozen = suites.by_name(name), sim.load_frozen()
    d = sim.run_dynamic(entry.workload(seed), entry.start, window_cycles=frozen["window_cycles"],
                        **{**frozen["controller"], "horizon_h": int(h)})
    return {"cycles": d.cycles, "overhead": d.overhead_cycles,
            "switches": [[r.knob, str(r.new)] for r in d.reconfig.log]}


# ----------------------------------------------------------------------
# the task list and the runner
# ----------------------------------------------------------------------
def all_tasks(only=None, workloads=None, seeds=None):
    only = only or ("main", "breakeven", "negative", "horizon")
    names = workloads or [e.name for e in suites.TEST_SUITE]
    seeds = seeds or list(suites.TEST_SEEDS)
    sweep_seeds = [s for s in SWEEP_SEEDS if s in seeds] or list(SWEEP_SEEDS)
    tasks = []
    if "main" in only:
        for seed in seeds:
            for name in names:
                for mode in MAIN_MODES:
                    tasks.append((key("main", name, seed, mode), task_main, (name, seed, mode)))
    if "breakeven" in only:
        for (knob, variant), lengths in BREAKEVEN.items():
            for length in lengths:
                for seed in sweep_seeds:
                    tasks.append((key("breakeven", knob, variant, length, seed), task_breakeven,
                                  (knob, variant, length, seed)))
    if "negative" in only:
        for bus in NEG_BUSES:
            for mshr in cfg.MSHR_CHOICES:
                tasks.append((key("neg", "mshr_bus", bus, mshr), task_negative, ("mshr_bus", bus, mshr)))
            tasks.append((key("neg", "controller_bus", bus), task_negative, ("controller_bus", bus)))
        for mode, dist in (("off", 0), ("next-line", 1), ("next-line", 4), ("next-line", 16)):
            tasks.append((key("neg", "junk", 16, mode, dist), task_negative, ("junk", 16, mode, dist)))
        for mshr in NEG_JUNK_MSHR:
            if mshr != 16:
                tasks.append((key("neg", "junk", mshr, "next-line", 4), task_negative,
                              ("junk", mshr, "next-line", 4)))
        for variant in ("baseline_off", "baseline_on", "dynamic_off", "dynamic_on"):
            tasks.append((key("neg", "junk_dynamic", variant), task_negative, ("junk_dynamic", variant)))
    if "horizon" in only:
        for name in HORIZON_ENTRIES:
            if name in names:
                for h in HORIZONS:
                    for seed in sweep_seeds:
                        tasks.append((key("horizon", name, h, seed), task_horizon, (name, h, seed)))
    return tasks


def run_all(store, budget=None, **filters):
    t0 = time.time()
    tasks = all_tasks(**filters)
    done = sum(1 for k, _, _ in tasks if k in store)
    for k, fn, args in tasks:
        if k in store:
            continue
        if budget is not None and time.time() - t0 > budget:
            print(f"time budget reached: {done}/{len(tasks)} tasks done; run again to continue", flush=True)
            return False
        t = time.time()
        store.put(k, fn(store, *args))
        done += 1
        print(f"[{done}/{len(tasks)}] {k}  ({time.time() - t:.1f}s)", flush=True)
    print(f"all done: {len(tasks)} tasks", flush=True)
    return True


def status(store, **filters):
    tasks = all_tasks(**filters)
    groups = {}
    for k, _, _ in tasks:
        g = groups.setdefault(k.split("|")[0], [0, 0])
        g[1] += 1
        g[0] += k in store
    for g, (d, n) in groups.items():
        print(f"{g:10s} {d}/{n}")
    return all(d == n for d, n in groups.values())


def _csv(text):
    return [t for t in text.split(",") if t] if text else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the experiments with the frozen controller")
    ap.add_argument("command", choices=("run", "status", "report"))
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--only", help="comma list of: main,breakeven,negative,horizon")
    ap.add_argument("--workloads", help="comma list of test workload names")
    ap.add_argument("--seeds", help="comma list of seeds")
    args = ap.parse_args(argv)
    filters = {"only": _csv(args.only), "workloads": _csv(args.workloads),
               "seeds": [int(s) for s in _csv(args.seeds)] if args.seeds else None}
    store = Store()
    if args.command == "status":
        return 0 if status(store, **filters) else 3
    if args.command == "run":
        return 0 if run_all(store, args.budget, **filters) else 3
    import analysis
    analysis.build_report(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
