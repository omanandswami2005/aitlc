"""Per-test run history, and the flake rate derived from it.

`patterns.yaml` answers "does this failure look like a known flake?" by
matching a signature someone wrote down. That only ever covers flakes
somebody already recognised and described. History answers the
complementary question — "does this test actually fail intermittently?" —
from observed outcomes, so a new flake is visible the second time it
happens rather than whenever a human gets around to characterising it.

Deliberately a local JSONL file, not a service. The commercial tools in
this space are CI-hosted platforms, and the open-source alternatives are
heavy infrastructure; neither is proportionate to "should this retry".
Append-only text keeps the data inspectable with `tail`, diffable, and
trivially deletable.

A flake here means: **this test has both passed and failed**, within the
window examined. A test that only ever fails is broken, not flaky, and
retrying it just spends time to reach the same answer — so the two are
reported separately.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestHistory:
    """One test's observed outcomes over the recorded window."""

    test_id: str
    runs: int
    passed: int
    failed: int
    last_status: str | None

    @property
    def is_flaky(self) -> bool:
        """True when this test has both passed and failed."""
        return self.passed > 0 and self.failed > 0

    @property
    def flake_rate(self) -> float:
        """Fraction of recorded runs that failed."""
        return (self.failed / self.runs) if self.runs else 0.0

    def to_dict(self) -> dict:
        """Return a JSON-serializable form of this history."""
        return {
            "test_id": self.test_id,
            "runs": self.runs,
            "passed": self.passed,
            "failed": self.failed,
            "last_status": self.last_status,
            "is_flaky": self.is_flaky,
            "flake_rate": round(self.flake_rate, 3),
        }


def history_path(root_dir: Path) -> Path:
    """Where run history is appended."""
    return root_dir / "reports" / ".aitlc" / "history.jsonl"


def record(
    root_dir: Path,
    *,
    test_id: str,
    passed: bool,
    duration_s: float | None = None,
    failed_step: str | None = None,
) -> None:
    """Append one run outcome.

    Never raises into a caller: recording history is a side benefit of
    running a test, and losing a line of telemetry must not turn a passing
    run into a failing command.
    """
    entry = {
        "ts": time.time(),
        "test_id": test_id,
        "status": "passed" if passed else "failed",
    }
    if duration_s is not None:
        entry["duration_s"] = round(duration_s, 2)
    if failed_step:
        entry["failed_step"] = failed_step[:300]

    try:
        path = history_path(root_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def load(root_dir: Path, *, last_n: int | None = None) -> list[dict]:
    """Read recorded entries, most recent last.

    Malformed lines are skipped rather than fatal: this file is appended
    to by concurrent runs, and a single truncated write should not make
    the whole history unreadable.
    """
    path = history_path(root_dir)
    if not path.exists():
        return []

    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []

    return entries[-last_n:] if last_n else entries


def summarize(entries: list[dict]) -> list[TestHistory]:
    """Collapse raw entries into per-test history, flakiest first."""
    by_test: dict[str, dict] = {}
    for entry in entries:
        test_id = entry.get("test_id")
        if not test_id:
            continue
        bucket = by_test.setdefault(test_id, {"passed": 0, "failed": 0, "last": None})
        status = entry.get("status")
        if status == "passed":
            bucket["passed"] += 1
        elif status == "failed":
            bucket["failed"] += 1
        bucket["last"] = status

    histories = [
        TestHistory(
            test_id=test_id,
            runs=data["passed"] + data["failed"],
            passed=data["passed"],
            failed=data["failed"],
            last_status=data["last"],
        )
        for test_id, data in by_test.items()
    ]
    # Flaky first, then by how often they fail: that ordering puts the
    # tests worth investigating at the top of the output.
    histories.sort(key=lambda h: (not h.is_flaky, -h.flake_rate, h.test_id))
    return histories


def is_known_flaky(root_dir: Path, test_id: str, *, min_runs: int = 3) -> bool:
    """Report whether this test has both passed and failed.

    `min_runs` guards the obvious trap: one pass and one fail is not yet
    evidence of flakiness, and retrying on that basis would mask a test
    that has simply started failing.
    """
    for history in summarize(load(root_dir)):
        if history.test_id == test_id:
            return history.is_flaky and history.runs >= min_runs
    return False
