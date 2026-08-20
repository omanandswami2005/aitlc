"""aitlc ties runs to a live CDP browser, and wraps `paver`.

Covers the two behaviours added for the "stop doing full runs, reuse the open
browser" workstream:

* `chrome_cdp.resolve_live_cdp_url` returns only a *running* instance.
* `aitlc paver ...` forwards to `poetry run paver ...` and, when a live debug
  Chrome exists, sets the project's CDP env var so the suite attaches to it.

The paver test asserts on the exact command and env overrides via
`--print-command`, so no real paver or browser is needed — but nothing here
stubs the thing under test: `resolve_live_cdp_url` runs for real over a
`list_instances` result, and the command builds its real argv.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from aitlc.cli import app
from aitlc.config import AitlcConfig
from aitlc.core import chrome_cdp
from aitlc.commands import passthrough_cmd

runner = CliRunner()


def _command_payload(output: str) -> dict:
    """Extract the `--print-command` JSON (indented) from mixed stdout+stderr.

    The CDP attach note is emitted compactly on stderr and the print-command
    payload is indented on stdout; CliRunner merges the two streams, so pick the
    payload out by its `{\\n` opening rather than assuming it is the whole output.
    """
    start = output.find("{\n")
    assert start != -1, f"no print-command payload in: {output!r}"
    return json.loads(output[start:])


def test_resolve_live_cdp_url_ignores_dead_instances(monkeypatch, tmp_path):
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root: [
            {"port": 9333, "running": False, "cdp_url": "http://127.0.0.1:9333"},
        ],
    )
    assert chrome_cdp.resolve_live_cdp_url(tmp_path) is None


def test_resolve_live_cdp_url_prefers_highest_running_port(monkeypatch, tmp_path):
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root: [
            {"port": 9333, "running": True, "cdp_url": "http://127.0.0.1:9333"},
            {"port": 9500, "running": True, "cdp_url": "http://127.0.0.1:9500"},
            {"port": 9600, "running": False, "cdp_url": "http://127.0.0.1:9600"},
        ],
    )
    assert chrome_cdp.resolve_live_cdp_url(tmp_path) == "http://127.0.0.1:9500"


def test_config_default_cdp_env_var():
    assert AitlcConfig().playwright_cdp_env == "PLAYWRIGHT_CDP_URL"


def test_config_reads_custom_cdp_env_var(tmp_path):
    (tmp_path / "aitlc.toml").write_text(
        '[project]\nname = "x"\nplaywright_cdp_env = "MY_CDP_URL"\n'
    )
    config = AitlcConfig.find_and_load(tmp_path)
    assert config.playwright_cdp_env == "MY_CDP_URL"


def test_paver_passthrough_forwards_args_and_attaches_live_cdp(monkeypatch):
    monkeypatch.setattr(passthrough_cmd.behave_runner, "resolve_poetry", lambda: ["poetry"])
    monkeypatch.setattr(
        passthrough_cmd.chrome_cdp,
        "resolve_live_cdp_url",
        lambda root: "http://127.0.0.1:9333",
    )
    # No pre-set CDP var in the environment, so the live instance is used.
    monkeypatch.delenv("PLAYWRIGHT_CDP_URL", raising=False)

    result = runner.invoke(
        app, ["paver", "run", "parallel", "--local", "--print-command"]
    )
    assert result.exit_code == 0
    payload = _command_payload(result.output)
    assert payload["command"] == ["poetry", "run", "paver", "run", "parallel", "--local"]
    assert payload["env_overrides"] == {"PLAYWRIGHT_CDP_URL": "http://127.0.0.1:9333"}


def test_paver_passthrough_no_cdp_flag_skips_attach(monkeypatch):
    monkeypatch.setattr(passthrough_cmd.behave_runner, "resolve_poetry", lambda: ["poetry"])
    monkeypatch.setattr(
        passthrough_cmd.chrome_cdp,
        "resolve_live_cdp_url",
        lambda root: "http://127.0.0.1:9333",
    )
    monkeypatch.delenv("PLAYWRIGHT_CDP_URL", raising=False)

    result = runner.invoke(
        app, ["paver", "run", "parallel", "--aitlc-no-cdp", "--print-command"]
    )
    assert result.exit_code == 0
    payload = _command_payload(result.output)
    assert payload["command"] == ["poetry", "run", "paver", "run", "parallel"]
    assert payload["env_overrides"] == {}


def test_paver_passthrough_requires_args():
    result = runner.invoke(app, ["paver"])
    assert result.exit_code == 2
    assert "no paver arguments given" in result.output


def test_paver_does_not_override_existing_env(monkeypatch):
    monkeypatch.setattr(passthrough_cmd.behave_runner, "resolve_poetry", lambda: ["poetry"])
    monkeypatch.setattr(
        passthrough_cmd.chrome_cdp,
        "resolve_live_cdp_url",
        lambda root: "http://127.0.0.1:9333",
    )
    monkeypatch.setenv("PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")

    result = runner.invoke(app, ["paver", "run", "parallel", "--print-command"])
    assert result.exit_code == 0
    payload = _command_payload(result.output)
    # The pre-set value is honoured by the child (inherited from os.environ),
    # so aitlc adds no override of its own.
    assert payload["env_overrides"] == {}
