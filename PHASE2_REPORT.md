# Phase 2 Report — The Adaptation Layer

**Project:** Self-Evolving Computer Architecture — runtime optimisation and reconfiguration of
computing resources, simulated in Python.
**Phase 2 scope:** monitoring, decision-making, reconfiguration and the three-mode simulation
driver (baseline, static oracle, dynamic).
**Status:** complete. 86 / 86 automated checks pass. Phase 3 (tuning, experiments, final report)
has not started.

## Contents

1. [Executive summary](#1-executive-summary)
2. [How the work maps to the assignment](#2-how-the-work-maps-to-the-assignment)
3. [System architecture](#3-system-architecture)
4. [The hardware being adapted (phase 1 recap)](#4-the-hardware-being-adapted-phase-1-recap)
5. [Monitoring — `monitor.py`](#5-monitoring--monitorpy)
6. [Reconfiguration — `reconfig.py`](#6-reconfiguration--reconfigpy)
7. [Decision making — `controller.py`](#7-decision-making--controllerpy)
8. [The simulation driver — `sim.py`](#8-the-simulation-driver--simpy)
9. [Validation](#9-validation)
10. [Results](#10-results)
11. [What the results tell us](#11-what-the-results-tell-us)
12. [Design decisions and their rationale](#12-design-decisions-and-their-rationale)
13. [Limitations and threats to validity](#13-limitations-and-threats-to-validity)
14. [Reproducing everything](#14-reproducing-everything)
15. [Open decisions and phase 3 plan](#15-open-decisions-and-phase-3-plan)
16. [Appendices](#16-appendices)

---

## 1. Executive summary

**What was built.** An architecture that watches its own behaviour while a program runs and
changes three resources of a single adaptive L2 cache — *associativity* (2 / 4 / 8 ways),
*MSHR count* (4 / 8 / 16 / 32) and *prefetch mode and distance* (off / next-line / stride,
distance 1–16) — without being redesigned per workload. Four components interact once per
10,000-cycle window:

```
Monitor (what is happening?) → Controller (should anything change?) →
Reconfigurator (change it safely and charge the cost) → Resources (the L2, MSHRs, prefetcher)
```

**Headline results** (default workload; each number is a measured simulation, not an estimate):

| Mode | Cycles | IPC | Switches | Overhead | Notes |
|---|---|---|---|---|---|
| Baseline (fixed 8-way, prefetch off, 16 MSHRs) | 955,822 | 1.727 | 0 | 0 | reference |
| **Dynamic** (this work) | **870,926 (−8.9%)** | 1.896 | 1 | **416 cycles (0.048%)** | raised MSHRs 16→32 when the stream began |
| Static oracle (idealised upper bound) | 858,438 (−10.2%) | 1.923 | 2, free | 0 | uses phase labels |

The dynamic system captured **87% of the oracle's gain** while paying for every reconfiguration.

**Five findings worth carrying into the final report**

1. **Adaptation has a cost, and the controller models it.** Switch costs span four orders of
   magnitude: prefetch 10 cycles, MSHR count ≈ 416, associativity ≈ 33,000 plus refetch misses.
   The controller only switches when expected savings over a horizon exceed the estimated cost.
2. **There is a measurable break-even phase length.** A thrashing phase of ~6 windows makes the
   adaptive run **6% slower** than doing nothing (the flush is not repaid); ~11 windows gives
   **−21%**; ~25 windows gives **−56%**.
3. **The knobs are coupled, not independent.** More MSHRs help independent misses (6.7× from 4
   to 32 on a stream) but *amplify* a useless prefetcher (+27% slower from 4 to 32 MSHRs on a
   junk-prefetch pattern). The rules therefore check prefetch accuracy and bus use before
   raising MSHRs.
4. **The decision engine is generic.** It reads counters only. Its source is verified (by an
   automated test) not to import or reference the workload or phase information, and the same
   unmodified engine handled five different situations.
5. **Phase detection works with small delay.** Regimes were confirmed 2, 3 and 2 windows after
   the true phase starts (hysteresis is 3 windows).

**What is deliberately not claimed yet.** Controller thresholds are first-guess values and
untuned; the default workload's phases are extreme (signals near 0 or 1); associativity
adaptation can only win when the starting associativity is below 8; absolute IPC is not a claim
(simplified core). Section 13 lists all of these.

---

## 2. How the work maps to the assignment

| Assignment requirement | Where it is met |
|---|---|
| Observe workload behaviour during execution | `monitor.py`: one feature vector per window from hardware counters, stream detectors and shadow tags (§5) |
| Define how runtime information is collected | Cumulative counters diffed per window; observers on the L2 access stream; feature definitions in §5.3 |
| Define how decisions are made | `controller.py`: static rules, hysteresis, residency, utility test (§7) |
| Dynamically modify resources or configuration | Three knobs on one L2: associativity, MSHR count, prefetch mode/distance (§4.3, §6) |
| *Not* treat the system as a new architecture per workload | One L2 object reconfigured **in place** at fixed capacity and line size; L1 fixed; one controller with no workload knowledge (§12) |
| Simulate as an interacting system | `sim.py --mode dynamic` runs monitor → controller → reconfigurator → resources every window; every decision and switch is logged (§8) |
| Show the overhead of adaptation | Every switch is charged to the clock and logged; overhead table, estimate-vs-real gap, refetch misses (§6, §10) |
| Example: compute-intensive → memory-intensive | The default workload; regime detected 2 windows after the stream starts and the MSHR pool grown (§10.2) |

---

## 3. System architecture

### 3.1 Component diagram

```
                      ┌───────────────────────────────────────────────────┐
   op stream ────────▶│ Core  width 4, ROB 128, dependency distances       │
                      └───────────────────────┬───────────────────────────┘
                                              │ loads / stores (address, PC)
                                   ┌──────────▼──────────┐
                                   │ L1 (fixed)           │  32 KB, 8-way, 64 MSHRs
                                   └──────────┬──────────┘
                                              │ misses (PC preserved)
 ┌────────────────────────┐        ┌──────────▼───────────────────────┐
 │ PrefetchUnit           │◀──────▶│ L2 (ADAPTIVE)  256 KB            │──▶ Memory
 │  stride table          │ issues │  associativity 2 / 4 / 8         │    200-cycle latency
 │  sequential detector   │        │  MSHRs 4 / 8 / 16 / 32           │    8 cycles/line bus
 │  mode + distance       │        │  prefetch mode + distance        │
 └────────────────────────┘        └──────────┬───────────────────────┘
                                              │ observers (every demand access)
                                   ┌──────────▼──────────┐
                                   │ ShadowTags 2/4/8-way │  tag-only LRU, 256 sets each
                                   └─────────────────────┘
```

### 3.2 The control loop (once per window)

```
Core.run  ── window elapsed ──▶  Controller.on_window(core, final)
                                   │
                                   ├─ Monitor.sample()           → Features (diff of cumulative counters)
                                   │
                                   └─ Controller.step(Features)
                                        1. skip the window right after a switch
                                        2. classify regime (compute / memory / latency / uncertain)
                                        3. propose()  → every change the rules want now
                                        4. per-proposal streaks (hysteresis)
                                        5. residency + utility gates
                                        6. pick ONE: urgent damage control, else largest net benefit
                                              │
                                              └─ Reconfigurator.apply(knob, value, reason)
                                                   validate → drain → flush → change → charge → verify → log
```

The monitor samples **before** the controller acts, so the configuration recorded in a
feature row is the one that was in force *during* that window.

### 3.3 Files and responsibilities

| File | Lines | Role | Phase |
|---|---|---|---|
| `sim_config.py` | 277 | Every constant; validated at import | 1+2 |
| `workload.py` | 313 | Deterministic phase-based op streams (7 phase kinds) | 1 |
| `memory.py` | 101 | DRAM latency + bus bandwidth model; `Result` type | 1 |
| `cache.py` | 378 | Write-back cache, LRU, MSHRs, pending fills, prefetch/observer hooks, flush/rebuild/remap primitives | 1 |
| `core.py` | 229 | Simplified out-of-order core: width, ROB, dependent loads, drain, charge | 1 |
| `prefetch.py` | 196 | Stride table, sequential detector, prefetch issue engine | 1 |
| `system.py` | 83 | Builds the hierarchy; counter snapshots; invariant checks | 1 |
| `monitor.py` | 287 | Windowed feature vectors, shadow tags | **2** |
| `reconfig.py` | 244 | Costed, verified, logged configuration changes | **2** |
| `controller.py` | 363 | Regime classification and decision rules | **2** |
| `sim.py` | 359 | Baseline / oracle / dynamic modes, resource accounting, CLI | **2** |

---

## 4. The hardware being adapted (phase 1 recap)

### 4.1 Parameters (all in `sim_config.py`)

| Item | Value |
|---|---|
| Line size | 64 B |
| L1 (fixed, never reconfigured) | 32 KB, 8-way, 4-cycle hit, 64 MSHRs |
| L2 (adaptive) | 256 KB, associativity ladder 2 / 4 / 8 (default 8), 12-cycle hit |
| DRAM | 200-cycle latency; each line transfer occupies the bus 8 cycles (floor of 8 cycles per line) |
| Full miss path | 4 (L1) + 12 (L2) + 200 (DRAM) = **216 cycles** |
| Core | width 4, ROB 128, dependency history 64 loads |
| L2 MSHR ladder | 4 / 8 / 16 / 32 (default 16) |
| Prefetch | modes off / next-line / stride; distance ladder 1 / 2 / 4 / 8 / 16 |
| Baseline configuration | 8-way, prefetch off, 16 MSHRs |
| Monitor window | 10,000 cycles |

The L1 MSHR count (64) must be at least the largest L2 choice (32), otherwise the L1 would be
the limit and the L2 MSHR count would have no effect. `sim_config.validate()` enforces this.

### 4.2 Behavioural rules of the memory system

* Write-back, write-allocate, LRU. Non-inclusive hierarchy: flushing the L2 never touches L1 contents.
* A line is allocated when its miss is issued but carries a data-ready time; a request that
  finds it earlier waits for it (a *late hit*) and does not create a second memory request.
* A refused access (MSHRs full here or downstream) changes **no** state and is not counted; the
  core waits until the earliest MSHR frees and retries.
* Dirty victims are written back downstream as posted writes: they use bus bandwidth and nobody waits.
* The core never blocks on a load by itself. Misses overlap up to the MSHR and ROB limits.
  Dependent loads wait for the load they depend on. Stores retire through a store buffer.
* Prefetches share MSHRs and bandwidth but may not take the last MSHR (`PF_MSHR_RESERVE = 1`).
* All counters are cumulative; the monitor diffs snapshots and nothing is ever reset.

### 4.3 The three adaptive knobs

| Knob | Values | What it controls | Cost class |
|---|---|---|---|
| L2 associativity | 2, 4, 8 | Conflict misses; sets double when ways halve (capacity fixed) | Very high: flush + refetch |
| L2 MSHR count | 4, 8, 16, 32 | How many misses overlap (memory-level parallelism) | Medium: drain |
| Prefetch mode + distance | off, next-line d∈{1..16}, stride d∈{1..16} | Latency hiding for regular streams | Near zero |

Why not capacity or the number of cache levels: those need a full flush and would almost always
cost more than they gain; they are listed as boundary conditions in §13.

### 4.4 Workload phase kinds

| Kind | Behaviour | Signature (baseline config) |
|---|---|---|
| `compute` | Mostly compute; 10% of ops touch an 8-line state | IPC 4.0, stall 0.00, no L2 misses |
| `streaming` | Sequential, footprint 8× L2, 25% stores | IPC 0.15, stall 1.00, 500 misses/1000 instr |
| `strided` | Stride of 8 lines, 4× L2 distinct lines | stride confidence 1.00, sequential score 0.00 |
| `pointer` | Dependent chain through a 64× L2 permutation (one cycle) | IPC ≈ 0, stall 1.00, dependent stall 1.00, 1000 misses/1000 instr |
| `mixed` | 70% sequential, 30% random | sequential and stride scores ≈ 0.70 |
| `conflict` | 128 sets × 6 lines each, same set at every associativity | thrashes at 2/4-way, fits at 8-way |
| `chase_stream` | One dependent chase load then 16 independent stream loads | latency-critical chain beside bulk traffic |

Default workload: compute 800,000 ops → streaming 50,000 → compute 800,000 → pointer 1,000
(about 95 monitor windows; true phase starts at windows 0, 20, 45, 65). One op is one
instruction, so an op index is also an instruction count; phase boundaries are written to a
separate file used **only to score** the system, never by it.

### 4.5 Dependency distances

An op's `dep` is `0` or `k ≥ 1`: "my address comes from the data of the k-th most recent load".
Stores and compute ops are not counted. This lets a pointer chase run *beside* a stream: the
chase load points back past the stream loads to its own previous hop. `True` still means 1.

---

## 5. Monitoring — `monitor.py`

### 5.1 Principle

The monitor reads only **hardware-visible state**: core, cache and memory counters, the
prefetch unit's detectors and its own shadow tags. It never sees workload labels.

### 5.2 Window mechanics

* `Core.run` calls back when at least `WINDOW_CYCLES` (10,000) have elapsed; a long stall can
  make a window longer, so the monitor always uses *elapsed* cycles, never the nominal length.
* On each call the monitor takes a snapshot, subtracts the previous one, and builds a
  `Features` row. Counters are never reset (if they are, it raises a clear error and offers
  `rebase()`).
* **Workload cycles** exclude reconfiguration (drain + stall) and the end-of-run wait:
  `cycles = Δcycle − Δreconfig_cycles − Δdrain_cycles − Δstall_end`. So adaptation cost can
  never hide inside IPC or a stall fraction, and a switch cannot disguise itself as a phase change.
* The final, shorter-than-a-window sample is flagged `partial` and ignored by the controller.

### 5.3 The feature vector

| Field | Definition | `None` when |
|---|---|---|
| `ipc` | instructions ÷ workload cycles | never |
| `l2_mpki` | 1000 × L2 demand misses ÷ instructions | never |
| `stall_frac` | (dependent + ROB + MSHR stall cycles) ÷ workload cycles, capped at 1 | never |
| `dep_stall_frac` | dependent-load stall cycles ÷ workload cycles | never |
| `bw_util` | DRAM bus busy cycles ÷ workload cycles, capped at 1 (reads + writebacks) | never |
| `mshr_occ` | time-averaged outstanding L2 misses (exact integral) | never |
| `mshr_occ_frac` | `mshr_occ` ÷ current MSHR count | never |
| `l2_miss_latency` | mean cycles from request to data for demand misses | no misses |
| `assoc`, `n_mshr`, `pf_mode`, `pf_distance` | configuration in force during the window | — |
| `pf_issued` | prefetches that actually went to memory | — |
| `pf_accuracy` | useful ÷ issued, capped at 1 | nothing issued |
| `pf_coverage` | useful ÷ (useful + demand misses) | no events |
| `pf_late_frac` | late ÷ useful (arrived after the demand access) | no useful prefetches |
| `pf_unused_evicted` | prefetched lines evicted without use (pollution) | — |
| `seq_score` | accesses whose previous line was touched in the last 64 ÷ L2 accesses | fewer than 32 L2 accesses |
| `stride_conf` | accesses continuing their PC's established stride ÷ **all** L2 accesses | fewer than 32 L2 accesses |
| `shadow_rate[a]` | estimated L2 miss rate at associativity *a* | fewer than 32 sampled accesses |

**`None` is never 0.** "No evidence" and "measured zero" are different facts, and the controller
never acts on missing evidence.

`stride_conf` is divided by *all* accesses (not just those with a trained stride) so irregular
streams score near 0 instead of "unknown": `mixed` reads ≈ 0.70 and `pointer` reads 0.00.

### 5.4 Stream detectors (in `prefetch.py`, always training)

* **Sequential score**: a 64-entry recent-line buffer; an access counts if line − 1 is present.
* **Stride table**: 16 entries, 4-way, keyed by PC (xor-folded hash). Confidence is a
  saturating counter up to 4; "confident" means ≥ 3 (0.7 × 4 rounded up). One stray access
  lowers confidence but keeps the stride; a sustained change retrains; |stride| > 64 lines is noise.
* The detectors train in **every** prefetch mode, including off. This is what allows the
  controller to learn that a stream is regular *before* turning prefetching on.

### 5.5 Shadow tags — predicting other associativities without trying them

Tag-only LRU copies of the L2 at 2, 4 and 8 ways, each on 256 randomly chosen (fixed seed) sets,
fed by the L2's own demand-access stream. They answer: *how many misses would this stream have
at each associativity?* — including the current one, so estimates are compared like for like.

* Random (not strided) set selection avoids aliasing with power-of-two access patterns.
* A window's estimate is trusted only with at least 32 sampled accesses per associativity.
* They model steady-state behaviour: no prefetch fills, no in-flight timing, and **no cold-start
  cost** — the controller adds that separately (§7.4).

**Validation** (run at 8-way, estimate for each associativity vs a real run at that
associativity, on a random-reuse conflict pattern):

| Associativity | Shadow estimate | Real miss rate |
|---|---|---|
| 2-way | 0.954 | 0.948 |
| 4-way | 0.630 | 0.628 |
| 8-way | 0.057 | 0.057 |

Errors are at most 0.006. The 0.125 the shadow reports for 8-way on the *cyclic* conflict
pattern is not an error: it includes the cold first pass (1 of 8 passes), while the real
cache was measured after warm-up.

### 5.6 How the signals separate the regimes

| Phase | IPC | Stall | Dependent stall | Bus use | MSHR use | Misses/1000 instr | Seq. score | Stride conf. |
|---|---|---|---|---|---|---|---|---|
| compute | 4.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0 | no data | no data |
| streaming | 0.15 | 1.00 | 0.00 | 0.66 | 0.98 | 500 | 1.00 | 1.00 |
| pointer | 0.00 | 1.00 | 1.00 | 0.05 | 0.06 | 1000 | 0.00 | 0.00 |

Stall fraction alone cannot tell streaming from pointer chasing (both 1.00); dependent stall,
MSHR use, bus use and the stream scores can. This is why the regime classifier uses dependent
stall (§7.2).

---
## 6. Reconfiguration — `reconfig.py`

### 6.1 Responsibilities

The reconfigurator changes a knob **safely**, charges the cost to the simulation clock,
**verifies** the result, and **logs** the switch. It is called from the `on_window` callback,
between instructions, so nothing is being issued while it runs. It advances the clock itself
(`core.drain()` and `core.charge()`), so the callback does not need to return the stall.

### 6.2 The fixed sequence

```
validate  →  (drain)  →  (flush)  →  change  →  (wait)  →  verify  →  log
```

* **Validate first.** An invalid request raises before any side effect. A request for the
  *current* value returns `None` and costs nothing.
* **Drain** (MSHR and associativity changes): the core stops issuing and waits for every
  outstanding load and miss in the hierarchy; the ROB is cleared. Nothing is in flight from here on.
* **Flush** (associativity only): dirty L2 lines are written back to memory and every L2 line
  is invalidated. The L1 is never touched (non-inclusive hierarchy).
* **Change**, **wait** (charge the stall), **verify**, **log**.

### 6.3 Cost model

| Knob | Charged to the clock | Rationale |
|---|---|---|
| Prefetch mode / distance | `PF_DISTANCE_STALL` = **10** cycles | A control-register write. No drain, no data moves; in-flight prefetches finish. |
| MSHR count | measured drain + `MSHR_DRAIN_STALL` = 200 → **416** measured under load | The pool cannot be resized with misses in flight. |
| Associativity | measured drain + `max(walk, writeback wait)` | The index/tag split moves, so every line is in the wrong place. |

For associativity: `walk = n_lines × K_ASSOC_PER_LINE = 4,096 × 8 = 32,768` cycles (visiting
every tag). `writeback wait` is how long the bus needs to push the flush's dirty lines to
memory. The stall is the larger of the two, because writebacks stream out *during* the walk.
After the stall the cache is empty ("fresh") at the same capacity.

**Not charged at the switch, but a real cost:** the lines the program has to fetch again
afterwards. These are tracked as `refetch_misses`: the first L2 demand miss to each line that
was resident at the flush. Lines that were resident but are never touched again cost nothing;
a line that comes back through a prefetch instead of a demand miss is not counted.

### 6.4 Verification (every switch)

* Memory writes during the flush equal the number of dirty lines found.
* The L2 holds no valid line, no MSHR, the ROB is empty, associativity is the requested one and
  `sets × ways × line size = capacity` still holds.
* No flush writeback is still occupying the bus when the stall ends.

A failed check raises `RuntimeError` and the switch is **not** logged as a success. A test
deliberately makes the flush lie about its dirty count and confirms this.

### 6.5 The log

Each `SwitchRecord` holds: index, cycle, the index of the **next** monitor window (the one the
controller must skip), knob, old and new value, drain cycles, stall cycles, the controller's
estimate, lines flushed and dirty, refetch misses, and the rule that triggered it.
`overhead_table()` aggregates per knob: switches, drain, stall, total, estimated total,
refetch misses and percentage of runtime. Cycles in the log add up exactly to the core's
`reconfig_cycles + drain_cycles` (tested).

### 6.6 Estimate versus real cost

The controller uses `estimate_overhead`, which comes from config constants only:

| Knob | Controller's estimate | Real cost | Gap |
|---|---|---|---|
| Prefetch | 10 | 10 | none |
| MSHR | 200 | drain + 200 (416 measured: drain 216) | the drain, workload-dependent |
| Associativity | 32,768 (× margin 1.5 inside the controller) | drain + max(walk, writeback wait) + refetch misses later | drain, bus-bound writebacks, refetch misses |

The gap is exactly the part a controller cannot know in advance; reporting it is part of the
analysis. Measured: an associativity switch with 2,048 lines resident (1,536 dirty) cost
**32,768**; with the bus twice as slow and 3,584 dirty lines the writebacks dominated and it
cost **57,344**.

### 6.7 Interplay with the monitor

No counter is reset, so no monitor re-baselining is needed. The switch's cycles fall into the
**next** window as `overhead_cycles` and are excluded from IPC and stall fractions. That window
also contains the cold-start misses, so the controller skips it for classification.

---

## 7. Decision making — `controller.py`

### 7.1 Principles

1. **Static rules over live counters.** No benefit table built from earlier runs, no learning
   across runs, no tuning to a particular workload.
2. **No phase labels.** The module imports only `dataclasses` and `sim_config`, and an automated
   test (an AST scan) fails if it ever references workload or phase information.
3. **Horizon assumption.** The behaviour seen now is assumed to continue for `HORIZON_H` = 20
   windows. This is the single stated assumption that turns "per-window saving" into a
   decision.
4. **Cost-aware.** A change happens only if expected savings over the horizon exceed its
   estimated cost.
5. **No evidence, no action.** Any signal that is `None` blocks the rules that need it.

### 7.2 What happens each window

1. **Skip.** The window right after a switch is ignored completely (cold caches). No streak
   counts, no regime update.
2. **Classify** the window's regime (§7.3). A regime is *confirmed* after `HYSTERESIS_M` = 3
   consecutive windows; confirmed changes are the controller's phase detection.
3. **Propose.** Run all rules (pure functions of the feature vector) and list every change
   currently wanted.
4. **Streaks.** Each proposal has its own streak counter, incremented while the proposal
   stays wanted and reset when it disappears. It is *ripe* after 3 windows (2 for switching a
   useless prefetcher off). Separate streaks mean a flickering rule cannot starve a steady one.
5. **Gates** for each ripe proposal: residency (§7.6) and the utility test (§7.4).
6. **Select** exactly one change: urgent damage control first; otherwise the largest net
   benefit (`saved × H − cost`); ties follow rule order. Execute it through the
   reconfigurator. A switch clears all streaks and starts the skip.

### 7.3 Regime classification

| Regime | Condition | Meaning |
|---|---|---|
| compute | `stall_frac < 0.15` | memory is not the bottleneck |
| latency | not compute and `dep_stall_frac ≥ 0.50` | dependent chain: misses cannot overlap; MSHRs and prefetch cannot help |
| memory | not compute, not latency, `stall_frac ≥ 0.35` | memory-bound with independent misses |
| uncertain | everything else | in between |

The regime deliberately uses **only time fractions**. An earlier "bandwidth-bound" label was
dropped because bus utilisation depends on the controller's own MSHR and prefetch choices —
raising MSHRs from 16 to 32 pushed the bus to ~1.0 and would have produced a fake "phase
change" inside one phase. Bus utilisation is still used as a guard in the rules.

### 7.4 The utility test

```
saved cycles per window  =  window cycles × stall fraction × (fraction of the stall the change should remove)
gain over the horizon    =  saved per window × HORIZON_H (20)
allowed if               gain  >  estimated switch cost
```

The estimated cost is the reconfigurator's estimate, multiplied by `ASSOC_OVERHEAD_MARGIN`
(1.5) for associativity only, because refetch misses are not in that estimate. Damage control
and resource-saving moves are exempt from the test.

**Expected fraction removed, per knob**

| Knob | Fraction of stall removed | Source |
|---|---|---|
| Prefetch on | `PF_EXPECTED_HIT_STRIDE` 0.7 or `PF_EXPECTED_HIT_NEXTLINE` 0.5 | config constants |
| Prefetch distance up | `PF_COV_GAIN_PER_STEP` 0.10 | config constant |
| MSHR up one step | `1 − 1/speedup`, `speedup = min(new/old, 1 / bus utilisation)` | Little's law and the bus floor |
| Associativity | `1 − r`, `r = shadow miss rate at candidate ÷ at current` | shadow tags |

**Worked examples (window = 10,000 cycles, horizon 20)**

* *Streaming, 16 MSHRs → 32.* stall 1.00, bus use 0.66. `speedup = min(2, 1/0.66 = 1.52) = 1.52`.
  Saved per window = 10,000 × 1.00 × (1 − 1/1.52) ≈ 3,420. Gain = 68,400 versus cost 200 →
  **allowed**. (Measured gain of the real change: 1.41×–1.69× across stream phases.)
* *Conflict phase at 2-way.* Shadow rates 1.00 (2-way) and 0.05 (8-way), so `r = 0.05`. Saved =
  10,000 × 1.00 × 0.95 = 9,500; gain = 190,000 versus cost 32,768 × 1.5 = 49,152 → net
  +140,848 → **allowed**.
* *Marginal associativity evidence.* 11% fewer misses at stall 0.25: saved = 275; gain = 5,500
  versus 49,152 → **blocked by utility** (a unit test).

### 7.5 The rules

All thresholds are in `sim_config.py`; every one can be overridden per run (`ControllerParams`).

**Prefetch (costs 10 cycles; a wrong choice is 2–3× slower)**

| Rule | Condition | Action |
|---|---|---|
| `pf-on` | prefetch off; `stall_frac ≥ 0.20`; `dep_stall_frac < 0.50`; bus `< 0.80`; spare MSHRs (`mshr_occ_frac < 0.80`); and stride confidence ≥ 0.6 → **stride**, else sequential score ≥ 0.6 → **next-line** | enable at the mode's default distance (stride 2, next-line 1) |
| `pf-off` *(urgent, fast)* | prefetch on and any of: accuracy < 0.30; coverage < 0.10 with ≥ 8 prefetches issued; both stream scores < 0.30 | switch off |
| `pf-lower` *(urgent)* | prefetch on, distance above 1, and accuracy < 0.40, or bus ≥ 0.80 with accuracy < 0.75 | one ladder step down |
| `pf-raise` | accuracy ≥ 0.75; late fraction > 0.50; bus < 0.80; spare MSHRs; distance below 16 | one step up |

**MSHR count (costs ≈ 416 cycles)**

| Rule | Condition | Action |
|---|---|---|
| `mshr-raise` | pool occupancy ≥ 0.80; `dep_stall_frac < 0.50`; bus < 0.80; **not** (prefetch on with accuracy < 0.40) | one step up |
| `mshr-shrink` *(off by default)* | `ENABLE_MSHR_SHRINK`; occupancy < 0.30; smaller pool still holds twice the observed occupancy | one step down (resource saving, exempt from utility) |

**Associativity (costs ≈ 33,000 cycles plus refetch)**

| Rule | Condition | Action |
|---|---|---|
| `assoc-better` | all three shadow rates valid; `stall_frac ≥ 0.25`; some other associativity has a shadow rate < 0.90 × the current one | switch to the best (lowest rate). Run in the other direction this is the **revert rule** — no separate mechanism, so no extra flush risk. |
| `assoc-shrink` *(off by default)* | `ENABLE_ASSOC_SHRINK`; a lower associativity with shadow rate ≤ 1.02 × current | one step down (resource saving) |

### 7.6 The gates in detail

* **Hysteresis.** `M = 3` windows for ordinary rules; `M_FAST = 2` for `pf-off` (a useless
  prefetcher costs 2–3×, so it is worth being quicker). The fast path never exceeds the normal
  one: it uses `min(M_FAST, M)` (a bug the tests caught).
* **Skip.** One window after any switch (`SKIP_WINDOW_AFTER_SWITCH`).
* **Residency.** After an MSHR or associativity switch, neither may change again for
  `MIN_RESIDENCY_K = 5` windows (they both drain the machine, so they share one clock).
  Prefetch has its own, `MIN_RESIDENCY_K_PF = 2`.
* **Selection.** One change per window. Urgent first, then largest net benefit.
  *Why not a fixed priority:* with "cheapest first", on a conflict phase the cheap MSHR rule
  fired first and blocked the 7.8× associativity fix for 5 windows; selecting by net benefit
  fixed that (that scenario improved from −68.8% to −75.8% versus baseline).

### 7.7 Why the rules look like this (evidence from phase 1)

| Design choice | Measurement behind it |
|---|---|
| Check accuracy and bus before raising MSHRs | A junk prefetcher gets *slower* as the pool grows: 767,387 → 977,898 cycles from 4 to 32 MSHRs (+27%), L2 miss rate 0.26 → 0.84, bus use 0.13 → 0.83 |
| Require spare MSHRs before enabling prefetch | When the pool is full prefetches cannot issue; growing the pool comes first |
| Prefer stride over next-line | Stride matches next-line on unit stride (80,320 vs 80,035 cycles), wins on stride-8 (80,320 vs 239,638) and stays silent on random access (0 prefetches for 2,024 accesses) |
| Distance follows *late fraction* up and *accuracy* down | On a clean stream late fraction is 1.00 up to distance 8 and 0.00 at 16 (cycles 239,623 → 51,896); on a junk pattern longer distance is monotonically worse (789,248 → 1,051,658) |
| Never act on a dependent chain | 64,800 cycles for 300 dependent loads vs 4,192 for the same loads independent; MSHRs and prefetch cannot help |
| Associativity through shadow tags | Estimates within 0.006 of real runs |
| Utility test before any switch | The associativity flush costs ≈ 33,000 cycles; a short phase cannot repay it (§10.4) |

### 7.8 What the controller records

* `trace` — one `Decision` per window: regime, confirmed regime, whether skipped, rules wanted,
  action taken, and why a ripe proposal was blocked (e.g. `residency (2/5 windows)` or
  `utility (5500 <= 49152 cycles)`).
* `regime_changes` — `(window confirmed, from, to)`; used to score phase-detection delay.

### 7.9 Walkthrough — the compute → memory → compute → pointer workload

| Windows | What the workload does | What the controller sees | What it does |
|---|---|---|---|
| 0 – 19 | compute | stall ≈ 0.00; no proposals | nothing; regime *compute* is confirmed within the first few windows |
| 20 – 22 | streaming begins | stall 1.00, MSHR pool 98% full, bus 0.66, dependent stall 0 | `mshr-raise` becomes ripe (3 windows); regime *memory* confirmed at window 22 |
| end of 22 | — | utility: gain 68,400 ≫ cost 200 | **MSHR 16 → 32** (416 cycles); window 23 skipped |
| 24 – 44 | streaming continues | pool at its maximum (32); bus near saturation | no proposals: the ladder is exhausted and a saturated bus blocks further growth |
| 45 – 47 | compute returns | stall falls | regime *compute* confirmed at window 48 |
| 65 – 66 | pointer chase | dependent stall 1.00, pool 6% used, stream scores 0 | regime *latency* confirmed at window 67; nothing helps a chase and prefetch is already off |

True phase starts were at windows 0, 20, 45, 65; confirmations came at 22, 48, 67 — delays of
2, 3 and 2 windows.

### 7.10 Parameter reference (`ControllerParams`)

| Parameter | Default | Meaning |
|---|---|---|
| `hysteresis_m` / `hysteresis_m_fast` | 3 / 2 | windows a condition must hold |
| `residency_k` / `residency_k_pf` | 5 / 2 | windows between switches (MSHR+assoc / prefetch) |
| `skip_after_switch` | 1 | windows ignored after a switch |
| `horizon_h` | 20 | windows the current behaviour is assumed to continue |
| `stall_compute` / `stall_memory` | 0.15 / 0.35 | regime thresholds |
| `stall_attention` / `stall_assoc` | 0.20 / 0.25 | memory must hurt this much to enable prefetch / change associativity |
| `dep_stall_latency` | 0.50 | dependent-stall fraction that means "latency-bound" |
| `bw_saturated` | 0.80 | bus too busy for more MSHRs or prefetch distance |
| `mshr_high` / `mshr_low` | 0.80 / 0.30 | pool occupancy thresholds |
| `seq_threshold` | 0.6 | stream score needed to enable prefetch |
| `acc_high` / `acc_low` / `acc_off` | 0.75 / 0.40 / 0.30 | prefetch accuracy thresholds |
| `cov_off`, `pf_min_issued` | 0.10, 8 | coverage floor and the sample needed to judge it |
| `late_high` | 0.50 | late fraction that means "raise the distance" |
| `irregular` | 0.30 | both stream scores below this = irregular |
| `shadow_improvement` / `shadow_no_loss` | 0.90 / 1.02 | 10% fewer misses to change; ≤ 2% more is "free" to shrink |
| `assoc_margin` | 1.5 | multiplier on the associativity cost estimate |
| `enable_mshr_shrink` / `enable_assoc_shrink` | False / False | resource-saving rules |

---

## 8. The simulation driver — `sim.py`

### 8.1 Modes

| Mode | What it does | Role |
|---|---|---|
| `baseline` | One fixed configuration throughout (default 8-way, prefetch off, 16 MSHRs) | reference |
| `dynamic` | Monitor + controller + reconfigurator; decisions from live counters only; every switch charged and logged | the system under study |
| `oracle` | Best configuration per phase, applied at phase boundaries **for free** | idealised upper bound |

Every mode attaches a monitor, so every run has a per-window feature log for charts, and every
run reports resource use (time-weighted ways, MSHR entries and share of time prefetch was on).

### 8.2 How the oracle chooses

1. For each phase **kind** in the workload, run it alone from a cold start (a probe of
   `PROBE_OPS[kind]` ops: compute 60,000; streaming/strided/mixed/conflict 12,000; pointer 600;
   chase_stream 8,000).
2. Coordinate descent from the starting configuration: prefetch (11 options: off, plus
   next-line and stride at five distances), then associativity (3), then MSHRs (4); a second
   pass only if the first changed something.
3. A change is accepted only if it beats the current choice by **2%** (`ORACLE_MIN_GAIN`), so
   phases where nothing matters keep the starting configuration.
4. Results are cached per (kind, start configuration, parameters, seed).
5. The full run applies each phase's configuration at its boundary with **no cost**: prefetch
   and MSHR settings are set directly; associativity uses `Cache.remap_assoc`, which keeps
   every line and misses in flight.

The oracle uses the workload's phase labels and a heuristic search, so it is an upper-bound
*reference*, not a deployable controller, and not a mathematically proven optimum. Compute
probes are long because short ones are dominated by cold-start misses (a 20,000-op probe made a
prefetcher look 1% useful).

### 8.3 Command line

```
python sim.py --mode dynamic                                   # default workload
python sim.py --mode oracle  --phases compute:200000,streaming:40000
python sim.py --mode baseline --assoc 4 --mshr 8 --pf stride:4 # different starting config
python sim.py --mode dynamic --csv out                         # out_windows.csv, out_switches.csv, out_trace.csv
```

### 8.4 The `Result` object

`cycles`, `instructions`, `ipc`, `overhead_cycles`, `resources()`, `summary()`, the
monitor's full history, the reconfigurator's log and overhead table, the controller's trace,
the oracle's schedule and the timeline of configuration changes.

---

## 9. Validation

### 9.1 Test suites

| Suite | Checks | What it proves |
|---|---|---|
| `check_base.py` | 23 | Core issue rate, scans, fitting sets, conflict vs associativity, writebacks, bus formula, MSHR scaling and saturation, ROB limit, dependent chains, pending-fill merging, refusal changes nothing, flush/rebuild/remap primitives, drain, window hook, dependency distances, chase beside stream, workload properties, default-workload scale |
| `check_prefetch.py` | 14 | Stride table learning / noise / retraining, config validation, detectors train while off, next-line vs stride vs pointer, quiet on random, distance hides latency, pollution grows with distance, MSHR reserve, stream signals per phase |
| `check_monitor.py` | 11 | Counter conservation, phase signatures, unknown = `None`, overhead excluded from signals, partial window, configuration-in-force, shadow ranking and agreement with real runs, prefetch fields, rebase, CSV |
| `check_reconfig.py` | 11 | Each knob's exact cost, no-op/invalid requests have no effect, flush exactness, slow-bus writeback wait, refetch accounting, safe mid-stream switches, monitor sees overhead, cycle conservation, verification fires |
| `check_controller.py` | 18 | Every rule and gate (including missing evidence), utility, hysteresis, skip, residency, ordering, regime confirmation, independence from the workload, five whole-simulation outcomes |
| `check_sim.py` | 9 | Baseline equals a plain run, a held-back controller changes nothing, free and deterministic oracle, oracle ≤ dynamic < baseline, time-weighted resources, config parsing, CLI and CSV |
| **Total** | **86** | all passing, about 50 seconds |

### 9.2 Counter-conservation laws (`System.check_invariants`)

After every simulated run these must hold exactly:
L1 accesses = core loads + stores; L1 hits + misses = accesses; L2 accesses = L1 misses;
L2 hits + misses = accesses; memory reads = L2 demand misses; memory prefetch reads = L2
prefetches issued; L1 writebacks out = L2 writebacks in; memory writes = L2 writebacks out.

### 9.3 Properties worth highlighting

* **Timing neutrality.** A monitor plus a controller that never acts changes cycle counts by
  exactly zero (`check_sim`), so observation does not perturb the system.
* **Reconfiguration safety.** The flush's dirty count is verified against memory writes, and a
  deliberately broken flush is caught.
* **No leakage of labels.** The AST test fails if the controller ever references workload or
  phase information.
* **Determinism.** The same seed yields the identical op stream on every iteration.

---
## 10. Results

All numbers are cycles from deterministic simulations (seed 1) unless stated otherwise.
"Gap closed" = (baseline − dynamic) ÷ (baseline − oracle).

### 10.1 Default workload — the three modes

| Mode | Cycles | vs baseline | IPC | Switches | Overhead | Avg L2 MSHRs | Prefetch on |
|---|---|---|---|---|---|---|---|
| Baseline | 955,822 | — | 1.727 | 0 | 0 | 16.0 | 0% |
| **Dynamic** | **870,926** | **−8.9%** | 1.896 | 1 | 416 cycles (0.048%) | 27.8 | 0% |
| Oracle | 858,438 | −10.2% | 1.923 | 2 (free) | 0 | 20.5 | 0% |

**Gap closed: 87%.** The dynamic system made one change — `n_mshr 16 → 32` at cycle 230,178
(rule `mshr-raise`, cost 416 cycles) — and then nothing, which is the correct behaviour: no
other knob helps this workload. The oracle's second switch merely restores the pool to 16 for
the second compute phase, which affects nothing (and is why its average MSHR count is lower).
Only the streaming phase benefits; compute and pointer phases are insensitive to every knob.

### 10.2 The assignment's example: compute-intensive → memory-intensive

| Event | True phase start (window) | Controller confirmed (window) | Delay |
|---|---|---|---|
| compute → streaming (memory-bound) | 20 | 22 | 2 windows |
| streaming → compute | 45 | 48 | 3 windows |
| compute → pointer chase (latency-bound) | 65 | 67 | 2 windows |

Hysteresis is 3 windows, so delays of 2–3 are the floor that setting allows. The MSHR pool was
grown in the same window the memory regime was confirmed (window 22).

### 10.3 Five situations, one unmodified controller

| Situation | Baseline | Dynamic | Oracle | Gap closed | What the controller did |
|---|---|---|---|---|---|
| Scaled default workload (the one used in tests) | 501,526 | 434,254 (−13.4%) | 421,758 | 84% | MSHR 16→32 (416 cycles) |
| Conflict phase, start at 2-way | 622,735 | 150,870 (−75.8%) | 85,903 | 88% | associativity 2→8 (≈ 33,000 cycles = 21.9% of runtime, 214 refetch misses) |
| Paced stream (compute between loads) | 534,295 | 203,903 (−61.8%) | 145,557 (−72.8%) | 85% | stride prefetch on at window 5, distance 2→4→8 (30 cycles total) |
| Pointer chase beside a stream | 1,012,664 | 955,168 (−5.7%) | 953,088 (−5.9%) | 97% | MSHR 16→32 at window 3 (416 cycles) |
| Useless prefetcher left on (pointer chase, start next-line d=4) | 321,448 | 324,000 (+0.8%) | — | — | prefetch off at window 2 (10 cycles) |

Notes. *Paced stream:* the stride unit's distance was raised twice because most useful prefetches
arrived late while accuracy stayed high; MSHRs were never the limit. *Conflict:* the overhead is
large in relative terms but still pays: the run is 4× faster. *Useless prefetcher:* the
controller correctly identified a prefetcher with ~0% accuracy and removed it; the +0.8% is the
residual of a few chance hits the prefetcher had been getting, not a mistake.

### 10.4 Break-even: how long must a phase last for associativity switching to pay?

Conflict phase between two compute phases, start at 2-way. Baseline stays at 2-way.

| Phase length (ops) | Windows in phase (at 2-way pace) | Baseline | Dynamic | Dynamic vs baseline |
|---|---|---|---|---|
| 6,000 | 3 | 86,799 | 119,927 | **+38.2%** (worse) |
| 12,000 | 6 | 121,319 | 128,870 | **+6.2%** (worse) |
| 20,000 | 11 | 166,615 | 130,870 | −21.5% |
| 30,000 | 16 | 223,943 | 133,370 | −40.4% |
| 45,000 | 25 | 309,263 | 137,120 | −55.7% |
| 60,000 | 33 | 394,919 | 140,870 | −64.3% |
| 100,000 | 56 | 622,735 | 150,870 | −75.8% |

The controller always switched at window 6 (3 windows to confirm plus the 3-window
hysteresis). The cost is roughly constant — about 33,000 cycles plus the slow windows spent
detecting — so the **break-even lies between 6 and 11 windows of thrashing** (about 12,000–20,000
ops). Below it, adapting is a loss. This is the expected consequence of "adaptation is not
free" and is the first of the negative cases the report needs.

### 10.5 Measured switch costs (recap)

| Switch | Cycles charged | Components |
|---|---|---|
| Prefetch mode or distance | 10 | control-register write |
| MSHR count under load | 416 | 216 drain + 200 fixed |
| Associativity, 2,048 lines resident (1,536 dirty) | 32,768 | flush walk dominates |
| Associativity, bus twice as slow, 3,584 dirty lines | 57,344 | writeback wait dominates |
| Associativity in the conflict scenario | 32,992 | ≈ 32,768 walk + ≈ 224 drain (derived), + 214 refetch misses afterwards |

---

## 11. What the results tell us

### 11.1 Each knob, when it helps and when it does not

**MSHR count** (4 / 8 / 16 / 32), cycles by phase kind, prefetch off, default bus:

| Phase | 4 | 8 | 16 | 32 | 16 → 32 |
|---|---|---|---|---|---|
| compute | 50,375 | 50,371 | 50,371 | 50,371 | none |
| streaming | 1,080,048 | 540,112 | 271,400 | 191,816 | 1.41× |
| strided | 1,080,024 | 540,056 | 270,120 | 160,208 | 1.69× |
| mixed | 1,016,096 | 508,128 | 254,568 | 162,936 | 1.56× |
| conflict (at 2-way) | 1,371,192 | 685,704 | 345,120 | 249,664 | 1.38× |
| pointer (600 ops) | 129,600 | 129,600 | 129,600 | 129,600 | none |

*(Measured with an ad-hoc script, not part of the test suite.)* The benefit follows Little's
law: MSHRs needed ≈ miss rate × latency. A stream missing every 8 cycles needs about 27 with a
216-cycle round trip, so 32. Compute has almost no misses and a pointer chase only ever has one
in flight, so nothing helps them. With a bus twice as slow, 16 and 32 MSHRs tie (320,200 cycles):
the bus, not the pool, is the limit — "more MSHRs stops helping".

**Prefetch** (clean paced stream, next-line, cycles 239,623 with prefetch off):

| Distance | 1 | 2 | 4 | 8 | 16 |
|---|---|---|---|---|---|
| Cycles | 159,829 | 119,866 | 80,035 | 51,896 | 51,896 |
| Speed-up | 1.50× | 2.00× | 2.99× | 4.62× | 4.62× |
| Late fraction | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 |

The distance needed ≈ miss latency ÷ time between accesses (216 / 25 ≈ 8.6 lines), which is why
late prefetches vanish only at 16 and why distance 16 gains nothing more (the loop is then
paced by compute, not memory). **Stride** equals next-line on unit stride and is the only one
that helps stride-8 access (80,320 vs 239,638 cycles; next-line is 0% useful). For this reason
the controller treats next-line as a fallback.

**Associativity** (conflict pattern, 6 lines per set): 2-way and 4-way miss every access (93,552
cycles); 8-way misses none (12,039 cycles) — a 7.8× cliff, not a gradient.

### 11.2 The knobs interact — MSHRs and prefetch

Reusable working set near L2 capacity with a next-line prefetcher (every prefetch is junk):

| L2 MSHRs | Cycles | L2 miss rate | Prefetches issued | Bus use |
|---|---|---|---|---|
| 4 | 767,387 | 0.26 | 5,683 | 0.13 |
| 8 | 888,962 | 0.45 | 16,093 | 0.25 |
| 16 | 932,884 | 0.66 | 37,938 | 0.47 |
| 32 | 977,898 | 0.84 | 79,997 | 0.83 |
| *prefetch off* | *354,062* | *0.13* | *0* | — |

A bigger pool lets more junk prefetches in flight, so the same MSHR change that gives 6.7× on a
stream costs **27%** here — and turning the prefetcher off is far better than shrinking the pool.
Hence the controller (a) never raises MSHRs while a low-accuracy prefetcher runs, (b) acts on a
useless prefetcher with priority, and (c) requires spare MSHRs before enabling prefetch.
Prefetch distance shows the opposite shape on a junk pattern: 789,248 → 932,884 → 1,051,658
cycles at distance 1 / 4 / 16, so distance must follow *measured accuracy*, not be fixed.

Measured with random independent demand misses and no prefetch, throughput never gets worse as
the pool grows (958k → 479k → 240k → 142k cycles) but miss latency rises from 212 to 252
cycles (+19%) once the bus saturates. That is the cost a latency-critical load sharing the bus
would pay. The chase-beside-stream experiment shows the same effect directly: the chase's own
miss latency stays at 216 cycles until 32 MSHRs, where it becomes 256, yet total time per hop
still falls 6.7× (3,456 → 513 cycles at 63 stream loads per hop) because the stream is real work.

### 11.3 What adaptation costs

* **Cost spans four orders of magnitude**, which is why the controller treats the knobs
  differently: prefetch (10) may be switched freely; MSHR (≈ 416) needs a sustained pool-limited
  stream; associativity (≈ 33,000 + refetch) needs evidence of a large saving that lasts.
* **Three negative cases are now reproducible:** (1) associativity switching in a short phase
  (§10.4: +6.2% at 6 windows, +38% at 3); (2) more MSHRs under a bandwidth limit (no gain, +19%
  miss latency); (3) prefetch pollution (2.2×–3.0× slower).
* **Estimates versus reality:** the controller's estimates omit exactly what it cannot know —
  measured drain, bus-bound writebacks and refetch misses — and the overhead table reports the gap.

### 11.4 Dynamic versus the oracle

The dynamic system closes 84–97% of the oracle's gain. The remainder comes from:
detection delay (2–3 windows to confirm a regime plus 3 windows of hysteresis before acting),
the skipped window after each switch, one-step-at-a-time MSHR growth with residency between
steps, estimate granularity, and the oracle's advantages — it knows the phase boundaries, pays
nothing to switch, uses an associativity change that keeps every line, and searches 18
configurations per phase. The gap is the price of being deployable.

### 11.5 A model property that affects experiment design

In this model **8-way is never worse than 2- or 4-way** (there is no hit-latency or energy cost
for more ways) and the baseline is 8-way, so associativity adaptation can only show a gain when
the start is below 8-way (as in the conflict scenarios); with an 8-way baseline it can only add
switching cost. Likewise nothing rewards a smaller MSHR pool. The shrinking rules exist
(`ENABLE_MSHR_SHRINK`, `ENABLE_ASSOC_SHRINK`) but are off. If the report wants to claim
resource-utilisation gains, a resource-cost model (active ways and MSHR entries over time, or an
L2 hit latency that grows with associativity) must be chosen; `Result.resources()` already
reports the time-weighted quantities.

### 11.6 Correspondence to real hardware (as far as I know)

* **Counters and thresholds with hysteresis** — realistic as a small firmware or state-machine
  controller.
* **Prefetch control** — real chips expose prefetcher enable bits and depth settings, and
  research designs adjust aggressiveness from measured accuracy, as here.
* **MSHR throttling** — studied in research as a way to control memory-level parallelism;
  commercial chips mostly fix the count.
* **Associativity** — real caches cannot re-index at run time because the tag and index bits
  are fixed in the array; the practical analogue is **way gating or masking**, where capacity
  falls with the ways. Our flush-and-rebuild at fixed capacity is an idealised, research-style
  reconfigurable cache. The oracle's remap is a further idealisation.
* **L1 kept fixed** because it sits on the critical hit path and is almost never reconfigured.

---

## 12. Design decisions and their rationale

| Decision | Rationale |
|---|---|
| Python simulator, not gem5 | Runtime reconfiguration of gem5 caches needs C++ work; a Python model lets every knob change live and gives fast sweeps. gem5 stays an optional static cross-check |
| L1 fixed; only the L2 adapts | Two adaptive levels would interact and make causes untraceable; the L2 knobs already give three cost levels for the break-even analysis |
| One L2 object reconfigured in place (no multiplexed copies) | Matches "not a new architecture per workload"; avoids two caches holding the same line |
| Fixed capacity and line size under associativity changes | Isolates the effect of associativity and keeps it "the same architecture" |
| Associativity switch = flush and refill | Simple, correct, and makes the refetch cost visible; the cold-start cost is the honest price |
| Static rules on live counters; no benefit table | Satisfies "decide on the fly with no prior experience"; avoids tuning to the test workload; generic by construction |
| Horizon assumption (20 windows) | The one explicit assumption that turns per-window gain into a decision; to be swept as a sensitivity test |
| Hysteresis + skip window + residency + utility | Prevents thrashing, ignores cold-start artefacts, and refuses changes that cannot repay their cost |
| One change per window | Avoids interaction effects between simultaneous changes |
| Select by urgency then net benefit | A fixed cheapest-first order delayed the largest win by 5 windows |
| Regime label from time fractions only | The bus and MSHR load depend on the controller's own choices; using them produced false phase changes |
| Shadow tags for associativity | Predict another associativity's misses without trying it; validated to within 0.006 |
| Detectors train even when prefetch is off | Lets the controller learn that a stream is regular *before* enabling prefetch |
| Missing evidence is `None`, never 0 | The controller must not act on data it does not have |
| Shrinking rules off by default | They only save resources; no resource cost exists in the model yet |
| Oracle uses labels and free switches | It is an upper-bound reference, clearly separated from the deployable controller |
| Idealised `remap_assoc` for the oracle only | Gives the "free switch" the oracle needs without letting it leak into real switches |

---

## 13. Limitations and threats to validity

**Model**
* Simplified core: no branch prediction, no instruction latencies except loads, no port limits;
  conclusions are relative, not absolute IPC.
* Caches are scaled down (256 KB L2); single core; non-inclusive hierarchy; the bus is FIFO in
  call order; stores are not tracked in the ROB.
* A run ends when its loads are done; store fills may still be in flight, so a drain right after
  a run can take a few hundred cycles.
* Evicting a still-in-flight line (only when every way of a set is in flight) drops that line
  and it re-misses; measured below 0.01% of L2 misses at 2-way.
* A prefetched line that returns a flushed line is not counted as a refetch miss.

**Workloads and evaluation**
* Default phases are extreme (signals pinned near 0 or 1), which makes thresholds easy and the
  controller look good. A second workload with intermediate-intensity phases is required before
  claiming generality.
* All controller thresholds are first-guess values; none has been tuned, and the horizon
  and window size have not been swept.
* Results use one seed; variance has not been measured.
* The oracle uses phase labels and a coordinate-descent search with short probes; it is an
  idealised reference, not a proven optimum.
* The dependency model refers to the k-th most recent load; irregular interleavings of several
  independent chains need a generator that computes the distance.

**Scope**
* Capacity and cache-level changes were deliberately excluded (a full flush would almost always
  cost more than it saves; to be mentioned as a boundary condition).
* Tested on Python 3.12 only; the code avoids features newer than 3.8 and uses
  `from __future__ import annotations`, but older versions have not been run.

---

## 14. Reproducing everything

```
# all validation (86 checks, about 50 seconds)
python check_base.py && python check_prefetch.py && python check_monitor.py \
  && python check_reconfig.py && python check_controller.py && python check_sim.py

# run a subset of a suite
python check_controller.py -k assoc

# the three modes on the default workload
python sim.py --mode baseline
python sim.py --mode dynamic
python sim.py --mode oracle

# a custom workload and starting configuration, writing CSVs for charts
python sim.py --mode dynamic --phases compute:100000,conflict:100000,compute:100000 \
              --assoc 2 --csv conflict_run
```

Programmatic use:

```python
import sim
from workload import Workload

w = Workload([("compute", 200_000), ("streaming", 40_000), ("compute", 200_000), ("pointer", 600)])
b, d, o = sim.run_baseline(w), sim.run_dynamic(w), sim.run_oracle(w)
print(d.summary())                     # cycles, ipc, overhead, switches, resource use
for row in d.reconfig.overhead_table():# per-knob cost, estimate vs real, refetch misses
    print(row)
d.controller.trace_rows()              # per-window decisions (for timelines)
d.controller.regime_changes            # confirmed phase changes (for detection scoring)
```

---

## 15. Open decisions and phase 3 plan

### Decisions for you
1. **Resource objective.** Keep shrinking rules off and report resource use only, or choose a
   resource-cost model and enable them?
2. **Baseline associativity.** An 8-way baseline cannot show associativity gains. Choose a lower
   baseline (for example 4-way) or report associativity only in scenarios that start below 8.
3. **Approval of the criteria as built** — in particular the two departures from the original
   criteria: selection by urgency then net benefit (instead of fixed priority) and the added
   "spare MSHRs" requirement for prefetch-on.

### Phase 3 work
1. **Tuning workload and freeze.** A workload separate from the test workloads; tune hysteresis,
   residency, horizon, window size, thresholds and the associativity margin on it; then freeze
   and record the values.
2. **Experiments.** Baseline vs oracle vs dynamic on the test workloads; detection delay and
   false-switch counts against the true boundaries; break-even sweeps for every knob (the
   associativity sweep in §10.4 is the template); the three negative cases; horizon
   sensitivity; several seeds with spread; the second workload with the controller unchanged.
3. **Charts and tables.** Counters over time with detected and true transitions; configuration
   timeline; execution time for the three groups; overhead table (switches, drain, stall,
   refetch, % of runtime); break-even curves; estimated versus measured overhead.
4. **Final report**, then optionally a gem5 cross-check of static settings (trends only).

---

## 16. Appendices

### A. Public interfaces

| Object | Key calls |
|---|---|
| `build_system(...)` → `System` | `.core`, `.l1`, `.l2`, `.memory`, `.pf`, `.snapshot()`, `.reset_counters()`, `.check_invariants()` |
| `Core` | `.run(ops, on_window, window_cycles, max_cycles, max_ops)`, `.drain()`, `.charge(n)`, `.snapshot()` |
| `Cache` | `.access`, `.prefetch`, `.writeback`, `.flush_invalidate`, `.set_assoc`, `.remap_assoc`, `.set_n_mshr`, `.resident_lines`, `.observers`, `.snapshot` |
| `PrefetchUnit` | `.set_config(mode, distance)`, `.resolve`, `.config()`, `.observe`, `.snapshot` |
| `Monitor(system)` | `.sample(core, final)`, `.on_window`, `.history`, `.running_mean`, `.rebase`, `.write_csv` |
| `Reconfigurator(system, monitor)` | `.set_prefetch`, `.set_n_mshr`, `.set_assoc`, `.apply`, `.estimate`, `.overhead_table`, `.log` |
| `Controller(system, monitor, rc, **params)` | `.on_window`, `.step`, `.propose`, `.trace`, `.regime_changes` |
| `sim` | `run_baseline`, `run_dynamic`, `run_oracle`, `run(mode, ...)`, `make_config`, `resource_use`, `oracle_schedule`, `main` |

### B. Hardware constants (`sim_config.py`)

`LINE_SIZE` 64; `L1_SIZE` 32 KB, `L1_ASSOC` 8, `L1_LATENCY` 4, `L1_MSHR` 64; `L2_SIZE` 256 KB,
`L2_ASSOC_CHOICES` (2, 4, 8), `L2_ASSOC_DEFAULT` 8, `L2_LATENCY` 12; `DRAM_LATENCY` 200,
`DRAM_BUS_CYCLES` 8; `CORE_WIDTH` 4, `ROB_SIZE` 128, `MAX_DEP_DISTANCE` 64; `MSHR_CHOICES`
(4, 8, 16, 32), `MSHR_DEFAULT` 16, `MSHR_DRAIN_STALL` 200; `PF_MODES` (off, next-line, stride),
`PF_DISTANCE_LADDER` (1, 2, 4, 8, 16), `PF_DISTANCE_STALL` 10, `PF_MSHR_RESERVE` 1;
`WINDOW_CYCLES` 10,000; `SHADOW_SAMPLE_SETS` 256, `SHADOW_MIN_SAMPLES` 32,
`STREAM_MIN_SAMPLES` 32; `K_ASSOC_PER_LINE` 8; `CONFLICT_SETS` 128, `CONFLICT_WAYS` 6.

### C. Glossary

| Term | Meaning |
|---|---|
| MSHR | Miss status holding register: tracks one outstanding cache miss; the count bounds how many misses overlap |
| Window | One monitoring interval (10,000 cycles of workload time) |
| Regime | The controller's coarse label for a window: compute, memory, latency or uncertain |
| Hysteresis | Requiring a condition to hold for several windows before acting |
| Residency | Minimum windows between switches of the same knob group |
| Horizon | Number of windows the current behaviour is assumed to continue |
| Utility | Expected saving over the horizon minus the estimated switch cost |
| Shadow tags | Tag-only copies of the L2 at other associativities, used to predict their miss rates |
| Late hit / late prefetch | A request that arrives while its line is still being fetched; a prefetch that arrived too late to hide the whole latency |
| Refetch miss | First demand miss to a line that was resident when an associativity switch flushed the cache |
| Oracle | Idealised reference that picks the best configuration per phase and switches for free |
| Break-even phase length | The phase length at which the savings from a switch equal its cost |

### D. Test inventory (generated from the suites)

**`check_base.py`** — Hardware: core, caches, memory, workloads (23 checks)

* `test_compute_ipc`
* `test_sequential_scan`
* `test_fitting_working_set`
* `test_conflict_vs_assoc`
* `test_writebacks`
* `test_bandwidth`
* `test_mshr_scaling`
* `test_mshr_saturation_under_bandwidth_pressure`
* `test_compute_insensitive_to_mshr`
* `test_rob_limit`
* `test_dependent_chain`
* `test_dependency_distance_semantics`
* `test_chase_stream_dependencies_point_at_the_previous_hop`
* `test_chase_beside_stream`
* `test_pending_hit_merges`
* `test_refused_access_changes_nothing`
* `test_remap_assoc_keeps_lines_and_charges_nothing`
* `test_flush_and_set_assoc`
* `test_drain`
* `test_window_callback`
* `test_prefetch_hook`
* `test_workload_properties`
* `test_default_workload_scale`

**`check_prefetch.py`** — Prefetch unit and stride detection (14 checks)

* `test_stride_table_learns_and_tolerates_noise`
* `test_stride_table_retrains_negative_and_ignores_huge`
* `test_stride_table_independent_pcs_and_capacity`
* `test_unit_config`
* `test_off_trains_detectors_but_never_issues`
* `test_idle_unit_does_not_change_timing`
* `test_next_line_helps_stream_not_pointer`
* `test_stride_helps_strided_and_next_line_does_not`
* `test_stride_matches_next_line_on_unit_stride`
* `test_stride_quiet_on_random_access`
* `test_distance_hides_latency`
* `test_pollution_grows_with_distance`
* `test_mshr_reserve_and_counters`
* `test_stream_signals_by_phase_kind`

**`check_monitor.py`** — Monitor and shadow tags (11 checks)

* `test_counters_conserved`
* `test_phase_signatures`
* `test_unknown_fields_are_none`
* `test_overhead_stays_out_of_signals`
* `test_final_partial_window_and_tail`
* `test_config_recorded_as_in_force_during_window`
* `test_shadow_ranks_associativity_and_ignores_real_config`
* `test_shadow_predicts_real_miss_rate`
* `test_prefetch_effectiveness_fields`
* `test_rebase_after_counter_reset`
* `test_csv_and_running_mean`

**`check_reconfig.py`** — Reconfigurator (11 checks)

* `test_prefetch_switch_is_cheap_and_touches_no_data`
* `test_mshr_switch_idle_and_with_misses_in_flight`
* `test_assoc_switch_cost_and_flush_exactness`
* `test_assoc_switch_waits_for_writebacks_when_bus_is_slow`
* `test_assoc_refetch_misses_count_only_lines_that_come_back`
* `test_switching_with_misses_and_prefetches_in_flight_is_safe`
* `test_monitor_reports_switch_cost_as_overhead`
* `test_log_table_and_cycle_conservation`
* `test_noop_and_invalid_requests_have_no_side_effects`
* `test_estimates_follow_the_config`
* `test_verification_catches_a_broken_flush`

**`check_controller.py`** — Controller rules, gates and outcomes (18 checks)

* `test_regime_classification`
* `test_prefetch_on_rules`
* `test_prefetch_off_lower_raise_rules`
* `test_mshr_rules`
* `test_assoc_rules`
* `test_utility_gate`
* `test_hysteresis`
* `test_skip_window_and_residency`
* `test_one_change_per_window_urgent_first_then_net_benefit`
* `test_regime_confirmation_needs_hysteresis`
* `test_on_window_ignores_missing_partial_and_final`
* `test_parameters_and_independence_from_the_workload`
* `test_default_workload_end_to_end`
* `test_nothing_to_gain_means_no_switches_and_identical_timing`
* `test_paced_stream_turns_prefetch_on_and_raises_distance`
* `test_useless_prefetcher_is_switched_off_on_a_pointer_chase`
* `test_conflict_phase_triggers_associativity_switch`
* `test_short_phase_switch_loses_and_long_phase_wins`

**`check_sim.py`** — Simulation driver (9 checks)

* `test_baseline_matches_a_plain_run`
* `test_dynamic_with_the_controller_held_back_equals_baseline`
* `test_oracle_switches_are_free_and_follow_the_phases`
* `test_oracle_search_is_deterministic_and_cached`
* `test_oracle_bounds_dynamic_and_dynamic_beats_baseline`
* `test_resource_use_is_time_weighted`
* `test_dynamic_result_reports_its_own_overhead_and_resources`
* `test_config_helpers_and_parsing`
* `test_command_line_runs_every_mode_and_writes_csv`

