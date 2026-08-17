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


@dataclass
class StepResult:
    """One dispatched step and how it ended."""

    step: str
    status: str
    duration_s: float
    error: str | None = None


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

    def run_steps(context: Any, text: str) -> list[dict]:
        results = []
        try:
            steps = parse_steps(text)
        except Exception as exc:
            print(json.dumps({"event": "parse_error", "error": str(exc)}), flush=True)
            return results

        for step in steps:
            match = registry.find_match(step)
            record: dict = {"step": f"{step.keyword} {step.name}".strip()}
            if match is None:
                record.update({"status": "undefined", "duration_s": 0.0})
                print(json.dumps(record), flush=True)
                results.append(record)
                continue

            context.table = step.table
            context.text = step.text
            started = time.time()
            try:
                match.run(context)
                record.update(
                    {"status": "passed", "duration_s": round(time.time() - started, 2)}
                )
            except Exception as exc:
                record.update(
                    {
                        "status": "failed",
                        "duration_s": round(time.time() - started, 2),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            print(json.dumps(record), flush=True)
            results.append(record)
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

    context = MinimalContext(browser_handle, page)

    setup_record = apply_scenario_setup(
        context,
        feature_file=args.feature_file,
        spec=args.scenario_setup,
    )
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
