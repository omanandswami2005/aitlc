"""Known-flake pattern matching (FR-6).

Loads patterns.yaml (ARCHITECTURE.md §6 — a data form of the prose
findings) and matches a failure's step text + error text against it.

Deliberately simple substring matching, not regex or fuzzy scoring: the
patterns.yaml entries were written from real, specific error strings this
project actually hit, and substring matching on those is both correct and
fully auditable — a human can read a pattern's `match` block and know
exactly what it does and doesn't catch. Reach for something fancier only if
substring matching demonstrably produces false positives/negatives on real
data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Pattern:
    """One known failure signature and what to do about it."""

    id: str
    description: str
    step_contains: list[str] = field(default_factory=list)
    error_contains: list[str] = field(default_factory=list)
    suggested_action: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pattern:
        """Build a pattern from its YAML mapping."""
        match = data.get("match", {})
        return cls(
            id=data["id"],
            description=data.get("description", "").strip(),
            step_contains=match.get("step_contains", []),
            error_contains=match.get("error_contains", []),
            suggested_action=data.get("suggested_action", "").strip(),
        )

    def matches(self, step: str, error: str) -> bool:
        """Return True when every non-empty match group is satisfied.

        A pattern matches if ALL of its non-empty match-field groups have
        at least one substring present (case-insensitive). A pattern with
        only step_contains needs the step to match; only error_contains
        needs the error to match; both present needs both to match.
        """
        step_lower = step.lower()
        error_lower = error.lower()

        if self.step_contains:
            if not any(s.lower() in step_lower for s in self.step_contains):
                return False
        if self.error_contains:
            if not any(s.lower() in error_lower for s in self.error_contains):
                return False
        # A pattern with neither field configured matches nothing — an
        # empty match block is a config mistake, not "matches everything".
        return bool(self.step_contains or self.error_contains)


@dataclass
class Match:
    """A failure matched against a known pattern."""

    pattern: Pattern
    step: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form of this match."""
        return {
            "pattern_id": self.pattern.id,
            "description": self.pattern.description,
            "suggested_action": self.pattern.suggested_action,
        }


class PatternLibrary:
    """The set of known failure patterns."""

    def __init__(self, patterns: list[Pattern]):
        """Hold the patterns this library will match against."""
        self._patterns = patterns

    @classmethod
    def load(cls, path: Path) -> PatternLibrary:
        """Load a pattern library from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        patterns = [Pattern.from_dict(p) for p in data.get("patterns", [])]
        return cls(patterns)

    def classify(self, step: str, error: str) -> Match | None:
        """Return the first matching pattern, or None if nothing matches.

        First-match, not best-match: patterns.yaml entries are specific
        enough (real error strings) that ambiguity between two patterns
        matching the same failure should be resolved by tightening the
        patterns, not by a scoring heuristic here.
        """
        for pattern in self._patterns:
            if pattern.matches(step, error):
                return Match(pattern=pattern, step=step, error=error)
        return None
