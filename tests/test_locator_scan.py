from pathlib import Path

from aitlc.core.locator_scan import scan_directory


def test_finds_string_dict_entries(tmp_path: Path):
    (tmp_path / "locators.py").write_text(
        "locators = {\n"
        '    "audience_tab": "//button[@id=\'audience\']",\n'
        '    "sign_in_link": "//a[text()=\'Sign In\']",\n'
        "}\n"
    )
    result = scan_directory(tmp_path)
    assert "//button[@id='audience']" in result
    assert result["//button[@id='audience']"].key == "audience_tab"


def test_ignores_non_string_values(tmp_path: Path):
    (tmp_path / "locators.py").write_text(
        'CONFIG = {"timeout": 30, "enabled": True, "name": "real_locator"}\n'
    )
    result = scan_directory(tmp_path)
    assert "real_locator" in result
    assert 30 not in result.values()


def test_scans_nested_directories(tmp_path: Path):
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "more_locators.py").write_text('L = {"key1": "value1"}\n')
    result = scan_directory(tmp_path)
    assert "value1" in result


def test_skips_unparseable_files_without_crashing(tmp_path: Path):
    (tmp_path / "broken.py").write_text("this is not { valid python :::")
    (tmp_path / "good.py").write_text('L = {"a": "b"}\n')
    result = scan_directory(tmp_path)
    assert "b" in result


def test_missing_directory_returns_empty(tmp_path: Path):
    result = scan_directory(tmp_path / "does-not-exist")
    assert result == {}


def test_first_occurrence_wins_on_duplicate_value(tmp_path: Path):
    (tmp_path / "a.py").write_text('L = {"first_key": "shared_value"}\n')
    (tmp_path / "b.py").write_text('L = {"second_key": "shared_value"}\n')
    result = scan_directory(tmp_path)
    # a.py sorts before b.py — first-seen wins, deterministic
    assert result["shared_value"].key == "first_key"
