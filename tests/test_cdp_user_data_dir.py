"""--user-data-dir/--profile-dir on `cdp launch` and `debug start`.

chrome_cdp.launch() already fully supported a custom user_data_dir
internally (a persistent named profile, e.g. one with saved logins, reused
across days -- distinct from aitlc's own auto-generated .cdp/profile-<port>
scratch dirs); only the CLI was missing the option. This is the wiring
test -- mocked, since the point is "does the flag reach the function",
not re-testing chrome_cdp.launch's own already-covered behavior.
"""

from __future__ import annotations

from pathlib import Path

from aitlc.cli import app
from aitlc.commands import cdp_cmd, debug_cmd
from aitlc.core import chrome_cdp
from typer.testing import CliRunner

runner = CliRunner()


def test_cdp_launch_forwards_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cdp_cmd.AitlcConfig, "find_and_load", staticmethod(lambda: _Cfg(tmp_path)))
    calls = []
    instance = chrome_cdp.CdpInstance(
        pid=1, port=9333, user_data_dir="/custom/profile", started_at=0.0
    )
    monkeypatch.setattr(
        cdp_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: calls.append(k) or (instance, False),
    )

    result = runner.invoke(
        app, ["cdp", "launch", "--user-data-dir", "/custom/profile"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["user_data_dir"] == Path("/custom/profile")


class _Cfg:
    def __init__(self, root: Path) -> None:
        self.root_dir = root


def test_debug_start_forwards_user_data_dir(monkeypatch, tmp_path):
    from test_debug_gate import _wire, runner as debug_runner

    _wire(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        debug_cmd.chrome_cdp,
        "launch",
        lambda *a, **k: calls.append(k)
        or (type("I", (), {"cdp_url": "http://127.0.0.1:9999", "port": 9999})(), False),
    )

    result = debug_runner.invoke(
        debug_cmd.app,
        ["start", "PROJ-1", "--at", "0", "--timeout", "60", "--user-data-dir", "/custom/profile"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["user_data_dir"] == Path("/custom/profile")

    debug_runner.invoke(debug_cmd.app, ["stop", "PROJ-1"])
