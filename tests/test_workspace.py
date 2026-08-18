"""One directory for every artifact a command produces."""

from __future__ import annotations

import pytest
from aitlc.core import workspace


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Module state is process-wide; leaking it between tests hides real bugs."""
    monkeypatch.delenv("AITLC_WORKSPACE", raising=False)
    workspace.set_workspace(None)
    workspace.set_config_default(None)
    yield
    workspace.set_workspace(None)
    workspace.set_config_default(None)


class TestPrecedence:
    def test_the_default_is_unchanged_for_anyone_who_does_not_ask(self):
        """Nobody's paths move because this feature exists."""
        assert workspace.current_workspace() == "reports"
        assert workspace.output_path("/p", ".aitlc", "runs").as_posix().endswith(
            "/p/reports/.aitlc/runs"
        )

    def test_the_config_file_is_used_when_nothing_else_is_set(self):
        workspace.set_config_default("PROJ-1")
        assert workspace.current_workspace() == "PROJ-1"

    def test_the_environment_beats_the_config_file(self, monkeypatch):
        workspace.set_config_default("from-file")
        monkeypatch.setenv("AITLC_WORKSPACE", "from-env")
        assert workspace.current_workspace() == "from-env"

    def test_the_flag_beats_everything(self, monkeypatch):
        workspace.set_config_default("from-file")
        monkeypatch.setenv("AITLC_WORKSPACE", "from-env")
        workspace.set_workspace("from-flag")
        assert workspace.current_workspace() == "from-flag"

    def test_clearing_the_flag_restores_normal_resolution(self, monkeypatch):
        monkeypatch.setenv("AITLC_WORKSPACE", "from-env")
        workspace.set_workspace("from-flag")
        workspace.set_workspace(None)
        assert workspace.current_workspace() == "from-env"


class TestSafety:
    """A workspace is a directory inside the project, and must stay one."""

    @pytest.mark.parametrize("bad", ["..", "../elsewhere", "a/../../b", "/etc"])
    def test_escaping_the_project_is_refused(self, bad):
        with pytest.raises(workspace.WorkspaceError):
            workspace.set_workspace(bad)

    def test_an_empty_name_is_refused_rather_than_meaning_the_root(self):
        with pytest.raises(workspace.WorkspaceError):
            workspace.set_workspace("   ")

    def test_a_trailing_slash_is_tolerated(self):
        workspace.set_workspace("PROJ-1/")
        assert workspace.current_workspace() == "PROJ-1"

    def test_an_absolute_path_is_refused_not_quietly_made_relative(self):
        """Stripping the leading slash would write to <project>/etc instead."""
        with pytest.raises(workspace.WorkspaceError) as caught:
            workspace.set_workspace("/etc")
        assert "absolute" in str(caught.value)

    def test_a_nested_relative_path_is_allowed(self):
        workspace.set_workspace("investigations/PROJ-1")
        assert workspace.current_workspace() == "investigations/PROJ-1"


class TestPaths:
    def test_everything_lands_under_the_one_directory(self):
        workspace.set_workspace("PROJ-9")
        for parts in ((".aitlc", "runs"), (".cdp", "chrome-9222.json"), ("traces",)):
            assert "/PROJ-9/" in workspace.output_path("/p", *parts).as_posix() + "/"

    def test_ensure_creates_the_parent_but_not_the_file(self, tmp_path):
        workspace.set_workspace("PROJ-9")
        path = workspace.ensure(tmp_path, ".aitlc", "deep", "thing.json")
        assert path.parent.is_dir()
        assert not path.exists()


def test_core_helpers_follow_the_workspace(tmp_path):
    """The point of doing this at core level: no command has to opt in."""
    from aitlc.core import artifact_cache, focus, history, journal

    workspace.set_workspace("PROJ-42")
    for produced in (
        journal.run_dir(tmp_path) if hasattr(journal, "run_dir") else None,
        artifact_cache.cache_dir(tmp_path) if hasattr(artifact_cache, "cache_dir") else None,
        focus.focus_path(tmp_path) if hasattr(focus, "focus_path") else None,
        history.history_path(tmp_path) if hasattr(history, "history_path") else None,
    ):
        if produced is not None:
            assert "PROJ-42" in str(produced), produced
