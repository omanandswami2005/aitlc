from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from aitlc.adapters.s3.evidence import S3EvidenceError, download_trace, find_trace_keys


def _obj(key: str, ts: float) -> dict:
    return {"Key": key, "LastModified": datetime.fromtimestamp(ts, tz=timezone.utc)}


def test_find_trace_keys_filters_by_test_id_substring():
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "Contents": [
                _obj("traces/PROJ-32054-1.1__20260812.zip", 200),
                _obj("traces/PROJ-99999-1.1__20260811.zip", 100),
            ]
        }
    ]
    results = find_trace_keys(client, "my-bucket", "PROJ-32054")
    assert len(results) == 1
    assert results[0].key == "traces/PROJ-32054-1.1__20260812.zip"


def test_find_trace_keys_sorts_newest_first():
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [
        {
            "Contents": [
                _obj("t/PROJ-1-old.zip", 100),
                _obj("t/PROJ-1-new.zip", 300),
                _obj("t/PROJ-1-mid.zip", 200),
            ]
        }
    ]
    results = find_trace_keys(client, "bucket", "PROJ-1")
    assert [r.key for r in results] == [
        "t/PROJ-1-new.zip",
        "t/PROJ-1-mid.zip",
        "t/PROJ-1-old.zip",
    ]


def test_find_trace_keys_empty_is_not_an_error():
    client = MagicMock()
    client.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
    results = find_trace_keys(client, "bucket", "PROJ-nonexistent")
    assert results == []


def test_download_trace_wraps_boto_errors(tmp_path):
    client = MagicMock()
    client.download_file.side_effect = Exception("403 Forbidden")
    with pytest.raises(S3EvidenceError, match="403 Forbidden"):
        download_trace(client, "bucket", "key", tmp_path / "out.zip")


def test_download_trace_creates_parent_dirs(tmp_path):
    client = MagicMock()
    out = tmp_path / "nested" / "dir" / "trace.zip"
    result = download_trace(client, "bucket", "key", out)
    assert result == out
    assert out.parent.exists()
    client.download_file.assert_called_once_with("bucket", "key", str(out))
