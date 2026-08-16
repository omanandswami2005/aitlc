import os
from pathlib import Path

import pytest
from aitlc.core import locks


def test_acquire_and_release(tmp_path: Path):
    lock = locks.TestLock(test_id="PROJ-1", lock_dir=tmp_path)
    lock.acquire()
    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()


def test_second_acquire_while_live_process_holds_raises(tmp_path: Path):
    lock_dir = tmp_path
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / "PROJ-1.lock"
    # Write our own pid — we are definitely alive, simulating a held lock.
    lock_path.write_text(str(os.getpid()))

    second = locks.TestLock(test_id="PROJ-1", lock_dir=lock_dir)
    with pytest.raises(locks.LockHeldError):
        second.acquire()


def test_stale_lock_from_dead_process_is_reclaimed(tmp_path: Path):
    lock_dir = tmp_path
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / "PROJ-1.lock"
    # A PID essentially guaranteed not to be alive.
    lock_path.write_text("999999")

    lock = locks.TestLock(test_id="PROJ-1", lock_dir=lock_dir)
    lock.acquire()  # should not raise — stale lock reclaimed
    assert lock_path.read_text() == str(os.getpid())


def test_context_manager_releases_on_exception(tmp_path: Path):
    test_id = "PROJ-1"
    try:
        with locks.held(test_id, tmp_path):
            raise ValueError("boom")
    except ValueError:
        pass
    lock = locks.TestLock(test_id=test_id, lock_dir=tmp_path)
    assert not lock.path.exists()


def test_different_test_ids_dont_collide(tmp_path: Path):
    lock_a = locks.TestLock(test_id="PROJ-1", lock_dir=tmp_path)
    lock_b = locks.TestLock(test_id="PROJ-2", lock_dir=tmp_path)
    lock_a.acquire()
    lock_b.acquire()  # should not raise
    lock_a.release()
    lock_b.release()
