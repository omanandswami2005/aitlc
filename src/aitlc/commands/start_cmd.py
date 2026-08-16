"""`aitlc start <test-id>` — one-command bootstrap for picking up a failing test cold.

Ties together nearly everything else in the tool into one artifact: a
session folder with the real feature file and a generated context.md that
front-loads exactly what a human or agent needs before touching anything —
the same information this whole tool's own build session had to discover
the hard way, one command at a time, across real debugging. Every section
degrades gracefully (a missing Xray/S3 config produces a clear "not
available: <reason>" note, never a crash) since most of these integrations
are genuinely optional depending on the project.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from aitlc.adapters.xray.client import XrayClient, XrayError
from aitlc.commands.doctor import _check_device_mobile_mismatch
from aitlc.config import AitlcConfig
from aitlc.core.dotenv import load_dotenv
from aitlc.core.patterns import PatternLibrary
from aitlc.core.playbook import PLAYBOOK_MD
from aitlc.core.trace_evidence import TraceEvidenceError, extract_last_frame

app = typer.Typer(help="Bootstrap a debugging session folder for one test.")


def _doctor_section(config: AitlcConfig, feature_path: Path) -> str:
    result = _check_device_mobile_mismatch(config, feature_path)
    icon = "✅" if result.ok else "⚠️"
    return f"{icon} **{result.name}**: {result.detail}"


def _gherkin_drift_section(
    config: AitlcConfig, test_id: str, feature_path: Path
) -> str:
    try:
        client_id = config.require_env("jira_xray_client_id")
        client_secret = config.require_env("jira_xray_client_secret")
    except Exception:
        return "*Not available — Xray credentials not configured in aitlc.toml/.env.*"

    try:
        client = XrayClient.from_client_credentials(
            graphql_url=config.xray_graphql_url,
            client_id=client_id,
            client_secret=client_secret,
        )
        result = client.compare_gherkin(test_id, feature_path.read_text())
    except XrayError as exc:
        return f"*Could not compare — {exc}*"

    if result.in_sync:
        return "✅ Local `.feature` file matches live Xray Gherkin — no drift."
    diff_block = "\n".join(result.diff[:40])
    return (
        "⚠️ **Local file has drifted from live Xray.** This exact situation cost real "
        "debugging time earlier — a locally-edited step that no longer exists live, or a "
        "live change never pulled down locally. Reconcile before trusting the local file:\n\n"
        f"```diff\n{diff_block}\n```"
    )


def _classification_section(config: AitlcConfig, prior_report: Path | None) -> str:
    if prior_report is None:
        return (
            "*No prior run data given — pass `--report <path>` (an `aitlc run --json` "
            "output) to classify a known failure, or run the test fresh first.*"
        )
    patterns_path = config.root_dir / "patterns.yaml"
    if not patterns_path.exists():
        return "*No patterns.yaml found — skipping known-flake classification.*"

    try:
        payload = json.loads(prior_report.read_text())
        library = PatternLibrary.load(patterns_path)
    except Exception as exc:
        return f"*Could not classify — {exc}*"

    failures = payload.get("failures", [])
    if not failures:
        return "*Prior report has no failures recorded.*"

    lines = []
    for failure in failures:
        match = library.classify(failure.get("step", ""), failure.get("error", ""))
        if match:
            lines.append(
                f"- **{failure['step']}** → known: `{match.pattern.id}` — "
                f"{match.pattern.suggested_action}"
            )
        else:
            lines.append(
                f"- **{failure['step']}** → ⚠️ **no known pattern matched** — "
                "this needs real investigation, not a retry."
            )
    return "\n".join(lines)


def _evidence_section(config: AitlcConfig, test_id: str, session_dir: Path) -> str:
    traces_dir = config.root_dir / "reports" / "traces"
    if not traces_dir.exists():
        return "*No local reports/traces/ directory found.*"

    matches = sorted(traces_dir.glob(f"*{test_id}*.zip"), reverse=True)
    if not matches:
        return f"*No local trace found for {test_id}.*"

    newest = matches[0]
    out_path = session_dir / "evidence_last_frame.jpg"
    try:
        extract_last_frame(newest, out_path)
    except TraceEvidenceError as exc:
        return f"*Found trace {newest.name} but could not extract a frame — {exc}*"
    return f"Extracted last frame from `{newest.name}` → `{out_path.relative_to(session_dir)}`"


@app.callback(invoke_without_command=True)
def start(
    test_id: str = typer.Argument(..., help="Test ID to bootstrap a session for."),
    out_dir: Path = typer.Option(Path(".aitlc-sessions"), "--out-dir"),
    prior_report: Path | None = typer.Option(
        None,
        "--report",
        help="An `aitlc run --json` output to classify known failures against.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Emit a bootstrap briefing for a fresh agent or engineer."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    feature_path = config.resolve_feature_path(test_id)
    if feature_path is None:
        typer.echo(
            json.dumps({"error": f"Could not resolve feature for '{test_id}'"}),
            err=True,
        )
        raise typer.Exit(code=2)

    session_dir = out_dir / test_id
    session_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(feature_path, session_dir / feature_path.name)

    doctor_section = _doctor_section(config, feature_path)
    drift_section = _gherkin_drift_section(config, test_id, feature_path)
    classification_section = _classification_section(config, prior_report)
    evidence_section = _evidence_section(config, test_id, session_dir)

    context_md = f"""# {test_id} — Debugging Context

Generated by `aitlc start`. Feature file copied alongside this doc:
[`{feature_path.name}`](./{feature_path.name})

## 1. Preflight

{doctor_section}

## 2. Gherkin drift (local vs. live Xray)

{drift_section}

## 3. Known-flake classification

{classification_section}

## 4. Evidence

{evidence_section}

## 5. {PLAYBOOK_MD.splitlines()[0].lstrip('#').strip()}

{chr(10).join(PLAYBOOK_MD.splitlines()[1:]).lstrip()}

## 6. When you're done

- [ ] Fix applied following the minimal-change-first rule above (§5.2)
- [ ] Verified with a full, real run: `aitlc run {test_id}`
- [ ] Evidence generated for the fix: `aitlc report {test_id} --out {test_id}-proof.html`
"""

    context_path = session_dir / "context.md"
    context_path.write_text(context_md)

    typer.echo(
        json.dumps(
            {
                "session_dir": str(session_dir),
                "feature_file": str(session_dir / feature_path.name),
                "context_md": str(context_path),
            }
        )
    )
