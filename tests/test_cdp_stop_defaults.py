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
from aitlc.core import chrome_cdp

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
    assert stopped_calls == [9333]
