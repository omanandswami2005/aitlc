"""`aitlc preflight` — what will differ if this feature runs here.

"It passes locally" and "it fails in CI" can only be compared when both are
running the same thing. Three differences make that untrue silently, and all
three are readable from the feature text before a browser is launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import fidelity

app = typer.Typer(help="Check a feature will run here the way it runs in CI.")

# Tags a project's hooks typically key on. Overridable in config; the point is
# to name them somewhere rather than have every reader rediscover which tags
# select setup.
DEFAULT_HOOK_TAGS = ["skip_login", "freemium_user", "trial_user", "subscription_user"]


@app.callback(invoke_without_command=True)
def preflight(
    ctx: typer.Context,
    test_id: str = typer.Argument(
        None,
        help="Test ID or path to a .feature file. Omit to use the project's "
        "default feature ([project].default_feature, or the sole *.feature in feature_dir).",
    ),
    hook_tag: list[str] = typer.Option(
        [],
        "--hook-tag",
        help="A feature-level tag the project's hooks read. Repeatable.",
    ),
) -> None:
    """Report how a local run of this feature would differ from a full execution."""
    if ctx.invoked_subcommand:
        return
    config = AitlcConfig.find_and_load()
    if not test_id:
        test_id = config.default_feature_id()
        if not test_id:
            typer.echo(
                json.dumps(
                    {
                        "error": "no test id given and no feature found",
                        "hint": f"pass a test id/path, or put one *.feature in "
                        f"'{config.feature_dir}' (or set [project].default_feature)",
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)

    candidate = Path(test_id)
    path = candidate if candidate.suffix == ".feature" else config.resolve_feature_path(test_id)
    if path is None or not Path(path).exists():
        typer.echo(json.dumps({"error": f"no feature file for {test_id!r}"}), err=True)
        raise typer.Exit(code=2)

    report = fidelity.analyze(
        Path(path).read_text(encoding="utf-8"),
        hook_tags=list(hook_tag) or DEFAULT_HOOK_TAGS,
    )
    payload = {"feature": str(path), **report.to_dict()}
    typer.echo(json.dumps(payload, indent=2))
    # Non-zero when something would genuinely run differently, so this can gate
    # a script that is about to claim "reproduced locally".
    raise typer.Exit(code=0 if report.faithful else 1)


# Mounted by commands/_registry.py.
COMMAND = {"name": "preflight", "attr": "app", "kind": "group", "order": 130}