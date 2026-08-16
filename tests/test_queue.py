import os
from pathlib import Path

import pytest
from aitlc.adapters.lambdatest.queue import NoSlotAvailableError, RemoteSlotPool, held


def test_acquires_first_free_slot(tmp_path: Path):
    pool = RemoteSlotPool(max_concurrent=3, slot_dir=tmp_path)
    slot = pool.acquire()
    assert slot.index == 0
    slot.release()


def test_fills_slots_in_order_and_blocks_when_full(tmp_path: Path):
    pool = RemoteSlotPool(max_concurrent=2, slot_dir=tmp_path)
    slot_a = pool.acquire()
    slot_b = pool.acquire()
    assert {slot_a.index, slot_b.index} == {0, 1}

    with pytest.raises(NoSlotAvailableError):
        pool.acquire()

    slot_a.release()
    slot_c = pool.acquire()
    assert slot_c.index == 0  # freed slot reused


def test_stale_slot_from_dead_process_is_reclaimed(tmp_path: Path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "slot-0.lock").write_text("999999")  # not alive
    pool = RemoteSlotPool(max_concurrent=1, slot_dir=tmp_path)
    slot = pool.acquire()  # should not raise
    assert slot.index == 0
    assert tmp_path.joinpath("slot-0.lock").read_text() == str(os.getpid())


def test_live_holder_blocks_all_slots(tmp_path: Path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "slot-0.lock").write_text(str(os.getpid()))  # genuinely alive
    pool = RemoteSlotPool(max_concurrent=1, slot_dir=tmp_path)
    with pytest.raises(NoSlotAvailableError):
        pool.acquire()


def test_wait_and_acquire_times_out_when_never_free(tmp_path: Path):
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "slot-0.lock").write_text(str(os.getpid()))
    pool = RemoteSlotPool(max_concurrent=1, slot_dir=tmp_path)
    with pytest.raises(NoSlotAvailableError):
        pool.wait_and_acquire(timeout_s=0.2, poll_interval_s=0.05)


def test_context_manager_releases_on_exit(tmp_path: Path):
    pool = RemoteSlotPool(max_concurrent=1, slot_dir=tmp_path)
    with held(pool) as slot:
        assert slot.path.exists()
    assert not slot.path.exists()


def test_context_manager_releases_on_exception(tmp_path: Path):
    pool = RemoteSlotPool(max_concurrent=1, slot_dir=tmp_path)
    try:
        with held(pool) as slot:
            raise ValueError("boom")
    except ValueError:
        pass
    assert not slot.path.exists()
