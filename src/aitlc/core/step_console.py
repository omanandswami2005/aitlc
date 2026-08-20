"""Structured, args-based step console.

Runs a slice of a scenario through behave's real step registry —
`registry.find_match` dispatch, line-range selection, Examples-row
substitution — in a non-interactive form that emits one JSON object per
step (JSON-lines) rather than colored text meant for human eyes. An
interactive REPL is the natural shape for a person and the wrong shape for
a program: a tool whose only output is colored terminal text cannot be
driven reliably by anything but a human.

This file is BOTH a real importable aitlc module (the `run_console()` API
below) AND a directly-runnable script (`if __name__ == "__main__"`) — the
latter is what actually executes, via `poetry run python3 <this file's
real path> <args>` inside the TARGET PROJECT's own environment. Unlike
live_status.py this does NOT need to be materialized/copied elsewhere
first: `poetry run python3 /any/path/script.py` already runs under the
target's own venv (behave, playwright, and the target's own step modules
all resolve correctly) regardless of where the script file physically
lives — the constraint that forced live_status.py's copy-into-PYTHONPATH
trick was specific to behave's `-f module:Class` loader, which this
doesn't use.

Login is deliberately NOT a hardcoded project import — the obvious
implementation reaches straight for a project's own sign-in page object,
and a generic core module cannot assume any project's login page-object
shape. Instead `--login-step "<gherkin text>"` runs
through the SAME registry dispatch as everything else, since a login
step is itself just a registered step like any other.
"""

from __future__ import annotations

import json
from datetime import datetime
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_object(spec: str | None) -> Any:
    """Import "module:Attr" from the target project, or None.

    Returns None rather than raising so a project that does not use the
    thing being looked up simply proceeds without it — an optional
    integration point must not be a hard requirement.
    """
    if not spec:
        return None
    module_path, _, attr = spec.partition(":")
    if not module_path or not attr:
        return None
    try:
        import importlib

        return getattr(importlib.import_module(module_path), attr)
    except Exception:
        return None


class _ConfigShim:
    """Minimal stand-in for `context.config`.

    A project's scenario-setup hook may read/write behave config fields
    (some set `context.config.logging_format`). Missing attributes
    return None rather than raising, so a hook touching a field this shim
    doesn't model degrades instead of aborting setup entirely.
    """

    def __init__(self) -> None:
        self.logging_format = "%(levelname)s:%(name)s:%(message)s"

    def __getattr__(self, name: str) -> None:
        return None


def _parse_scenario(feature_file: str, playwright_free: bool = True) -> Any:
    """Return the first real Scenario object from a feature file, or None.

    A genuine behave Scenario (not a stub) is what makes the project's own
    hook usable unmodified: hooks read `scenario.effective_tags`,
    `scenario.tags`, `scenario.name` and may call `scenario.skip()`.
    Hand-rolling those is how reimplementations drift from the real
    before_scenario behavior.
    """
    try:
        from behave.parser import parse_file

        feature = parse_file(feature_file)
        if feature is None:
            return None
        scenarios = list(feature.walk_scenarios()) or list(feature.scenarios)
        return scenarios[0] if scenarios else None
    except Exception:
        return None


def apply_scenario_setup(context: Any, *, feature_file: str, spec: str | None) -> dict:
    """Run the target project's own per-scenario setup against `context`.

    Why this exists: a step slice executed outside a real behave run gets
    none of `before_scenario`. Observed live on a real suite: that hook
    generated ~27 per-scenario environment variables (unique emails, names
    and similar), so without it a step that creates a user from one of
    those slots sent a null value to the API. The user was never created,
    the login that followed could not succeed, and the failure surfaced
    189 seconds later as a login-validation timeout — three steps away
    from the real cause.

    Rather than reimplement that setup (which would silently drift from
    the project's own), `spec` names the project's real callable as
    `module.path:function`, and it is invoked with the same
    `(context, scenario)` signature behave uses.

    Returns a record describing what happened. `status` is one of:
      ok       - the hook ran
      skipped  - no hook configured (explicitly `none`)
      failed   - a hook was configured but could not be run

    Never silently returns success when setup did not happen: silence is
    exactly what made the original failure so expensive to diagnose.
    """
    if spec in (None, "", "none"):
        return {
            "event": "scenario_setup",
            "status": "skipped",
            "detail": "no scenario setup configured; per-scenario data will be absent",
        }

    module_path, _, func_name = spec.partition(":")
    if not module_path or not func_name:
        return {
            "event": "scenario_setup",
            "status": "failed",
            "detail": f"invalid spec {spec!r}; expected 'module.path:function'",
        }

    try:
        import importlib

        module = importlib.import_module(module_path)
        setup_func = getattr(module, func_name)
    except Exception as exc:
        return {
            "event": "scenario_setup",
            "status": "failed",
            "detail": f"could not import {spec}: {exc}",
        }

    scenario = _parse_scenario(feature_file)
    if scenario is None:
        return {
            "event": "scenario_setup",
            "status": "failed",
            "detail": f"could not parse a Scenario from {feature_file}",
        }

    # Attributes real hooks expect to already exist on the context.
    if not hasattr(context, "scenario_variables") or context.scenario_variables is None:
        context.scenario_variables = {}
    if not hasattr(context, "config"):
        context.config = _ConfigShim()
    if not hasattr(context, "graphql_calls"):
        context.graphql_calls = []
    if not hasattr(context, "graphql_request_map"):
        context.graphql_request_map = {}
    if not hasattr(context, "_finalizers"):
        context._finalizers = []

    # Snapshot the environment so the report can show what the hook actually
    # produced. Diffing beats naming expected keys: which variables a setup
    # hook writes is entirely project-specific, and a hardcoded list would
    # silently report nothing for any project that names them differently.
    before = dict(os.environ)

    try:
        setup_func(context, scenario)
    except Exception as exc:
        return {
            "event": "scenario_setup",
            "status": "failed",
            "detail": f"{spec} raised: {type(exc).__name__}: {exc}",
        }

    changed = sorted(k for k, v in os.environ.items() if before.get(k) != v)

    return {
        "event": "scenario_setup",
        "status": "ok",
        "hook": spec,
        "scenario": getattr(scenario, "name", None),
        # Surfaced so a caller can see the data really landed rather than
        # inferring it from a later step's success. Values are omitted:
        # per-scenario data routinely includes addresses and account
        # identifiers, and this record is printed to stdout.
        "env_vars_set": changed,
        "env_vars_set_count": len(changed),
    }


