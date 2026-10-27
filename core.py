"""
core.py — Simplified out-of-order core model.

SIMPLIFIED. Say so in the report: this is a timing model, not a pipeline.

What it models
  * Issue: up to CORE_WIDTH instructions per cycle (compute, load, store).
  * Loads never block the core on a hit or a miss by themselves. A load's
    data is ready at the completion cycle the cache returns. Independent
    loads keep issuing, so misses overlap (memory-level parallelism).
  * ROB limit: the core stalls when the oldest outstanding load is
    ROB_SIZE or more instructions older than the one being issued. Retire is
    in order, so the oldest unfinished load is what blocks.
  * Dependent ops: `dep` is 0 (independent) or k >= 1, meaning "my address
    comes from the data of the k-th most recent LOAD" (k=1: the previous load;
    True counts as 1). The op cannot issue until that load's data has arrived.
    Stores and compute ops are not loads and are not counted. This lets a
    pointer chase run beside a stream: the chase load points back past the
    stream loads to its own previous hop. The core remembers the last
    MAX_DEP_DISTANCE loads; a larger k is an error, and a k reaching back
    before the first load of the run simply finds nothing to wait for.
  * Stores retire through a store buffer: the core does not wait for their
    data, but a store that finds the MSHRs full must wait to be accepted.
  * MSHR full (L1 or L2): the access is refused; the core waits until the
    earliest MSHR frees and retries.
  * At the end of the op stream the core waits for outstanding loads, so
    the final cycle count is the real finish time.

What it does not model: branch prediction, instruction latencies other than
loads, store-to-load forwarding, port limits, a real front end.

Stall cycles are cycles in which the core advanced time only because it was
waiting: stall_dep, stall_rob, stall_mshr, stall_end. Cycles spent in a
reconfiguration are counted separately (drain_cycles via drain(),
reconfig_cycles via charge() or the on_window return value) so overhead is
never hidden inside IPC or the memory-stall fraction.

Counters are cumulative; the monitor diffs snapshots.
"""

import collections

import sim_config as cfg
from workload import OP_COMPUTE, OP_STORE

STALL_KEYS = ("stall_dep", "stall_rob", "stall_mshr", "stall_end")


