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


CATALOG_URL = {
    "steps": CATALOG["steps"]
    + [
        {
            "pattern": "click on the element locator \"{locator}\" and wait for the "
            "next element locator \"{next_locator}\"",
            "function": "click_and_wait_direct_mail_menu_item",
            "file": "features/steps/step_definition_admin_page.py",
            "line": 304,
            "keywords": ["when"],
            "description": "",
            "uses_api": False,
            "used_by": [],
            "prefer_over": [],
            "superseded_by": [],
            "curator_notes": [],
            "category": {"group": "pages", "slug": "direct-mail", "label": "Direct Mail"},
            "url": "/pages/direct-mail/#anchor-c",
        },
        {
            "pattern": "Open Admin Panel and select \"{options}\"",
            "function": "open_admin_panel_and_select_option_from_list",
            "file": "features/steps/step_definition_admin_page.py",
            "line": 91,
            "keywords": ["when"],
            "description": "",
            "uses_api": False,
            "used_by": [],
            "prefer_over": [],
            "superseded_by": [],
            "curator_notes": [],
            "category": {"group": "pages", "slug": "admin-page", "label": "Admin Page"},
            "url": "/pages/admin-page/#anchor-d",
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


def test_info_filters_by_page_slug_alone_no_query_needed(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info", "--page", "search-page"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["function"] == "select_database"


def test_info_filters_by_page_label_case_insensitive_substring(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info", "--page", "common page"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["function"] == "click_on_element_by_id"


def test_info_filters_by_group(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info", "--group", "pages"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["function"] == "select_database"


def test_info_uses_api_filter_composes_with_page(project):
    catalog = json.loads(json.dumps(CATALOG))
    catalog["steps"][1]["uses_api"] = True
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(catalog))

    result = runner.invoke(app, ["stepatlas", "info", "--group", "pages", "--uses-api"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["matches"][0]["function"] == "select_database"

    result = runner.invoke(app, ["stepatlas", "info", "--group", "pages", "--no-uses-api"])
    assert result.exit_code == 2


def test_info_filters_by_file_substring(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info", "--file", "search_page"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["matches"][0]["function"] == "select_database"


def test_info_page_filter_composes_with_free_text_query(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    # "click" only appears in the common-page step's pattern -- --page narrows
    # to search-page first, so this must find nothing despite the query alone
    # matching a step elsewhere in the catalog.
    result = runner.invoke(app, ["stepatlas", "info", "click", "--page", "search-page"])
    assert result.exit_code == 2


def test_info_requires_a_query_or_at_least_one_filter(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG))
    result = runner.invoke(app, ["stepatlas", "info"])
    assert result.exit_code == 2
    assert "provide a query" in result.output


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


def test_stop_kills_matching_preview_process(project, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "pgrep":
            return type("R", (), {"returncode": 0, "stdout": "12345\n"})()
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr("aitlc.commands.stepatlas_cmd.subprocess.run", fake_run)
    result = runner.invoke(app, ["stepatlas", "stop"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"stopped": [12345]}
    assert calls[0][0] == "pgrep"
    assert calls[1] == ["kill", "12345"]


def test_stop_reports_empty_when_nothing_running(project, monkeypatch):
    monkeypatch.setattr(
        "aitlc.commands.stepatlas_cmd.subprocess.run",
        lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": ""})(),
    )
    result = runner.invoke(app, ["stepatlas", "stop"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"stopped": []}


def test_info_url_matches_camel_case_path_segment_to_kebab_case_slug(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG_URL))
    result = runner.invoke(
        app,
        [
            "stepatlas",
            "info",
            "--url",
            "https://app.example.com/admin/accounts/settings/directMail?acId=abc123",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    functions = {m["function"] for m in payload["matches"]}
    # "directMail" (camelCase path segment) must resolve to the "direct-mail"
    # (kebab-case slug) category -- tokenization has to normalize past the
    # casing convention mismatch, not assume one.
    assert "click_and_wait_direct_mail_menu_item" in functions
    assert "select_database" not in functions  # unrelated category, excluded


def test_info_url_reports_matched_categories_and_scores(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG_URL))
    result = runner.invoke(
        app, ["stepatlas", "info", "--url", "https://app.example.com/admin/accounts"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"] == "https://app.example.com/admin/accounts"
    slugs = {c["slug"] for c in payload["url_match"]}
    assert "admin-page" in slugs
    for c in payload["url_match"]:
        assert 0 < c["score"] <= 1
        assert c["matched_tokens"]


def test_info_url_with_no_token_overlap_errors_with_a_hint(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG_URL))
    result = runner.invoke(
        app, ["stepatlas", "info", "--url", "https://app.example.com/totally/unrelated/zzz"]
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["url_match"] == []
    assert "hint" in payload


def test_info_url_composes_with_keyword_filter(project):
    (project / "StepAtlas" / "catalog.json").write_text(json.dumps(CATALOG_URL))
    result = runner.invoke(
        app,
        [
            "stepatlas",
            "info",
            "--url",
            "https://app.example.com/admin/accounts/settings/directMail",
            "--keyword",
            "then",
        ],
    )
    # Every step in CATALOG_URL is "when" -- --keyword then must narrow to nothing
    # despite --url matching real categories.
    assert result.exit_code == 2


def test_info_url_query_param_is_never_tokenized():
    from aitlc.commands.stepatlas_cmd import _url_path_tokens

    tokens = _url_path_tokens(
        "https://app.example.com/admin/accounts?acId=QWNjb3VudFR5cGU6NDk1MWVj"
    )
    assert "admin" in tokens
    assert "accounts" in tokens
    assert not any("qwnjb3vudfr5" in t for t in tokens)
