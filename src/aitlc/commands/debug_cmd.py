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
from pathlib import Path

import typer
from aitlc.config import AitlcConfig
from aitlc.core import behave_runner, chrome_cdp, debug_session
from aitlc.core.dotenv import load_dotenv
from aitlc.core.step_console import run_console

app = typer.Typer(help="Interactive debug session over one feature file.")


def _slice_file(root_dir: Path, steps: list[str]) -> Path:
    """Materialise steps as a parseable feature under the project's tree.

    Written inside the project rather than a temp dir because Behave resolves
    its steps directory relative to the feature: a slice in /tmp dies with
    "No steps directory", which is a confusing way to learn that.
    """
    out = root_dir / "reports" / ".aitlc" / "debug" / "_slice.feature"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"\t{s.strip()}" for s in steps)
    out.write_text(
        "Feature: aitlc debug slice\n\n\t@skip_login\n\tScenario: slice\n" + body + "\n"
    )
    return out


def _run_steps(config, session: debug_session.DebugSession, steps: list[str]) -> dict:
    """Dispatch a list of steps into the session's browser."""
    if not steps:
        return {"results": []}
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


@app.command("start")
def start(
    test_id: str = typer.Argument(..., help="Test ID or feature file path."),
    at: int = typer.Option(
        0, "--at", help="Step index to park on (0-based). Steps before it are run."
    ),
    env_file: str = typer.Option(".env", "--env-file"),
) -> None:
    """Launch an isolated browser and drive the feature to a step."""
    config = AitlcConfig.find_and_load()
    load_dotenv(config.root_dir / env_file)
    feature = config.resolve_feature_path(test_id)
    steps = debug_session.feature_steps(Path(feature).read_text())
    if not steps:
        typer.echo(json.dumps({"error": f"no steps found in {feature}"}), err=True)
        raise typer.Exit(code=2)

    # Always a fresh, isolated browser: a long-lived shared profile accumulates
    # sessions and eventually fails a run at the framework's own login, which
    # reads as a test bug rather than a dirty profile.
    instance = chrome_cdp.launch(config.root_dir, port=None)
    session = debug_session.DebugSession(
        test_id=test_id,
        feature=str(feature),
        cdp_url=instance.cdp_url,
        port=instance.port,
        steps=steps,
        index=max(0, min(at, len(steps))),
    )
    ran = _run_steps(config, session, session.slice_through(session.index))
    debug_session.save(config.root_dir, session)
    typer.echo(
        json.dumps(
            {
                "test_id": test_id,
                "cdp_url": session.cdp_url,
                "total_steps": len(steps),
                "parked_at": session.index,
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
    if session.finished:
        typer.echo(json.dumps({"done": True, "message": "no steps left"}))
        return
    if advance:
        session.advance()
        if session.finished:
            debug_session.save(config.root_dir, session)
            typer.echo(json.dumps({"done": True, "message": "reached the end"}))
            return
    step = session.current or ""
    out = _run_steps(config, session, [step])
    status_ = out["results"][0]["status"] if out["results"] else "failed"
    session.record(status_)
    debug_session.save(config.root_dir, session)
    payload = {
        "step": step.strip(),
        "index": session.index,
        "status": status_,
        "attempts_here": len(session.attempts_for_current()),
        **({"error": out["results"][0]["error"]} if out["results"] else {}),
    }
    if out.get("stderr_tail"):
        payload["stderr_tail"] = out["stderr_tail"]
    typer.echo(json.dumps(payload, indent=2))
    raise typer.Exit(code=0 if status_ == "passed" else 1)


@app.command("retry")
def retry(test_id: str = typer.Argument(...)) -> None:
    """Re-run the current step after an edit, without restarting the scenario."""
    config = AitlcConfig.find_and_load()
    _run_current(config, _require(config, test_id), advance=False)


@app.command("next")
def next_step(test_id: str = typer.Argument(...)) -> None:
    """Advance one step and run it, keeping the state you already have."""
    config = AitlcConfig.find_and_load()
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
    stopped = chrome_cdp.stop_all(config.root_dir)
    debug_session.clear(config.root_dir, test_id)
    typer.echo(json.dumps({"stopped_ports": stopped, "session_cleared": True}))
