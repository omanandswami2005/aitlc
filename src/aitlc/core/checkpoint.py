"""Snapshot an expensive setup, and come back to it.

A scenario can cost fifteen minutes of setup — provision a user, complete
first-time onboarding, get an approval — before reaching the step actually
under investigation. Nothing could capture that, so the fallback was a full
re-run: one investigation here cost five runs of about thirty minutes each,
re-executing eighty-six setup steps every time to look at one step. The tool
made restarting cheaper than resuming, so restarting is what happened.

Three things have to be captured together, and any one alone is useless:

1. **Browser state** — cookies and origin storage, which is what actually
   carries the session.
2. **Run-scoped values** — the generated names, e-mails and ids a suite mints
   per process. Restore a session without these and it hunts for an audience
   whose name no longer exists, which looks exactly like the app losing data.
3. **Entities created on the server** — the users, audiences and orders the
   setup produced, so a later run can reuse them instead of provisioning a
   fresh set and abandoning the old one.

**Staleness is part of the record, not an afterthought.** A session cookie and
a freshly provisioned user do not stay valid indefinitely, and silently
restoring a dead one produces precisely the kind of false failure that wastes
a day. A checkpoint carries when it was taken and refuses to restore past its
TTL rather than handing back something that no longer works.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aitlc.core import workspace

DEFAULT_TTL_SECONDS = 3600.0


class CheckpointError(RuntimeError):
    """A checkpoint cannot be written, found, or trusted."""


@dataclass
class Checkpoint:
    """Everything needed to return to a point in a scenario."""

    name: str
    test_id: str = ""
    feature: str = ""
    step_index: int = 0
    created_at: float = 0.0
    # Playwright's own {"cookies": [...], "origins": [...]} shape.
    storage_state: dict = field(default_factory=dict)
    # Run-scoped values the suite minted for this process.
    run_values: dict = field(default_factory=dict)
    # Things now existing on the server, so a later run can reuse them.
    created_entities: list = field(default_factory=list)

    def age_seconds(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.created_at)

    def is_stale(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, now: float | None = None) -> bool:
        return self.age_seconds(now) > ttl_seconds

    def to_dict(self) -> dict:
        data = asdict(self)
        data["age_seconds"] = round(self.age_seconds(), 1)
        return data

    def summary(self) -> dict:
        """The listing form: what this is, without the payload."""
        return {
            "name": self.name,
            "test_id": self.test_id,
            "feature": self.feature,
            "step_index": self.step_index,
            "created_at": self.created_at,
            "age_seconds": round(self.age_seconds(), 1),
            "cookies": len(self.storage_state.get("cookies", [])),
            "run_values": sorted(self.run_values),
            "created_entities": len(self.created_entities),
        }


def _dir(root_dir: Path) -> Path:
    return workspace.output_path(root_dir, ".aitlc", "checkpoints")


def _path(root_dir: Path, name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    if not safe:
        raise CheckpointError(f"unusable checkpoint name: {name!r}")
    return _dir(root_dir) / f"{safe}.json"


def save(root_dir: Path, checkpoint: Checkpoint) -> Path:
    """Write a checkpoint, stamping it with the time it was taken."""
    if not checkpoint.created_at:
        checkpoint.created_at = time.time()
    path = _path(root_dir, checkpoint.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(checkpoint), indent=2))
    return path


def load(root_dir: Path, name: str) -> Checkpoint | None:
    """Read a checkpoint, or None when there is none by that name."""
    path = _path(root_dir, name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CheckpointError(f"checkpoint {name!r} is corrupt: {exc}") from exc
    known = {f for f in Checkpoint.__dataclass_fields__}
    return Checkpoint(**{k: v for k, v in data.items() if k in known})


def list_all(root_dir: Path) -> list[Checkpoint]:
    """Every checkpoint, newest first."""
    directory = _dir(root_dir)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.json"):
        try:
            found.append(load(root_dir, path.stem))
        except CheckpointError:
            continue
    return sorted(
        [c for c in found if c is not None], key=lambda c: c.created_at, reverse=True
    )


def delete(root_dir: Path, name: str) -> bool:
    path = _path(root_dir, name)
    if not path.exists():
        return False
    path.unlink()
    return True


def _playwright():
    """Imported lazily and through one seam, so tests can replace it.

    A module-level import would pull Playwright in for every command that
    merely lists checkpoints.
    """
    from playwright.sync_api import sync_playwright

    return sync_playwright()


sync_playwright = None  # replaced in tests; see _playwright()


def capture_browser_state(cdp_url: str) -> dict:
    """Cookies and origin storage from a live browser.

    Only the session-carrying parts. A screenshot or a DOM dump would make the
    file large and restore nothing, and the point of a checkpoint is that it
    can be replayed rather than looked at.
    """
    factory = sync_playwright or _playwright
    with factory() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise CheckpointError("connected over CDP but the browser has no context")
        return browser.contexts[0].storage_state()


def restore_browser_state(cdp_url: str, state: dict) -> dict:
    """Replay cookies into a live browser, and report what was applied.

    Cookies only. Origin storage cannot be pushed into an already-open context
    without navigating to each origin first, and a restore that silently
    navigates would trample whatever the caller had on screen. Reporting what
    was skipped is better than pretending it was applied -- an unexplained
    half-restore is how a checkpoint becomes untrustworthy.
    """
    cookies = state.get("cookies") or []
    origins = state.get("origins") or []
    factory = sync_playwright or _playwright
    with factory() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise CheckpointError("connected over CDP but the browser has no context")
        context = browser.contexts[0]
        if cookies:
            context.add_cookies(cookies)
    return {
        "cookies_restored": len(cookies),
        "origins_not_restored": len(origins),
        "note": (
            "origin storage is not replayed into a live context; it needs a "
            "navigation per origin, which would discard the current page"
        )
        if origins
        else "",
    }
