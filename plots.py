"""
plots.py — Figures for RESULTS.md. Called by analysis.build_report; needs matplotlib.

    bars                  execution time (cycles / baseline) per workload and mode
    overhead              adaptation overhead per workload and knob; estimated vs measured cost
    breakeven             dynamic / baseline against phase length, with the break-even point
    negative              MSHRs under a bandwidth limit; prefetch pollution
    horizon               sensitivity to the horizon H
    timeline-<workload>   counters over time with true and detected transitions, the dynamic
                          run with its switches, and the configuration timeline (seed 1)
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import detection
import experiments as ex
import sim_config as cfg

REGIME_COLOR = {"compute": "#d9f0d3", "memory": "#fde0b8", "latency": "#e1d5f0", None: "#eeeeee"}
SIGNALS = (("stall_frac", "stall fraction", "#d62728"), ("dep_stall_frac", "dependent stall", "#9467bd"),
           ("bw_util", "bus use", "#1f77b4"), ("mshr_occ_frac", "MSHR pool use", "#2ca02c"))


def _save(fig, fig_dir, name):
    os.makedirs(fig_dir, exist_ok=True)
    path = os.path.join(fig_dir, f"{name}.png")
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return f"figures/{name}.png"


def fig_bars(a, fig_dir):
    rows = a.main_rows()
    names = [r["workload"].replace("test-", "") for r in rows]
    fig, ax = plt.subplots(figsize=(10, 4.2))
    w = 0.26
    xs = range(len(rows))
    ax.bar([x - w for x in xs], [r["dyn_ratio"] for r in rows], w, yerr=[r["dyn_ratio_sd"] for r in rows],
           label="dynamic (frozen)", color="#1f77b4", capsize=3)
    ax.bar(list(xs), [r["default_ratio"] for r in rows], w, label="dynamic (default constants)", color="#9ecae1")
    ax.bar([x + w for x in xs], [r["oracle_ratio"] for r in rows], w, label="static oracle", color="#2ca02c")
    ax.axhline(1.0, color="k", lw=1)
    ax.text(len(rows) - 0.5, 1.01, "baseline = 1.0", ha="right", fontsize=8)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(names, rotation=15)
    ax.set_ylabel("cycles / baseline cycles")
    ax.set_title("Execution time relative to the fixed baseline (mean of seeds 1-5; bars = sd)")
    ax.legend(fontsize=8, loc="lower left")
    return _save(fig, fig_dir, "bars")


def fig_overhead(a, fig_dir):
    rows = [r for r in a.overhead_rows() if r["knob"] != "all"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    names = sorted({r["workload"] for r in rows}, key=lambda n: [e.name for e in a.entries].index(n))
    bottoms = [0.0] * len(names)
    for knob, col in (("prefetch", "#2ca02c"), ("n_mshr", "#1f77b4"), ("assoc", "#d62728")):
        vals = [next((r["pct_of_runtime"] for r in rows if r["workload"] == n and r["knob"] == knob), 0.0)
                for n in names]
        ax1.bar(range(len(names)), vals, bottom=bottoms, label=knob, color=col)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax1.set_xticks(range(len(names)))
    ax1.set_xticklabels([n.replace("test-", "") for n in names], rotation=20, fontsize=8)
    ax1.set_ylabel("% of runtime")
    ax1.set_title("Adaptation overhead by knob (frozen controller)")
    ax1.legend(fontsize=8)
    for knob, col, mk in (("prefetch", "#2ca02c", "o"), ("n_mshr", "#1f77b4", "s"), ("assoc", "#d62728", "^")):
        pts = [(r["estimated_cycles"] / r["switches"], r["total_cycles"] / r["switches"])
               for r in rows if r["knob"] == knob and r["switches"]]
        ax2.scatter([p[0] for p in pts], [p[1] for p in pts], c=col, marker=mk, label=knob, s=40)
    lim = [5, 1e5]
    ax2.plot(lim, lim, "k--", lw=1)
    ax2.set_xscale("log")
    ax2.set_yscale("log")
    ax2.set_xlabel("controller's estimate per switch (cycles)")
    ax2.set_ylabel("measured per switch (cycles)")
    ax2.set_title("Estimated vs measured switch cost")
    ax2.legend(fontsize=8)
    return _save(fig, fig_dir, "overhead")


def fig_breakeven(a, fig_dir):
    series = a.breakeven_series()
    titles = {("assoc", 4): "Associativity, start 4-way\n(conflict phase)", ("assoc", 2): "Associativity, start 2-way\n(conflict phase)",
              ("mshr", 0): "MSHR 16 -> 32\n(streaming phase)", ("prefetch", 0): "Prefetch on + distance\n(paced streaming)"}
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.8), sharey=False)
    for ax, (k, pts) in zip(axes, series.items()):
        pts = sorted(pts, key=lambda p: p["windows"])
        xs = [p["windows"] for p in pts]
        ys = [p["ratio"] for p in pts]
        er = [p["ratio_sd"] for p in pts]
        ax.errorbar(xs, ys, yerr=er, marker="o", color="#1f77b4", capsize=3)
        ax.axhline(1.0, color="k", lw=1)
        w, text = a.breakeven_point(pts)
        if w is not None:
            ax.axvline(w, color="#d62728", ls="--")
            ax.text(w, max(ys) * 0.97, f" break-even ~{w:.0f} windows", color="#d62728", fontsize=8, va="top")
        ax.set_xscale("log")
        ax.set_xlabel("phase length (windows at baseline pace)")
        ax.set_title(titles[k], fontsize=9)
        ax.fill_between(xs, 1.0, [max(y, 1.0) for y in ys], color="#d62728", alpha=0.15)
    axes[0].set_ylabel("dynamic / baseline cycles")
    fig.suptitle("Break-even: above 1.0 the adaptive run is slower than doing nothing", y=1.02)
    return _save(fig, fig_dir, "breakeven")


def fig_negative(a, fig_dir):
    neg = a.negative()
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(14, 3.8))
    for bus, col in zip(ex.NEG_BUSES, ("#1f77b4", "#d62728")):
        ax1.plot(cfg.MSHR_CHOICES, [neg["mshr_bus"][(bus, m)]["cycles"] for m in cfg.MSHR_CHOICES],
                 marker="o", color=col, label=f"bus {bus} cycles/line")
    ax1.set_xscale("log", base=2)
    ax1.set_yscale("log")
    ax1.set_xticks(cfg.MSHR_CHOICES)
    ax1.set_xticklabels(cfg.MSHR_CHOICES)
    ax1.set_xlabel("L2 MSHRs")
    ax1.set_ylabel("cycles")
    ax1.set_title("(a) More MSHRs stop helping once the bus is the limit", fontsize=9)
    ax1.legend(fontsize=8)
    jd = neg["junk_dist"]
    labels = ["off", "next-line d1", "next-line d4", "next-line d16"]
    vals = [jd[k]["cycles"] for k in (("off", 0), ("next-line", 1), ("next-line", 4), ("next-line", 16))]
    ax2.bar(labels, vals, color=["#2ca02c", "#fdae6b", "#fd8d3c", "#d94801"])
    for i, v in enumerate(vals):
        ax2.text(i, v, f"{v / vals[0]:.1f}x", ha="center", va="bottom", fontsize=8)
    ax2.set_ylabel("cycles")
    ax2.set_title("(b) Prefetch pollution: junk prefetches slow the run", fontsize=9)
    ax2.tick_params(axis="x", labelsize=8)
    jm = neg["junk_mshr"]
    ax3.plot(list(jm), [jm[m]["cycles"] for m in jm], marker="s", color="#d94801", label="next-line d4")
    ax3.axhline(vals[0], color="#2ca02c", ls="--", label="prefetch off")
    ax3.set_xscale("log", base=2)
    ax3.set_xticks(list(jm))
    ax3.set_xticklabels(list(jm))
    ax3.set_xlabel("L2 MSHRs")
    ax3.set_ylabel("cycles")
    ax3.set_title("(c) A bigger pool amplifies a useless prefetcher", fontsize=9)
    ax3.legend(fontsize=8)
    return _save(fig, fig_dir, "negative")


def fig_horizon(a, fig_dir):
    rows = a.horizon_rows()
    fig, ax = plt.subplots(figsize=(6, 3.8))
    for name, col in zip(ex.HORIZON_ENTRIES, ("#d62728", "#1f77b4")):
        r = [x for x in rows if x["workload"] == name]
        ax.errorbar([x["horizon"] for x in r], [x["ratio"] for x in r], yerr=[x["ratio_sd"] for x in r],
                    marker="o", capsize=3, color=col, label=name)
    ax.axvline(20, color="gray", ls=":")
    ax.text(20, ax.get_ylim()[1], " frozen H=20", fontsize=8, va="top")
    ax.set_xscale("log")
    ax.set_xlabel("horizon H (windows)")
    ax.set_ylabel("dynamic / baseline cycles")
    ax.set_title("Sensitivity to the horizon")
    ax.legend(fontsize=8)
    return _save(fig, fig_dir, "horizon")


def fig_timeline(a, name, fig_dir, seed=1):
    base, dyn = a.cell(name, seed, "baseline"), a.cell(name, seed, "dynamic")
    layout = detection.phase_layout(base["win_instr"], base["boundaries"])
    starts = [first for first, _ in layout]
    ends = starts[1:] + [len(base["win_instr"])]
    expected = base["expected"]
    sb, sd_ = base["series"], dyn["series"]

    fig, axes = plt.subplots(5, 1, figsize=(11, 8.2), sharex=True,
                             gridspec_kw={"height_ratios": [3, 2, 1, 1, 1]})
    axA, axB, axM, axW, axP = axes
    for ax in axes:
        for s, e, r in zip(starts, ends, expected):
            ax.axvspan(s - 0.5, e - 0.5, color=REGIME_COLOR.get(r), alpha=0.55, lw=0)
        for s in starts[1:]:
            ax.axvline(s - 0.5, color="green", ls="--", lw=1)
    for col, label, color in SIGNALS:
        axA.plot(sb["window"], sb[col], label=label, color=color, lw=1.4)
    for w, frm, to in base["regime_changes"]:
        axA.axvline(w, color="#1f4e9c", lw=1.6)
        axA.annotate(f"{frm}->{to}", (w, 1.04), fontsize=7, color="#1f4e9c", rotation=40, annotation_clip=False)
    axA.set_ylim(-0.03, 1.08)
    axA.set_ylabel("baseline counters")
    axA.legend(fontsize=7, ncol=4, loc="lower left")
    axA.set_title(f"{name}: shaded = expected regime per phase (green dashes = true phase starts, "
                  f"blue = confirmed regime change)", fontsize=9, pad=24)
    axB.plot(sb["window"], sb["stall_frac"], color="gray", ls="--", lw=1, label="baseline")
    axB.plot(sd_["window"], sd_["stall_frac"], color="#d62728", lw=1.4, label="dynamic")
    for r in dyn["switch_log"]:
        axB.plot([r["window"]], [1.02], marker="v", color="k", ms=6, clip_on=False)
        axB.annotate(f"{r['knob']}->{r['new']}", (r["window"], 1.04), fontsize=6, rotation=40, annotation_clip=False)
    axB.set_ylim(-0.03, 1.08)
    axB.set_ylabel("stall fraction")
    axB.legend(fontsize=7, loc="lower left")
    axM.step(sd_["window"], sd_["n_mshr"], where="post", color="#1f77b4")
    axM.set_ylabel("L2 MSHRs", fontsize=8)
    axM.set_yticks(list(cfg.MSHR_CHOICES))
    axW.step(sd_["window"], sd_["assoc"], where="post", color="#d62728")
    axW.set_ylabel("L2 ways", fontsize=8)
    axW.set_yticks(list(cfg.L2_ASSOC_CHOICES))
    dist = [d if m != "off" else 0 for d, m in zip(sd_["pf_distance"], sd_["pf_mode"])]
    axP.step(sd_["window"], dist, where="post", color="#2ca02c")
    axP.set_ylabel("prefetch dist.\n(0 = off)", fontsize=8)
    axP.set_xlabel("window (10,000 cycles of workload time)")
    return _save(fig, fig_dir, f"timeline-{name}")


def make_all(a, fig_dir):
    figs = {"bars": fig_bars(a, fig_dir), "overhead": fig_overhead(a, fig_dir),
            "breakeven": fig_breakeven(a, fig_dir), "negative": fig_negative(a, fig_dir),
            "horizon": fig_horizon(a, fig_dir)}
    for e in a.entries:
        figs[f"timeline-{e.name}"] = fig_timeline(a, e.name, fig_dir)
    return figs
