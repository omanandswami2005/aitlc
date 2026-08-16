"""`aitlc classify-failure` — pattern-match a run's failures (FR-6)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core.patterns import PatternLibrary


def classify_failure(
    report_json: Path = typer.Argument(
        ...,
        exists=True,
        help="Path to an `aitlc run --json` output file, or a raw report.json.",
    ),
    patterns_file: Path = typer.Option(
        None,
        "--patterns",
        help="Path to patterns.yaml. Defaults to <config root>/patterns.yaml.",
    ),
) -> None:
    """Match a failure against the known-pattern library."""
    config = AitlcConfig.find_and_load()
    resolved_patterns_path = patterns_file or (config.root_dir / "patterns.yaml")
    if not resolved_patterns_path.exists():
        typer.echo(
            json.dumps(
                {"error": f"No patterns.yaml found at {resolved_patterns_path}"}
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    library = PatternLibrary.load(resolved_patterns_path)

    payload = json.loads(report_json.read_text())
    failures = payload.get("failures", [])

    results = []
    any_unmatched = False
    for failure in failures:
        step = failure.get("step", "")
        error = failure.get("error", "")
        match = library.classify(step, error)
        if match:
            results.append({"step": step, "error": error, "match": match.to_dict()})
        else:
            any_unmatched = True
            results.append({"step": step, "error": error, "match": None})

    typer.echo(json.dumps({"results": results}, indent=2))
    # Non-zero when anything is unmatched — this is the explicit "does not
    # match any known pattern" signal FR-1.7's retry wrapper depends on
    # (SRS FR-6.2: report "no match" distinctly from a match).
    raise typer.Exit(code=1 if any_unmatched else 0)