class Core:
    def __init__(self, l1, width=None, rob_size=None):
        self.l1 = l1
        self.width = cfg.CORE_WIDTH if width is None else width
        self.rob_size = cfg.ROB_SIZE if rob_size is None else rob_size
        if self.width < 1 or self.rob_size < 1:
            raise ValueError("width and rob_size must be >= 1")

        self.cycle = 0
        self.slot = 0                       # instructions issued in the current cycle
        self.rob = collections.deque()      # (seq, done_cycle) of outstanding loads, oldest first
        # data-ready cycle of the most recent loads, newest last
        self._load_ready = collections.deque(maxlen=cfg.MAX_DEP_DISTANCE)
        self._seq = 0                       # instruction sequence number, never reset
        self._seq_base = 0
        self.stats = {}
        self.reset_counters()

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------
    def reset_counters(self):
        self._seq_base = self._seq
        self.stats = dict.fromkeys(("loads", "stores") + STALL_KEYS
                                   + ("drain_cycles", "reconfig_cycles"), 0)

    def snapshot(self):
        d = dict(self.stats)
        d["instructions"] = self._seq - self._seq_base
        d["cycle"] = self.cycle
        return d

    def mem_stall_cycles(self):
        return sum(self.stats[k] for k in STALL_KEYS)

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------
    def _stall_until(self, t, key):
        if t > self.cycle:
            if key is not None:
                self.stats[key] += t - self.cycle
            self.cycle = t
            self.slot = 0

    def charge(self, cycles):
        """Advance the clock by `cycles` of reconfiguration overhead (not a
        memory stall). reconfig.py uses this; returns the cycles charged."""
        if cycles < 0:
            raise ValueError("cannot charge negative cycles")
        if cycles:
            self.cycle += cycles
            self.slot = 0
            self.stats["reconfig_cycles"] += cycles
        return cycles

    def drain(self):
        """Wait for every outstanding load and every outstanding miss in the
        hierarchy, then clear the ROB. Returns the cycles spent. Used by
        reconfig.py before changing associativity or the MSHR count."""
        start = self.cycle
        target = self.cycle
        for _, done in self.rob:
            if done > target:
                target = done
        target = max(target, self.l1.drain_time())
        self._stall_until(target, None)
        self.rob.clear()
        self.l1.retire_all(self.cycle)
        spent = self.cycle - start
        self.stats["drain_cycles"] += spent
        return spent

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def run(self, ops, on_window=None, window_cycles=None,
            max_cycles=None, max_ops=None):
        """
        Execute ops until the stream ends (or max_cycles / max_ops is hit).

        on_window(core, final) is called each time a window of at least
        window_cycles has elapsed (a long stall can make a window longer, so
        the monitor must use elapsed cycles, not the nominal window length),
        and once more with final=True when the run ends. During the call the
        callback may inspect or reconfigure the system (reconfig.py does its
        own drain() and charge()) and may also return extra stall cycles (int),
        charged to reconfig_cycles. The return value of the final call is
        ignored.

        Returns the cycle count at the end of the run.
        """
        window_cycles = window_cycles or cfg.WINDOW_CYCLES
        next_window = self.cycle + window_cycles
        l1 = self.l1
        rob = self.rob
        width = self.width
        rob_size = self.rob_size
        st = self.stats
        hit_lat = l1.latency
        hist = self._load_ready
        max_dep = hist.maxlen
        n_run = 0
        exhausted = True

        for kind, addr, dep, pc in ops:
            seq = self._seq

            # ROB: retire finished loads; stall if the oldest is too old.
            while rob:
                head_seq, head_done = rob[0]
                if head_done <= self.cycle:
                    rob.popleft()
                elif seq - head_seq >= rob_size:
                    self._stall_until(head_done, "stall_rob")
                    rob.popleft()
                else:
                    break

            if kind != OP_COMPUTE:
                if dep:
                    if dep > max_dep:
                        raise ValueError(f"dependency distance {dep} exceeds "
                                         f"MAX_DEP_DISTANCE={max_dep}")
                    if dep <= len(hist) and hist[-dep] > self.cycle:
                        self._stall_until(hist[-dep], "stall_dep")
                is_write = kind == OP_STORE
                while True:
                    res = l1.access(addr, self.cycle, is_write, False, pc)
                    if res.completion is not None:
                        break
                    if res.retry_at is None or res.retry_at <= self.cycle:
                        raise RuntimeError("refused access with no usable retry time")
                    self._stall_until(res.retry_at, "stall_mshr")
                if is_write:
                    st["stores"] += 1
                else:
                    st["loads"] += 1
                    done = res.completion
                    hist.append(done)
                    if done - self.cycle > hit_lat:
                        rob.append((seq, done))

            self._seq = seq + 1
            self.slot += 1
            if self.slot >= width:
                self.slot = 0
                self.cycle += 1
            n_run += 1

            if self.cycle >= next_window:
                if on_window is not None:
                    extra = on_window(self, False) or 0
                    if extra > 0:
                        self.cycle += extra
                        self.slot = 0
                        st["reconfig_cycles"] += extra
                next_window = self.cycle + window_cycles

            if ((max_cycles is not None and self.cycle >= max_cycles)
                    or (max_ops is not None and n_run >= max_ops)):
                exhausted = False
                break

        if exhausted:
            self._finish()
        if on_window is not None:
            on_window(self, True)
        return self.cycle

    def _finish(self):
        """End of program: count the partly used cycle and wait for loads."""
        if self.slot:
            self.slot = 0
            self.cycle += 1
        tail = self.cycle
        for _, done in self.rob:
            if done > tail:
                tail = done
        self._stall_until(tail, "stall_end")
        self.rob.clear()
