"""Shared-memory control block between the supervisor and each worker.

One lock-free double array per worker. The supervisor writes targets, the
worker writes heartbeats and telemetry. Deliberately tiny and allocation-free
so it stays reliable even when the machine is completely saturated.

Nothing here uses a lock, and that is load-bearing. `multiprocessing.Event`
and friends are built on POSIX semaphores in shared memory: SIGKILL a process
while it holds one and every other process blocks on that futex forever. This
program kills its own workers on purpose, so all cross-process state is plain
words in shared memory that a dead process cannot possibly be holding.
"""
from __future__ import annotations

import multiprocessing as mp
import time

SLOT_TARGET = 0      # supervisor -> worker: desired intensity (meaning varies)
SLOT_HEARTBEAT = 1   # worker -> supervisor: monotonic timestamp
SLOT_A = 2           # worker -> supervisor: primary telemetry
SLOT_B = 3           # worker -> supervisor: secondary telemetry
SLOT_PANIC = 4       # supervisor -> worker: 1 = release resources NOW
SLOT_HINT = 5        # supervisor -> worker: free-form behaviour hint
SLOT_C = 6
SLOT_D = 7
N_SLOTS = 8


def new_block():
    return mp.Array("d", N_SLOTS, lock=False)


F_STOP = 0           # 1 = everybody shut down
F_SUPERVISOR_HB = 1  # supervisor liveness, for orphan detection
F_SLOTS = 4


class Flags:
    """Process-wide, lock-free stop flag shared through fork()."""

    __slots__ = ("block",)

    def __init__(self, block=None):
        self.block = block if block is not None else mp.Array("d", F_SLOTS, lock=False)

    def set_stop(self) -> None:
        self.block[F_STOP] = 1.0

    @property
    def stopping(self) -> bool:
        return self.block[F_STOP] >= 0.5

    def beat(self) -> None:
        self.block[F_SUPERVISOR_HB] = time.monotonic()

    def supervisor_age(self) -> float:
        hb = self.block[F_SUPERVISOR_HB]
        return time.monotonic() - hb if hb > 0 else 0.0


class Ctl:
    """Worker-side view of one control block."""

    __slots__ = ("block", "flags", "kind", "index", "_last_beat", "_local_stop")

    def __init__(self, block, flags: "Flags", kind: str, index: int):
        self.block = block
        self.flags = flags
        self._local_stop = False
        self.kind = kind
        self.index = index
        self._last_beat = 0.0

    # -- supervisor -> worker -------------------------------------------
    @property
    def target(self) -> float:
        return self.block[SLOT_TARGET]

    @property
    def panic(self) -> bool:
        return self.block[SLOT_PANIC] >= 0.5

    @property
    def hint(self) -> float:
        return self.block[SLOT_HINT]

    def request_stop(self) -> None:
        """Local (in-process) stop, e.g. from our own SIGTERM handler."""
        self._local_stop = True

    def should_stop(self) -> bool:
        if self._local_stop:
            return True
        # An orphaned worker exits by itself even if PDEATHSIG was missed.
        if self.flags.supervisor_age() > 120.0:
            return True
        return self.flags.stopping

    # -- worker -> supervisor -------------------------------------------
    def beat(self, a: float | None = None, b: float | None = None,
             force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_beat >= 0.2:
            self._last_beat = now
            self.block[SLOT_HEARTBEAT] = now
        if a is not None:
            self.block[SLOT_A] = a
        if b is not None:
            self.block[SLOT_B] = b

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep: polls the lock-free flag, so shutdown is
        noticed within ~40ms without any shared lock being involved."""
        end = time.monotonic() + seconds
        while seconds > 0:
            time.sleep(min(0.04, seconds))
            if self.should_stop():
                return
            seconds = end - time.monotonic()
