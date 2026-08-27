"""`aitlc debug ...` — an interactive session over one feature file.

The engine is a real, paused behave run (`aitlc.runtime.runner:AitlcRunner` in
gate mode). behave itself runs before_all/before_scenario and the setup steps,
then parks at the target step holding the live Context and browser; `next` and
`retry` advance/re-run REAL behave Step objects over a control socket. There is
no reconstruction of behave's loop, so there is nothing to diverge from it:
Examples binding, data tables, docstrings, run-scoped data and the project's
own hooks are all behave's, because it IS behave.

Verbs:

    start <TEST-ID> [--at N]   reuse a live `cdp launch` browser (or launch an isolated one), drive real behave to step N
    status                     where the paused run is
    retry                      re-run the current step (picks up an edit first)
    next                       advance one real step
    certify [--times 2]        fresh instance, real feature, N passes required
    stop                       stop the browser and drop the session
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, checkpoint, chrome_cdp, debug_session, gate_client, gate_launch, journal
from aitlc.core.cdp_attach import inspect as cdp_inspect
from aitlc.core.dotenv import load_dotenv
from aitlc.core import workspace

app = typer.Typer(help="Interactive debug session over one feature file.")


_CAPTURED_OUTPUT_PRETTY_CHARS = 2000


def _print_pretty_step(reply: dict) -> None:
    """Render one step's result the way a real run's console shows it.

    Printed to stderr so stdout stays exactly the same JSON a script would
    parse -- this is purely for the human watching `next`/`retry`/`continue`
    live, matching the colored Given/When/Then line plus real captured
    stdout/log a plain run shows, instead of a bare JSON blob.
    """
    step_text = reply.get("step")
    if not step_text:
        return
    status = reply.get("status")
    color_on, color_off = "", ""
    if sys.stderr.isatty():
        color_on = "\033[32m" if status == "passed" else "\033[31m" if status == "failed" else ""
        color_off = "\033[0m" if color_on else ""
    duration = reply.get("duration_s")
    suffix = f"  # {duration}s" if duration is not None else ""
    keyword = reply.get("keyword")
    line = f"{keyword} {step_text}" if keyword and not step_text.startswith(keyword) else step_text
    step_index = reply.get("step_index")
    total = reply.get("total")
    prefix = f"[{step_index}/{total}] " if step_index is not None and total is not None else ""
    typer.echo(f"{color_on}{prefix}{line}{suffix}{color_off}", err=True)
    captured = reply.get("captured_output")
    # Only on a FAILURE -- a passing step's own real output (INFO:root:...
    # logging, GraphQL traffic, whatever the project logs) has nothing to
    # act on, and printing it for every single step (pass or fail) is what
    # was actually flooding the terminal, real complaint hit live. Still
    # there in full on stdout's JSON and in the journal regardless; this
    # only decides what's worth showing live.
    if captured and status == "failed":
        captured = captured.rstrip("\n")
        # A step that logs a large payload (a full GraphQL query/response,
        # say) can otherwise bury the actual pass/fail line and error text
        # under thousands of lines of noise -- capped here, in the PRETTY
        # rendering only; the raw JSON on stdout keeps the real, complete
        # captured_output for a script that actually needs it.
        if len(captured) > _CAPTURED_OUTPUT_PRETTY_CHARS:
            typer.echo(captured[-_CAPTURED_OUTPUT_PRETTY_CHARS:], err=True)
            typer.echo(
                f"... ({len(captured)} chars total, truncated -- see the raw "
                "JSON's captured_output for everything)",
                err=True,
            )
        else:
            typer.echo(captured, err=True)
    error = reply.get("error")
    if error and status == "failed":
        typer.echo(f"{color_on}{error}{color_off}", err=True)
    failed_at = reply.get("failed_at")
    if failed_at:
        typer.echo(
            f"{color_on}at {failed_at.get('file')}:{failed_at.get('line')} "
            f"in {failed_at.get('function')}{color_off}",
            err=True,
        )
    tb = reply.get("traceback")
    if tb:
        typer.echo(tb.rstrip("\n"), err=True)
    page_state = reply.get("page_state")
    if page_state:
        url = page_state.get("url")
        if url:
            typer.echo(f"{color_on}page: {url}{color_off}", err=True)
        acc = page_state.get("accessibility") or {}
        tree = acc.get("tree")
        if tree:
            typer.echo(tree, err=True)
            if acc.get("truncated"):
                typer.echo(
                    f"... ({acc.get('chars')} chars total, truncated -- "
                    "`aitlc cdp inspect --a11y` for the full tree)",
                    err=True,
                )


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


def _bp_socket(session: debug_session.DebugSession) -> str:
    """The separate socket a code-level `breakpoint()` pause parks on.

    Its mere existence on disk (checked with a real request, not just
    `os.path.exists` -- a leftover stale file must not be mistaken for a
    live pause) is the "is a breakpoint active right now" signal; see
    `_serve_breakpoint_pause`'s own docstring in runner.py for why this
    can't share the main gate socket.
    """
    return session.socket + ".bp"


def _bp_status_if_active(session: debug_session.DebugSession) -> dict | None:
    """The breakpoint pause's status, or None if none is active right now."""
    if not session.socket:
        return None
    try:
        return gate_client.request(_bp_socket(session), "status")
    except (gate_client.GateUnavailable, OSError):
        return None




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
    cdp_url: str = typer.Option(
        None,
        "--cdp-url",
        help="Attach to this existing Chrome instead of launching a new "
        "isolated one. Unset and a tracked `aitlc cdp launch` instance is "
        "live, that one is reused automatically; pass --no-cdp to force a "
        "fresh isolated browser instead.",
    ),
    no_cdp: bool = typer.Option(
        False,
        "--no-cdp",
        help="Always launch a fresh isolated browser, even if a tracked "
        "instance is live.",
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
    extra_tag: list[str] = typer.Option(
        [],
        "--extra-tag",
        help="Add this tag (without @) to the feature/scenario before its hooks "
        "run, without editing the file -- e.g. --extra-tag skip_login to make "
        "a project's own tag-driven hook logic behave as if it were tagged. "
        "Repeatable.",
    ),
    user_data_dir: Path | None = typer.Option(
        None,
        "--user-data-dir",
        "--profile-dir",
        help="Chrome profile directory for a freshly LAUNCHED browser (a "
        "persistent named profile you reuse across days, instead of "
        "aitlc's own auto-generated .cdp/profile-<port>). Only used when "
        "no live tracked instance is being reused and no --cdp-url is "
        "given -- pass --no-cdp too if a tracked instance would otherwise "
        "win.",
    ),
) -> None:
    """Attach to a live `cdp launch` browser (or launch one), drive REAL behave to a step, then park."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)

    # G89, real orphans found live: `start` used to overwrite this test_id's
    # session record unconditionally, with no attempt to stop whatever gate
    # process the PREVIOUS `debug start` for it left running -- three
    # separate real behave processes for the same test_id were found still
    # alive, from three separate `start` calls, none of which had ever been
    # told to stop. `restart` already does this same check; `start` needs
    # it just as much, since calling it again for a test_id that already
    # has a session is exactly the same situation.
    existing = debug_session.load(config.root_dir, test_id)
    if existing is not None and existing.socket:
        try:
            gate_client.request(existing.socket, "stop", cleanup=False)
        except (gate_client.GateUnavailable, OSError):
            pass

    feature = config.resolve_feature_path(test_id)
    if feature is None:
        typer.echo(json.dumps({"error": f"could not resolve feature for {test_id!r}"}), err=True)
        raise typer.Exit(code=2)
    feature = Path(feature)
    line = _example_line(feature.read_text(), example)

    socket_path = gate_launch.socket_path(config.root_dir, test_id)
    progress_path = debug_session.progress_path(config.root_dir, test_id)

    # Reuse a live `aitlc cdp launch` instance by default -- launching one
    # unconditionally (the previous behavior) orphaned it as a second window
    # every time, defeating the whole point of `cdp launch` once per day.
    instance = None
    reused = False
    resolved_cdp_url = None
    resolved_port = None
    if cdp_url:
        resolved_cdp_url = cdp_url
        resolved_port = urlparse(cdp_url).port
        reused = True
    elif not no_cdp:
        running = [i for i in chrome_cdp.list_instances(config.root_dir) if i.get("running")]
        if running:
            newest = max(running, key=lambda i: int(i.get("port", 0)))
            instance = chrome_cdp.load_state(config.root_dir, newest["port"])
            if instance is not None:
                resolved_cdp_url = instance.cdp_url
                resolved_port = instance.port
                reused = True
    if resolved_cdp_url is None:
        # No live tracked instance (or --no-cdp/unresolvable --cdp-url): a
        # fresh, isolated debug Chrome the suite will attach to; it survives
        # the gate's hard exit, so `stop` owns its teardown.
        instance, reused = chrome_cdp.launch(
            config.root_dir, port=None, window_size=window_size, user_data_dir=user_data_dir
        )
        resolved_cdp_url = instance.cdp_url
        resolved_port = instance.port

    # G75-class collision, but for `debug start` itself rather than plain
    # `run`: reusing this same test_id's own browser across repeated
    # `debug start` attempts is not caught by `is_dirty_for` (only used by
    # `run --debug`, and only trips on a *different* driver) -- any prior
    # use at all (driven_count > 0) may already be authenticated, and a
    # scenario whose own hooks/steps log in can fail there before ever
    # reaching the step actually being debugged.
    reuse_warning = None
    if reused and resolved_port:
        prior = instance or chrome_cdp.load_state(config.root_dir, resolved_port)
        prior_driven_count = getattr(prior, "driven_count", 0) if prior is not None else 0
        if prior_driven_count > 0:
            reuse_warning = (
                f"port {resolved_port} has been driven {prior_driven_count} "
                f"time(s) before (last by {getattr(prior, 'last_driven_by', None)!r}) and may "
                "already be authenticated -- if this feature's own hooks/"
                "steps log in, that step can fail against an "
                "already-logged-in browser. Use `aitlc cdp launch --new` "
                "for a fresh instance, or `@skip_login` if the reused "
                "session is intentional."
            )

    debug_session.write_progress(
        config.root_dir, test_id,
        {"state": "running", "test_id": test_id, "target": at, "done": 0,
         "started_at": time.time()},
    )
    gate_env = {"AITLC_GATE": "1", "AITLC_GATE_AT": str(at), "AITLC_GATE_EXAMPLE": str(example)}
    if extra_tag:
        gate_env["AITLC_EXTRA_TAGS"] = ",".join(extra_tag)
    proc = gate_launch.launch(
        config, feature=feature, line=line,
        cdp_url=resolved_cdp_url, socket_path_=socket_path,
        progress_path=progress_path,
        gate_env=gate_env,
        log_name="gate.log",
    )
    pid = proc.pid
    session = debug_session.DebugSession(
        test_id=test_id, feature=str(feature), cdp_url=resolved_cdp_url,
        port=resolved_port, example=example, socket=str(socket_path), pid=pid, park=at,
        index=at, reused=reused,
        log_path=str(workspace.output_path(config.root_dir, ".aitlc", "debug", "gate.log")),
    )
    debug_session.save(config.root_dir, session)
    if reused and resolved_port:
        chrome_cdp.mark_driven(config.root_dir, resolved_port, test_id)

    if background:
        payload = {
            "background": True,
            "test_id": test_id,
            "cdp_url": resolved_cdp_url,
            "reused": reused,
            "poll": f"aitlc debug status {test_id}",
            "progress_file": str(progress_path),
        }
        if reuse_warning:
            payload["warning"] = reuse_warning
        typer.echo(json.dumps(payload, indent=2))
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
        payload = {
            "test_id": test_id,
            "cdp_url": resolved_cdp_url,
            "reused": reused,
            "parked_at": status_reply.get("index"),
            "total_steps": status_reply.get("total"),
            "setup_passed": status_reply.get("index"),
            "setup_failed": 0,
        }
        if reuse_warning:
            payload["warning"] = reuse_warning
        typer.echo(json.dumps(payload, indent=2))
        return
    payload = {
        "test_id": test_id,
        "cdp_url": resolved_cdp_url,
        "reused": reused,
        "engine": "behave-gate",
        "parked_at": status_reply.get("index"),
        "total_steps": status_reply.get("total"),
        "current_step": status_reply.get("current_step"),
    }
    if reuse_warning:
        payload["warning"] = reuse_warning
    typer.echo(json.dumps(payload, indent=2))


@app.command("restart")
def restart(
    test_id: str = typer.Argument(None),
    example: int = typer.Option(
        0, "--example", help="Examples row to run, 0-based. Scenario Outlines only."
    ),
    extra_tag: list[str] = typer.Option(
        [],
        "--extra-tag",
        help="Add this tag (without @) to the feature/scenario before its hooks "
        "run, without editing the file -- e.g. --extra-tag skip_login when the "
        "same browser is already authenticated from the run being restarted. "
        "Repeatable.",
    ),
    cleanup: bool = typer.Option(
        False,
        "--cleanup",
        help="Fire the project's real after_scenario/after_feature hooks on "
        "the OLD session before restarting (same meaning as `debug stop "
        "--cleanup`). Off by default.",
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
    """Re-run this scenario from step 0, reusing the SAME browser this session already has.

    The `debug stop` + `debug start --cdp-url <same>` two-step, in one
    command -- for the case that motivated it: the scenario needs re-running
    from the top, but paying for a whole new browser (and its own login)
    defeats the point of staying in one session. `--extra-tag skip_login`
    (or whatever a project's own tag-driven hook logic checks) is the
    generic way to tell the restarted run the browser is already in a state
    its own hooks shouldn't fight -- without editing the feature file.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    old_cdp_url = session.cdp_url
    if session.socket:
        try:
            gate_client.request(session.socket, "stop", cleanup=cleanup)
        except (gate_client.GateUnavailable, OSError):
            pass
    debug_session.clear(config.root_dir, test_id)
    debug_session.clear_progress(config.root_dir, test_id)
    start(
        test_id,
        at=0,
        example=example,
        env_file=env_file,
        window_size=window_size,
        cdp_url=old_cdp_url,
        no_cdp=False,
        summary=summary,
        background=background,
        timeout=timeout,
        extra_tag=extra_tag,
    )


@app.command("status")
def status(test_id: str = typer.Argument(None)) -> None:
    """Show where the paused run is."""
    config = AitlcConfig.find_and_load()
    test_id = _default_id(config, test_id)
    session = debug_session.load(config.root_dir, test_id)
    if session is not None and session.socket:
        # Checked FIRST, before ever touching the main gate socket: while a
        # breakpoint pause is live, the main socket is busy blocked inside
        # the very step whose code hit it, so a status request there would
        # just hang until the pause resumes rather than answering quickly.
        bp_reply = _bp_status_if_active(session)
        if bp_reply is not None:
            typer.echo(
                json.dumps(
                    {"test_id": test_id, "cdp_url": session.cdp_url, **bp_reply},
                    indent=2,
                )
            )
            return
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


def _log_size(log_path: str) -> int:
    """Current size of the gate's log file, or 0 if it doesn't exist yet."""
    if not log_path:
        return 0
    try:
        return Path(log_path).stat().st_size
    except OSError:
        return 0


def _tail_log_since(log_path: str, bookmark: int) -> str:
    """Bytes appended to the gate's log file since `bookmark`, as text.

    The gate subprocess runs with --no-capture (see gate_launch.launch), so
    a step's real stdout/stderr/logging goes straight into this file rather
    than onto behave's own capture machinery -- this is how `next`/`retry`/
    `continue` show the same real console output a plain run would, without
    needing behave's capture (which --no-capture switches off regardless of
    any per-step override). Never raises: this is display plumbing riding
    along with a real step result.
    """
    if not log_path:
        return ""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(bookmark)
            data = handle.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _journal_step(config, test_id: str, command: str, reply: dict) -> None:
    """Record one step's outcome the same way plain `run` already journals
    itself -- so `aitlc journal list`/`show`/`diff` cover a debug session
    too, not just a full run, in the exact same place and shape (this
    reply already carries step/keyword/status/duration_s/error, matching
    `RunResult.to_dict()`'s "steps" entries for a plain run). Never lets a
    journaling problem fail the command it's merely recording.
    """
    try:
        journal.record(
            config.root_dir,
            command=command,
            argv=[test_id],
            exit_code=0 if reply.get("status") in (None, "passed") and not reply.get("error") else 1,
            duration_s=reply.get("duration_s") or 0.0,
            payload=reply,
            tags=["debug", test_id],
        )
    except OSError:
        pass


def _jump_first(config, session, from_line: int | None, rel: int | None) -> None:
    """Move the cursor before driving a step command -- shared by `continue
    --from`, `retry --from`/`--rel`, `next --from`/`--rel`. Mutates `session`
    in place (same contract `debug jump` itself uses) and exits(2) on a
    jump that can't land anywhere."""
    if from_line is None and rel is None:
        return
    if from_line is not None and rel is not None:
        typer.echo(json.dumps({"error": "pass --from or --rel, not both"}), err=True)
        raise typer.Exit(code=2)
    _request_or_die(session, "reload", step_dir=config.step_dir)
    kwargs = {"line": from_line} if from_line is not None else {"rel": rel}
    jump_reply = _request_or_die(session, "jump", **kwargs)
    if jump_reply.get("error"):
        typer.echo(json.dumps(jump_reply), err=True)
        raise typer.Exit(code=2)
    session.index = jump_reply["jumped_to"]
    debug_session.save(config.root_dir, session)


def _drive(config, test_id: str, cmd: str) -> None:
    """Send one stepping command to the gate and report the reply."""
    session = _require(config, test_id)
    # Pick up an edited step definition AND a Gherkin edit before running:
    # `retry`/`next` must mean "run this again with my change" — for both
    # Python and the feature file — not against a stale parse.
    reload_reply = _request_or_die(session, "reload", step_dir=config.step_dir)
    bookmark = _log_size(session.log_path)
    reply = _request_or_die(session, cmd)
    reply["captured_output"] = _tail_log_since(session.log_path, bookmark)

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
    _print_pretty_step(reply)
    _journal_step(config, test_id, f"debug {cmd}", reply)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if reply.get("status") in (None, "passed") else 1)


