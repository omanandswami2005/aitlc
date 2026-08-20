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


def session_path(root_dir: Path, test_id: str) -> Path:
    """Where a session for this test is stored."""
    safe = test_id.replace("/", "_").replace(" ", "_")
    return workspace.output_path(root_dir, ".aitlc", "debug", f"{safe}.json")


def progress_path(root_dir: Path, test_id: str) -> Path:
    """Where ``debug start``'s live setup progress is written."""
    safe = test_id.replace("/", "_").replace(" ", "_")
    return workspace.output_path(root_dir, ".aitlc", "debug", f"{safe}.progress.json")


def save(root_dir: Path, session: DebugSession) -> Path:
    """Persist a session, creating the directory if needed."""
    path = session_path(root_dir, session.test_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2))
    return path


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
