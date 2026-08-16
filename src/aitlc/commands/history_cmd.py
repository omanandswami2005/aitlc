"""`aitlc history` — observed per-test outcomes and flake rate."""

from __future__ import annotations

import json

import typer
from aitlc.config import AitlcConfig
from aitlc.core import history as history_core

app = typer.Typer(help="Per-test run history and observed flake rate.")


@app.command("show")
def show(
    last: int | None = typer.Option(
        None, "--last", help="Only consider the most recent N recorded runs."
    ),
    flaky_only: bool = typer.Option(
        False, "--flaky-only", help="Only tests that have both passed and failed."
    ),
    test_id: str | None = typer.Option(None, "--test", help="Limit to one test ID."),
) -> None:
    """Summarize recorded runs, flakiest first.

    A test counts as flaky only when it has *both* passed and failed.
    One that has only ever failed is broken rather than flaky, and
    retrying it spends time to reach the same answer — so the two are
    reported separately instead of being lumped together.
    """
    config = AitlcConfig.find_and_load()
    entries = history_core.load(config.root_dir, last_n=last)
    histories = history_core.summarize(entries)

    if test_id:
        histories = [h for h in histories if h.test_id == test_id]
    if flaky_only:
        histories = [h for h in histories if h.is_flaky]

    typer.echo(
        json.dumps(
            {
                "recorded_runs": len(entries),
                "tests": len(histories),
                "flaky": sum(1 for h in histories if h.is_flaky),
                "history": [h.to_dict() for h in histories],
            },
            indent=2,
        )
    )


@app.command("record")
def record(
    test_id: str = typer.Argument(..., help="Test ID this outcome belongs to."),
    passed: bool = typer.Option(..., "--passed/--failed", help="The outcome."),
    duration: float | None = typer.Option(None, "--duration", help="Seconds taken."),
    failed_step: str | None = typer.Option(
        None, "--failed-step", help="The step that failed, when it failed."
    ),
) -> None:
    """Append one outcome by hand.

    `aitlc run` records automatically; this exists for outcomes produced
    elsewhere — a CI job, or a run driven through `aitlc behave`.
    """
    config = AitlcConfig.find_and_load()
    history_core.record(
        config.root_dir,
        test_id=test_id,
        passed=passed,
        duration_s=duration,
        failed_step=failed_step,
    )
    typer.echo(
        json.dumps({"recorded": test_id, "status": "passed" if passed else "failed"})
    )


@app.command("clear")
def clear() -> None:
    """Delete all recorded history."""
    config = AitlcConfig.find_and_load()
    path = history_core.history_path(config.root_dir)
    existed = path.exists()
    if existed:
        path.unlink()
    typer.echo(json.dumps({"cleared": existed, "path": str(path)}))