def _mobile_context_options(playwright: Any, device_name: str) -> dict:
    """Playwright device-emulation context options for --mobile.

    Real gap found live debugging a mobile_browser scenario through this
    console: both branches below previously always created a plain desktop
    context — the CDP-attach branch reused the target browser's existing
    desktop-shaped context, and the fresh-launch branch never applied
    device emulation at all — silently running "mobile" steps at desktop
    viewport with no error. playwright.devices is Playwright's own generic
    catalog, not a project-specific concept, so this stays project-agnostic.
    """
    if device_name not in playwright.devices:
        example_keys = ", ".join(sorted(playwright.devices)[:5])
        raise SystemExit(
            f"Unknown Playwright device {device_name!r}. Examples: {example_keys}, ... "
            "(any key from playwright.sync_api.Playwright.devices)"
        )
    options = dict(playwright.devices[device_name])
    options.pop("default_browser_type", None)
    options["accept_downloads"] = True
    return options


def _stamp(epoch: float) -> str:
    """A local-time ISO stamp for correlating a step with an external log.

    Local rather than UTC because the logs it gets compared against -- CI
    console output, an application's own log -- are read in local time, and
    a stamp nobody can line up by eye does not get used.
    """
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


@dataclass
class StepResult:
    """One dispatched step and how it ended."""

    step: str
    status: str
    duration_s: float
    error: str | None = None
    # Absolute wall-clock, not just a duration. A duration answers "how long
    # did this step take"; it cannot answer "when did the app clear that
    # banner", which is the question asked whenever a wait is tuned against
    # a real backend job. Correlating a step with a server-side log needs a
    # timestamp both sides share.
    started_at: str = ""
    ended_at: str = ""


_KNOWN_EVENTS = frozenset(
    {
        "loaded_step_modules",
        "scenario_setup",
        "mobile_emulation",
        "done",
        "step_module_skipped",
    }
)


@dataclass
class ConsoleRunResult:
    """Everything a step-console run produced."""

    loaded_step_modules: int
    results: list[StepResult]
    exit_code: int
    # Everything the child said that this parser has no branch for. Without
    # these two, a fatal child error is indistinguishable from a run that
    # simply had no steps: the child writes the reason to stderr (or emits a
    # parse_error event) and the caller prints {"results": []} with exit 0.
    stderr_tail: str = ""
    unhandled_events: list[dict] = field(default_factory=list)
    network: list[dict] = field(default_factory=list)
    trace_path: str = ""
    # What the project's per-scenario setup did. Surfaced rather than kept
    # internal: "setup silently did not run" is the failure mode that makes
    # later steps fail for reasons that look unrelated.
    scenario_setup: dict | None = None

    @property
    def failed(self) -> list[StepResult]:
        """Only the steps that failed."""
        return [r for r in self.results if r.status == "failed"]

    @property
    def passed(self) -> bool:
        """True when the console run exited successfully."""
        return self.exit_code == 0


def call_project_function(
    spec: str,
    *,
    cwd: Path,
    poetry_cmd: list[str],
    args: list[str] | None = None,
    cdp_url: str | None = None,
    step_dir: str = "features/steps",
    scenario_setup: str | None = None,
    browser_actions: str | None = None,
    pass_browser: str = "auto",
    feature_file: Path | None = None,
) -> dict:
    """Call one project function in the target's interpreter and return its record.

    A separate entry point rather than a step, because the thing worth calling
    is usually not a step: a page object's private helper that answers "which
    user does the app think is signed in" has no Gherkin expression, and
    checking it meant writing a Playwright script by hand.
    """
    script_path = Path(__file__).resolve()
    target_feature = feature_file or script_path.parent / "_empty.feature"
    if feature_file is None and not target_feature.exists():
        target_feature.write_text("Feature: call\n\n  Scenario: call\n")

    cmd = poetry_cmd + ["run", "python3", str(script_path), str(target_feature)]
    cmd += ["--call", spec, "--step-dir", step_dir]
    cmd += ["--call-pass-browser", pass_browser]
    for value in args or []:
        cmd += ["--call-arg", value]
    if cdp_url:
        cmd += ["--cdp-url", cdp_url]
    if scenario_setup:
        cmd += ["--scenario-setup", scenario_setup]
    else:
        cmd.append("--allow-missing-setup")
    if browser_actions:
        cmd += ["--browser-actions", browser_actions]

    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") in ("call_result", "call_error"):
            return record
    return {
        "event": "call_error",
        "target": spec,
        "error": "the console produced no result",
        "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-12:]),
    }


class ConsoleUnavailable(RuntimeError):
    """The persistent console could not be reached; fall back to a one-shot run."""


