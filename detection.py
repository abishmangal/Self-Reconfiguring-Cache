"""
detection.py — Scores the controller's phase detection against the true phase
boundaries. The boundaries are used here, to SCORE; the controller never sees them.

What is scored
  * Window accuracy: of the monitor windows lying entirely inside a phase, the
    fraction whose window-level regime equals the phase's expected regime.
  * Detection delay: for each true boundary where the expected regime changes, the
    number of windows from the first window of the new phase until the controller
    CONFIRMED the change (it needs HYSTERESIS_M windows in a row).
  * Missed changes: expected changes the controller never confirmed in time.
  * False changes: confirmed changes that match no expected change (flicker).

The expected regime comes from the phase KIND, not from the classifier:
    compute -> compute;  pointer -> latency;  streaming / strided / mixed /
    chase_stream -> memory;  conflict -> memory while the L2 is below 8-way (it
    thrashes) and compute at 8-way (it fits).
A phase's regime must not depend on what the controller does to the machine, so the
scoring is done on an OBSERVE-ONLY replay: the controller is stepped over the
baseline run's windows with a reconfigurator that never changes anything.
"""

from __future__ import annotations

import sim_config as cfg
from controller import Controller


def expected_regime(kind, start_assoc=None):
    if kind == "compute":
        return "compute"
    if kind == "pointer":
        return "latency"
    if kind == "conflict":
        return "memory" if (start_assoc or cfg.L2_ASSOC_DEFAULT) < cfg.CONFLICT_WAYS else "compute"
    if kind in ("streaming", "strided", "mixed", "chase_stream"):
        return "memory"
    raise ValueError(f"unknown phase kind {kind!r}")


def expected_sequence(phases, start_assoc=None):
    return [expected_regime(kind, start_assoc) for kind, _ in phases]


class ObserveOnly:
    """A reconfigurator that changes nothing: a Controller stepped with it only
    classifies and confirms regimes."""

    def estimate(self, knob):
        return {"prefetch": cfg.EST_OVERHEAD_PREFETCH_DIST, "n_mshr": cfg.EST_OVERHEAD_MSHR,
                "assoc": (cfg.L2_SIZE // cfg.LINE_SIZE) * cfg.K_ASSOC_PER_LINE}[knob]

    def apply(self, knob, value, reason=""):
        return None


def replay_regimes(features, **controller_overrides):
    """Step a controller (observe-only) over a list of monitor Features.
    Returns ({window: window-level regime}, [(window confirmed, from, to)], controller)."""
    ctl = Controller(None, None, ObserveOnly(), **controller_overrides)
    for f in features:
        if not f.partial:
            ctl.step(f)
    return {d.window: d.regime for d in ctl.trace if d.regime}, list(ctl.regime_changes), ctl


def phase_layout(window_instructions, boundaries):
    """For each phase: (first window, windows lying entirely inside it).
    window_instructions: instructions executed in each window, in order;
    boundaries: workload.boundaries (start_op / end_op, in instructions)."""
    his, cum = [], 0
    for n in window_instructions:
        cum += n
        his.append(cum)
    los = [0] + his[:-1]
    layout = []
    for b in boundaries:
        first = next((w for w, hi in enumerate(his) if hi > b["start_op"]), len(his))
        inside = [w for w, (lo, hi) in enumerate(zip(los, his))
                  if lo >= b["start_op"] and hi <= b["end_op"]]
        layout.append((first, inside))
    return layout


def score(window_instructions, boundaries, expected, window_regimes, changes, max_delay=20):
    layout = phase_layout(window_instructions, boundaries)
    total = correct = 0
    for (first, inside), exp in zip(layout, expected):
        if exp is None:
            continue
        for w in inside:
            if w == 0:                       # cold first window of the run
                continue
            r = window_regimes.get(w)
            if r is not None:
                total += 1
                correct += r == exp

    used, delays, missed, n_expected = set(), [], 0, 0
    for i in range(1, len(boundaries)):
        if expected[i] is None or expected[i - 1] is None or expected[i] == expected[i - 1]:
            continue
        n_expected += 1
        lo = layout[i][0]
        hi = layout[i + 1][0] if i + 1 < len(layout) else len(window_instructions)
        hit = next((c for c in changes if c not in used and c[2] == expected[i]
                    and lo <= c[0] < hi and c[0] - lo <= max_delay), None)
        if hit is None:
            missed += 1
        else:
            used.add(hit)
            delays.append(hit[0] - lo)
    return {
        "windows_scored": total,
        "window_accuracy": correct / total if total else None,
        "expected_changes": n_expected,
        "detected": len(delays),
        "missed": missed,
        "delays": delays,
        "mean_delay": sum(delays) / len(delays) if delays else None,
        "false_changes": len([c for c in changes if c not in used]),
    }
