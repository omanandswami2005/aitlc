import pytest
from aitlc.core.toon import ToonEncodeError, encode_table


def test_encodes_uniform_rows():
    rows = [
        {"scenario": "A", "step": "click X", "error": "timeout"},
        {"scenario": "B", "step": "click Y", "error": "not found"},
    ]
    out = encode_table(rows, name="failures")
    lines = out.splitlines()
    assert lines[0] == "failures[2]{scenario,step,error}:"
    assert lines[1] == "  A,click X,timeout"
    assert lines[2] == "  B,click Y,not found"


def test_quotes_values_with_commas():
    rows = [{"a": "has,comma", "b": "plain"}]
    out = encode_table(rows)
    assert '"has,comma"' in out


def test_empty_rows_raises():
    with pytest.raises(ToonEncodeError):
        encode_table([])


def test_non_uniform_rows_raises():
    rows = [{"a": 1, "b": 2}, {"a": 1, "c": 3}]
    with pytest.raises(ToonEncodeError):
        encode_table(rows)


def test_none_value_encodes_as_empty():
    rows = [{"a": None, "b": "x"}]
    out = encode_table(rows)
    assert out.splitlines()[1] == "  ,x"
