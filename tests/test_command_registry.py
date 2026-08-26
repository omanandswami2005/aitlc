"""The command registry mounts exactly the expected commands, in order.

This locks the discovery-based wiring against the hand-wired list it replaced:
if a command stops being discovered (a dropped `COMMAND`, a renamed attr) or a
new one appears, this test says so by name rather than letting a subcommand go
silently missing.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from aitlc.commands import _registry

runner = CliRunner()

# The full set, in mount order. "" is the root escape-hatch group
# (aitlc behave / aitlc pw), mounted last.
EXPECTED_ORDER = [
    "run",
    "env",
    "doctor",
    "init",
    "classify-failure",
    "report",
    "record",
    "start",
    "propose-fix",
    "notify-teams",
    "xray",
    "cdp",
    "call",
    "preflight",
    "debug",
    "tunnel",
    "jira",
    "trace",
    "steps",
    "s3",
    "parallel",
    "users",
    "history",
    "journal",
    "locators",
    "",
]


def test_registry_mounts_expected_commands_in_order():
    app = typer.Typer()
    mounted = _registry.register_all(app)
    assert mounted == EXPECTED_ORDER


def test_cli_app_exposes_the_same_set():
    from aitlc.cli import app

    plain = {c.name for c in app.registered_commands}
    groups = {g.name for g in app.registered_groups}
    expected_plain = {n for n in EXPECTED_ORDER if n in _PLAIN}
    expected_groups = {n for n in EXPECTED_ORDER if n not in _PLAIN}
    assert plain == expected_plain
    assert groups == expected_groups


def test_help_runs():
    from aitlc.cli import app

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_bad_kind_is_rejected_loudly():
    import pytest

    with pytest.raises(ValueError):
        _registry.CommandSpec(name="x", attr="app", kind="nonsense")


def test_plain_command_requires_a_name():
    import pytest

    with pytest.raises(ValueError):
        _registry.CommandSpec(name="", attr="fn", kind="command")


_PLAIN = {
    "run",
    "env",
    "doctor",
    "init",
    "classify-failure",
    "report",
    "record",
    "start",
    "propose-fix",
    "notify-teams",
}
