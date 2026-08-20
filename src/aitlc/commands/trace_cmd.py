"""`aitlc trace ...` — Playwright trace evidence commands."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer
from aitlc.adapters.s3 import evidence as s3_evidence
from aitlc.config import AitlcConfig, ConfigError
from aitlc.core.dotenv import load_dotenv
from aitlc.core.trace_evidence import (
    TraceEvidenceError,
    extract_last_frame,
    list_screencast_frames,
)

app = typer.Typer(help="Extract or view evidence from a Playwright trace .zip.")


@app.callback()
def _load_env(
    env_file: str = typer.Option(
        ".env",
        "--env-file",
        help="Load env vars (S3 credentials, etc.) from this file.",
    ),
) -> None:
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)


@app.command("extract-frame")
def extract_frame(
    trace_zip: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(..., "--out", help="Where to save the extracted JPEG."),
) -> None:
    """Extract the final trace frame as a static image.

    Fast path: pull the last screencast frame as a static image, no
    interactive viewer needed (FR-5's cheaper escalation tier).
    """
    try:
        result_path = extract_last_frame(trace_zip, out)
    except TraceEvidenceError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"extracted": str(result_path)}))


@app.command("list-frames")
def list_frames(trace_zip: Path = typer.Argument(..., exists=True)) -> None:
    """List the screencast frames inside a trace archive."""
    try:
        frames = list_screencast_frames(trace_zip)
    except TraceEvidenceError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "count": len(frames),
                "frames": [
                    {"path": f.zip_path, "epoch_ms": f.epoch_ms} for f in frames
                ],
            }
        )
    )


@app.command("fetch-s3")
def fetch_s3(
    test_id: str = typer.Argument(
        ..., help="Test ID to search S3 for (e.g. PROJ-32054)."
    ),
    out: Path = typer.Option(
        ..., "--out", help="Where to save the extracted last-frame JPEG."
    ),
    prefix: str = typer.Option(
        "", "--prefix", help="Optional S3 key prefix to narrow the search."
    ),
) -> None:
    """Find the newest trace in S3 matching test_id, extract its last frame.

    Reports "none found" plainly, not as an error — real CI-only upload means an ad-hoc
    run's test ID often has nothing in S3 yet; that is an expected outcome, not a
    failure (see adapters/s3/evidence.py).
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)
    try:
        access_key = config.require_env("s3_access_key_id")
        secret_key = config.require_env("s3_secret_access_key")
    except ConfigError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=2) from exc
    session_token = config.env.resolve("s3_session_token")

    client = s3_evidence.build_client(
        access_key, secret_key, session_token, config.s3_region
    )
    matches = s3_evidence.find_trace_keys(
        client, config.s3_bucket, test_id, prefix=prefix
    )
    if not matches:
        typer.echo(
            json.dumps(
                {
                    "found": False,
                    "detail": (
                        f"No S3 trace found for '{test_id}' — trace upload is "
                        "typically real-CI-only, so this is expected for an "
                        "ad-hoc local/remote run. Try `aitlc trace extract-frame` "
                        "against a local reports/traces/*.zip instead."
                    ),
                }
            )
        )
        raise typer.Exit(code=1)

    newest = matches[0]
    tmp_zip = Path(tempfile.mkdtemp(prefix="aitlc_s3_trace_")) / "trace.zip"
    try:
        s3_evidence.download_trace(client, config.s3_bucket, newest.key, tmp_zip)
        result_path = extract_last_frame(tmp_zip, out)
    except (s3_evidence.S3EvidenceError, TraceEvidenceError) as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps({"found": True, "s3_key": newest.key, "extracted": str(result_path)})
    )


@app.command("show")
def show(trace_zip: Path = typer.Argument(..., exists=True)) -> None:
    """Open the full interactive trace viewer.

    Full interactive replay — the expensive escalation tier for when a
    single frame doesn't explain the failure and the actual DOM/network
    sequence matters. Passes through to
    Playwright's own viewer rather than reimplementing it.
    """
    playwright_bin = shutil.which("playwright")
    if not playwright_bin:
        typer.echo(
            json.dumps({"error": "`playwright` CLI not found on PATH"}), err=True
        )
        raise typer.Exit(code=2)
    proc = subprocess.run([playwright_bin, "show-trace", str(trace_zip)])
    raise typer.Exit(code=proc.returncode)


# Mounted by commands/_registry.py.
COMMAND = {"name": "trace", "attr": "app", "kind": "group", "order": 170}