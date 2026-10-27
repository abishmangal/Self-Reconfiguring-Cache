"""
workload.py — Phase-based op stream generator.

An op is a 4-tuple:
    (kind, addr, dep, pc)
where
    kind : OP_COMPUTE, OP_LOAD or OP_STORE
    addr : byte address for loads/stores, None for compute
    dep  : True if this load's ADDRESS depends on the data of the previous
           load (pointer chasing). The core makes it wait for that load.
           False otherwise. Stores and compute ops always carry False.
    pc   : id of the access stream (what a real PC would identify); the
           stride prefetcher keys on it. 0 for compute ops.

One op is one instruction, so an op index is also an instruction count.
Phase boundaries are known up front (every phase yields exactly `length`
ops) and are written to a separate file. The controller must never read
it; it is only used for detection scoring, by mapping boundary op indices
to windows through the per-window instruction counts.

The stream is deterministic: ops() can be iterated any number of times
(baseline / oracle / dynamic runs) and yields the identical sequence.

Phase kinds:
    compute   : mostly compute, a few loads/stores to a tiny working set
    streaming : sequential loads/stores over a footprint >> L2
    strided   : regular stride, footprint >> L2
    pointer   : dependent chain of loads (single random cycle) >> L2
    mixed     : partly sequential, partly random, footprint >> L2
    conflict  : many lines per L2 set; thrashes at low associativity,
                fits at high associativity (see sim_config CONFLICT_*)
"""

import itertools
import json
import random

import sim_config as cfg

OP_COMPUTE = "compute"
OP_LOAD = "load"
OP_STORE = "store"

PHASE_KINDS = ("compute", "streaming", "strided", "pointer", "mixed", "conflict")

# Each phase kind lives in its own 256 MB address region, so one kind never
# warms (or collides with) another kind's data. Repeated phases of the same
# kind share a region on purpose (a second compute phase reuses its state).
REGION_SHIFT = 28
REGION_BASE = {kind: i << REGION_SHIFT for i, kind in enumerate(PHASE_KINDS)}

L2_LINES = cfg.L2_SIZE // cfg.LINE_SIZE

# Footprints are given as multiples of the L2 line count so they scale with
# sim_config. compute_per_mem = compute ops emitted after each memory op.
DEFAULT_PARAMS = {
    "compute":   dict(load_frac=0.10, store_frac=0.30, state_lines=8, pc=0x1000),
    "streaming": dict(footprint_mult=8, compute_per_mem=1, store_frac=0.25, pc=0x2000),
    "strided":   dict(footprint_mult=4, stride_lines=8, compute_per_mem=1,
                      store_frac=0.0, pc=0x3000),
    "pointer":   dict(footprint_mult=8, compute_per_mem=0, pc=0x4000),
    "mixed":     dict(footprint_mult=8, seq_frac=0.7, compute_per_mem=1,
                      store_frac=0.1, pc_seq=0x5000, pc_rand=0x5100),
    "conflict":  dict(sets=cfg.CONFLICT_SETS, ways=cfg.CONFLICT_WAYS,
                      compute_per_mem=1, store_frac=0.2, pc=0x6000),
}

_COMPUTE_OP = (OP_COMPUTE, None, False, 0)


