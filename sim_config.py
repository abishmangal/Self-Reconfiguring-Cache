"""
sim_config.py — Single source of truth for all simulator constants.

Every tunable lives here. Nothing else in the codebase should hardcode
a number that could reasonably change between experiments.

Sections:
  1. Cache geometry
  2. Latencies and bandwidth
  3. Core model
  4. MSHR ladder
  5. Prefetch
  6. Monitor
  7. Controller (thresholds, gates, horizon)
  8. Reconfiguration overhead estimates
  9. Experiment settings
 10. Workload
 11. Validation (runs at import)
"""

# ---------------------------------------------------------------------------
# 1. Cache geometry
# ---------------------------------------------------------------------------

LINE_SIZE = 64                     # bytes

# L1: fixed, never reconfigured
L1_SIZE = 32 * 1024                # 32 KB
L1_ASSOC = 8
L1_LATENCY = 4                     # cycles, hit latency

# L2: the single adaptive cache
L2_SIZE = 256 * 1024               # 256 KB (scaled down so workloads stress it)
L2_ASSOC_CHOICES = (2, 4, 8)       # adaptive associativity ladder
L2_ASSOC_DEFAULT = 8               # baseline config
L2_LATENCY = 12                    # cycles, hit latency

# Number of sets is derived from size / (assoc * line_size).
# It changes when associativity changes; the cache is rebuilt in place.

# ---------------------------------------------------------------------------
# 2. Latencies and bandwidth
# ---------------------------------------------------------------------------

DRAM_LATENCY = 200                 # cycles, base latency per miss
DRAM_BUS_CYCLES = 8                # cycles the bus is occupied per line transfer
                                   # (bandwidth limit = LINE_SIZE / DRAM_BUS_CYCLES bytes/cycle)

# ---------------------------------------------------------------------------
# 3. Core model (simplified)
# ---------------------------------------------------------------------------

CORE_WIDTH = 4                     # max instructions issued per cycle
ROB_SIZE = 128                     # reorder buffer entries; the core stalls when
                                   # the oldest outstanding load is this many
                                   # instructions old
MAX_DEP_DISTANCE = 64              # an op may depend on a load at most this many
                                   # loads back (the core keeps that much history)

# ---------------------------------------------------------------------------
# 4. MSHR ladder
# ---------------------------------------------------------------------------

MSHR_CHOICES = (4, 8, 16, 32)      # L2 MSHR ladder (the adaptive one)
MSHR_DEFAULT = 16                  # baseline L2 config
MSHR_DRAIN_STALL = 200             # fixed cycles to quiesce and resize the MSHR table,
                                   # charged ON TOP of the measured drain time

# L1 MSHRs are fixed. They must be at least as large as the biggest L2 choice,
# otherwise L1 would be the binding limit and the L2 MSHR count would have no
# effect.
L1_MSHR = 64

# ---------------------------------------------------------------------------
# 5. Prefetch
# ---------------------------------------------------------------------------

PF_MODES = ("off", "next-line", "stride")
PF_MODE_DEFAULT = "off"

PF_DISTANCE_LADDER = (1, 2, 4, 8, 16)
PF_DISTANCE_DEFAULT = {
    "off":       0,
    "next-line": 1,
    "stride":    2,
}
PF_CONFIDENCE_THRESHOLD = 0.7      # stride detector must be at least this confident
PF_CONF_MAX = 4                    # confidence counter ceiling; confident when
                                   # counter / PF_CONF_MAX >= PF_CONFIDENCE_THRESHOLD
PF_MAX_STRIDE_LINES = 64           # larger |stride| (in lines) is treated as noise
PF_DISTANCE_STALL = 10             # cycles to change mode/distance
PF_ACC_HIGH = 0.75                 # "prefetch is accurate" threshold
PF_ACC_LOW = 0.40                  # "prefetch is inaccurate" threshold
PF_ACC_OFF = 0.30                  # "turn off" threshold
PF_COV_OFF = 0.10                  # "turn off" coverage threshold
PF_LATE_HIGH = 0.50                # most useful prefetches arrive late -> raise distance
PF_IRREGULAR = 0.30                # sequential score AND stride confidence below this
                                   # -> the stream is irregular, prefetching is pointless
PF_MIN_ISSUED = 8                  # prefetches in a window before coverage is judged

# Prefetches may not take the last PF_MSHR_RESERVE MSHRs, so they can never
# starve demand misses.
PF_MSHR_RESERVE = 1

# Expected hit rates used by the live estimator (config constants, not
# measured from the test workload).
PF_EXPECTED_HIT_NEXTLINE = 0.5
PF_EXPECTED_HIT_STRIDE = 0.7
PF_COV_GAIN_PER_STEP = 0.10        # coverage improvement per distance step

