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
import os
from pathlib import Path
from typing import Any

import typer
from aitlc.adapters.s3 import evidence as s3_evidence
from aitlc.core import artifact_cache, journal, test_history, test_lookup, triage
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
    """An S3 client from a named profile when one is configured, else static keys.

    A profile is preferred because botocore resolves it on every call, so an
    SSO session refreshed in another terminal is picked up without editing
    anything. Static keys in a file expire silently and then take precedence
    over the refreshed profile, which makes `aws sso login` look broken.
    """
    profile = os.environ.get("AWS_PROFILE") or config.s3_profile
    if profile:
        return s3_evidence.build_client_from_profile(profile, config.s3_region)
    try:
        access_key = config.require_env("s3_access_key_id")
        secret_key = config.require_env("s3_secret_access_key")
    except ConfigError as exc:
        message = str(exc)
        typer.echo(
            json.dumps(
                {
                    "error": message,
                    "hint": (
                        "set [s3].profile in aitlc.toml (or AWS_PROFILE) to use a "
                        "named AWS profile, including an SSO one, instead of "
                        "static keys that expire"
                    ),
                }
            ),
            err=True,
        )
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
    # Filter first, truncate second. The other order silently hides an older
    # run behind `--limit` newer ones and reports "no report objects matched",
    # which reads as "that run does not exist" -- it cost three guesses at
    # `--limit` before a real run appeared.
    keys = [m.key for m in matches]
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
    keys = keys[:limit]

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


def _behave_objects(client: Any, config: AitlcConfig, name_contains: str) -> list:
    return s3_evidence.find_objects(
        client, config.s3_bucket, name_contains, prefix=config.s3_report_prefix
    )


def _newest_runs(keys: list[str], count: int) -> list[str]:
    """The `count` newest run timestamps present in these keys."""
    stamps = {t for k in keys if (t := triage.run_timestamp(k))}
    return sorted(stamps, reverse=True)[:count]


def _runs_within_days(keys: list[str], days: int) -> list[str]:
    """Every run timestamp falling on one of the `days` most recent dates.

    A suite runs many times a day, so "the last 10 runs" can all share one
    date and collapse a history matrix to a single column -- confirmed live,
    which is why day-scoping is the default for history.
    """
    stamps = sorted({t for k in keys if (t := triage.run_timestamp(k))}, reverse=True)
    dates = sorted({test_history.run_date(s) for s in stamps if test_history.run_date(s)}, reverse=True)
    wanted = set(dates[:days])
    return [s for s in stamps if test_history.run_date(s) in wanted]


