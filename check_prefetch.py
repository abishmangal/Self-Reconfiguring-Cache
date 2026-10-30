"""
check_prefetch.py — Validate prefetch.py and its integration with the caches.

Run:  python check_prefetch.py          (all)
      python check_prefetch.py -k stride (only names containing "stride")

Checks: stride table learning / noise tolerance / retraining, config
validation, detectors training while off, next-line helps streams and not
pointer chasing, stride helps strided access and next-line does not, stride
stays quiet on random access, distance hides latency, pollution grows with
distance on a reusable working set, MSHR reserve, counters.
"""

import random
import sys

import sim_config as cfg
from check_base import L2_LINES, _paced, main, mem_ops, run
from memory import Memory
from prefetch import PrefetchUnit, StrideTable
from system import build_system
from workload import OP_COMPUTE, OP_LOAD, Workload

N_STREAM = L2_LINES // 2          # lines per paced scan: all cold misses


def paced_seq(n=N_STREAM, pad=100):
    return _paced(mem_ops(range(n), pc=0x2000), pad)


def run_cfg(ops, mode="off", distance=None, **kw):
    s = build_system(pf_mode=mode, pf_distance=distance, **kw)
    run(s, ops)
    s.check_invariants()
    return s


def accuracy(s):
    st = s.l2.stats
    return st["pf_useful"] / st["pf_issued"] if st["pf_issued"] else 0.0


# ----------------------------------------------------------------------
# stride table
# ----------------------------------------------------------------------
def test_stride_table_learns_and_tolerates_noise():
    t = StrideTable()
    pc = 0x10
    res = [t.update(pc, 100 + 8 * i) for i in range(10)]
    assert res[0] == (False, False, 0, False), "first access only allocates"
    assert res[1][2] == 8 and not res[1][0], "second access proposes the stride"
    assert res[2][0] and res[2][1], "third access is trained and matches"
    first_conf = next(i for i, r in enumerate(res) if r[3])
    assert first_conf == 1 + t.conf_min, "confident after conf_min matches"
    assert all(r[3] for r in res[first_conf:])

    last = 100 + 8 * 9
    assert t.update(pc, last + 5000)[2] == 8, "one stray access keeps the stride"
    _, matched, stride, confident = t.update(pc, last + 5000 + 8)
    assert matched and stride == 8 and confident, "stream resumes after a stray access"


def test_stride_table_retrains_negative_and_ignores_huge():
    t = StrideTable()
    for i in range(10):
        t.update(0x10, 1000 + 8 * i)
    line = 1000 + 8 * 9
    for _ in range(15):                    # sustained new stride
        line += 3
        r = t.update(0x10, line)
    assert r[2] == 3 and r[3], "sustained change retrains"

    t2 = StrideTable()
    for i in range(12):
        r = t2.update(0x20, 5000 - 4 * i)
    assert r[2] == -4 and r[3], "negative strides work"

    t3 = StrideTable()
    for i in range(12):
        r = t3.update(0x30, i * (cfg.PF_MAX_STRIDE_LINES + 1))
    assert r[2] == 0 and not r[3], "strides beyond the cap are noise"


def test_stride_table_independent_pcs_and_capacity():
    t = StrideTable()
    ra = rb = None
    for i in range(12):
        ra = t.update(0x2000, i)
        rb = t.update(0x3000, 500 + 8 * i)
    assert ra[2] == 1 and ra[3] and rb[2] == 8 and rb[3]
    for pc in range(0x100, 0x100 + 8 * cfg.STRIDE_TABLE_SIZE, 8):   # far more PCs than entries
        t.update(pc, 1)
        t.update(pc, 2)
    assert sum(len(s) for s in t.sets) <= cfg.STRIDE_TABLE_SIZE


# ----------------------------------------------------------------------
# unit configuration
# ----------------------------------------------------------------------
def test_unit_config():
    u = PrefetchUnit()
    assert u.config() == ("off", 0)
    u.set_config("stride")
    assert u.config() == ("stride", cfg.PF_DISTANCE_DEFAULT["stride"])
    u.set_config("next-line", 8)
    assert u.config() == ("next-line", 8)
    u.set_config("off", 8)
    assert u.config() == ("off", 0)
    for bad in (("bogus", None), ("stride", 3), ("next-line", 0)):
        try:
            u.set_config(*bad)
            raise AssertionError(f"{bad} should be rejected")
        except ValueError:
            pass
    assert u.config() == ("off", 0), "a rejected change leaves the config alone"


