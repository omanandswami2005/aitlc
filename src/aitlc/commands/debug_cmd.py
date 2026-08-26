"""`aitlc debug ...` — an interactive session over one feature file.

The engine is a real, paused behave run (`aitlc.runtime.runner:AitlcRunner` in
gate mode). behave itself runs before_all/before_scenario and the setup steps,
then parks at the target step holding the live Context and browser; `next` and
`retry` advance/re-run REAL behave Step objects over a control socket. There is
no reconstruction of behave's loop, so there is nothing to diverge from it:
Examples binding, data tables, docstrings, run-scoped data and the project's
own hooks are all behave's, because it IS behave.

Verbs:

    start <TEST-ID> [--at N]   launch an isolated browser, drive real behave to step N
    status                     where the paused run is
    retry                      re-run the current step (picks up an edit first)
    next                       advance one real step
    certify [--times 2]        fresh instance, real feature, N passes required
    stop                       stop the browser and drop the session
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, checkpoint, chrome_cdp, debug_session, gate_client, gate_launch
from aitlc.core.dotenv import load_dotenv
from aitlc.core import workspace

app = typer.Typer(help="Interactive debug session over one feature file.")


def _example_line(feature_text: str, example: int) -> int | None:
    """1-based line of the Nth Examples data row, or None when there is none.

    behave selects a single Scenario Outline row by FILE:LINE, so this is how a
    debug session targets exactly the row it is meant to — the binding itself
    is then behave's, which is the whole point (no placeholder guessing).
    """
    lines = feature_text.splitlines()
    ex = next((i for i, l in enumerate(lines) if l.strip().startswith("Examples")), None)
    if ex is None:
        return None
    header_seen = False
    seen = 0
    for i in range(ex + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if not stripped.startswith("|"):
            break
        if not header_seen:
            header_seen = True
            continue
        if seen == example:
            return i + 1
        seen += 1
    return None


def _default_id(config, test_id: str | None) -> str:
    """Return test_id, or the project's default feature stem when none was given.

    Lets every debug verb run with no argument in a single-feature project:
    `debug start` defaults to the sole feature, and `next`/`retry`/`status`/
    `stop` default to that same session key.
    """
    if test_id:
        return test_id
    resolved = config.default_feature_id()
    if not resolved:
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
    return resolved


def _require(config, test_id: str) -> debug_session.DebugSession:
    session = debug_session.load(config.root_dir, test_id)
    if session is None:
        typer.echo(
            json.dumps(
                {"error": f"no debug session for {test_id}; run `debug start` first"}
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    return session




@app.command("start")
def start(
    test_id: str = typer.Argument(
        None,
        help="Test ID or feature file path. Omit to use the project's default "
        "feature ([project].default_feature, or the sole *.feature in feature_dir).",
    ),
    at: int = typer.Option(
        0, "--at", help="Step index to park on (0-based). Steps before it are run."
    ),
    example: int = typer.Option(
        0, "--example", help="Examples row to run, 0-based. Scenario Outlines only."
    ),
    env_file: str = typer.Option(".env", "--env-file"),
    window_size: str = typer.Option(
        chrome_cdp.DESKTOP_WINDOW_SIZE,
        "--window-size",
        help="Browser window as WIDTH,HEIGHT. Desktop by default; a phone size "
        "for a mobile suite.",
    ),
    summary: bool = typer.Option(
        False, "--summary", help="Emit a compact {parked_at, total_steps, ...} summary."
    ),
    background: bool = typer.Option(
        False,
        "--background",
        help="Return immediately; the setup runs detached. Poll `debug status`.",
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", help="Seconds to wait for the run to park (foreground)."
    ),
) -> None:
    """Launch an isolated browser and drive REAL behave to a step, then park."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    feature = config.resolve_feature_path(test_id)
    if feature is None:
        typer.echo(json.dumps({"error": f"could not resolve feature for {test_id!r}"}), err=True)
        raise typer.Exit(code=2)
    feature = Path(feature)
    line = _example_line(feature.read_text(), example)

    socket_path = gate_launch.socket_path(config.root_dir, test_id)
    progress_path = debug_session.progress_path(config.root_dir, test_id)

    # A fresh, isolated debug Chrome the suite will attach to; it survives the
    # gate's hard exit, so `stop` owns its teardown.
    instance, _reused = chrome_cdp.launch(config.root_dir, port=None, window_size=window_size)

    debug_session.write_progress(
        config.root_dir, test_id,
        {"state": "running", "test_id": test_id, "target": at, "done": 0,
         "started_at": time.time()},
    )
    proc = gate_launch.launch(
        config, feature=feature, line=line,
        cdp_url=instance.cdp_url, socket_path_=socket_path,
        progress_path=progress_path,
        gate_env={"AITLC_GATE": "1", "AITLC_GATE_AT": str(at), "AITLC_GATE_EXAMPLE": str(example)},
        log_name="gate.log",
    )
    pid = proc.pid
    session = debug_session.DebugSession(
        test_id=test_id, feature=str(feature), cdp_url=instance.cdp_url,
        port=instance.port, example=example, socket=str(socket_path), pid=pid, park=at,
        index=at,
    )
    debug_session.save(config.root_dir, session)

    if background:
        typer.echo(
            json.dumps(
                {
                    "background": True,
                    "test_id": test_id,
                    "cdp_url": instance.cdp_url,
                    "poll": f"aitlc debug status {test_id}",
                    "progress_file": str(progress_path),
                },
                indent=2,
            )
        )
        return

    outcome = gate_launch.await_park_or_exit(socket_path, proc, timeout)
    if outcome == "exited":
        # behave --stop aborts on a failed setup step: the gate never parked.
        # This is the faithful "parked on a broken precondition" signal (G46).
        log = workspace.output_path(config.root_dir, ".aitlc", "debug", "gate.log")
        tail = ""
        try:
            tail = "\n".join(log.read_text().splitlines()[-15:])
        except OSError:
            pass
        typer.echo(
            json.dumps(
                {
                    "error": "setup did not reach the park point",
                    "reason": "a setup step failed (behave --stop aborted) or the run crashed",
                    "test_id": test_id,
                    "log_tail": tail,
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(code=1)
    if outcome == "timeout":
        typer.echo(
            json.dumps({"error": f"run did not park within {timeout}s", "test_id": test_id}),
            err=True,
        )
        raise typer.Exit(code=1)

    status_reply = outcome
    if summary:
        typer.echo(
            json.dumps(
                {
                    "test_id": test_id,
                    "cdp_url": instance.cdp_url,
                    "parked_at": status_reply.get("index"),
                    "total_steps": status_reply.get("total"),
                    "setup_passed": status_reply.get("index"),
                    "setup_failed": 0,
                },
                indent=2,
            )
        )
        return
    typer.echo(
        json.dumps(
            {
                "test_id": test_id,
                "cdp_url": instance.cdp_url,
                "engine": "behave-gate",
                "parked_at": status_reply.get("index"),
                "total_steps": status_reply.get("total"),
                "current_step": status_reply.get("current_step"),
            },
            indent=2,
        )
    )


@app.command("status")
def status(test_id: str = typer.Argument(None)) -> None:
    """Show where the paused run is."""
    config = AitlcConfig.find_and_load()
    test_id = _default_id(config, test_id)
    session = debug_session.load(config.root_dir, test_id)
    if session is not None and session.socket:
        try:
            reply = gate_client.request(session.socket, "status")
            typer.echo(
                json.dumps(
                    {
                        "test_id": test_id,
                        "cdp_url": session.cdp_url,
                        "parked_at": reply.get("index"),
                        "total_steps": reply.get("total"),
                        "current_step": reply.get("current_step"),
                        "finished": reply.get("finished"),
                        "error": reply.get("error"),
                    },
                    indent=2,
                )
            )
            return
        except (gate_client.GateUnavailable, OSError):
            pass  # gate not up yet, or already gone -- fall through to progress

    progress = debug_session.read_progress(config.root_dir, test_id)
    if progress is not None:
        out = {"test_id": test_id, "in_progress": True, **progress}
        if "started_at" in progress:
            out["elapsed_s"] = round(time.time() - progress["started_at"], 1)
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        json.dumps({"error": f"no debug session for {test_id}; run `debug start` first"}),
        err=True,
    )
    raise typer.Exit(code=2)


def _request_or_die(session: debug_session.DebugSession, cmd: str, **kwargs: Any) -> dict:
    """Send one command to the live gate, or report unreachable and exit.

    Shared by every command that talks to an already-parked session
    (`retry`/`next` via `_drive`, and `run-text`/`eval` below) so the
    "session no longer reachable" error is worded once, not once per verb.
    """
    if not session.socket:
        typer.echo(json.dumps({"error": "session has no gate socket"}), err=True)
        raise typer.Exit(code=2)
    try:
        return gate_client.request(session.socket, cmd, **kwargs)
    except (gate_client.GateUnavailable, OSError):
        typer.echo(
            json.dumps(
                {
                    "error": "the paused run is no longer reachable",
                    "hint": "it may have been stopped or have exited; run `debug start` again",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=3)


def _drive(config, test_id: str, cmd: str) -> None:
    """Send one stepping command to the gate and report the reply."""
    session = _require(config, test_id)
    # Pick up an edited step definition AND a Gherkin edit before running:
    # `retry`/`next` must mean "run this again with my change" — for both
    # Python and the feature file — not against a stale parse.
    reload_reply = _request_or_die(session, "reload", step_dir=config.step_dir)
    reply = _request_or_die(session, cmd)

    if isinstance(reload_reply, dict) and reload_reply.get("feature"):
        # A Gherkin edit was picked up: report how the cursor moved.
        reply["feature"] = reload_reply["feature"]
    if isinstance(reload_reply, dict) and reload_reply.get("reloaded_modules"):
        # G54: confirms a page-object/helper edit outside step_dir WAS picked
        # up automatically -- positive signal, not just silence when it works.
        reply["reloaded_modules"] = reload_reply["reloaded_modules"]
    if isinstance(reload_reply, dict) and reload_reply.get("stale_modules"):
        # G54: a page-object/helper edit outside step_dir stays cached in
        # sys.modules -- surface it here so `retry`/`next` never look like a
        # clean run of code that was never actually re-executed.
        reply["stale_modules"] = reload_reply["stale_modules"]
        reply["warning"] = reload_reply["warning"]
    if reply.get("index") is not None:
        session.index = reply["index"]
        debug_session.save(config.root_dir, session)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if reply.get("status") in (None, "passed") else 1)


@app.command("retry")
def retry(
    test_id: str = typer.Argument(None),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Re-run the current step (picking up an edit), without advancing."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    _drive(config, _default_id(config, test_id), "retry")


@app.command("next")
def next_step(
    test_id: str = typer.Argument(None),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Advance one real behave step and run it, keeping the live state."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    _drive(config, _default_id(config, test_id), "next")


@app.command("continue")
def continue_steps(
    test_id: str = typer.Argument(None),
    max_steps: int = typer.Option(
        500, "--max-steps", help="Safety cap on how many steps to advance."
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Advance through every remaining step, stopping at the first failure or the end.

    The bulk version of repeated `next` calls: same reload-before-run
    contract per step, same live state throughout, but one command instead
    of driving `next` in a shell loop yourself. Stops immediately on a
    failing step -- it does not skip ahead past one -- so the reply's last
    entry is always the one that needs attention (if any).
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    session = _require(config, _default_id(config, test_id))

    results = []
    for _ in range(max_steps):
        reload_reply = _request_or_die(session, "reload", step_dir=config.step_dir)
        reply = _request_or_die(session, "next")
        if isinstance(reload_reply, dict) and reload_reply.get("feature"):
            reply["feature"] = reload_reply["feature"]
        if isinstance(reload_reply, dict) and reload_reply.get("reloaded_modules"):
            reply["reloaded_modules"] = reload_reply["reloaded_modules"]
        if isinstance(reload_reply, dict) and reload_reply.get("stale_modules"):
            reply["stale_modules"] = reload_reply["stale_modules"]
            reply["warning"] = reload_reply["warning"]
        results.append(reply)
        if reply.get("index") is not None:
            session.index = reply["index"]
            debug_session.save(config.root_dir, session)
        if reply.get("status") not in (None, "passed") or reply.get("finished"):
            break

    payload = {
        "steps_run": len(results),
        "stopped_reason": (
            "finished" if results and results[-1].get("finished")
            else "failed" if results and results[-1].get("status") not in (None, "passed")
            else "max_steps_reached"
        ),
        "results": results,
    }
    typer.echo(json.dumps(payload, indent=2))
    last_status = results[-1].get("status") if results else None
    raise typer.Exit(code=0 if last_status in (None, "passed") else 1)


@app.command("run-text")
def run_text(
    text: str = typer.Argument(
        ..., help='Ad-hoc Gherkin step text, e.g. \'click on element ID: "save_btn"\'.'
    ),
    test_id: str = typer.Argument(None),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Run one ad-hoc Gherkin step against the live paused context.

    Any step already registered in the project works, not just the ones in
    the paused feature file -- the same live browser, login and setup the
    session already paid for, without advancing the debug cursor: `next`
    still runs the same step it would have before this. This is
    `core/step_console.py`'s `run_text` gate command (already built, wired
    to nothing) exposed as a real command.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    session = _require(config, _default_id(config, test_id))
    # Same "pick up an edit before running" contract as retry/next -- an
    # ad-hoc step should exercise your latest code, not whatever was live
    # when the session started.
    reload_reply = _request_or_die(session, "reload", step_dir=config.step_dir)
    reply = _request_or_die(session, "run_text", text=text)
    if isinstance(reload_reply, dict) and reload_reply.get("reloaded_modules"):
        reply["reloaded_modules"] = reload_reply["reloaded_modules"]
    if isinstance(reload_reply, dict) and reload_reply.get("stale_modules"):
        reply["stale_modules"] = reload_reply["stale_modules"]
        reply["warning"] = reload_reply["warning"]
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if reply.get("status") in (None, "passed") else 1)


@app.command("eval")
def eval_js(
    expr: str = typer.Argument(..., help="JavaScript expression to evaluate on the live page."),
    test_id: str = typer.Argument(None),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Evaluate a JS expression against the live paused browser page.

    Runs via Playwright's `page.evaluate()` against whichever page the gate
    finds on the live context -- read DOM state, count/inspect elements,
    pull text -- without advancing the debug cursor or touching the step
    registry. The breakpoint()-equivalent for the browser side of a paused
    session, reachable without editing a page object to add one.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    session = _require(config, _default_id(config, test_id))
    reply = _request_or_die(session, "eval", expr=expr)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if not reply.get("error") else 1)


@app.command("stop")
def stop(
    test_id: str = typer.Argument(None),
    cleanup: bool = typer.Option(
        False,
        "--cleanup",
        help=(
            "Fire the project's real after_scenario/after_feature hooks "
            "(e.g. a tag-driven logout) before exiting. Off by default: "
            "the debug browser is often the persistent one `run --debug` "
            "reuses across invocations, and this suite's own cleanup can "
            "log it out -- opt in only for a deliberate clean handoff, "
            "never as routine teardown."
        ),
    ),
) -> None:
    """Stop the paused run and its browser, and drop the session."""
    config = AitlcConfig.find_and_load()
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    gate_stopped = False
    cleanup_result = None
    if session.socket:
        try:
            reply = gate_client.request(session.socket, "stop", cleanup=cleanup)
            gate_stopped = True
            cleanup_result = reply.get("cleanup")
        except (gate_client.GateUnavailable, OSError):
            pass
    # Only THIS session's own browser -- real bug found live: `stop_all`
    # kills every CDP instance tracked for the project, including an
    # unrelated persistent Chrome someone launched separately (`aitlc cdp
    # launch` for manual work, or another `run --debug`'s reused browser)
    # that this session never touched. `session.port` is exactly the one
    # `debug start` launched for this test id; stop only that.
    stopped_this_session = chrome_cdp.stop(config.root_dir, port=session.port)
    debug_session.clear(config.root_dir, test_id)
    debug_session.clear_progress(config.root_dir, test_id)
    payload = {
        "gate_stopped": gate_stopped,
        "stopped_port": session.port if stopped_this_session else None,
        "session_cleared": True,
    }
    if cleanup:
        # Never silently claim success: if cleanup was asked for but the
        # gate was unreachable to run it, say so rather than staying quiet.
        payload["cleanup"] = cleanup_result or {
            "after_scenario": "skipped: gate unreachable, cleanup did not run",
            "after_feature": "skipped: gate unreachable, cleanup did not run",
        }
    typer.echo(json.dumps(payload))


@app.command("certify")
def certify(
    test_id: str = typer.Argument(None),
    times: int = typer.Option(
        2,
        "--times",
        help="Consecutive clean runs required. One pass does not disprove a race.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Run the real feature in a fresh instance, N times, and report.

    Deliberately not the debug browser: a CDP session reuses an existing
    browser context -- the opposite of isolation -- so it carries whatever
    earlier work left behind. Certification has to start from nothing, which is
    also why this is a separate verb rather than a flag on `retry`.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    session = debug_session.load(config.root_dir, test_id)
    feature = session.feature if session else str(config.resolve_feature_path(test_id))

    runs = []
    for attempt in range(1, times + 1):
        result = behave_runner.run(Path(feature), cwd=config.root_dir)
        payload = result.to_dict()
        runs.append(
            {
                "attempt": attempt,
                "passed": result.passed,
                "steps_by_status": payload.get("steps_by_status", {}),
                "failures": payload.get("failures", []),
            }
        )
        if not result.passed:
            break

    certified = len(runs) == times and all(r["passed"] for r in runs)
    typer.echo(
        json.dumps(
            {
                "test_id": test_id,
                "feature": feature,
                "required": times,
                "ran": len(runs),
                "certified": certified,
                "runs": runs,
            },
            indent=2,
        )
    )
    raise typer.Exit(code=0 if certified else 1)


@app.command("checkpoint")
def checkpoint_cmd(
    name: str = typer.Argument(..., help="Name for this snapshot."),
    test_id: str = typer.Option("", "--test-id", help="Session to snapshot."),
    cdp_url: str | None = typer.Option(None, "--cdp-url"),
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port"),
    value: list[str] = typer.Option(
        [], "--value", help="A run-scoped KEY=VALUE to record. Repeatable."
    ),
    entity: list[str] = typer.Option(
        [], "--entity", help="Something created on the server, e.g. user=a@b.c."
    ),
) -> None:
    """Snapshot the current session so an expensive setup can be returned to.

    Fifteen minutes of setup before the interesting step used to mean paying
    it again on every look. Record the session here and `restore` returns to
    it.
    """
    config = AitlcConfig.find_and_load()
    session = debug_session.load(config.root_dir, test_id) if test_id else None

    url = cdp_url or (session.cdp_url if session else None)
    if not url:
        instance = chrome_cdp.load_state(config.root_dir, port)
        url = f"http://127.0.0.1:{instance.port}" if instance else None
    if not url:
        typer.echo(
            json.dumps({"error": "no live browser to snapshot; pass --cdp-url"}),
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        state = checkpoint.capture_browser_state(url)
    except Exception as exc:
        typer.echo(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), err=True)
        raise typer.Exit(code=2) from exc

    def pairs(items):
        out = {}
        for item in items:
            key, _, val = item.partition("=")
            if key:
                out[key.strip()] = val.strip()
        return out

    record = checkpoint.Checkpoint(
        name=name,
        test_id=test_id or (session.test_id if session else ""),
        feature=session.feature if session else "",
        step_index=session.index if session else 0,
        storage_state=state,
        run_values=pairs(value),
        created_entities=[pairs([e]) for e in entity],
    )
    path = checkpoint.save(config.root_dir, record)
    payload = record.summary()
    payload["saved_to"] = str(path.relative_to(config.root_dir))
    if not payload["cookies"]:
        # A checkpoint with no cookies restores nothing. Saying so now beats
        # discovering it when a restore silently lands on a login page.
        payload["warning"] = (
            "no cookies were captured, so this snapshot carries no session"
        )
    typer.echo(json.dumps(payload, indent=2))


@app.command("restore")
def restore_cmd(
    name: str = typer.Argument(..., help="Checkpoint to restore."),
    cdp_url: str | None = typer.Option(None, "--cdp-url"),
    port: int = typer.Option(chrome_cdp.DEFAULT_PORT, "--port"),
    ttl: float = typer.Option(
        checkpoint.DEFAULT_TTL_SECONDS,
        "--ttl",
        help="Refuse to restore a checkpoint older than this many seconds.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Restore even when past the TTL."
    ),
) -> None:
    """Replay a checkpoint's session into a live browser."""
    config = AitlcConfig.find_and_load()
    try:
        record = checkpoint.load(config.root_dir, name)
    except checkpoint.CheckpointError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=2) from exc
    if record is None:
        typer.echo(json.dumps({"error": f"no checkpoint named {name!r}"}), err=True)
        raise typer.Exit(code=2)

    if record.is_stale(ttl) and not force:
        # A dead session restored silently produces exactly the false failure
        # this is meant to prevent.
        typer.echo(
            json.dumps(
                {
                    "error": "checkpoint is older than the TTL and may no longer work",
                    "age_seconds": round(record.age_seconds(), 1),
                    "ttl_seconds": ttl,
                    "hint": "re-take it, or pass --force to restore anyway",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)

    url = cdp_url
    if not url:
        instance = chrome_cdp.load_state(config.root_dir, port)
        url = f"http://127.0.0.1:{instance.port}" if instance else None
    if not url:
        typer.echo(json.dumps({"error": "no live browser; pass --cdp-url"}), err=True)
        raise typer.Exit(code=2)

    try:
        applied = checkpoint.restore_browser_state(url, record.storage_state)
    except Exception as exc:
        typer.echo(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        json.dumps(
            {
                **record.summary(),
                **applied,
                "run_values": record.run_values,
                "created_entities": record.created_entities,
            },
            indent=2,
        )
    )


@app.command("checkpoints")
def checkpoints_cmd(
    delete: str = typer.Option("", "--delete", help="Remove this checkpoint."),
    ttl: float = typer.Option(checkpoint.DEFAULT_TTL_SECONDS, "--ttl"),
) -> None:
    """List saved checkpoints, newest first, with whether each is still usable."""
    config = AitlcConfig.find_and_load()
    if delete:
        typer.echo(json.dumps({"deleted": checkpoint.delete(config.root_dir, delete)}))
        return
    rows = []
    for record in checkpoint.list_all(config.root_dir):
        row = record.summary()
        row["usable"] = not record.is_stale(ttl)
        rows.append(row)
    typer.echo(json.dumps({"count": len(rows), "checkpoints": rows}, indent=2))


# Mounted by commands/_registry.py.
COMMAND = {"name": "debug", "attr": "app", "kind": "group", "order": 140}