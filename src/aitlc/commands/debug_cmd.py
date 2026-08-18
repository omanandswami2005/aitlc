"""`aitlc debug ...` — an interactive session over one feature file.

The pieces this drives already existed (`cdp launch`, `steps run --cdp-url`,
`cdp inspect`, `run`). What did not exist was anything holding the *position*,
so the natural thing to type after an edit was `aitlc run` again — a full
scenario, minutes long, and on a suite that creates users and moves credits,
destructive. This keeps one browser and one step index so `retry` is cheap and
`certify` is the only thing that pays for a clean run.

Verbs:

    start <TEST-ID> [--at N]   launch an isolated browser, drive to step N
    status                     where the session is, and its attempt history
    retry                      re-run the current step in that browser
    next                       advance one step and run it
    inspect [--check ...]      read the live page
    certify [--times 2]        fresh instance, real feature, N passes required
    stop                       stop the browser and drop the session
"""

from __future__ import annotations

import json
import time
import subprocess
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, checkpoint, chrome_cdp, debug_session, step_console
from aitlc.core.dotenv import load_dotenv
from aitlc.core.step_console import run_console
from aitlc.core import workspace

app = typer.Typer(help="Interactive debug session over one feature file.")


def _slice_file(root_dir: Path, steps: list[str]) -> Path:
    """Materialise steps as a parseable feature under the project's tree.

    Written inside the project rather than a temp dir because Behave resolves
    its steps directory relative to the feature: a slice in /tmp dies with
    "No steps directory", which is a confusing way to learn that.
    """
    out = workspace.output_path(root_dir, ".aitlc", "debug", "_slice.feature")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"\t{s.strip()}" for s in steps)
    out.write_text(
        "Feature: aitlc debug slice\n\n\t@skip_login\n\tScenario: slice\n" + body + "\n"
    )
    return out


def _run_steps(config, session: debug_session.DebugSession, steps: list[str]) -> dict:
    """Dispatch a list of steps into the session's browser.

    Prefers a persistent console when one is running: it holds the imported
    step registry and, crucially, the same run-scoped data every step in the
    session shares. Falls back to a one-shot process when it is not there, so
    a missing optimisation never turns into a broken command.
    """
    if not steps:
        return {"results": []}

    socket_path = step_console.console_socket(config.root_dir, session.test_id)
    try:
        reply = step_console.request_steps(socket_path, steps)
    except step_console.ConsoleUnavailable:
        pass
    else:
        return {
            **({"reloaded": reply["reloaded"]} if reply.get("reloaded") else {}),
            "results": [
                {
                    "step": r.get("step", ""),
                    "status": r.get("status", "failed"),
                    "duration_s": r.get("duration_s", 0.0),
                    "error": r.get("error"),
                    "started_at": r.get("started_at", ""),
                    "ended_at": r.get("ended_at", ""),
                }
                for r in reply.get("results", [])
            ],
            "stderr_tail": reply.get("error", ""),
            "unhandled_events": [],
            "via": "console",
        }
    slice_path = _slice_file(config.root_dir, steps)
    result = run_console(
        slice_path,
        cwd=config.root_dir,
        poetry_cmd=behave_runner.resolve_poetry(),
        cdp_url=session.cdp_url,
        scenario_setup=config.scenario_setup,
        step_dir=config.step_dir,
        browser_actions=config.browser_actions,
        browser_factory=config.browser_factory,
    )
    return {
        "results": [
            {
                "step": r.step,
                "status": r.status,
                "duration_s": r.duration_s,
                "error": r.error,
            }
            for r in result.results
        ],
        "stderr_tail": result.stderr_tail,
        "unhandled_events": result.unhandled_events,
    }


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


