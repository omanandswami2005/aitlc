"""`--version` reports what is actually installed."""

from __future__ import annotations

import json

from aitlc import cli
from typer.testing import CliRunner

runner = CliRunner()


def test_version_prints_json_and_exits_zero():
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["aitlc"]


def test_version_matches_package_metadata_not_a_constant():
    """A hardcoded version is a second place to forget to bump."""
    from importlib.metadata import version

    assert json.loads(runner.invoke(cli.app, ["--version"]).output)["aitlc"] == version(
        "aitlc"
    )


def test_short_flag_works_too():
    assert runner.invoke(cli.app, ["-V"]).exit_code == 0


def test_subcommands_still_run_with_the_callback_installed():
    """A root callback can silently break every subcommand's arg parsing."""
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "debug" in result.output
