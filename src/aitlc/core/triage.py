"""Turn a run's Behave JSON reports into the table a person actually wants.

The per-execution Behave JSON is the better artifact: a whole run is a few
hundred KB against a multi-megabyte HTML report, and it already carries
per-step status, timings, tags and error text. What was missing was anything
that read it, so triaging one run meant listing hundreds of object keys,
downloading each file singly, and writing a parser -- twice, because the first
attempt reported the wrong line.

Pure functions over parsed JSON: no S3, no network, no Typer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# behave appends every captured stream after the traceback. The real exception
# is above them, so all three have to go before taking the last line -- missing
# one is how a step reported a urllib3 warning as its failure.
_CAPTURED_MARKERS = ("\nCaptured stdout", "\nCaptured logging", "\nCaptured stderr")
_RUN_TS = re.compile(r"(\d{4}-\d{2}-\d{2}T[\d-]+)")


@dataclass
class Failure:
    """One failing scenario, reduced to what a triage table needs."""

    test_key: str
    execution_key: str
    feature: str
    scenario: str
    step: str
    error: str
    locator: str = ""


@dataclass
class TriageResult:
    """Totals plus one row per failure."""

    features_passed: int = 0
    features_failed: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    failures: list[Failure] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "features": {
                "passed": self.features_passed,
                "failed": self.features_failed,
            },
            "scenarios": {
                "passed": self.scenarios_passed,
                "failed": self.scenarios_failed,
            },
            "failures": [f.__dict__ for f in self.failures],
        }


def extract_error(error_message: str | list[str] | None) -> tuple[str, str]:
    """The real exception line, and the locator it was waiting on.

    Returns ("", "") when there is nothing usable rather than inventing text --
    a plausible-looking wrong error is worse than an empty one.
    """
    if isinstance(error_message, list):
        error_message = "\n".join(error_message)
    text = error_message or ""
    for marker in _CAPTURED_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return "", ""
    head = next(
        (
            line
            for line in lines
            if "Error" in line or "Assertion" in line or "Timeout" in line
        ),
        lines[0],
    )
    locator = next(
        (line for line in lines if "waiting for" in line or "locator(" in line), ""
    )
    return head, locator


def run_timestamp(key: str) -> str | None:
    """The run timestamp embedded in a report key, if there is one."""
    match = _RUN_TS.search(key)
    return match.group(1) if match else None


def _test_key(tags: list, fallback: str) -> str:
    for tag in tags or []:
        name = tag if isinstance(tag, str) else tag.get("name", "")
        if name.startswith("TEST_"):
            return name.replace("TEST_", "")
    return fallback


def triage_documents(documents: list[tuple[str, object]]) -> TriageResult:
    """Aggregate parsed Behave JSON documents into totals and failures.

    `documents` is a list of (source-name, parsed-json) so a failure can be
    attributed to the file it came from when a run spans several test plans --
    which it does whenever one CI job covers more than one plan.
    """
    result = TriageResult()
    for source, doc in documents:
        features = doc if isinstance(doc, list) else [doc]
        for feature in features:
            if not isinstance(feature, dict):
                continue
            tags = feature.get("tags") or []
            execution = _test_key(tags, Path(source).stem)
            feature_failed = False
            for element in feature.get("elements", []) or []:
                steps = element.get("steps", []) or []
                failed = [
                    s
                    for s in steps
                    if (s.get("result") or {}).get("status") == "failed"
                ]
                if not failed:
                    result.scenarios_passed += 1
                    continue
                result.scenarios_failed += 1
                feature_failed = True
                step = failed[0]
                error, locator = extract_error(
                    (step.get("result") or {}).get("error_message")
                )
                result.failures.append(
                    Failure(
                        test_key=_test_key(element.get("tags") or [], execution),
                        execution_key=execution,
                        feature=feature.get("name", ""),
                        scenario=element.get("name", ""),
                        step=(
                            (step.get("keyword") or "") + (step.get("name") or "")
                        ).strip(),
                        error=error,
                        locator=locator,
                    )
                )
            if feature_failed:
                result.features_failed += 1
            else:
                result.features_passed += 1
    return result


def triage_paths(paths: list[Path]) -> TriageResult:
    """Same, reading from disk. Unparseable files are skipped, not fatal."""
    documents: list[tuple[str, object]] = []
    for path in paths:
        try:
            documents.append((path.name, json.loads(path.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    return triage_documents(documents)
