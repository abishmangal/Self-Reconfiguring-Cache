"""
suites.py — The named workloads used to TUNE the controller and to TEST it.

The two sets are kept apart on purpose. The controller's constants are chosen on
TUNING_SUITE (with TUNING_SEEDS) and then frozen (see tune.py); every reported
result comes from TEST_SUITE (with TEST_SEEDS) and is measured only after the
freeze. Names, workloads and seeds never overlap, and tune.py is checked not to
reference anything from the test set.

An Entry holds the phases, optional workload parameter overrides (per phase kind,
see workload.DEFAULT_PARAMS) and the starting configuration (keys of
sim.make_config: l2_assoc, pf_mode, pf_distance, n_mshr).

Tuning set: what each entry teaches the controller
    tune-strided           independent misses, regular stride: MSHR growth
    tune-paced-strided     regular stride with compute between loads: prefetch + distance
    tune-conflict4-long    conflict phase from 4-way, long enough to repay the flush
    tune-conflict4-short   the same, near break-even: the cost/benefit trade-off
    tune-compute           nothing to gain: the controller must do nothing
    tune-pointer-pf-on     pointer chase with a useless prefetcher left on
    tune-alternating       memory / latency / memory / compute: hysteresis vs responsiveness
    tune-chase-stream8     dependent chain beside a stream
    tune-mixed             partly predictable access: do no harm
    tune-sparse            sparse misses (intermediate stall fraction)

Test set: the situations the report is about
    test-default           the assignment's compute -> memory -> compute -> latency example
    test-paced-stream      prefetch on a paced stream
    test-conflict4/2       conflict phase from 4-way and from 2-way
    test-chase-stream      dependent chain beside a stream
    test-intermediate      the SECOND workload: intermediate-intensity phases of several kinds
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sim_config as cfg
from workload import Workload

TUNING_SEEDS = (101, 102)          # tune on the first, check stability on the second
TEST_SEEDS = tuple(cfg.SEEDS)      # 1..5


@dataclass(frozen=True)
class Entry:
    name: str
    phases: tuple
    params: dict = field(default_factory=dict)
    start: dict = field(default_factory=dict)
    note: str = ""

    def workload(self, seed):
        return Workload(list(self.phases), seed=seed, params=self.params or None)

    def total_ops(self):
        return sum(n for _, n in self.phases)


def _e(name, phases, params=None, start=None, note=""):
    return Entry(name, tuple(phases), params or {}, start or {}, note)


TUNING_SUITE = (
    _e("tune-strided", [("compute", 100_000), ("strided", 60_000), ("compute", 100_000)],
       note="independent strided misses; MSHR growth"),
    _e("tune-paced-strided", [("compute", 60_000), ("strided", 120_000)],
       params={"strided": {"compute_per_mem": 40}},
       note="regular stride, compute between loads; prefetch and its distance"),
    _e("tune-conflict4-long", [("compute", 80_000), ("conflict", 60_000), ("compute", 80_000)],
       start={"l2_assoc": 4}, note="conflict phase from 4-way, long enough to repay the flush"),
    _e("tune-conflict4-short", [("compute", 100_000), ("conflict", 16_000), ("compute", 100_000)],
       start={"l2_assoc": 4}, note="conflict phase near break-even"),
    _e("tune-compute", [("compute", 300_000)], note="nothing to gain: do nothing"),
    _e("tune-pointer-pf-on", [("pointer", 1_200)], start={"pf_mode": "next-line", "pf_distance": 4},
       note="useless prefetcher left on during a pointer chase"),
    _e("tune-alternating", [("streaming", 30_000), ("pointer", 1_000), ("streaming", 30_000),
                            ("compute", 60_000)],
       params={"streaming": {"compute_per_mem": 4}},
       note="memory / latency / memory / compute: hysteresis against responsiveness"),
    _e("tune-chase-stream8", [("chase_stream", 60_000)],
       params={"chase_stream": {"stream_per_chase": 8}}, note="dependent chain beside a stream"),
    _e("tune-mixed", [("compute", 60_000), ("mixed", 80_000), ("compute", 60_000)],
       note="partly predictable access: do no harm"),
    _e("tune-sparse", [("compute", 50_000), ("streaming", 160_000), ("compute", 50_000)],
       params={"streaming": {"compute_per_mem": 450}}, note="sparse misses, intermediate stall"),
)

TEST_SUITE = (
    _e("test-default", cfg.DEFAULT_PHASES, note="compute -> streaming -> compute -> pointer"),
    _e("test-paced-stream", [("compute", 80_000), ("streaming", 500_000)],
       params={"streaming": {"compute_per_mem": 100}}, note="paced stream: prefetch matters"),
    _e("test-conflict4", [("compute", 100_000), ("conflict", 100_000), ("compute", 100_000)],
       start={"l2_assoc": 4}, note="conflict phase from 4-way"),
    _e("test-conflict2", [("compute", 100_000), ("conflict", 100_000), ("compute", 100_000)],
       start={"l2_assoc": 2}, note="conflict phase from 2-way"),
    _e("test-chase-stream", [("chase_stream", 150_000)], note="dependent chain beside a stream"),
    _e("test-intermediate",
       [("compute", 150_000), ("streaming", 300_000), ("mixed", 250_000), ("strided", 250_000),
        ("conflict", 60_000), ("compute", 100_000), ("pointer", 800)],
       params={"streaming": {"compute_per_mem": 600}, "mixed": {"compute_per_mem": 400},
               "strided": {"compute_per_mem": 300}},
       start={"l2_assoc": 4},
       note="second workload: sparse-miss phases (stall 0.3-0.7) of several kinds, then a conflict phase"),
)


def by_name(name):
    for e in TUNING_SUITE + TEST_SUITE:
        if e.name == name:
            return e
    raise KeyError(name)


def _check_disjoint():
    names_t = {e.name for e in TUNING_SUITE}
    names_e = {e.name for e in TEST_SUITE}
    if names_t & names_e or len(names_t) != len(TUNING_SUITE) or len(names_e) != len(TEST_SUITE):
        raise ValueError("suite names must be unique and the two suites disjoint")
    if set(TUNING_SEEDS) & set(TEST_SEEDS):
        raise ValueError("tuning and test seeds must not overlap")
    sigs_t = {(e.phases, repr(sorted(e.params.items())), repr(sorted(e.start.items())))
              for e in TUNING_SUITE}
    for e in TEST_SUITE:
        if (e.phases, repr(sorted(e.params.items())), repr(sorted(e.start.items()))) in sigs_t:
            raise ValueError(f"{e.name} duplicates a tuning workload")


_check_disjoint()
