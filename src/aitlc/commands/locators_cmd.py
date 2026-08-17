"""`aitlc locators lint` — flag locator shapes known to match the wrong element."""

from __future__ import annotations

import json

import typer
from aitlc.config import AitlcConfig
from aitlc.core import locator_lint

app = typer.Typer(help="Inspect the project's locator definitions.")


@app.command("lint")
def lint(
    severity: str = typer.Option(
        "high",
        "--severity",
        help="Minimum severity to report: high, medium or low (low is noisy by design).",
    ),
    limit: int = typer.Option(40, "--limit", help="Cap the findings printed."),
) -> None:
    """Report risky locators, highest severity first."""
    config = AitlcConfig.find_and_load()
    locators_dir = getattr(config, "locators_dir", None)
    if not locators_dir:
        typer.echo(
            json.dumps({"error": "aitlc.toml has no [project].locators_dir set"}),
            err=True,
        )
        raise typer.Exit(code=2)

    root = config.root_dir / locators_dir
    if not root.exists():
        typer.echo(json.dumps({"error": f"no such directory: {root}"}), err=True)
        raise typer.Exit(code=2)

    wanted = {
        "high": {"high"},
        "medium": {"high", "medium"},
        "low": {"high", "medium", "low"},
    }.get(severity)
    if wanted is None:
        typer.echo(json.dumps({"error": f"unknown severity {severity!r}"}), err=True)
        raise typer.Exit(code=2)

    findings = [
        f
        for f in locator_lint.lint_paths(sorted(root.glob("*.py")))
        if f.severity in wanted
    ]
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1

    typer.echo(
        json.dumps(
            {
                "locators_dir": str(locators_dir),
                "total": len(findings),
                "by_rule": by_rule,
                "findings": [f.__dict__ for f in findings[:limit]],
                "truncated": max(0, len(findings) - limit),
            },
            indent=2,
        )
    )


@app.command("rules")
def rules() -> None:
    """Explain what lint checks, and the failure each rule comes from."""
    typer.echo(
        json.dumps(
            {
                "rules": [
                    {
                        "rule": "positional-index",
                        "severity": "high",
                        "checks": "aria-rowindex / data-rowindex / trailing [n]",
                        "why": (
                            "follows whatever sorts first rather than the record "
                            "you meant; opened an orphan account among three "
                            "same-named ones"
                        ),
                    },
                    {
                        "rule": "grid-cell-without-role",
                        "severity": "high",
                        "checks": "@data-field without a role='cell' guard",
                        "why": (
                            "a grid header carries data-field too, so the "
                            "selector can return the column title"
                        ),
                    },
                    {
                        "rule": "unanchored-xpath",
                        "severity": "low",
                        "checks": "//* or //div with no id/testid/name/aria anchor",
                        "why": (
                            "matches anywhere in the document; with .first it "
                            "silently takes an element from another container"
                        ),
                    },
                ],
                "note": (
                    "Advisory only. Upstream guidance is that .first hides "
                    "ambiguity rather than resolving it, and that filter()/role "
                    "selectors are the fix."
                ),
            },
            indent=2,
        )
    )
