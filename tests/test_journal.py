"""The journal exists so a follow-up question is a file read, not a re-run.

On this kind of suite a re-run costs minutes and mutates real data, so the
properties that matter are: secrets never reach disk, the file cannot grow
without bound, and two runs of the same command are comparable.
"""

from __future__ import annotations

from aitlc.core import journal


def test_secrets_are_redacted_before_anything_is_written(tmp_path):
    """The one property that makes this feature safe to have at all."""
    path = journal.record(
        tmp_path,
        command="xray compare",
        payload={"authorization": "Bearer super-secret-token", "key": "PROJ-1"},
        secret_values=["super-secret-token"],
    )
    on_disk = path.read_text()
    assert "super-secret-token" not in on_disk
    assert "PROJ-1" in on_disk


def test_oversized_payloads_are_truncated_and_flagged(tmp_path):
    journal.record(
        tmp_path,
        command="s3 report",
        payload={"body": "x" * (journal.MAX_PAYLOAD_CHARS + 50)},
    )
    entry = journal.entries(tmp_path)[0]
    assert entry.truncated is True
    assert entry.payload is not None and "raw" in entry.payload


def test_entries_are_newest_first_and_pruned_to_keep(tmp_path):
    for i in range(6):
        journal.record(tmp_path, command=f"run-{i}", at=1_000_000 + i, keep=3)
    listed = journal.entries(tmp_path)
    assert len(listed) == 3
    assert listed[0].command == "run-5"


def test_two_invocations_in_the_same_second_do_not_overwrite(tmp_path):
    a = journal.record(tmp_path, command="run", at=1_000_000)
    b = journal.record(tmp_path, command="run", at=1_000_000)
    assert a != b
    assert len(journal.entries(tmp_path)) == 2


def test_read_accepts_an_id_prefix(tmp_path):
    journal.record(tmp_path, command="run", at=1_000_000)
    entry = journal.entries(tmp_path)[0]
    assert journal.read(tmp_path, entry.entry_id[:12]) is not None
    assert journal.read(tmp_path, "nope") is None


def test_diff_reports_what_changed_between_two_runs(tmp_path):
    journal.record(
        tmp_path, command="run", exit_code=1, payload={"failures": ["a"]}, at=1_000_000
    )
    journal.record(
        tmp_path, command="run", exit_code=0, payload={"failures": []}, at=1_000_001
    )
    newest, older = journal.entries(tmp_path)[:2]
    result = journal.diff(older, newest)
    assert result["same_command"] is True
    assert result["exit_code_changed"] is True
    assert result["payload_changed"] is True


def test_a_foreign_file_does_not_break_listing(tmp_path):
    journal.record(tmp_path, command="run", at=1_000_000)
    (journal.journal_dir(tmp_path) / "garbage.json").write_text("not json")
    assert len(journal.entries(tmp_path)) == 1
