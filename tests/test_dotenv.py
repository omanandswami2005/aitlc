import os
from pathlib import Path

from aitlc.core.dotenv import load_dotenv


def test_loads_simple_key_value(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_VAR", raising=False)
    (tmp_path / ".env").write_text("SOME_TEST_VAR=hello\n")
    assert load_dotenv(tmp_path / ".env") is True
    assert os.environ["SOME_TEST_VAR"] == "hello"


def test_strips_quotes(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QUOTED_VAR", raising=False)
    (tmp_path / ".env").write_text('QUOTED_VAR="hello world"\n')
    load_dotenv(tmp_path / ".env")
    assert os.environ["QUOTED_VAR"] == "hello world"


def test_skips_comments_and_blank_lines(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("REAL_VAR", raising=False)
    (tmp_path / ".env").write_text("# a comment\n\nREAL_VAR=x\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["REAL_VAR"] == "x"


def test_does_not_overwrite_existing_shell_var(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PRESET_VAR", "from_shell")
    (tmp_path / ".env").write_text("PRESET_VAR=from_dotenv\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["PRESET_VAR"] == "from_shell"


def test_missing_file_returns_false(tmp_path: Path):
    assert load_dotenv(tmp_path / "does-not-exist.env") is False
