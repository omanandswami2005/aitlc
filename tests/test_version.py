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


def test_the_version_is_declared_in_exactly_one_place():
    """It was declared twice and the two disagreed: 0.1.0 against 0.2.0.

    Two places to bump means one of them gets missed, and `--version` then
    reports a build nobody shipped.
    """
    import tomllib
    from pathlib import Path

    import aitlc

    root = Path(aitlc.__file__).resolve().parents[2]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert aitlc.__version__ == declared


def test_the_reported_version_matches_the_package():
    from importlib.metadata import version

    import aitlc

    assert aitlc.__version__ == version("aitlc")
