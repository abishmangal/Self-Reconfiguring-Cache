"""
check_base.py — Validate the base simulator before extending it.

Run:  python check_base.py          (all checks)
      python check_base.py -k mshr  (only checks whose name contains "mshr")

Covers: core issue rate, scans, fitting working sets, conflict misses vs
associativity, dirty writebacks, bus bandwidth, MSHR scaling, ROB limit,
dependent loads, pending-fill merging, flush/set_assoc primitives, drain,
the window callback, workload properties, and the scale of the default
workload (windows per phase, stall fraction per phase).
"""

import json
import os
import sys
import tempfile
import traceback

import sim_config as cfg
from memory import Memory
from system import build_system
from workload import (OP_COMPUTE, OP_LOAD, OP_STORE, PHASE_KINDS, REGION_SHIFT,
                      Workload, make_workload)

L1_LINES = cfg.L1_SIZE // cfg.LINE_SIZE
L2_LINES = cfg.L2_SIZE // cfg.LINE_SIZE


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def mem_ops(lines, kind=OP_LOAD, dep=False, pc=0x100, base=0):
    return [(kind, base + ln * cfg.LINE_SIZE, dep, pc) for ln in lines]


def run(s, ops, **kw):
    s.core.run(iter(ops), **kw)
    return s.core.cycle


def conflict_lines():
    stride = cfg.L2_SIZE // (min(cfg.L2_ASSOC_CHOICES) * cfg.LINE_SIZE)
    return [r + j * stride for j in range(cfg.CONFLICT_WAYS)
            for r in range(cfg.CONFLICT_SETS)]


# ----------------------------------------------------------------------
# core
# ----------------------------------------------------------------------
def test_compute_ipc():
    s = build_system()
    n = 40_000
    run(s, [(OP_COMPUTE, None, False, 0)] * n)
    ipc = s.core.snapshot()["instructions"] / s.core.cycle
    print(f"[ipc] pure compute IPC = {ipc:.3f} (width {cfg.CORE_WIDTH})")
    assert abs(ipc - cfg.CORE_WIDTH) < 1e-6
    assert s.core.mem_stall_cycles() == 0


def test_sequential_scan():
    n = 4 * L2_LINES
    s = build_system()
    run(s, mem_ops(range(n)))
    print(f"[scan] {n} lines, {s.core.cycle} cycles, L2 miss rate = {s.l2.miss_rate():.3f}")
    assert s.l2.stats["misses"] == n, "every line of a scan is a cold miss"
    assert s.memory.stats["reads"] == n
    s.check_invariants()


def test_fitting_working_set():
    n = L2_LINES // 2                      # bigger than L1, fits in L2
    s = build_system()
    run(s, mem_ops(range(n)))              # warm-up
    s.reset_counters()
    run(s, mem_ops(list(range(n)) * 4))
    print(f"[fit] L1 miss rate = {s.l1.miss_rate():.3f}, L2 miss rate = {s.l2.miss_rate():.3f}")
    assert s.l2.stats["misses"] == 0, "working set fits in L2: no L2 misses after warm-up"
    assert s.l1.miss_rate() > 0.99, "working set exceeds L1: L1 should thrash"
    s.check_invariants()


def test_conflict_vs_assoc():
    lines = conflict_lines()
    res = {}
    for assoc in cfg.L2_ASSOC_CHOICES:
        s = build_system(l2_assoc=assoc)
        run(s, mem_ops(lines))             # warm-up
        s.reset_counters()
        run(s, mem_ops(lines * 8))
        res[assoc] = (s.l2.miss_rate(), s.core.cycle, s.l1.miss_rate())
        s.check_invariants()
    print("[conflict] " + ", ".join(
        f"{a}-way: L2 miss {m:.3f} ({c} cyc)" for a, (m, c, _) in sorted(res.items())))
    top = max(cfg.L2_ASSOC_CHOICES)
    for a, (m, _, l1m) in res.items():
        assert l1m > 0.9, "conflict pattern must overflow L1 so the L2 sees it"
        if a < cfg.CONFLICT_WAYS:
            assert m > 0.95, f"{a}-way should thrash"
    assert res[top][0] < 0.02, f"{top}-way should hold the conflict set"
    assert res[min(res)][1] > res[top][1], "thrashing must cost cycles"


