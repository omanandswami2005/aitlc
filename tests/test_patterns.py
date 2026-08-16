from pathlib import Path

import pytest
from aitlc.core.patterns import Pattern, PatternLibrary


def test_pattern_matches_both_step_and_error():
    p = Pattern(
        id="x",
        description="d",
        step_contains=["has type chip"],
        error_contains=["unexpected value"],
    )
    assert p.matches('has type chip: "Suppressed"', 'unexpected value "0"')


def test_pattern_does_not_match_when_step_missing():
    p = Pattern(id="x", description="d", step_contains=["has type chip"])
    assert not p.matches("click on saved criteria row", "some error")


def test_pattern_does_not_match_when_error_missing():
    p = Pattern(id="x", description="d", error_contains=["InvalidProxyURL"])
    assert not p.matches("some step", "a normal timeout")


def test_pattern_is_case_insensitive():
    p = Pattern(id="x", description="d", error_contains=["InvalidProxyURL"])
    assert p.matches("step", "requests.exceptions.invalidproxyurl: bad host")


def test_pattern_with_only_error_contains_ignores_step():
    p = Pattern(id="x", description="d", error_contains=["ERR::WS::CTRL::CONN::DWN"])
    assert p.matches("any step at all", "tunnel log: ERR::WS::CTRL::CONN::DWN")


def test_empty_pattern_matches_nothing():
    p = Pattern(id="x", description="d")
    assert not p.matches("anything", "anything")


def test_library_classify_returns_first_match():
    lib = PatternLibrary(
        [
            Pattern(id="a", description="d", error_contains=["boom"]),
            Pattern(id="b", description="d", error_contains=["boom"]),
        ]
    )
    match = lib.classify("step", "boom happened")
    assert match is not None
    assert match.pattern.id == "a"


def test_library_classify_returns_none_when_unmatched():
    lib = PatternLibrary([Pattern(id="a", description="d", error_contains=["boom"])])
    assert lib.classify("step", "something totally different") is None


@pytest.fixture
def real_patterns_yaml(tmp_path: Path) -> Path:
    # A minimal copy mirroring the real patterns.yaml's shape/content for
    # the specific patterns exercised below, kept in sync by hand rather
    # than reading the real file, so this test doesn't silently pass/fail
    # based on unrelated edits to the real patterns.yaml.
    content = """
patterns:
  - id: backend-suppression-timing
    description: "Backend suppression job timing"
    match:
      step_contains: ["has type chip: \\"Suppressed\\""]
      error_contains: ["unexpected value"]
    suggested_action: "retry solo"
  - id: lt-proxy-invalid-url
    description: "Proxy dict built even when unset"
    match:
      error_contains: ["InvalidProxyURL", "malformed and could be missing the host"]
    suggested_action: "check LT_PROXY_HOST/PORT"
"""
    path = tmp_path / "patterns.yaml"
    path.write_text(content)
    return path


def test_loads_real_shaped_yaml_and_classifies_known_failure(real_patterns_yaml: Path):
    lib = PatternLibrary.load(real_patterns_yaml)
    # Real failure text from this project's own session (PROJ-32056).
    match = lib.classify(
        'Then validate saved criteria "random_name2" has type chip: "Suppressed"',
        '- unexpected value "0"',
    )
    assert match is not None
    assert match.pattern.id == "backend-suppression-timing"


def test_loads_real_shaped_yaml_and_classifies_proxy_failure(real_patterns_yaml: Path):
    lib = PatternLibrary.load(real_patterns_yaml)
    # Real failure text from this project's own session (PROJ-32054).
    match = lib.classify(
        'Then create user from provisioning api: "user_email1:sc" using queue',
        "requests.exceptions.InvalidProxyURL: Please check proxy URL. "
        "It is malformed and could be missing the host.",
    )
    assert match is not None
    assert match.pattern.id == "lt-proxy-invalid-url"
