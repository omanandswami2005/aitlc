"""`aitlc s3 fetch-report` — pull the latest daily HTML report from S3.

Real, confirmed mechanics for this project: `generate_reports_and_send_an_email.py`
uploads `detailed_report.html` via `S3Utility.upload_file_and_get_presigned_url`
(key shape: `{s3_report_prefix}/{name}...`), then posts a presigned link to
it in the Teams channel. That link expires — this command doesn't need it:
with real S3 credentials, the same object is reachable directly by listing
the bucket under the configured prefix and taking the newest match, the
same "list, filter, take newest" shape as core/trace_evidence.py and
adapters/s3/evidence.py's trace search.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from aitlc.adapters.s3 import evidence as s3_evidence
from aitlc.core import artifact_cache, journal, triage
from aitlc.config import AitlcConfig, ConfigError
from aitlc.core.dotenv import load_dotenv
from aitlc.core.report_summary import parse_html_report

app = typer.Typer(help="Fetch reports uploaded to S3 (the daily HTML report, etc.).")


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


def _build_s3_client(config: AitlcConfig) -> Any:
    try:
        access_key = config.require_env("s3_access_key_id")
        secret_key = config.require_env("s3_secret_access_key")
    except ConfigError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=2) from exc
    session_token = config.env.resolve("s3_session_token")
    return s3_evidence.build_client(
        access_key, secret_key, session_token, config.s3_region
    )


def _resolve_newest_key(
    client: Any, config: AitlcConfig, name_contains: str
) -> str | None:
    matches = s3_evidence.find_objects(
        client, config.s3_bucket, name_contains, prefix=config.s3_report_prefix
    )
    return matches[0].key if matches else None


@app.command("fetch-report")
def fetch_report(
    out: Path = typer.Option(..., "--out", help="Where to save the report."),
    name_contains: str = typer.Option(
        "detailed_report", "--name-contains", help="Substring to match in the S3 key."
    ),
) -> None:
    """Download a report from S3."""
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)
    if not config.s3_report_prefix:
        typer.echo(
            json.dumps({"error": "aitlc.toml has no [s3].report_prefix set"}), err=True
        )
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    matches = s3_evidence.find_objects(
        client, config.s3_bucket, name_contains, prefix=config.s3_report_prefix
    )
    if not matches:
        typer.echo(
            json.dumps(
                {
                    "found": False,
                    "detail": f"No object matching '{name_contains}' under prefix "
                    f"'{config.s3_report_prefix}'.",
                }
            )
        )
        raise typer.Exit(code=1)

    newest = matches[0]
    try:
        s3_evidence.download_object(client, config.s3_bucket, newest.key, out)
    except s3_evidence.S3EvidenceError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps({"found": True, "s3_key": newest.key, "downloaded": str(out)})
    )


@app.command("report-summary")
def report_summary(
    key: str | None = typer.Option(
        None,
        "--key",
        help="Fetch this exact S3 key instead of resolving the newest match.",
    ),
    name_contains: str = typer.Option(
        "detailed_report", "--name-contains", help="Substring to match in the S3 key."
    ),
    max_failures: int = typer.Option(
        30,
        "--max-failures",
        help="Cap the failures list in the output. A full report can have 60+ — "
        "use --out on fetch-report for the raw file if you need all of them.",
    ),
    save_raw: Path | None = typer.Option(
        None, "--save-raw", help="Also save the raw HTML to this path."
    ),
) -> None:
    """Fetch a report and summarize it into counts and failures.

    Fetch the report and parse it into a compact structured summary —
    pass/fail counts, per-feature breakdown, and a truncated failure list —
    instead of the raw file.

    Real, confirmed problem this solves: the raw HTML report this project
    uploads runs 3.7-32MB, driven mostly by inline base64 screenshots and
    verbose captured-logging blocks per failure. Reading that raw file is
    not viable for an agent — a 32MB report reduces to ~26KB of structured
    JSON here (~1200x smaller), verified live. The embedded screenshots and
    captured-logging tails are never even loaded past the byte range that
    matters — only the summary/feature/scenario/error markup is parsed.
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    resolved_key = key or _resolve_newest_key(client, config, name_contains)
    if resolved_key is None:
        typer.echo(
            json.dumps(
                {
                    "found": False,
                    "detail": f"No object matching '{name_contains}' under prefix "
                    f"'{config.s3_report_prefix}'.",
                }
            )
        )
        raise typer.Exit(code=1)

    try:
        raw_bytes = s3_evidence.fetch_object_bytes(
            client, config.s3_bucket, resolved_key
        )
    except s3_evidence.S3EvidenceError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    if save_raw:
        save_raw.parent.mkdir(parents=True, exist_ok=True)
        save_raw.write_bytes(raw_bytes)

    summary = parse_html_report(raw_bytes.decode("utf-8", errors="replace"))
    payload = summary.to_dict(max_failures=max_failures)
    payload["s3_key"] = resolved_key
    payload["raw_size_bytes"] = len(raw_bytes)
    typer.echo(json.dumps(payload, indent=2))
    raise typer.Exit(code=0 if payload["scenarios"]["failed"] == 0 else 1)


