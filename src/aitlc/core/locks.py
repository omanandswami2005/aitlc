"""Per-test-ID concurrency lock (FR-1.6).

A real mistake this
tool is built from — the same test launched twice at once (a solo run plus
the same test inside a sequential batch reaching it moments later), sharing
session state and hardcoded example-table data. Caught by luck (a second log
file was noticed growing), not by design. This closes that gap.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Return True if a process with this PID exists (used to reclaim stale lock/slot files left by a crashed process).

    Shared between TestLock and RemoteSlotPool (queue.py) rather than duplicated.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by someone else — treat as alive.
        return True
    return True


class LockHeldError(RuntimeError):
    """Raised when a test ID's lock is already held by another process."""

    def __init__(self, test_id: str, lock_path: Path, holder_pid: int | None):
        """Record which test is locked and by whom."""
        self.test_id = test_id
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        holder = f"pid {holder_pid}" if holder_pid else "another process"
        super().__init__(
            f"Test '{test_id}' already has a run in progress ({holder}). "
            f"Lock file: {lock_path}"
        )


@dataclass
class TestLock:
    """A lockfile at reports/.locks/<TEST-ID>.lock, holding the locking PID."""

    test_id: str
    lock_dir: Path

    @property
    def path(self) -> Path:
        """The lock file backing this lock."""
        safe_id = self.test_id.replace("/", "_")
        return self.lock_dir / f"{safe_id}.lock"

    def _read_holder_pid(self) -> int | None:
        try:
            content = self.path.read_text().strip()
            return int(content) if content else None
        except (FileNotFoundError, ValueError):
            return None

    def acquire(self) -> None:
        """Acquire the lock, raising LockHeldError if another live process holds it.

        A lock file left behind by a process that's no longer running (a
        crash, a kill -9) is treated as stale and silently reclaimed —
        otherwise a single crashed run would permanently block that test ID.
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        existing_pid = self._read_holder_pid()
        if existing_pid is not None and pid_alive(existing_pid):
            raise LockHeldError(self.test_id, self.path, existing_pid)
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        """Release the lock, but only if this process still holds it."""
        try:
            if self._read_holder_pid() == os.getpid():
                self.path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def held(test_id: str, lock_dir: Path) -> Iterator[TestLock]:
    """Context manager: acquire the lock for test_id, release on exit."""
    lock = TestLock(test_id=test_id, lock_dir=lock_dir)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


def wait_and_acquire(
    test_id: str, lock_dir: Path, timeout_s: float, poll_interval_s: float = 1.0
) -> TestLock:
    """Block until the lock is free (or timeout_s elapses), then acquire it.

    Used by the --queue-behind-existing path (SRS FR-1.6's "refuses or
    queues" — this implements "queues").
    """
    lock = TestLock(test_id=test_id, lock_dir=lock_dir)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock.acquire()
            return lock
        except LockHeldError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll_interval_s)
