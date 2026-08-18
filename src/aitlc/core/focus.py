"""Persistent "what am I working on" selection.

The habit this preserves: with `paver run parallel --local` you never type
a feature name. You mark what should *not* run with `@skip_xray_test` once,
then run the same bare command over and over while iterating. Typing a
filename on every invocation is a real UX regression from that, and the
instinct behind the tag workflow is right.

The cost of the tag workflow is that expressing "just this one" means
editing every *other* feature file, then remembering to revert them — edits
that pollute `git status` and can be committed by accident.

Focus keeps the ergonomics and drops the cost: set the selection once, in
a state file outside the repo's tracked content, then run the bare command
as usual. No feature file is modified, so nothing can leak into a commit,
and `@skip_xray_test` tags already in the repo keep working untouched —
focus narrows *within* whatever the tags already allow.

The state file lives under `reports/` (already ignored) and stores repo
paths, so it survives across shells the way the tag edits did.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from aitlc.core import workspace


@dataclass(frozen=True)
class Focus:
    """A saved selection of feature paths, relative to the project root."""

    features: tuple[str, ...]
    set_at: float

    @property
    def is_empty(self) -> bool:
        """True when no features are focused."""
        return not self.features


def focus_path(root_dir: Path) -> Path:
    """Where the saved focus is stored."""
    return workspace.output_path(root_dir, ".aitlc", "focus.json")


def load(root_dir: Path) -> Focus | None:
    """Read the saved focus, or None when nothing is focused.

    A corrupt/unreadable state file is treated as "no focus" rather than an
    error: focus is a convenience layer, and failing a test run because a
    scratch file got truncated would be worse than simply running the full
    set the user would have gotten anyway.
    """
    path = focus_path(root_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        features = tuple(str(f) for f in raw.get("features", []))
        if not features:
            return None
        return Focus(features=features, set_at=float(raw.get("set_at", 0.0)))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def save(root_dir: Path, features: list[str]) -> Focus:
    """Persist a focus selection and return it."""
    focus = Focus(features=tuple(features), set_at=time.time())
    path = focus_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"features": list(focus.features), "set_at": focus.set_at}, indent=2)
    )
    return focus


def clear(root_dir: Path) -> bool:
    """Remove any saved focus. Returns True if something was cleared."""
    path = focus_path(root_dir)
    if not path.exists():
        return False
    path.unlink()
    return True