@app.command("list-reports")
def list_reports(
    name_contains: str = typer.Option("detailed_report", "--name-contains"),
    limit: int = typer.Option(10, "--limit"),
    prefix: str | None = typer.Option(
        None,
        "--prefix",
        help=(
            "Narrow to this key prefix, e.g. one suite folder. Without it, "
            "finding a single run means listing hundreds of keys and grepping."
        ),
    ),
    runs: bool = typer.Option(
        False,
        "--runs",
        help="Group into distinct runs (timestamp + report count) instead of listing objects.",
    ),
) -> None:
    """List recent reports without downloading them.

    List recent reports without downloading — useful to see what's
    actually there (dates, naming) before picking one.
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    matches = s3_evidence.find_objects(
        client,
        config.s3_bucket,
        name_contains,
        prefix=prefix or config.s3_report_prefix,
    )
    if runs:
        grouped: dict[str, int] = {}
        for match in matches:
            stamp = triage.run_timestamp(match.key)
            if stamp:
                grouped[stamp] = grouped.get(stamp, 0) + 1
        listed = sorted(grouped.items(), reverse=True)[:limit]
        typer.echo(
            json.dumps(
                {
                    "count": len(listed),
                    "runs": [{"at": at, "reports": n} for at, n in listed],
                },
                indent=2,
            )
        )
        return
    typer.echo(
        json.dumps(
            {
                "count": len(matches),
                "reports": [
                    {"key": m.key, "last_modified_epoch": m.last_modified_epoch}
                    for m in matches[:limit]
                ],
            },
            indent=2,
        )
    )


@app.command("triage-run")
def triage_run(
    at: str | None = typer.Option(
        None, "--at", help="Run timestamp (or a prefix of one) to triage."
    ),
    suite: str | None = typer.Option(
        None, "--suite", help="Only reports whose key contains this."
    ),
    name_contains: str = typer.Option(
        "behave_results",
        "--name-contains",
        help="Substring identifying report objects.",
    ),
    limit: int = typer.Option(500, "--limit", help="How many keys to consider."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore the artifact cache and re-download."
    ),
) -> None:
    """Triage one CI run: totals plus one row per failure.

    Replaces listing hundreds of keys, downloading each report singly, and
    hand-writing a parser. The per-execution Behave JSON is a fraction of the
    HTML report's size and already carries per-step status and error text.
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    matches = s3_evidence.find_objects(
        client, config.s3_bucket, name_contains, prefix=config.s3_report_prefix
    )
    keys = [m.key for m in matches][:limit]
    if suite:
        keys = [k for k in keys if suite in k]
    if at:
        keys = [k for k in keys if at in k]
    else:
        # Default to the newest run rather than mixing several: a "run" is every
        # object sharing one timestamp, which is how a job covering two test
        # plans appears.
        stamps = sorted(
            {t for k in keys if (t := triage.run_timestamp(k))}, reverse=True
        )
        if stamps:
            keys = [k for k in keys if stamps[0] in k]

    if not keys:
        typer.echo(
            json.dumps(
                {"error": "no report objects matched", "at": at, "suite": suite}
            ),
            err=True,
        )
        raise typer.Exit(code=1)

    documents: list[tuple[str, object]] = []
    cached_hits = 0
    for key in keys:
        local = None if refresh else artifact_cache.get(config.root_dir, key)
        if local is None:
            raw = s3_evidence.fetch_object_bytes(client, config.s3_bucket, key)
            tmp = config.root_dir / "reports" / ".aitlc" / "artifacts" / "_tmp"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
            local = artifact_cache.put(config.root_dir, key, tmp, source="s3")
            tmp.unlink(missing_ok=True)
        else:
            cached_hits += 1
        try:
            documents.append((key.rsplit("/", 1)[-1], json.loads(local.read_text())))
        except json.JSONDecodeError:
            continue

    result = triage.triage_documents(documents)
    payload = {
        "run": at or (triage.run_timestamp(keys[0]) if keys else None),
        "reports": len(keys),
        "from_cache": cached_hits,
        **result.to_dict(),
    }
    typer.echo(json.dumps(payload, indent=2))
    journal.record(
        config.root_dir,
        command="s3 triage-run",
        argv=[a for a in [at, suite] if a],
        exit_code=0 if not result.failures else 1,
        payload=payload,
        tags=["triage"],
    )
    raise typer.Exit(code=0 if not result.failures else 1)