def test_writebacks():
    n = 4 * L2_LINES
    s = build_system()
    run(s, mem_ops(range(n)))
    assert s.memory.stats["writes"] == 0, "read-only scan must not write back"
    s2 = build_system()
    run(s2, mem_ops(range(n), kind=OP_STORE))
    w = s2.memory.stats["writes"]
    print(f"[writeback] store scan of {n} lines -> {w} memory writes")
    assert n - L2_LINES - L1_LINES <= w <= n
    s2.check_invariants()


def test_bandwidth():
    n = 64
    for bus in (8, 32):
        m = Memory(bus_cycles=bus)
        last = 0
        for i in range(n):
            last = m.access(i * cfg.LINE_SIZE, 0).completion
        expect = (n - 1) * bus + m.latency
        print(f"[bandwidth] bus={bus}: last completion {last} (expected {expect})")
        assert last == expect
    m = Memory()
    m.writeback(0, 0)
    assert m.access(0, 0).completion == m.bus_cycles + m.latency, "writebacks use the bus"


def test_mshr_scaling():
    n = 20_000
    cyc = {}
    for m in cfg.MSHR_CHOICES:
        s = build_system(l2_mshr=m)
        cyc[m] = run(s, mem_ops(range(n)))
        s.check_invariants()
    print("[mshr] cycles: " + ", ".join(f"{m}: {c}" for m, c in cyc.items()))
    ms = list(cfg.MSHR_CHOICES)
    for lo, hi in zip(ms, ms[1:]):
        assert cyc[hi] <= cyc[lo], "more MSHRs must never be slower here"
    assert cyc[ms[0]] > cyc[ms[-1]] * 1.5, "small MSHR count must visibly limit MLP"
    assert cyc[ms[-1]] >= n * cfg.DRAM_BUS_CYCLES, "bandwidth is the floor"
    # With plenty of MSHRs the stream should be bus-bound, not latency-bound
    assert cyc[ms[-1]] < n * cfg.DRAM_BUS_CYCLES * 1.5


def test_mshr_saturation_under_bandwidth_pressure():
    """With the bus twice as slow, the bus is the limit from 16 MSHRs up, so
    going 16 -> 32 must give nothing. This is the 'more MSHRs stops helping'
    case; the default bus only reaches that floor at 32."""
    n = 20_000
    bus = 2 * cfg.DRAM_BUS_CYCLES
    cyc = {}
    for m in cfg.MSHR_CHOICES:
        s = build_system(l2_mshr=m, bus_cycles=bus)
        cyc[m] = run(s, mem_ops(range(n)))
        s.check_invariants()
    print(f"[mshr-sat] bus={bus}: " + ", ".join(f"{m}: {c}" for m, c in cyc.items()))
    assert cyc[4] > 2.5 * cyc[16], "few MSHRs must still be latency-bound"
    assert cyc[32] >= 0.99 * cyc[16], "bus-bound: extra MSHRs give no benefit"
    assert cyc[16] >= n * bus


def test_compute_insensitive_to_mshr():
    """A compute phase has no steady-state misses, so 4 MSHRs behave like 32."""
    cyc = {}
    for m in (4, 32):
        s = build_system(l2_mshr=m)
        s.core.run(Workload([("compute", 100_000)]).ops())
        cyc[m] = s.core.cycle
    print(f"[mshr-compute] cycles with 4 MSHRs: {cyc[4]}, with 32: {cyc[32]}")
    assert abs(cyc[4] - cyc[32]) / cyc[32] < 0.01


def test_rob_limit():
    n = 8_000
    small = build_system(rob_size=4)
    big = build_system(rob_size=cfg.ROB_SIZE)
    c_small = run(small, mem_ops(range(n)))
    c_big = run(big, mem_ops(range(n)))
    print(f"[rob] rob=4: {c_small} cycles, rob={cfg.ROB_SIZE}: {c_big} cycles")
    assert c_small > 2 * c_big
    assert small.core.stats["stall_rob"] > 0


