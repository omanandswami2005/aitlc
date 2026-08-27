"""`aitlc stepatlas ...` -- reads StepAtlas's catalog.json directly and
shells out to its own build/serve tooling. No real uv/pnpm/StepAtlas
checkout involved here: subprocess calls are mocked, catalog.json is a
small fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aitlc.cli import app
from typer.testing import CliRunner

runner = CliRunner()

CATALOG = {
    "steps": [
        {
            "pattern": "click on element ID: \"{locator}\"",
            "function": "click_on_element_by_id",
            "file": "features/steps/step_definition_common_page.py",
            "line": 100,
            "keywords": ["when"],
            "description": "",
            "uses_api": False,
            "used_by": [],
            "prefer_over": [],
            "superseded_by": [],
            "curator_notes": [],
            "category": {"group": "common", "slug": "common-page", "label": "Common Page"},
            "url": "/common/common-page/#anchor-a",
        },
        {
            "pattern": "select database: \"{db}\"",
            "function": "select_database",
            "file": "features/steps/step_definition_search_page.py",
            "line": 50,
            "keywords": ["when"],
            "description": "",
            "uses_api": False,
            "used_by": [],
            "prefer_over": [],
            "superseded_by": [],
            "curator_notes": [],
            "category": {"group": "pages", "slug": "search-page", "label": "Search Page"},
            "url": "/pages/search-page/#anchor-b",
        },
    ]
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    stepatlas_dir = tmp_path / "StepAtlas"
    stepatlas_dir.mkdir()
    (tmp_path / "aitlc.toml").write_text(
        f'[project]\nname = "t"\nfeature_dir = "features"\n\n'
        f'[stepatlas]\npath = "{stepatlas_dir}"\n'
    )
    (tmp_path / "features").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_info_errors_when_stepatlas_not_configured(tmp_path, monkeypatch):
    (tmp_path / "aitlc.toml").write_text('[project]\nname = "t"\n')
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["stepatlas", "info", "select"])
    assert result.exit_code == 2
    assert "no [stepatlas] path configured" in result.output


def test_info_errors_when_catalog_missing(project):
    result = runner.invoke(app, ["stepatlas", "info", "select"])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_info_finds_by_text_fragment(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info", "select database"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["function"] == "select_database"


def test_info_finds_by_exact_file_line(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(
        app, ["stepatlas", "info", "features/steps/step_definition_search_page.py:50"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["matches"][0]["function"] == "select_database"


def test_info_falls_back_to_nearest_line_in_same_file(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(
        app, ["stepatlas", "info", "features/steps/step_definition_search_page.py:55"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["matches"][0]["function"] == "select_database"


def test_info_no_match_exits_nonzero(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info", "totally-unrelated-xyz"])
    assert result.exit_code == 2


def test_build_shells_out_with_project_config(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "aitlc.commands.stepatlas_cmd.subprocess.run",
        lambda cmd, **kw: calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )
    result = runner.invoke(app, ["stepatlas", "build"])
    assert result.exit_code == 0, result.output
    cmd, kwargs = calls[0]
    assert cmd[:4] == ["uv", "run", "stepatlas", "build"]
    assert "--skip-site-build" in cmd
    assert str(kwargs["cwd"]) == str(project / "StepAtlas")


def test_serve_skip_build_only_runs_preview(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "aitlc.commands.stepatlas_cmd.subprocess.run",
        lambda cmd, **kw: calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )
    result = runner.invoke(app, ["stepatlas", "serve", "--skip-build"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == ["pnpm", "run", "preview"]


def test_serve_default_builds_when_nothing_is_built_yet(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "aitlc.commands.stepatlas_cmd.subprocess.run",
        lambda cmd, **kw: calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )
    result = runner.invoke(app, ["stepatlas", "serve"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0][0][:4] == ["uv", "run", "stepatlas", "build"]
    assert calls[1][0] == ["pnpm", "run", "preview"]


def test_serve_default_skips_build_when_already_built(project, monkeypatch):
    dist = project / "StepAtlas" / "site" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    calls = []
    monkeypatch.setattr(
        "aitlc.commands.stepatlas_cmd.subprocess.run",
        lambda cmd, **kw: calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )
    result = runner.invoke(app, ["stepatlas", "serve"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == ["pnpm", "run", "preview"]


def test_serve_rebuild_forces_build_even_if_already_built(project, monkeypatch):
    dist = project / "StepAtlas" / "site" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    calls = []
    monkeypatch.setattr(
        "aitlc.commands.stepatlas_cmd.subprocess.run",
        lambda cmd, **kw: calls.append((cmd, kw)) or type("R", (), {"returncode": 0})(),
    )
    result = runner.invoke(app, ["stepatlas", "serve", "--rebuild"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0][0][:4] == ["uv", "run", "stepatlas", "build"]
