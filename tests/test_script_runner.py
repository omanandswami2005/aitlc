"""Tests for the project-script wrapper plumbing.

The wrapped scripts create/delete real users in a shared DynamoDB table and
hit Xray, so nothing here executes them — subprocess.run is patched and the
assertions are about command assembly, exit-code propagation and redaction.
"""

from __future__ import annotations

import subprocess

from aitlc.core import script_runner


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCommandAssembly:
    def test_runs_script_under_project_poetry_env(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        script_runner.run_project_script(
            "scripts/x.py", cwd=tmp_path, poetry_cmd=["poetry"]
        )

        # The scripts import the target's own modules, which only resolve
        # under the target's environment — not aitlc's interpreter.
        assert captured["cmd"] == ["poetry", "run", "python3", "scripts/x.py"]
        assert captured["cwd"] == tmp_path

    def test_extra_args_are_appended_in_order(self, monkeypatch, tmp_path):
        captured = {}
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: (captured.setdefault("cmd", cmd), _FakeProc())[1],
        )
        script_runner.run_project_script(
            "scripts/x.py",
            cwd=tmp_path,
            poetry_cmd=["poetry"],
            args=["PROJ-1", "--status", "FAILED"],
        )
        assert captured["cmd"][-3:] == ["PROJ-1", "--status", "FAILED"]


class TestExitCode:
    def test_zero_is_passed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeProc(0))
        result = script_runner.run_project_script(
            "s.py", cwd=tmp_path, poetry_cmd=["poetry"]
        )
        assert result.passed is True
        assert result.to_dict()["exit_code"] == 0

    def test_nonzero_propagates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeProc(3))
        result = script_runner.run_project_script(
            "s.py", cwd=tmp_path, poetry_cmd=["poetry"]
        )
        assert result.passed is False
        assert result.to_dict()["exit_code"] == 3


class TestOutputHandling:
    def test_tail_limits_output(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeProc(
                0, stdout="\n".join(str(i) for i in range(200))
            ),
        )
        result = script_runner.run_project_script(
            "s.py", cwd=tmp_path, poetry_cmd=["poetry"], tail_lines=5
        )
        assert result.stdout_tail.splitlines() == ["195", "196", "197", "198", "199"]

    def test_secrets_are_redacted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: _FakeProc(0, stdout="token=SUPERSECRET done"),
        )
        result = script_runner.run_project_script(
            "s.py",
            cwd=tmp_path,
            poetry_cmd=["poetry"],
            secret_values=["SUPERSECRET"],
        )
        # These scripts log freely; an unredacted token would land in a
        # terminal scrollback or a pasted bug report.
        assert "SUPERSECRET" not in result.stdout_tail

    def test_empty_output_is_empty_string(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeProc(0))
        result = script_runner.run_project_script(
            "s.py", cwd=tmp_path, poetry_cmd=["poetry"]
        )
        assert result.stdout_tail == ""
        assert result.stderr_tail == ""
