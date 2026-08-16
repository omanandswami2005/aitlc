"""`aitlc xray ...` — Xray Gherkin CLI (FR-3)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.adapters.xray.client import XrayClient, XrayError
from aitlc.config import AitlcConfig, ConfigError
from aitlc.core.dotenv import load_dotenv

app = typer.Typer(help="Read/write/compare a Test's Gherkin against live Xray.")


@app.callback()
def _load_env(
    env_file: str = typer.Option(
        ".env",
        "--env-file",
        help="Load env vars (Xray credentials, etc.) from this file.",
    ),
) -> None:
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)


def _build_client(config: AitlcConfig) -> XrayClient:
    """Build an XrayClient from client credentials.

    Build an XrayClient, exchanging Xray API client_id/client_secret for
    a bearer token via the documented Xray Cloud authenticate flow
    (client_secret alone is not a usable bearer token).
    """
    try:
        client_id = config.require_env("jira_xray_client_id")
        client_secret = config.require_env("jira_xray_client_secret")
    except ConfigError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=2) from exc
    return XrayClient.from_client_credentials(
        graphql_url=config.xray_graphql_url,
        client_id=client_id,
        client_secret=client_secret,
    )


@app.command("get-gherkin")
def get_gherkin(
    key: str = typer.Argument(..., help="Xray Test key, e.g. PROJ-12345.")
) -> None:
    """Print a Test's live Gherkin."""
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    try:
        test = client.get_gherkin(key)
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {"key": test.key, "issue_id": test.issue_id, "gherkin": test.gherkin}
        )
    )


@app.command("update-gherkin")
def update_gherkin(
    key: str = typer.Argument(..., help="Xray Test key."),
    file: Path = typer.Option(
        ..., "--file", exists=True, help="File containing the new Gherkin body."
    ),
) -> None:
    """Write a Test's Gherkin from a file."""
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    new_gherkin = file.read_text()
    try:
        test = client.update_gherkin(key, new_gherkin)
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"key": test.key, "updated": True}))


@app.command("compare-gherkin")
def compare_gherkin(
    key: str = typer.Argument(..., help="Xray Test key."),
    file: Path | None = typer.Argument(
        None, help="Local .feature file. Auto-resolved from key if omitted (FR-3.3)."
    ),
) -> None:
    """Compare a local feature file against live Xray."""
    config = AitlcConfig.find_and_load()
    resolved_file = file or config.resolve_feature_path(key)
    if resolved_file is None or not resolved_file.exists():
        typer.echo(
            json.dumps(
                {"error": f"Could not resolve a local feature file for '{key}'"}
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    client = _build_client(config)
    try:
        result = client.compare_gherkin(key, resolved_file.read_text())
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            {"key": result.key, "in_sync": result.in_sync, "diff": result.diff},
            indent=2,
        )
    )
    raise typer.Exit(code=0 if result.in_sync else 1)


@app.command("create-test")
def create_test(
    project_key: str = typer.Option(..., help="Jira project key, e.g. PROJ."),
    summary: str = typer.Option(..., help="Test summary."),
    gherkin_file: Path = typer.Option(
        ..., exists=True, help="File containing the Gherkin body."
    ),
) -> None:
    """Create a new Cucumber-type Test."""
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    try:
        test = client.create_test(project_key, summary, gherkin_file.read_text())
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"key": test.key, "issue_id": test.issue_id}))


@app.command("link-to-execution")
def link_to_execution(
    test_key: str = typer.Argument(...),
    execution_key: str = typer.Argument(...),
) -> None:
    """Link a Test to a Test Execution."""
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    try:
        client.link_to_execution(test_key, execution_key)
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {"test_key": test_key, "execution_key": execution_key, "linked": True}
        )
    )


@app.command("executions-for-test")
def executions_for_test(key: str = typer.Argument(..., help="Xray Test key.")) -> None:
    """List the Test Executions a Test belongs to."""
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    try:
        executions = client.get_test_executions_for_test(key)
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"test_key": key, "executions": [e.key for e in executions]}))


