"""Secret redaction for anything aitlc reads/prints.

NFR-3: a live secret (LT_ACCESS_KEY) leaked into a real session's own
transcript multiple times this project via `ps aux`, debug prints, and
framework logging. Redaction here is by known secret *value*, not by
scanning for a hardcoded key name pattern — the value is what actually
leaks, regardless of what printed it.
"""

from __future__ import annotations

REDACTED_PLACEHOLDER = "***REDACTED***"

# Values shorter than this are almost certainly not real secrets (short
# flags, single words) and redacting them would make ordinary output
# unreadable with false positives.
MIN_SECRET_LENGTH = 8


def redact_text(text: str, secret_values: list[str]) -> str:
    """Replace every occurrence of each known secret value in text.

    Longest values are replaced first so a secret that happens to be a
    substring of another (rare, but possible with prefixed tokens) doesn't
    leave a partial value exposed.
    """
    if not text or not secret_values:
        return text

    ordered = sorted(
        (v for v in secret_values if v and len(v) >= MIN_SECRET_LENGTH),
        key=len,
        reverse=True,
    )
    for value in ordered:
        if value in text:
            text = text.replace(value, REDACTED_PLACEHOLDER)
    return text


def redact_mapping(data: dict, secret_values: list[str]) -> dict:
    """Recursively redact secret values from a JSON-like structure (dict/list/str)."""
    return _redact_value(data, secret_values)  # type: ignore[return-value]


def _redact_value(value: object, secret_values: list[str]) -> object:
    if isinstance(value, str):
        return redact_text(value, secret_values)
    if isinstance(value, dict):
        return {k: _redact_value(v, secret_values) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, secret_values) for v in value]
    return value