@app.command("find-test")
def find_test(
    test_keys: list[str] = typer.Argument(..., help="One or more test keys."),
    runs: int = typer.Option(
        1, "--runs", help="How many recent runs to consider, newest first."
    ),
    name_contains: str = typer.Option("behave_results", "--name-contains"),
) -> None:
    """Locate the run artifacts for one or more test keys, without downloading.

    Answers "which plan ran this, and in which run" from the object listing
    alone. A key that names an execution resolves here for free; a key that
    only exists as a scenario tag inside another file cannot be seen without
    reading the documents, and is reported as needing `verify-test` rather
    than as "did not run" -- confusing those two is what sends someone
    guessing at report titles and downloading megabytes to check.
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    keys = [m.key for m in _behave_objects(client, config, name_contains)]
    wanted_runs = _newest_runs(keys, runs)
    scoped = [k for k in keys if any(stamp in k for stamp in wanted_runs)]

    found: dict[str, list[dict]] = {}
    for test_key in test_keys:
        hits = [k for k in scoped if test_lookup.key_names_test(k, test_key)]
        found[test_key] = [
            info.__dict__
            for k in hits
            if (info := test_lookup.parse_object_key(k)) is not None
        ]

    unresolved = [k for k, v in found.items() if not v]
    payload = {
        "runs_considered": wanted_runs,
        "objects_in_scope": len(scoped),
        "found": found,
    }
    if unresolved:
        payload["not_named_by_any_object"] = unresolved
        payload["hint"] = (
            "these keys are not execution keys; they may still have run as "
            "scenario tags inside another file -- `aitlc s3 verify-test` reads "
            "the documents and will find them"
        )
    typer.echo(json.dumps(payload, indent=2))


@app.command("verify-test")
def verify_test(
    test_keys: list[str] = typer.Argument(..., help="One or more test keys."),
    runs: int = typer.Option(
        1, "--runs", help="How many recent runs to consider, newest first."
    ),
    name_contains: str = typer.Option("behave_results", "--name-contains"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore the artifact cache and re-download."
    ),
) -> None:
    """Did these tests pass in the most recent run, and if not, where did they stop?

    The command that closes the loop after pushing a fix: it resolves each key
    to its run artifact, reads it, and reports pass/fail with the failing step
    and its real error. It deliberately reads the per-test Behave JSON rather
    than the HTML report -- the JSON is orders of magnitude smaller, already
    structured, and carries the scenario tags that make a nested test key
    findable at all.
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    keys = [m.key for m in _behave_objects(client, config, name_contains)]
    wanted_runs = _newest_runs(keys, runs)
    scoped = [k for k in keys if any(stamp in k for stamp in wanted_runs)]

    # Read the named objects first; only fall back to the whole run for keys
    # that no object name accounts for. On a suite where every key is an
    # execution key this downloads exactly as many objects as keys asked for.
    named: dict[str, list[str]] = {
        t: [k for k in scoped if test_lookup.key_names_test(k, t)] for t in test_keys
    }
    to_read = {k for hits in named.values() for k in hits}
    if any(not hits for hits in named.values()):
        to_read = set(scoped)

    documents: list[tuple[str, object]] = []
    for key in sorted(to_read):
        local = None if refresh else artifact_cache.get(config.root_dir, key)
        if local is None:
            raw = s3_evidence.fetch_object_bytes(client, config.s3_bucket, key)
            tmp = config.root_dir / "reports" / ".aitlc" / "artifacts" / "_tmp"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
            local = artifact_cache.put(config.root_dir, key, tmp, source="s3")
            tmp.unlink(missing_ok=True)
        try:
            documents.append((key, json.loads(local.read_text())))
        except json.JSONDecodeError:
            continue

    results = []
    any_failed = False
    for test_key in test_keys:
        best = None
        for key, doc in documents:
            outcome = test_lookup.outcome_for_test(doc, test_key, source=key)
            if outcome.status != "not_found":
                best = outcome
                break
        if best is None:
            best = test_lookup.TestOutcome(test_key=test_key)
        if best.status == "failed":
            any_failed = True
        results.append(best.to_dict())

    payload = {
        "runs_considered": wanted_runs,
        "objects_read": len(documents),
        "results": results,
    }
    typer.echo(json.dumps(payload, indent=2))
    journal.record(
        config.root_dir,
        command="s3 verify-test",
        argv=list(test_keys),
        exit_code=1 if any_failed else 0,
        payload=payload,
        tags=["triage"],
    )
    raise typer.Exit(code=1 if any_failed else 0)


