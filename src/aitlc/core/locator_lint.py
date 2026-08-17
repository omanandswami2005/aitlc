"""Flag locator shapes that are known to match the wrong element.

Playwright's own guidance is that `.first` hides ambiguity rather than
resolving it, and that strict mode exists to surface a DOM containing more
matches than you expected. Suites accumulate the opposite, and the failures are
expensive because they are silent: the step passes, reads a value from
somewhere else, and the assertion fails several layers away.

Every rule here comes from a real defect, not a style preference:

- an unanchored `//*` or `//div` xpath matched a row in a *different* grid on
  the same page, so a credit read returned a two-digit number while the visible
  cell showed four;
- a positional `aria-rowindex='2'` always opened the first search result, which
  was an orphan record among three same-named accounts;
- a grid selector without a `role='cell'` guard matched the **header** row and
  returned the column title where a number was expected.

Findings are advisory: reported, ranked, never fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_POSITIONAL = re.compile(
    r"(aria-rowindex|data-rowindex|data-colindex)\s*=|\)\[\d+\]|\[\d+\]$"
)
_GRID_FIELD = re.compile(r"@data-field\s*=")
_ROLE_CELL = re.compile(r"@role\s*=\s*'cell'|@role\s*=\s*\"cell\"")
_LOCATOR_LINE = re.compile(r"""^\s*["']([\w.]+)["']\s*:\s*(.+)$""")


@dataclass
class Finding:
    """One risky locator."""

    file: str
    line: int
    name: str
    value: str
    rule: str
    detail: str
    severity: str  # "high" | "medium" | "low"
    suggestion: str = ""


def _value_of(raw: str) -> str:
    """The string literal on the right of a `"name": "value"` mapping line."""
    match = re.search(r"""["'](.*)["']\s*,?\s*$""", raw.strip())
    return match.group(1) if match else raw.strip()


def _suggest(value: str, rule: str) -> str:
    """The shape upstream recommends instead, expressed for this selector.

    Naming the problem is not much use on its own -- a report of 190 risky
    locators with no rewrite attached is a backlog nobody starts. Playwright's
    guidance is concrete: scope with a relative predicate (the xpath analogue
    of `locator.filter(has=...)`) or select by role, rather than taking the
    first match.
    """
    if rule == "positional-index":
        return (
            "anchor on a value in the row instead of its position, e.g. "
            "//div[@role='row'][.//div[@role='cell' and normalize-space()='<known>']]"
            "//div[@role='cell' and @data-field='<field>'] -- the xpath form of "
            "locator.filter(has=...)"
        )
    if rule == "grid-cell-without-role":
        return (
            "add a role='cell' guard so the column header cannot match: "
            "//div[@role='cell' and " + value.split("[", 1)[-1][:40] + "..."
        )
    if rule == "unanchored-xpath":
        return (
            "pin it to something stable -- @data-testid, @id, or a role-based "
            "selector -- rather than relying on document order plus .first"
        )
    return ""


def lint_text(text: str, *, filename: str = "<locators>") -> list[Finding]:
    """Findings for one locator module."""
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _LOCATOR_LINE.match(line)
        if not match:
            continue
        name, raw = match.group(1), match.group(2)
        value = _value_of(raw)
        if not value.startswith(("//", "(", "./")):
            continue  # ids and test-ids are fine; this is about xpath shapes

        # A wildcard/`//div` root is only risky when nothing in the expression
        # pins it to a unique element. `@data-testid`, `@id` and `@name` are
        # exactly the anchors the app already provides, so flagging those would
        # bury the real findings -- an early version produced 763 rows, which is
        # the same as producing none.
        anchored = any(
            token in value
            # "[.//" is a relative predicate -- `//div[@role='row'][.//div[...]]`
            # scopes the match to a row containing a known cell. That is the
            # shape this lint wants people to move toward, so flagging it would
            # penalise the fix.
            for token in (
                "@id",
                "@data-testid",
                "@name",
                "@aria-label",
                "@data-test",
                "[.//",
            )
        )
        if value.startswith(("//*", "//div", "//span")) and not anchored:
            findings.append(
                Finding(
                    file=filename,
                    line=number,
                    name=name,
                    value=value,
                    rule="unanchored-xpath",
                    detail=(
                        "matches anywhere in the document; with .first this "
                        "silently takes an element from another container"
                    ),
                    severity="low",
                    suggestion=_suggest(value, "unanchored-xpath"),
                )
            )
        if _POSITIONAL.search(value):
            findings.append(
                Finding(
                    file=filename,
                    line=number,
                    name=name,
                    value=value,
                    rule="positional-index",
                    detail=(
                        "depends on row/column order, so it follows whatever "
                        "sorts first rather than the record you meant"
                    ),
                    severity="high",
                    suggestion=_suggest(value, "positional-index"),
                )
            )
        if _GRID_FIELD.search(value) and not _ROLE_CELL.search(value):
            findings.append(
                Finding(
                    file=filename,
                    line=number,
                    name=name,
                    value=value,
                    rule="grid-cell-without-role",
                    detail=(
                        "a grid's header carries data-field too, so this can "
                        "match the column title instead of a data cell"
                    ),
                    severity="high",
                    suggestion=_suggest(value, "grid-cell-without-role"),
                )
            )
    return findings


def lint_paths(paths: list[Path]) -> list[Finding]:
    """Findings across locator modules, highest severity first."""
    findings: list[Finding] = []
    for path in paths:
        try:
            findings.extend(lint_text(path.read_text(), filename=path.name))
        except OSError:
            continue
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: (order.get(f.severity, 9), f.file, f.line))
