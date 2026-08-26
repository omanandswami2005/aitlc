"""Live-overwritten current_step.json status file (FR-1.5).

STDLIB-ONLY, NO IMPORTS FROM THE REST OF AITLC. This is load-bearing, not a
style choice: `behave_runner.run()` shells out to `poetry run behave`
inside the TARGET PROJECT's own environment, which does not have aitlc
installed — only the target project's own dependencies. A behave formatter
must be importable from wherever `behave` itself runs, so this exact file
gets copied byte-for-byte into a temp directory that's added to the
subprocess's PYTHONPATH (see behave_runner.run) — it must survive being
dropped into a totally different Python environment with zero aitlc
present, hence zero imports beyond the standard library.

Purpose: checking "is it still running, what step" today means `tail -N`
on a log that grows to hundreds of lines over a run. This writes one small
JSON file, OVERWRITTEN (not appended) on every step, so a progress check
is a ~100-byte read regardless of how long the run has been going.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from behave.formatter.base import Formatter


class LiveStatusFormatter(Formatter):
    """Behave formatter that writes the current step to a file."""

    name = "aitlc-live-status"
    description = "Writes a live-overwritten current_step.json (aitlc FR-1.5)"

    def __init__(self, stream_opener: Any, config: Any) -> None:
        """Set up the formatter and resolve the status file path."""
        super().__init__(stream_opener, config)
        self._status_path = os.environ.get("AITLC_STATUS_FILE")
        self._steps_passed = 0
        self._steps_failed = 0
        self._scenario_total_steps = 0
        self._scenario_step_index = 0
        self._start = time.monotonic()

    def scenario(self, scenario: Any) -> None:
        """Record the step count for the scenario now starting.

        Without this, a status-file reader only ever sees a running
        passed/failed tally -- no way to tell "3 steps in" from "3 of 47",
        which is the difference between "on track" and "basically done".
        """
        self._scenario_total_steps = len(getattr(scenario, "steps", None) or [])
        self._scenario_step_index = 0

    def step(self, step: Any) -> None:
        """Record a step as it is about to run."""
        self._scenario_step_index += 1
        self._write(step, "running")

    def result(self, step: Any) -> None:
        """Record a step's outcome once it has run."""
        status = getattr(step.status, "name", str(step.status))
        if status == "passed":
            self._steps_passed += 1
        elif status == "failed":
            self._steps_failed += 1
        self._write(step, status)

    def _write(self, step: Any, status: str) -> None:
        if not self._status_path:
            return
        data = {
            "step": f"{step.keyword} {step.name}".strip(),
            "status": status,
            "elapsed_s": round(time.monotonic() - self._start, 1),
            "steps_passed": self._steps_passed,
            "steps_failed": self._steps_failed,
            # Current step's 1-based position in ITS scenario, and that
            # scenario's own step count -- same "index/total" shape `aitlc
            # debug status` already reports, for one consistent progress
            # indicator across every aitlc command that has a notion of steps.
            "step_index": self._scenario_step_index,
            "step_total": self._scenario_total_steps,
        }
        tmp_path = f"{self._status_path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, self._status_path)  # atomic — never a half-written read
