import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aitlc.core.step_console import _mobile_context_options, run_console


def test_mobile_context_options_returns_device_dict():
    fake_playwright = MagicMock()
    fake_playwright.devices = {
        "Galaxy S8": {
            "viewport": {"width": 360, "height": 740},
            "user_agent": "fake-ua",
            "default_browser_type": "chromium",
        }
    }
    options = _mobile_context_options(fake_playwright, "Galaxy S8")
    assert options["viewport"] == {"width": 360, "height": 740}
    assert options["accept_downloads"] is True
    assert "default_browser_type" not in options


def test_mobile_context_options_raises_on_unknown_device():
    fake_playwright = MagicMock()
    fake_playwright.devices = {"Galaxy S8": {}}
    with pytest.raises(SystemExit, match="Unknown Playwright device"):
        _mobile_context_options(fake_playwright, "Definitely Not A Real Device")


def _fake_proc(stdout_lines: list[str], returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.stdout = "\n".join(stdout_lines)
    proc.returncode = returncode
    return proc


def test_parses_json_lines_into_step_results(tmp_path: Path):
    lines = [
        json.dumps({"event": "loaded_step_modules", "count": 5}),
        json.dumps({"step": "When do a thing", "status": "passed", "duration_s": 1.2}),
        json.dumps(
            {
                "step": "Then check it",
                "status": "failed",
                "duration_s": 0.5,
                "error": "boom",
            }
        ),
        json.dumps({"event": "done", "total": 2, "failed": 1}),
    ]
    with patch(
        "aitlc.core.step_console.subprocess.run", return_value=_fake_proc(lines, 1)
    ):
        result = run_console(
            tmp_path / "f.feature", cwd=tmp_path, poetry_cmd=["poetry"]
        )

    assert result.loaded_step_modules == 5
    assert len(result.results) == 2
    assert result.results[0].status == "passed"
    assert result.results[1].status == "failed"
    assert result.results[1].error == "boom"
    assert not result.passed
    assert len(result.failed) == 1


def test_all_passed_is_passed_true(tmp_path: Path):
    lines = [
        json.dumps({"event": "loaded_step_modules", "count": 3}),
        json.dumps({"step": "a", "status": "passed", "duration_s": 0.1}),
    ]
    with patch(
        "aitlc.core.step_console.subprocess.run", return_value=_fake_proc(lines, 0)
    ):
        result = run_console(
            tmp_path / "f.feature", cwd=tmp_path, poetry_cmd=["poetry"]
        )
    assert result.passed
    assert result.failed == []


def test_malformed_json_lines_are_skipped_not_crashed(tmp_path: Path):
    lines = [
        "not json at all",
        json.dumps({"step": "a", "status": "passed", "duration_s": 0.1}),
        "",
    ]
    with patch(
        "aitlc.core.step_console.subprocess.run", return_value=_fake_proc(lines, 0)
    ):
        result = run_console(
            tmp_path / "f.feature", cwd=tmp_path, poetry_cmd=["poetry"]
        )
    assert len(result.results) == 1


def test_builds_correct_command_with_all_options(tmp_path: Path):
    with patch(
        "aitlc.core.step_console.subprocess.run",
        return_value=_fake_proc([], 0),
    ) as mock_run:
        run_console(
            tmp_path / "f.feature",
            cwd=tmp_path,
            poetry_cmd=["poetry"],
            line_range="10-20",
            example_row=1,
            step_dir="features/steps",
            cdp_url="http://127.0.0.1:9222",
            login_step="Given open the app",
        )
    called_cmd = mock_run.call_args.args[0]
    assert "--range" in called_cmd and "10-20" in called_cmd
    assert "--example-row" in called_cmd and "1" in called_cmd
    assert "--cdp-url" in called_cmd and "http://127.0.0.1:9222" in called_cmd
    assert "--login-step" in called_cmd and "Given open the app" in called_cmd


def test_mobile_flag_passed_through_to_subprocess(tmp_path: Path):
    # Real gap found live: a mobile_browser scenario debugged through this
    # console silently ran at desktop viewport with no error, both fresh-
    # launched and CDP-attached. --mobile must reach the subprocess.
    with patch(
        "aitlc.core.step_console.subprocess.run",
        return_value=_fake_proc([], 0),
    ) as mock_run:
        run_console(
            tmp_path / "f.feature",
            cwd=tmp_path,
            poetry_cmd=["poetry"],
            mobile="Galaxy S8",
        )
    called_cmd = mock_run.call_args.args[0]
    assert "--mobile" in called_cmd and "Galaxy S8" in called_cmd


def test_no_mobile_flag_when_not_requested(tmp_path: Path):
    with patch(
        "aitlc.core.step_console.subprocess.run",
        return_value=_fake_proc([], 0),
    ) as mock_run:
        run_console(tmp_path / "f.feature", cwd=tmp_path, poetry_cmd=["poetry"])
    called_cmd = mock_run.call_args.args[0]
    assert "--mobile" not in called_cmd