# ---------------------------------------------------------------------------
# 6. Monitor
# ---------------------------------------------------------------------------

WINDOW_CYCLES = 10_000             # one feature vector per 10k cycles
AVG_WINDOW = 20                    # running-average window for relative thresholds
SHADOW_SAMPLE_SETS = 256           # sets sampled per associativity for the shadow
                                   # tags (random but fixed; must be <= the set
                                   # count of the largest associativity choice)
SHADOW_MIN_SAMPLES = 32            # minimum sampled accesses per window before a
                                   # shadow miss-rate estimate is trusted
STREAM_MIN_SAMPLES = 32            # minimum L2 accesses per window before the
                                   # sequential score / stride confidence count
RECENT_ADDR_BUFFER = 64            # ring buffer size for sequential_score
STRIDE_TABLE_SIZE = 16             # entries in the stride detector table
STRIDE_TABLE_WAYS = 4              # associativity of the stride table

# ---------------------------------------------------------------------------
# 7. Controller
# ---------------------------------------------------------------------------

# Phase classification. The regime is a property of the WORKLOAD (compute /
# memory / latency), so it uses only fractions of time, never the bus or MSHR
# state that the controller itself changes.
STALL_FRAC_COMPUTE = 0.15          # below this → compute-bound
STALL_FRAC_MEMORY = 0.35           # at or above this (and not latency-bound) → memory-bound
DEP_STALL_LATENCY = 0.50           # dependent-load stall fraction at/above this → latency-bound
                                   # (misses cannot overlap: MSHRs and prefetch cannot help)

# Gates
HYSTERESIS_M = 3                   # consecutive windows a condition must hold
HYSTERESIS_M_FAST = 2              # ... for damage control (turning a useless prefetcher off)
MIN_RESIDENCY_K = 5                # windows after a switch before next switch
MIN_RESIDENCY_K_PF = 2             # separate, shorter residency for prefetch
                                   # distance changes
SKIP_WINDOW_AFTER_SWITCH = 1       # skip this many windows for classification

# Horizon: controller assumes current behavior continues for H windows
HORIZON_H = 20                     # windows (swept once as sensitivity test)

# Thresholds for specific rules
MSHR_HIGH_FRAC = 0.80              # MSHR occupancy at/above this = pool is the limit; also
                                   # "no spare MSHRs for prefetches"
MSHR_LOW_FRAC = 0.30               # MSHR occupancy below this = pool mostly idle (shrink candidate)
BW_UTIL_SATURATED = 0.80           # bus this busy: more MSHRs / prefetch distance cannot help
SEQ_SCORE_THRESHOLD = 0.6          # sequential score or stride confidence needed to enable prefetch
STALL_FRAC_ATTENTION = 0.20        # memory must be hurting before prefetch is enabled
STALL_FRAC_ASSOC = 0.25            # associativity matters only above this stall fraction
SHADOW_IMPROVEMENT_FRAC = 0.90     # 10% fewer estimated misses required to change associativity
                                   # (the same test, applied the other way, is the revert rule)
SHADOW_NO_LOSS_FRAC = 1.02         # shrinking is "free" if the shadow shows <= 2% more misses

# Utility: expected cycles saved per window * HORIZON_H must exceed the estimated
# switch cost. The associativity estimate leaves out the refetch misses, so it is
# multiplied by this margin (tune on the tuning workload).
ASSOC_OVERHEAD_MARGIN = 1.5

# Shrinking rules are resource-saving moves with no performance gain, so they are
# off unless a resource objective is wanted.
ENABLE_MSHR_SHRINK = False
ENABLE_ASSOC_SHRINK = False

# ---------------------------------------------------------------------------
# 8. Reconfiguration overhead estimates (used by the controller)
# ---------------------------------------------------------------------------
# These are the controller's ESTIMATES. The simulator measures the REAL
# overhead. The report shows the gap between the two.

EST_OVERHEAD_PREFETCH_MODE = PF_DISTANCE_STALL
EST_OVERHEAD_PREFETCH_DIST = PF_DISTANCE_STALL
EST_OVERHEAD_MSHR = MSHR_DRAIN_STALL
EST_OVERHEAD_ASSOC = None          # computed at runtime: n_lines * K_ASSOC
K_ASSOC_PER_LINE = 8               # cycles per line during associativity rebuild

# The simulator's REAL overhead (reconfig.py):
#   prefetch : PF_DISTANCE_STALL
#   n_mshr   : measured drain + MSHR_DRAIN_STALL
#   assoc    : measured drain + max(n_lines * K_ASSOC_PER_LINE, time for the
#              flush's writebacks to clear the bus)
# plus, for assoc, the refetch misses after the flush, counted separately.
# The controller's estimates above leave out the measured drain and the
# refetch misses, which it cannot predict; the report shows that gap.

