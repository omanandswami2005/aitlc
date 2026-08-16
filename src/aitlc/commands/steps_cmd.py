"""`aitlc steps run ...` — structured step console CLI (Idea 2)."""

from __future__ import annotations

import json

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, unused_steps
from aitlc.core.dotenv import load_dotenv
from aitlc.core.step_console import run_console

app = typer.Typer(help="Run a slice of a feature file's steps against a live browser.")


@app.command("run")
def run(
    feature_file: str = typer.Argument(..., help="Test ID or feature file path."),
    range_: str | None = typer.Option(
        None, "--range", help="File line numbers START-END (1-based, inclusive)."
    ),
    example_row: int = typer.Option(
        0, "--example-row", help="Scenario Outline Examples row index."
    ),
    cdp_url: str | None = typer.Option(
        None,
        "--cdp-url",
        help="Attach to an existing Chrome instead of launching a new one.",
    ),
    login_step: str | None = typer.Option(
        None,
        "--login-step",
        help='e.g. "Given open the app" — run first, dispatched like any other step.',
    ),
    mobile: str | None = typer.Option(
        None,
        "--mobile",
        help=(
            "Playwright device to emulate (e.g. 'Galaxy S8'). Required for "
            "TEST_TYPE=mobile_browser scenarios — without it steps run at "
            "desktop viewport with no error."
        ),
    ),
    scenario_setup: str | None = typer.Option(
        None,
        "--scenario-setup",
        help=(
            "'module.path:function' for the project's per-scenario setup. "
            "Defaults to [project].scenario_setup in aitlc.toml. Pass 'none' "
            "to skip it (per-scenario data such as random_email will be absent)."
        ),
    ),
    allow_missing_setup: bool = typer.Option(
        False,
        "--allow-missing-setup",
        help="Run the steps even if scenario setup fails (default: stop immediately).",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Run a slice of a feature's steps."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    resolved = config.resolve_feature_path(feature_file)
    if resolved is None:
        typer.echo(
            json.dumps({"error": f"Could not resolve feature for '{feature_file}'"}),
            err=True,
        )
        raise typer.Exit(code=2)

    result = run_console(
        resolved,
        cwd=config.root_dir,
        poetry_cmd=behave_runner.resolve_poetry(),
        line_range=range_,
        example_row=example_row,
        step_dir=config.step_dir,
        cdp_url=cdp_url,
        login_step=login_step,
        mobile=mobile,
        scenario_setup=scenario_setup or config.scenario_setup,
        allow_missing_setup=allow_missing_setup,
        browser_actions=config.browser_actions,
        browser_factory=config.browser_factory,
    )

    payload = {
        "loaded_step_modules": result.loaded_step_modules,
        "scenario_setup": result.scenario_setup,
        "results": [
            {
                "step": r.step,
                "status": r.status,
                "duration_s": r.duration_s,
                "error": r.error,
            }
            for r in result.results
        ],
    }
    typer.echo(json.dumps(payload, indent=2))
    raise typer.Exit(code=0 if result.passed else 1)


@app.command("unused")
def unused(
    step_dir: str | None = typer.Option(
        None, "--step-dir", help="Override the step directory."
    ),
    feature_dir: str | None = typer.Option(
        None, "--feature-dir", help="Override the feature directory."
    ),
    include_composite: bool = typer.Option(
        True,
        "--include-composite/--no-include-composite",
        help=(
            "Treat steps invoked via context.execute_steps() as used. "
            "Disabling this reproduces Cucumber's known false positives."
        ),
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Report step definitions that no feature file uses.

    behave has no equivalent of Cucumber's unused-step report, so dead
    step definitions accumulate unnoticed: they still import, still pass
    review, and still cost time in every refactor and migration.

    Matching goes through behave's own registry rather than a regex, so
    the answer agrees with what the runner would actually dispatch.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    steps_path = config.root_dir / (step_dir or config.step_dir)
    features_path = config.root_dir / (feature_dir or config.feature_dir)
    if not steps_path.exists():
        typer.echo(json.dumps({"error": f"no step directory: {steps_path}"}), err=True)
        raise typer.Exit(code=2)

    composite: list[str] = []
    if include_composite:
        for block in unused_steps.extract_execute_steps_literals(steps_path):
            composite.extend(unused_steps.split_gherkin_block(block))

    result = unused_steps.analyze(
        cwd=config.root_dir,
        poetry_cmd=behave_runner.resolve_poetry(),
        step_dir=str(steps_path.relative_to(config.root_dir)),
        feature_dir=str(features_path.relative_to(config.root_dir)),
        execute_steps_texts=composite,
    )

    payload = result.to_dict()
    payload["composite_steps_considered"] = len(composite)
    typer.echo(json.dumps(payload, indent=2))
    raise typer.Exit(code=0)
