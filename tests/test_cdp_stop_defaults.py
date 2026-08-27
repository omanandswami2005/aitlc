"""`cdp stop` must default to the newest RUNNING tracked instance, not a
fixed port.

Real confusion hit live: with one real, currently-open browser tracked on
its own port and nothing at all tracked on the fixed default port (9333),
bare `cdp stop` reported `{"port": 9333, "stopped": true}` -- a false
positive (nothing was ever running at 9333, so `chrome_cdp.stop`'s own
"stopped or probe(port) is None" fallback trivially reads as success) --
while the actual visible browser window was left running, untouched.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from aitlc.commands import cdp_cmd
from aitlc.core import chrome_cdp, debug_session

runner = CliRunner()


def test_stop_with_no_port_targets_the_newest_running_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cdp_cmd.AitlcConfig,
        "find_and_load",
        classmethod(lambda cls: type("C", (), {"root_dir": tmp_path})()),
    )
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root_dir: [
            {"port": 9333, "running": False},  # stale record, nothing there
            {"port": 56097, "running": True},  # the real, currently-open browser
        ],
    )
    stopped_calls = []
    monkeypatch.setattr(
        chrome_cdp,
        "stop",
        lambda root_dir, *, port: stopped_calls.append(port) or True,
    )

    result = runner.invoke(cdp_cmd.app, ["stop"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"port": 56097, "stopped": True}
    assert stopped_calls == [56097]


def test_stop_with_no_running_instance_errors_instead_of_a_false_positive(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        cdp_cmd.AitlcConfig,
        "find_and_load",
        classmethod(lambda cls: type("C", (), {"root_dir": tmp_path})()),
    )
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root_dir: [{"port": 9333, "running": False}],
    )

    def _boom(*a, **k):
        raise AssertionError("chrome_cdp.stop must not be called with nothing running")

    monkeypatch.setattr(chrome_cdp, "stop", _boom)

    result = runner.invoke(cdp_cmd.app, ["stop"])
    assert result.exit_code == 2
    assert "no running tracked instance" in result.output


def test_stop_with_explicit_port_ignores_which_instance_is_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cdp_cmd.AitlcConfig,
        "find_and_load",
        classmethod(lambda cls: type("C", (), {"root_dir": tmp_path})()),
    )
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root_dir: [
            {"port": 9333, "running": True},
            {"port": 56097, "running": True},
        ],
    )
    stopped_calls = []
    monkeypatch.setattr(
        chrome_cdp,
        "stop",
        lambda root_dir, *, port: stopped_calls.append(port) or True,
    )

    result = runner.invoke(cdp_cmd.app, ["stop", "--port", "9333"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"port": 9333, "stopped": True}


def test_stop_warns_when_a_live_debug_session_was_using_that_port(monkeypatch, tmp_path):
    """Real confusion hit live: `cdp stop` killed the browser a live `debug`
    session was actively using, with no warning at all -- `debug status`/
    `continue` kept reporting success afterward on any step that never
    touches the page, silently masking that the session's browser was gone.
    """
    monkeypatch.setattr(
        cdp_cmd.AitlcConfig,
        "find_and_load",
        classmethod(lambda cls: type("C", (), {"root_dir": tmp_path})()),
    )
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root_dir: [{"port": 64111, "running": True}],
    )
    monkeypatch.setattr(chrome_cdp, "stop", lambda root_dir, *, port: True)
    debug_session.save(
        tmp_path,
        debug_session.DebugSession(
            test_id="run_this",
            feature="features/run_this.feature",
            cdp_url="http://127.0.0.1:64111",
            port=64111,
        ),
    )

    result = runner.invoke(cdp_cmd.app, ["stop"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["port"] == 64111
    assert "run_this" in payload["warning"]


def test_stop_all_warns_for_every_orphaned_session(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cdp_cmd.AitlcConfig,
        "find_and_load",
        classmethod(lambda cls: type("C", (), {"root_dir": tmp_path})()),
    )
    monkeypatch.setattr(
        chrome_cdp, "stop_all", lambda root_dir: [64111, 9333]
    )
    debug_session.save(
        tmp_path,
        debug_session.DebugSession(
            test_id="run_this",
            feature="features/run_this.feature",
            cdp_url="http://127.0.0.1:64111",
            port=64111,
        ),
    )

    result = runner.invoke(cdp_cmd.app, ["stop", "--all"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "run_this" in payload["warning"]


def test_stop_no_warning_when_nothing_was_orphaned(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cdp_cmd.AitlcConfig,
        "find_and_load",
        classmethod(lambda cls: type("C", (), {"root_dir": tmp_path})()),
    )
    monkeypatch.setattr(
        chrome_cdp,
        "list_instances",
        lambda root_dir: [{"port": 64111, "running": True}],
    )
    monkeypatch.setattr(chrome_cdp, "stop", lambda root_dir, *, port: True)

    result = runner.invoke(cdp_cmd.app, ["stop"])
    assert result.exit_code == 0, result.output
    assert "warning" not in json.loads(result.stdout)