@app.command("tests-for-execution")
def tests_for_execution(
    key: str = typer.Argument(..., help="Xray Test Execution key.")
) -> None:
    """List the Tests inside a Test Execution."""
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    try:
        tests = client.get_tests_for_execution(key)
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"execution_key": key, "tests": [t.key for t in tests]}))


@app.command("runs-for-execution")
def runs_for_execution(
    key: str = typer.Argument(..., help="Xray Test Execution key.")
) -> None:
    """Report per-test pass/fail status within one execution.

    The leaf of the report -> run -> execution -> test hierarchy — actual
    pass/fail status per test within one execution.
    """
    config = AitlcConfig.find_and_load()
    client = _build_client(config)
    try:
        runs = client.get_test_runs_for_execution(key)
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "execution_key": key,
                "runs": [
                    {"id": r.id, "test_key": r.test_key, "status": r.status}
                    for r in runs
                ],
            },
            indent=2,
        )
    )


@app.command("find-step-usage")
def find_step_usage(
    step_text: str = typer.Argument(
        ..., help='Exact substring to search for, e.g. "open mobile menu".'
    ),
    jql: str | None = typer.Option(
        None,
        "--jql",
        help="Defaults to `project = <issue_key_prefix without trailing ->`.",
    ),
    page_size: int = typer.Option(
        100, "--page-size", help="Max allowed by Xray Cloud is 100."
    ),
) -> None:
    """Find where a step is actually used across Tests.

    Where is this step actually used — the real answer to what Xray's
    BDD Step Library UI shows, since it has no public API (verified via
    live introspection + official docs). Complete and correct, not a
    sampled/best-effort search: every test in scope is checked.
    """
    config = AitlcConfig.find_and_load()
    resolved_jql = jql or f"project = {config.issue_key_prefix.rstrip('-')}"
    client = _build_client(config)

    def _progress(fetched: int, total: int) -> None:
        typer.echo(f"  checked {fetched}/{total} tests...", err=True)

    try:
        matches = client.find_step_usage(
            resolved_jql, step_text, page_size=page_size, progress_callback=_progress
        )
    except XrayError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps({"step_text": step_text, "jql": resolved_jql, "matches": matches})
    )


@app.command("fetch-features")
def fetch_features(
    keys: list[str] = typer.Argument(
        ..., help="Test Execution and/or Test Plan keys, e.g. PROJ-29026"
    ),
    status: str = typer.Option(
        "FAILED", "--status", help="Filter by run status. Use 'all' for every run."
    ),
    out_dir: str | None = typer.Option(
        None, "--out-dir", help="Output dir (default: features/<first-key>-<status>/)."
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Download feature files for the runs inside a Test Execution or Test Plan.

    Wraps the project's own `scripts/fetch_failed_features.py`. Distinct
    from `get-gherkin`, which reads ONE Test's definition: this resolves a
    whole execution/plan and writes runnable .feature files for the runs
    matching `--status` — the "give me everything that failed last night"
    entry point.
    """
    from aitlc.core import behave_runner
    from aitlc.core.dotenv import load_dotenv
    from aitlc.core.script_runner import run_project_script

    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    script = "scripts/fetch_failed_features.py"
    if not (config.root_dir / script).exists():
        typer.echo(json.dumps({"error": f"script not found: {script}"}), err=True)
        raise typer.Exit(code=2)

    args = list(keys) + ["--status", status]
    if out_dir:
        args += ["--out-dir", out_dir]

    secret_values = [
        v
        for name in (
            "lt_access_key",
            "jira_token",
            "jira_xray_client_secret",
            "s3_secret_access_key",
            "s3_session_token",
        )
        if (v := config.env.resolve(name))
    ]

    result = run_project_script(
        script,
        cwd=config.root_dir,
        poetry_cmd=behave_runner.resolve_poetry(),
        args=args,
        secret_values=secret_values,
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))
    raise typer.Exit(code=0 if result.passed else 1)
