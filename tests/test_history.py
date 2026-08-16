"""Tests for observed-flake history."""

from __future__ import annotations

from aitlc.core import history


class TestRecordAndLoad:
    def test_empty_history_is_empty_list(self, tmp_path):
        assert history.load(tmp_path) == []

    def test_round_trips(self, tmp_path):
        history.record(tmp_path, test_id="PROJ-1", passed=True)
        history.record(tmp_path, test_id="PROJ-1", passed=False, failed_step="click X")
        entries = history.load(tmp_path)
        assert [e["status"] for e in entries] == ["passed", "failed"]
        assert entries[1]["failed_step"] == "click X"

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path):
        history.record(tmp_path, test_id="PROJ-1", passed=True)
        path = history.history_path(tmp_path)
        with path.open("a") as handle:
            handle.write("{truncated\n")
        history.record(tmp_path, test_id="PROJ-2", passed=True)
        # Concurrent runs append here; one bad write must not destroy the rest.
        assert len(history.load(tmp_path)) == 2

    def test_last_n_limits_window(self, tmp_path):
        for _ in range(5):
            history.record(tmp_path, test_id="PROJ-1", passed=True)
        assert len(history.load(tmp_path, last_n=2)) == 2

    def test_record_never_raises_on_bad_path(self, tmp_path):
        blocker = tmp_path / "reports"
        blocker.write_text("not a directory")
        history.record(blocker.parent, test_id="PROJ-1", passed=True)  # must not raise


class TestSummarize:
    def test_flaky_requires_both_outcomes(self, tmp_path):
        history.record(tmp_path, test_id="flaky", passed=True)
        history.record(tmp_path, test_id="flaky", passed=False)
        history.record(tmp_path, test_id="broken", passed=False)
        history.record(tmp_path, test_id="broken", passed=False)
        history.record(tmp_path, test_id="solid", passed=True)

        by_id = {h.test_id: h for h in history.summarize(history.load(tmp_path))}
        assert by_id["flaky"].is_flaky is True
        # Only-ever-failing is broken, not flaky: retrying it just spends
        # time to reach the same answer.
        assert by_id["broken"].is_flaky is False
        assert by_id["solid"].is_flaky is False

    def test_flake_rate(self, tmp_path):
        history.record(tmp_path, test_id="t", passed=True)
        history.record(tmp_path, test_id="t", passed=True)
        history.record(tmp_path, test_id="t", passed=False)
        summary = history.summarize(history.load(tmp_path))[0]
        assert summary.runs == 3
        assert round(summary.flake_rate, 2) == 0.33

    def test_flaky_sorts_first(self, tmp_path):
        history.record(tmp_path, test_id="stable", passed=True)
        history.record(tmp_path, test_id="wobbly", passed=True)
        history.record(tmp_path, test_id="wobbly", passed=False)
        assert history.summarize(history.load(tmp_path))[0].test_id == "wobbly"


class TestIsKnownFlaky:
    def test_needs_enough_runs(self, tmp_path):
        history.record(tmp_path, test_id="t", passed=True)
        history.record(tmp_path, test_id="t", passed=False)
        # One pass and one fail is not yet evidence; treating it as flaky
        # would mask a test that has simply started failing.
        assert history.is_known_flaky(tmp_path, "t", min_runs=3) is False

    def test_true_with_enough_evidence(self, tmp_path):
        for passed in (True, False, True, False):
            history.record(tmp_path, test_id="t", passed=passed)
        assert history.is_known_flaky(tmp_path, "t", min_runs=3) is True

    def test_unknown_test_is_false(self, tmp_path):
        assert history.is_known_flaky(tmp_path, "never-seen") is False
