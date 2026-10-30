# Changelog — Self-Evolving Cache Simulator

All notable changes from the first corrected base simulator through the end of phase 2.
Entries are listed **newest first**. Version numbers are milestone numbers in **delivery
order** (0.1.0 was delivered first, 0.5.0 last). Each milestone says which project phase
it belongs to.

## Phase map

| Phase | What it covers | Milestones |
|---|---|---|
| **1 — Hardware** | The memory system and core being adapted: caches, MSHRs, memory, core timing, prefetcher, workloads, dependency modelling | 0.1.0, 0.1.1, 0.2.0 (prefetch part), 0.4.0 |
| **2 — Adaptation** | Observing, deciding and reconfiguring: monitor, reconfigurator, controller, the three-mode runner | 0.2.0 (monitor part), 0.3.0, 0.5.0 |
| **3 — Experiments** *(not started)* | Tuning, comparisons, break-even sweeps, second workload, charts, final report | see "Next" at the bottom |

Suggested git tags: `phase1-hardware` (state of 0.4.0 hardware files) and
`phase2-adaptation` (state of 0.5.0).

## Project size at the end of phase 2

| | Files | Lines |
|---|---|---|
| Simulator source | 11 (`cache`, `controller`, `core`, `memory`, `monitor`, `prefetch`, `reconfig`, `sim`, `sim_config`, `system`, `workload`) | 2,830 |
| Test suites | 6 (`check_base`, `check_prefetch`, `check_monitor`, `check_reconfig`, `check_controller`, `check_sim`) | 1,996 |
| **Total** | **17** | **4,826** |

Test checks: `check_base` 23, `check_prefetch` 14, `check_monitor` 11, `check_reconfig` 11,
`check_controller` 18, `check_sim` 9 — **86 checks, all passing, about 50 seconds**.

---

## [0.5.0] — Controller, simulation driver and static oracle  *(phase 2 complete)*

