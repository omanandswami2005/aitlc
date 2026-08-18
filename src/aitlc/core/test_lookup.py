"""Resolve a test key to its run artifacts, without guessing which report holds it.

The gap this closes, from a real session that wasted a lot of tokens on it:
answering "did PROJ-123 pass in last night's run?" had no command. What
happened instead was — guess which suite's HTML report probably contains
the key from its human title, download that report (megabytes), fail to
find the key, grep the raw HTML by hand, guess a second report, download
that too, find nothing, and only then discover the key was resolvable from
the object listing alone.

Two facts about the key layout make that whole detour unnecessary, and both
are encoded here:

1. **The execution key is already in the object name.** Behave JSON is
   uploaded as `<prefix>/<env>/<Plan_Name>/behave_<EXEC-KEY>_<run-ts>.json`,
   so listing keys answers "which plan, which run" with no download at all.

2. **The key you are asking about is often NOT the execution key.** This is
   the part that burned a whole investigation. A Jira Test Plan runs one
   feature file per *execution* key, but the individual tests inside it
   carry their own `@TEST_<KEY>` scenario tags. Searching object names for
   such a key finds nothing, which reads exactly like "that test did not
   run" when in truth it ran, inside a differently-named file. Any lookup
   that only matches filenames is wrong for the majority of test keys, so
   `outcome_for_test` scans tags at both feature and scenario level.

The distinction is surfaced rather than hidden: a caller can tell whether a
hit came from the object name or from inside the document, because those two
answers have very different costs and very different failure modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from aitlc.core.triage import extract_error, run_timestamp

# `behave_<EXECUTION-KEY>_<RUN-TIMESTAMP>.json` -- the execution key is
# everything between the prefix and the run timestamp that follows it.
_OBJECT_NAME = re.compile(r"behave_(?P<execution>.+?)_(?P<stamp>\d{4}-\d{2}-\d{2}T[\d-]+)")


@dataclass(frozen=True)
class ObjectKeyInfo:
    """What an object key says about itself, before anything is downloaded."""

    key: str
    plan: str
    execution_key: str
    run_timestamp: str


def parse_object_key(key: str) -> ObjectKeyInfo | None:
    """Pull plan, execution key and run timestamp out of an object key.

    Returns None for a key that does not look like a Behave result object,
    so a bucket holding other artifacts alongside them can be scanned
    without filtering by hand first.
    """
    name = key.rsplit("/", 1)[-1]
    match = _OBJECT_NAME.search(name)
    if not match:
        return None
    parts = key.split("/")
    # The folder directly above the object is the test plan; a key with no
    # folder at all still parses, it just has no plan to report.
    plan = parts[-2] if len(parts) >= 2 else ""
    return ObjectKeyInfo(
        key=key,
        plan=plan,
        execution_key=match.group("execution"),
        run_timestamp=match.group("stamp"),
    )


def _tag_names(tags: list | None) -> list[str]:
    """Tag names, whether Behave wrote them as strings or as dicts."""
    names = []
    for tag in tags or []:
        name = tag if isinstance(tag, str) else tag.get("name", "")
        if name:
            names.append(name)
    return names


def tags_name_test(tags: list | None, test_key: str) -> bool:
    """Whether these tags identify `test_key`.

    Matches both `@TEST_PROJ-1` and a bare `@PROJ-1`: exports differ on
    whether the prefix survives, and a lookup that only understood one of
    them would silently report "did not run" for the other.
    """
    wanted = test_key.upper()
    for name in _tag_names(tags):
        bare = name.lstrip("@").upper()
        if bare == wanted or bare == f"TEST_{wanted}" or bare.removeprefix("TEST_") == wanted:
            return True
    return False


@dataclass
class TestOutcome:
    """How one test key fared inside one run artifact."""

    test_key: str
    execution_key: str = ""
    plan: str = ""
    run_timestamp: str = ""
    source: str = ""
    matched_by: str = ""
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    failures: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.scenarios_failed:
            return "failed"
        if self.scenarios_passed:
            return "passed"
        return "not_found"

    def to_dict(self) -> dict:
        return {
            "test_key": self.test_key,
            "status": self.status,
            "execution_key": self.execution_key,
            "plan": self.plan,
            "run": self.run_timestamp,
            "matched_by": self.matched_by,
            "scenarios": {
                "passed": self.scenarios_passed,
                "failed": self.scenarios_failed,
            },
            "failures": self.failures,
        }


def outcome_for_test(
    document: object,
    test_key: str,
    *,
    source: str = "",
    plan: str = "",
) -> TestOutcome:
    """Find `test_key` inside one parsed Behave document.

    Scenario Outline rows share one `@TEST_` tag, so a key routinely appears
    on several scenarios in the same file. They are aggregated rather than
    last-one-wins -- getting that wrong once reported a test as fully passing
    when only its final example row had.
    """
    info = parse_object_key(source) if source else None
    outcome = TestOutcome(
        test_key=test_key,
        execution_key=info.execution_key if info else "",
        plan=plan or (info.plan if info else ""),
        run_timestamp=(info.run_timestamp if info else (run_timestamp(source) or "")),
        source=Path(source).name if source else "",
    )

    features = document if isinstance(document, list) else [document]
    for feature in features:
        if not isinstance(feature, dict):
            continue
        feature_named = tags_name_test(feature.get("tags"), test_key)
        for element in feature.get("elements", []) or []:
            if not isinstance(element, dict):
                continue
            if element.get("type") == "background":
                continue
            if not (feature_named or tags_name_test(element.get("tags"), test_key)):
                continue

            steps = element.get("steps", []) or []
            failed = [
                s for s in steps if (s.get("result") or {}).get("status") == "failed"
            ]
            if not failed:
                outcome.scenarios_passed += 1
                continue
            outcome.scenarios_failed += 1
            step = failed[0]
            error, locator = extract_error((step.get("result") or {}).get("error_message"))
            outcome.failures.append(
                {
                    "feature": feature.get("name", ""),
                    "scenario": element.get("name", ""),
                    # Behave writes the keyword with a trailing space in some
                    # exports and without one in others; concatenating blindly
                    # produced "Thendouble click ..." against real data.
                    "step": " ".join(
                        part
                        for part in (
                            (step.get("keyword") or "").strip(),
                            (step.get("name") or "").strip(),
                        )
                        if part
                    ),
                    "error": error,
                    "locator": locator,
                }
            )

    if outcome.status != "not_found":
        outcome.matched_by = "tag"
    return outcome


def key_names_test(key: str, test_key: str) -> bool:
    """Whether an object key's execution key *is* this test key.

    The cheap half of the lookup: true here means the answer is one small
    download away, with no scanning of anything else in the run.
    """
    info = parse_object_key(key)
    return bool(info) and info.execution_key.upper() == test_key.upper()