# ---------------------------------------------------------------------------
# 9. Experiment settings
# ---------------------------------------------------------------------------

SEEDS = (1, 2, 3, 4, 5)
DEFAULT_SEED = 1

# Modes for sim.py
MODE_BASELINE = "baseline"
MODE_ORACLE = "oracle"
MODE_DYNAMIC = "dynamic"

# Workload phase boundaries are written to a separate file and used ONLY
# for scoring detection. The controller must never read them.

BOUNDARY_FILE = "phase_boundaries.json"

# Baseline fixed configuration (Mode = baseline)
BASELINE_CONFIG = {
    "l2_assoc": L2_ASSOC_DEFAULT,
    "pf_mode": PF_MODE_DEFAULT,
    "pf_distance": PF_DISTANCE_DEFAULT[PF_MODE_DEFAULT],
    "n_mshr": MSHR_DEFAULT,
}

# ---------------------------------------------------------------------------
# 10. Workload
# ---------------------------------------------------------------------------

# Conflict phase: CONFLICT_SETS L2 sets, each hit by CONFLICT_WAYS lines that
# all map to the same set at every associativity. It thrashes below
# CONFLICT_WAYS-way and fits at or above it. It must sit strictly above the
# second-largest and at most the largest associativity choice (checked below),
# and CONFLICT_SETS * CONFLICT_WAYS must exceed the L1 line count so the L1
# cannot absorb it.
CONFLICT_SETS = 128
CONFLICT_WAYS = 6

# Default test workload: (kind, length in ops). One op = one instruction, so
# an op index is also an instruction count. Lengths are chosen so each phase
# spans roughly 20+ monitor windows under the baseline configuration
# (check_base.py prints the measured number).
DEFAULT_PHASES = (
    ("compute",   800_000),
    ("streaming",  50_000),
    ("compute",   800_000),
    ("pointer",     1_000),
)

# ---------------------------------------------------------------------------
# 11. Validation
# ---------------------------------------------------------------------------

def _require(cond, msg):
    if not cond:
        raise ValueError(f"sim_config: {msg}")


def validate():
    _require(L2_ASSOC_DEFAULT in L2_ASSOC_CHOICES, "L2_ASSOC_DEFAULT not in L2_ASSOC_CHOICES")
    _require(MSHR_DEFAULT in MSHR_CHOICES, "MSHR_DEFAULT not in MSHR_CHOICES")
    _require(PF_MODE_DEFAULT in PF_MODES, "PF_MODE_DEFAULT not in PF_MODES")
    _require(set(PF_DISTANCE_DEFAULT) == set(PF_MODES), "PF_DISTANCE_DEFAULT keys must match PF_MODES")
    for a in L2_ASSOC_CHOICES:
        _require(L2_SIZE % (a * LINE_SIZE) == 0, f"L2_SIZE not divisible by {a}-way x line size")
    _require(L1_SIZE % (L1_ASSOC * LINE_SIZE) == 0, "L1_SIZE not divisible by L1_ASSOC x line size")
    _require(L1_MSHR >= max(MSHR_CHOICES), "L1_MSHR must be >= the largest L2 MSHR choice")
    _require(STRIDE_TABLE_SIZE % STRIDE_TABLE_WAYS == 0, "STRIDE_TABLE_SIZE must be a multiple of STRIDE_TABLE_WAYS")
    _require(PF_CONF_MAX >= 1 and 0 < PF_CONFIDENCE_THRESHOLD <= 1, "bad stride confidence settings")
    for a in L2_ASSOC_CHOICES:
        _require(L2_SIZE // (a * LINE_SIZE) >= SHADOW_SAMPLE_SETS,
                 f"SHADOW_SAMPLE_SETS exceeds the {a}-way set count")
    _require(MAX_DEP_DISTANCE >= 1, "MAX_DEP_DISTANCE must be >= 1")
    _require(CORE_WIDTH >= 1 and ROB_SIZE >= 1, "CORE_WIDTH and ROB_SIZE must be >= 1")
    _require(DRAM_LATENCY > 0 and DRAM_BUS_CYCLES > 0, "DRAM latency and bus cycles must be > 0")
    ways = sorted(L2_ASSOC_CHOICES)
    _require(ways[-2] < CONFLICT_WAYS <= ways[-1],
             "CONFLICT_WAYS must be above the second-largest and at most the largest L2 associativity")
    _require(CONFLICT_SETS * CONFLICT_WAYS > L1_SIZE // LINE_SIZE,
             "conflict footprint must exceed the L1 line count")
    _require(all(length > 0 for _, length in DEFAULT_PHASES), "phase lengths must be > 0")


validate()
