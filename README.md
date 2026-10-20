# Base Simulator Report

**Self-Evolving Computer Architecture — Phase 1: Base Simulator**

---

## 1. Scope of this report

This report documents the design and implementation of the **base simulator** — the fixed substrate on which the adaptive controller will later run. It covers:

- The architectural model (what is simulated, what is simplified, and why)
- The design decisions behind each module
- The exact implementation of `sim_config.py`, `workload.py`, `memory.py`, `cache.py`, `core.py`, `system.py`
- The validation suite (`check_base.py`) and what each test proves
- The known limitations and their justification

The guiding constraint throughout: **the base simulator must be a faithful enough timing model that the controller's decisions (which cache associativity, prefetch policy, and MSHR count to use) are meaningfully evaluated, but simple enough that the whole loop is understandable, reproducible, and fast to run across many experiments.**

---

## 2. Architectural model

### 2.1 The simulated system

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│   Core   │────▶│    L1    │────▶│    L2    │────▶│  Memory  │
│ (issue,  │     │ (fixed)  │     │(adaptive)│     │ (latency │
│  ROB)    │◀────│          │◀────│          │◀────│  + bus)  │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
     │                                                    ▲
     │              writebacks (dirty evictions)          │
     └────────────────────────────────────────────────────┘
```

- **L1:** fixed 32 KB, 8-way, 4-cycle hit latency, 64 MSHRs. Never reconfigured.
- **L2:** adaptive 256 KB, 2/4/8-way, 12-cycle hit latency, 4/8/16/32 MSHRs. This is the only reconfigurable cache.
- **Memory:** 200-cycle DRAM latency, bus occupancy of 8 cycles per line transfer. Bus is FIFO.
- **Core:** simplified out-of-order model with a 128-entry ROB, 4-instruction issue width, dependent-load stalling, and MSHR-full refusal handling.

### 2.2 Design philosophy: what is modeled and what is not

**Modeled faithfully enough for the controller to matter:**

- **Set-associative cache behavior.** Tag storage, LRU replacement, dirty bits, write-back with write-allocate. This is what determines whether associativity changes help or hurt.
- **MSHRs and pending fills.** Multiple outstanding misses can be in flight up to a per-cache limit. A second request to an in-flight line merges into the fill (counted as a "late hit"). This is what makes MSHR count meaningful.
- **Bus bandwidth.** Each line transfer occupies the bus for `DRAM_BUS_CYCLES`. This is what produces the "more MSHRs stops helping" behavior.
- **Reorder buffer and dependent-load stalling.** Independent loads can overlap; dependent loads (pointer chasing) cannot. This distinguishes the workloads that benefit from MSHR increases from those that do not.
- **Refused-access semantics.** When MSHRs are full, the access is refused without changing state. The core waits and retries. This prevents silent state corruption and makes "MSHR pressure" measurable.

**Simplified, and stated as such in the report:**

- **No branch prediction.** All instructions issue in program order.
- **No instruction latencies except loads.** Compute ops retire in one cycle (at 4 per cycle). This keeps `compute` phases pure-IPC.
- **No store-to-load forwarding.** Stores retire through the cache; a store miss does not block subsequent loads.
- **No port limits on the L1.** The L1 can service one access per cycle from the core, but there is no modeling of bank conflicts or port contention inside the cache.
- **No front-end model.** Fetch is not modeled; the workload is a pre-fetched op stream.

These simplifications do not affect the controller's core decisions. The controller observes IPC, stall fraction, MPKI, MSHR occupancy, bus utilization, prefetch accuracy, and shadow-tag miss estimates. Each of these is preserved by the simplified model. What is lost is cycle-level accuracy — an absolute cycle count of, say, "this phase took 200,000 cycles" is not a prediction of a real processor. But the *relative* ordering (which configuration is faster for which phase, by roughly how much) is preserved, and that is what the controller uses.

---

## 3. Module-by-module design and implementation

### 3.1 `sim_config.py` — Single source of truth

Every tunable constant lives here. The design rule: **no other file contains a number that could reasonably change between experiments.**

**Sections:**

1. **Cache geometry.** L1 size, associativity, latency; L2 size, associativity ladder, latency, default.
2. **Latencies and bandwidth.** DRAM latency, bus occupancy.
3. **Core model.** Issue width, ROB size.
4. **MSHR ladder.** L2 MSHR choices, default, drain stall.
5. **Prefetch.** Modes, distance ladder, thresholds, expected hit rates for the live estimator.
6. **Monitor.** Window size, running-average window, shadow sample set count, stride table size.
7. **Controller.** Phase classification thresholds, hysteresis, residency, horizon, per-rule thresholds.
8. **Reconfiguration overhead estimates.** What the controller *believes* a switch costs, versus what the simulator *measures*.
9. **Experiment settings.** Seeds, modes, baseline config.
10. **Workload.** Default phases, conflict phase parameters.

**Key design invariant:** `L2_SIZE % (assoc * LINE_SIZE) == 0` for every associativity in the ladder. This guarantees that associativity changes are **line-preserving** — the total number of lines is constant (4096), only the set/way split changes. Verified at import time by `validate()`.

**Reconfiguration overhead model.** The controller and the simulator use the same formula for each parameter's overhead (`K_ASSOC_PER_LINE`, `MSHR_DRAIN_STALL`, `PF_DISTANCE_STALL`), but the simulator additionally counts **warm-up misses** after a switch. This gap — the component the controller cannot predict — is a deliberate part of the experimental design. The report will quantify how large it is and whether the controller's utility function remains profitable in spite of it.

### 3.2 `workload.py` — Phase-based op stream generator

The workload is a deterministic stream of 4-tuples:

```
(kind, addr, dep, pc)
```

- `kind`: `OP_COMPUTE`, `OP_LOAD`, or `OP_STORE`.
- `addr`: byte address for loads/stores; `None` for compute.
- `dep`: `True` if the load's address depends on the previous load's data (pointer chasing).
- `pc`: identifier of the access stream (what a real PC would identify). The stride prefetcher keys on it.

**Design decisions:**

- **One op = one instruction.** An op index is also an instruction count, which makes phase boundaries easy to map to window instructions later.
- **Deterministic replay.** The stream is identical on every iteration of `ops()`, so baseline, oracle, and dynamic runs consume the same sequence. This is essential for fair comparison.
- **Regions.** Each phase kind lives in its own 256 MB address region (`REGION_SHIFT = 28`). A streaming phase never warms a pointer phase's data, and repeated compute phases reuse the same small state. This makes each phase's behavior independent of the others.
- **Phase boundaries recorded up front.** `boundaries` stores `(index, kind, start_op, end_op)` for each phase. This is written to a separate JSON file for **scoring only**. The controller must never read it; a separate file is the design's way of making that structurally impossible.

**Phase generators:**

| Kind | Behavior | Footprint |
|---|---|---|
| `compute` | 10% memory ops (70% loads, 30% stores) to an 8-line working set | ~8 lines |
| `streaming` | Sequential loads/stores over an 8× L2 array | 8 × L2 lines |
| `strided` | Loads at 8-line stride over a 4× L2 array | 4 × L2 lines (strided) |
| `pointer` | Dependent chain of loads through a random permutation | 8 × L2 lines |
| `mixed` | 70% sequential, 30% random | 8 × L2 lines |
| `conflict` | 6 lines per L2 set at every associativity, 128 sets | 768 lines |

**Pointer chain construction.** Sattolo's algorithm generates a single random cycle through all lines, so the pointer chase cannot fall into a short loop that would warm the cache and hide the latency. This is important: a short loop would look like a `streaming` phase with high hit rate, not like a latency-bound dependent chain.

**Conflict phase.** `CONFLICT_WAYS = 6` is chosen so that at 2-way and 4-way the cache thrashes (6 lines per set cannot fit in 2 or 4 ways), and at 8-way it fits. This makes associativity changes observable in isolation. The constraint `CONFLICT_SETS * CONFLICT_WAYS > L1 line count` ensures the L1 cannot absorb the conflict pattern and hide it from the L2.

**Interleaving.** For non-compute phases, memory ops are interleaved with `compute_per_mem` compute ops. The default is 1. This makes the streaming phase memory-bound (0.15 IPC at `compute_per_mem=1`), which is intentional. Higher values of `compute_per_mem` let the experimenter construct an intermediate workload when the monitor's classification thresholds need to be tested against an "uncertain" band.

**Extension hooks:**

- `dep` field exists from day one for dependent loads.
- `pc` is present for stride prefetcher.
- `params` override lets experiments change `compute_per_mem`, `footprint_mult`, etc. per run.
- `boundaries` is written to a separate file, not stored on the workload object accessible to the controller.

### 3.3 `memory.py` — DRAM with latency and bandwidth

The memory model has two independent limits:

1. **Latency:** 200 cycles from request to data.
2. **Bus occupancy:** 8 cycles per line transfer. The bus serializes transfers.

**Why both?** Latency alone makes MSHRs free (more outstanding misses → always faster). Bus occupancy alone makes MSHRs useless (bus is the only limit, latency is hidden). Together they produce the actual MSHR behavior: small MSHR counts are latency-bound, large counts are bandwidth-bound, and the transition point is what the controller's Rule E detects.

**The downstream interface.** `Memory` implements the same interface as `Cache`:

```
access(addr, now, is_write, is_prefetch, pc) -> Result
writeback(addr, now) -> None
drain_time() -> 0
retire_all(now) -> no-op
```

This means `Cache` can sit on top of either another `Cache` or `Memory` with the same code. The interface is defined once in `memory.py` (as `Result`) and shared.

**Counters.** `reads`, `prefetch_reads`, `writes`, `bus_busy_cycles`. The monitor takes **differences between snapshots**, not resets. This is a crucial design choice: resetting counters mid-run would lose information about pending MSHRs and in-flight writebacks. Diffs are clean and correct.

**`bus_util(busy_delta, elapsed)`** is a static method because it is a pure function of two snapshot values. This keeps the monitor's job simple: take two snapshots, subtract, call `bus_util`.

**Writebacks are posted.** A dirty line evicted from L2 calls `memory.writeback(addr, now)`. The bus is reserved for `bus_cycles`, but nobody waits for the write to complete. This is realistic: writebacks are fire-and-forget in real hardware, and it is essential for the "MSHRs don't help once bandwidth-saturated" behavior, because writebacks consume bus bandwidth without occupying MSHRs.

### 3.4 `cache.py` — The central module

This is the largest and most important module. It implements a set-associative, write-back, write-allocate cache with LRU replacement, per-set MSHRs, pending-fill tracking, and prefetcher bookkeeping.

#### 3.4.1 Line representation

A line is a list of 6 fields:

```
[valid, tag, dirty, prefetched, lru, ready]
```

- `valid`: tag is meaningful.
- `tag`: address tag.
- `dirty`: line has been written and must be written back on eviction.
- `prefetched`: line was filled by a prefetch, not a demand miss. Used for accuracy accounting.
- `lru`: logical timestamp for LRU.
- `ready`: **cycle at which the line's data actually arrived.**

The `ready` field is the key innovation. A line can be **allocated** (tag installed) when its miss is issued, but its data arrives later. A request that finds the line before `ready` **merges into the in-flight fill** and waits until `ready`. This is counted as a `late_hit`, not a miss. This models how real caches merge second-level requests into pending MSHR entries.

Without `ready`, the simulator would either (a) not allocate the line until the fill completes, missing the merge behavior, or (b) allocate it immediately and serve garbage data. Both are wrong. With `ready`, the cache behaves correctly: a line is a placeholder until the fill arrives, and any request to it waits.

#### 3.4.2 Geometry

Sets are built lazily:

```python
def _build(self, assoc):
    self.assoc = assoc
    self.n_sets = self.size // (assoc * cfg.LINE_SIZE)
    self.sets = [[[False, 0, False, False, 0, 0]
                  for _ in range(assoc)]
                 for _ in range(self.n_sets)]
