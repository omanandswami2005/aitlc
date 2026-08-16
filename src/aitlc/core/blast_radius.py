"""Blast-radius check for a proposed diff (Idea 3 groundwork, propose-only).

The motivating observation: more than once, a fix was only safe to apply
because grep confirmed the touched function/locator was used narrowly.
That check is necessary but not sufficient." This module automates the
"is this touching shared code" half of that check — a real, mechanical
signal, not a substitute for actually reading the diff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


@dataclass
class BlastRadiusReport:
    """Which shared areas a proposed diff would touch."""

    changed_files: list[str]
    touches_shared_dirs: list[str]

    @property
    def is_scoped_narrowly(self) -> bool:
        """True when the diff avoids shared step/locator code."""
        return not self.touches_shared_dirs


def changed_files_from_unified_diff(diff_text: str) -> list[str]:
    """Extract changed file paths from a standard `git diff` / unified diff."""
    return sorted(set(_DIFF_FILE_RE.findall(diff_text)))


def check(diff_text: str, *, step_dir: str, locators_dir: str) -> BlastRadiusReport:
    """Flag whether a diff touches shared step or locator directories.

    Flag whether a proposed diff touches shared step/locator directories —
    The explicit escalation trigger: "the fix touches shared code
    used outside the one failing test" should default to escalate, not act.

    Substring match, not a strict path-prefix match: a diff's file paths
    depend on where it was generated (repo root vs. a project subdirectory)
    and won't reliably line up with config.root_dir as an exact prefix —
    found live testing this against a real diff from this project's own
    root. Substring containment is more forgiving of that, and erring
    toward over-flagging is the correct bias here ("bias hard
    toward escalation"), not under-flagging for the sake of precision.
    """
    changed = changed_files_from_unified_diff(diff_text)
    shared_markers = (str(Path(step_dir)), str(Path(locators_dir)))
    touches = [f for f in changed if any(marker in f for marker in shared_markers)]
    return BlastRadiusReport(changed_files=changed, touches_shared_dirs=touches)
