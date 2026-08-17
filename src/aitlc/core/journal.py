"""A record of what each command did, so the next question is a file read.

Command output currently lives only in terminal scrollback. Asking a follow-up
an hour later means running the thing again -- and here a run is minutes long
and mutates real data, a compare sweep is one API call per test, a report
triage is a large download. All to re-derive an answer that was already
computed once.

Entries are redacted before they touch disk, capped in size, and pruned by
count. That order matters: a journal that quietly writes tokens into a
directory nobody thinks of as sensitive is worse than no journal.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aitlc.core.redact import redact_text

# A payload big enough to be a liability rather than a record. Report bodies and
# full behave transcripts land well above this; the summary is what has value.
MAX_PAYLOAD_CHARS = 200_000
DEFAULT_KEEP = 200


@dataclass
class JournalEntry:
    """One invocation: what ran, how it ended, and what it printed."""

    entry_id: str
    command: str
    argv: list[str]
    exit_code: int
    duration_s: float
    at: float
    payload: dict | None = None
    truncated: bool = False
    note: str = ""
    tags: list[str] = field(default_factory=list)


def journal_dir(root_dir: Path) -> Path:
    """Where entries are stored."""
    return root_dir / "reports" / ".aitlc" / "runs"


def _entry_id(at: float, command: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(at))
    return f"{stamp}-{command.replace(' ', '_')}"


def record(
    root_dir: Path,
    *,
    command: str,
    argv: list[str] | None = None,
    exit_code: int = 0,
    duration_s: float = 0.0,
    payload: dict | None = None,
    secret_values: list[str] | None = None,
    note: str = "",
    tags: list[str] | None = None,
    keep: int = DEFAULT_KEEP,
    at: float | None = None,
) -> Path:
    """Append one entry and prune old ones. Returns the file written."""
    at = time.time() if at is None else at
    body = json.dumps(payload, indent=2, default=str) if payload is not None else ""
    body = redact_text(body, secret_values or [])
    truncated = len(body) > MAX_PAYLOAD_CHARS
    if truncated:
        body = body[:MAX_PAYLOAD_CHARS]

    try:
        stored = json.loads(body) if body and not truncated else None
    except json.JSONDecodeError:
        stored = None

    entry = JournalEntry(
        entry_id=_entry_id(at, command),
        command=command,
        argv=list(argv or []),
        exit_code=exit_code,
        duration_s=round(duration_s, 3),
        at=at,
        payload=stored if stored is not None else ({"raw": body} if body else None),
        truncated=truncated,
        note=note,
        tags=list(tags or []),
    )

    directory = journal_dir(root_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entry.entry_id}.json"
    # Two invocations inside one second would otherwise overwrite each other.
    suffix = 1
    while path.exists():
        path = directory / f"{entry.entry_id}-{suffix}.json"
        suffix += 1
    path.write_text(json.dumps(asdict(entry), indent=2))
    prune(root_dir, keep=keep)
    return path


def entries(root_dir: Path, limit: int | None = None) -> list[JournalEntry]:
    """Entries, newest first."""
    directory = journal_dir(root_dir)
    if not directory.exists():
        return []
    out: list[JournalEntry] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            out.append(JournalEntry(**json.loads(path.read_text())))
        except (json.JSONDecodeError, TypeError):
            continue  # a half-written or foreign file must not break listing
        if limit is not None and len(out) >= limit:
            break
    return out


def read(root_dir: Path, entry_id: str) -> JournalEntry | None:
    """One entry by id, or the newest whose id starts with it."""
    for entry in entries(root_dir):
        if entry.entry_id == entry_id or entry.entry_id.startswith(entry_id):
            return entry
    return None


def prune(root_dir: Path, keep: int = DEFAULT_KEEP) -> int:
    """Delete all but the newest `keep` entries. Returns how many went."""
    directory = journal_dir(root_dir)
    if not directory.exists():
        return 0
    paths = sorted(directory.glob("*.json"), reverse=True)
    removed = 0
    for path in paths[keep:]:
        path.unlink()
        removed += 1
    return removed


def diff(left: JournalEntry, right: JournalEntry) -> dict:
    """What changed between two runs of the same command.

    Answers the question that motivated the journal: *did my fix work, or was
    that one green run luck?* Comparing two entries is the whole point.
    """
    return {
        "left": {
            "id": left.entry_id,
            "exit_code": left.exit_code,
            "duration_s": left.duration_s,
        },
        "right": {
            "id": right.entry_id,
            "exit_code": right.exit_code,
            "duration_s": right.duration_s,
        },
        "same_command": left.command == right.command,
        "exit_code_changed": left.exit_code != right.exit_code,
        "payload_changed": left.payload != right.payload,
    }
