"""`aitlc jira create-task` (FR-7)."""

from __future__ import annotations

import json

import typer
from aitlc.adapters.jira.tasks import JiraTaskError, create_task
from aitlc.config import AitlcConfig, ConfigError
from aitlc.core.dotenv import load_dotenv

app = typer.Typer(help="Plain Jira board Task creation.")


@app.callback()
def _load_env(
    env_file: str = typer.Option(
        ".env",
        "--env-file",
        help="Load env vars (Jira credentials, etc.) from this file.",
    ),
) -> None:
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)


@app.command("create-task")
def create_task_cmd(
    project_key: str = typer.Option(
        ..., "--project", help="Jira project key, e.g. PROJ."
    ),
    summary: str = typer.Option(..., "--summary"),
    description: str = typer.Option("", "--description"),
) -> None:
    """Create a Jira Task."""
    config = AitlcConfig.find_and_load()
    if not config.jira_server_url:
        typer.echo(
            json.dumps({"error": "aitlc.toml has no [jira].server_url set"}),
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        email = config.require_env("jira_email")
        token = config.require_env("jira_token")
    except ConfigError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=2) from exc

    try:
        task = create_task(
            server_url=config.jira_server_url,
            email=email,
            api_token=token,
            project_key=project_key,
            summary=summary,
            description=description,
        )
    except JiraTaskError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps({"key": task.key, "url": task.url}))
