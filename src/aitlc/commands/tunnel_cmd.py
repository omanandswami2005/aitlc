"""`aitlc tunnel status` / `aitlc tunnel restart` (FR-9)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.adapters.lambdatest import tunnel as tunnel_adapter
from aitlc.config import AitlcConfig, ConfigError
from aitlc.core.dotenv import load_dotenv

app = typer.Typer(help="LT tunnel health check and restart.")

DEFAULT_LOG_PATH = (
    "/tmp/lt_tunnel.log"  # nosec B108 - the LT binary's own default log location
)
DEFAULT_BINARY_PATH = Path.home() / ".lambdatest" / "LT"


@app.callback()
def _load_env(
    env_file: str = typer.Option(
        ".env",
        "--env-file",
        help="Load env vars (LT credentials, etc.) from this file.",
    ),
) -> None:
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)


@app.command("status")
def status(
    log_file: str = typer.Option(DEFAULT_LOG_PATH, "--log-file"),
) -> None:
    """Report whether the tunnel is healthy."""
    result = tunnel_adapter.check_status(Path(log_file))
    typer.echo(json.dumps({"healthy": result.healthy, "detail": result.detail}))
    raise typer.Exit(code=0 if result.healthy else 1)


@app.command("restart")
def restart(
    log_file: str = typer.Option(DEFAULT_LOG_PATH, "--log-file"),
    binary_path: Path = typer.Option(DEFAULT_BINARY_PATH, "--binary"),
    timeout: float = typer.Option(
        20.0, "--timeout", help="Seconds to wait for a healthy relaunch."
    ),
) -> None:
    """Restart the tunnel and wait for it to come back."""
    config = AitlcConfig.find_and_load()
    try:
        username = config.require_env("lt_username")
        access_key = config.require_env("lt_access_key")
    except ConfigError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=2) from exc

    tunnel_name = config.lambdatest.tunnel_name
    if not tunnel_name:
        typer.echo(
            json.dumps({"error": "aitlc.toml has no [lambdatest].tunnel_name set"}),
            err=True,
        )
        raise typer.Exit(code=2)

    result = tunnel_adapter.restart(
        binary_path=binary_path,
        username=username,
        access_key=access_key,
        tunnel_name=tunnel_name,
        log_path=Path(log_file),
        poll_timeout_s=timeout,
    )
    typer.echo(json.dumps({"healthy": result.healthy, "detail": result.detail}))
    raise typer.Exit(code=0 if result.healthy else 1)
