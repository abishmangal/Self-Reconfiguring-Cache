"""
config.py — Single source of truth for all simulator constants.

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
ROB_SIZE = 128                     # reorder buffer entries; stall if oldest miss
                                   # is older than this many instructions
LOAD_HIT_LATENCY = L1_LATENCY      # cycles to serve an L1 hit
DEPENDENT_LOAD_PENALTY = 0         # extra stall for a dependent load (0 = none,
                                   # the dependency itself already stalls it)

# ---------------------------------------------------------------------------
# 4. MSHR ladder
# ---------------------------------------------------------------------------

MSHR_CHOICES = (4, 8, 16, 32)
MSHR_DEFAULT = 16                  # baseline config
MSHR_DRAIN_STALL = 200             # cycles to drain and resize MSHRs

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
PF_DISTANCE_STALL = 10             # cycles to change mode/distance
PF_ACC_HIGH = 0.75                 # "prefetch is accurate" threshold
PF_ACC_LOW = 0.40                  # "prefetch is inaccurate" threshold
PF_ACC_OFF = 0.30                  # "turn off" threshold
PF_COV_LOW = 0.30                  # "coverage too low" threshold
PF_COV_OFF = 0.10                  # "turn off" coverage threshold

# Expected hit rates used by the live estimator (config constants, not
# measured from the test workload).
PF_EXPECTED_HIT_NEXTLINE = 0.5
PF_EXPECTED_HIT_STRIDE = 0.7
PF_COV_GAIN_PER_STEP = 0.10        # coverage improvement per distance step
PF_POLLUTION_FACTOR = 0.3          # fraction of wrong prefetches that displace
                                   # a useful line

# ---------------------------------------------------------------------------
# 6. Monitor
# ---------------------------------------------------------------------------

WINDOW_CYCLES = 10_000             # one feature vector per 10k cycles
AVG_WINDOW = 20                    # running-average window for relative thresholds
SHADOW_SAMPLE_SETS = 32            # number of L2 sets sampled for shadow tags
SHADOW_MIN_SAMPLES = 32            # minimum valid samples before shadow estimate
                                   # is trusted by Rule G
RECENT_ADDR_BUFFER = 64            # ring buffer size for sequential_score
STRIDE_TABLE_SIZE = 16             # entries in the stride detector table
STRIDE_TABLE_WAYS = 4              # associativity of the stride table

# ---------------------------------------------------------------------------
# 7. Controller
# ---------------------------------------------------------------------------

# Phase classification (relative to running average)
STALL_FRAC_COMPUTE = 0.15          # below this → compute-bound candidate
STALL_FRAC_MEMORY = 0.35           # above this → memory-bound
MPKI_LOW_FACTOR = 0.8              # mpki < avg * this → compute-bound
MPKI_HIGH_FACTOR = 1.3             # mpki > avg * this → memory-bound

# Gates
HYSTERESIS_M = 3                   # consecutive windows of same phase to confirm
MIN_RESIDENCY_K = 5                # windows after a switch before next switch
MIN_RESIDENCY_K_PF = 2             # separate, shorter residency for prefetch
                                   # distance changes
SKIP_WINDOW_AFTER_SWITCH = 1       # skip this many windows for classification

# Horizon: controller assumes current behavior continues for H windows
HORIZON_H = 20                     # windows (swept once as sensitivity test)

# Thresholds for specific rules
MSHR_HIGH_FRAC = 0.80              # Rule E: MSHR occupancy above this → increase
MSHR_LOW_FRAC = 0.30               # Rule F: MSHR occupancy below this → decrease
BW_UTIL_HEADROOM = 0.70            # Rule E: increase only if bw_util below this
BW_UTIL_SATURATED = 0.80           # Rule D: decrease prefetch distance if above this
SEQ_SCORE_THRESHOLD = 0.6          # Rule B/I: sequential_score must exceed this
STALL_FRAC_ATTENTION = 0.20        # Rule B: memory must be hurting
STALL_FRAC_ASSOC = 0.25            # Rule G/H: associativity matters only above this
SHADOW_IMPROVEMENT_FRAC = 0.90     # Rule G: 10% fewer estimated misses required
SHADOW_REVERT_FRAC = 0.95          # Rule H: 5% better required to revert

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

# The simulator's REAL overhead for associativity is also computed as
# n_lines * K_ASSOC_PER_LINE, plus warm-up misses measured after the switch.
# The controller uses the same formula; the estimate and the real cost differ
# only in the warm-up component, which the controller cannot predict.

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