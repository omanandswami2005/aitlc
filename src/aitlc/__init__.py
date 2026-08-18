"""aitlc — structured CLI for Behave+Playwright debugging and Xray verification workflows."""

# Read from installed metadata rather than repeated here. This constant said
# 0.1.0 while pyproject said 0.2.0 -- two places to bump, one of them missed,
# and `aitlc --version` would have reported a build nobody shipped.
try:  # pragma: no cover - trivial, and exercised by test_packaging
    from importlib.metadata import version as _version

    __version__ = _version("aitlc")
except Exception:  # pragma: no cover - running from a bare source tree
    __version__ = "unknown"