```

The `validate()` in `sim_config.py` guarantees `size % (assoc * LINE_SIZE) == 0`, so `n_sets` is an integer. When associativity changes, `_build` is called again, producing a new set structure with the same total line count but a different set/way split. **The cache must be empty** before `_build` is called — this is enforced by `set_assoc` raising if `count_valid() > 0`.

#### 3.4.3 Address decoding

```python
def _decode(self, addr):
    line = addr // cfg.LINE_SIZE
    return line % self.n_sets, line // self.n_sets
```

The set index is the low bits of the line address; the tag is the high bits. When `n_sets` changes (associativity change), the split point moves. A line that was in set 137 at 2-way has a different set index at 8-way. This is why **line-preserving remap** is nontrivial: the same physical storage holds different logical lines.

#### 3.4.4 Access protocol

`access(addr, now, is_write, is_prefetch, pc)`:

1. Retire completed MSHRs up to `now` (frees slots).
2. Search the decoded set for a matching tag.
3. **Hit path** (`_hit`):
   - Increment `accesses` and `hits`.
   - Compute `completion = max(now + latency, ready)`.
   - If `ready > now`, the line is a pending fill; increment `late_hits`.
   - If the access is a demand hit on a prefetched line, increment `pf_useful` (or `pf_late` if the fill was still pending).
   - If it is a write, mark dirty.
   - Touch LRU.
4. **Miss path** (`_miss`):
   - If MSHRs are full, increment `blocked` and return `Result(False, None, earliest_mshr_completion)`. **No state changes.**
   - Otherwise, call the downstream with `t_down = now + latency` (tag check latency, then downstream).
   - If the downstream refuses, increment `blocked` and return its retry time. **No state changes.**
   - Pick a victim, evict it (write back if dirty), install the new tag with `ready = completion`.
   - Push `completion` onto the MSHR heap.
   - Update counters and return `Result(False, completion, None)`.
5. **Prefetcher training:** after a demand access, if the prefetcher is set, call `observe()` and issue any returned prefetches via `prefetch()`.

**Refused-access semantics are the central correctness rule.** A refused access does not increment `accesses`, `hits`, or `misses`, does not evict, does not install, does not touch LRU, and does not call downstream. The only counter that changes is `blocked`. This is tested in `test_refused_access_changes_nothing`. Without this rule, MSHR pressure would corrupt accounting; with it, everything is conserved.

#### 3.4.5 Victim selection

```python
def _victim(self, lines, now):
    lru_any = lines[0]
    lru_ready = None
    for line in lines:
        if not line[V]:
            return line
        if line[LRU] < lru_any[LRU]:
            lru_any = line
        if line[READY] <= now and (lru_ready is None or line[LRU] < lru_ready[LRU]):
            lru_ready = line
    return lru_ready if lru_ready is not None else lru_any
