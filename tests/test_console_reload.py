"""An edit must reach the next step, or `retry` is a lie.

The persistent console imports the suite's modules once. Without re-importing,
`retry` re-runs the code that was loaded at startup and reports a pass or a
failure for a version of the file that no longer exists on disk -- which is
worse than being slow, since it looks like a real result.
"""

from __future__ import annotations

import sys
import time

import pytest
from aitlc.core import step_console


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A tiny importable project: a page object and a step module using it."""
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "__init__.py").write_text("")
    (tmp_path / "pages" / "widget.py").write_text("VALUE = 'before'\n")

    steps = tmp_path / "features" / "steps"
    steps.mkdir(parents=True)
    (tmp_path / "features" / "__init__.py").write_text("")
    (steps / "__init__.py").write_text("")
    # Binds the page object at import time -- the case that makes reload order
    # matter, because reloading the page alone leaves this holding the old one.
    (steps / "step_widget.py").write_text(
        "from pages.widget import VALUE\n\ndef current():\n    return VALUE\n"
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("pages", "pages.widget", "features", "features.steps",
                 "features.steps.step_widget"):
        sys.modules.pop(name, None)
    import features.steps.step_widget as step_module  # noqa: F401

    yield tmp_path

    for name in list(sys.modules):
        if name.startswith(("pages", "features")):
            sys.modules.pop(name, None)


def _edit(path, text):
    # Coarse mtime resolution on some filesystems would hide a fast edit.
    path.write_text(text)
    stamp = time.time() + 2
    import os

    os.utime(path, (stamp, stamp))


class TestReload:
    def test_an_edit_to_a_page_object_reaches_the_step(self, project):
        import features.steps.step_widget as step_module

        assert step_module.current() == "before"
        tracked = step_console._module_mtimes(project)

        _edit(project / "pages" / "widget.py", "VALUE = 'after'\n")
        reloaded = step_console._reload_changed(project, "features/steps", tracked)

        assert "pages.widget" in reloaded
        assert sys.modules["features.steps.step_widget"].current() == "after", (
            "the step module still holds the object it imported at startup"
        )

    def test_an_edit_to_the_step_module_itself_is_picked_up(self, project):
        tracked = step_console._module_mtimes(project)
        _edit(
            project / "features" / "steps" / "step_widget.py",
            "from pages.widget import VALUE\n\ndef current():\n    return 'edited'\n",
        )

        reloaded = step_console._reload_changed(project, "features/steps", tracked)

        assert "features.steps.step_widget" in reloaded
        assert sys.modules["features.steps.step_widget"].current() == "edited"

    def test_nothing_is_reloaded_when_nothing_changed(self, project):
        tracked = step_console._module_mtimes(project)
        assert step_console._reload_changed(project, "features/steps", tracked) == []

    def test_pages_reload_before_step_modules(self, project):
        """Order is the whole point: the reverse leaves a stale binding."""
        tracked = step_console._module_mtimes(project)
        _edit(project / "pages" / "widget.py", "VALUE = 'x'\n")
        _edit(
            project / "features" / "steps" / "step_widget.py",
            "from pages.widget import VALUE\n\ndef current():\n    return VALUE\n",
        )

        reloaded = step_console._reload_changed(project, "features/steps", tracked)

        assert reloaded.index("pages.widget") < reloaded.index(
            "features.steps.step_widget"
        )
        assert sys.modules["features.steps.step_widget"].current() == "x"


class TestWhatIsNeverReloaded:
    """These hold the live browser and the resolved config."""

    @pytest.mark.parametrize(
        "name", ["helper.drivers.browser", "config.configs", "features.behave_env.hooks.before"]
    )
    def test_live_state_modules_are_excluded(self, name, tmp_path):
        module = type("M", (), {"__name__": name, "__file__": str(tmp_path / "x.py")})()
        assert step_console._reloadable(module, tmp_path) is False

    def test_a_project_module_is_reloadable(self, tmp_path):
        module = type(
            "M", (), {"__name__": "pages.widget", "__file__": str(tmp_path / "w.py")}
        )()
        assert step_console._reloadable(module, tmp_path) is True

    def test_a_module_outside_the_project_is_not_touched(self, tmp_path):
        module = type("M", (), {"__name__": "json", "__file__": "/usr/lib/json.py"})()
        assert step_console._reloadable(module, tmp_path) is False