def test_off_trains_detectors_but_never_issues():
    s = run_cfg(paced_seq(), mode="off")
    st, u = s.l2.stats, s.pf.stats
    assert st["pf_issued"] == 0 and st["pf_dropped"] == 0 and st["pf_redundant"] == 0
    assert u["obs_accesses"] == s.l1.stats["misses"]
    assert u["seq_hits"] > 0.95 * u["obs_accesses"]
    assert u["stride_matches"] > 0.95 * u["obs_accesses"]


def test_idle_unit_does_not_change_timing():
    ops = paced_seq(n=512)
    a = build_system()
    run(a, ops)
    b = build_system()
    b.l2.prefetcher = None
    run(b, ops)
    assert a.core.cycle == b.core.cycle


# ----------------------------------------------------------------------
# behaviour
# ----------------------------------------------------------------------
def test_next_line_helps_stream_not_pointer():
    seq = paced_seq()
    off = run_cfg(seq)
    nl = run_cfg(seq, "next-line", 4)
    print(f"[next-line] stream: off {off.core.cycle}, next-line {nl.core.cycle} cycles, "
          f"accuracy {accuracy(nl):.2f}")
    assert nl.core.cycle < 0.5 * off.core.cycle
    assert accuracy(nl) > 0.9
    assert nl.memory.stats["reads"] < off.memory.stats["reads"]

    ptr = list(Workload([("pointer", 300)]).ops())
    off_p = run_cfg(ptr)
    nl_p = run_cfg(ptr, "next-line", 4)
    print(f"[next-line] pointer: off {off_p.core.cycle}, next-line {nl_p.core.cycle} cycles, "
          f"issued {nl_p.l2.stats['pf_issued']}, useful {nl_p.l2.stats['pf_useful']}")
    assert accuracy(nl_p) < 0.01, "pointer-chase addresses are unpredictable"
    assert nl_p.core.cycle >= 0.98 * off_p.core.cycle, "prefetch cannot speed up a dependent chain"


def test_stride_helps_strided_and_next_line_does_not():
    n = 2048
    ops = _paced(mem_ops([i * 8 for i in range(n)], pc=0x3000), 100)
    off = run_cfg(ops)
    nl = run_cfg(ops, "next-line", 4)
    st = run_cfg(ops, "stride", 4)
    print(f"[stride] stride-8 stream: off {off.core.cycle}, next-line {nl.core.cycle} "
          f"(acc {accuracy(nl):.2f}), stride {st.core.cycle} (acc {accuracy(st):.2f})")
    assert st.core.cycle < 0.5 * off.core.cycle
    assert accuracy(st) > 0.9
    assert nl.l2.stats["pf_useful"] == 0, "next-line never fetches the lines a stride-8 stream wants"
    assert nl.core.cycle >= 0.95 * off.core.cycle


def test_stride_matches_next_line_on_unit_stride():
    seq = paced_seq()
    nl = run_cfg(seq, "next-line", 4)
    st = run_cfg(seq, "stride", 4)
    print(f"[stride] unit stride: next-line {nl.core.cycle}, stride {st.core.cycle}")
    assert abs(st.core.cycle - nl.core.cycle) < 0.1 * nl.core.cycle


def test_stride_quiet_on_random_access():
    rng = random.Random(3)
    lines = [rng.randrange(8 * L2_LINES) for _ in range(N_STREAM)]
    s = run_cfg(_paced(mem_ops(lines, pc=0x4000), 100), "stride", 4)
    issued, acc = s.l2.stats["pf_issued"], s.l1.stats["misses"]
    print(f"[stride] random: issued {issued} prefetches for {acc} L2 accesses")
    assert issued < 0.02 * acc


