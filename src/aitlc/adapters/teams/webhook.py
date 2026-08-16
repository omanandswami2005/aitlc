"""Generic Microsoft Teams webhook poster (FR-10).

Written from scratch rather than wrapping a host project's own Teams client.
Such clients typically read the project's global config object directly,
which is exactly the coupling ARCHITECTURE.md §1 forbids in an adapter. The
Adaptive-Card shape here is the ordinary one (a table of pass/fail/skip plus
failure detail), but the webhook URL and subject arrive as plain arguments,
resolved by the caller from aitlc.toml and the environment like every other
adapter.

Scope note: this posts a PER-RUN summary — which scenarios failed in *this*
run and how long each took to fail — not a cross-run "failing for N days"
streak. No such history exists in this project's own reporting code either
(confirmed by reading helper/reporting/enhanced_test_report_generator.py
and the Xray Test.testRuns API before building this).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from aitlc.core.behave_runner import RunResult


class TeamsWebhookError(Exception):
    """Raised when the webhook POST itself fails (network, non-2xx)."""


@dataclass
class RunSummary:
    """One test ID's run, reduced to what the Teams card needs."""

    test_id: str
    result: RunResult


def _cell(text: Any, color: str | None = None, weight: str | None = None) -> dict:
    block: dict[str, Any] = {"type": "TextBlock", "text": str(text), "wrap": True}
    if color:
        block["color"] = color
    if weight:
        block["weight"] = weight
    return {"type": "TableCell", "items": [block]}


def _row(cells: list[dict]) -> dict:
    return {
        "type": "TableRow",
        "cells": cells,
        "horizontalCellContentAlignment": "Center",
        "horizontalAlignment": "Center",
    }


def build_summary_card(
    subject: str,
    runs: list[RunSummary],
    report_url: str | None = None,
) -> dict:
    """Build the Adaptive Card payload for a run summary.

    Build the Adaptive Card payload: one row per test ID (status +
    duration), plus one detail line per failed scenario (step + error),
    matching the real report_generator's totals-row-then-detail shape.
    """
    passed_count = sum(1 for r in runs if r.result.passed)
    failed_count = len(runs) - passed_count

    header = _row(
        [
            _cell(h, weight="Bolder")
            for h in ("Test", "Status", "Duration (s)", "Failed step")
        ]
    )
    rows = [header]
    for run in runs:
        status = "PASSED" if run.result.passed else "FAILED"
        status_color = "Good" if run.result.passed else "Attention"
        total_duration = round(sum(s.duration_seconds for s in run.result.scenarios), 2)
        first_failure = run.result.failures[0] if run.result.failures else None
        failure_text = (
            f"{first_failure.step}: {first_failure.error}" if first_failure else ""
        )
        rows.append(
            _row(
                [
                    _cell(run.test_id),
                    _cell(status, color=status_color, weight="Bolder"),
                    _cell(total_duration),
                    _cell(failure_text[:300]),
                ]
            )
        )

    table = {
        "type": "Table",
        "columns": [{"width": 1} for _ in range(4)],
        "rows": rows,
    }

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": subject,
            "wrap": True,
            "weight": "Bolder",
            "color": "Good" if failed_count == 0 else "Attention",
            "size": "Large",
            "separator": True,
        },
        {
            "type": "TextBlock",
            "text": f"{passed_count} passed, {failed_count} failed, of {len(runs)} total",
            "wrap": True,
        },
        table,
    ]

    if report_url:
        body.append(
            {
                "type": "ActionSet",
                "actions": [
                    {
                        "type": "Action.OpenUrl",
                        "title": "View report",
                        "url": report_url,
                        "iconUrl": "icon:DocumentChevronDouble",
                    }
                ],
                "height": "stretch",
            }
        )

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "body": body,
                    "version": "1.5",
                    "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
                    "msteams": {"width": "Full"},
                },
            }
        ],
    }


def post(webhook_url: str, payload: dict, *, timeout: float = 60.0) -> None:
    """POST a payload to a webhook, raising on any non-success response."""
    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise TeamsWebhookError(f"POST to Teams webhook failed: {exc}") from exc
    if response.status_code not in (200, 202):
        raise TeamsWebhookError(
            f"Teams webhook returned {response.status_code}: {response.text[:500]}"
        )