def test_dependent_chain():
    n = 300
    w = Workload([("pointer", n)])
    ops = list(w.ops())
    assert not ops[0][2] and all(o[2] for o in ops[1:])
    dep = build_system()
    c_dep = run(dep, ops)
    ind = build_system()
    c_ind = run(ind, [(k, a, False, p) for k, a, _, p in ops])
    print(f"[dependent] chain: {c_dep} cycles, same loads independent: {c_ind} cycles")
    assert c_dep >= n * cfg.DRAM_LATENCY
    assert c_ind < c_dep / 4
    assert dep.core.stats["stall_dep"] > 0
    dep.check_invariants()


def test_pending_hit_merges():
    s = build_system()
    r1 = s.l1.access(0, 0)
    r2 = s.l1.access(0, 1)
    assert not r1.hit and r2.hit
    assert r2.completion == r1.completion, "second request waits for the in-flight fill"
    assert s.memory.stats["reads"] == 1, "no second memory request"
    assert s.l1.stats["late_hits"] == 1


def test_refused_access_changes_nothing():
    s = build_system(l2_mshr=2)
    for i in range(2):
        assert s.l1.access(i * cfg.LINE_SIZE, 0).completion is not None
    before = (dict(s.l1.stats), dict(s.l2.stats), dict(s.memory.stats), s.l1.count_valid())
    r = s.l1.access(10 * cfg.LINE_SIZE, 0)
    assert r.completion is None and r.retry_at is not None and r.retry_at > 0
    after = (dict(s.l1.stats), dict(s.l2.stats), dict(s.memory.stats), s.l1.count_valid())
    for b, a in zip(before, after):
        if isinstance(b, dict):
            b, a = dict(b), dict(a)
            b.pop("blocked", None); a.pop("blocked", None)
        assert b == a, "a refused access must not change cache or memory state"
    r2 = s.l1.access(10 * cfg.LINE_SIZE, r.retry_at)
    assert r2.completion is not None, "retry at retry_at succeeds"


def test_flush_and_set_assoc():
    s = build_system(l2_assoc=8)
    n = 100
    for i in range(n):
        s.l2.access(i * cfg.LINE_SIZE, i * 1000, is_write=True)
    # outstanding miss -> refuse
    s.l2.access(500 * cfg.LINE_SIZE, 10**6)
    try:
        s.l2.flush_invalidate(10**6)
        raise AssertionError("flush with outstanding misses must raise")
    except RuntimeError:
        pass
    s.l2.retire_all(10**7)
    n_valid, n_dirty = s.l2.flush_invalidate(10**7)
    assert (n_valid, n_dirty) == (n + 1, n)
    assert s.memory.stats["writes"] == n
    assert s.l2.count_valid() == 0
    old_sets = s.l2.n_sets
    s.l2.set_assoc(4)
    assert s.l2.assoc == 4 and s.l2.n_sets == old_sets * 2
    assert s.l2.size == s.l2.n_sets * s.l2.assoc * cfg.LINE_SIZE, "capacity unchanged"
    r1 = s.l2.access(0, 10**8)
    r2 = s.l2.access(0, 10**8 + 1000)
    assert not r1.hit and r2.hit
    try:
        s.l2.retire_all(10**9)
        s.l2.set_assoc(8)
        raise AssertionError("set_assoc on a non-empty cache must raise")
    except RuntimeError:
        pass
    try:
        s.l2.flush_invalidate(10**9)
        s.l2.set_assoc(3)
        raise AssertionError("invalid associativity must raise")
    except ValueError:
        pass


def test_drain():
    s = build_system()
    s.core.run(iter(mem_ops(range(200))), max_ops=200)   # stop without the end-of-run wait
    t = s.l1.drain_time()
    spent = s.core.drain()
    print(f"[drain] drained in {spent} cycles")
    assert spent > 0 and s.core.cycle >= t and not s.core.rob
    assert not s.l1.mshr and not s.l2.mshr
    assert s.core.drain() == 0
    assert s.core.stats["drain_cycles"] == spent