def console_socket(root_dir: Path, test_id: str) -> Path:
    """Where a session's step-console socket lives.

    Deliberately in the system temp directory rather than under the project.
    A Unix socket path is capped at roughly 104 bytes by the OS -- not by
    Python -- and `<project>/reports/.aitlc/console-<test-id>.sock` blows
    past that inside any deeply nested checkout, failing at bind() with
    "AF_UNIX path too long". Found by running the tests from a temp
    directory, which is exactly the kind of path a real project can have.

    The digest keys the socket to this project *and* this test, so two
    checkouts, or two sessions, never collide on one socket.
    """
    import hashlib
    import tempfile

    digest = hashlib.sha256(
        f"{Path(root_dir).resolve()}::{test_id}".encode()
    ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"aitlc-console-{digest}.sock"


def request_steps(socket_path: Path, steps: list[str], *, timeout: float = 900.0) -> dict:
    """Ask a running console to execute a batch, and return its reply.

    Raises ConsoleUnavailable for anything that means "no server there", so
    the caller can fall back to spawning a process rather than failing. A
    debugging tool that stops working because an optimisation is missing is
    worse than one that is merely slow.
    """
    import socket as _socket

    if not socket_path.exists():
        raise ConsoleUnavailable(f"no console socket at {socket_path}")
    try:
        client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(socket_path))
    except OSError as exc:
        raise ConsoleUnavailable(f"console not answering: {exc}") from exc

    with client:
        client.sendall((json.dumps({"steps": steps}) + "\n").encode())
        payload = b""
        while not payload.endswith(b"\n"):
            try:
                chunk = client.recv(65536)
            except OSError as exc:
                raise ConsoleUnavailable(f"console went away: {exc}") from exc
            if not chunk:
                break
            payload += chunk

    if not payload.strip():
        raise ConsoleUnavailable("console returned nothing")
    try:
        return json.loads(payload.decode())
    except json.JSONDecodeError as exc:
        raise ConsoleUnavailable(f"console returned invalid JSON: {exc}") from exc


def console_is_alive(socket_path: Path, *, timeout: float = 2.0) -> bool:
    """Whether a console is listening and answering on this socket.

    A stale socket file left by a killed process looks identical to a live
    one on disk, so liveness is a round trip, not an existence check.
    """
    import socket as _socket

    if not socket_path.exists():
        return False
    try:
        client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(socket_path))
        with client:
            client.sendall((json.dumps({"cmd": "ping"}) + "\n").encode())
            return b"alive" in client.recv(4096)
    except OSError:
        return False


def stop_console(socket_path: Path, *, timeout: float = 5.0) -> bool:
    """Ask a console to shut down. True if one was there to stop."""
    import socket as _socket

    if not socket_path.exists():
        return False
    try:
        client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(str(socket_path))
        with client:
            client.sendall((json.dumps({"cmd": "stop"}) + "\n").encode())
            client.recv(4096)
        return True
    except OSError:
        return False


def run_console(
    feature_file: Path,
    *,
    cwd: Path,
    poetry_cmd: list[str],
    line_range: str | None = None,
    example_row: int = 0,
    step_dir: str = "features/steps",
    cdp_url: str | None = None,
    login_step: str | None = None,
    mobile: str | None = None,
    scenario_setup: str | None = None,
    allow_missing_setup: bool = False,
    browser_actions: str | None = None,
    browser_factory: str | None = None,
    trace: str | None = None,
    capture_network: bool = False,
    progress_file: str | None = None,
) -> ConsoleRunResult:
    """Run a slice of a feature file's steps, parsing the JSON-lines output.

    mobile: a playwright.devices key (e.g. "Galaxy S8"), or None for
    desktop. Required for TEST_TYPE=mobile_browser scenarios — without it
    steps run at desktop viewport with no error (a real gap found live).
    """
    script_path = Path(__file__).resolve()
    cmd = poetry_cmd + ["run", "python3", str(script_path), str(feature_file)]
    cmd += ["--step-dir", step_dir]
    if line_range:
        cmd += ["--range", line_range]
    if example_row:
        cmd += ["--example-row", str(example_row)]
    if cdp_url:
        cmd += ["--cdp-url", cdp_url]
    if login_step:
        cmd += ["--login-step", login_step]
    if mobile:
        cmd += ["--mobile", mobile]
    if scenario_setup:
        cmd += ["--scenario-setup", scenario_setup]
    if allow_missing_setup:
        cmd.append("--allow-missing-setup")
    if browser_actions:
        cmd += ["--browser-actions", browser_actions]
    if browser_factory:
        cmd += ["--browser-factory", browser_factory]
    if trace:
        cmd += ["--trace", trace]
    if capture_network:
        cmd.append("--capture-network")
    if progress_file:
        cmd += ["--progress-file", str(progress_file)]

    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    loaded = 0
    setup_record: dict | None = None
    results: list[StepResult] = []
    unhandled: list[dict] = []
    network: list[dict] = []
    trace_path = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = record.get("event")
        if event == "loaded_step_modules":
            loaded = record.get("count", 0)
        elif event == "scenario_setup":
            setup_record = record
        elif event == "network":
            network = record.get("responses", [])
        elif event == "trace_saved":
            trace_path = record.get("path", "")
        elif event in (None, "") and "step" not in record:
            unhandled.append(record)
        elif event and "step" not in record and event not in _KNOWN_EVENTS:
            unhandled.append(record)
        elif "step" in record:
            results.append(
                StepResult(
                    step=record["step"],
                    status=record["status"],
                    duration_s=record.get("duration_s", 0.0),
                    error=record.get("error"),
                    started_at=record.get("started_at", ""),
                    ended_at=record.get("ended_at", ""),
                )
            )

    return ConsoleRunResult(
        loaded_step_modules=loaded,
        results=results,
        stderr_tail="\n".join(proc.stderr.strip().splitlines()[-12:]),
        unhandled_events=unhandled,
        network=network,
        trace_path=trace_path,
        exit_code=proc.returncode,
        scenario_setup=setup_record,
    )


# ---------------------------------------------------------------------------
# Everything below this line is the directly-runnable script half. It only
# imports behave/playwright/the target project's own modules — never aitlc
# itself, since it executes inside the TARGET's environment, not aitlc's.
# ---------------------------------------------------------------------------


