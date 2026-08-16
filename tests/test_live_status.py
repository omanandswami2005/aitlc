import io
import json
import types
from pathlib import Path

import pytest
from aitlc.core.live_status import LiveStatusFormatter
from behave.formatter.base import StreamOpener


class _FakeStatus:
    def __init__(self, name: str):
        self.name = name


class _FakeStep:
    def __init__(self, keyword: str, name: str, status: str):
        self.keyword = keyword
        self.name = name
        self.status = _FakeStatus(status)


def _make_formatter() -> LiveStatusFormatter:
    # Real StreamOpener, not a stand-in — LiveStatusFormatter writes to
    # AITLC_STATUS_FILE directly and never touches self.stream, but
    # behave's own Formatter.__init__ requires a real stream_opener shape.
    stream_opener = StreamOpener(stream=io.StringIO())
    config = types.SimpleNamespace()
    return LiveStatusFormatter(stream_opener, config)


@pytest.fixture
def formatter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    status_path = tmp_path / "current_step.json"
    monkeypatch.setenv("AITLC_STATUS_FILE", str(status_path))
    f = _make_formatter()
    f._status_path_for_test = status_path  # type: ignore[attr-defined]
    return f


def test_writes_status_on_step(formatter):
    step = _FakeStep("When", "do a thing", "running")
    formatter.step(step)
    data = json.loads(formatter._status_path_for_test.read_text())
    assert data["step"] == "When do a thing"
    assert data["status"] == "running"


def test_result_increments_passed_count(formatter):
    step = _FakeStep("When", "do a thing", "passed")
    formatter.result(step)
    data = json.loads(formatter._status_path_for_test.read_text())
    assert data["steps_passed"] == 1
    assert data["status"] == "passed"


def test_result_increments_failed_count(formatter):
    formatter.result(_FakeStep("When", "a", "passed"))
    formatter.result(_FakeStep("Then", "b", "failed"))
    data = json.loads(formatter._status_path_for_test.read_text())
    assert data["steps_passed"] == 1
    assert data["steps_failed"] == 1


def test_file_is_overwritten_not_appended(formatter):
    formatter.result(_FakeStep("When", "first step", "passed"))
    formatter.result(_FakeStep("Then", "second step", "passed"))
    content = formatter._status_path_for_test.read_text()
    # Exactly one JSON object — not two concatenated, not an array growing.
    data = json.loads(content)
    assert data["step"] == "Then second step"


def test_no_status_env_var_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("AITLC_STATUS_FILE", raising=False)
    f = _make_formatter()
    # Should not raise even with nowhere to write.
    f.step(_FakeStep("When", "x", "running"))
