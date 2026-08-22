"""JSON-wrapping Behave runner (FR-1).

behave's native json.pretty formatter needs no custom parser — only
correct invocation and a compact summary read back from its output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aitlc.core.feature_select import attach_line_spec

_LIVE_STATUS_MODULE_NAME = "_aitlc_live_status"


_POETRY_FALLBACK_PATHS = [
    Path.home() / "Library/Python/3.10/bin/poetry",
    Path.home() / ".local/bin/poetry",
    Path("/usr/local/bin/poetry"),
]


def resolve_poetry() -> list[str]:
    """Return the command tokens to invoke poetry, in install-location order."""
    if shutil.which("poetry"):
        return ["poetry"]
    for fallback in _POETRY_FALLBACK_PATHS:
        if fallback.exists():
            return [str(fallback)]
    return [sys.executable, "-m", "poetry"]


@dataclass
class StepFailure:
    """A single failed step and the error it produced."""

    scenario: str
    step: str
    error: str


@dataclass
class ScenarioResult:
    """One scenario's outcome + real elapsed time (FR-10 — Teams notify).

    duration_seconds is the sum of its steps' own `result.duration` (behave's
    standard JSON field, seconds, present once a step actually executes) —
    i.e. how long the scenario ran before finishing or failing, not a
    cross-run streak. Steps that never ran (skipped after an earlier
    failure) contribute 0, which is correct: they added no elapsed time.
    """

    feature: str
    name: str
    status: str
    duration_seconds: float


@dataclass
class RunResult:
    """Structured result of one Behave run — the FR-1.2 output shape."""

    steps_by_status: dict[str, int] = field(default_factory=dict)
    failures: list[StepFailure] = field(default_factory=list)
    scenarios: list[ScenarioResult] = field(default_factory=list)
    exit_code: int = 0
    raw_report_path: Path | None = None
    # Set only when behave died before writing report_path at all (an
    # import-time SyntaxError/ImportError in a hooks/steps module, e.g.) --
    # see run()'s docstring note. Empty string means no such crash happened.
    crash_traceback: str = ""

    @property
    def passed(self) -> bool:
        """True when the run completed with no failures."""
        return self.exit_code == 0 and not self.failures

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable form of this result."""
        result: dict[str, Any] = {
            "steps_by_status": self.steps_by_status,
            "failures": [
                {"scenario": f.scenario, "step": f.step, "error": f.error}
                for f in self.failures
            ],
        }
        if self.crash_traceback:
            result["crash_traceback"] = self.crash_traceback
        return result