class Workload:
    def __init__(self, phases, seed=None, params=None):
        """
        phases : list of (kind, length_in_ops)
        seed   : int; same seed -> identical op stream
        params : optional per-kind overrides of DEFAULT_PARAMS,
                 e.g. {"streaming": {"store_frac": 0.5}}
        """
        self.phases = [tuple(p) for p in phases]
        self.seed = cfg.DEFAULT_SEED if seed is None else seed

        overrides = params or {}
        unknown = set(overrides) - set(DEFAULT_PARAMS)
        if unknown:
            raise ValueError(f"unknown param groups: {sorted(unknown)}")
        self.params = {k: {**DEFAULT_PARAMS[k], **overrides.get(k, {})}
                       for k in DEFAULT_PARAMS}

        # Boundaries (scoring only), computed up front.
        self.boundaries = []
        pos = 0
        for idx, (kind, length) in enumerate(self.phases):
            if kind not in PHASE_KINDS:
                raise ValueError(f"unknown phase kind: {kind}")
            if length <= 0:
                raise ValueError(f"phase {idx} ({kind}): length must be > 0")
            self.boundaries.append({"index": idx, "kind": kind,
                                    "start_op": pos, "end_op": pos + length})
            pos += length
        self.total_ops = pos

        self._validate_conflict()
        self._chain = None   # pointer-chase permutation, built on first use

    # ------------------------------------------------------------------
    # Footprints (in distinct lines), for sanity checks and reports
    # ------------------------------------------------------------------
    @property
    def footprint_lines(self):
        p = self.params
        return {
            "compute":   p["compute"]["state_lines"],
            "streaming": p["streaming"]["footprint_mult"] * L2_LINES,
            "strided":   p["strided"]["footprint_mult"] * L2_LINES,
            "pointer":   p["pointer"]["footprint_mult"] * L2_LINES,
            "mixed":     p["mixed"]["footprint_mult"] * L2_LINES,
            "conflict":  p["conflict"]["sets"] * p["conflict"]["ways"],
        }

    def _validate_conflict(self):
        c = self.params["conflict"]
        ways = sorted(cfg.L2_ASSOC_CHOICES)
        if not (ways[-2] < c["ways"] <= ways[-1]):
            raise ValueError("conflict ways must be above the second-largest "
                             "and at most the largest L2 associativity")
        if c["sets"] * c["ways"] <= cfg.L1_SIZE // cfg.LINE_SIZE:
            raise ValueError("conflict footprint must exceed the L1 line count")
        # Region size check for the spread-out kinds
        stride = self.conflict_stride_lines
        top = (c["sets"] + c["ways"] * stride) * cfg.LINE_SIZE
        s = self.params["strided"]
        top_s = s["footprint_mult"] * L2_LINES * s["stride_lines"] * cfg.LINE_SIZE
        if max(top, top_s) >= (1 << REGION_SHIFT):
            raise ValueError("footprint exceeds the 256 MB per-kind address region")

    @property
    def conflict_stride_lines(self):
        """Line spacing that maps lines to the same set at EVERY L2 assoc."""
        return cfg.L2_SIZE // (min(cfg.L2_ASSOC_CHOICES) * cfg.LINE_SIZE)

    # ------------------------------------------------------------------
    # Pointer-chase permutation: one random cycle through every line
    # (Sattolo's algorithm), so the chase cannot fall into a short loop.
    # ------------------------------------------------------------------
    @property
    def chain(self):
        if self._chain is None:
            n = self.footprint_lines["pointer"]
            rng = random.Random(f"{self.seed}:chain")
            chain = list(range(n))
            for i in range(n - 1, 0, -1):
                j = rng.randrange(i)
                chain[i], chain[j] = chain[j], chain[i]
            self._chain = chain
        return self._chain

    # ------------------------------------------------------------------
    # Op stream
    # ------------------------------------------------------------------
    def ops(self):
        """Yield all ops. Safe to call repeatedly; identical every time."""
        for idx, (kind, length) in enumerate(self.phases):
            yield from self._phase_ops(idx, kind, length)

    def _phase_ops(self, idx, kind, length):
        rng = random.Random(f"{self.seed}:{idx}:{kind}")
        p = self.params[kind]
        if kind == "compute":
            yield from self._compute_ops(rng, p, length)
        else:
            gen = getattr(self, f"_gen_{kind}")(rng, p)
            yield from self._interleave(gen, p["compute_per_mem"], length)

    @staticmethod
    def _interleave(mem_ops, compute_per_mem, length):
        """Memory ops, each followed by compute_per_mem compute ops, cut to
        exactly `length` ops."""
        emitted = 0
        while emitted < length:
            yield next(mem_ops)
            emitted += 1
            for _ in range(compute_per_mem):
                if emitted >= length:
                    return
                yield _COMPUTE_OP
                emitted += 1

    # -- compute -------------------------------------------------------
    def _compute_ops(self, rng, p, length):
        base = REGION_BASE["compute"]
        for _ in range(length):
            if rng.random() < p["load_frac"]:
                kind = OP_STORE if rng.random() < p["store_frac"] else OP_LOAD
                line = rng.randrange(p["state_lines"])
                yield (kind, base + line * cfg.LINE_SIZE, False, p["pc"])
            else:
                yield _COMPUTE_OP

    # -- memory-op generators (infinite) -------------------------------
    def _gen_streaming(self, rng, p):
        base = REGION_BASE["streaming"]
        n = p["footprint_mult"] * L2_LINES
        i = 0
        while True:
            kind = OP_STORE if rng.random() < p["store_frac"] else OP_LOAD
            yield (kind, base + (i % n) * cfg.LINE_SIZE, False, p["pc"])
            i += 1

    def _gen_strided(self, rng, p):
        base = REGION_BASE["strided"]
        n = p["footprint_mult"] * L2_LINES
        stride = p["stride_lines"]
        i = 0
        while True:
            kind = OP_STORE if rng.random() < p["store_frac"] else OP_LOAD
            yield (kind, base + (i % n) * stride * cfg.LINE_SIZE, False, p["pc"])
            i += 1

    def _gen_pointer(self, rng, p):
        base = REGION_BASE["pointer"]
        chain = self.chain
        cur = rng.randrange(len(chain))
        first = True
        while True:
            # The first load's address is free; every later address comes
            # from the previous load's data, hence dep=True.
            yield (OP_LOAD, base + cur * cfg.LINE_SIZE, not first, p["pc"])
            first = False
            cur = chain[cur]

    def _gen_mixed(self, rng, p):
        base = REGION_BASE["mixed"]
        n = p["footprint_mult"] * L2_LINES
        cursor = rng.randrange(n)
        while True:
            if rng.random() < p["seq_frac"]:
                line, pc = cursor, p["pc_seq"]
                cursor = (cursor + 1) % n
            else:
                line, pc = rng.randrange(n), p["pc_rand"]
            kind = OP_STORE if rng.random() < p["store_frac"] else OP_LOAD
            yield (kind, base + line * cfg.LINE_SIZE, False, pc)

    def _gen_conflict(self, rng, p):
        base = REGION_BASE["conflict"]
        stride = self.conflict_stride_lines
        lines = [r + j * stride for j in range(p["ways"]) for r in range(p["sets"])]
        for line in itertools.cycle(lines):
            kind = OP_STORE if rng.random() < p["store_frac"] else OP_LOAD
            yield (kind, base + line * cfg.LINE_SIZE, False, p["pc"])

    # ------------------------------------------------------------------
    def write_boundaries(self, path=None):
        """Write phase boundaries (op indices) for SCORING ONLY."""
        path = path or cfg.BOUNDARY_FILE
        with open(path, "w") as f:
            json.dump(self.boundaries, f, indent=2)


def make_workload(phases=None, seed=None, params=None):
    """Default test workload from sim_config.DEFAULT_PHASES."""
    if phases is None:
        phases = cfg.DEFAULT_PHASES
    return Workload(phases, seed=seed, params=params)
