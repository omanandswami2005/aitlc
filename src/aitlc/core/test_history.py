"""Per-test behaviour across many runs: chronic, intermittent, or an outage.

One run answers "did it fail". It cannot answer the question that decides what
to do next -- *has it failed this way every time, or is this the first time?* --
and those two demand opposite responses:

| Pattern                          | Meaning       | Response                     |
|----------------------------------|---------------|------------------------------|
| same step+error every run        | deterministic | reproduce it; one run shows it |
| several different signatures     | intermittent  | establish a base rate first  |

Getting that backwards costs hours. A single in-order run once produced the
conclusion "inherited state proven", which had to be withdrawn -- the history
said intermittent all along.

Three things here are less obvious than they look, and all three were got wrong
by hand before being written down:

1. **Scenario Outline rows share one tag.** A naive last-row-wins pass marks a
   test with one failing row as passing. Rows are aggregated instead.
2. **Counts are not the signal; signatures are.** Two failures on two days mean
   nothing until you know whether they failed the *same way*.
3. **A day where everything failed is an outage, not N defects.** Averaging such
   a day into a per-test rate quietly inflates every test's failure rate, so
   those runs are labelled and excluded from rates rather than counted.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from aitlc.core import workspace

_RUN_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Volatile fragments inside an error message -- timings, generated names, ids.
# Two runs of the same defect differ in these and in nothing else, so leaving
# them in makes every occurrence look like its own unique signature.
_VOLATILE = [
    (re.compile(r"\b\d+(?:\.\d+)?\s*ms\b"), "<ms>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*s\b"), "<s>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4,}\b"), "<n>"),
]

VERDICT_HEALTHY = "healthy"
VERDICT_DETERMINISTIC = "deterministic"
VERDICT_INTERMITTENT = "intermittent"
VERDICT_UNKNOWN = "unknown"


def run_date(run_timestamp: str) -> str:
    """The calendar date of a run timestamp, for grouping and display."""
    match = _RUN_DATE.search(run_timestamp or "")
    return match.group(1) if match else ""


def signature_of(step: str, error: str) -> str:
    """A stable identity for "this failure, again".

    Deliberately coarse: the failing step plus the error with its volatile
    parts masked. Two runs of one defect must collapse to one signature, or
    every occurrence looks unique and a chronic failure reads as flaky.
    """
    text = error or ""
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    # The exception class and its first clause carry the identity; the rest is
    # usually a call log that varies run to run.
    head = text.strip().splitlines()[0] if text.strip() else ""
    return f"{(step or '').strip()}|{head.strip()}"


@dataclass
class RunOutcome:
    """How one test behaved in one run."""

    date: str = ""
    run: str = ""
    outcome: str = "not_found"
    where: str = "ci"
    plan: str = ""
    execution_key: str = ""
    step: str = ""
    error: str = ""
    signature: str = ""
    infrastructure: bool = False


@dataclass
class TestHistory:
    """Everything known about one test across the window."""

    test_key: str
    verdict: str = VERDICT_UNKNOWN
    last_passed: str = ""
    first_failed: str = ""
    runs_considered: int = 0
    runs_failed: int = 0
    signatures: dict[str, int] = field(default_factory=dict)
    runs: list[RunOutcome] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "test_key": self.test_key,
            "verdict": self.verdict,
            "last_passed": self.last_passed,
            "first_failed": self.first_failed,
            "runs_considered": self.runs_considered,
            "runs_failed": self.runs_failed,
            "signatures": self.signatures,
            "runs": [asdict(r) for r in self.runs],
        }


def mark_infrastructure_runs(
    outcomes_by_run: dict[str, list[RunOutcome]], *, min_tests: int = 2
) -> set[str]:
    """Runs where every test failed -- an outage, not N independent defects.

    `min_tests` guards the degenerate case: one test failing in a window that
    only ever looked at one test is not evidence of an outage.
    """
    outages = set()
    for run, outcomes in outcomes_by_run.items():
        ran = [o for o in outcomes if o.outcome in ("passed", "failed")]
        if len(ran) >= min_tests and all(o.outcome == "failed" for o in ran):
            outages.add(run)
    return outages


def classify(runs: list[RunOutcome]) -> str:
    """deterministic / intermittent / healthy, ignoring outage days.

    A test that failed with one signature every time it ran is deterministic.
    Any variation -- a passing run, or a second signature -- makes it
    intermittent, because both mean a single reproduction cannot be trusted.
    """
    counted = [r for r in runs if r.outcome in ("passed", "failed") and not r.infrastructure]
    if not counted:
        return VERDICT_UNKNOWN
    failures = [r for r in counted if r.outcome == "failed"]
    if not failures:
        return VERDICT_HEALTHY
    if len(failures) == len(counted) and len({r.signature for r in failures}) == 1:
        return VERDICT_DETERMINISTIC
    return VERDICT_INTERMITTENT


def build_history(test_key: str, runs: list[RunOutcome]) -> TestHistory:
    """Fold one test's per-run outcomes into a verdict and the dates that matter."""
    ordered = sorted(runs, key=lambda r: r.run)
    counted = [r for r in ordered if r.outcome in ("passed", "failed")]
    failures = [r for r in counted if r.outcome == "failed" and not r.infrastructure]

    signatures: dict[str, int] = {}
    for failure in failures:
        signatures[failure.signature] = signatures.get(failure.signature, 0) + 1

    last_passed = ""
    for outcome in ordered:
        if outcome.outcome == "passed":
            last_passed = outcome.date

    # The break date: the first failure that was never followed by a pass.
    first_failed = ""
    for outcome in ordered:
        if outcome.outcome == "passed":
            first_failed = ""
        elif outcome.outcome == "failed" and not outcome.infrastructure and not first_failed:
            first_failed = outcome.date

    return TestHistory(
        test_key=test_key,
        verdict=classify(ordered),
        last_passed=last_passed,
        first_failed=first_failed,
        runs_considered=len(counted),
        runs_failed=len(failures),
        signatures=signatures,
        runs=ordered,
    )


def matrix(histories: list[TestHistory]) -> dict:
    """A test-by-date grid: the artifact that makes a pattern visible at a glance."""
    dates = sorted({r.date for h in histories for r in h.runs if r.date})
    symbol = {"passed": "PASS", "failed": "FAIL", "not_found": "-"}
    rows = []
    for history in histories:
        by_date = {r.date: r for r in history.runs}
        rows.append(
            {
                "test_key": history.test_key,
                "verdict": history.verdict,
                "cells": [
                    symbol.get(by_date[d].outcome, "-") if d in by_date else "."
                    for d in dates
                ],
                "fail_rate": (
                    f"{history.runs_failed}/{history.runs_considered}"
                    if history.runs_considered
                    else "0/0"
                ),
            }
        )
    return {"dates": dates, "rows": rows}


def default_store(root_dir: Path) -> Path:
    """Where the consolidated record lives."""
    return workspace.output_path(root_dir, ".aitlc", "test-history.json")


def merge_into_store(path: Path, histories: list[TestHistory]) -> dict:
    """Update the consolidated record, keyed by test.

    Persisted rather than printed because the alternative is that the next
    person re-derives the same matrix and re-downloads every artifact to do
    it. Existing keys are replaced wholesale -- a fresh window is a better
    answer than a merge of two windows, and merging run lists across windows
    invites duplicate entries.
    """
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            # A corrupt store must not block the answer the user asked for.
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    for history in histories:
        existing[history.test_key] = history.to_dict()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2))
    return existing