def test_window_callback():
    n = 40_000
    ops = [(OP_COMPUTE, None, False, 0)] * n
    s = build_system()
    calls = []
    run(s, ops, on_window=lambda core, final: calls.append((core.cycle, final)),
        window_cycles=1_000)
    assert [c for c, f in calls if not f] == list(range(1000, 10_001, 1000))
    assert calls[-1] == (10_000, True)

    s2 = build_system()
    seen = []
    def cb(core, final):
        seen.append(final)
        return 500 if (not final and len(seen) == 1) else 0
    run(s2, ops, on_window=cb, window_cycles=1_000)
    assert s2.core.cycle == 10_500
    assert s2.core.stats["reconfig_cycles"] == 500
    assert s2.core.mem_stall_cycles() == 0, "reconfig stall must not pollute memory stalls"



class _NextLinePrefetcher:
    """Minimal prefetcher used only to verify the hook plumbing."""
    def __init__(self, degree=4):
        self.degree = degree
    def observe(self, addr, pc, hit, now):
        return [addr + d * cfg.LINE_SIZE for d in range(1, self.degree + 1)]


def _paced(ops, pad):
    """Insert `pad` compute ops after every memory op (slows the access rate so
    the MSHRs are not saturated and prefetches have room)."""
    out = []
    pad_ops = [(OP_COMPUTE, None, False, 0)] * pad
    for op in ops:
        out.append(op)
        out.extend(pad_ops)
    return out


def test_prefetch_hook():
    n = L2_LINES // 2
    seq = _paced(mem_ops(range(n)), 100)
    plain = build_system()
    c_plain = run(plain, seq)
    pf = build_system(l2_prefetcher=_NextLinePrefetcher())
    c_pf = run(pf, seq)
    st = pf.l2.stats
    print(f"[prefetch] plain {c_plain} cycles, next-line {c_pf} cycles, "
          f"issued {st['pf_issued']}, useful {st['pf_useful']}, late {st['pf_late']}, "
          f"dropped {st['pf_dropped']}")
    assert st["pf_issued"] > 0 and st["pf_useful"] > 0
    assert st["pf_useful"] <= st["pf_issued"]
    assert c_pf < 0.8 * c_plain, "next-line prefetch must help a sequential scan"
    assert pf.memory.stats["reads"] < plain.memory.stats["reads"], \
        "prefetched lines turn demand misses into hits"
    pf.check_invariants()

    # Random addresses: the same prefetcher must be much less accurate.
    import random
    rng = random.Random(3)
    rnd_lines = [rng.randrange(0, 8 * L2_LINES) * 8 for _ in range(n)]
    rnd = build_system(l2_prefetcher=_NextLinePrefetcher())
    run(rnd, _paced(mem_ops(rnd_lines), 100))
    acc_rnd = rnd.l2.stats["pf_useful"] / max(1, rnd.l2.stats["pf_issued"])
    acc_seq = st["pf_useful"] / st["pf_issued"]
    print(f"[prefetch] accuracy sequential {acc_seq:.2f}, random {acc_rnd:.2f}; "
          f"unused evicted (random) {rnd.l2.stats['pf_unused_evicted']}")
    assert acc_seq > 0.5 and acc_rnd < 0.1
    rnd.check_invariants()


