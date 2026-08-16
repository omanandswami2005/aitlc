"""Tests for attaching instrumentation without modifying the target project.

The property under test is the whole point of the runtime package: a
project that has not been edited still gets instrumented, and the choice
of mechanism degrades sensibly across behave versions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from aitlc.runtime import attach


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


_HELP_127 = "usage: behave [options]\n  --runner-class RUNNER_CLASS  Tells Behave...\n"
_HELP_13X = (
    "usage: behave [options]\n  -r RUNNER, --runner RUNNER   Use a runner alias...\n"
)
_HELP_126 = "usage: behave [options]\n  --no-capture  Do not capture stdout\n"


class TestDetectRunnerFlag:
    def test_prefers_runner_class_when_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_127))
        flag, reason = attach.detect_runner_flag(["behave"], tmp_path)
        assert flag == "--runner-class"
        assert "--runner-class" in reason

    def test_finds_runner_on_newer_behave(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_13X))
        flag, _ = attach.detect_runner_flag(["behave"], tmp_path)
        assert flag == "--runner"

    def test_none_when_option_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_126))
        flag, reason = attach.detect_runner_flag(["behave"], tmp_path)
        assert flag is None
        assert "no custom-runner option" in reason

    def test_unrunnable_behave_is_reported_distinctly(self, monkeypatch, tmp_path):
        """'behave has no option' and 'behave would not run' are different bugs.

        Both fall back to the same mechanism, so reporting the wrong one
        sends whoever debugs it to the wrong place entirely.
        """

        def boom(*a, **k):
            raise OSError("no such file")

        monkeypatch.setattr(subprocess, "run", boom)
        flag, reason = attach.detect_runner_flag(["behave"], tmp_path)
        assert flag is None
        assert "could not run behave" in reason
        assert "no custom-runner option" not in reason

    def test_nonzero_exit_is_not_treated_as_help(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _Proc("boom", returncode=2)
        )
        flag, reason = attach.detect_runner_flag(["behave"], tmp_path)
        assert flag is None
        assert "could not probe" in reason


class TestPlan:
    def test_no_instrumentation_requested_is_a_noop(self, tmp_path):
        plan = attach.plan(
            ["behave"], tmp_path, tmp_path / "w", aitlc_src=tmp_path / "src"
        )
        assert plan.mechanism == "none"
        assert plan.extra_args == []
        assert plan.env == {}

    def test_supported_option_is_preferred(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_127))
        plan = attach.plan(
            ["behave"],
            tmp_path,
            tmp_path / "w",
            aitlc_src=tmp_path / "src",
            pause_on_failure=True,
        )
        assert plan.mechanism == "runner-class"
        # 1.2.7 parses with rsplit(".", 1): it needs the DOTTED form.
        assert plan.extra_args == ["--runner-class", "aitlc.runtime.runner.AitlcRunner"]
        assert plan.env["AITLC_PAUSE_ON_FAILURE"] == "1"
        # The target's interpreter must be able to import aitlc.
        assert plan.env["PYTHONPATH"] == str(tmp_path / "src")

    def test_falls_back_to_sitecustomize(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_126))
        work = tmp_path / "w"
        plan = attach.plan(
            ["behave"],
            tmp_path,
            work,
            aitlc_src=tmp_path / "src",
            pause_on_failure=True,
        )
        assert plan.mechanism == "sitecustomize"
        assert plan.extra_args == []
        assert plan.env["AITLC_INSTRUMENT"] == "1"
        generated = work / "sitecustomize.py"
        assert generated.exists()
        # Must no-op unless explicitly switched on: this file lands on the
        # PYTHONPATH of every child process, including ones with no behave.
        assert 'AITLC_INSTRUMENT") != "1"' in generated.read_text()

    def test_generated_sitecustomize_is_import_safe(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_126))
        work = tmp_path / "w"
        attach.plan(
            ["behave"],
            tmp_path,
            work,
            aitlc_src=tmp_path / "src",
            pause_on_failure=True,
        )
        source = (work / "sitecustomize.py").read_text()
        compile(source, "sitecustomize.py", "exec")  # must be valid Python
        # A missing behave must not crash unrelated interpreters.
        assert "except Exception:" in source

    def test_events_path_is_passed_through(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(_HELP_127))
        events = tmp_path / "events.jsonl"
        plan = attach.plan(
            ["behave"],
            tmp_path,
            tmp_path / "w",
            aitlc_src=tmp_path / "src",
            events_path=events,
        )
        assert plan.env["AITLC_EVENTS"] == str(events)
        assert plan.mechanism == "runner-class"


class TestRunnerPathFormats:
    """Each flag needs the class spelled the way ITS parser expects.

    Caught end-to-end, not in review: behave 1.2.7's --runner-class splits
    on the last dot, so the scoped "module:Class" form fails with
    "No module named ..." — a message that reads like aitlc is missing
    rather than like the path is formatted for a different behave.
    """

    def test_runner_class_uses_dotted_form(self):
        assert attach.RUNNER_PATH_BY_FLAG["--runner-class"].endswith(".AitlcRunner")
        assert ":" not in attach.RUNNER_PATH_BY_FLAG["--runner-class"]

    def test_runner_uses_scoped_form(self):
        assert attach.RUNNER_PATH_BY_FLAG["--runner"].endswith(":AitlcRunner")

    def test_both_resolve_to_the_same_real_class(self):
        for path in attach.RUNNER_PATH_BY_FLAG.values():
            module_path, _, class_name = path.replace(":", ".").rpartition(".")
            module = __import__(module_path, fromlist=[class_name])
            assert hasattr(module, class_name), path

    def test_every_detectable_flag_has_a_format(self):
        # A newly detected flag with no entry would KeyError at plan() time.
        assert set(attach.RUNNER_PATH_BY_FLAG) == {"--runner-class", "--runner"}


class TestExtraArgsPlacement:
    """Instrumentation flags must land where behave accepts options.

    behave's positional path argument ends option parsing, so appending
    `--runner-class` after the feature path is silently treated as another
    path and the instrumentation never loads.
    """

    def test_extra_args_precede_the_feature_path(self):
        from aitlc.core.behave_runner import build_command

        cmd = build_command(
            Path("f.feature"),
            Path("r.json"),
            extra_args=["--runner-class", "x.Y"],
        )
        assert cmd[-1] == "f.feature"
        assert cmd[-3:-1] == ["--runner-class", "x.Y"]

    def test_no_extra_args_leaves_command_unchanged(self):
        from aitlc.core.behave_runner import build_command

        assert build_command(Path("f.feature"), Path("r.json"))[-1] == "f.feature"

    def test_extra_args_compose_with_line_spec(self):
        from aitlc.core.behave_runner import build_command

        cmd = build_command(
            Path("f.feature"), Path("r.json"), line=42, extra_args=["--runner", "a:B"]
        )
        assert cmd[-1] == "f.feature:42"
        assert "--runner" in cmd


class TestPauseMessageReachesTheUser:
    """The halt message must survive `os._exit`.

    Found live, not in review: behave captures stdout, and `os._exit` skips
    the flush that would release that buffer — so a `print()` here is
    written and then discarded. The pause fired, the browser stayed open,
    and nothing on screen said so. The message must go somewhere that
    survives: logging and stderr, not stdout.
    """

    def _source(self) -> str:
        from aitlc.runtime import runner

        return Path(runner.__file__).read_text()

    def _halt_code(self) -> str:
        """Return the halt method's real code, with comments stripped.

        The explanation of this bug necessarily contains the word it warns
        against, so a naive substring search matches the comment and passes
        for the wrong reason.
        """
        halt = self._source().split("_halt_for_inspection")[-1]
        return "\n".join(
            line for line in halt.splitlines() if not line.strip().startswith("#")
        )

    def test_does_not_rely_on_print(self):
        assert (
            "print(" not in self._halt_code()
        ), "stdout is captured by behave and lost on os._exit"

    def test_uses_logging_and_stderr(self):
        halt = self._halt_code()
        assert "logging.warning" in halt
        assert "sys.stderr" in halt

    def test_still_exits_hard(self):
        # A normal exit would unwind into after_scenario, where suites close
        # the browser — destroying the thing worth inspecting.
        assert "os._exit" in self._halt_code()
