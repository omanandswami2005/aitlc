"""Normalize a local .feature file's body to match Xray's stored Gherkin format.

Xray's `gherkin` field (verified live this project, PROJ-19017/PROJ-28899/
PROJ-32055) starts directly at the first step keyword and ends after the
Examples: table. It does NOT contain: the `Feature:` line, `Background:`
steps, `@tag` lines, the `Scenario`/`Scenario Outline:` header, or `#`
comments — Xray's Gherkin editor doesn't support comments at all.

A `Background:` block immediately followed by a `#@PRECOND_...` tag comment
is injected from a linked Xray Precondition issue at export time — it is
NOT part of the Test's own Gherkin and must never be included when
comparing or writing back (found live: 10 of 17 feature files in one real
session had this pattern).

This module is pure text transformation — no network calls — so it's fully
unit-testable without live Xray credentials.
"""

from __future__ import annotations

import re

_PRECOND_TAG_RE = re.compile(r"^\s*#@PRECOND_")
_TAG_LINE_RE = re.compile(r"^\s*@\S+")
_SCENARIO_HEADER_RE = re.compile(r"^\s*Scenario( Outline)?\s*:")
_COMMENT_LINE_RE = re.compile(r"^\s*#")


def normalize_local_feature(text: str) -> str:
    """Reduce a local feature file to the step body Xray stores.

    Strip a local .feature file's header/comments/injected-Background down
    to just the step body, matching Xray's stored `gherkin` field shape.
    """
    lines = text.splitlines()

    # Find the Scenario/Scenario Outline header line — everything from
    # there onward (minus its own line) is the candidate body. Everything
    # before it (Feature:, blank lines, tags, and any injected Background:
    # block) is dropped.
    scenario_idx = None
    for i, line in enumerate(lines):
        if _SCENARIO_HEADER_RE.match(line):
            scenario_idx = i
            break

    body_lines = lines[scenario_idx + 1 :] if scenario_idx is not None else lines

    kept: list[str] = []
    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if _TAG_LINE_RE.match(line):
            continue
        if _COMMENT_LINE_RE.match(line):
            continue
        kept.append(line.strip("\t"))

    # Collapse leading/trailing blank lines; keep interior blank lines
    # (Xray's own gherkin field uses blank lines between logical sections).
    while kept and kept[0] == "":
        kept.pop(0)
    while kept and kept[-1] == "":
        kept.pop()

    return "\n".join(kept)


def normalize_gherkin_body(text: str) -> str:
    """Reduce anything the caller passes to the step body Xray stores.

    Accepts either a full `.feature` file or a body that is already normalized,
    so `--file the-same-file-you-just-compared` is safe. Idempotent: a body
    with no header survives unchanged.

    Raises ValueError rather than writing a payload that would leave the Test
    invalid — a `Feature:` line inside the step body is not something Xray can
    hold, and the resulting corruption is only visible on the next fetch.
    """
    body = normalize_local_feature(text)
    offenders = [
        line
        for line in body.splitlines()
        if _TAG_LINE_RE.match(line) or line.lstrip().startswith("Feature:")
    ]
    if offenders:
        raise ValueError(
            "refusing to write a Gherkin body that still contains header "
            f"lines after normalization: {offenders[:3]}"
        )
    return body


def diff_lines(local_normalized: str, live: str) -> list[str]:
    """Minimal unified-style diff between normalized-local and live Gherkin.

    Not a full diff algorithm — line-by-line comparison is sufficient for
    "does this match" reporting, which is what compare-gherkin needs
    (FR-3.3). Returns an empty list if identical.
    """
    local_lines = local_normalized.splitlines()
    live_lines = live.splitlines()

    if local_lines == live_lines:
        return []

    import difflib

    return list(
        difflib.unified_diff(
            live_lines,
            local_lines,
            fromfile="xray (live)",
            tofile="local (normalized)",
            lineterm="",
        )
    )