# ----------------------------------------------------------------------
# workload
# ----------------------------------------------------------------------
def test_workload_properties():
    length = 2_000
    for kind in PHASE_KINDS:
        w = Workload([(kind, length)], seed=7)
        a, b = list(w.ops()), list(w.ops())
        assert len(a) == length, f"{kind}: must yield exactly `length` ops"
        assert a == b, f"{kind}: stream must be identical on every iteration"
        if kind != "strided":   # strided has no randomness at all
            assert a != list(Workload([(kind, length)], seed=8).ops()), \
                f"{kind}: different seeds should give different streams"
        for op_kind, addr, dep, pc in a:
            if op_kind == OP_COMPUTE:
                assert addr is None and not dep
            else:
                assert addr is not None and addr >= 0
                assert addr >> REGION_SHIFT == PHASE_KINDS.index(kind), \
                    f"{kind}: address outside its region"
                assert addr % cfg.LINE_SIZE == 0

    w = Workload([("compute", length), ("streaming", length), ("pointer", length)])
    assert [b["end_op"] - b["start_op"] for b in w.boundaries] == [length] * 3
    assert w.boundaries[-1]["end_op"] == w.total_ops == 3 * length

    # footprints
    fp = w.footprint_lines
    assert fp["compute"] <= 8
    for k in ("streaming", "strided", "pointer", "mixed"):
        assert fp[k] >= 4 * L2_LINES, f"{k} footprint must be >= 4x L2"

    # pointer chain is a single cycle through every line
    chain = w.chain
    seen, cur = set(), 0
    for _ in range(len(chain)):
        seen.add(cur)
        cur = chain[cur]
    assert len(seen) == len(chain) and cur == 0

    # stores exist where intended
    kinds = {k: {o[0] for o in Workload([(k, length)]).ops()} for k in PHASE_KINDS}
    assert OP_STORE in kinds["streaming"] and OP_STORE in kinds["compute"]
    assert OP_STORE not in kinds["pointer"]

    # conflict: exactly CONFLICT_WAYS lines per used L2 set at every associativity
    lines = {o[1] // cfg.LINE_SIZE for o in Workload([("conflict", 10_000)]).ops()
             if o[0] != OP_COMPUTE}
    lines = {ln & ((1 << (REGION_SHIFT - 6)) - 1) for ln in lines}
    for assoc in cfg.L2_ASSOC_CHOICES:
        n_sets = cfg.L2_SIZE // (assoc * cfg.LINE_SIZE)
        per_set = {}
        for ln in lines:
            per_set.setdefault(ln % n_sets, set()).add(ln)
        assert len(per_set) == cfg.CONFLICT_SETS
        assert all(len(v) == cfg.CONFLICT_WAYS for v in per_set.values())

    # boundary file round-trip
    path = os.path.join(tempfile.mkdtemp(), "b.json")
    w.write_boundaries(path)
    assert json.load(open(path)) == w.boundaries


def test_default_workload_scale():
    """Run the default workload on the baseline config and report windows,
    IPC and memory-stall fraction per phase. Informational numbers that the
    controller thresholds will be built around; loose sanity asserts only."""
    w = make_workload()
    s = build_system()
    recs = []
    def cb(core, final):
        snap = core.snapshot()
        recs.append((snap["cycle"], snap["instructions"],
                     core.mem_stall_cycles(), s.l2.stats["misses"]))
    s.core.run(w.ops(), on_window=cb)
    s.check_invariants()

    rows = []
    for b in w.boundaries:
        cyc = ins = stall = miss = n_win = 0
        for prev, cur in zip([(0, 0, 0, 0)] + recs, recs):
            if b["start_op"] <= prev[1] and cur[1] <= b["end_op"]:
                n_win += 1
                cyc += cur[0] - prev[0]
                ins += cur[1] - prev[1]
                stall += cur[2] - prev[2]
                miss += cur[3] - prev[3]
        rows.append((b["kind"], n_win, ins / cyc if cyc else 0,
                     stall / cyc if cyc else 0, 1000 * miss / ins if ins else 0))
    print("[scale] phase       windows   IPC   stall_frac   L2 MPKI")
    for kind, n_win, ipc, sf, mpki in rows:
        print(f"[scale] {kind:<10} {n_win:7d} {ipc:6.2f} {sf:11.2f} {mpki:9.1f}")
    for kind, n_win, ipc, sf, mpki in rows:
        assert n_win >= 8, f"{kind} phase too short for hysteresis/residency"
    by_kind = {}
    for kind, n_win, ipc, sf, mpki in rows:
        by_kind.setdefault(kind, []).append((ipc, sf, mpki))
    assert all(sf < 0.1 for _, sf, _ in by_kind["compute"])
    assert all(sf > 0.4 for _, sf, _ in by_kind["streaming"])
    assert all(sf > 0.8 for _, sf, _ in by_kind["pointer"])


# ----------------------------------------------------------------------
def main(argv):
    pattern = argv[argv.index("-k") + 1] if "-k" in argv else ""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and pattern in n]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - len(failed)}/{len(tests)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))