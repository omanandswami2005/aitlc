"""Text/JSON compaction for an AI caller -- the `--for-ai` flag.

An AI driving `debug eval`/`cdp inspect`/`debug inspect` pays for every
character of a reply as tokens, and the wire format optimizes for a human
terminal, not that: `json.dumps(..., indent=2)` spends tokens on repeated
indentation whitespace, and real page/log content (a JS eval result's
formatting, a captured stdout's blank-line padding) routinely carries
trailing whitespace and blank-line runs that add nothing either reads.

One centralized function, applied wherever `--for-ai` is accepted, rather
than each command inventing its own ad-hoc stripping -- so the definition
of "safe to trim" stays in exactly one place. Deliberately conservative:
this is whitespace CLEANUP, not text summarization or truncation (a
caller that also wants a hard length cap already has that per-command,
e.g. `captured_output_pretty_chars` in `debug_cmd.py`'s pretty-printer --
composing the two is fine, but they're not the same job).
"""

from __future__ import annotations

import json
import re
from typing import Any

_TRAILING_WHITESPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_LINE_RUN = re.compile(r"\n{3,}")


def compact_text(text: str) -> str:
    """Strip token-wasting whitespace from one string, preserving meaning.

    Two things only, both always safe:

    1. Trailing whitespace at the end of a line -- never meaningful in any
       text this tool prints (JS eval results, an aria-yaml tree, a
       traceback, a captured log).
    2. Three or more consecutive newlines collapsed to two (one blank
       line) -- more than one blank line in a row essentially never
       carries information a single one doesn't.

    Leading whitespace on a non-blank line is NEVER touched. For an
    aria-yaml accessibility tree, indentation is the only thing encoding
    nesting -- collapsing "extra" spaces there would silently corrupt the
    tree's structure, not just its formatting. Same reasoning protects a
    traceback's or a code snippet's indentation.
    """
    if not text:
        return text
    text = _TRAILING_WHITESPACE.sub("", text)
    text = _BLANK_LINE_RUN.sub("\n\n", text)
    return text.strip()


def compact_value(value: Any) -> Any:
    """Recursively apply `compact_text` to every string in a JSON-shaped value.

    Walks dict/list structures as-is (keys, ordering, and non-string
    values are untouched) so the result still validates against whatever
    shape a caller expects -- only string leaves are trimmed.
    """
    if isinstance(value, str):
        return compact_text(value)
    if isinstance(value, list):
        return [compact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: compact_value(v) for k, v in value.items()}
    return value


def dumps_for_ai(payload: Any, *, for_ai: bool) -> str:
    """JSON-serialize `payload`, compacted for an AI caller when `for_ai`.

    `for_ai=True` applies `compact_value` to every string, then serializes
    with no indentation and minimal separators (`json.dumps(indent=2)`'s
    per-line indentation is itself a real chunk of the token cost on a
    deeply nested reply -- e.g. `debug inspect --interactive`'s element
    list). `for_ai=False` is the existing human-terminal default: full
    fidelity, `indent=2`.
    """
    if for_ai:
        return json.dumps(compact_value(payload), separators=(",", ":"))
    return json.dumps(payload, indent=2)