def _run_call(spec, *, raw_args, pass_browser, browser_handle, page, context) -> None:
    """Call one project function against the live browser and print the result.

    The fast loop stops at the Gherkin boundary, and real debugging keeps
    crossing it: asserting on a page object's private helper -- "which user
    does the app think is signed in" -- is not a step and had no expression
    at all, so it was done with hand-written scripts against the same
    browser.

    Runs inside the *target project's* interpreter, which is the only place
    its modules are importable. Whether the browser handle is passed is
    detected from the signature by default, because project helpers are
    inconsistent about it and guessing wrong produces a TypeError that looks
    like the helper is broken.
    """
    import importlib
    import inspect as _inspect

    def parse(value):
        try:
            return json.loads(value)
        except Exception:
            return value

    module_name, _, attr_path = spec.partition(":")
    if not module_name or not attr_path:
        print(
            json.dumps(
                {
                    "event": "call_error",
                    "error": f"expected 'module:attr', got {spec!r}",
                }
            ),
            flush=True,
        )
        sys.exit(2)

    try:
        target = importlib.import_module(module_name)
        for part in attr_path.split("."):
            target = getattr(target, part)
    except Exception as exc:
        print(
            json.dumps(
                {"event": "call_error", "error": f"{type(exc).__name__}: {exc}"}
            ),
            flush=True,
        )
        sys.exit(2)

    call_args = [parse(value) for value in raw_args]

    if pass_browser == "yes":
        wants_browser = True
    elif pass_browser == "no":
        wants_browser = False
    else:
        try:
            parameters = list(_inspect.signature(target).parameters)
        except (TypeError, ValueError):
            parameters = []
        named = {"driver", "browser", "page", "context", "self"}
        wants_browser = bool(parameters) and parameters[0] in named

    if wants_browser:
        # The project's own handle when it has one, the raw Page otherwise --
        # the same object a step definition would receive, so a helper behaves
        # here exactly as it does in a real run.
        call_args.insert(0, browser_handle if browser_handle is not None else page)

    started = time.time()
    try:
        value = target(*call_args)
        record = {
            "event": "call_result",
            "target": spec,
            "passed_browser": wants_browser,
            "duration_s": round(time.time() - started, 2),
        }
        try:
            json.dumps(value)
            record["value"] = value
        except TypeError:
            # A page object or a Playwright handle is not serialisable; its
            # repr still answers most questions and is better than failing.
            record["value_repr"] = repr(value)
            record["value_type"] = type(value).__name__
        print(json.dumps(record), flush=True)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "call_error",
                    "target": spec,
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_s": round(time.time() - started, 2),
                }
            ),
            flush=True,
        )
        sys.exit(1)


def _setup_with_behave_hooks(feature_file, cdp_url, step_dir):
    """Load the project's own environment and fire its real hooks.

    The shim this replaces called *one* configured function. That is not what
    a real run does: a suite's `environment.py` defines before_all,
    before_feature, before_scenario and after_step, and the feature-level
    hooks are where tag-driven setup lives -- skip_login, plan tiers, and the
    rest. None of it fired in a debug session, so the session could not be
    compared with CI, which is the one comparison the tool exists to support.

    Using behave's own Runner instead means the hooks, the tag handling and
    the Context layers are behave's, not an approximation of them. The browser
    is handed over through PLAYWRIGHT_CDP_URL, which suites that support CDP
    attach already read -- so the project attaches to the debug browser rather
    than launching a second one, and aitlc stops re-implementing setup it does
    not own.

    Returns (context, record). A None context means the caller should fall
    back to the older path rather than fail: a session with approximate setup
    is worth more than no session.
    """
    try:
        from behave.configuration import Configuration
        from behave.model import Feature
        from behave.parser import parse_file
        from behave.runner import Context, Runner
    except Exception as exc:
        return None, {"event": "behave_hooks", "status": "failed",
                      "detail": f"behave API unavailable: {exc}"}

    if cdp_url:
        # The handover: the suite's own platform layer attaches here instead
        # of launching a browser aitlc would then have to keep in step.
        os.environ["PLAYWRIGHT_CDP_URL"] = cdp_url

    try:
        config = Configuration(command_args=[], load_config=True)
        config.paths = [str(feature_file)]
        runner = Runner(config)
        runner.setup_paths()
        runner.load_hooks()
        runner.load_step_definitions()
    except Exception as exc:
        return None, {"event": "behave_hooks", "status": "failed",
                      "detail": f"could not load the project environment: "
                                f"{type(exc).__name__}: {exc}"}

    # runner.hooks is the exec'd namespace of environment.py, so it also holds
    # imports and dunders. Only the hook names mean anything to a reader.
    hooks_found = sorted(
        name
        for name in (runner.hooks or {})
        if name.startswith(("before_", "after_")) and callable(runner.hooks[name])
    )
    try:
        feature = parse_file(str(feature_file))
    except Exception as exc:
        return None, {"event": "behave_hooks", "status": "failed",
                      "detail": f"could not parse the feature: {exc}"}

    scenario = next(iter(feature.scenarios), None) if feature else None

    context = Context(runner)
    runner.context = context
    try:
        context._push("testrun")
        runner.run_hook("before_all", context)
        context.feature = feature
        context._push("feature")
        runner.run_hook("before_feature", context, feature)
        if scenario is not None:
            context.scenario = scenario
            context._push("scenario")
            runner.run_hook("before_scenario", context, scenario)
    except Exception as exc:
        return None, {"event": "behave_hooks", "status": "failed",
                      "detail": f"a project hook raised: {type(exc).__name__}: {exc}",
                      "hooks_found": hooks_found}

    # A suite may legitimately not build a browser in its hooks: the feature
    # can be skipped by a tag rule, or the platform branch can be gated on
    # feature-level tags that an export left on the scenario instead. Report
    # that plainly -- the caller supplies its own handle rather than the
    # session dying with "Context has no attribute browser" several steps
    # later, which reads as a broken suite.
    feature_status = getattr(getattr(feature, "status", None), "name", "")
    return context, {
        "event": "behave_hooks",
        "status": "ok",
        "hooks_found": hooks_found,
        "feature_tags": sorted(feature.tags or []) if feature else [],
        "scenario_tags": sorted(scenario.tags or []) if scenario is not None else [],
        "scenario": getattr(scenario, "name", ""),
        "attached_over_cdp": bool(cdp_url),
        "feature_status": feature_status,
        "hooks_provided_browser": hasattr(context, "browser"),
    }


# Modules that must never be reloaded: they hold the live browser handle and
# the resolved configuration. Re-importing them hands the session a fresh
# object while the browser it was driving stays behind, which detaches the
# console from the very thing it exists to hold.
_NEVER_RELOAD = ("helper.", "config.configs", "features.behave_env", "features.environment")