def _extract_error_message(step: dict[str, Any]) -> str:
    """Pull the real exception line out of a step's captured traceback.

    Two real bugs found and fixed building this the first time (in
    behave's JSON output): error_message can be a list of lines, not a str; and
    taking the first line of a joined traceback gives the useless
    "Traceback (most recent call last):" line instead of the actual
    exception. Both are handled here.
    """
    result = step.get("result", {})
    error = result.get("error_message", "")
    if isinstance(error, list):
        error = "\n".join(error)
    if not error:
        return ""

    # "Captured stderr" belongs here as much as the other two: behave appends
    # every captured stream after the traceback, and this function returns the
    # LAST non-blank line. A run that writes to stderr after the assertion --
    # an HTTPS InsecureRequestWarning, say -- otherwise reports
    # "warnings.warn(" as the failure. That string is also what
    # classify-failure matches on, so no pattern library can compensate.
    for marker in (
        "\nCaptured stdout:",
        "\nCaptured logging:",
        "\nCaptured stderr:",
    ):
        idx = error.find(marker)
        if idx != -1:
            error = error[:idx]

    lines = [line for line in error.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def parse_report(report_path: Path) -> RunResult:
    """Parse behave's json.pretty report into a RunResult."""
    result = RunResult(raw_report_path=report_path)
    if not report_path.exists():
        return result

    try:
        text = report_path.read_text().strip()
        if not text:
            return result
        features = json.loads(text)
    except (json.JSONDecodeError, OSError):
        # A paused run (os._exit before behave writes its report) leaves an
        # empty or truncated file. That is not an error — the pause itself is
        # the outcome, and run.py reports it from stderr.
        return result

    for feature in features:
        feature_name = feature.get("name", "")
        for element in feature.get("elements", []):
            if element.get("type") not in (None, "scenario"):
                continue  # skip Background elements — not a real scenario row
            scenario_name = element.get("name", "")
            scenario_duration = 0.0
            for step in element.get("steps", []):
                step_result = step.get("result", {})
                status = step_result.get("status", "untested")
                scenario_duration += step_result.get("duration", 0) or 0
                result.steps_by_status[status] = (
                    result.steps_by_status.get(status, 0) + 1
                )
                if status == "failed":
                    result.failures.append(
                        StepFailure(
                            scenario=scenario_name,
                            step=f"{step.get('keyword', '').strip()} {step.get('name', '')}".strip(),
                            error=_extract_error_message(step),
                        )
                    )
            result.scenarios.append(
                ScenarioResult(
                    feature=feature_name,
                    name=scenario_name,
                    status=element.get("status", "untested"),
                    duration_seconds=round(scenario_duration, 2),
                )
            )
    return result


def build_command(
    feature_path: Path,
    report_path: Path | None,
    *,
    tags: str | None = None,
    name_pattern: str | None = None,
    dry_run: bool = False,
    no_capture: bool = False,
    live_status: bool = False,
    stop: bool = False,
    line: int | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build the behave invocation.

    Shared by run() and anything else that needs the exact same command with a different
    execution wrapper (e.g. core/terminal_replay.py's `script`-based color capture for
    `aitlc report`) — kept as one function so the two never drift apart. `line` renders
    behave's native `FILE:LINE` form to run just the scenario at that line. It is passed
    as the positional path argument (not a flag) because that is behave's own documented
    syntax: `behave --help` -> `[DIR|FILE|FILE:LINE]`.

    `report_path=None` skips the json.pretty formatter entirely -- for a
    gated session (`gate_launch.launch`) that has no use for a report file,
    since its result comes from the live socket protocol instead. `stop`
    adds behave's own `--stop`, wanted by every gated invocation (a fixed
    park index or gate-on-failure) so one failed setup step halts the whole
    run rather than behave trying the next scenario.
    """
    cmd = resolve_poetry() + ["run", "behave", "--color=always"]
    if dry_run:
        cmd.append("--dry-run")
    if tags:
        cmd += ["--tags", tags]
    if name_pattern:
        cmd += ["--name", name_pattern]
    if no_capture:
        cmd.append("--no-capture")
    if stop:
        cmd.append("--stop")
    if report_path is not None:
        cmd += ["-f", "pretty", "-o", "-", "-f", "json.pretty", "-o", str(report_path)]
    if live_status:
        cmd += [
            "-f",
            f"{_LIVE_STATUS_MODULE_NAME}:LiveStatusFormatter",
            "-o",
            os.devnull,
        ]
    # Injected before the positional path, where behave expects options.
    cmd += list(extra_args or [])
    cmd.append(attach_line_spec(feature_path, line))
    return cmd


def _materialize_live_status_module(tmp_dir: Path) -> None:
    """Materialize live_status.py where the target project can import it.

    Copy live_status.py byte-for-byte into tmp_dir so it's importable
    from the TARGET PROJECT's own environment (which doesn't have aitlc
    installed) via PYTHONPATH — see live_status.py's own docstring for why
    this has to be a stdlib-only file, not a normal aitlc.core import.
    """
    source = Path(__file__).resolve().parent / "live_status.py"
    (tmp_dir / f"{_LIVE_STATUS_MODULE_NAME}.py").write_text(source.read_text())


def run(
    feature_path: Path,
    *,
    cwd: Path,
    tags: str | None = None,
    name_pattern: str | None = None,
    dry_run: bool = False,
    no_capture: bool = False,
    env: dict[str, str] | None = None,
    status_file: Path | None = None,
    line: int | None = None,
    extra_args: list[str] | None = None,
    log_file: Path | None = None,
) -> RunResult:
    """Run one feature file through Behave, capturing a JSON report.

    Builds the behave command and invokes it as a subprocess,
    generalized to not assume any one project's paths.

    status_file: if given, a live-overwritten JSON status file is written
    on every step (FR-1.5) — see live_status.py for why this needs a
    stdlib-only formatter module materialized onto PYTHONPATH rather than
    a normal `from aitlc.core import ...`.
    """
    report_dir = Path(tempfile.mkdtemp(prefix="aitlc_report_"))
    report_path = report_dir / f"{feature_path.stem}.report.json"

    proc_env = {**os.environ, **(env or {})}

    cmd = build_command(
        feature_path,
        report_path,
        tags=tags,
        name_pattern=name_pattern,
        dry_run=dry_run,
        no_capture=no_capture,
        live_status=status_file is not None,
        line=line,
        extra_args=extra_args,
    )

    if status_file is not None:
        formatter_dir = Path(tempfile.mkdtemp(prefix="aitlc_live_status_"))
        _materialize_live_status_module(formatter_dir)
        existing_pythonpath = proc_env.get("PYTHONPATH", "")
        proc_env["PYTHONPATH"] = (
            f"{formatter_dir}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(formatter_dir)
        )
        proc_env["AITLC_STATUS_FILE"] = str(status_file)
        status_file.parent.mkdir(parents=True, exist_ok=True)

    if log_file is not None:
        # Keep this run's complete console output. With several features in
        # flight the summary's stderr tail is not enough: the one run whose
        # failure looks unlike the others is exactly the one you need to read
        # in full, and afterwards it is gone.
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("wb") as handle:
            proc = subprocess.run(
                cmd, cwd=cwd, env=proc_env, stdout=handle, stderr=subprocess.STDOUT
            )
        crash_source = log_file
    else:
        # stderr always goes to a file even without an explicit log_file:
        # behave can die before writing report_path at all -- an import-time
        # SyntaxError/ImportError in a hooks/steps module, hit live on a real
        # suite (a stray character pasted into a hooks file broke every run).
        # parse_report then returns an all-empty RunResult, indistinguishable
        # from "nothing ran" -- there is no failing step to react to, so the
        # only visible move is to re-run, which reproduces the identical
        # empty crash forever. stdout is left inherited so `--no-capture`-style
        # live progress still streams to the terminal as before.
        crash_source = report_dir / "stderr.log"
        with crash_source.open("wb") as stderr_handle:
            proc = subprocess.run(cmd, cwd=cwd, env=proc_env, stderr=stderr_handle)

    result = parse_report(report_path)
    result.exit_code = proc.returncode

    if result.exit_code != 0 and not result.steps_by_status and not result.failures:
        try:
            captured = crash_source.read_text(errors="replace")
        except OSError:
            captured = ""
        result.crash_traceback = detect_crash_traceback(captured)

    return result


def detect_crash_traceback(captured_output: str) -> str:
    """Pull a real Python crash out of captured stdout/stderr, or "".

    Gated on an actual traceback marker, not just "the text is non-empty":
    a legitimate `--debug` pause ALSO writes plenty of output before it takes
    over (the suite's own INFO/WARNING logging, then aitlc's own "paused on
    failure"/"gate parked" marker) -- none of that is a crash. Caught live:
    the first version of this flagged a real pause as `crashed: true` because
    its stderr happened to be non-empty, which is always true. A genuine
    collection-time crash is the one case that prints Python's own
    "Traceback (most recent call last):" -- nothing else does. Shared by the
    plain-run crash path above and `run --debug`'s gated path (`gate_launch`
    has no subprocess.run() of its own to attach this to, so it calls this
    directly against its own log file).
    """
    marker = "Traceback (most recent call last):"
    idx = captured_output.rfind(marker)
    if idx == -1:
        return ""
    tail = captured_output[idx:].strip()
    return "\n".join(tail.splitlines()[-40:])