@app.command("retry")
def retry(
    test_id: str = typer.Argument(None),
    from_line: int = typer.Option(
        None, "--from", help="Move the cursor to this file line first, then retry that step."
    ),
    rel: int = typer.Option(
        None,
        "--rel",
        help="Move the cursor this many steps from where it's parked (e.g. -1 for the "
        "previous step, 1 for the next), then retry that step -- no need to look up "
        "its file line by hand.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Re-run the current step (picking up an edit), without advancing."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    _jump_first(config, session, from_line, rel)
    _drive(config, test_id, "retry")


@app.command("next")
def next_step(
    test_id: str = typer.Argument(None),
    from_line: int = typer.Option(
        None, "--from", help="Move the cursor to this file line first, then advance one step."
    ),
    rel: int = typer.Option(
        None,
        "--rel",
        help="Move the cursor this many steps from where it's parked (e.g. -1 for the "
        "previous step, 1 for the next), then advance one step from there.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Advance one real behave step and run it, keeping the live state."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    _jump_first(config, session, from_line, rel)
    _drive(config, test_id, "next")


_COMPACT_STEP_FIELDS = (
    "step",
    "keyword",
    "status",
    "duration_s",
    "step_index",
    "total",
    "error",
    "failed_at",
)


def _compact_step_summary(reply: dict) -> dict:
    """Just enough to see what happened, per step -- drops captured_output/
    traceback/page_state, which already streamed once, live, via that
    step's own pretty line during the run. The full record never actually
    goes away: it's what got journaled (`aitlc journal list --last 1`)."""
    return {k: reply[k] for k in _COMPACT_STEP_FIELDS if k in reply}


@app.command("continue")
def continue_steps(
    test_id: str = typer.Argument(None),
    max_steps: int = typer.Option(
        500, "--max-steps", help="Safety cap on how many steps to advance."
    ),
    from_line: int = typer.Option(
        None,
        "--from",
        help="Move the cursor to this file line first (see `debug jump`), "
        "then continue as normal -- for when you navigated the browser "
        "manually and want the session to pick up from where things "
        "actually stand.",
    ),
    rel: int = typer.Option(
        None,
        "--rel",
        help="Move the cursor this many steps from where it's parked (e.g. -1 for the "
        "previous step), then continue as normal.",
    ),
    full: bool = typer.Option(
        None,
        "--full/--compact",
        help="Final stdout summary: --full repeats every step's complete "
        "record (captured_output/traceback/page_state included); --compact "
        "keeps only step/status/duration/error (already shown once, live, "
        "per step, during the run). Defaults to [debug].continue_output in "
        "aitlc.toml (\"compact\" if unset). The journal always keeps the "
        "full record either way -- see `aitlc journal list --last 1`.",
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
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    compact_output = not full if full is not None else config.debug.continue_output != "full"

    _jump_first(config, session, from_line, rel)

    results = []
    for _ in range(max_steps):
        reload_reply = _request_or_die(session, "reload", step_dir=config.step_dir)
        bookmark = _log_size(session.log_path)
        reply = _request_or_die(session, "next")
        reply["captured_output"] = _tail_log_since(session.log_path, bookmark)
        if isinstance(reload_reply, dict) and reload_reply.get("feature"):
            reply["feature"] = reload_reply["feature"]
        if isinstance(reload_reply, dict) and reload_reply.get("reloaded_modules"):
            reply["reloaded_modules"] = reload_reply["reloaded_modules"]
        if isinstance(reload_reply, dict) and reload_reply.get("stale_modules"):
            reply["stale_modules"] = reload_reply["stale_modules"]
            reply["warning"] = reload_reply["warning"]
        results.append(reply)
        _print_pretty_step(reply)
        if reply.get("index") is not None:
            session.index = reply["index"]
            debug_session.save(config.root_dir, session)
        if reply.get("status") not in (None, "passed") or reply.get("finished"):
            break

    last_status = results[-1].get("status") if results else None
    payload = {
        "steps_run": len(results),
        "stopped_reason": (
            "finished" if results and results[-1].get("finished")
            else "failed" if last_status not in (None, "passed")
            else "max_steps_reached"
        ),
        "results": results,
    }
    try:
        journal.record(
            config.root_dir,
            command="debug continue",
            argv=[test_id],
            exit_code=0 if last_status in (None, "passed") else 1,
            duration_s=sum(r.get("duration_s") or 0.0 for r in results),
            payload=payload,
            tags=["debug", "continue", test_id],
        )
    except OSError:
        pass
    echo_payload = payload
    if compact_output:
        echo_payload = {**payload, "results": [_compact_step_summary(r) for r in results]}
    typer.echo(json.dumps(echo_payload, indent=2))
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
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
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
    _print_pretty_step(reply)
    _journal_step(config, test_id, "debug run-text", reply)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if reply.get("status") in (None, "passed") else 1)


@app.command("run-line")
def run_line(
    line: int = typer.Argument(
        ..., help="1-based line number in the feature file, e.g. the line you're looking at."
    ),
    test_id: str = typer.Argument(None),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Run the step at this file line against the live paused context.

    Same idea as `run-text` (doesn't advance the debug cursor), but you
    point at a line instead of retyping the step's exact text -- no
    quoting, no escaping, no copying table rows by hand. Reads the REAL
    bound Step object from the file (same file-reparse `next`/`retry` use,
    so a Scenario Outline's <placeholder>s are already substituted), so
    it's higher-fidelity than `run-text`, not just more convenient.
    """
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    reload_reply = _request_or_die(session, "reload", step_dir=config.step_dir)
    reply = _request_or_die(session, "run_line", line=line)
    if isinstance(reload_reply, dict) and reload_reply.get("reloaded_modules"):
        reply["reloaded_modules"] = reload_reply["reloaded_modules"]
    if isinstance(reload_reply, dict) and reload_reply.get("stale_modules"):
        reply["stale_modules"] = reload_reply["stale_modules"]
        reply["warning"] = reload_reply["warning"]
    _print_pretty_step(reply)
    _journal_step(config, test_id, "debug run-line", reply)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if reply.get("status") == "passed" and not reply.get("error") else 1)


@app.command("jump")
def jump(
    line: int = typer.Argument(
        None,
        help="1-based line number in the feature file to move the cursor to. "
        "Omit if passing --rel instead.",
    ),
    test_id: str = typer.Argument(None),
    rel: int = typer.Option(
        None,
        "--rel",
        help="Move the cursor this many steps from where it's parked instead of an "
        "absolute line -- e.g. --rel -1 for the previous step, --rel 1 for the next. "
        "Mutually exclusive with the line argument.",
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Move the debug cursor to the step at (or nearest before) this file line -- no execution.

    For when the browser is no longer where the session thinks it is (you
    navigated manually, clicked ahead, went back) and you want `next`/
    `retry`/`continue` to pick up from where things actually stand, without
    re-running or skip-verifying anything in between. `run-line` is the
    opposite case: run one step without moving the cursor; this moves the
    cursor without running anything.
    """
    if (line is None) == (rel is None):
        typer.echo(
            json.dumps({"error": "pass exactly one of: line argument, --rel"}), err=True
        )
        raise typer.Exit(code=2)
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    test_id = _default_id(config, test_id)
    session = _require(config, test_id)
    _request_or_die(session, "reload", step_dir=config.step_dir)
    kwargs = {"line": line} if line is not None else {"rel": rel}
    reply = _request_or_die(session, "jump", **kwargs)
    if reply.get("jumped_to") is not None:
        session.index = reply["jumped_to"]
        debug_session.save(config.root_dir, session)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if not reply.get("error") else 1)


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
    # A breakpoint pause blocks the main socket (see _bp_status_if_active's
    # comment in `status`) -- route there instead when one is active, so
    # `eval` still works for exactly the case it matters most: inspecting
    # state while genuinely paused mid-step.
    if _bp_status_if_active(session) is not None:
        try:
            reply = gate_client.request(_bp_socket(session), "eval", expr=expr)
        except (gate_client.GateUnavailable, OSError):
            reply = {"error": "the breakpoint pause is no longer reachable"}
    else:
        reply = _request_or_die(session, "eval", expr=expr)
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if not reply.get("error") else 1)


@app.command("py")
def py_eval(
    expr: str = typer.Argument(..., help="Python expression to evaluate in the paused frame."),
    test_id: str = typer.Argument(None),
) -> None:
    """Evaluate a Python expression in a paused `breakpoint()`'s own scope.

    Only meaningful while `debug status` shows `"paused_at": "breakpoint"`
    -- this is the actual point of breakpoint support: `debug eval` (JS on
    the page) already works at any gate pause, so it can't see local
    variables in the failing Python code. This can, evaluated exactly like
    `pdb`'s `p <expr>` against the frame where `breakpoint()` was written.
    """
    config = AitlcConfig.find_and_load()
    session = _require(config, _default_id(config, test_id))
    if _bp_status_if_active(session) is None:
        typer.echo(
            json.dumps(
                {
                    "error": "no breakpoint is currently paused",
                    "hint": "check `debug status` for \"paused_at\": \"breakpoint\" first",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        reply = gate_client.request(_bp_socket(session), "pyeval", expr=expr)
    except (gate_client.GateUnavailable, OSError):
        reply = {"error": "the breakpoint pause is no longer reachable"}
    typer.echo(json.dumps(reply, indent=2))
    raise typer.Exit(code=0 if not reply.get("error") else 1)


@app.command("resume")
def resume(test_id: str = typer.Argument(None)) -> None:
    """Continue past a code-level `breakpoint()`, exactly where it paused.

    Only meaningful while a `breakpoint()` in project code is actually
    paused there (see `PYTHONBREAKPOINT` in gate_launch.launch) -- `debug
    status` shows `"paused_at": "breakpoint"` when one is. This is the
    only thing that unblocks it: the original call stays frozen until
    this arrives, then returns and keeps running from exactly that line --
    nothing restarted, nothing lost. `next`/`retry` on the same session
    will just hang until this is sent first, since the main gate socket
    stays busy for as long as the breakpoint is paused.
    """
    config = AitlcConfig.find_and_load()
    session = _require(config, _default_id(config, test_id))
    try:
        reply = gate_client.request(_bp_socket(session), "resume")
    except (gate_client.GateUnavailable, OSError):
        typer.echo(
            json.dumps(
                {
                    "error": "no breakpoint is currently paused",
                    "hint": "check `debug status` for \"paused_at\": \"breakpoint\" first",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    typer.echo(json.dumps(reply, indent=2))


@app.command("screenshot")
def screenshot(
    test_id: str = typer.Argument(None),
    path: Path = typer.Option(
        None,
        "--path",
        "-o",
        help="Where to save it. Defaults to .aitlc/debug/<test_id>-screenshot.png.",
    ),
    full_page: bool = typer.Option(
        False, "--full-page", help="Full-page screenshot, not just viewport."
    ),
) -> None:
    """Screenshot of the live paused page -- no need to know or type its CDP port.

    `cdp inspect --screenshot` already does this, but requires the port by
    hand every time even though a debug session already knows its own
    `cdp_url` -- the exact friction `next`/`retry`/`eval`/`run-text` don't
    have. This is that same session-aware convenience for a screenshot.
    """
    config = AitlcConfig.find_and_load()
    session = _require(config, _default_id(config, test_id))
    if path is None:
        safe = session.test_id.replace("/", "_").replace(" ", "_")
        path = workspace.output_path(config.root_dir, ".aitlc", "debug", f"{safe}-screenshot.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = cdp_inspect(session.cdp_url, screenshot_path=path, full_page=full_page)
    typer.echo(json.dumps(result.to_dict(), indent=2))
    raise typer.Exit(code=0 if result.screenshot_path else 1)


@app.command("inspect")
def inspect_page(
    test_id: str = typer.Argument(None),
    a11y_query: str = typer.Option(
        None,
        "--a11y-query",
        help="Return only accessibility lines containing this text.",
    ),
    a11y_selector: str = typer.Option(
        None, "--a11y-selector", help="Scope the accessibility tree to this selector's subtree."
    ),
    all_nodes: bool = typer.Option(
        False, "--a11y-all", help="Keep semantically uninteresting nodes too."
    ),
) -> None:
    """Accessibility snapshot of the live paused page -- no CDP port needed.

    Session-aware version of `cdp inspect --a11y`: same accessibility tree
    (cheap, greppable, shows nesting and control state), resolved from the
    session's own `cdp_url` instead of a port you have to look up and type.
    """
    config = AitlcConfig.find_and_load()
    session = _require(config, _default_id(config, test_id))
    result = cdp_inspect(
        session.cdp_url,
        accessibility=True,
        interesting_only=not all_nodes,
        a11y_selector=a11y_selector,
        a11y_query=a11y_query,
    )
    typer.echo(json.dumps(result.to_dict(), indent=2))


def _gate_alive(session: debug_session.DebugSession) -> bool:
    """Is this session's gate subprocess actually still answering?

    A session file persists until `debug stop` clears it -- if that never
    happens (the process was killed out of band, crashed, or the machine
    restarted), the file is pure stale bookkeeping pointing at a socket
    nothing is listening on any more. Reusing the same
    GateUnavailable/OSError-means-dead convention `stop`/`eval`/`status`
    already use elsewhere, rather than inventing a new liveness check.
    """
    if not session.socket:
        return False
    try:
        gate_client.request(session.socket, "status")
        return True
    except (gate_client.GateUnavailable, OSError):
        return False


@app.command("list")
def list_sessions(
    prune: bool = typer.Option(
        False,
        "--prune",
        help=(
            "Also drop tracked sessions whose gate process is no longer "
            "reachable (crashed, killed out of band, or from before a "
            "restart) -- deletes only their bookkeeping files, never a "
            "browser. A session still answering its socket is left alone "
            "even with this on: it may just be a debug session the user "
            "hasn't finished with yet, not something to guess about."
        ),
    ),
) -> None:
    """List every tracked debug session -- test_id, browser, where it's parked.

    For juggling more than one session/browser at once: which `next`/
    `retry`/`eval` (no explicit test_id) would hit which browser is
    otherwise invisible without checking each `debug status <test_id>` by
    hand.
    """
    config = AitlcConfig.find_and_load()
    sessions = debug_session.list_all(config.root_dir)
    live_ports = {i["port"] for i in chrome_cdp.list_instances(config.root_dir) if i.get("running")}
    pruned = []
    rows = []
    for s in sessions:
        gate_alive = _gate_alive(s)
        if prune and not gate_alive:
            debug_session.clear(config.root_dir, s.test_id)
            debug_session.clear_progress(config.root_dir, s.test_id)
            pruned.append(s.test_id)
            continue
        rows.append(
            {
                "test_id": s.test_id,
                "cdp_url": s.cdp_url,
                "port": s.port,
                "browser_alive": s.port in live_ports,
                "gate_alive": gate_alive,
                "parked_at": s.index,
                "reused": s.reused,
            }
        )
    payload = {"sessions": rows, "count": len(rows)}
    if prune:
        payload["pruned"] = pruned
    typer.echo(json.dumps(payload, indent=2))


def _find_orphaned_gate_pids(root_dir: Path, known_pids: set[int]) -> list[int]:
    """PIDs of real OS processes running THIS project's gate that no known
    session record points at.

    `debug list --prune` only ever prunes SESSION RECORDS whose gate is
    unreachable -- it has nothing to check if there was never a session
    record to begin with (a crash, a killed shell, or `debug start`
    overwriting one without stopping the old process first, the exact gap
    G89 fixed -- pre-existing orphans from before that fix have no record
    at all). This looks at real `ps` output instead: any process whose
    command line shows it was launched with this exact `--runner`/
    `--runner-class` attach AND whose feature-file argument sits under
    THIS project's root, that isn't one of the pids known sessions already
    account for.  Unix-only (`ps`), same assumption the whole gate/socket
    mechanism already makes.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True, check=True, timeout=5
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    root = str(Path(root_dir).resolve())
    pids = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        pid_str, _, rest = line.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid in known_pids:
            continue
        is_gate = (
            "aitlc.runtime.runner:AitlcRunner" in rest
            or "aitlc.runtime.runner.AitlcRunner" in rest
        )
        if is_gate and root in rest:
            pids.append(pid)
    return pids


@app.command("reap")
def reap(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be killed, without killing anything."
    ),
) -> None:
    """Kill every real gate process for THIS project that no session record points at.

    For orphans `debug list --prune` can't see -- it only prunes stale
    SESSION RECORDS; a process that was orphaned before a record-based fix
    existed (or from a crash, a killed shell) has no record at all, so
    there's nothing for `--prune` to find. This looks at real OS processes
    instead, scoped to this project's own gate invocations only -- an
    unrelated `aitlc` session for a different project on the same machine
    is never touched.
    """
    config = AitlcConfig.find_and_load()
    known_pids = {s.pid for s in debug_session.list_all(config.root_dir) if s.pid}
    orphans = _find_orphaned_gate_pids(config.root_dir, known_pids)
    if dry_run:
        typer.echo(json.dumps({"would_kill": orphans, "count": len(orphans)}, indent=2))
        return
    killed = []
    for pid in orphans:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
    typer.echo(json.dumps({"killed": killed, "count": len(killed)}, indent=2))


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
    kill_browser: bool = typer.Option(
        False,
        "--kill-browser",
        help=(
            "Kill the browser even if this session only attached to an "
            "already-live tracked instance (one it did not launch itself). "
            "Default: a reused browser is left running so `aitlc cdp launch` "
            "keeps its point -- only a browser this session actually "
            "launched is killed automatically."
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
    #
    # A session that only *attached* to a live tracked instance (session.
    # reused) did not launch that browser and should not kill it either --
    # otherwise every `debug stop` after the reuse fix (G68) would tear down
    # the exact persistent browser `aitlc cdp launch` exists to keep alive.
    browser_left_running = False
    if session.reused and not kill_browser:
        stopped_this_session = False
        browser_left_running = True
    else:
        stopped_this_session = chrome_cdp.stop(config.root_dir, port=session.port)
    debug_session.clear(config.root_dir, test_id)
    debug_session.clear_progress(config.root_dir, test_id)
    payload = {
        "gate_stopped": gate_stopped,
        "stopped_port": session.port if stopped_this_session else None,
        "browser_left_running": browser_left_running,
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