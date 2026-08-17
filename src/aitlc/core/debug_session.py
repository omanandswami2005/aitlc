"""State for an interactive debug session over one feature file.

The commands this backs (`aitlc debug start / retry / next / certify`) exist
because the cheap loop is not the obvious one. Re-running a whole scenario
after every edit is what people reach for, and on a suite where setup takes
minutes -- and where running a scenario mutates real data -- it is the single
most expensive habit available. This module holds the position so that
re-running *one step against the browser you already have* is a single command.

Deliberately free of Playwright, Typer and network calls: the position
arithmetic is where the mistakes live, so it is unit-testable on its own.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Continuation keywords cannot open a parsed slice -- Behave rejects a block
# starting with And/But because there is no preceding step to continue. Roughly
# half the lines of a real feature start with one, so a slice runner that does
# not promote them fails on the majority of positions a user will pick.
_CONTINUATION = ("and ", "but ", "* ")
_STEP_KEYWORDS = ("given ", "when ", "then ") + _CONTINUATION


def is_step_line(line: str) -> bool:
    """True when a feature-file line is a step rather than structure."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "@", "|", '"""')):
        return False
    return stripped.lower().startswith(_STEP_KEYWORDS)


def promote_leading_continuation(steps: list[str]) -> list[str]:
    """Rewrite a leading And/But/* so the slice parses standalone.

    Uses `When` rather than `Given`/`Then` because it is the neutral choice:
    the keyword carries no meaning at dispatch time -- Behave matches on the
    text -- so this only has to be *parseable*, not semantically apt.
    """
    if not steps:
        return steps
    head = steps[0].strip()
    if head.lower().startswith(_CONTINUATION):
        keyword_len = len(head.split(" ", 1)[0])
        indent = steps[0][: len(steps[0]) - len(steps[0].lstrip())]
        steps = list(steps)
        steps[0] = f"{indent}When {head[keyword_len:].lstrip()}"
    return steps


def feature_steps(feature_text: str) -> list[str]:
    """The step lines of a feature, in order, excluding the Examples table."""
    lines = feature_text.splitlines()
    examples_at = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("Examples")),
        None,
    )
    if examples_at is not None:
        lines = lines[:examples_at]
    return [line for line in lines if is_step_line(line)]


@dataclass
class Attempt:
    """One run of one step, kept so 'did my fix work' is a diff, not memory."""

    index: int
    step: str
    status: str
    at: float


@dataclass
class DebugSession:
    """Where a debug session is, and how it got there."""

    test_id: str
    feature: str
    cdp_url: str
    port: int
    index: int = 0
    steps: list[str] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def current(self) -> str | None:
        """The step the session is parked on."""
        if 0 <= self.index < len(self.steps):
            return self.steps[self.index]
        return None

    @property
    def finished(self) -> bool:
        """True once the session has advanced past the last step."""
        return self.index >= len(self.steps)

    def slice_through(self, upto: int) -> list[str]:
        """Steps [0, upto), ready to parse as a standalone block."""
        return promote_leading_continuation(self.steps[:upto])

    def record(self, status: str) -> None:
        """Note the outcome of running the current step."""
        step = self.current
        if step is None:
            return
        self.attempts.append(
            Attempt(index=self.index, step=step.strip(), status=status, at=time.time())
        )

    def advance(self) -> None:
        """Move to the next step. Never past the end."""
        self.index = min(self.index + 1, len(self.steps))

    def attempts_for_current(self) -> list[Attempt]:
        """Every attempt at the step currently parked on."""
        return [a for a in self.attempts if a.index == self.index]


def session_path(root_dir: Path, test_id: str) -> Path:
    """Where a session for this test is stored."""
    safe = test_id.replace("/", "_").replace(" ", "_")
    return root_dir / "reports" / ".aitlc" / "debug" / f"{safe}.json"


def save(root_dir: Path, session: DebugSession) -> Path:
    """Persist a session, creating the directory if needed."""
    path = session_path(root_dir, session.test_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2))
    return path


def load(root_dir: Path, test_id: str) -> DebugSession | None:
    """Read a session back, or None when there is none to resume."""
    path = session_path(root_dir, test_id)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    attempts = [Attempt(**a) for a in raw.pop("attempts", [])]
    return DebugSession(**raw, attempts=attempts)


def clear(root_dir: Path, test_id: str) -> bool:
    """Drop a session. True when one existed."""
    path = session_path(root_dir, test_id)
    if path.exists():
        path.unlink()
        return True
    return False
