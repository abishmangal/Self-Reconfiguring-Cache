"""
analysis.py — Turns the cached experiment results into tables, CSV files, figures and
RESULTS.md. Pure aggregation: no simulation is run here.

Conventions
  ratio      = cycles of a mode / cycles of the baseline for the same workload and seed
               (below 1.0 is faster than doing nothing)
  gap closed = (baseline - dynamic) / (baseline - oracle), per seed, then averaged
  mean +- sd across seeds 1-5 for the main comparison, seeds 1-3 for the sweeps
"""

from __future__ import annotations

import csv
import math
import os
import statistics

import experiments as ex
import sim
import sim_config as cfg
import suites

RESULTS_DIR = ex.RESULTS_DIR
FIG_DIR = os.path.join(RESULTS_DIR, "figures")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def geomean(xs):
    xs = list(xs)
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def pct(r):
    return f"{100 * (r - 1):+.1f}%"


class Analysis:
    def __init__(self, store):
        self.store = store
        self.entries = list(suites.TEST_SUITE)
        self.seeds = list(suites.TEST_SEEDS)

    # ------------------------------------------------------------------
    def cell(self, name, seed, mode):
        return self.store.get(ex.key("main", name, seed, mode))

    def cycles(self, name, seed, mode):
        return self.cell(name, seed, mode)["summary"]["cycles"]

    # ------------------------------------------------------------------
    def main_rows(self):
        rows = []
        for e in self.entries:
            r = {"workload": e.name, "note": e.note}
            ratios = {m: [] for m in ("dynamic", "dynamic_default", "oracle")}
            gaps, ovh, ovh_pct, sw, mshr, pf = [], [], [], [], [], []
            for s in self.seeds:
                b = self.cycles(e.name, s, "baseline")
                for m in ratios:
                    ratios[m].append(self.cycles(e.name, s, m) / b)
                d = self.cell(e.name, s, "dynamic")["summary"]
                o = self.cycles(e.name, s, "oracle")
                gaps.append((b - d["cycles"]) / (b - o) if b - o > 0.01 * b else None)
                ovh.append(d["overhead_cycles"])
                ovh_pct.append(d["overhead_pct"])
                sw.append(d["switches"])
                mshr.append(d["avg_mshr"])
                pf.append(d["pf_on_frac"])
            r.update({
                "baseline_cycles": mean(self.cycles(e.name, s, "baseline") for s in self.seeds),
                "dyn_ratio": mean(ratios["dynamic"]), "dyn_ratio_sd": sd(ratios["dynamic"]),
                "dyn_ratio_max": max(ratios["dynamic"]),
                "default_ratio": mean(ratios["dynamic_default"]),
                "oracle_ratio": mean(ratios["oracle"]),
                "gap_closed": mean(gaps), "gap_closed_sd": sd(gaps),
                "overhead_cycles": mean(ovh), "overhead_pct": mean(ovh_pct),
                "switches": mean(sw), "avg_mshr": mean(mshr), "pf_on_frac": mean(pf),
            })
            rows.append(r)
        return rows

    def main_summary(self):
        rows = self.main_rows()
        return {"dynamic": geomean(r["dyn_ratio"] for r in rows),
                "default": geomean(r["default_ratio"] for r in rows),
                "oracle": geomean(r["oracle_ratio"] for r in rows)}

    # ------------------------------------------------------------------
    def overhead_rows(self):
        rows = []
        for e in self.entries:
            tables = [self.cell(e.name, s, "dynamic")["overhead_table"] for s in self.seeds]
            for knob in ("prefetch", "n_mshr", "assoc", "all"):
                sel = [next(t for t in tab if t["knob"] == knob) for tab in tables]
                if not any(x["switches"] for x in sel):
                    continue
                rows.append({"workload": e.name, "knob": knob,
                             **{k: mean(x[k] for x in sel) for k in
                                ("switches", "drain_cycles", "stall_cycles", "total_cycles",
                                 "estimated_cycles", "refetch_misses", "pct_of_runtime")}})
        return rows

    def switch_rows(self):
        """Every switch of the seed-1 dynamic runs (for the report)."""
        rows = []
        for e in self.entries:
            for r in self.cell(e.name, self.seeds[0], "dynamic")["switch_log"]:
                rows.append({"workload": e.name, "window": r["window"], "cycle": r["cycle"],
                             "knob": r["knob"], "change": f"{r['old']} -> {r['new']}",
                             "cost": r["total_cycles"], "estimate": r["est_cycles"],
                             "refetch": r["refetch_misses"], "rule": r["reason"]})
        return rows

    # ------------------------------------------------------------------
    def detection_rows(self):
        rows = []
        for e in self.entries:
            det = [self.cell(e.name, s, "baseline")["detection"] for s in self.seeds]
            delays = [d for x in det for d in x["delays"]]
            rows.append({"workload": e.name,
                         "window_accuracy": mean(x["window_accuracy"] for x in det),
                         "windows_scored": mean(x["windows_scored"] for x in det),
                         "expected_changes": sum(x["expected_changes"] for x in det),
                         "detected": sum(x["detected"] for x in det),
                         "missed": sum(x["missed"] for x in det),
                         "false_changes": sum(x["false_changes"] for x in det),
                         "mean_delay": mean(delays), "max_delay": max(delays) if delays else None})
        return rows

    # ------------------------------------------------------------------
    def breakeven_series(self):
        out = {}
        for (knob, variant), lengths in ex.BREAKEVEN.items():
            pts = []
            for L in lengths:
                cells = [self.store.get(ex.key("breakeven", knob, variant, L, s))
                         for s in ex.SWEEP_SEEDS]
                ratios = [c["dynamic"] / c["baseline"] for c in cells]
                pts.append({"ops": L, "windows": mean(c["phase_windows"] for c in cells),
                            "ratio": mean(ratios), "ratio_sd": sd(ratios),
                            "net": mean(c["baseline"] - c["dynamic"] for c in cells),
                            "overhead": mean(c["overhead"] for c in cells),
                            "switches": mean(len(c["switches"]) for c in cells)})
            out[(knob, variant)] = pts
        return out

    @staticmethod
    def breakeven_point(pts):
        """Phase length (in windows at baseline pace) where ratio crosses 1.0."""
        pts = sorted(pts, key=lambda p: p["windows"])
        if all(p["ratio"] >= 1 for p in pts):
            return None, "never pays in the tested range"
        if all(p["ratio"] < 1 for p in pts):
            return None, f"pays from the shortest tested phase ({pts[0]['windows']:.0f} windows)"
        for a, b in zip(pts, pts[1:]):
            if a["ratio"] >= 1 > b["ratio"]:
                w = a["windows"] + (1 - a["ratio"]) * (b["windows"] - a["windows"]) / (b["ratio"] - a["ratio"])
                return w, f"about {w:.0f} window{'s' if round(w) != 1 else ''}"
        return None, "no single crossing"

    # ------------------------------------------------------------------
    def horizon_rows(self):
        rows = []
        for name in ex.HORIZON_ENTRIES:
            for h in ex.HORIZONS:
                ratios = [self.store.get(ex.key("horizon", name, h, s))["cycles"]
                          / self.cycles(name, s, "baseline") for s in ex.SWEEP_SEEDS]
                sw = [len(self.store.get(ex.key("horizon", name, h, s))["switches"])
                      for s in ex.SWEEP_SEEDS]
                rows.append({"workload": name, "horizon": h, "ratio": mean(ratios),
                             "ratio_sd": sd(ratios), "switches": mean(sw)})
        return rows

    def negative(self):
        g = self.store.get
        mshr_bus = {(bus, m): g(ex.key("neg", "mshr_bus", bus, m)) for bus in ex.NEG_BUSES
                    for m in cfg.MSHR_CHOICES}
        junk_dist = {(m, d): g(ex.key("neg", "junk", 16, m, d))
                     for m, d in (("off", 0), ("next-line", 1), ("next-line", 4), ("next-line", 16))}
        junk_mshr = {m: g(ex.key("neg", "junk", m, "next-line", 4)) for m in ex.NEG_JUNK_MSHR}
        junk_dyn = {v: g(ex.key("neg", "junk_dynamic", v))
                    for v in ("baseline_off", "baseline_on", "dynamic_off", "dynamic_on")}
        ctl_bus = {bus: g(ex.key("neg", "controller_bus", bus)) for bus in ex.NEG_BUSES}
        return {"mshr_bus": mshr_bus, "junk_dist": junk_dist, "junk_mshr": junk_mshr,
                "junk_dynamic": junk_dyn, "controller_bus": ctl_bus}

    def resource_rows(self):
        rows = []
        for e in self.entries:
            for mode in ex.MAIN_MODES:
                sm = [self.cell(e.name, s, mode)["summary"] for s in self.seeds]
                rows.append({"workload": e.name, "mode": mode,
                             "avg_ways": mean(x["avg_ways"] for x in sm),
                             "avg_mshr": mean(x["avg_mshr"] for x in sm),
                             "pf_on_frac": mean(x["pf_on_frac"] for x in sm)})
        return rows


