"""`aitlc notify-teams <test-ids...>` (FR-10).

Runs each test ID, then posts a summary — which scenarios failed and how
long each took — to a Teams webhook as an Adaptive Card. Per-run only: see
adapters/teams/webhook.py's docstring for why this doesn't attempt
cross-run "failing for N days" tracking (no such data source exists in
this project today — checked enhanced_test_report_generator.py's n8n
pipeline and Xray's Test.testRuns API before building this).
"""

from __future__ import annotations

import json

import typer
from aitlc.adapters.teams import webhook as teams_webhook
from aitlc.config import AitlcConfig, ConfigError
from aitlc.core import behave_runner
from aitlc.core.dotenv import load_dotenv


def notify_teams(
    test_ids: list[str] = typer.Argument(..., help="One or more test IDs to run."),
    env_file: str = typer.Option(".env", "--env-file"),
    subject: str | None = typer.Option(
        None, "--subject", help="Card title. Defaults to '<project> run: X/Y passed'."
    ),
    report_url: str | None = typer.Option(
        None,
        "--report-url",
        help="Optional link attached to the card (e.g. an S3 report URL).",
    ),
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the Adaptive Card JSON instead of posting — for previewing without spamming the channel.",
    ),
) -> None:
    """Post a run summary to a Teams webhook."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    runs: list[teams_webhook.RunSummary] = []
    for test_id in test_ids:
        feature_path = config.resolve_feature_path(test_id)
        if feature_path is None:
            typer.echo(
                json.dumps({"error": f"Could not resolve feature for '{test_id}'"}),
                err=True,
            )
            raise typer.Exit(code=2)
        typer.echo(f"Running {test_id}...", err=True)
        result = behave_runner.run(feature_path, cwd=config.root_dir)
        runs.append(teams_webhook.RunSummary(test_id=test_id, result=result))

    passed_count = sum(1 for r in runs if r.result.passed)
    resolved_subject = subject or (
        f"{config.project_name} run: {passed_count}/{len(runs)} passed"
    )
    card = teams_webhook.build_summary_card(
        resolved_subject, runs, report_url=report_url
    )

    if print_only:
        typer.echo(json.dumps(card, indent=2))
    else:
        try:
            webhook_url = config.require_env("teams_webhook_url")
        except ConfigError as exc:
            typer.echo(json.dumps({"error": str(exc)}), err=True)
            raise typer.Exit(code=2) from exc
        try:
            teams_webhook.post(webhook_url, card)
        except teams_webhook.TeamsWebhookError as exc:
            typer.echo(json.dumps({"error": str(exc)}), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(
            json.dumps({"posted": True, "passed": passed_count, "total": len(runs)})
        )

    raise typer.Exit(code=0 if passed_count == len(runs) else 1)