def _reloadable(module, project_root):
    """A project module safe to re-import."""
    name = getattr(module, "__name__", "") or ""
    path = getattr(module, "__file__", None)
    if not path or not name:
        return False
    if any(name == skip or name.startswith(skip) for skip in _NEVER_RELOAD):
        return False
    try:
        return str(Path(path).resolve()).startswith(str(Path(project_root).resolve()))
    except OSError:
        return False


def _module_mtimes(project_root):
    """Current mtime of every reloadable project module now imported."""
    import sys as _sys

    stamps = {}
    for name, module in list(_sys.modules.items()):
        if not _reloadable(module, project_root):
            continue
        try:
            stamps[name] = os.path.getmtime(module.__file__)
        except OSError:
            continue
    return stamps


def _reload_changed(project_root, step_dir, tracked):
    """Re-import project modules whose files changed, in dependency order.

    The order is not cosmetic. Step definitions bind page objects with
    `from ... import` at import time, so reloading a page module alone updates
    that module and leaves the step module holding the object it imported
    before. Page objects and locators are reloaded first, then the step
    modules, so the bindings are rebuilt against the new code.

    Without this the console runs whatever it imported at startup: an edit has
    no effect, `retry` re-runs the old code, and it reports a pass or a failure
    for a version of the file that no longer exists on disk. That is worse
    than being slow, which is why it happens automatically rather than on
    request.
    """
    import importlib
    import sys as _sys

    current = _module_mtimes(project_root)
    changed = [
        name for name, stamp in current.items()
        if name in tracked and stamp != tracked[name]
    ]
    # A module imported since the last check is new, not changed -- record it
    # so a later edit to it is noticed.
    tracked.update(current)
    if not changed:
        return []

    step_prefix = str(Path(step_dir)).replace("/", ".").replace("\\", ".")
    supporting = sorted(n for n in changed if not n.startswith(step_prefix))
    step_modules = sorted(n for n in changed if n.startswith(step_prefix))

    # A page object changing is not enough on its own. Step definitions bind
    # those objects with `from ... import` at import time, so a step module
    # that did not change still holds the object it imported before. Every
    # step module is therefore re-executed whenever anything beneath it moved
    # -- caught by a test that edited only a page object and watched the step
    # keep returning the old value.
    if supporting:
        step_modules = sorted(
            {
                name
                for name in _sys.modules
                if name.startswith(step_prefix) and _sys.modules[name] is not None
            }
            | set(step_modules)
        )

    reloaded = []
    for name in supporting + step_modules:
        module = _sys.modules.get(name)
        if module is None:
            continue
        try:
            importlib.reload(module)
            reloaded.append(name)
        except Exception as exc:
            print(
                json.dumps({"event": "reload_failed", "module": name,
                            "error": f"{type(exc).__name__}: {exc}"}),
                flush=True,
            )

    # Rebuild the registry bindings: re-executing a step module re-runs its
    # @given/@when/@then decorators against the freshly imported page objects.
    if step_modules:
        try:
            from behave.step_registry import registry

            for attr in ("steps", "_steps"):
                table = getattr(registry, attr, None)
                if isinstance(table, dict):
                    for keyword in table:
                        table[keyword] = [
                            m for m in table[keyword]
                            if getattr(m.func, "__module__", "") not in step_modules
                        ]
            for name in step_modules:
                importlib.reload(_sys.modules[name])
        except Exception as exc:
            print(
                json.dumps({"event": "reload_failed", "module": "step registry",
                            "error": f"{type(exc).__name__}: {exc}"}),
                flush=True,
            )

    tracked.update(_module_mtimes(project_root))
    return reloaded


