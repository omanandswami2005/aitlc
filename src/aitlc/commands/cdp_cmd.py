"""`aitlc cdp ...` — launch/inspect/stop a long-lived CDP debug Chrome (FR-4)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import chrome_cdp
from aitlc.core.cdp_attach import inspect as cdp_inspect

app = typer.Typer(help="Launch, inspect and stop a live Chromium instance over CDP.")


@app.command("inspect")
def inspect(
    cdp_url: str = typer.Option(..., "--cdp-url", help="e.g. ws://127.0.0.1:9222/..."),
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
) -> None:
    """Inspect a live page over CDP."""
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
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))


@app.command("launch")
def launch(
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port", help="Debugging port."),
    window_size: str = typer.Option(
        chrome_cdp.DEFAULT_WINDOW_SIZE,
        "--window-size",
        help=(
            "Native window size, e.g. '375,812'. Mobile-sized by default: the "
            "framework's pre-scenario login runs before per-scenario device "
            "emulation applies, so a desktop-sized window breaks mobile login."
        ),
    ),
    desktop: bool = typer.Option(
        False, "--desktop", help="Launch at the OS default size instead of mobile."
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
) -> None:
    """Start (or reuse) a detached debug Chrome that outlives this command."""
    config = AitlcConfig.find_and_load()
    try:
        instance, reused = chrome_cdp.launch(
            config.root_dir,
            port=None if new else port,
            window_size=None if desktop else window_size,
            chrome_binary=chrome_binary,
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
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port", help="Debugging port."),
    all_: bool = typer.Option(
        False, "--all", help="Stop every tracked instance, not just --port."
    ),
) -> None:
    """Stop the tracked debug Chrome (or all of them with --all)."""
    config = AitlcConfig.find_and_load()
    if all_:
        ports = chrome_cdp.stop_all(config.root_dir)
        typer.echo(json.dumps({"stopped_ports": ports, "count": len(ports)}))
        return
    stopped = chrome_cdp.stop(config.root_dir, port=port)
    typer.echo(json.dumps({"port": port, "stopped": stopped}))