def _launch_console(config, session) -> dict:
    """Start the persistent step console for this session, detached.

    Without this the console is unreachable code: `_run_steps` looks for a
    socket, never finds one, and silently falls back to spawning a process per
    step -- which is the slow *and* incorrect path the console exists to
    replace. Nothing failed, which is exactly why it went unnoticed.

    Detached and non-fatal: a session whose console will not start is still a
    working session, just a slower one.
    """
    socket_path = step_console.console_socket(config.root_dir, session.test_id)
    if step_console.console_is_alive(socket_path):
        return {"started": False, "reason": "already running", "socket": str(socket_path)}

    script = Path(step_console.__file__).resolve()
    cmd = behave_runner.resolve_poetry() + [
        "run",
        "python3",
        str(script),
        str(session.feature),
        "--serve",
        str(socket_path),
        "--step-dir",
        config.step_dir,
        "--cdp-url",
        session.cdp_url,
    ]
    if config.scenario_setup:
        cmd += ["--scenario-setup", config.scenario_setup]
    else:
        cmd.append("--allow-missing-setup")
    if config.browser_actions:
        cmd += ["--browser-actions", config.browser_actions]

    log_path = workspace.ensure(config.root_dir, ".aitlc", "debug", "console.log")
    try:
        with log_path.open("ab") as handle:
            subprocess.Popen(
                cmd,
                cwd=config.root_dir,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as exc:
        return {"started": False, "reason": f"{type(exc).__name__}: {exc}"}

    # Wait briefly for it to bind; a console that is not listening yet would
    # send the first retry down the fallback path for no reason.
    deadline = time.time() + 60
    while time.time() < deadline:
        if step_console.console_is_alive(socket_path):
            return {"started": True, "socket": str(socket_path)}
        time.sleep(0.5)
    return {
        "started": False,
        "reason": "console did not begin listening in time",
        "log": str(log_path),
    }


@app.command("start")
def start(
    test_id: str = typer.Argument(..., help="Test ID or feature file path."),
    at: int = typer.Option(
        0, "--at", help="Step index to park on (0-based). Steps before it are run."
    ),
    example: int = typer.Option(
        0,
        "--example",
        help="Examples row to bind, 0-based. Scenario Outlines only.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
    window_size: str = typer.Option(
        chrome_cdp.DESKTOP_WINDOW_SIZE,
        "--window-size",
        help="Browser window as WIDTH,HEIGHT. Desktop by default, to match a "
        "real run; pass a phone size for a mobile suite.",
    ),
) -> None:
    """Launch an isolated browser and drive the feature to a step."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    feature = config.resolve_feature_path(test_id)
    try:
        steps = debug_session.feature_steps(
            Path(feature).read_text(), example=example
        )
    except debug_session.ExampleBindingError as exc:
        typer.echo(
            json.dumps({"error": str(exc), "feature": str(feature)}), err=True
        )
        raise typer.Exit(code=2)
    if not steps:
        typer.echo(json.dumps({"error": f"no steps found in {feature}"}), err=True)
        raise typer.Exit(code=2)

    # Always a fresh, isolated browser: a long-lived shared profile accumulates
    # sessions and eventually fails a run at the framework's own login, which
    # reads as a test bug rather than a dirty profile.
    instance, _reused = chrome_cdp.launch(
        config.root_dir, port=None, window_size=window_size
    )
    session = debug_session.DebugSession(
        test_id=test_id,
        feature=str(feature),
        cdp_url=instance.cdp_url,
        port=instance.port,
        steps=steps,
        index=max(0, min(at, len(steps))),
        example=example,
    )
    ran = _run_steps(config, session, session.slice_through(session.index))
    debug_session.save(config.root_dir, session)
    # Start the console only once the browser is at the parked step: it
    # attaches to that same browser and holds one set of run-scoped data from
    # here on, which is what makes retry/next both fast and correct.
    console_state = _launch_console(config, session)
    typer.echo(
        json.dumps(
            {
                "test_id": test_id,
                "cdp_url": session.cdp_url,
                "total_steps": len(steps),
                "parked_at": session.index,
                "console": console_state,
                "current_step": (session.current or "").strip(),
                "setup": ran["results"],
            },
            indent=2,
        )
    )


@app.command("status")
def status(test_id: str = typer.Argument(...)) -> None:
    """Show where the session is and every attempt so far."""
    config = AitlcConfig.find_and_load()
    session = _require(config, test_id)
    typer.echo(
        json.dumps(
            {
                "test_id": session.test_id,
                "cdp_url": session.cdp_url,
                "parked_at": session.index,
                "total_steps": len(session.steps),
                "current_step": (session.current or "").strip(),
                "finished": session.finished,
                "attempts_here": [
                    {"status": a.status, "step": a.step}
                    for a in session.attempts_for_current()
                ],
            },
            indent=2,
        )
    )


def _run_current(config, session, advance: bool) -> None:
    # Pick up an edit to the Gherkin before deciding which step to run. The
    # session held the list parsed when it started, so without this `retry`
    # re-runs text that may no longer be in the file.
    resynced: dict = {}
    try:
        resynced = debug_session.resync(session, Path(session.feature).read_text())
    except OSError:
        resynced = {}
    if resynced.get("feature_reloaded"):
        debug_session.save(config.root_dir, session)

    if session.finished:
        typer.echo(json.dumps({"done": True, "message": "no steps left"}))
        return
    # `start --at N` parks *on* step N without running it, so the first `next`
    # used to advance to N+1 and silently skip N entirely. Every later step
    # that depended on it then failed, and the session looked like the app was
    # broken -- confirmed live: parking at "Open Admin Panel" and stepping
    # forward left the panel closed, so three admin steps failed in a row.
    #
    # "next" means move forward through the scenario. If the step under the
    # cursor has never been attempted, running it *is* moving forward.
    if advance and session.attempts_for_current():
        session.advance()
        if session.finished:
            debug_session.save(config.root_dir, session)
            typer.echo(json.dumps({"done": True, "message": "reached the end"}))
            return
    step = session.current or ""
    # A lone And/But cannot open a parsed block, exactly as in slice_through.
    # Without this the console silently returns nothing and the step reads as
    # failed -- roughly half of real feature lines start with a continuation.
    runnable = debug_session.promote_leading_continuation([step])
    out = _run_steps(config, session, runnable)
    results = out.get("results") or []

    # Three outcomes, not two. An empty result list means the console never ran
    # the step; reporting that as "failed" sends the user to fix working code.
    if results:
        status_ = results[0]["status"]
    else:
        status_ = "not_run"

    session.record(status_)
    debug_session.save(config.root_dir, session)
    payload = {
        "step": step.strip(),
        "index": session.index,
        "status": status_,
        "attempts_here": len(session.attempts_for_current()),
    }
    if results:
        payload["error"] = results[0]["error"]
    else:
        payload["message"] = (
            "the step console produced no result -- the step did not run. "
            "This is not a step failure."
        )
    if out.get("stderr_tail"):
        payload["stderr_tail"] = out["stderr_tail"]
    if out.get("unhandled_events"):
        payload["unhandled_events"] = out["unhandled_events"]
    typer.echo(json.dumps(payload, indent=2))
    raise typer.Exit(code=0 if status_ == "passed" else 1)


@app.command("retry")
def retry(
    test_id: str = typer.Argument(...),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Re-run the current step after an edit, without restarting the scenario."""
    config = AitlcConfig.find_and_load()
    # Without this the step modules fail to import for want of config, every
    # step resolves as "undefined", and the run reports a failure that never
    # happened. `start` loaded it; `retry`/`next` did not, so a session worked
    # until the first re-run and then reported nonsense.
    load_dotenv(config.root_dir / env_file)
    _run_current(config, _require(config, test_id), advance=False)


@app.command("next")
def next_step(
    test_id: str = typer.Argument(...),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Advance one step and run it, keeping the state you already have."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    _run_current(config, _require(config, test_id), advance=True)


@app.command("certify")
def certify(
    test_id: str = typer.Argument(...),
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


@app.command("stop")
def stop(test_id: str = typer.Argument(...)) -> None:
    """Stop the session's browser and drop the session."""
    config = AitlcConfig.find_and_load()
    _require(config, test_id)  # refuse politely when there is nothing to stop
    # Stop the console before the browser: it holds a live connection to it,
    # and tearing the browser out from under it produces a confusing error
    # from a process that is about to be shut down anyway.
    console_stopped = step_console.stop_console(
        step_console.console_socket(config.root_dir, test_id)
    )
    stopped = chrome_cdp.stop_all(config.root_dir)
    debug_session.clear(config.root_dir, test_id)
    typer.echo(
        json.dumps(
            {
                "stopped_ports": stopped,
                "console_stopped": console_stopped,
                "session_cleared": True,
            }
        )
    )


@app.command("console")
def console(
    test_id: str = typer.Argument(...),
    stop_it: bool = typer.Option(False, "--stop", help="Shut the console down."),
    start_it: bool = typer.Option(
        False,
        "--start",
        help="Start one for an existing session, without re-running its setup.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Report (or stop) the session's persistent step console.

    The console is what makes `retry`/`next` fast and, more importantly,
    correct: it holds one set of run-scoped data, where a process per step
    regenerates it and leaves later steps waiting for names that never
    existed.
    """
    config = AitlcConfig.find_and_load()
    socket_path = step_console.console_socket(config.root_dir, test_id)
    if start_it:
        # Without this, a console that died left no way back except `start`,
        # which re-runs every setup step -- the exact cost the console exists
        # to avoid paying twice.
        #
        # The env file has to be loaded here for the same reason retry/next
        # need it: the console is a child process that inherits this
        # environment, and without it every step module fails to import, the
        # scenario setup that mints the run-scoped values raises, and the
        # console dies before it ever listens. `start` loaded it and this
        # path did not, which is exactly the shape of the earlier bug.
        load_dotenv(config.root_dir / env_file)
        session = _require(config, test_id)
        typer.echo(json.dumps(_launch_console(config, session), indent=2))
        return
    if stop_it:
        typer.echo(
            json.dumps({"stopped": step_console.stop_console(socket_path)})
        )
        return
    alive = step_console.console_is_alive(socket_path)
    typer.echo(
        json.dumps(
            {
                "alive": alive,
                "socket": str(socket_path),
                "note": (
                    "steps run in one process, sharing run-scoped data"
                    if alive
                    else "no console; retry/next spawn a process per step"
                ),
            }
        )
    )


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
