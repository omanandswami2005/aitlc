"""Minimal TOON (Token-Oriented Object Notation) encoder.

On output format: JSON repeats every field's key name for every
record in an array, which is pure overhead for the uniform, same-shaped-row
data most of aitlc's output actually is. TOON replaces that with a
CSV-style header-plus-rows layout while staying more explicitly structured
(and therefore more reliably parseable by a model) than raw CSV.

This is a minimal, self-contained implementation of the specific shape
aitlc needs (a flat list of same-keyed dicts) — not a full spec
implementation. Only use it where SRS FR-1.3 calls for it: uniform tabular
arrays. Irregular/nested data should stay JSON (the gain is real only that
forcing TOON onto irregular data doesn't recover meaningful tokens).
"""

from __future__ import annotations

from typing import Any


class ToonEncodeError(ValueError):
    """Raised when the given data isn't a uniform array TOON can represent."""


def encode_table(rows: list[dict[str, Any]], name: str = "rows") -> str:
    """Encode a list of same-shaped dicts as TOON.

    Format:
        name[N]{field1,field2,...}:
          val1,val2,...
          val1,val2,...

    Raises ToonEncodeError if rows is empty or the dicts don't share
    identical key sets — TOON's whole advantage comes from uniformity, and
    encoding non-uniform data would silently produce misleading output.
    """
    if not rows:
        raise ToonEncodeError("encode_table requires at least one row")

    fields = list(rows[0].keys())
    field_set = set(fields)
    for i, row in enumerate(rows):
        if set(row.keys()) != field_set:
            raise ToonEncodeError(
                f"row {i} has keys {sorted(row.keys())}, expected {sorted(fields)} "
                "— TOON's encode_table requires uniform rows; use JSON for "
                "irregular data instead."
            )

    header = f"{name}[{len(rows)}]{{{','.join(fields)}}}:"
    lines = [header]
    for row in rows:
        values = [_encode_scalar(row[f]) for f in fields]
        lines.append("  " + ",".join(values))
    return "\n".join(lines)


def _encode_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if "," in text or "\n" in text or text.strip() != text:
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text
