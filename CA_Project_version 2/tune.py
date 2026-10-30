"""
tune.py — Chooses the controller's constants on the TUNING suite and freezes them.

    python tune.py                      # full run: writes frozen_params.json,
                                        #   tuning_log.csv and TUNING.md
    python tune.py --passes 2 --seed 101 --check-seed 102

Protocol
  1. Tune on suites.TUNING_SUITE with the first tuning seed. This file imports
     nothing from the test set (an automated test checks that).
  2. Objective: the geometric mean over the tuning workloads of
         (dynamic cycles) / (baseline cycles)
     lower is better; 1.0 means "no better than doing nothing". The set contains
     workloads where nothing can be gained, so a controller that switches when it
     should not is penalised, and one near break-even, so being too eager is too.
     Guard: a candidate is REJECTED if any single tuning workload ends up more than
     MAX_REGRESSION (0.05 = five percentage points of its baseline cycles) worse than
     it is under the default controller. Without it a gain on most workloads can pay
     for a collapse on one class (a first run did exactly that: a shorter window
     gained elsewhere but starved sparse-miss streams of evidence).
  3. Coordinate descent over GRID: for each parameter in turn try its other values
     with everything else held, and move only if the best of them beats the current
     score by EPSILON (relative). Repeat until a whole pass changes nothing (or
     MAX_PASSES). Defaults win ties, so a parameter only moves on real evidence.
  4. Check stability on the second tuning seed (still tuning workloads).
  5. FREEZE: write the full resolved parameter set, the window size, the scores and a
     fingerprint of sim_config.py's constants to frozen_params.json. After this,
     nothing is changed on the basis of test-suite results.

Regime thresholds (stall_compute, stall_memory) and the rule-off flags are not in
the grid: they affect labels or disabled rules, not cycle counts.

Resumable: every evaluated candidate is saved to tuning_cache.json (keyed by the
parameters, and valid only for the same seed, suite and sim_config constants). With
--budget SECONDS a run stops cleanly when the time is up (exit code 3); run the same
command again and it replays the search instantly from the cache and continues.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import time

import sim
import sim_config as cfg
from controller import ControllerParams
from suites import TUNING_SEEDS, TUNING_SUITE          # nothing from the test set

HERE = os.path.dirname(os.path.abspath(__file__))

GRID = {
    "window_cycles":      [5_000, 10_000, 20_000],
    "hysteresis_m":       [2, 3, 4, 5],
    "hysteresis_m_fast":  [1, 2, 3],
    "residency_k":        [3, 5, 8],
    "residency_k_pf":     [1, 2, 4],
    "skip_after_switch":  [1, 2],
    "horizon_h":          [5, 10, 20, 40],
    "assoc_margin":       [1.0, 1.5, 2.0, 3.0],
    "stall_attention":    [0.10, 0.20, 0.30],
    "stall_assoc":        [0.10, 0.25, 0.40],
    "dep_stall_latency":  [0.30, 0.50, 0.70],
    "mshr_high":          [0.70, 0.80, 0.90],
    "bw_saturated":       [0.70, 0.80, 0.90],
    "seq_threshold":      [0.50, 0.60, 0.80],
    "acc_high":           [0.60, 0.75, 0.90],
    "acc_low":            [0.30, 0.40, 0.50],
    "acc_off":            [0.20, 0.30, 0.40],
    "late_high":          [0.30, 0.50, 0.70],
    "irregular":          [0.20, 0.30, 0.40],
    "cov_off":            [0.05, 0.10, 0.20],
    "shadow_improvement": [0.80, 0.90, 0.95],
}
EPSILON = 0.001
MAX_PASSES = 3
MAX_REGRESSION = 0.05


class BudgetExhausted(Exception):
    """The time budget for this invocation ran out; progress is saved."""


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def geomean(xs):
    xs = list(xs)
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def default_params():
    p = ControllerParams()
    d = {k: getattr(p, k) for k in GRID if k != "window_cycles"}
    d["window_cycles"] = cfg.WINDOW_CYCLES
    return d


def config_fingerprint():
    """Hash of sim_config's numeric constants (not comments): the frozen values
    are only valid for the constants they were tuned under."""
    items = sorted((k, repr(v)) for k, v in vars(cfg).items() if k.isupper())
    return hashlib.sha1(repr(items).encode()).hexdigest()


def valid(params):
    return params["acc_off"] <= params["acc_low"] <= params["acc_high"]


# ----------------------------------------------------------------------
# evaluation
# ----------------------------------------------------------------------
class Evaluator:
    """Scores a parameter set on a suite at one seed. Baselines are simulated once
    (the monitor does not change timing, so they do not depend on the window)."""

    def __init__(self, suite, seed):
        self.seed = seed
        self.meta = {"v": 2, "seed": seed, "suite": [e.name for e in suite],
                     "guard": MAX_REGRESSION, "fingerprint": config_fingerprint()}
        self.items = []
        for entry in suite:
            w = entry.workload(seed)
            self.items.append((entry, w, sim.run_baseline(w, entry.start).cycles))

    def run(self, params):
        overrides = {k: v for k, v in params.items() if k != "window_cycles"}
        ratios, details = [], []
        for entry, w, base in self.items:
            d = sim.run_dynamic(w, entry.start, window_cycles=params["window_cycles"], **overrides)
            ratios.append(d.cycles / base)
            details.append({"name": entry.name, "baseline": base, "dynamic": d.cycles,
                            "ratio": d.cycles / base, "switches": len(d.reconfig.log),
                            "overhead": d.overhead_cycles})
        return geomean(ratios), details

    def objective(self, cache_path=None, deadline=None):
        """The scoring function used by the search. With cache_path every new score is
        saved and reused on the next invocation; with deadline (time.time() value) it
        raises BudgetExhausted instead of starting a new evaluation after it."""
        memo = {}
        ref = []
        if cache_path and os.path.exists(cache_path):
            with open(cache_path) as fh:
                data = json.load(fh)
            if data.get("meta") == self.meta:
                memo, ref = data["scores"], data.get("reference", [])

        def save():
            with open(cache_path, "w") as fh:
                json.dump({"meta": self.meta, "scores": memo, "reference": ref}, fh)

        def f(params):
            if not valid(params):
                return None
            key = json.dumps(sorted(params.items()))
            if key not in memo:
                if deadline is not None and time.time() > deadline:
                    raise BudgetExhausted
                if not ref:                       # per-workload ratios of the default controller
                    ref.extend(d["ratio"] for d in self.run(default_params())[1])
                score, det = self.run(params)
                memo[key] = [score, max(d["ratio"] - r for d, r in zip(det, ref))]
                if cache_path:
                    save()
            score, worst = memo[key]
            return None if worst > MAX_REGRESSION else score
        return f


# ----------------------------------------------------------------------
# the search (independent of what is being scored, so it can be unit tested)
# ----------------------------------------------------------------------
def coordinate_descent(evaluate, grid, start, epsilon=EPSILON, max_passes=MAX_PASSES, progress=None):
    """Returns (params, score, passes, last_pass_changed, rows, sensitivity)."""
    cur = dict(start)
    cur_score = evaluate(cur)
    rows, passes, changed = [], 0, False
    sens = {}
    for passes in range(1, max_passes + 1):
        changed, sens = False, {}
        for name, values in grid.items():
            best = None
            sens[name] = [(cur[name], cur_score)]
            for v in values:
                if v == cur[name]:
                    continue
                s = evaluate({**cur, name: v})
                sens[name].append((v, s))
                rows.append({"pass": passes, "param": name, "value": v, "score": s,
                             "current_score": cur_score})
                if s is not None and s < cur_score * (1 - epsilon) and (best is None or s < best[1]):
                    best = (v, s)
            if best:
                if progress:
                    progress(f"pass {passes}: {name} {cur[name]} -> {best[0]}  "
                             f"score {cur_score:.5f} -> {best[1]:.5f}")
                cur[name], cur_score, changed = best[0], best[1], True
        if progress:
            progress(f"pass {passes} done: score {cur_score:.5f}, "
                     f"{'changed' if changed else 'no change, converged'}")
        if not changed:
            break
    if changed:                              # hit MAX_PASSES mid-move: sweep once around the result
        sens = {}
        for name, values in grid.items():
            sens[name] = [(v, cur_score if v == cur[name] else evaluate({**cur, name: v}))
                          for v in values]
    return cur, cur_score, passes, changed, rows, sens


# ----------------------------------------------------------------------
# the full procedure
# ----------------------------------------------------------------------
def run_tuning(seed=None, check_seed=None, passes=MAX_PASSES, out_path=None, log_path=None,
               report_path=None, suite=TUNING_SUITE, grid=None, verbose=True,
               cache_path=None, budget=None):
    """Returns the result dict, or None if the time budget ran out (progress is saved
    in cache_path; call again to continue)."""
    grid = GRID if grid is None else grid
    seed = TUNING_SEEDS[0] if seed is None else seed
    check_seed = TUNING_SEEDS[1] if check_seed is None else check_seed
    say = (lambda m: print(m, flush=True)) if verbose else (lambda m: None)
    t0 = time.time()

    say(f"building baselines for {len(suite)} workloads (seed {seed}) ...")
    ev = Evaluator(suite, seed)
    start = {k: v for k, v in default_params().items() if k in grid or k == "window_cycles"}
    start.setdefault("window_cycles", cfg.WINDOW_CYCLES)
    for k in grid:
        if start.get(k) not in grid[k]:
            raise ValueError(f"default of {k} ({start.get(k)}) is not in its grid {grid[k]}")

    evaluate = ev.objective(cache_path, None if budget is None else t0 + budget)
    try:
        best, score, n_passes, last_changed, rows, sens = coordinate_descent(
            evaluate, grid, {**default_params(), **start}, EPSILON, passes, say)
    except BudgetExhausted:
        say("time budget reached: progress saved, run the same command again to continue")
        return None

    ev_check = Evaluator(suite, check_seed)
    full_default = default_params()
    d_score, d_det = ev.run(full_default)
    t_score, t_det = ev.run(best)
    dc_score, dc_det = ev_check.run(full_default)
    tc_score, tc_det = ev_check.run(best)
    say(f"tuning seed {seed}: default {d_score:.5f} -> tuned {t_score:.5f}")
    say(f"check seed {check_seed}: default {dc_score:.5f} -> tuned {tc_score:.5f}")

    changed = {k: [full_default[k], best[k]] for k in best if best[k] != full_default[k]}
    frozen = {
        "controller": {k: v for k, v in best.items() if k != "window_cycles"},
        "window_cycles": best["window_cycles"],
        "changed_from_default": changed,
        "scores": {"metric": "geometric mean of dynamic/baseline cycles over the tuning suite",
                   "tuning_seed": seed, "default": d_score, "tuned": t_score,
                   "check_seed": check_seed, "default_check": dc_score, "tuned_check": tc_score},
        "tuning": {"suite": [e.name for e in suite], "seeds": [seed, check_seed],
                   "epsilon": EPSILON, "max_regression": MAX_REGRESSION,
                   "passes": n_passes, "converged": not last_changed,
                   "evaluations": len(rows), "seconds": round(time.time() - t0, 1), "grid": grid},
        "per_workload": [{"name": a["name"], "default": a["ratio"], "tuned": b["ratio"],
                          "default_check": c["ratio"], "tuned_check": d["ratio"]}
                         for a, b, c, d in zip(d_det, t_det, dc_det, tc_det)],
        "sim_config_fingerprint": config_fingerprint(),
    }
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(frozen, fh, indent=2)
    if log_path and rows:
        with open(log_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    result = {"frozen": frozen, "default_details": (d_det, dc_det), "tuned_details": (t_det, tc_det),
              "sensitivity": sens, "suite": suite, "default": full_default, "best": best}
    if report_path:
        write_report(result, report_path)
    say(f"done in {time.time() - t0:.0f} s; changed parameters: {changed or 'none'}")
    return result


# ----------------------------------------------------------------------
# report
# ----------------------------------------------------------------------
def _fmt(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


def write_report(result, path):
    fz, suite = result["frozen"], result["suite"]
    sc, tn = fz["scores"], fz["tuning"]
    L = ["# Controller tuning and freeze", "",
         "Generated by `tune.py`. The controller's constants were chosen on a **tuning suite that is "
         "separate from the test suite** and then frozen; no test-suite result was used to change them.", "",
         "## Objective", "",
         f"Geometric mean over the tuning workloads of `dynamic cycles / baseline cycles` "
         f"(lower is better; 1.000 = no better than doing nothing). Tuning seed {sc['tuning_seed']}; "
         f"stability check on seed {sc['check_seed']}.", "",
         f"**Guard:** a candidate is rejected if any single tuning workload ends up more than "
         f"{tn['max_regression']:.2f} (percentage points of its baseline cycles) worse than under the "
         f"default controller, so a gain on most workloads cannot pay for a collapse on one class.", "",
         "## Result", "",
         "| | Defaults | Tuned | Change |", "|---|---|---|---|",
         f"| Tuning seed {sc['tuning_seed']} | {sc['default']:.4f} | {sc['tuned']:.4f} | "
         f"{100 * (sc['tuned'] / sc['default'] - 1):+.2f}% |",
         f"| Check seed {sc['check_seed']} (held out) | {sc['default_check']:.4f} | {sc['tuned_check']:.4f} | "
         f"{100 * (sc['tuned_check'] / sc['default_check'] - 1):+.2f}% |", "",
         f"Search: coordinate descent, {tn['passes']} pass(es), {tn['evaluations']} candidate evaluations, "
         f"{'converged' if tn['converged'] else 'stopped at the pass limit'}, move threshold "
         f"{tn['epsilon'] * 100:.1f}% relative, {tn['seconds']} s.", "",
         "## Parameters", "", "| Parameter | Default | Frozen | Changed |", "|---|---|---|---|"]
    best, default = result["best"], result["default"]
    for k in GRID if set(best) >= set(GRID) else best:
        L.append(f"| `{k}` | {_fmt(default[k])} | {_fmt(best[k])} | {'**yes**' if best[k] != default[k] else ''} |")
    L += ["", "## Per-workload effect", "",
          f"Cycles for each tuning workload (seed {sc['tuning_seed']}).", "",
          "| Workload | Baseline | Default controller | Tuned controller | Tuned vs baseline | Switches (default / tuned) |",
          "|---|---|---|---|---|---|"]
    for d, t in zip(result["default_details"][0], result["tuned_details"][0]):
        L.append(f"| {d['name']} | {d['baseline']:,} | {d['dynamic']:,} ({100 * (d['ratio'] - 1):+.1f}%) | "
                 f"{t['dynamic']:,} ({100 * (t['ratio'] - 1):+.1f}%) | {100 * (t['ratio'] - 1):+.1f}% | "
                 f"{d['switches']} / {t['switches']} |")
    L += ["", f"Same, held-out seed {sc['check_seed']}.", "",
          "| Workload | Baseline | Default controller | Tuned controller |", "|---|---|---|---|"]
    for d, t in zip(result["default_details"][1], result["tuned_details"][1]):
        L.append(f"| {d['name']} | {d['baseline']:,} | {d['dynamic']:,} ({100 * (d['ratio'] - 1):+.1f}%) | "
                 f"{t['dynamic']:,} ({100 * (t['ratio'] - 1):+.1f}%) |")
    L += ["", "## Sensitivity around the frozen point", "",
          "Score when one parameter is moved and the rest stay frozen (frozen value in bold). "
          "A flat row means the controller is insensitive to that constant on this suite (robust, or not "
          "exercised by it); \"rejected\" means the candidate broke an ordering rule or the regression guard.", "",
          "| Parameter | Values and scores |", "|---|---|"]
    for name, pairs in result["sensitivity"].items():
        cells = []
        for v, s in sorted(pairs, key=lambda p: p[0]):
            txt = f"{_fmt(v)}: {'rejected' if s is None else format(s, '.4f')}"
            cells.append(f"**{txt}**" if v == best[name] else txt)
        L.append(f"| `{name}` | {' · '.join(cells)} |")
    L += ["", "## The tuning suite", "", "| Workload | Phases (ops) | Start configuration | Teaches |", "|---|---|---|---|"]
    for e in suite:
        ph = ", ".join(f"{k} {n:,}" for k, n in e.phases)
        L.append(f"| {e.name} | {ph} | {e.start or 'baseline'} | {e.note} |")
    L += ["", "## Freeze", "",
          "`frozen_params.json` holds the full resolved parameter set, the window size, these scores and a "
          "fingerprint of `sim_config.py`'s constants. If a constant in `sim_config.py` changes, the fingerprint "
          "no longer matches and the controller must be re-tuned (an automated test enforces this).", ""]
    with open(path, "w") as fh:
        fh.write("\n".join(L))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tune and freeze the controller constants")
    ap.add_argument("--passes", type=int, default=MAX_PASSES)
    ap.add_argument("--seed", type=int, default=TUNING_SEEDS[0])
    ap.add_argument("--check-seed", type=int, default=TUNING_SEEDS[1])
    ap.add_argument("--out", default=os.path.join(HERE, "frozen_params.json"))
    ap.add_argument("--log", default=os.path.join(HERE, "tuning_log.csv"))
    ap.add_argument("--report", default=os.path.join(HERE, "TUNING.md"))
    ap.add_argument("--cache", default=os.path.join(HERE, "tuning_cache.json"))
    ap.add_argument("--budget", type=float, default=None,
                    help="stop cleanly after this many seconds (exit code 3); re-run to continue")
    args = ap.parse_args(argv)
    res = run_tuning(args.seed, args.check_seed, args.passes, args.out, args.log, args.report,
                     cache_path=args.cache, budget=args.budget)
    return 0 if res is not None else 3


if __name__ == "__main__":
    sys.exit(main())
