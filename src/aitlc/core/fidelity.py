"""Will this feature run here the way it runs in CI?

Reproducing a CI failure locally is the whole point of the tool, and nothing
checked that the local execution had the same *shape* as the remote one. Three
differences caused real wasted investigations, and all three are visible in the
feature text before anything is launched:

1. **A feature that does not create its own session.** In CI the session comes
   from a before-hook or from an earlier scenario in the same execution. Pulled
   out and run alone, the login is missing — sometimes literally commented out —
   so it fails locally for a reason that does not exist remotely, and the
   failure looks like the app.

2. **Feature tags and scenario tags are not interchangeable.** Hooks key on
   *feature*-level tags, while an export from an issue tracker puts the issue's
   labels on the *scenario*. A file that looks correctly tagged then silently
   gets none of the setup those tags select.

3. **Scenarios that depend on a sibling.** An execution runs many scenarios in
   order, sharing a browser and each other's data. Split into one file per test
   — which is what "fetch the features" does — a scenario that searches inside
   something an earlier scenario built passes alone and fails in sequence, or
   the reverse.

None of this needs a browser, so it is a static report rather than a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_STEP = re.compile(r"^\s*(Given|When|Then|And|But)\s+", re.IGNORECASE)
_COMMENTED_STEP = re.compile(r"^\s*#\s*(Given|When|Then|And|But)\s+", re.IGNORECASE)
_TAG = re.compile(r"@[\w.-]+")
_SCENARIO = re.compile(r"^\s*(Scenario Outline|Scenario|Example)\s*:", re.IGNORECASE)
_FEATURE = re.compile(r"^\s*Feature\s*:", re.IGNORECASE)
_BACKGROUND = re.compile(r"^\s*Background\s*:", re.IGNORECASE)

# Words that mark a step as establishing a session. Deliberately broad: a false
# "it logs in" is much cheaper than a false "it does not", which sends someone
# to debug a login that was never supposed to run here.
_SESSION_WORDS = ("log in", "login", "log-in", "sign in", "signin", "sign-in", "authenticate")


def _most_specific(steps: list[str]) -> str:
    """The step that best explains the missing session.

    Several steps mention signing in -- opening the page, clicking the button,
    entering credentials. The one naming an actual account is the informative
    one; quoting "click Sign in" instead sends the reader looking at a button.
    """
    for step in steps:
        lowered = step.lower()
        if "login to" in lowered or "log in to" in lowered or "sign in to" in lowered:
            return step
    return steps[0]


@dataclass
class Finding:
    """One way this run may differ from the remote one."""

    kind: str
    detail: str
    severity: str = "warn"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


@dataclass
class FidelityReport:
    """What will differ if this file is run here, as written."""

    scenarios: int = 0
    feature_tags: list[str] = field(default_factory=list)
    scenario_tags: list[str] = field(default_factory=list)
    creates_session: bool = False
    commented_out_steps: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def faithful(self) -> bool:
        return not any(f.severity == "blocker" for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "scenarios": self.scenarios,
            "feature_tags": self.feature_tags,
            "scenario_tags": self.scenario_tags,
            "creates_session": self.creates_session,
            "commented_out_steps": self.commented_out_steps,
            "faithful": self.faithful,
            "findings": [f.to_dict() for f in self.findings],
        }


def _tags_on(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("@"):
        return []
    return _TAG.findall(stripped)


def analyze(feature_text: str, *, hook_tags: list[str] | None = None) -> FidelityReport:
    """Report how a local run of this text would differ from a full execution.

    `hook_tags` are the feature-level tags the project's hooks key on. Given
    them, the report can say that a tag sits at scenario level where nothing
    will read it — the difference between "correctly tagged" and "tagged
    somewhere that has no effect".
    """
    lines = feature_text.splitlines()
    report = FidelityReport()

    pending_tags: list[str] = []
    seen_feature = False
    for line in lines:
        tags = _tags_on(line)
        if tags:
            pending_tags.extend(tags)
            continue
        if _FEATURE.match(line):
            report.feature_tags.extend(pending_tags)
            pending_tags = []
            seen_feature = True
            continue
        if _SCENARIO.match(line):
            report.scenarios += 1
            # Tags standing before a Scenario belong to the scenario -- unless
            # the Feature line has not appeared yet, in which case they are the
            # file's own.
            (report.scenario_tags if seen_feature else report.feature_tags).extend(
                pending_tags
            )
            pending_tags = []
            continue
        if _COMMENTED_STEP.match(line):
            report.commented_out_steps.append(line.strip().lstrip("#").strip())
            continue
        if _STEP.match(line) or _BACKGROUND.match(line):
            lowered = line.lower()
            if any(word in lowered for word in _SESSION_WORDS):
                report.creates_session = True

    # 1. No session of its own.
    if not report.creates_session:
        commented_login = [
            step
            for step in report.commented_out_steps
            if any(word in step.lower() for word in _SESSION_WORDS)
        ]
        if commented_login:
            report.findings.append(
                Finding(
                    kind="session_commented_out",
                    severity="blocker",
                    detail=(
                        "the login steps are commented out, so in a full execution "
                        "the session comes from a hook or an earlier scenario. Run "
                        "alone this starts signed out and fails for a reason that "
                        f"does not exist remotely: {_most_specific(commented_login)!r}"
                    ),
                )
            )
        else:
            report.findings.append(
                Finding(
                    kind="no_session_of_its_own",
                    severity="blocker",
                    detail=(
                        "nothing here signs in, so this relies on a session it does "
                        "not create. Locally that session does not exist unless a "
                        "hook provides it."
                    ),
                )
            )

    # 2. Tags in a position nothing reads.
    if hook_tags:
        misplaced = sorted(
            {t for t in report.scenario_tags if t.lstrip("@") in {h.lstrip("@") for h in hook_tags}}
        )
        if misplaced:
            report.findings.append(
                Finding(
                    kind="tags_at_scenario_level",
                    severity="blocker",
                    detail=(
                        f"{', '.join(misplaced)} select setup, but hooks read "
                        "feature-level tags and these sit on the scenario, where "
                        "nothing will read them. An export from an issue tracker "
                        "puts labels here, so the file looks tagged and is not."
                    ),
                )
            )

    # 3. Split out of a multi-scenario execution.
    if report.scenarios == 1:
        report.findings.append(
            Finding(
                kind="single_scenario",
                severity="info",
                detail=(
                    "one scenario. If the execution it came from runs several in "
                    "order, this may pass alone and fail in sequence, or the "
                    "reverse -- they share a browser and each other's data."
                ),
            )
        )
    elif report.scenarios > 1:
        report.findings.append(
            Finding(
                kind="shared_session",
                severity="info",
                detail=(
                    f"{report.scenarios} scenarios share one browser session and "
                    "each other's data, so order matters and a single scenario "
                    "cannot be judged on its own."
                ),
            )
        )

    return report
