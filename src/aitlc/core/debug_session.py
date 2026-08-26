"""State for a gated debug session.

The gated runner (``aitlc.runtime.runner``) single-steps a real, paused behave
process, so it owns the steps, the Context and the browser. This module records
only what aitlc needs to find and drive that process again from a later command:
its control socket, pid, the step it parked on, and the debug browser it
attached to.

The progress file is separate from the session file on purpose: it exists
*while* ``debug start`` is still bringing the run up to the park point -- before
a session is usable -- so ``debug status`` can report progress instead of
erroring "no session" or the run being a silent wait.

Deliberately free of Playwright, behave and network calls, so it is trivially
unit-testable.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from aitlc.core import workspace


@dataclass
class DebugSession:
    """How to find and drive one paused, gated behave run."""

    test_id: str
    feature: str
    cdp_url: str
    port: int
    # Which Examples row the run targeted (behave bound it; recorded for report).
    example: int = 0
    # The gate's control socket, the behave pid, and the step index it parked on.
    socket: str = ""
    pid: int = 0
    park: int = 0
    # The cursor as last reported by the gate, for display and for a checkpoint's
    # step_index.
    index: int = 0
    # Where the gate subprocess's own stdout/stderr land (gate_launch.launch's
    # `log_name`, resolved to a full path) -- since the gate runs with
    # --no-capture, a step's real console output goes straight there, not
    # onto behave's own capture machinery. `next`/`retry`/`continue` tail
    # this file around each step to show that real output.
    log_path: str = ""
    # Whether this session attached to an already-live tracked browser rather
    # than launching its own. `stop` uses this to decide whether killing the
    # browser is this session's call to make -- a session that only attached
    # to someone else's persistent `cdp launch` browser should not kill it by
    # default. Defaults False so sessions saved by an older build (which
    # always launched their own isolated browser) keep their old behavior.
    reused: bool = False


def session_path(root_dir: Path, test_id: str) -> Path:
    """Where a session for this test is stored."""
    safe = test_id.replace("/", "_").replace(" ", "_")
    return workspace.output_path(root_dir, ".aitlc", "debug", f"{safe}.json")


def progress_path(root_dir: Path, test_id: str) -> Path:
    """Where ``debug start``'s live setup progress is written."""
    safe = test_id.replace("/", "_").replace(" ", "_")
    return workspace.output_path(root_dir, ".aitlc", "debug", f"{safe}.progress.json")


def step_log_path(root_dir: Path, test_id: str) -> Path:
    """Where this session's structured, one-line-per-step history is kept.

    `gate.log` is the gate subprocess's raw, undifferentiated stdout -- a
    step that logs something large (a full GraphQL query/response) buries
    the actual per-step pass/fail signal in thousands of interleaved lines,
    with no way to tell where one step's output ends and the next begins.
    This file is the structured counterpart: one JSON object per
    `next`/`retry`/`continue`/`run-text`/`run-line` call, in order, so a
    whole session's real step-by-step outcome is `grep`/`jq`-able (or
    handed to an agent) after the fact, not only visible transiently in
    that one command's own reply.
    """
    safe = test_id.replace("/", "_").replace(" ", "_")
    return workspace.output_path(root_dir, ".aitlc", "debug", f"{safe}.steps.jsonl")


def append_step_log(root_dir: Path, test_id: str, record: dict) -> Path:
    """Append one step's outcome as a JSON line. Never raises -- a logging
    problem must not fail the step it is merely recording."""
    path = step_log_path(root_dir, test_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass
    return path


def save(root_dir: Path, session: DebugSession) -> Path:
    """Persist a session, creating the directory if needed."""
    path = session_path(root_dir, session.test_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2))
    return path


def list_all(root_dir: Path) -> list[DebugSession]:
    """Every tracked debug session, for juggling more than one at a time.

    Session files (`<test_id>.json`) and progress files
    (`<test_id>.progress.json`) share a directory; only the former parse as
    a `DebugSession` (the latter's shape is different), so a bad/foreign
    file here is skipped rather than raising -- this is a listing, not a
    single lookup where a missing file is the caller's own bug to fix.
    """
    debug_dir = workspace.output_path(root_dir, ".aitlc", "debug")
    if not debug_dir.exists():
        return []
    sessions = []
    for path in sorted(debug_dir.glob("*.json")):
        if path.name.endswith(".progress.json"):
            continue
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        known = {f.name for f in fields(DebugSession)}
        try:
            sessions.append(DebugSession(**{k: v for k, v in raw.items() if k in known}))
        except TypeError:
            continue
    return sessions


def load(root_dir: Path, test_id: str) -> DebugSession | None:
    """Read a session back, or None when there is none to resume.

    Tolerant of unknown keys so a session written by an older build (which had
    more fields) still loads rather than raising.
    """
    path = session_path(root_dir, test_id)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    known = {f.name for f in fields(DebugSession)}
    return DebugSession(**{k: v for k, v in raw.items() if k in known})


def clear(root_dir: Path, test_id: str) -> bool:
    """Drop a session. True when one existed."""
    path = session_path(root_dir, test_id)
    if path.exists():
        path.unlink()
        return True
    return False


def write_progress(root_dir: Path, test_id: str, data: dict) -> Path:
    """Overwrite the progress file, stamping updated_at."""
    path = progress_path(root_dir, test_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**data, "updated_at": time.time()}, indent=2))
    return path


def read_progress(root_dir: Path, test_id: str) -> dict | None:
    """Read the progress file, or None when there is none / it is unreadable."""
    path = progress_path(root_dir, test_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_progress(root_dir: Path, test_id: str) -> bool:
    """Drop the progress file. True when one existed."""
    path = progress_path(root_dir, test_id)
    if path.exists():
        path.unlink()
        return True
    return False
