"""
sim.py — Runs one workload in one of three modes and returns a Result.

    python sim.py --mode dynamic
    python sim.py --mode oracle --phases compute:200000,streaming:40000
    python sim.py --mode baseline --assoc 4 --mshr 8 --pf stride:4

baseline : one fixed configuration for the whole run.
oracle   : the best configuration for each phase, found by trying candidates on
           that phase alone (coordinate descent, a change must beat the current
           choice by ORACLE_MIN_GAIN), then applied at the phase boundaries for FREE: no
           stall, no flush, associativity changed with the idealized remap. It
           uses the workload's phase labels, so it is an upper-bound reference,
           not a deployable controller.
dynamic  : monitor + controller + reconfigurator. Decisions use live counters
           only; every switch is charged to the clock and logged.

All three attach a monitor, so every run has a per-window feature log for the
charts. Resource use (time-weighted ways, MSHR entries and prefetch-on share) is
reported for every mode.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import sim_config as cfg
from controller import Controller
from monitor import Monitor
from reconfig import Reconfigurator
from system import build_system
from workload import PHASE_KINDS, Workload, make_workload

PF_OPTIONS = [("off", 0)] + [(m, d) for m in ("next-line", "stride") for d in cfg.PF_DISTANCE_LADDER]

# Ops simulated per phase kind when the oracle tries candidate configurations.
ORACLE_MIN_GAIN = 0.02      # a candidate must beat the current choice by this fraction

# Compute keeps a long probe: a short one is dominated by cold-start misses and
# makes a prefetcher look useful. The memory-bound kinds reach steady state quickly
# (12k-op probes pick the same configurations as 30k-60k-op ones).
PROBE_OPS = {"compute": 60_000, "streaming": 12_000, "strided": 12_000, "mixed": 12_000,
             "pointer": 600, "conflict": 12_000, "chase_stream": 8_000}


# ----------------------------------------------------------------------
# configuration helpers
# ----------------------------------------------------------------------
def make_config(overrides=None):
    """BASELINE_CONFIG with any keys replaced (l2_assoc, pf_mode, pf_distance, n_mshr)."""
    c = dict(cfg.BASELINE_CONFIG)
    ov = dict(overrides or {})
    for k, v in ov.items():
        if k not in c:
            raise ValueError(f"unknown config key {k!r}; choose from {sorted(c)}")
        c[k] = v
    if "pf_mode" in ov and "pf_distance" not in ov:
        c["pf_distance"] = None                 # a new mode brings its own default distance
    c["pf_mode"], c["pf_distance"] = _resolve_pf(c["pf_mode"], c["pf_distance"])
    return c


def _resolve_pf(mode, distance):
    from prefetch import PrefetchUnit
    return PrefetchUnit.resolve(mode, distance if mode != "off" else 0)


def build(config):
    return build_system(l2_assoc=config["l2_assoc"], l2_mshr=config["n_mshr"],
                        pf_mode=config["pf_mode"], pf_distance=config["pf_distance"])


def _with(config, knob, value):
    c = dict(config)
    if knob == "prefetch":
        c["pf_mode"], c["pf_distance"] = value
    else:
        c[knob if knob != "assoc" else "l2_assoc"] = value
    return c


# ----------------------------------------------------------------------
# result
# ----------------------------------------------------------------------
def resource_use(start, changes, total_cycles):
    """Time-weighted average L2 ways and MSHR entries, and the share of time the
    prefetcher was on. `changes` is [(cycle, config_key, new_value)] in order."""
    cur = dict(start)
    t_prev = 0
    acc = {"ways": 0.0, "mshr": 0.0, "pf_on": 0.0}

    def close(t):
        nonlocal t_prev
        span = max(0, min(t, total_cycles) - t_prev)
        acc["ways"] += cur["l2_assoc"] * span
        acc["mshr"] += cur["n_mshr"] * span
        acc["pf_on"] += (cur["pf_mode"] != "off") * span
        t_prev = max(t_prev, min(t, total_cycles))

    for cycle, key, value in changes:
        close(cycle)
        cur[key] = value
    close(total_cycles)
    n = max(1, total_cycles)
    return {"avg_ways": acc["ways"] / n, "avg_mshr": acc["mshr"] / n, "pf_on_frac": acc["pf_on"] / n}


@dataclass
class Result:
    mode: str
    system: object
    monitor: Monitor
    start_config: dict
    config_changes: list = field(default_factory=list)   # (cycle, config_key, new_value)
    controller: Controller | None = None
    reconfig: Reconfigurator | None = None
    schedule: list | None = None                         # oracle: config per phase
    phases: list = field(default_factory=list)           # the workload's (kind, ops) list

    @property
    def cycles(self):
        return self.system.core.cycle

    @property
    def instructions(self):
        return self.system.core.snapshot()["instructions"]

    @property
    def ipc(self):
        return self.instructions / self.cycles if self.cycles else 0.0

    @property
    def overhead_cycles(self):
        s = self.system.core.stats
        return s["reconfig_cycles"] + s["drain_cycles"]

    def resources(self):
        return resource_use(self.start_config, self.config_changes, self.cycles)

    def summary(self):
        l2, mem = self.system.l2, self.system.memory
        d = {
            "mode": self.mode, "cycles": self.cycles, "instructions": self.instructions,
            "ipc": round(self.ipc, 4), "overhead_cycles": self.overhead_cycles,
            "overhead_pct": round(100.0 * self.overhead_cycles / self.cycles, 3) if self.cycles else 0.0,
            "switches": len(self.reconfig.log) if self.reconfig else len(self.config_changes),
            "l2_misses": l2.stats["misses"], "l2_miss_rate": round(l2.miss_rate(), 4),
            "mem_reads": mem.stats["reads"] + mem.stats["prefetch_reads"],
            "mem_writes": mem.stats["writes"],
        }
        d.update({k: round(v, 3) for k, v in self.resources().items()})
        return d


# ----------------------------------------------------------------------
# modes
# ----------------------------------------------------------------------
def run_baseline(workload, config=None):
    config = make_config(config)
    s = build(config)
    mon = Monitor(s)
    s.core.run(workload.ops(), on_window=mon.on_window)
    s.check_invariants()
    return Result("baseline", s, mon, config, phases=list(workload.phases))


def run_dynamic(workload, config=None, **controller_overrides):
    config = make_config(config)
    s = build(config)
    mon = Monitor(s)
    rc = Reconfigurator(s, mon)
    ctl = Controller(s, mon, rc, **controller_overrides)
    s.core.run(workload.ops(), on_window=ctl.on_window)
    s.check_invariants()
    changes = [(r.cycle + r.total_cycles, key, val) for r in rc.log for key, val in _changes_of(r)]
    return Result("dynamic", s, mon, config, changes, controller=ctl, reconfig=rc,
                  phases=list(workload.phases))


def _changes_of(rec):
    if rec.knob == "prefetch":
        return [("pf_mode", rec.new[0]), ("pf_distance", rec.new[1])]
    return [("l2_assoc" if rec.knob == "assoc" else "n_mshr", rec.new)]


_search_cache: dict = {}


def _probe_cycles(kind, config, params, seed):
    s = build(config)
    s.core.run(Workload([(kind, PROBE_OPS[kind])], seed=seed, params=params).ops())
    return s.core.cycle


def best_config_for(kind, start, params=None, seed=None):
    """Best configuration for one phase kind by coordinate descent from `start`
    (prefetch, then associativity, then MSHRs, two passes). A change is taken only
    if it beats the current choice by at least ORACLE_MIN_GAIN, so phases where
    nothing matters keep the starting configuration."""
    key = (kind, tuple(sorted(start.items())), repr(params), seed)
    if key in _search_cache:
        return dict(_search_cache[key])
    cur = dict(start)
    knobs = (("prefetch", PF_OPTIONS), ("assoc", list(cfg.L2_ASSOC_CHOICES)),
             ("n_mshr", list(cfg.MSHR_CHOICES)))
    for _ in range(2):
        changed = False
        for knob, options in knobs:
            cyc = {opt: _probe_cycles(kind, _with(cur, knob, opt), params, seed) for opt in options}
            now = (cur["pf_mode"], cur["pf_distance"]) if knob == "prefetch" else \
                cur["l2_assoc" if knob == "assoc" else "n_mshr"]
            best = min(cyc, key=cyc.get)
            if best != now and cyc[best] < (1.0 - ORACLE_MIN_GAIN) * cyc[now]:
                cur = _with(cur, knob, best)
                changed = True
        if not changed:
            break
    _search_cache[key] = dict(cur)
    return dict(cur)


def oracle_schedule(workload, config=None, seed=None):
    start = make_config(config)
    return [best_config_for(kind, start, workload.params, seed if seed is not None else workload.seed)
            for kind, _ in workload.phases]


def _apply_free(system, old, new):
    if (new["pf_mode"], new["pf_distance"]) != (old["pf_mode"], old["pf_distance"]):
        system.pf.set_config(new["pf_mode"], new["pf_distance"])
    if new["n_mshr"] != old["n_mshr"]:
        system.l2.set_n_mshr(new["n_mshr"])
    if new["l2_assoc"] != old["l2_assoc"]:
        system.l2.remap_assoc(new["l2_assoc"], system.core.cycle)


def run_oracle(workload, config=None, schedule=None):
    start = make_config(config)
    schedule = schedule or oracle_schedule(workload, start)
    s = build(schedule[0])
    mon = Monitor(s)
    changes = []
    cur = dict(schedule[0])
    for key, val in schedule[0].items():
        if start[key] != val:
            changes.append((0, key, val))
    starts = [b["start_op"] for b in workload.boundaries]

    def ops():
        nonlocal cur
        nxt = 1
        for i, op in enumerate(workload.ops()):
            while nxt < len(starts) and i == starts[nxt]:
                new = schedule[nxt]
                if new != cur:
                    _apply_free(s, cur, new)
                    changes.extend((s.core.cycle, k, v) for k, v in new.items() if cur[k] != v)
                    cur = dict(new)
                nxt += 1
            yield op

    s.core.run(ops(), on_window=mon.on_window)
    s.check_invariants()
    return Result("oracle", s, mon, start, changes, schedule=schedule,
                  phases=list(workload.phases))


def run(mode, workload, config=None, **kw):
    if mode == cfg.MODE_BASELINE:
        return run_baseline(workload, config)
    if mode == cfg.MODE_DYNAMIC:
        return run_dynamic(workload, config, **kw)
    if mode == cfg.MODE_ORACLE:
        return run_oracle(workload, config, **kw)
    raise ValueError(f"unknown mode {mode!r}")


# ----------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------
def parse_phases(text):
    phases = []
    for item in text.split(","):
        kind, _, n = item.partition(":")
        if kind not in PHASE_KINDS or not n.isdigit() or int(n) <= 0:
            raise ValueError(f"bad phase {item!r}: use kind:ops with kind in {PHASE_KINDS}")
        phases.append((kind, int(n)))
    return phases


def parse_pf(text):
    mode, _, dist = text.partition(":")
    return _resolve_pf(mode, int(dist) if dist else None)


def print_report(res, out=print):
    out(f"mode {res.mode}: {res.cycles} cycles, {res.instructions} instructions, IPC {res.ipc:.3f}")
    for k, v in res.summary().items():
        if k not in ("mode", "cycles", "instructions", "ipc"):
            out(f"  {k:16s} {v}")
    if res.reconfig and res.reconfig.log:
        out("  switches (window, cycle, knob, change, cost, rule):")
        for r in res.reconfig.log:
            out(f"    w{r.window:<4} {r.cycle:>9}  {r.knob:<8} {r.old} -> {r.new}  "
                f"{r.total_cycles} cycles  {r.reason}")
        for row in res.reconfig.overhead_table():
            if row["switches"]:
                out(f"  overhead {row['knob']:<8} {row['switches']} switches, "
                    f"{row['total_cycles']} cycles ({row['pct_of_runtime']:.2f}% of runtime), "
                    f"estimated {row['estimated_cycles']}, refetch misses {row['refetch_misses']}")
    if res.schedule:
        out("  oracle schedule:")
        for (kind, n), c in zip(res.phases, res.schedule):
            out(f"    {kind:<10} {n:>8} ops  {c}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Self-evolving L2 simulator")
    ap.add_argument("--mode", choices=(cfg.MODE_BASELINE, cfg.MODE_ORACLE, cfg.MODE_DYNAMIC),
                    default=cfg.MODE_DYNAMIC)
    ap.add_argument("--phases", help="kind:ops,kind:ops (default: sim_config.DEFAULT_PHASES)")
    ap.add_argument("--seed", type=int, default=cfg.DEFAULT_SEED)
    ap.add_argument("--assoc", type=int, help="starting L2 associativity")
    ap.add_argument("--mshr", type=int, help="starting L2 MSHR count")
    ap.add_argument("--pf", help="starting prefetch, e.g. off, stride, next-line:4")
    ap.add_argument("--csv", help="write <prefix>_windows.csv, _switches.csv, _trace.csv")
    args = ap.parse_args(argv)

    overrides = {}
    if args.assoc:
        overrides["l2_assoc"] = args.assoc
    if args.mshr:
        overrides["n_mshr"] = args.mshr
    if args.pf:
        overrides["pf_mode"], overrides["pf_distance"] = parse_pf(args.pf)
    wl = Workload(parse_phases(args.phases), seed=args.seed) if args.phases \
        else make_workload(seed=args.seed)
    res = run(args.mode, wl, overrides)
    print_report(res)
    if args.csv:
        import csv
        def dump(path, rows):
            if rows:
                with open(path, "w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                    w.writeheader()
                    w.writerows(rows)
        dump(f"{args.csv}_windows.csv", res.monitor.to_rows())
        if res.reconfig:
            dump(f"{args.csv}_switches.csv", res.reconfig.log_rows())
        if res.controller:
            dump(f"{args.csv}_trace.csv", res.controller.trace_rows())
    return 0


if __name__ == "__main__":
    sys.exit(main())