# ----------------------------------------------------------------------
# output
# ----------------------------------------------------------------------
def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write_csv(path, rows):
    if rows:
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def build_report(store, out_dir=RESULTS_DIR, make_figures=True):
    a = Analysis(store)
    os.makedirs(out_dir, exist_ok=True)
    main, ovh, det = a.main_rows(), a.overhead_rows(), a.detection_rows()
    be, hz, neg = a.breakeven_series(), a.horizon_rows(), a.negative()
    summ = a.main_summary()
    write_csv(os.path.join(out_dir, "main_results.csv"), main)
    write_csv(os.path.join(out_dir, "overhead.csv"), ovh)
    write_csv(os.path.join(out_dir, "switches_seed1.csv"), a.switch_rows())
    write_csv(os.path.join(out_dir, "detection.csv"), det)
    write_csv(os.path.join(out_dir, "horizon.csv"), hz)
    write_csv(os.path.join(out_dir, "resources.csv"), a.resource_rows())
    write_csv(os.path.join(out_dir, "breakeven.csv"),
              [{"knob": k, "variant": v, **p} for (k, v), pts in be.items() for p in pts])
    figures = {}
    if make_figures:
        import plots
        figures = plots.make_all(a, os.path.join(out_dir, "figures"))
    with open(os.path.join(out_dir, "RESULTS.md"), "w") as fh:
        fh.write(render_markdown(a, main, ovh, det, be, hz, neg, summ, figures))
    print(f"wrote {out_dir}/RESULTS.md, CSV files and {len(figures)} figures")
    return a


