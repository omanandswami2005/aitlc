"""`aitlc users ...` — wrappers for the project's DynamoDB user-pool scripts.

These wrap `scripts/user_validator.py` and
`scripts/multi_threaded_user_generator.py`, which are the tooling for the
pooled test-user lifecycle. Both take **no arguments** — they read the
DynamoDB table name from the project's own config and act on every row —
so these wrappers deliberately do not invent flags the scripts cannot
honor.

Both mutate shared state (they create, verify and delete users in a shared
DynamoDB table used by CI), so each requires explicit confirmation. That
guard is the point of wrapping them: `poetry run python3
scripts/user_validator.py` deletes users the moment it is typed, with no
prompt and no dry run.
"""

from __future__ import annotations

import json

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner
from aitlc.core.dotenv import load_dotenv
from aitlc.core.script_runner import run_project_script

app = typer.Typer(help="Manage the pooled DynamoDB test users.")

_SECRET_KEYS = (
    "lt_access_key",
    "jira_token",
    "jira_xray_client_secret",
    "s3_secret_access_key",
    "s3_session_token",
)


def _secret_values(config: AitlcConfig) -> list[str]:
    return [v for key in _SECRET_KEYS if (v := config.env.resolve(key))]


def _run(script: str, env_file: str, confirmed: bool, action: str) -> None:
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    script_file = config.root_dir / script
    if not script_file.exists():
        typer.echo(json.dumps({"error": f"script not found: {script}"}), err=True)
        raise typer.Exit(code=2)

    if not confirmed:
        typer.echo(
            json.dumps(
                {
                    "error": f"{action} mutates the shared DynamoDB user pool",
                    "hint": "re-run with --yes once you are sure",
                    "script": script,
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    result = run_project_script(
        script,
        cwd=config.root_dir,
        poetry_cmd=behave_runner.resolve_poetry(),
        secret_values=_secret_values(config),
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))
    raise typer.Exit(code=0 if result.passed else 1)


@app.command("validate")
def validate(
    yes: bool = typer.Option(
        False, "--yes", help="Confirm — this DELETES users that fail validation."
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Validate pooled users; deletes ones with a wrong license status or >2h old."""
    _run("scripts/user_validator.py", env_file, yes, "user validation")


@app.command("generate")
def generate(
    yes: bool = typer.Option(False, "--yes", help="Confirm — this CREATES real users."),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Create the pooled users currently listed in the DynamoDB table."""
    _run("scripts/multi_threaded_user_generator.py", env_file, yes, "user generation")


# Mounted by commands/_registry.py.
COMMAND = {"name": "users", "attr": "app", "kind": "group", "order": 210}