def _serve_forever(
    socket_path, *, context, run_steps, idle_timeout, project_root=None, step_dir=""
) -> None:
    """Answer step batches over a socket, keeping one live context.

    This is what makes the fast loop both fast and *correct*. Spawning a
    process per step re-imports the step registry and re-runs scenario setup
    every time -- slow, but the real damage is that run-scoped data
    (generated names, ids, emails) is regenerated per process. A step that
    waits for something an earlier step created then polls forever for a
    name that never existed, which looks exactly like the application
    hanging. One process means one set of that data.

    Deliberately a plain newline-delimited JSON protocol over a Unix socket:
    no dependency, no port to collide, and the socket file doubles as the
    liveness check.
    """
    import socket as _socket

    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    # What the process imported at startup. Anything edited after this point is
    # re-imported before the next step runs.
    tracked = _module_mtimes(project_root) if project_root else {}

    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    server.settimeout(idle_timeout)
    print(json.dumps({"event": "serving", "socket": str(path)}), flush=True)

    try:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                # Idle timeout: exit rather than sit forever holding a browser.
                print(json.dumps({"event": "idle_exit"}), flush=True)
                return
            with conn:
                payload = b""
                while not payload.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    payload += chunk
                if not payload.strip():
                    continue
                try:
                    request = json.loads(payload.decode())
                except Exception as exc:
                    conn.sendall(
                        (json.dumps({"error": f"bad request: {exc}"}) + "\n").encode()
                    )
                    continue

                if request.get("cmd") == "stop":
                    conn.sendall((json.dumps({"stopped": True}) + "\n").encode())
                    return
                if request.get("cmd") == "ping":
                    conn.sendall((json.dumps({"alive": True}) + "\n").encode())
                    continue
                if request.get("cmd") == "reload":
                    forced = (
                        _reload_changed(project_root, step_dir, tracked)
                        if project_root else []
                    )
                    conn.sendall(
                        (json.dumps({"reloaded": forced}) + "\n").encode()
                    )
                    continue

                steps = request.get("steps") or []
                reloaded = []
                if project_root:
                    # Before every batch, so an edit is picked up without the
                    # caller having to remember. `retry` means "run this again
                    # with my change", and it cannot mean that against code
                    # imported minutes ago.
                    reloaded = _reload_changed(project_root, step_dir, tracked)
                try:
                    results = run_steps(context, "\n".join(steps))
                    reply = {"results": results}
                    if reloaded:
                        reply["reloaded"] = reloaded
                except Exception as exc:  # a bad batch must not kill the server
                    reply = {
                        "results": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                conn.sendall((json.dumps(reply) + "\n").encode())
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _script_main() -> None:
    import argparse
    import importlib
    import os
    import pkgutil
    import time
    from contextlib import contextmanager

    sys.path.insert(0, os.getcwd())

    from behave.parser import parse_steps
    from behave.step_registry import registry
    from playwright.sync_api import sync_playwright

    def load_step_definitions(step_dir: str) -> int:
        loaded = 0
        package_name = str(Path(step_dir)).replace("/", ".").replace("\\", ".")
        for _finder, name, _ispkg in pkgutil.iter_modules([step_dir]):
            if name.startswith("_"):
                continue
            module_name = f"{package_name}.{name}"
            try:
                importlib.import_module(module_name)
                loaded += 1
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "step_module_skipped",
                            "module": module_name,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    ),
                    flush=True,
                )
        return loaded

    class MinimalContext:
        """Minimal stand-in for behave's Context.

        Stand-in for behave's Context — the same shape as the
        ReplContext, trimmed to what step dispatch itself needs.
        """

        def __init__(self, browser: Any, page: Any) -> None:
            self.browser = browser
            self.primary_browser = browser
            self.page = page
            self.primary_page = page
            self.table = None
            self.text = None
            self.created_users: list[str] = []
            self.scenario_variables: dict = {}
            viewport = page.viewport_size
            if viewport is None:
                # CDP-attached pages emulated via raw Emulation.setDeviceMetrics
                # Override have no Playwright-tracked viewport — falling back to
                # the 1280x800 desktop default here silently mis-sizes every
                # mobile-device CDP-attach console session. window.innerWidth/
                # innerHeight reflects the real rendered size regardless of
                # which mechanism applied it.
                viewport = page.evaluate(
                    "({width: window.innerWidth, height: window.innerHeight})"
                )
            self.screen_width = viewport.get("width", 1280)
            self.screen_height = viewport.get("height", 800)

        @contextmanager
        def use_with_user_mode(self) -> Any:
            # behave's Match.run() requires this context manager to exist —
            # real error hit live building this: AttributeError without it.
            # A real Behave Context supports switching between multiple
            # simulated users; this console runs as a single session, so
            # it's a no-op.
            yield self

        def execute_steps(self, text: str) -> None:
            run_steps(self, text)

    def _step_status(name: str):
        """behave's Status enum when available; hooks compare against it."""
        try:
            from behave.model_core import Status

            return getattr(Status, name)
        except Exception:
            return name

    def run_steps(context: Any, text: str) -> list[dict]:
        results = []
        try:
            steps = parse_steps(text)
        except Exception as exc:
            print(json.dumps({"event": "parse_error", "error": str(exc)}), flush=True)
            return results

        def _emit_progress() -> None:
            # Overwrite a small progress file after each step so `debug status`
            # can read where the setup is while `debug start` is still running,
            # rather than the whole run being a silent multi-minute wait (G48).
            progress_file = getattr(args, "progress_file", "")
            if not progress_file:
                return
            try:
                Path(progress_file).parent.mkdir(parents=True, exist_ok=True)
                Path(progress_file).write_text(
                    json.dumps(
                        {
                            "state": "running",
                            "done": len(results),
                            "total": len(steps),
                            "current_step": results[-1]["step"] if results else "",
                            "passed": sum(1 for r in results if r.get("status") == "passed"),
                            "failed": sum(1 for r in results if r.get("status") == "failed"),
                            "updated_at": time.time(),
                        }
                    )
                )
            except OSError:
                pass

        for step in steps:
            match = registry.find_match(step)
            record: dict = {"step": f"{step.keyword} {step.name}".strip()}
            if match is None:
                record.update({"status": "undefined", "duration_s": 0.0})
                print(json.dumps(record), flush=True)
                results.append(record)
                _emit_progress()
                continue

            context.table = step.table
            context.text = step.text

            # The suite's own per-step hooks. after_step is where a project
            # captures its evidence -- the failure screenshot, and the GraphQL
            # query and response that repeatedly settled whether a failure was
            # the app or the test. A debug session that skips it throws away
            # exactly the material that makes a failure explicable, and leaves
            # the person to reproduce it again to get the same information.
            runner = getattr(context, "_runner", None)
            step_hook = getattr(runner, "run_hook", None) if runner else None
            if step_hook:
                context.step = step
                try:
                    step_hook("before_step", context, step)
                except Exception as exc:  # a hook must not fail the step
                    print(
                        json.dumps({"event": "hook_error", "hook": "before_step",
                                    "error": f"{type(exc).__name__}: {exc}"}),
                        flush=True,
                    )

            started = time.time()
            record["started_at"] = _stamp(started)
            try:
                match.run(context)
                ended = time.time()
                step.status = _step_status("passed")
                record.update(
                    {
                        "status": "passed",
                        "duration_s": round(ended - started, 2),
                        "ended_at": _stamp(ended),
                    }
                )
            except Exception as exc:
                ended = time.time()
                step.status = _step_status("failed")
                record.update(
                    {
                        "status": "failed",
                        "duration_s": round(ended - started, 2),
                        "ended_at": _stamp(ended),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

            if step_hook:
                # after_step reads step.status to decide whether to capture, so
                # the status above is set before this runs, exactly as behave
                # does it.
                try:
                    step_hook("after_step", context, step)
                    record["hooks_ran"] = True
                except Exception as exc:
                    print(
                        json.dumps({"event": "hook_error", "hook": "after_step",
                                    "error": f"{type(exc).__name__}: {exc}"}),
                        flush=True,
                    )
            print(json.dumps(record), flush=True)
            results.append(record)
            _emit_progress()
        return results

    def parse_line_range(spec: str, max_line: int) -> tuple[int, int]:
        start_s, _, end_s = spec.partition("-")
        start = int(start_s) if start_s else 1
        end = int(end_s) if end_s else max_line
        return start, end

    parser = argparse.ArgumentParser()
    parser.add_argument("feature_file")
    parser.add_argument(
        "--range", default=None, help="START-END file line numbers, 1-based inclusive"
    )
    parser.add_argument("--example-row", type=int, default=0)
    parser.add_argument("--step-dir", default="features/steps")
    parser.add_argument(
        "--trace",
        default="",
        help=(
            "Write a Playwright trace here. Tracing is otherwise only enabled "
            "on the remote grid, so a local debug session produces no timeline "
            "-- and `aitlc trace` already knows how to read one."
        ),
    )
    parser.add_argument(
        "--capture-network",
        action="store_true",
        help=(
            "Record API responses seen during the slice. A slice runs without "
            "the project's before_scenario, so any collector the suite installs "
            "there is absent -- this is the substitute."
        ),
    )
    parser.add_argument("--cdp-url", default=os.environ.get("PLAYWRIGHT_CDP_URL", ""))
    parser.add_argument(
        "--mobile",
        nargs="?",
        const="Galaxy S8",
        default=None,
        metavar="DEVICE",
        help=(
            "Apply Playwright device emulation — bare --mobile defaults to "
            "'Galaxy S8', or pass any playwright.devices key e.g. "
            "--mobile 'iPhone 14'. Needed for TEST_TYPE=mobile_browser "
            "scenarios; without it every step runs at desktop viewport."
        ),
    )
    parser.add_argument(
        "--login-step",
        default=None,
        help='A Gherkin step to run first, e.g. "Given open the app" — dispatched '
        "through the same registry as everything else, not a hardcoded import.",
    )
    parser.add_argument(
        "--scenario-setup",
        default=os.environ.get("AITLC_SCENARIO_SETUP") or None,
        help=(
            "'module.path:function' for the project's own per-scenario setup "
            "(e.g. features.environment_helpers:populate_scenario_data). "
            "Without it, per-scenario generated data such as a unique email "
            "is absent and data-dependent steps fail in confusing, delayed "
            "ways. Pass 'none' to skip."
        ),
    )
    parser.add_argument(
        "--browser-actions",
        default=os.environ.get("AITLC_BROWSER_ACTIONS") or None,
        help=(
            "'module:Class' wrapping a Playwright Page, assigned to "
            "context.browser. Most suites' steps go through such a wrapper; "
            "without one the raw Page is used."
        ),
    )
    parser.add_argument(
        "--browser-factory",
        default=os.environ.get("AITLC_BROWSER_FACTORY") or None,
        help=(
            "'module:Class' exposing launch_local_mobile_browser_via_cdp"
            "(playwright, cdp_url, device_name). Needed only for --mobile "
            "with --cdp-url."
        ),
    )
    parser.add_argument(
        "--allow-missing-setup",
        action="store_true",
        help="Continue even if scenario setup fails (default: stop immediately).",
    )
    parser.add_argument(
        "--behave-hooks",
        dest="behave_hooks",
        action="store_true",
        default=True,
        help="Fire the project's own environment.py hooks (default).",
    )
    parser.add_argument(
        "--no-behave-hooks",
        dest="behave_hooks",
        action="store_false",
        help="Use the configured scenario-setup shim instead of real hooks.",
    )
    parser.add_argument(
        "--call",
        default="",
        help=(
            "Call 'module:attr' (dotted attrs allowed) with the live browser "
            "and print the result, instead of running steps."
        ),
    )
    parser.add_argument(
        "--call-arg",
        action="append",
        default=[],
        help="Positional argument for --call. Repeatable. Parsed as JSON, else str.",
    )
    parser.add_argument(
        "--call-pass-browser",
        default="auto",
        choices=("auto", "yes", "no"),
        help="Pass the project browser handle as the first argument.",
    )
    parser.add_argument(
        "--serve",
        default="",
        help=(
            "Listen on this socket path and run step batches on request, "
            "instead of running the feature once and exiting."
        ),
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=3600.0,
        help="Exit after this many seconds with no request, so a server cannot leak.",
    )
    parser.add_argument(
        "--progress-file",
        default="",
        help=(
            "Write {done,total,current_step,passed,failed} here after each step. "
            "Lets `aitlc debug status` report progress while `debug start` is "
            "still running its setup, instead of the run being a silent wait."
        ),
    )
    args = parser.parse_args()

    loaded = load_step_definitions(args.step_dir)
    print(json.dumps({"event": "loaded_step_modules", "count": loaded}), flush=True)

    playwright = sync_playwright().start()
    if args.cdp_url:
        if args.mobile:
            # browser.new_context(**playwright.devices[...]) does NOT reliably
            # apply device emulation on a connect_over_cdp-attached browser —
            # confirmed live: viewport_size stayed None and the desktop user
            # agent leaked through. Same underlying limitation as
            # set_viewport_size() on a CDP-attached page. The proven fix is
            # to drive Emulation.setDeviceMetricsOverride / setUserAgentOverride
            # / setTouchEmulationEnabled directly over a raw CDP session on the
            # existing context/page — see
            # helper.drivers.browser_factory.BrowserFactory.launch_local_mobile_browser_via_cdp,
            # which this reuses rather than re-implementing.
            factory = _load_object(args.browser_factory)
            if factory is None:
                raise SystemExit(
                    "--mobile with --cdp-url needs a project browser factory. "
                    "Set [project].browser_factory in aitlc.toml to "
                    "'module:Class' exposing launch_local_mobile_browser_via_cdp"
                    "(playwright, cdp_url, device_name), or drop --mobile."
                )
            browser, pw_context, page = factory.launch_local_mobile_browser_via_cdp(
                playwright, args.cdp_url, device_name=args.mobile
            )
        else:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            pw_context = (
                browser.contexts[0] if browser.contexts else browser.new_context()
            )
            page = pw_context.pages[0] if pw_context.pages else pw_context.new_page()
    else:
        browser = playwright.chromium.launch(channel="chromium", headless=False)
        if args.mobile:
            page = browser.new_context(
                **_mobile_context_options(playwright, args.mobile)
            ).new_page()
        else:
            page = browser.new_context(accept_downloads=True).new_page()
    if args.mobile:
        print(
            json.dumps({"event": "mobile_emulation", "device": args.mobile}),
            flush=True,
        )

    # Step definitions usually reach the browser through a project wrapper
    # (context.browser) rather than the raw Page. Which class that is differs
    # per project, so it is named in config; without one, the raw Page is
    # used, which is right for suites that drive Playwright directly.
    actions_cls = _load_object(args.browser_actions)
    browser_handle = actions_cls(page) if actions_cls is not None else page

    if args.trace:
        try:
            pw_context.tracing.start(screenshots=True, snapshots=True, sources=False)
        except Exception as exc:  # noqa: BLE001 - tracing must never fail a run
            print(json.dumps({"event": "trace_error", "error": str(exc)}), flush=True)

    captured: list[dict] = []
    if args.capture_network:

        def _on_response(response) -> None:  # pragma: no cover - event callback
            try:
                request = response.request
                if request.method != "POST":
                    return
                body = request.post_data or ""
                name = ""
                if body.startswith("{"):
                    try:
                        name = json.loads(body).get("operationName") or ""
                    except (json.JSONDecodeError, AttributeError):
                        name = ""
                captured.append(
                    {
                        "operation": name,
                        "status": response.status,
                        "url": response.url.split("?")[0],
                    }
                )
            except Exception:  # noqa: BLE001 - never raise from a listener
                return

        page.on("response", _on_response)

    context = None
    if args.behave_hooks:
        # Preferred: the project's own hooks, its own tag handling, behave's
        # own Context. Falls back below rather than failing, because an
        # approximate session beats no session.
        context, hook_record = _setup_with_behave_hooks(
            args.feature_file, args.cdp_url, args.step_dir
        )
        print(json.dumps(hook_record), flush=True)

    if context is None:
        context = MinimalContext(browser_handle, page)
        setup_record = apply_scenario_setup(
            context,
            feature_file=args.feature_file,
            spec=args.scenario_setup,
        )
    else:
        if not hasattr(context, "browser"):
            # Hooks ran but built no browser. Hand over the one already
            # attached here so steps can still run against the same page the
            # session is parked on.
            context.browser = browser_handle
            context.page = page
        setup_record = {
            "event": "scenario_setup",
            "status": "ok",
            "hook": "project environment.py (behave hooks)",
            "browser_from": (
                "project hooks" if hook_record.get("hooks_provided_browser")
                else "aitlc (hooks built none)"
            ),
        }
    print(json.dumps(setup_record), flush=True)
    if setup_record["status"] == "failed" and not args.allow_missing_setup:
        # Hard stop by default. Running on without scenario data is how a
        # missing setup turned into a 189s "login never succeeds" mystery
        # instead of an immediate, obvious error.
        print(
            json.dumps(
                {
                    "event": "done",
                    "total": 0,
                    "failed": 1,
                    "reason": "scenario setup failed; pass --allow-missing-setup to run anyway",
                }
            ),
            flush=True,
        )
        sys.exit(1)

    if args.login_step:
        run_steps(context, args.login_step)

    lines = Path(args.feature_file).read_text().splitlines()
    numbered = list(enumerate(lines, start=1))

    examples_at = next(
        (i for i, line in enumerate(lines) if line.strip().startswith("Examples")), None
    )
    substitutions: dict = {}
    if examples_at is not None:
        table_lines = [
            line for line in lines[examples_at + 1 :] if line.strip().startswith("|")
        ]
        if table_lines:
            header = [c.strip() for c in table_lines[0].strip().strip("|").split("|")]
            data_rows = table_lines[1:]
            if args.example_row < len(data_rows):
                values = [
                    c.strip()
                    for c in data_rows[args.example_row].strip().strip("|").split("|")
                ]
                if len(values) != len(header):
                    # A ragged Examples row would otherwise zip-truncate:
                    # some <placeholder> keeps its literal angle brackets and
                    # the step fails on a nonsense argument, several layers
                    # away from the malformed table that caused it.
                    print(
                        json.dumps(
                            {
                                "event": "examples_row_mismatch",
                                "header_columns": len(header),
                                "row_columns": len(values),
                                "row_index": args.example_row,
                                "detail": (
                                    "Examples row column count does not match the "
                                    "header; unmatched placeholders will remain "
                                    "literal in the steps below"
                                ),
                            }
                        ),
                        flush=True,
                    )
                substitutions = dict(zip(header, values, strict=False))
        numbered = numbered[:examples_at]

    def substitute(line: str) -> str:
        for key, value in substitutions.items():
            line = line.replace(f"<{key}>", value)
        return line

    filtered = [
        (ln, substitute(text))
        for ln, text in numbered
        if text.strip()
        and not text.strip().startswith(("#", "@", "Feature:", "Scenario"))
    ]

    if args.range:
        start, end = parse_line_range(args.range, len(lines))
        filtered = [(ln, text) for ln, text in filtered if start <= ln <= end]

    if args.call:
        _run_call(
            args.call,
            raw_args=args.call_arg,
            pass_browser=args.call_pass_browser,
            browser_handle=browser_handle,
            page=page,
            context=context,
        )
        sys.exit(0)

    if args.serve:
        _serve_forever(
            args.serve,
            context=context,
            run_steps=run_steps,
            idle_timeout=args.idle_timeout,
            project_root=os.getcwd(),
            step_dir=args.step_dir,
        )
        sys.exit(0)

    body = "\n".join(text for _, text in filtered)
    results = run_steps(context, body)

    if args.capture_network:
        print(
            json.dumps({"event": "network", "responses": captured[-40:]}),
            flush=True,
        )
    if args.trace:
        try:
            pw_context.tracing.stop(path=args.trace)
            print(json.dumps({"event": "trace_saved", "path": args.trace}), flush=True)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "trace_error", "error": str(exc)}), flush=True)

    failed = [r for r in results if r["status"] == "failed"]
    print(
        json.dumps({"event": "done", "total": len(results), "failed": len(failed)}),
        flush=True,
    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _script_main()