@app.command("history")
def history_compare(
    test_keys: list[str] = typer.Argument(..., help="One or more test keys."),
    days: int = typer.Option(
        7,
        "--days",
        help=(
            "How many recent calendar days to cover. Days, not runs: a suite "
            "executes many times a day, so a run count collapses the matrix "
            "into a single column."
        ),
    ),
    runs: int | None = typer.Option(
        None, "--runs", help="Consider exactly this many recent runs instead of --days."
    ),
    name_contains: str = typer.Option("behave_results", "--name-contains"),
    refresh: bool = typer.Option(False, "--refresh", help="Re-download, ignore cache."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Update the consolidated history file."
    ),
) -> None:
    """How have these tests behaved across the last N runs -- chronic or flaky?

    One run says "it failed". This says whether it fails the *same way* every
    time, which is the thing that decides what to do next: a deterministic
    failure is worth reproducing locally, an intermittent one needs a base rate
    before anyone bisects anything. Answering it by hand took a bespoke script
    every time, and got the Scenario Outline aggregation wrong on the first try.

    The result is also written to `reports/.aitlc/test-history.json`, so the
    next person reads the answer instead of re-downloading every artifact.
    """
    config = AitlcConfig.find_and_load()
    if not config.s3_bucket:
        typer.echo(json.dumps({"error": "aitlc.toml has no [s3].bucket set"}), err=True)
        raise typer.Exit(code=2)

    client = _build_s3_client(config)
    keys = [m.key for m in _behave_objects(client, config, name_contains)]
    if runs is not None:
        wanted_runs = _newest_runs(keys, runs)
    else:
        wanted_runs = _runs_within_days(keys, days)
    scoped = [k for k in keys if any(stamp in k for stamp in wanted_runs)]
    if not scoped:
        typer.echo(json.dumps({"error": "no run artifacts found"}), err=True)
        raise typer.Exit(code=1)

    documents: list[tuple[str, object]] = []
    for key in sorted(scoped):
        local = None if refresh else artifact_cache.get(config.root_dir, key)
        if local is None:
            raw = s3_evidence.fetch_object_bytes(client, config.s3_bucket, key)
            tmp = config.root_dir / "reports" / ".aitlc" / "artifacts" / "_tmp"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
            local = artifact_cache.put(config.root_dir, key, tmp, source="s3")
            tmp.unlink(missing_ok=True)
        try:
            documents.append((key, json.loads(local.read_text())))
        except json.JSONDecodeError:
            continue

    # One outcome per (test, run). A test's scenarios can be spread over more
    # than one object in a run, so outcomes are folded per run rather than
    # taking the first object that mentions the key.
    per_test: dict[str, dict[str, test_history.RunOutcome]] = {t: {} for t in test_keys}
    for key, doc in documents:
        info = test_lookup.parse_object_key(key)
        if info is None:
            continue
        for test_key in test_keys:
            found = test_lookup.outcome_for_test(doc, test_key, source=key)
            if found.status == "not_found":
                continue
            slot = per_test[test_key].get(info.run_timestamp)
            failure = found.failures[0] if found.failures else None
            outcome = test_history.RunOutcome(
                date=test_history.run_date(info.run_timestamp),
                run=info.run_timestamp,
                outcome=found.status,
                plan=info.plan,
                execution_key=info.execution_key,
                step=failure["step"] if failure else "",
                error=failure["error"] if failure else "",
                signature=(
                    test_history.signature_of(failure["step"], failure["error"])
                    if failure
                    else ""
                ),
            )
            # A failure anywhere in the run outranks a pass elsewhere in it.
            if slot is None or (slot.outcome == "passed" and outcome.outcome == "failed"):
                per_test[test_key][info.run_timestamp] = outcome

    by_run: dict[str, list[test_history.RunOutcome]] = {}
    for outcomes in per_test.values():
        for stamp, outcome in outcomes.items():
            by_run.setdefault(stamp, []).append(outcome)
    outages = test_history.mark_infrastructure_runs(by_run)
    for outcomes in per_test.values():
        for stamp, outcome in outcomes.items():
            if stamp in outages:
                outcome.infrastructure = True

    histories = [
        test_history.build_history(test_key, list(per_test[test_key].values()))
        for test_key in test_keys
    ]

    payload = {
        "runs_considered": wanted_runs,
        "objects_read": len(documents),
        "infrastructure_runs": sorted(outages),
        "matrix": test_history.matrix(histories),
        "tests": [h.to_dict() for h in histories],
    }
    if store:
        path = test_history.default_store(config.root_dir)
        test_history.merge_into_store(path, histories)
        payload["stored_at"] = str(path.relative_to(config.root_dir))

    typer.echo(json.dumps(payload, indent=2))
    journal.record(
        config.root_dir,
        command="s3 history",
        argv=list(test_keys),
        exit_code=0,
        payload={"runs": wanted_runs, "tests": [h.test_key for h in histories]},
        tags=["triage"],
    )