```

Priority: invalid line first, else LRU among ready lines, else plain LRU. This is a refinement over plain LRU: it prefers evicting lines whose data has arrived, because evicting a pending fill drops the in-flight data. The final `lru_any` fallback (when all lines are pending) can happen only in tiny sets with many concurrent misses; it drops a fill, which re-misses later. Measured at 5 picks in 87,842 L2 misses at 2-way with random traffic — negligible, and reconfiguration drains before switching so it cannot affect a switch.

#### 3.4.6 MSHR handling

The MSHR heap stores completion cycles of outstanding misses:

```python
self.mshr = []   # heap of completion cycles
```

`retire_mshrs(now)` pops completed entries. It also integrates occupancy over time for the monitor:

```python
integral += len(heap) * (done - t)
```

This produces **average MSHR occupancy over the window** as `occ_integral / elapsed_cycles`. The controller's `mshr_occ` signal is this average, not a snapshot. This is correct: a snapshot would jitter, an average is a stable measure.

`drain_time()` returns the maximum of this cache's pending MSHR completions and its downstream's drain time. This is the time until the hierarchy is fully drained — used before reconfiguration.

`set_n_mshr(new_n)` resizes the MSHR limit. Shrinking below current occupancy is fine; new misses are refused until it drains. `reconfig.py` will drain first, so this branch is not exercised at reconfiguration time.

#### 3.4.7 Prefetcher integration

The prefetcher is an object with `observe(addr, pc, hit, now) -> iterable of addresses`. It is trained after each demand access, not after prefetch misses (avoiding feedback loops).

`prefetch(addr, now)`:

1. Retire MSHRs.
2. If the line is already present, count `pf_dropped` and return.
3. If MSHRs available are below `n_mshr - pf_mshr_reserve`, count `pf_dropped` and return. **The reserve is critical:** it prevents prefetches from stealing the last MSHR that a demand miss might need, which would cause a stall right at the moment the prefetcher was supposed to help.
4. Issue the miss. Increment `pf_issued`.

Prefetched lines are marked with `prefetched = True`. When a demand access hits such a line, it is counted as `pf_useful`. If the line is evicted without being used, `pf_unused_evicted` is incremented. `pf_late` counts demand hits on prefetched lines whose fill was still pending.

This yields the four prefetch metrics the lab requires:
- **Accuracy** = `pf_useful / pf_issued`.
- **Coverage** = `pf_useful / demand_misses` (computed by the monitor).
- **Timeliness** = `(pf_useful - pf_late) / pf_useful` (fraction that arrived in time).
- **Traffic** = `memory.prefetch_reads` (from the memory snapshot).

#### 3.4.8 Reconfiguration primitives

Two methods are exposed:

**`flush_invalidate(now)`**:

- Raises if MSHRs are outstanding (reconfiguration must drain first).
- Iterates every set, every line: if valid, counts it; if dirty, writes back downstream and counts it; sets invalid.
- Returns `(n_valid, n_dirty)`, which `reconfig.py` will use to compute the associativity overhead.
- Increments `flushes` and `writebacks_out`.

**`set_assoc(new_assoc)`**:

- Raises if MSHRs are outstanding or if the cache is not empty.
- Calls `_build(new_assoc)`, which resets the set structure to the new geometry.

The contract is: `flush_invalidate` then `set_assoc`. This is what `test_flush_and_set_assoc` verifies. The reconfiguration **stall** is injected by the caller (`reconfig.py`), not by the cache — this keeps the cache's code focused on state, and lets the overhead model be adjusted without touching the cache.

### 3.5 `core.py` — Simplified out-of-order model

The core model is a timing model, not a pipeline model. Its job is to translate the op stream into cycle counts with the right **relative** behavior across configurations.

#### 3.5.1 State

```python
self.cycle = 0
self.slot = 0                        # instructions issued this cycle
self.rob = deque()                   # (seq, done_cycle) of outstanding loads
self.last_load_ready = 0             # data-ready cycle of the previous load
self._seq = 0                        # instruction sequence, never reset
```

The ROB is a deque of `(seq, done_cycle)` pairs. Retire is in-order: the oldest load must complete before subsequent instructions can retire. `last_load_ready` is the completion cycle of the most recent load, used for dependent-load stalling.

#### 3.5.2 Execution loop

For each op:

1. **ROB check.** Pop completed loads from the front. If the oldest outstanding load is `ROB_SIZE` or more instructions older than the current sequence number, stall until its completion (`stall_rob`).
2. **Dependent load stall.** If `dep=True` and `last_load_ready > self.cycle`, stall until ready (`stall_dep`).
3. **Issue to L1.** Call `l1.access()`. If refused, stall until `retry_at` (`stall_mshr`) and retry. The retry loop handles the case where the earliest MSHR completion is still in the future.
4. **Track completion.** For a load with `done - cycle > hit_lat`, push `(seq, done)` onto the ROB. Hits retire immediately (they are within the ROB's window). Stores do not push onto the ROB — they retire through the store buffer.
5. **Issue slot.** Increment `slot`. If `slot >= width`, advance the cycle and reset `slot`.

#### 3.5.3 End of stream

`_finish()`:

1. Count the partly used cycle if `slot > 0`.
2. Wait for all outstanding loads (`stall_end`).
3. Clear the ROB.

This makes the final cycle count the real finish time, not the time of the last op's issue.

#### 3.5.4 Window callback

`run(ops, on_window, window_cycles, ...)` calls `on_window(self, final)` each time a window of at least `window_cycles` has elapsed, and once more with `final=True` at the end. The callback may:

- Inspect counters via `core.snapshot()`.
- Call `core.drain()` to drain MSHRs.
- Return an integer `extra` to charge additional stall cycles to `reconfig_cycles`.

**The return value is how reconfiguration overhead is injected without touching the cache.** `sim.py` will call `reconfig.py`, which will do the flush/rebuild and return the stall cycles; `sim.py` returns that from `on_window`, and the core advances time by that amount. This keeps the cache's code path clean and makes the overhead model explicit and adjustable.

**Window length.** A long stall can make a window longer than `window_cycles`. The monitor must use **elapsed cycles** (from the core's cycle counter difference), not the nominal window length. This is documented in the code and is why the monitor computes `elapsed = cycle_now - cycle_prev`, not `window_cycles`.

#### 3.5.5 Stall accounting

`STALL_KEYS = ("stall_dep", "stall_rob", "stall_mshr", "stall_end")`.

- `stall_dep`: waiting for a dependent load.
- `stall_rob`: waiting for the oldest outstanding load to retire.
- `stall_mshr`: waiting for MSHRs to free.
- `stall_end`: waiting for outstanding loads at end of program.

**Reconfiguration is separate:** `drain_cycles` (from `drain()`) and `reconfig_cycles` (from the `on_window` return value). The monitor's stall fraction will use **`stall_dep + stall_rob + stall_mshr`**, excluding `stall_end` and both reconfiguration counters. This is because `stall_end` is not a workload property (it depends on program length), and reconfiguration is measured separately for the overhead analysis.

`mem_stall_cycles()` sums all four stall keys. The monitor **must not** use this method for stall fraction; it should use the three-key sum directly. This is documented for the monitor's implementation.

### 3.6 `system.py` — Hierarchy construction

`build_system()` constructs the hierarchy in a fixed pattern:

```python
memory = Memory(latency, bus_cycles)
l2 = Cache("L2", ..., downstream=memory, ...)
l1 = Cache("L1", ..., downstream=l2, ...)
core = Core(l1, ...)
```

This single function is used by tests, experiments, and `sim.py`. Centralizing it means there is no risk of one test constructing a different hierarchy than another, and reconfiguration tests can be sure they are working with the same object graph as the dynamic runs.

`snapshot()` returns a full state dict for the monitor:

```python
{
  "cycle": ..., "core": {...}, "l1": {...}, "l2": {...}, "mem": {...}
}
```

`check_invariants()` enforces counter conservation:

- L1 accesses == core loads + stores.
- L1 hits + misses == accesses.
- L2 accesses == L1 misses.
- Memory reads == L2 demand misses.
- Memory prefetch reads == L2 prefetch issued.
- L1 writebacks out == L2 writebacks in.
- Memory writes == L2 writebacks out.

These are not optional. They prove that no access is lost, duplicated, or mis-attributed. If any counter ever disagrees, the simulator has a bug. `check_invariants` is called once at the end of each run, not per window, because in-flight accesses break the invariants mid-window.

---

## 4. Validation suite

`check_base.py` runs 19 tests, each with a stated purpose. Together they prove the simulator's behavior matches the design.

| Test | Purpose | Key evidence |
|---|---|---|
| `test_bandwidth` | Bus serialization is correct | Completion at bus=32 is exactly `(n-1)*32 + latency`; writebacks consume the bus |
| `test_compute_ipc` | Pure-compute IPC equals width | IPC = 4.000 exactly; no memory stall |
| `test_conflict_vs_assoc` | Associativity matters for conflict pattern | 2/4-way miss rate = 1.000; 8-way = 0.000 |
| `test_default_workload_scale` | Default workload phases behave distinctly | `compute`: IPC 4, stall 0; `streaming`: IPC 0.15, stall 1.0; `pointer`: IPC 0, stall 1.0 |
| `test_dependent_chain` | Dependent loads stall; independent do not | Chain: 64,800 cycles; same loads independent: 4,192 cycles |
| `test_drain` | Drain empties all MSHRs | Drain returns nonzero; second drain returns 0 |
| `test_fitting_working_set` | Working set fits L2 after warm-up | L2 miss rate = 0.000 |
| `test_flush_and_set_assoc` | Reconfiguration primitives are correct | Flush with outstanding MSHRs raises; valid+dirty counts match; set_assoc preserves capacity |
| `test_mshr_scaling` | MSHR count affects cycles monotonically | 4: 1,080,024 → 32: 160,208 cycles; bandwidth floor reached |
| `test_pending_hit_merges` | Second request merges into in-flight fill | `late_hits = 1`; memory reads = 1 for two requests |
| `test_prefetch_hook` | Prefetcher reduces cycles on sequential, not random | Sequential: 239,623 → 80,035 cycles; accuracy 1.00 vs 0.00 |
| `test_refused_access_changes_nothing` | Refused access does not change state | All counters except `blocked` are unchanged; retry succeeds |
| `test_rob_limit` | Small ROB throttles MLP | ROB=4: 432,024 cycles; ROB=128: 108,120 cycles |
| `test_sequential_scan` | Full-scan miss rate is 1.0 in L2 | Every line is a cold miss |
| `test_window_callback` | Window callback fires at correct intervals | Windows at 1000, 2000, ..., final at run end; reconfiguration stall adds to cycle count |
| `test_workload_properties` | Workload is deterministic, correct, and consistent | Identical streams across iterations; correct footprints; boundaries match; conflict pattern consistent |
| `test_writebacks` | Dirty evictions produce writebacks | Store scan: 12,288 memory writes (matches expected range) |
| `test_mshr_saturation_slow_bus` (added) | Bus saturation makes MSHRs stop helping | At bus=16, 16→32 MSHRs gives 1.00x |
| `test_compute_mshr_insensitivity` (added) | Compute phase is MSHR-insensitive | 4 vs 32 MSHRs: 25,375 vs 25,371 cycles |

**What these prove for the controller:**

- Rule B (enable next-line on sequential) is validated by `test_prefetch_hook` — 1.5× speedup on streaming.
- Rule E (increase MSHRs when occupancy high and bandwidth not saturated) is validated by `test_mshr_scaling` — monotone improvement, bandwidth floor at 32.
- Rule E's negative case (don't increase when saturated) is validated by `test_mshr_saturation_slow_bus` — 1.00× at bus=16.
- Rule F (decrease MSHRs on compute) is validated by `test_compute_mshr_insensitivity` — 0.02% difference.
- Rule G (change associativity on conflict) is validated by `test_conflict_vs_assoc` — 1.000 → 0.000 miss rate.
- Rule A (turn off prefetch on pointer) is validated by `test_dependent_chain` — pointer is pure-latency-bound; prefetch cannot help.

The one rule not yet validated is Rule C (stride prefetch), because `prefetch.py` is not yet written. This is the next module.

---

## 5. Design decisions and their justification

This section records the reasoning behind every non-obvious choice.

**Why a simplified core instead of a cycle-accurate pipeline?** A cycle-accurate core (O3CPU in gem5, for example) models branch prediction, issue port conflicts, execution unit latencies, and store-to-load forwarding. None of these change the controller's decision variables. What changes the decision is: does a phase stall on memory, how many misses can overlap, and does prefetching hide latency. The simplified core models exactly those three. The cost of a cycle-accurate core would be a 10–100× slowdown and a large increase in code complexity, with no change to the conclusions about the controller.

**Why "refused access changes nothing"?** If a refused access incremented `accesses`, then MSHR pressure would look like additional traffic, and the miss rate would depend on MSHR count — which is a feedback loop that makes the "MSHRs help" measurement circular. Refusing cleanly means traffic is a property of the workload, not of the MSHR configuration.

**Why pending-fill lines are installed immediately with `ready`?** Real caches install the tag as soon as the miss is issued, then merge subsequent requests. Installing immediately is what makes `late_hits` possible and what makes the L1 behave like a real L1 under a streaming workload. The alternative — install on fill — would undercount hits and overcount misses, because the merge case would become a miss.

**Why `pf_mshr_reserve`?** Without a reserve, a prefetcher with a high hit rate could use every MSHR for prefetches, leaving none for demand misses, causing stalls. The reserve guarantees that demand misses always have MSHRs available. This is a real design feature of many hardware prefetchers.

**Why counter diffs instead of resets?** Resets lose in-flight state. If the monitor reset `accesses` at the start of each window, a miss in flight at the boundary could be counted in the next window's misses but the current window's accesses, producing a miss rate greater than 1. Diffs avoid this because `accesses`, `misses`, and `hits` are monotone; the difference between two snapshots is always the increment during that window.

**Why a static `bus_util`?** It is a pure function of two counters. Making it a method would require a `Memory` object, which `monitor.py` will have, but a static method makes it clear that the computation has no hidden state.

**Why `flush_invalidate` returns `(n_valid, n_dirty)`?** These are exactly the two numbers `reconfig.py` needs to compute associativity overhead: `n_valid` gives the number of lines to remap (if you preserve lines) or to consider lost (if you flush), and `n_dirty` gives the extra writeback cost. Making the function return them means `reconfig.py` does not need to re-walk the cache.

**Why the reconfiguration stall is injected by the caller and not by the cache?** The cache's job is state; the caller's job is time. Separating them means the reconfiguration overhead model can be changed (for a sensitivity sweep, or a different hardware assumption) without touching the cache's correctness code. It also means the same `flush_invalidate`/`set_assoc` primitives can be used in baseline runs where no stall should be charged.

---

## 6. Known limitations and their justification

**Stall fraction in streaming is 1.00.** This is not a bug. A `compute_per_mem = 1` streaming workload issues one memory op and one compute op per "iter"; the core is memory-throughput-limited, so IPC is near memory throughput and stall is 1.00. If an experiment needs an intermediate stall fraction, pass `params={"streaming": {"compute_per_mem": 4}}` (or higher) to that run. This is documented in `sim_config.py`.

**Pointer phase has IPC 0.00.** Correct. Every load depends on the previous, so nothing overlaps. Prefetching cannot help. This is exactly what makes the pointer phase useful for testing Rule A (turn off prefetch).

**Two pending fills in a 2-way set can drop one fill.** Measured at 5 in 87,842 misses. The dropped fill re-misses later. This is a performance cost, not a correctness bug, and it cannot affect a reconfiguration (drain empties MSHRs first). Documented in `_victim`'s docstring.

**No pollution case on pure streaming.** A sequential stream never pollutes; every prefetch is useful. The pollution case requires a workload with a reusable working set near L2 size, which is the `mixed` phase. This is deferred to `prefetch.py` and its tests.

**MSHR saturation at default bus only at the top of the ladder.** At `bus_cycles = 8`, 32 MSHRs already reach the bandwidth floor. The negative case is available as a knob (`bus_cycles = 16`), not baked into the default config. This is a deliberate choice: the default config should be a "well-configured machine" so the controller's job is to make good choices on top of it, not to compensate for a badly configured baseline.

**No L2-to-L3 hierarchy.** The L2 is the last-level cache; misses go to memory. This is a simplification. The controller operates on the L2 as the adaptive cache; an L3 would add a level to reason about without changing the controller's logic. If a future extension needs an L3, `Cache` can be layered — the downstream interface already supports it.

---

## 7. What the base simulator establishes for the project

The base simulator is now ready to support the following modules:

- **`prefetch.py`**: implements next-line and stride prefetchers conforming to the `observe(addr, pc, hit, now) -> iterable of addresses` interface. The cache already calls this.
- **`monitor.py`**: reads `snapshot()` diffs from `system.py`, computes `sequential_score`, `stride_confidence`, `shadow_est[a]`, `stall_frac`, `bw_util`, `mshr_occ`, and emits a feature vector per window.
- **`controller.py`**: implements the 9 rules A–I on top of the feature vectors. Uses `HORIZON_H` for the utility function.
- **`reconfig.py`**: calls `cache.flush_invalidate(now)` and `cache.set_assoc(new_assoc)` for associativity; `cache.set_n_mshr(new_n)` for MSHRs; calls `core.drain()` before each; returns the stall cycles for `sim.py` to charge.
- **`sim.py`**: implements the adaptive loop. Calls `core.run(ops, on_window=loop_callback)` where the callback runs the monitor, then the controller, then (if a switch is requested) the reconfigurator, and returns the stall to charge.
- **`experiments.py`**: runs baseline, oracle, and dynamic modes; runs break-even sweeps; runs the negative cases.
- **`plots.py`**: generates the report's figures.

Every hook these modules need already exists:

| Module needs | Provided by |
|---|---|
| Feature vector inputs | `system.snapshot()`, `cache.snapshot()`, `memory.snapshot()`, `core.snapshot()` |
| Per-window invocation | `core.run(on_window=...)` |
| Reconfiguration primitives | `cache.flush_invalidate`, `cache.set_assoc`, `cache.set_n_mshr` |
| Drain | `core.drain()` |
| Overhead injection | `on_window` return value → `core.reconfig_cycles` |
| Counter conservation | `system.check_invariants()` (end of run) |
| Deterministic workload | `workload.ops()` (replayable) |
| Detection scoring | `workload.write_boundaries()` (separate file) |
| Bandwidth-limited negative case | `Memory(bus_cycles=16)` in `build_system` |

---

## 8. Where the project goes next

The remaining modules, in order:

1. **`prefetch.py`** — next-line and stride prefetchers. Add three validation tests to `check_base.py`: stride helps `strided` ≥20% and not `streaming`, pollution case on `mixed` with reduced footprint, and prefetch does not help `pointer`.
2. **`monitor.py`** — window sampling, `sequential_score`, `stride_confidence`, `shadow_est`, stall fraction excluding `stall_end`, bus utilization.
3. **`controller.py`** — the nine rules, gated by hysteresis, residency, and utility.
4. **`reconfig.py`** — flush, rebuild, or resize, with stall accounting.
5. **`sim.py`** — the main loop with `--mode baseline | oracle | dynamic`.
6. **`experiments.py`** — baseline, oracle, dynamic, break-even sweep, negative cases.
7. **`plots.py`** — figures for the report.

The base simulator has been validated against all the behaviors the controller will rely on. The remaining work is at the adaptation layer — the part that makes the system "self-evolving" — and the infrastructure for it is in place.