from aitlc.core.redact import redact_mapping, redact_text


def test_redacts_known_secret_value():
    text = "the key is sk_live_abcdef1234567890 in this log line"
    assert redact_text(text, ["sk_live_abcdef1234567890"]) == (
        "the key is ***REDACTED*** in this log line"
    )


def test_ignores_short_values_to_avoid_false_positives():
    text = "status is ok"
    assert redact_text(text, ["ok"]) == "status is ok"


def test_redacts_multiple_occurrences():
    secret = "supersecrettoken123"
    text = f"{secret} appeared twice: {secret}"
    result = redact_text(text, [secret])
    assert secret not in result
    assert result.count("***REDACTED***") == 2


def test_no_secrets_is_a_noop():
    text = "nothing sensitive here"
    assert redact_text(text, []) == text


def test_redact_mapping_recurses_into_nested_structures():
    secret = "topsecretvalue1234"
    data = {"a": secret, "b": [secret, {"c": secret}]}
    redacted = redact_mapping(data, [secret])
    assert redacted["a"] == "***REDACTED***"
    assert redacted["b"][0] == "***REDACTED***"
    assert redacted["b"][1]["c"] == "***REDACTED***"


def test_longest_value_redacted_first_avoids_partial_leak():
    # A shorter secret that's a prefix of a longer one shouldn't leave the
    # longer one partially exposed if the short one were replaced first.
    short = "abcdefgh"
    long = "abcdefghijklmnop"
    text = long
    result = redact_text(text, [short, long])
    assert result == "***REDACTED***"
