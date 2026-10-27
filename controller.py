"""
controller.py — The decision layer: reads the monitor's feature vector once per
window and decides whether to change prefetch, MSHR count or L2 associativity.

    mon = Monitor(system); rc = Reconfigurator(system, mon)
    ctl = Controller(system, mon, rc)
    system.core.run(ops, on_window=ctl.on_window)

Static rules over live counters. No benefit table, no memory of earlier runs,
no phase labels (this module never touches the workload; the phase boundaries
exist only to score it afterwards). It assumes the behaviour it sees now
continues for HORIZON_H windows.

Every window, in order
  1. The window right after a switch is skipped entirely (cold caches; the
     switch's own cycles are already excluded as overhead by the monitor).
  2. The window is classified: compute / memory / latency / uncertain. A
     regime is CONFIRMED when it has held for HYSTERESIS_M windows; confirmed
     changes are the controller's phase detection.
  3. propose() lists every change the rules currently want.
  4. A proposal must hold for HYSTERESIS_M windows in a row (HYSTERESIS_M_FAST
     for switching a useless prefetcher off). Streaks are tracked per proposal,
     so a flickering rule cannot starve a steady one.
  5. A ripe proposal is allowed only if its knob group has been still for long
     enough (MIN_RESIDENCY_K for MSHR/associativity, MIN_RESIDENCY_K_PF for
     prefetch) and its utility is positive:
         saved cycles per window * HORIZON_H  >  estimated switch cost
     where saved = window cycles * stall fraction * the fraction of stall the
     change should remove. Damage control and resource-saving moves are exempt.
  6. Of the allowed proposals the controller makes ONE change per window:
     urgent damage control (switching a useless prefetcher off or down) first,
     otherwise the largest net benefit (saved * HORIZON_H - cost), so a big win
     is not held up behind a cheap small one. A switch clears all streaks.

The rules (constants live in sim_config.py)
  prefetch off -> on   memory hurts (stall >= STALL_FRAC_ATTENTION), misses can
                       overlap (dependent stall < DEP_STALL_LATENCY), the bus has
                       room (< BW_UTIL_SATURATED), spare MSHRs exist (occupancy
                       < MSHR_HIGH_FRAC) and the stream is regular: stride if
                       stride confidence >= SEQ_SCORE_THRESHOLD, else next-line if
                       the sequential score is.
  prefetch on -> off   accuracy < PF_ACC_OFF, or coverage < PF_COV_OFF with enough
                       prefetches issued, or both stream scores < PF_IRREGULAR.
  distance down        accuracy < PF_ACC_LOW, or the bus is saturated and accuracy
                       is not high.
  distance up          accuracy >= PF_ACC_HIGH, most useful prefetches late
                       (> PF_LATE_HIGH), bus and MSHR headroom.
  MSHR up one step     pool occupancy >= MSHR_HIGH_FRAC, misses independent, bus
                       below BW_UTIL_SATURATED, and no low-accuracy prefetcher
                       running. Gain estimate: speedup = min(new/old, 1/bus
                       utilisation).
  MSHR down            (ENABLE_MSHR_SHRINK) occupancy < MSHR_LOW_FRAC and the
                       smaller pool still holds twice the observed occupancy.
  associativity        stall >= STALL_FRAC_ASSOC and the shadow tags show another
                       associativity with at least 10% fewer misses (valid
                       samples only). The same test run in the other direction is
                       the revert rule. Shrinking (ENABLE_ASSOC_SHRINK) needs a
                       shadow miss rate no worse than SHADOW_NO_LOSS_FRAC.
Missing evidence (None) never triggers anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

import sim_config as cfg

HEAVY = ("assoc", "n_mshr")        # knobs that drain the machine share one residency clock
REGIMES = ("compute", "memory", "latency", "uncertain")


@dataclass
class ControllerParams:
    """Every tunable the controller uses, defaulting to sim_config. Override
    individual fields: Controller(system, mon, rc, hysteresis_m=1)."""
    hysteresis_m: int = cfg.HYSTERESIS_M
    hysteresis_m_fast: int = cfg.HYSTERESIS_M_FAST
    residency_k: int = cfg.MIN_RESIDENCY_K
    residency_k_pf: int = cfg.MIN_RESIDENCY_K_PF
    skip_after_switch: int = cfg.SKIP_WINDOW_AFTER_SWITCH
    horizon_h: int = cfg.HORIZON_H
    stall_compute: float = cfg.STALL_FRAC_COMPUTE
    stall_memory: float = cfg.STALL_FRAC_MEMORY
    stall_attention: float = cfg.STALL_FRAC_ATTENTION
    stall_assoc: float = cfg.STALL_FRAC_ASSOC
    dep_stall_latency: float = cfg.DEP_STALL_LATENCY
    bw_saturated: float = cfg.BW_UTIL_SATURATED
    mshr_high: float = cfg.MSHR_HIGH_FRAC
    mshr_low: float = cfg.MSHR_LOW_FRAC
    seq_threshold: float = cfg.SEQ_SCORE_THRESHOLD
    acc_high: float = cfg.PF_ACC_HIGH
    acc_low: float = cfg.PF_ACC_LOW
    acc_off: float = cfg.PF_ACC_OFF
    cov_off: float = cfg.PF_COV_OFF
    late_high: float = cfg.PF_LATE_HIGH
    irregular: float = cfg.PF_IRREGULAR
    pf_min_issued: int = cfg.PF_MIN_ISSUED
    shadow_improvement: float = cfg.SHADOW_IMPROVEMENT_FRAC
    shadow_no_loss: float = cfg.SHADOW_NO_LOSS_FRAC
    assoc_margin: float = cfg.ASSOC_OVERHEAD_MARGIN
    enable_mshr_shrink: bool = cfg.ENABLE_MSHR_SHRINK
    enable_assoc_shrink: bool = cfg.ENABLE_ASSOC_SHRINK


@dataclass(frozen=True)
class Proposal:
    knob: str                  # "prefetch" | "n_mshr" | "assoc"
    value: object              # (mode, distance) for prefetch, an int otherwise
    rule: str
    saved_per_window: float    # expected cycles saved per window (0 for exempt moves)
    fast: bool = False         # may use the shorter hysteresis
    exempt: bool = False       # damage control / resource saving: no utility test
    urgent: bool = False       # damage control: goes before any performance gain

    @property
    def key(self):
        return (self.knob, self.value)


@dataclass
class Decision:
    """What the controller did with one window (for timelines and reports)."""
    window: int
    t_end: int
    regime: str | None = None
    confirmed_regime: str | None = None
    skipped: bool = False
    proposals: list = field(default_factory=list)   # rule names currently wanted
    action: str = ""                                # "" or e.g. "n_mshr 16->32 (mshr-raise)"
    blocked: str = ""                               # why a ripe proposal did not run


def classify(f, p):
    """Workload regime of one window. Uses time fractions only, so the label does
    not change just because the controller changed the bus or MSHR load."""
    if f.stall_frac < p.stall_compute:
        return "compute"
    if f.dep_stall_frac >= p.dep_stall_latency:
        return "latency"
    if f.stall_frac >= p.stall_memory:
        return "memory"
    return "uncertain"


def _step_up(ladder, v):
    return ladder[ladder.index(v) + 1] if v in ladder and ladder.index(v) + 1 < len(ladder) else None


def _step_down(ladder, v):
    return ladder[ladder.index(v) - 1] if v in ladder and ladder.index(v) > 0 else None


class Controller:
    def __init__(self, system, monitor, reconfig, **overrides):
        valid = {f.name for f in fields(ControllerParams)}
        bad = set(overrides) - valid
        if bad:
            raise ValueError(f"unknown controller parameters: {sorted(bad)}")
        self.p = ControllerParams(**overrides)
        self.sys = system
        self.monitor = monitor
        self.rc = reconfig
        self.trace: list[Decision] = []
        self.regime_changes: list[tuple] = []        # (window confirmed, from, to)
        self.confirmed_regime = None
        self._cand, self._cand_n = None, 0
        self._streak: dict = {}
        self._skip_until = 0
        self._last_heavy = None
        self._last_pf = None

    # ------------------------------------------------------------------
    # Core.run callback
    # ------------------------------------------------------------------
    def on_window(self, core, final):
        f = self.monitor.sample(core, final)
        if f is None or final or f.partial:
            return 0
        self.step(f)
        return 0

    # ------------------------------------------------------------------
    # One window
    # ------------------------------------------------------------------
    def step(self, f):
        p = self.p
        d = Decision(window=f.window, t_end=f.t_end)
        self.trace.append(d)
        if f.window < self._skip_until:
            d.skipped = True
            d.confirmed_regime = self.confirmed_regime
            return d

        d.regime = classify(f, p)
        self._update_regime(f.window, d.regime)
        d.confirmed_regime = self.confirmed_regime

        proposals = self.propose(f)
        d.proposals = [pr.rule for pr in proposals]
        live = {pr.key for pr in proposals}
        self._streak = {k: n + 1 for k, n in ((k, self._streak.get(k, 0)) for k in live)}

        allowed = []
        for pr in proposals:
            need = min(p.hysteresis_m_fast, p.hysteresis_m) if pr.fast else p.hysteresis_m
            if self._streak[pr.key] < need:
                continue
            why = self._not_allowed(pr, f)
            if why:
                d.blocked = d.blocked or f"{pr.rule}: {why}"
                continue
            allowed.append(pr)
        if allowed:
            pr = max(allowed, key=lambda x: (x.urgent, self._net(x)))   # ties: rule order
            rec = self.rc.apply(pr.knob, pr.value, reason=pr.rule)
            if rec is not None:                       # None: already in that state
                d.action = f"{pr.knob} {rec.old}->{rec.new} ({pr.rule})"
                self._after_switch(pr.knob, f.window)
        return d

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    def _not_allowed(self, pr, f):
        p = self.p
        last, k = ((self._last_pf, p.residency_k_pf) if pr.knob == "prefetch"
                   else (self._last_heavy, p.residency_k))
        if last is not None and f.window - last < k:
            return f"residency ({f.window - last}/{k} windows)"
        if not pr.exempt:
            cost = self._cost(pr)
            gain = pr.saved_per_window * p.horizon_h
            if gain <= cost:
                return f"utility ({gain:.0f} <= {cost:.0f} cycles)"
        return ""

    def _cost(self, pr):
        return self.rc.estimate(pr.knob) * (self.p.assoc_margin if pr.knob == "assoc" else 1.0)

    def _net(self, pr):
        """Expected net benefit over the horizon (0 for exempt moves)."""
        if pr.exempt:
            return 0.0
        return pr.saved_per_window * self.p.horizon_h - self._cost(pr)

    def _after_switch(self, knob, window):
        if knob == "prefetch":
            self._last_pf = window
        else:
            self._last_heavy = window
        self._streak.clear()
        self._skip_until = window + 1 + self.p.skip_after_switch

    def _update_regime(self, window, regime):
        if regime == self._cand:
            self._cand_n += 1
        else:
            self._cand, self._cand_n = regime, 1
        if self._cand_n >= self.p.hysteresis_m and regime != self.confirmed_regime:
            if self.confirmed_regime is not None:
                self.regime_changes.append((window, self.confirmed_regime, regime))
            self.confirmed_regime = regime

    # ------------------------------------------------------------------
    # The rules (pure: Features in, proposals out)
    # ------------------------------------------------------------------
    def propose(self, f):
        return self._prefetch_rules(f) + self._mshr_rules(f) + self._assoc_rules(f)

    def _prefetch_rules(self, f):
        p, ladder = self.p, cfg.PF_DISTANCE_LADDER
        mode, dist = f.pf_mode, f.pf_distance
        if mode is None:
            return []
        acc, cov, late = f.pf_accuracy, f.pf_coverage, f.pf_late_frac
        seq, stride = f.seq_score, f.stride_conf
        out = []
        if mode != "off":
            irregular = (seq is not None and stride is not None
                         and seq < p.irregular and stride < p.irregular)
            useless = ((acc is not None and acc < p.acc_off)
                       or (cov is not None and f.pf_issued >= p.pf_min_issued and cov < p.cov_off))
            if irregular or useless:
                out.append(Proposal("prefetch", ("off", 0), "pf-off", 0.0,
                                    fast=True, exempt=True, urgent=True))
            lower = _step_down(ladder, dist)
            if lower is not None and ((acc is not None and acc < p.acc_low)
                                      or (f.bw_util >= p.bw_saturated
                                          and (acc is None or acc < p.acc_high))):
                out.append(Proposal("prefetch", (mode, lower), "pf-lower", 0.0,
                                    exempt=True, urgent=True))
            higher = _step_up(ladder, dist)
            if (higher is not None and acc is not None and acc >= p.acc_high
                    and late is not None and late > p.late_high
                    and f.bw_util < p.bw_saturated and f.mshr_occ_frac < p.mshr_high):
                saved = f.cycles * f.stall_frac * cfg.PF_COV_GAIN_PER_STEP
                out.append(Proposal("prefetch", (mode, higher), "pf-raise", saved))
        else:
            hurts = f.stall_frac >= p.stall_attention and f.dep_stall_frac < p.dep_stall_latency
            room = f.bw_util < p.bw_saturated and f.mshr_occ_frac < p.mshr_high
            if hurts and room:
                if stride is not None and stride >= p.seq_threshold:
                    new, hit = "stride", cfg.PF_EXPECTED_HIT_STRIDE
                elif seq is not None and seq >= p.seq_threshold:
                    new, hit = "next-line", cfg.PF_EXPECTED_HIT_NEXTLINE
                else:
                    new = None
                if new:
                    out.append(Proposal("prefetch", (new, cfg.PF_DISTANCE_DEFAULT[new]), "pf-on",
                                        f.cycles * f.stall_frac * hit))
        return out

    def _mshr_rules(self, f):
        p, ladder = self.p, cfg.MSHR_CHOICES
        n = f.n_mshr
        out = []
        up = _step_up(ladder, n)
        junk = (f.pf_mode not in (None, "off") and f.pf_accuracy is not None
                and f.pf_accuracy < p.acc_low)
        if (up is not None and f.mshr_occ_frac >= p.mshr_high
                and f.dep_stall_frac < p.dep_stall_latency
                and f.bw_util < p.bw_saturated and not junk):
            speedup = min(up / n, 1.0 / max(f.bw_util, 1e-3))
            out.append(Proposal("n_mshr", up, "mshr-raise",
                                f.cycles * f.stall_frac * (1.0 - 1.0 / speedup)))
        down = _step_down(ladder, n)
        if (p.enable_mshr_shrink and down is not None and f.mshr_occ_frac < p.mshr_low
                and down >= 2 * f.mshr_occ):
            out.append(Proposal("n_mshr", down, "mshr-shrink", 0.0, exempt=True))
        return out

    def _assoc_rules(self, f):
        p = self.p
        if not f.shadow_valid:
            return []
        cur = f.assoc
        rates = f.shadow_rate
        rc = rates.get(cur)
        if rc is None:
            return []
        out = []
        if f.stall_frac >= p.stall_assoc and rc > 0:
            better = [(r, a) for a, r in rates.items()
                      if a != cur and r is not None and r < p.shadow_improvement * rc]
            if better:
                r, a = min(better)
                out.append(Proposal("assoc", a, "assoc-better",
                                    f.cycles * f.stall_frac * (1.0 - r / rc)))
        if p.enable_assoc_shrink:
            lower = [a for a in cfg.L2_ASSOC_CHOICES if a < cur
                     and rates.get(a) is not None and rates[a] <= p.shadow_no_loss * rc]
            if lower:
                out.append(Proposal("assoc", max(lower), "assoc-shrink", 0.0, exempt=True))
        return out

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def trace_rows(self):
        return [{"window": d.window, "t_end": d.t_end, "regime": d.regime,
                 "confirmed_regime": d.confirmed_regime, "skipped": d.skipped,
                 "proposals": ";".join(d.proposals), "action": d.action,
                 "blocked": d.blocked} for d in self.trace]