def test_distance_hides_latency():
    seq = paced_seq()
    off = run_cfg(seq)
    cyc, late = {}, {}
    for d in cfg.PF_DISTANCE_LADDER:
        s = run_cfg(seq, "next-line", d)
        st = s.l2.stats
        cyc[d] = s.core.cycle
        late[d] = st["pf_late"] / st["pf_useful"]
    print("[distance] off %d | " % off.core.cycle + ", ".join(
        f"d={d}: {cyc[d]} cyc, late {late[d]:.2f}" for d in cyc))
    ds = list(cfg.PF_DISTANCE_LADDER)
    assert cyc[ds[0]] < off.core.cycle
    for lo, hi in zip(ds, ds[1:]):
        assert cyc[hi] <= 1.01 * cyc[lo], "on a clean stream a longer distance never hurts"
    assert cyc[ds[0]] > 1.2 * cyc[ds[-1]], "a longer distance must matter when latency is exposed"
    assert late[ds[-1]] < late[ds[0]], "late prefetches shrink with distance"
    assert late[ds[-1]] < 0.2, "at the longest distance the fills arrive in time"


def test_pollution_grows_with_distance():
    """A reusable working set (sparse, so +1..+16 lines are never used) that
    nearly fills the L2: every prefetch is junk and evicts useful lines. The
    longer the distance, the worse it gets: the opposite of the clean stream,
    which is why distance has to follow measured accuracy."""
    rng = random.Random(1)
    lines = [rng.randrange(3300) * 33 for _ in range(30_000)]
    ops = _paced(mem_ops(lines, pc=0x5000), 20)
    res = {}
    for mode, d in (("off", None), ("next-line", 1), ("next-line", 4), ("next-line", 16)):
        res[d if mode != "off" else 0] = run_cfg(ops, mode, d)
    print("[pollution] " + ", ".join(
        f"d={d}: {s.core.cycle} cyc, L2 miss {s.l2.miss_rate():.2f}, "
        f"unused-evicted {s.l2.stats['pf_unused_evicted']}" for d, s in res.items()))
    cyc = [res[d].core.cycle for d in (0, 1, 4, 16)]
    assert cyc == sorted(cyc) and cyc[0] < 0.5 * cyc[-1]
    assert res[0].l2.miss_rate() < 0.2 < 0.5 < res[16].l2.miss_rate()
    assert res[16].l2.stats["pf_useful"] == 0
    assert res[16].l2.stats["pf_unused_evicted"] > 0.8 * res[16].l2.stats["pf_issued"]


# ----------------------------------------------------------------------
# cache-side prefetch plumbing
# ----------------------------------------------------------------------
def test_mshr_reserve_and_counters():
    s = build_system(l2_mshr=3)                       # prefetch may use n_mshr - 1 = 2
    s.l2.access(0, 0)
    s.l2.access(64, 0)
    assert s.l2.prefetch(10 * 64, 0) is False, "reserve: prefetch cannot take the last MSHR"
    assert s.l2.stats["pf_dropped"] == 1 and s.l2.stats["pf_redundant"] == 0
    assert s.l2.access(20 * 64, 0).completion is not None, "demand still gets the last MSHR"

    s2 = build_system()
    assert s2.l2.prefetch(5 * 64, 0) is True
    assert s2.l2.prefetch(5 * 64, 1) is False, "line already in flight"
    st = s2.l2.stats
    assert st["pf_issued"] == 1 and st["pf_redundant"] == 1 and st["pf_dropped"] == 0
    assert s2.memory.stats["prefetch_reads"] == 1


def test_stream_signals_by_phase_kind():
    """What the detectors report for each access pattern; the monitor turns
    these counters into seq_score / stride_conf."""
    expect = {                      # kind: (seq range, stride range)
        "streaming": ((0.95, 1.0), (0.95, 1.0)),
        "strided":   ((0.0, 0.05), (0.95, 1.0)),
        "mixed":     ((0.55, 0.85), (0.55, 0.85)),
        "pointer":   ((0.0, 0.05), (0.0, 0.05)),
    }
    for kind, (seq_r, str_r) in expect.items():
        s = build_system()
        run(s, list(Workload([(kind, 20_000 if kind != "pointer" else 600)]).ops()))
        u = s.pf.stats
        n = u["obs_accesses"]
        seq, stride = u["seq_hits"] / n, u["stride_matches"] / n
        print(f"[signals] {kind:<9} seq {seq:.2f}  stride {stride:.2f}  ({n} L2 accesses)")
        assert seq_r[0] <= seq <= seq_r[1], f"{kind}: sequential score {seq:.2f}"
        assert str_r[0] <= stride <= str_r[1], f"{kind}: stride confidence {stride:.2f}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], globals()))
