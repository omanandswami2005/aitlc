"""`aitlc call` — run one of the project's own functions against a live browser.

The fast loop stops at the Gherkin boundary and real debugging keeps crossing
it. Asserting on a page object's private helper -- "which user does the app
actually think is signed in" -- is not a step, has no Gherkin expression, and
previously meant writing a Playwright script by hand against the same debug
Chrome.
"""

from __future__ import annotations

import json

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, chrome_cdp, step_console

app = typer.Typer(help="Call a project function against the live debug browser.")


@app.callback(invoke_without_command=True)
def call(
    ctx: typer.Context,
    target: str = typer.Argument(
        ..., help="'module:attr', e.g. 'pages.login.sign_in:SignInPage.current_user'."
    ),
    arg: list[str] = typer.Option(
        [], "--arg", help="Positional argument. Repeatable. JSON if it parses, else text."
    ),
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port"),
    cdp_url: str | None = typer.Option(None, "--cdp-url"),
    pass_browser: str = typer.Option(
        "auto",
        "--pass-browser",
        help="Pass the browser handle first: auto (detect from the signature), yes, no.",
    ),
) -> None:
    """Call a project function and print what it returned."""
    if ctx.invoked_subcommand:
        return
    config = AitlcConfig.find_and_load()

    url = cdp_url
    if not url:
        instance = chrome_cdp.load_state(config.root_dir, port)
        url = f"http://127.0.0.1:{instance.port}" if instance else None

    record = step_console.call_project_function(
        target,
        cwd=config.root_dir,
        poetry_cmd=behave_runner.resolve_poetry(),
        args=list(arg),
        cdp_url=url,
        step_dir=config.step_dir,
        scenario_setup=config.scenario_setup,
        browser_actions=config.browser_actions,
        pass_browser=pass_browser,
    )
    typer.echo(json.dumps(record, indent=2))
    raise typer.Exit(code=0 if record.get("event") == "call_result" else 1)


# Mounted by commands/_registry.py.
COMMAND = {"name": "call", "attr": "app", "kind": "group", "order": 120}