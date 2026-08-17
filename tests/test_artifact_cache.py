"""Fetched artifacts are kept, so looking at the same run twice is free."""

from __future__ import annotations

from aitlc.core import artifact_cache

KEY = "project/behave_results/Stage/A_Test_Plan/behave_PROJ-1_2026-01-01T00-00-00.json"


def test_miss_then_hit(tmp_path):
    source = tmp_path / "downloaded.json"
    source.write_text('{"ok": true}')

    assert artifact_cache.get(tmp_path, KEY) is None
    artifact_cache.put(tmp_path, KEY, source, source="s3")
    cached = artifact_cache.get(tmp_path, KEY)
    assert cached is not None
    assert cached.read_text() == '{"ok": true}'


def test_keys_that_flatten_to_the_same_name_do_not_collide(tmp_path):
    """S3 keys are nested; flattening slashes alone would let these collide."""
    a = tmp_path / "a.json"
    a.write_text("A")
    b = tmp_path / "b.json"
    b.write_text("B")

    artifact_cache.put(tmp_path, "plan-one/behave_PROJ-1.json", a)
    artifact_cache.put(tmp_path, "plan-two/behave_PROJ-1.json", b)

    assert (
        artifact_cache.get(tmp_path, "plan-one/behave_PROJ-1.json").read_text() == "A"
    )
    assert (
        artifact_cache.get(tmp_path, "plan-two/behave_PROJ-1.json").read_text() == "B"
    )


def test_stats_and_clear(tmp_path):
    source = tmp_path / "x.json"
    source.write_text("12345")
    artifact_cache.put(tmp_path, KEY, source)

    stats = artifact_cache.stats(tmp_path)
    assert stats["entries"] == 1 and stats["bytes"] == 5

    assert artifact_cache.clear(tmp_path) >= 1
    assert artifact_cache.get(tmp_path, KEY) is None


def test_a_corrupt_index_does_not_break_a_later_put(tmp_path):
    source = tmp_path / "x.json"
    source.write_text("1")
    artifact_cache.put(tmp_path, KEY, source)
    (artifact_cache.cache_dir(tmp_path) / "index.json").write_text("not json")
    artifact_cache.put(tmp_path, KEY + "-2", source)  # must not raise
    assert artifact_cache.get(tmp_path, KEY + "-2") is not None
