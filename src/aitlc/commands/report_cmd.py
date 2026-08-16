"""`aitlc report <test-ids...> --out FILE.html` (FR-5)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, terminal_replay
from aitlc.core.dotenv import load_dotenv

PAGE_CSS = """
:root {
  --bg: #f4f4f5; --panel-bg: #1e1e1e; --panel-header: #2d2d2d;
  --text-muted: #52525b; --card-border: #d4d4d8; --heading: #18181b;
  --dot-red: #ff5f56; --dot-yellow: #ffbd2e; --dot-green: #27c93f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0b0b0d; --card-border: #2a2a2e; --heading: #f4f4f5; --text-muted: #9a9aa2;
  }
}
:root[data-theme="dark"] {
  --bg: #0b0b0d; --card-border: #2a2a2e; --heading: #f4f4f5; --text-muted: #9a9aa2;
}
body {
  background: var(--bg); color: var(--heading);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 0; padding: 32px 20px 60px;
}
.wrap { max-width: 900px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--text-muted); font-size: 14px; margin: 0 0 28px; }
.card { border: 1px solid var(--card-border); border-radius: 10px; overflow: hidden; margin-bottom: 28px; }
.card-title { padding: 14px 18px 6px; font-size: 15px; font-weight: 600; }
.termbar { background: var(--panel-header); padding: 9px 12px; display: flex; align-items: center; gap: 6px; }
.termbar .dot { width: 11px; height: 11px; border-radius: 50%; }
.termbar .r { background: var(--dot-red); }
.termbar .y { background: var(--dot-yellow); }
.termbar .g { background: var(--dot-green); }
.termbar .label { margin-left: 10px; color: #999; font-size: 12px; font-family: ui-monospace, monospace; }
.term {
  background: var(--panel-bg); color: #d4d4d4;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px; line-height: 1.55; padding: 14px 18px 18px;
  white-space: pre-wrap; word-break: break-word; overflow-x: auto;
  max-height: 500px; overflow-y: auto;
}
.summary { padding: 10px 18px 16px; font-size: 12.5px; color: var(--text-muted); }
.summary b { color: var(--heading); }
"""


def _render_page(entries: list[terminal_replay.TestReportEntry]) -> str:
    cards = []
    for entry in entries:
        dot_color = "g" if entry.passed else "r"
        cards.append(f"""
  <div class="card">
    <div class="card-title">{entry.test_id}</div>
    <div class="termbar"><span class="dot {dot_color}"></span><span class="dot y"></span><span class="dot r"></span>
      <span class="label">behave — {entry.test_id}</span></div>
    <div class="term">{entry.terminal_html}</div>
    <div class="summary">{entry.summary}</div>
  </div>""")
    return f"""<title>aitlc report</title>
<style>{PAGE_CSS}</style>
<div class="wrap">
  <h1>aitlc report</h1>
  <p class="sub">Real captured behave terminal output, replayed through a
  real terminal emulator so redraw sequences resolve correctly.</p>
  {"".join(cards)}
</div>"""


def report(
    test_ids: list[str] = typer.Argument(..., help="One or more test IDs."),
    out: Path = typer.Option(..., "--out", help="Output HTML file path."),
    env_file: str = typer.Option(".env", "--env-file"),
    cols: int = typer.Option(
        80, "--cols", help="Terminal replay width — must match the real capture width."
    ),
) -> None:
    """Run a test and capture a replayable terminal recording."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)

    entries: list[terminal_replay.TestReportEntry] = []
    log_dir = Path(tempfile.mkdtemp(prefix="aitlc_report_logs_"))

    for test_id in test_ids:
        feature_path = config.resolve_feature_path(test_id)
        if feature_path is None:
            entries.append(
                terminal_replay.TestReportEntry(
                    test_id=test_id,
                    passed=False,
                    summary=f"Could not resolve a feature file for '{test_id}'",
                    terminal_html="",
                )
            )
            continue

        report_json_dir = Path(tempfile.mkdtemp(prefix="aitlc_report_json_"))
        report_json_path = report_json_dir / f"{feature_path.stem}.report.json"
        cmd = behave_runner.build_command(feature_path, report_json_path)

        log_path = log_dir / f"{test_id}.log"
        typer.echo(f"Running {test_id}...", err=True)
        exit_code = terminal_replay.capture(cmd, cwd=config.root_dir, log_path=log_path)

        result = behave_runner.parse_report(report_json_path)
        result.exit_code = exit_code

        try:
            terminal_html = terminal_replay.replay_to_html(log_path, cols=cols)
        except terminal_replay.CaptureError as exc:
            terminal_html = f"(no captured output: {exc})"

        summary = (
            f"<b>{sum(result.steps_by_status.values())} steps</b>, "
            f"{result.steps_by_status.get('passed', 0)} passed, "
            f"{len(result.failures)} failed"
        )
        entries.append(
            terminal_replay.TestReportEntry(
                test_id=test_id,
                passed=result.passed,
                summary=summary,
                terminal_html=terminal_html,
            )
        )

    out.write_text(_render_page(entries))
    typer.echo(f"Wrote {out}")