def render_markdown(a, main, ovh, det, be, hz, neg, summ, figures):
    L = ["# Phase 3 results — test suite, frozen controller", "",
         "Generated by `analysis.py` from `results/cache.json`. Every number is a deterministic "
         "simulation. The controller constants are the **frozen** ones (`frozen_params.json`, tuned on "
         "a separate suite); no result below was used to change them. Main comparison: 6 test "
         "workloads × seeds 1–5; sweeps: seeds 1–3. *ratio* = cycles ÷ baseline cycles for the same "
         "workload and seed (below 1.00 is faster than doing nothing); *gap closed* = (baseline − dynamic) "
         "÷ (baseline − oracle).", ""]

    L += ["## 1. Baseline vs dynamic vs static oracle", "",
          md_table(["Workload", "Baseline (cycles)", "Dynamic ratio (mean ± sd)", "Worst seed",
                    "Default-constants ratio", "Oracle ratio", "Gap closed", "Switches", "Overhead"],
                   [[r["workload"], f"{r['baseline_cycles']:,.0f}",
                     f"{r['dyn_ratio']:.3f} ± {r['dyn_ratio_sd']:.3f} ({pct(r['dyn_ratio'])})",
                     f"{r['dyn_ratio_max']:.3f}", f"{r['default_ratio']:.3f}",
                     f"{r['oracle_ratio']:.3f} ({pct(r['oracle_ratio'])})",
                     "n/a" if r["gap_closed"] is None else f"{100 * r['gap_closed']:.0f}% ± {100 * r['gap_closed_sd']:.0f}",
                     f"{r['switches']:.1f}", f"{r['overhead_pct']:.2f}%"] for r in main]), "",
          f"**Geometric mean ratio over the six workloads:** dynamic (frozen) **{summ['dynamic']:.3f}**, "
          f"dynamic with the default constants {summ['default']:.3f}, static oracle {summ['oracle']:.3f}. "
          "The oracle uses phase labels and switches for free, so it is an upper-bound reference.", ""]
    if "bars" in figures:
        L += [f"![Execution time per mode]({figures['bars']})", ""]

    L += ["## 2. Overhead of adaptation", "",
          "Frozen controller, mean over seeds 1–5 (only knobs that switched are listed). *Estimated* is "
          "what the controller assumed; the gap is the measured drain, bus-bound writebacks and (for "
          "associativity) refetch misses.", "",
          md_table(["Workload", "Knob", "Switches", "Drain", "Stall", "Total cycles", "Estimated",
                    "Refetch misses", "% of runtime"],
                   [[r["workload"], r["knob"], f"{r['switches']:.1f}", f"{r['drain_cycles']:,.0f}",
                     f"{r['stall_cycles']:,.0f}", f"{r['total_cycles']:,.0f}",
                     f"{r['estimated_cycles']:,.0f}", f"{r['refetch_misses']:,.0f}",
                     f"{r['pct_of_runtime']:.2f}%"] for r in ovh]), ""]
    if "overhead" in figures:
        L += [f"![Overhead]({figures['overhead']})", ""]
    sw = a.switch_rows()
    L += ["Every switch in the seed-1 dynamic runs:", "",
          md_table(["Workload", "Window", "Cycle", "Knob", "Change", "Cost", "Estimate", "Refetch", "Rule"],
                   [[r["workload"], r["window"], f"{r['cycle']:,}", r["knob"], r["change"],
                     f"{r['cost']:,}", f"{r['estimate']:,}", r["refetch"], r["rule"]] for r in sw]), ""]

    L += ["## 3. Phase detection", "",
          "Scored on an observe-only replay of the baseline run (the controller classifies and confirms "
          "regimes but changes nothing), against the regime expected from each phase's kind. "
          "*Window accuracy*: windows inside a phase whose regime matches; *delay*: windows from the "
          "start of the new phase to the confirmed change (hysteresis is "
          f"{sim.load_frozen()['controller']['hysteresis_m']} windows); *false*: confirmed "
          "changes that match no true change.", "",
          md_table(["Workload", "Window accuracy", "True changes", "Detected", "Missed", "False changes",
                    "Mean delay (windows)", "Max delay"],
                   [[r["workload"], "n/a" if r["window_accuracy"] is None else f"{100 * r['window_accuracy']:.1f}%",
                     r["expected_changes"], r["detected"], r["missed"], r["false_changes"],
                     "n/a" if r["mean_delay"] is None else f"{r['mean_delay']:.1f}",
                     "n/a" if r["max_delay"] is None else r["max_delay"]] for r in det]), "",
          "Misses and false changes occur only where a phase lasts about 2-3 windows (the closing compute "
          "phase of the conflict workloads, one short compute phase in test-intermediate): too short for a "
          "2-window confirmation, so the change is confirmed after the phase has already ended, or not "
          "before the run ends.", ""]

    L += ["## 4. Break-even phase length", "",
          "Dynamic ÷ baseline cycles as a function of how long the phase lasts (windows measured at the "
          "baseline pace). Below the break-even the switch costs more than it saves.", ""]
    for (knob, variant), pts in be.items():
        label = {"assoc": f"Associativity switch (conflict phase, start {variant}-way)",
                 "mshr": "MSHR growth 16 → 32 (streaming phase)",
                 "prefetch": "Prefetch on and distance (paced streaming phase)"}[knob]
        w, text = a.breakeven_point(pts)
        L += [f"**{label}** — break-even: {text}.", "",
              md_table(["Phase (ops)", "Phase windows", "Dynamic ÷ baseline (mean ± sd)", "Net cycles saved",
                        "Overhead cycles", "Switches"],
                       [[f"{p['ops']:,}", f"{p['windows']:.0f}", f"{p['ratio']:.3f} ± {p['ratio_sd']:.3f} ({pct(p['ratio'])})",
                         f"{p['net']:,.0f}", f"{p['overhead']:,.0f}", f"{p['switches']:.1f}"] for p in pts]), ""]
    if "breakeven" in figures:
        L += [f"![Break-even curves]({figures['breakeven']})", ""]

    L += ["## 5. Cases where adaptation or a knob hurts", ""]
    mb = neg["mshr_bus"]
    L += ["**(a) More MSHRs under a bandwidth limit** — static cycles for a 20,000-line stream:", "",
          md_table(["Bus (cycles per line)"] + [f"{m} MSHRs" for m in cfg.MSHR_CHOICES] + ["16 → 32 gain"],
                   [[bus] + [f"{mb[(bus, m)]['cycles']:,}" for m in cfg.MSHR_CHOICES]
                    + [f"{mb[(bus, 16)]['cycles'] / mb[(bus, 32)]['cycles']:.2f}×"] for bus in ex.NEG_BUSES]), ""]
    cb = neg["controller_bus"]
    L += ["With the slow bus the controller " + (
        "did not switch (the bus guard held)." if not cb[16]["dynamic"]["switches"]
        else f"switched {cb[16]['dynamic']['switches']}.") +
        f" Cycles baseline {cb[16]['baseline']['cycles']:,}, dynamic {cb[16]['dynamic']['cycles']:,}.", ""]
    jd, jm = neg["junk_dist"], neg["junk_mshr"]
    L += ["**(b) Prefetch pollution** — a reusable working set near L2 capacity where every prefetch is junk:", "",
          md_table(["Configuration", "Cycles", "vs prefetch off", "L2 miss rate", "Prefetches issued", "Unused, evicted"],
                   [[f"{m} d={d}" if m != "off" else "prefetch off", f"{v['cycles']:,}",
                     pct(v['cycles'] / jd[('off', 0)]['cycles']), f"{v['l2_miss_rate']:.2f}",
                     f"{v['pf_issued']:,}", f"{v['pf_unused_evicted']:,}"] for (m, d), v in jd.items()]), "",
          "The same junk prefetcher as the MSHR pool grows (next-line, distance 4):", "",
          md_table(["L2 MSHRs", "Cycles", "L2 miss rate", "Prefetches issued"],
                   [[m, f"{v['cycles']:,}", f"{v['l2_miss_rate']:.2f}", f"{v['pf_issued']:,}"]
                    for m, v in jm.items()]), ""]
    jy = neg["junk_dynamic"]
    L += ["With the controller on that pattern:", "",
          md_table(["Run", "Cycles", "Switches"],
                   [[{"baseline_off": "fixed, prefetch off", "baseline_on": "fixed, next-line d=4 (bad start)",
                      "dynamic_off": "controller, starts off", "dynamic_on": "controller, starts next-line d=4"}[k],
                     f"{v['cycles']:,}", "; ".join(f"{s[0]} -> {s[1]} (window {s[2]})" for s in v["switches"]) or "none"]
                    for k, v in jy.items()]), "",
          "**(c) Short phases** — see the associativity break-even table above: a conflict phase shorter "
          "than the break-even makes the adaptive run slower than doing nothing.", ""]
    if "negative" in figures:
        L += [f"![Negative cases]({figures['negative']})", ""]

    L += ["## 6. Sensitivity to the horizon", "",
          "Dynamic ÷ baseline for different horizons H (windows the current behaviour is assumed to continue); "
          "the frozen value is 20.", "",
          md_table(["Workload", "Horizon", "Ratio (mean ± sd)", "Switches"],
                   [[r["workload"], r["horizon"], f"{r['ratio']:.3f} ± {r['ratio_sd']:.3f}", f"{r['switches']:.1f}"]
                    for r in hz]), ""]
    if "horizon" in figures:
        L += [f"![Horizon sensitivity]({figures['horizon']})", ""]

    L += ["## 7. Resource use", "",
          "Time-weighted average L2 ways, L2 MSHR entries and share of time the prefetcher was on "
          "(mean over seeds). Reported, not optimised.", "",
          md_table(["Workload", "Mode", "Avg ways", "Avg MSHRs", "Prefetch on"],
                   [[r["workload"], r["mode"], f"{r['avg_ways']:.2f}", f"{r['avg_mshr']:.1f}",
                     f"{100 * r['pf_on_frac']:.0f}%"] for r in a.resource_rows()]), ""]

    if any(k.startswith("timeline-") for k in figures):
        L += ["## 8. Timelines (seed 1)", "",
              "Top: baseline counters with true phase starts (green) and the controller's confirmed regime "
              "changes (blue). Middle: the dynamic run's stall fraction with switches marked. Bottom: "
              "configuration over time.", ""]
        for e in a.entries:
            k = f"timeline-{e.name}"
            if k in figures:
                L += [f"### {e.name}", f"*{e.note}*", "", f"![{e.name}]({figures[k]})", ""]
    return "\n".join(L)
