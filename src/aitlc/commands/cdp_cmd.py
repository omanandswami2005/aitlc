"""`aitlc cdp ...` — launch/inspect/stop a long-lived CDP debug Chrome (FR-4)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import chrome_cdp
from aitlc.core import cdp_attach
from aitlc.core.cdp_attach import inspect as cdp_inspect

app = typer.Typer(help="Launch, inspect and stop a live Chromium instance over CDP.")


@app.command("inspect")
def inspect(
    cdp_url: str | None = typer.Option(
        None, "--cdp-url", help="e.g. http://127.0.0.1:9333. Defaults to --port."
    ),
    port: int = typer.Option(
        chrome_cdp.DEFAULT_PORT,
        "--port",
        help="Use the tracked instance on this port. Every other cdp command "
        "takes --port; requiring a URL here only for inspect is a trap.",
    ),
    screenshot: Path | None = typer.Option(
        None, "--screenshot", help="Path to save a screenshot."
    ),
    check: str | None = typer.Option(
        None,
        "--check",
        help="Comma-separated selectors (CSS, text, or xpath) to check.",
    ),
    full_page: bool = typer.Option(
        False, "--full-page", help="Full-page screenshot, not just viewport."
    ),
    a11y: bool = typer.Option(
        False,
        "--a11y",
        help=(
            "Include the accessibility tree — the roles and names a screen "
            "reader announces. Usually the better answer to 'is X on screen': "
            "assertable as text, and far smaller than a screenshot."
        ),
    ),
    all_nodes: bool = typer.Option(
        False,
        "--a11y-all",
        help="With --a11y, keep semantically uninteresting nodes too.",
    ),
    a11y_query: str | None = typer.Option(
        None,
        "--a11y-query",
        help=(
            "Return only accessibility lines containing this text. "
            "Answering 'is the upgrade button on screen' costs one line "
            "instead of the whole page."
        ),
    ),
    a11y_selector: str | None = typer.Option(
        None,
        "--a11y-selector",
        help="Scope the accessibility tree to this selector's subtree.",
    ),
    storage: bool = typer.Option(
        False,
        "--storage",
        help="Include cookies and localStorage. Values are fingerprinted, not printed.",
    ),
    reveal: bool = typer.Option(
        False,
        "--reveal",
        help="Print storage values in full. A session cookie is a working credential.",
    ),
) -> None:
    """Inspect a live page over CDP."""
    if not cdp_url:
        instance = chrome_cdp.load_state(AitlcConfig.find_and_load().root_dir, port)
        if instance is None:
            typer.echo(
                json.dumps(
                    {
                        "error": f"no tracked browser on port {port}",
                        "hint": "start one with `aitlc cdp launch`, or pass --cdp-url",
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        cdp_url = f"http://127.0.0.1:{instance.port}"
    selectors = [s.strip() for s in check.split(",")] if check else []
    result = cdp_inspect(
        cdp_url,
        screenshot_path=screenshot,
        check_selectors=selectors,
        full_page=full_page,
        accessibility=a11y,
        interesting_only=not all_nodes,
        a11y_selector=a11y_selector,
        a11y_query=a11y_query,
        storage=storage,
        reveal_values=reveal,
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))


@app.command("launch")
def launch(
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port", help="Debugging port."),
    window_size: str = typer.Option(
        chrome_cdp.DESKTOP_WINDOW_SIZE,
        "--window-size",
        help=(
            "Native window size, e.g. '1920,1080'. Desktop by default. "
            "Pass a mobile size (e.g. '375,812') or --mobile for a mobile suite."
        ),
    ),
    mobile: bool = typer.Option(
        False, "--mobile", help="Launch at the mobile default size (375x812)."
    ),
    desktop: bool = typer.Option(
        False, "--desktop", help="(Deprecated, now the default.) Desktop size."
    ),
    chrome_binary: str | None = typer.Option(
        None, "--chrome", help="Path to a specific Chrome/Chromium binary."
    ),
    new: bool = typer.Option(
        False,
        "--new",
        "--isolated",
        help=(
            "Force a brand-new isolated browser on an OS-assigned free port "
            "with its own profile, instead of reusing the shared one. Use for "
            "running several browsers at once (parallel suites, or multiple "
            "agents each needing their own)."
        ),
    ),
    user_data_dir: Path | None = typer.Option(
        None,
        "--user-data-dir",
        "--profile-dir",
        help="Chrome profile directory to use, e.g. a persistent named "
        "profile you reuse across days (already-logged-in sessions, saved "
        "history) instead of aitlc's own auto-generated .cdp/profile-<port>. "
        "Passed straight through to chrome_cdp.launch, which already "
        "supports this -- only the CLI was missing the option.",
    ),
) -> None:
    """Start (or reuse) a detached debug Chrome that outlives this command."""
    config = AitlcConfig.find_and_load()
    resolved_size = chrome_cdp.DEFAULT_WINDOW_SIZE if mobile else window_size
    try:
        instance, reused = chrome_cdp.launch(
            config.root_dir,
            port=None if new else port,
            window_size=resolved_size,
            chrome_binary=chrome_binary,
            user_data_dir=user_data_dir,
        )
    except chrome_cdp.ChromeCdpError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            {
                "cdp_url": instance.cdp_url,
                "port": instance.port,
                "pid": instance.pid,
                "reused": reused,
                "user_data_dir": instance.user_data_dir,
            }
        )
    )


@app.command("status")
def status(
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port", help="Debugging port."),
) -> None:
    """Report whether a debug Chrome is actually answering on the port."""
    config = AitlcConfig.find_and_load()
    version = chrome_cdp.probe(port)
    instance = chrome_cdp.load_state(config.root_dir, port)

    payload: dict = {"port": port, "running": version is not None}
    if version is not None:
        payload["cdp_url"] = f"http://127.0.0.1:{port}"
        payload["browser"] = version.get("Browser")
    if instance is not None:
        payload["pid"] = instance.pid
        payload["user_data_dir"] = instance.user_data_dir
    if version is None and instance is not None:
        # Tracked but dead — the exact state that produces a bare
        # ECONNREFUSED later, so name it here instead.
        payload["note"] = (
            "tracked instance is no longer answering; run 'aitlc cdp launch'"
        )

    typer.echo(json.dumps(payload))
    raise typer.Exit(code=0 if version is not None else 1)


@app.command("list")
def list_() -> None:
    """List every tracked debug Chrome and whether it is still alive."""
    config = AitlcConfig.find_and_load()
    instances = chrome_cdp.list_instances(config.root_dir)
    typer.echo(json.dumps({"instances": instances, "count": len(instances)}, indent=2))


@app.command("stop")
def stop(
    port: int | None = typer.Option(
        None,
        "--port",
        help="Debugging port. Omit to stop the newest RUNNING tracked instance -- "
        "the same one `debug start`/`cdp status` auto-reuse when no port is given -- "
        "instead of a fixed port that may not be the one you're actually looking at.",
    ),
    all_: bool = typer.Option(
        False, "--all", help="Stop every tracked instance, not just --port."
    ),
) -> None:
    """Stop the tracked debug Chrome (or all of them with --all).

    Real confusion hit live: bare `cdp stop` used to always target a FIXED
    port (9333) regardless of what was actually running -- with more than
    one tracked instance (a stale one plus the real, currently-visible
    browser on a different port), it silently reported "stopped": true for
    the wrong one, leaving the actual window open. Defaulting to the
    newest RUNNING instance instead matches what a bare `debug start` would
    have reused, so "stop the browser I'm using" and "the instance stop
    picks by default" are the same thing again.
    """
    config = AitlcConfig.find_and_load()
    if all_:
        ports = chrome_cdp.stop_all(config.root_dir)
        typer.echo(json.dumps({"stopped_ports": ports, "count": len(ports)}))
        return
    resolved_port = port
    if resolved_port is None:
        running = [i for i in chrome_cdp.list_instances(config.root_dir) if i.get("running")]
        if not running:
            typer.echo(
                json.dumps(
                    {
                        "error": "no running tracked instance to stop",
                        "hint": "pass --port <n>, or --all to clear stale records too",
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        newest = max(running, key=lambda i: int(i.get("port", 0)))
        resolved_port = newest["port"]
    stopped = chrome_cdp.stop(config.root_dir, port=resolved_port)
    typer.echo(json.dumps({"port": resolved_port, "stopped": stopped}))


@app.command("time-until")
def time_until(
    selector: str = typer.Argument(..., help="CSS or XPath selector to watch."),
    condition: str = typer.Option(
        "hidden", "--condition", help="'hidden' or 'visible'."
    ),
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port"),
    cdp_url: str | None = typer.Option(None, "--cdp-url", help="Attach to this URL."),
    timeout: float = typer.Option(900.0, "--timeout", help="Give up after N seconds."),
    poll: float = typer.Option(2.0, "--poll", help="Seconds between checks."),
    allow_already: bool = typer.Option(
        False,
        "--allow-already",
        help="Do not require the opposite state first (measurement is then untrusted).",
    ),
) -> None:
    """Measure how long the page takes to satisfy a condition, in wall-clock time.

    For tuning a wait against a real backend job: how long does that banner
    actually stay up? Guessing produces either a flaky test or a timeout so
    large it hides a regression.

    By default the element must first be observed in the *opposite* state.
    Without that check an element that is already hidden -- because the page
    never loaded, or the session dropped -- reports a confident "cleared in
    0.4s" for something that never happened, which is how two hand-run
    measurements produced numbers that were quietly meaningless.
    """
    config = AitlcConfig.find_and_load()
    url = cdp_url
    if not url:
        instance = chrome_cdp.load_state(config.root_dir, port)
        if instance is None:
            typer.echo(
                json.dumps(
                    {
                        "error": f"no tracked browser on port {port}",
                        "hint": "start one with `aitlc cdp launch`, or pass --cdp-url",
                    }
                ),
                err=True,
            )
            raise typer.Exit(code=2)
        url = f"http://127.0.0.1:{instance.port}"

    timing = cdp_attach.time_condition(
        url,
        selector,
        condition=condition,
        timeout_s=timeout,
        poll_s=poll,
        require_start_state=not allow_already,
    )
    typer.echo(json.dumps(timing.to_dict(), indent=2))
    raise typer.Exit(code=0 if timing.met else 1)


# Mounted by commands/_registry.py.
COMMAND = {"name": "cdp", "attr": "app", "kind": "group", "order": 110}