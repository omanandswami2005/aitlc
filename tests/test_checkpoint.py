"""Snapshotting an expensive setup, and refusing to restore a dead one.

The value of a checkpoint is entirely in whether the thing it hands back
still works. A snapshot that restores a expired session is worse than no
snapshot, because it fails later and somewhere else.
"""

from __future__ import annotations

import time

import pytest
from aitlc.core import checkpoint, workspace

STATE = {
    "cookies": [{"name": "session", "value": "tok", "domain": "app.example", "path": "/"}],
    "origins": [{"origin": "https://app.example", "localStorage": [{"name": "u", "value": "x"}]}],
}


@pytest.fixture(autouse=True)
def _workspace():
    workspace.set_workspace(None)
    workspace.set_config_default(None)
    yield
    workspace.set_workspace(None)
    workspace.set_config_default(None)


def _make(name="setup", **kwargs):
    return checkpoint.Checkpoint(
        name=name,
        test_id="PROJ-1",
        feature="f.feature",
        step_index=86,
        storage_state=STATE,
        run_values={"random_name": "abcxyz"},
        created_entities=[{"user": "a@example.com"}],
        **kwargs,
    )


class TestRoundTrip:
    def test_a_checkpoint_survives_save_and_load(self, tmp_path):
        checkpoint.save(tmp_path, _make())
        loaded = checkpoint.load(tmp_path, "setup")
        assert loaded.step_index == 86
        assert loaded.storage_state["cookies"][0]["value"] == "tok"

    def test_run_scoped_values_are_kept(self, tmp_path):
        """Without these a restored session hunts for a name that never existed."""
        checkpoint.save(tmp_path, _make())
        assert checkpoint.load(tmp_path, "setup").run_values["random_name"] == "abcxyz"

    def test_created_entities_are_kept_so_they_can_be_reused(self, tmp_path):
        checkpoint.save(tmp_path, _make())
        assert checkpoint.load(tmp_path, "setup").created_entities == [
            {"user": "a@example.com"}
        ]

    def test_a_missing_checkpoint_is_none_not_an_error(self, tmp_path):
        assert checkpoint.load(tmp_path, "nope") is None

    def test_a_corrupt_checkpoint_is_reported_not_silently_empty(self, tmp_path):
        path = checkpoint.save(tmp_path, _make())
        path.write_text("{not json")
        with pytest.raises(checkpoint.CheckpointError):
            checkpoint.load(tmp_path, "setup")

    def test_it_lands_in_the_workspace(self, tmp_path):
        workspace.set_workspace("PROJ-9")
        path = checkpoint.save(tmp_path, _make())
        assert "PROJ-9" in str(path)


class TestStaleness:
    """A dead session restored silently is the failure this must prevent."""

    def test_a_fresh_checkpoint_is_usable(self, tmp_path):
        assert _make(created_at=time.time()).is_stale(3600) is False

    def test_an_old_checkpoint_is_stale(self, tmp_path):
        assert _make(created_at=time.time() - 7200).is_stale(3600) is True

    def test_age_is_reported_so_a_human_can_judge(self, tmp_path):
        record = _make(created_at=time.time() - 120)
        assert 110 < record.age_seconds() < 130

    def test_saving_stamps_the_time_when_absent(self, tmp_path):
        checkpoint.save(tmp_path, _make())
        assert checkpoint.load(tmp_path, "setup").created_at > 0


class TestListing:
    def test_checkpoints_are_listed_newest_first(self, tmp_path):
        checkpoint.save(tmp_path, _make("older", created_at=time.time() - 500))
        checkpoint.save(tmp_path, _make("newer", created_at=time.time()))
        assert [c.name for c in checkpoint.list_all(tmp_path)] == ["newer", "older"]

    def test_the_summary_omits_the_payload(self, tmp_path):
        """A listing should be readable; storage state is not."""
        summary = _make(created_at=time.time()).summary()
        assert summary["cookies"] == 1
        assert "storage_state" not in summary

    def test_deleting_reports_whether_anything_went(self, tmp_path):
        checkpoint.save(tmp_path, _make())
        assert checkpoint.delete(tmp_path, "setup") is True
        assert checkpoint.delete(tmp_path, "setup") is False

    def test_a_corrupt_file_does_not_break_the_listing(self, tmp_path):
        checkpoint.save(tmp_path, _make("good", created_at=time.time()))
        bad = checkpoint.save(tmp_path, _make("bad", created_at=time.time()))
        bad.write_text("{not json")
        assert [c.name for c in checkpoint.list_all(tmp_path)] == ["good"]


class TestRestoreHonesty:
    """A half-restore reported as complete is how a checkpoint becomes untrusted."""

    class _Ctx:
        def __init__(self):
            self.added = []

        def add_cookies(self, cookies):
            self.added.extend(cookies)

    def _wire(self, monkeypatch, context):
        browser = type("B", (), {"contexts": [context]})()
        playwright = type(
            "P", (), {"chromium": type("C", (), {"connect_over_cdp": staticmethod(lambda _u: browser)})()}
        )()
        monkeypatch.setattr(
            checkpoint,
            "sync_playwright",
            lambda: type(
                "CM",
                (),
                {
                    "__enter__": staticmethod(lambda *_a: playwright),
                    "__exit__": staticmethod(lambda *_a: False),
                },
            )(),
            raising=False,
        )

    def test_cookies_are_actually_pushed_into_the_browser(self, monkeypatch):
        context = self._Ctx()
        self._wire(monkeypatch, context)
        applied = checkpoint.restore_browser_state("http://x", STATE)
        assert context.added[0]["name"] == "session"
        assert applied["cookies_restored"] == 1

    def test_origin_storage_is_declared_not_restored(self, monkeypatch):
        self._wire(monkeypatch, self._Ctx())
        applied = checkpoint.restore_browser_state("http://x", STATE)
        assert applied["origins_not_restored"] == 1
        assert "not replayed" in applied["note"]

    def test_a_state_without_origins_says_nothing_extra(self, monkeypatch):
        self._wire(monkeypatch, self._Ctx())
        applied = checkpoint.restore_browser_state("http://x", {"cookies": []})
        assert applied["note"] == ""