### Added
- **`controller.py`** — the decision layer. Static rules over live monitor counters; no benefit
  table, no memory of earlier runs, no workload phase labels.
  - Per-window procedure: skip the window after a switch → classify the regime → list proposals →
    per-proposal streaks (hysteresis) → residency and utility gates → execute at most one change.
  - Rules for prefetch (on / off / lower / raise), MSHR count (raise, optional shrink) and L2
    associativity (better, optional shrink; the same test run the other way is the revert rule).
  - `ControllerParams` dataclass: every tunable defaults to `sim_config.py` and can be overridden
    per run (`Controller(system, monitor, rc, hysteresis_m=1)`).
  - `Decision` trace (regime, confirmed regime, proposals, action, why a ripe proposal was
    blocked) and `regime_changes` (the controller's phase detection, for scoring later).
- **`sim.py`** — runs one workload in one of three modes and returns a `Result`.
  - `baseline`: one fixed configuration.
  - `dynamic`: monitor + controller + reconfigurator, every switch charged and logged.
  - `oracle`: best configuration per phase found by trying candidates on each phase kind
    (coordinate descent, 11 prefetch options × 3 associativities × 4 MSHR counts, two passes, a
    change must beat the current choice by 2%), applied at phase boundaries **for free**. Uses
    phase labels, so it is an upper-bound reference, not a deployable controller.
  - `resource_use()` — time-weighted L2 ways, MSHR entries and prefetch-on share for any run.
  - Command line: `python sim.py --mode dynamic --phases compute:200000,streaming:40000 --csv out`.
- **`Cache.remap_assoc(new_assoc, now)`** — idealized zero-cost associativity change that keeps
  every line (recency, dirty bit, prefetch flag and data-ready time preserved; lines that no
  longer fit are evicted and written back if dirty). Used only by the oracle's free switches.
- **`check_controller.py`** (18 checks) — every rule and gate on hand-built feature vectors with
  a stub reconfigurator, plus whole-simulation outcomes (end-to-end, paced stream, useless
  prefetcher, conflict phase, break-even).
- **`check_sim.py`** (9 checks) — modes, oracle freedom and determinism, resource accounting,
  config parsing, command line and CSV output.
- `check_base.py`: `test_remap_assoc_keeps_lines_and_charges_nothing` (22 → 23 checks).
- **New config constants** (`sim_config.py`): `PF_LATE_HIGH` (0.50), `PF_IRREGULAR` (0.30),
  `PF_MIN_ISSUED` (8), `DEP_STALL_LATENCY` (0.50), `HYSTERESIS_M_FAST` (2),
  `SHADOW_NO_LOSS_FRAC` (1.02), `ASSOC_OVERHEAD_MARGIN` (1.5), `ENABLE_MSHR_SHRINK` (False),
  `ENABLE_ASSOC_SHRINK` (False).

### Changed
- **Rule ordering.** The original criteria used a fixed priority (prefetch → MSHR → associativity,
  cheapest first). Replaced by: *urgent damage control first, otherwise the largest net benefit*
  (`saved × horizon − cost`). Reason: on a conflict phase the cheap MSHR rule fired first and,
  through the residency wait, delayed the 7.8× associativity fix by 5 windows. Measured effect on
  that scenario: dynamic improved from −68.8% to −75.8% versus baseline.
- **Regime classification** now uses only time fractions (compute / memory / latency / uncertain).
  The earlier "bandwidth-bound" regime was dropped as a *label* because bus utilisation depends
  on the controller's own MSHR and prefetch choices, which made a regime flip inside one phase.
  Bus utilisation remains a guard on the rules.
- **MSHR-raise gate.** The hard `BW_UTIL_HEADROOM` (0.70) cutoff was replaced by the utility
  estimate `speedup = min(new/old, 1 / bus utilisation)`, because the measured streaming value
  (0.66–0.68) sat right next to the cutoff. `BW_UTIL_SATURATED` (0.80) stays as a hard guard.
- **Prefetch-on and prefetch-raise** additionally require spare MSHRs (`mshr_occ_frac <
  MSHR_HIGH_FRAC`): when the pool is full, prefetches cannot issue and MSHR growth should come first.
- **Pointer-chase footprint 8× → 64× the L2** (`workload.py`). At 8× a long chase landed on
  lines a next-line prefetcher had fetched by chance, making next-line look ~4% useful on a
  pointer chase and making the oracle pick it. A real chase wanders a heap far larger than the cache.
- **Pointer-chain permutation cached per (seed, size)** in `workload.py`; the oracle's probes no
  longer rebuild a 262,144-entry permutation each time.
- **Oracle probe lengths** shortened for the memory-bound kinds (12k ops; same chosen
  configurations as 30k–60k-op probes). Compute keeps a 60k probe because shorter ones are
  dominated by cold-start misses and make a prefetcher look useful. Oracle search time per
  phase kind dropped from up to ~18 s to 0.5–6 s. Whole `check_sim` run: 66 s → 27 s.
- Oracle gain threshold 1% → 2% (`ORACLE_MIN_GAIN`) for the same cold-start reason.

### Fixed
- **Fast hysteresis stricter than normal** (found by `test_one_change_per_window_urgent_first_…`):
  with `hysteresis_m` below `hysteresis_m_fast` the damage-control path demanded *more* windows
  than an ordinary rule. It now uses `min(fast, m)`.
- **`sim.make_config`** failed when a prefetch mode was given without a distance (it kept the
  baseline distance 0). A new mode now takes its default distance.

### Removed
Dead config constants that nothing referenced: `MPKI_LOW_FACTOR`, `MPKI_HIGH_FACTOR`,
`BW_UTIL_HEADROOM`, `SHADOW_REVERT_FRAC`, `PF_COV_LOW`, `PF_POLLUTION_FACTOR`.

### Measured
| Run | Baseline | Dynamic | Oracle | Dynamic closes |
|---|---|---|---|---|
| Default workload | 955,822 cycles | 870,926 (−8.9%) | 858,438 (−10.2%) | 87% of oracle gain |
| Scaled default (tests) | 501,526 | 434,254 (−13.4%) | 421,758 | 84% |
| Conflict phase, start 2-way | 622,735 | 150,870 (−75.8%) | 85,903 | 88% |
| Paced stream | 534,295 | 203,903 (−61.8%) | 145,557 | 85% |
| Chase beside stream | 1,012,664 | 955,168 (−5.7%) | 953,088 | 97% |
| Useless prefetcher on a pointer chase (start next-line d=4) | 321,448 | 324,000 (+0.8%) | — | switched off in 2 windows |

Overhead on the default workload: 1 switch, 416 cycles (0.048% of runtime). Regime changes were
confirmed 2, 3 and 2 windows after the true phase starts.
**Break-even (conflict phase, start 2-way):** a phase of ~12,000 ops (6 windows) makes dynamic
**+6.2% slower** than doing nothing; ~20,000 ops gives −21.5%; ~45,000 ops gives −55.7%.

---

## [0.4.0] — Dependency distance  *(phase 1, hardware)*

### Added
- **Per-op dependency distance.** `dep` is now `0` (independent) or `k ≥ 1`: "my address comes
  from the data of the k-th most recent *load*". `True` still means 1 and `False` means 0, so
  nothing existing changed. Stores and compute ops are not counted.
- **`MAX_DEP_DISTANCE = 64`** (`sim_config.py`): the core keeps that many loads of history. A
  larger distance raises `ValueError`; a distance reaching back before the first load of a run
  simply has nothing to wait for.
- **`chase_stream` phase kind**: one dependent pointer-chase load, then `stream_per_chase`
  (default 16) independent stream loads, repeated; the chase load names its own previous hop
  `stream_per_chase + 1` loads back (stores in the stream are not counted). Not in the default
  workload yet; a candidate for the second workload.
- Tests: `test_dependency_distance_semantics`, `test_chase_stream_dependencies_point_at_the_previous_hop`,
  `test_chase_beside_stream` (`check_base` 19 → 22).

### Changed
- `core.py`: `last_load_ready` (single value) replaced by a bounded history of data-ready cycles.
- `workload.py`: pointer phase emits `dep` as the integer 1 instead of a bool.

### Measured
Chase alone: 216 cycles per hop. Beside 32 stream loads per hop: per-hop time falls with the MSHR
pool (1783 / 892 / 447 / 265 cycles at 4 / 8 / 16 / 32 MSHRs) while the chase's own miss latency
stays 216 until the bus saturates at 32 MSHRs, where it rises to 256 (+19%).
**This corrected an earlier prediction**: more MSHRs do *not* lose overall here, because the stream
is real work that finishes sooner; they only lose when the extra traffic is useless (junk prefetch).

---

## [0.3.0] — Reconfigurator  *(phase 2)*

### Added
- **`reconfig.py`** — `Reconfigurator(system, monitor)` with `set_prefetch`, `set_n_mshr`,
  `set_assoc`, `apply`, `estimate`, `overhead_table`, `log_rows`.
  - Fixed order for every switch: validate → drain → flush → change → wait → verify → log.
    Validation first, so a bad request has no side effects; a request for the current value
    returns `None` and costs nothing.
  - **Cost model.** Prefetch: `PF_DISTANCE_STALL` (10). MSHR: measured drain + `MSHR_DRAIN_STALL`
    (200). Associativity: measured drain + `max(walk, writeback wait)` with
    `walk = n_lines × K_ASSOC_PER_LINE` (4,096 × 8 = 32,768).
  - **Verification** after every switch: dirty lines written = dirty lines found; the L2 is empty
    and consistent (capacity unchanged); nothing in flight; no flush traffic left on the bus.
  - **Refetch tracking** (`refetch_misses`): first L2 miss to each line that was resident at the
    flush — the real, unpredictable cost of an associativity change. Lines never touched again
    cost nothing.
  - `SwitchRecord` log: cycle, next monitor window, knob, old/new, drain, stall, estimate,
    lines flushed/dirty, refetch misses, reason.
- **`Core.charge(cycles)`** — advances the clock by overhead cycles into `reconfig_cycles`.
- **`Cache.resident_lines()`**, **`PrefetchUnit.resolve(mode, distance)`** (pure validation, so a
  no-op switch can be detected without side effects).
- **`check_reconfig.py`** — 11 checks (one is a deliberately broken flush that the verification
  must reject).

### Changed
- `sim_config.py` comments now describe the real overhead model (estimate vs measured).
- **Checklist item dropped:** "reset the monitor baseline after a switch". No counter is ever
  reset, and the monitor already excludes overhead cycles, so no rebase is needed. The window
  after a switch is the one the *controller* skips (its index is stored in the record).

### Measured
Prefetch switch 10 cycles. MSHR switch under load: drain 216 + fixed 200 = **416** (the controller
estimates 200: it cannot know the drain). Associativity switch: 2,048 lines flushed (1,536 dirty)
charged **32,768**; on a bus twice as slow with 3,584 dirty lines the writebacks dominate:
**57,344**.

---

## [0.2.0] — Prefetch unit and monitor  *(prefetch = phase 1, monitor = phase 2)*

### Added
- **`prefetch.py`** — `StrideTable` (set-associative, keyed by PC, saturating confidence,
  tolerates a stray access, retrains on a sustained change, ignores strides over 64 lines) and
  `PrefetchUnit` with modes `off` / `next-line` / `stride` and distances 1, 2, 4, 8, 16.
  **The detectors train in every mode**, so the monitor can tell whether a stream is prefetchable
  before the controller turns prefetching on.
- **`monitor.py`** — `Monitor`, `Features`, `ShadowTags`. One feature vector per window (IPC,
  L2 misses per 1000 instructions, stall fractions, bus use, MSHR occupancy, miss latency,
  prefetch accuracy / coverage / late fraction / pollution, sequential score, stride confidence,
  shadow miss rate per associativity, configuration in force). Unknown values are `None`, never 0.
  Reconfiguration and end-of-run cycles are excluded from IPC and the stall fractions.
- **`ShadowTags`** — tag-only LRU copies of the L2 at 2, 4 and 8 ways, 256 randomly chosen (fixed
  seed) sets each, fed by the L2's own demand-access stream through the new observer hook.
- **`Cache.observers`** (read-only tap on every accepted demand access) and **`pf_redundant`**
  counter (prefetch skipped because the line is already present or in flight), separate from
  **`pf_dropped`** (refused for lack of a free MSHR).
- `system.py`: a `PrefetchUnit` (mode off) is always attached; `System.pf`; `build_system(...,
  pf_mode, pf_distance)`.
- `check_base.main(argv, namespace)` so the other check files reuse its runner.
- Config: `PF_CONF_MAX` (4), `PF_MAX_STRIDE_LINES` (64), `STREAM_MIN_SAMPLES` (32).
- **`check_prefetch.py`** (14 checks) and **`check_monitor.py`** (11 checks).

### Changed
- **`SHADOW_SAMPLE_SETS` 32 → 256.** At 32 sets a 10,000-cycle window had too few samples to
  trust an estimate.
- **Stride confidence redefined** as matches ÷ *all* L2 accesses (was matches ÷ accesses that
  already had a trained stride). Before: `mixed` read 0.99 and `pointer` read "no data". Now
  `mixed` ≈ 0.70 and `pointer` = 0.
- Config validation checks table sizes and shadow-sample feasibility.
- `monitor.py` uses `from __future__ import annotations` so it imports on Python < 3.10.

### Fixed
- **PC propagation:** `Cache._miss` passed PC `0` to the next level, so a stride detector at the
  L2 would have seen one PC for everything. The PC now travels from the core through the L1 to the L2.

### Measured
Streaming: next-line distance 1 / 2 / 4 / 8 / 16 → 1.5× / 2.0× / 3.0× / 4.6× / 4.6× faster than
off (late fraction 1.00 up to 8, 0.00 at 16). Stride-8 stream: stride 2.98× faster, next-line
0% useful. Stride prefetch stays silent on random access (0 prefetches for 2,024 accesses).
**Pollution:** on a reusable working set near L2 capacity every prefetch is junk and cycles rise
from 354,062 (off) to 789,248 / 932,884 / 1,051,658 at distance 1 / 4 / 16.
**Shadow tags vs reality** (run at 8-way, estimate for 2 / 4 / 8 ways vs a real run at each):
0.954 / 0.948, 0.630 / 0.628, 0.057 / 0.057.

---

## [0.1.1] — Review follow-up  *(phase 1; no simulator logic change)*

Two external reviews of 0.1.0 were evaluated claim by claim against measurements.

### Added
- `test_mshr_saturation_under_bandwidth_pressure`: with the bus twice as slow, 16 and 32 MSHRs
  tie (320,200 cycles) — the "more MSHRs stops helping" case.
- `test_compute_insensitive_to_mshr`: 4 vs 32 MSHRs on a compute phase differ by 4 cycles of 25,375.
- `check_base`: 17 → 19 checks.

### Changed
- `Cache._victim` docstring now states exactly what happens when every way of a set is still in
  flight (the in-flight line is dropped and re-misses; nothing is corrupted) and its measured
  frequency (0 picks in the conflict and streaming phases; 5 in 87,842 L2 misses of random
  traffic at 2-way with 32 MSHRs).

### Rejected review claims (with evidence)
- *"Streaming at 100% stall means no compute/memory overlap"* — the core does overlap through the
  ROB; the stream is limited by memory throughput. Raising compute per load to 4 gives stall 0.92,
  not the promised 0.4–0.8 (100 per load still gives 0.75).
- *"The pending-fill victim corrupts lines with the wrong tag"* — fills never write into a line later.
- *"Late prefetches mean latency isn't hidden"* — the test already used degree 4; late fraction
  drops to 0 at degree 16 and cycles fall 4.6×.
- *"Degree 16 should be worse than 4 from pollution"* — impossible on a clean sequential stream.
- *"No MSHR saturation"* — 32 MSHRs already reach the bus floor (160,208 vs 160,000 cycles).

---

## [0.1.0] — Base simulator corrected and validated  *(phase 1)*

Starting point: the first draft of `workload.py`, `cache.py`, `core.py`, `memory.py`,
`sim_config.py` and a duplicated `check_base.py`.

### Fixed — `cache.py`
- Filled lines were left **invalid**, so nothing could ever hit.
- The L1 called a non-existent `request()` on the L2; the L1→L2 chain crashed.
- The prefetch-useful check was inverted.
- Writebacks were counted but never charged to the memory bus.
- The MSHR queue assumed completions arrive in order; an L2 hit completes before an earlier DRAM
  miss, so retirement was wrong. Now a heap.
- Data appeared instantly: lines had no data-ready time. Added a per-line `ready` cycle, so a
  request that finds an in-flight line waits for it (a "late hit"), and no second memory
  request is made.
- Refused (MSHR-full) accesses changed state or double-counted. Now a refusal changes nothing
  and counters move only on success.

### Fixed — `core.py`
- Independent misses never stalled the core (memory-bound phases looked free) and the ROB size
  was ignored.
- `CORE_WIDTH` was unused (1 op per cycle); an L1 hit blocked the core for 4 cycles.
- A refused MSHR access dropped the op instead of retrying.
- The end of a run did not wait for in-flight loads, so the final cycle count was too small.
- A meaningless `last_load_data` value was used as a dependent load's address.

### Fixed — `workload.py`
- Compute-phase addresses were random 32-bit values, not a tiny working set.
- The pointer chain was built but never used, and a plain shuffle can produce short cycles.
  Now Sattolo's algorithm: one cycle through every line.
- The stream differed on every iteration (shared RNG). Now a per-phase RNG: identical every run.
- The strided footprint was only 64 KB and fit in the L2; streaming and mixed footprints were
  too small. All large footprints are now ≥ 4× the L2.
- No stores (so no dirty lines or writebacks), no associativity-sensitive phase, no PC for the
  stride prefetcher, no compute between memory ops, overlapping address ranges.

### Fixed — `memory.py`, tests, config
- `bus_util` used cumulative counters, wrong for windowed use.
- `check_base.py` was duplicated and its tests never exercised the hierarchy properly.
- Config file name mismatch (`config.py` in the docstring, `sim_config` in every import).

### Added
- **`system.py`**: `build_system()`, `System`, `System.snapshot()`, `System.check_invariants()`
  (counter conservation across all levels).
- **Unified downstream interface** (`Cache` and `Memory`): `access`, `writeback`, `drain_time`,
  `retire_all`, returning a `Result(hit, completion, retry_at)`.
- Cumulative counters everywhere with `snapshot()` and `reset_counters()`.
- `Core.drain()`, the `on_window(core, final)` hook, `Cache.flush_invalidate()`,
  `Cache.set_assoc()`, `Cache.set_n_mshr()`, the prefetcher hook.
- `sim_config.py` validation that runs at import.
- New constants: `L1_MSHR` (64, must be ≥ the largest L2 MSHR choice), `PF_MSHR_RESERVE` (1),
  `CONFLICT_SETS` (128), `CONFLICT_WAYS` (6), `DEFAULT_PHASES`.
- A `conflict` phase kind: 6 lines per set at every associativity, so it thrashes at 2- and
  4-way and fits at 8-way.

### Changed
- Op format is now `(kind, addr, dep, pc)`; counters are reset with `reset_counters()`.
- Removed unused `LOAD_HIT_LATENCY` and `DEPENDENT_LOAD_PENALTY`.

### Measured (first full validation, 17/17 checks)
IPC 4.000 on pure compute. Sequential scan: 16,384 lines in 221,304 cycles (13.5 cycles per line,
latency-limited at 16 MSHRs). Fitting working set: L1 miss rate 1.000, L2 miss rate 0.000.
Conflict pattern: 2-way and 4-way miss every access (93,552 cycles), 8-way misses none (12,039
cycles, 7.8× faster). Store scan of 16,384 lines: 12,288 memory writes. Bus formula exact for
bus = 8 and 32. MSHR scaling at 4 / 8 / 16 / 32: 1,080,024 / 540,056 / 270,120 / 160,208 cycles.
ROB 4 vs 128: 432,024 vs 108,120. Dependent chain 64,800 cycles vs 4,192 for the same loads
independent. Drain after a stream: 216 cycles. Default-workload phases: compute IPC 4.0,
streaming 0.15 (stall 1.00, 500 misses per 1000 instructions), pointer 0.00 (stall 1.00, 1000).

---

## Known limitations at the end of phase 2

- **Simplified core:** no branch prediction, no instruction latencies other than loads, no port
  limits. Conclusions are relative; absolute IPC is not a claim.
- **Scaled caches** (256 KB L2) so small workloads stress them; a single core; a non-inclusive
  hierarchy.
- **Associativity switches always flush.** Going up and down cost the same. Real hardware would
  use way gating (capacity changes with ways); the idealized remap is only used by the oracle.
- **8-way is never worse** than 2- or 4-way in this model (no latency or energy cost of ways), and
  the baseline is 8-way, so associativity adaptation only shows gains when the start is 2- or
  4-way. Shrinking rules exist but are off by default (`ENABLE_*_SHRINK`).
- **Default phases are extreme** (signals pinned near 0 or 1). A second workload with
  intermediate-intensity phases is needed before claiming generality.
- **Refetch tracking** does not count a line that returns through a prefetch rather than a demand miss.
- A run ends when its loads are done; store fills can still be in flight, so the first drain
  after a run can take a few hundred cycles.
- **Controller thresholds are first guesses**; nothing has been tuned yet.
- The oracle uses phase labels and a coordinate-descent search, so it is an idealized reference.

## Next — phase 3

1. Build a separate **tuning workload**; tune thresholds, hysteresis, residency, horizon, window
   size and the associativity margin on it; **freeze** them.
2. **Experiments:** baseline vs oracle vs dynamic on the test workloads; detection delay and false
   switches; break-even sweeps for every knob; more negative cases; horizon sensitivity; several
   seeds; the second (intermediate-intensity) workload with the unchanged controller.
3. **Decide:** baseline associativity, and whether to enable the shrinking rules / add a
   resource-cost model.
4. **Charts and tables**, then the final report. Optional: gem5 cross-check of static settings.
