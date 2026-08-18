"""Reading live page state, and calling a project function.

Both existed only as hand-written Playwright scripts before: `cdp inspect`
reported URL, screenshots and selector checks but not cookies or
localStorage, and nothing could call a helper that is not a step. "Which
account is this browser signed in as" is the question a wrong-account
failure turns on, and it lives in exactly those two places.
"""

from __future__ import annotations

import json

from aitlc.core import cdp_attach, step_console


class _Context:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return self._cookies


class _Page:
    def __init__(self, storage, raises=False):
        self._storage = storage
        self._raises = raises

    def evaluate(self, _script):
        if self._raises:
            raise RuntimeError("page closed")
        return self._storage


COOKIES = [
    {"name": "session", "domain": "app.example", "path": "/", "value": "a-real-token"},
]
STORAGE = {"local": {"user": "someone@example.com"}, "session": {}}


class TestStorageIsReadableButNotLeaked:
    def test_cookies_and_local_storage_are_reported(self):
        found = cdp_attach.collect_storage(_Context(COOKIES), _Page(STORAGE))
        assert found["cookies"][0]["name"] == "session"
        assert "user" in found["local_storage"]

    def test_values_are_fingerprinted_by_default(self):
        """A session cookie is a working credential, not a debug string."""
        found = cdp_attach.collect_storage(_Context(COOKIES), _Page(STORAGE))
        assert "a-real-token" not in json.dumps(found)
        assert found["cookies"][0]["value"].startswith("<12 chars, sha256:")

    def test_the_fingerprint_still_distinguishes_two_sessions(self):
        """Otherwise it cannot answer 'is this the same account as before'."""
        other = [dict(COOKIES[0], value="a-different-token")]
        first = cdp_attach.collect_storage(_Context(COOKIES), _Page(STORAGE))
        second = cdp_attach.collect_storage(_Context(other), _Page(STORAGE))
        assert first["cookies"][0]["value"] != second["cookies"][0]["value"]

    def test_the_same_session_fingerprints_identically(self):
        a = cdp_attach.collect_storage(_Context(COOKIES), _Page(STORAGE))
        b = cdp_attach.collect_storage(_Context(list(COOKIES)), _Page(STORAGE))
        assert a["cookies"][0]["value"] == b["cookies"][0]["value"]

    def test_reveal_prints_the_real_values(self):
        found = cdp_attach.collect_storage(_Context(COOKIES), _Page(STORAGE), reveal=True)
        assert found["cookies"][0]["value"] == "a-real-token"
        assert found["revealed"] is True

    def test_a_closed_page_reports_the_error_and_still_returns_cookies(self):
        """A snapshot must never be the thing that fails the investigation."""
        found = cdp_attach.collect_storage(_Context(COOKIES), _Page(STORAGE, raises=True))
        assert found["cookies"][0]["name"] == "session"
        assert "error" in found["local_storage"]


class TestCallPlumbing:
    def _cmd(self, monkeypatch, **kwargs):
        seen = {}

        class _Proc:
            stdout = json.dumps({"event": "call_result", "value": "someone@example.com"})
            stderr = ""

        def fake_run(cmd, **_kw):
            seen["cmd"] = cmd
            return _Proc()

        monkeypatch.setattr(step_console.subprocess, "run", fake_run)
        result = step_console.call_project_function(
            "pages.login:SignInPage.current_user",
            cwd=kwargs.pop("cwd"),
            poetry_cmd=["poetry"],
            **kwargs,
        )
        return seen["cmd"], result

    def test_the_result_is_returned_as_a_record(self, monkeypatch, tmp_path):
        _cmd, result = self._cmd(monkeypatch, cwd=tmp_path)
        assert result["value"] == "someone@example.com"

    def test_it_runs_in_the_target_project_not_ours(self, monkeypatch, tmp_path):
        """Project modules are importable only in the project's interpreter."""
        cmd, _ = self._cmd(monkeypatch, cwd=tmp_path)
        assert cmd[:2] == ["poetry", "run"]
        assert "--call" in cmd

    def test_arguments_are_passed_through(self, monkeypatch, tmp_path):
        cmd, _ = self._cmd(monkeypatch, cwd=tmp_path, args=["12", "hello"])
        assert cmd.count("--call-arg") == 2
        assert "12" in cmd and "hello" in cmd

    def test_a_console_that_says_nothing_is_an_error_not_a_silent_pass(
        self, monkeypatch, tmp_path
    ):
        class _Silent:
            stdout = ""
            stderr = "ImportError: no such module"

        monkeypatch.setattr(step_console.subprocess, "run", lambda *a, **k: _Silent())
        result = step_console.call_project_function(
            "nope:nothing", cwd=tmp_path, poetry_cmd=["poetry"]
        )
        assert result["event"] == "call_error"
        assert "ImportError" in result["stderr_tail"]
