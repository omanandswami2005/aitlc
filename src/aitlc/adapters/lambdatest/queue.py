"""Remote concurrency-aware slot pool (FR-8).

The single biggest mistake in the sessions that motivated this was launching
~15 remote sessions at once against an account with a real ceiling of ~5 —
10 failed before even reaching login. This is a counting-semaphore version
of core/locks.py's per-test-ID exclusive lock: N numbered slot files instead
of one, so N separate `aitlc run --remote` process invocations can
coordinate a shared ceiling without a long-running daemon.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from aitlc.core.locks import pid_alive


class NoSlotAvailableError(RuntimeError):
    """Raised when every remote session slot is already taken."""

    def __init__(self, max_concurrent: int, slot_dir: Path):
        """Record how many slots exist and where they are tracked."""
        self.max_concurrent = max_concurrent
        self.slot_dir = slot_dir
        super().__init__(
            f"All {max_concurrent} remote slots are in use. Slot dir: {slot_dir}"
        )


@dataclass
class RemoteSlotPool:
    """N numbered slots at slot_dir/slot-<i>.lock, each holding a PID."""

    max_concurrent: int
    slot_dir: Path

    def _slot_path(self, index: int) -> Path:
        return self.slot_dir / f"slot-{index}.lock"

    def _try_acquire_one(self) -> AcquiredSlot | None:
        self.slot_dir.mkdir(parents=True, exist_ok=True)
        for i in range(self.max_concurrent):
            path = self._slot_path(i)
            try:
                holder_pid = int(path.read_text().strip()) if path.exists() else None
            except ValueError:
                holder_pid = None

            if holder_pid is not None and pid_alive(holder_pid):
                continue  # slot genuinely in use

            # Free, or held by a dead process — reclaim it.
            path.write_text(str(os.getpid()))
            return AcquiredSlot(index=i, path=path)
        return None

    def acquire(self) -> AcquiredSlot:
        """Claim a free slot, or raise NoSlotAvailableError if none is left."""
        slot = self._try_acquire_one()
        if slot is None:
            raise NoSlotAvailableError(self.max_concurrent, self.slot_dir)
        return slot

    def wait_and_acquire(
        self, timeout_s: float, poll_interval_s: float = 5.0
    ) -> AcquiredSlot:
        """Queue behind existing runs until a slot frees up (or timeout)."""
        deadline = time.monotonic() + timeout_s
        while True:
            slot = self._try_acquire_one()
            if slot is not None:
                return slot
            if time.monotonic() >= deadline:
                raise NoSlotAvailableError(self.max_concurrent, self.slot_dir)
            time.sleep(poll_interval_s)


@dataclass
class AcquiredSlot:
    """A held concurrency slot, released by its owning process."""

    index: int
    path: Path

    def release(self) -> None:
        """Free the slot, but only if this process still owns it."""
        try:
            if int(self.path.read_text().strip()) == os.getpid():
                self.path.unlink()
        except (FileNotFoundError, ValueError):
            pass


@contextmanager
def held(
    pool: RemoteSlotPool, *, wait_timeout_s: float | None = None
) -> Iterator[AcquiredSlot]:
    """Acquire a slot (queueing if wait_timeout_s is given), release on exit."""
    slot = (
        pool.wait_and_acquire(wait_timeout_s)
        if wait_timeout_s is not None
        else pool.acquire()
    )
    try:
        yield slot
    finally:
        slot.release()
