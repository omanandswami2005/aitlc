"""A reused debug profile should announce itself before it costs a run.

A CDP attach reuses an existing browser context, so a long-lived profile keeps
its sessions. Once it holds a stale one, the run dies at the project's own
login step -- which reads as a test bug, and cost two wasted runs before the
cause was found.
"""

from __future__ import annotations

from aitlc.core import chrome_cdp


def _save(tmp_path, port=9333, driver=""):
    instance = chrome_cdp.CdpInstance(
        pid=1234,
        port=port,
        user_data_dir=str(tmp_path / "profile"),
        started_at=0.0,
        last_driven_by=driver,
    )
    chrome_cdp.save_state(tmp_path, instance)
    return instance


def test_a_fresh_profile_is_not_dirty(tmp_path):
    _save(tmp_path)
    dirty, why = chrome_cdp.is_dirty_for(tmp_path, 9333, "PROJ-1")
    assert dirty is False and why == ""


def test_a_profile_driven_by_another_test_is_flagged(tmp_path):
    _save(tmp_path, driver="PROJ-2")
    dirty, why = chrome_cdp.is_dirty_for(tmp_path, 9333, "PROJ-1")
    assert dirty is True
    assert "PROJ-2" in why
    assert "--new" in why  # points at the fix, not just the problem


def test_the_same_test_reattaching_is_fine(tmp_path):
    _save(tmp_path, driver="PROJ-1")
    dirty, _ = chrome_cdp.is_dirty_for(tmp_path, 9333, "PROJ-1")
    assert dirty is False


def test_mark_driven_records_who_and_how_often(tmp_path):
    _save(tmp_path)
    chrome_cdp.mark_driven(tmp_path, 9333, "PROJ-1")
    instance = chrome_cdp.mark_driven(tmp_path, 9333, "PROJ-1")
    assert instance is not None
    assert instance.last_driven_by == "PROJ-1"
    assert instance.driven_count == 2


def test_no_state_means_no_opinion(tmp_path):
    assert chrome_cdp.is_dirty_for(tmp_path, 9999, "PROJ-1") == (False, "")
    assert chrome_cdp.mark_driven(tmp_path, 9999, "PROJ-1") is None
