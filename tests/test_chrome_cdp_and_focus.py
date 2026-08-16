"""Tests for CDP lifecycle helpers and the persistent focus selection.

Deliberately no real Chrome launch here — that is covered by live use.
These cover the pure logic and the failure modes that were silent bugs.
"""

from __future__ import annotations

from aitlc.core import chrome_cdp, focus

# An inert path used only to populate a dataclass field; never opened.
FIXTURE_DIR = "/tmp/p"  # nosec B108


class TestDebugEnv:
    def test_sets_both_vars_together(self):
        # A suite's own pause hook gates on its pause flag *and*
        # PLAYWRIGHT_CDP_URL — setting one alone silently tears the failed
        # page down, which is the bug this pairing prevents.
        env = chrome_cdp.debug_env("http://127.0.0.1:9333")
        assert env["PLAYWRIGHT_CDP_URL"] == "http://127.0.0.1:9333"
        assert env["DEBUG_PAUSE_ON_FAILURE"] == "1"

    def test_pause_can_be_disabled(self):
        env = chrome_cdp.debug_env("http://127.0.0.1:9333", pause_on_failure=False)
        assert "DEBUG_PAUSE_ON_FAILURE" not in env
        assert env["PLAYWRIGHT_CDP_URL"] == "http://127.0.0.1:9333"


class TestFreePort:
    def test_returns_a_usable_port(self):
        port = chrome_cdp.free_port()
        assert 1024 < port <= 65535

    def test_successive_calls_differ(self):
        # Isolated instances depend on not colliding; identical ports would
        # make two "isolated" browsers fight over one endpoint.
        ports = {chrome_cdp.free_port() for _ in range(5)}
        assert len(ports) > 1


class TestProbe:
    def test_dead_port_is_none_not_exception(self):
        # A closed port must read as "not running", not raise — status/list
        # call this for every tracked instance.
        assert chrome_cdp.probe(chrome_cdp.free_port(), timeout=0.2) is None


class TestState:
    def test_missing_state_is_none(self, tmp_path):
        assert chrome_cdp.load_state(tmp_path, 9333) is None

    def test_save_then_load_round_trips(self, tmp_path):
        instance = chrome_cdp.CdpInstance(
            pid=123,
            port=9333,
            user_data_dir=FIXTURE_DIR,
            started_at=1.0,
        )
        chrome_cdp.save_state(tmp_path, instance)
        loaded = chrome_cdp.load_state(tmp_path, 9333)
        assert loaded is not None
        assert loaded.pid == 123
        assert loaded.cdp_url == "http://127.0.0.1:9333"

    def test_corrupt_state_is_none_not_crash(self, tmp_path):
        path = chrome_cdp.state_path(tmp_path, 9333)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert chrome_cdp.load_state(tmp_path, 9333) is None

    def test_list_instances_empty_when_no_dir(self, tmp_path):
        assert chrome_cdp.list_instances(tmp_path) == []

    def test_list_reports_dead_tracked_instance(self, tmp_path):
        # A stale state file is exactly what turns a later attach into an
        # unexplained ECONNREFUSED, so it must stay visible.
        dead_port = chrome_cdp.free_port()
        chrome_cdp.save_state(
            tmp_path,
            chrome_cdp.CdpInstance(
                pid=999999,
                port=dead_port,
                user_data_dir=FIXTURE_DIR,
                started_at=1.0,
            ),
        )
        instances = chrome_cdp.list_instances(tmp_path)
        assert len(instances) == 1
        assert instances[0]["running"] is False
        assert instances[0]["port"] == dead_port


class TestFocus:
    def test_no_focus_by_default(self, tmp_path):
        assert focus.load(tmp_path) is None

    def test_save_then_load(self, tmp_path):
        focus.save(tmp_path, ["PROJ-24026", "PROJ-25931"])
        loaded = focus.load(tmp_path)
        assert loaded is not None
        assert loaded.features == ("PROJ-24026", "PROJ-25931")

    def test_clear_removes_it(self, tmp_path):
        focus.save(tmp_path, ["PROJ-24026"])
        assert focus.clear(tmp_path) is True
        assert focus.load(tmp_path) is None

    def test_clear_when_absent_is_false_not_error(self, tmp_path):
        assert focus.clear(tmp_path) is False

    def test_empty_list_reads_as_no_focus(self, tmp_path):
        # An empty focus must not mean "run nothing" — that would silently
        # turn a bare run into a no-op.
        focus.save(tmp_path, [])
        assert focus.load(tmp_path) is None

    def test_corrupt_focus_falls_back_to_none(self, tmp_path):
        path = focus.focus_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken")
        assert focus.load(tmp_path) is None

    def test_line_specs_are_preserved(self, tmp_path):
        focus.save(tmp_path, ["PROJ-25466:47"])
        loaded = focus.load(tmp_path)
        assert loaded is not None
        assert loaded.features == ("PROJ-25466:47",)


class TestStopPidReuseGuard:
    """`stop()` must not kill a PID that is no longer our Chrome.

    PIDs get recycled. A stale state file naming a long-dead Chrome could
    otherwise cause a `killpg` against an unrelated process group that
    inherited the number — killing someone else's processes outright.
    """

    def test_unverifiable_pid_is_not_killed(self, tmp_path, monkeypatch):
        killed = []
        monkeypatch.setattr(chrome_cdp.os, "killpg", lambda *a: killed.append(a))
        monkeypatch.setattr(chrome_cdp.os, "kill", lambda *a: killed.append(a))
        # A PID that exists but is NOT our chrome (ps returns something else).
        monkeypatch.setattr(
            chrome_cdp, "_looks_like_our_chrome", lambda instance: False
        )

        chrome_cdp.save_state(
            tmp_path,
            chrome_cdp.CdpInstance(
                pid=4242,
                port=9333,
                user_data_dir=FIXTURE_DIR,
                started_at=1.0,
            ),
        )
        chrome_cdp.stop(tmp_path, port=9333)
        assert killed == [], "must not signal a PID that is not our Chrome"

    def test_verified_pid_is_killed(self, tmp_path, monkeypatch):
        killed = []
        monkeypatch.setattr(chrome_cdp.os, "killpg", lambda *a: killed.append(a))
        monkeypatch.setattr(chrome_cdp.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(chrome_cdp, "_looks_like_our_chrome", lambda instance: True)

        chrome_cdp.save_state(
            tmp_path,
            chrome_cdp.CdpInstance(
                pid=4242,
                port=9333,
                user_data_dir=FIXTURE_DIR,
                started_at=1.0,
            ),
        )
        chrome_cdp.stop(tmp_path, port=9333)
        assert killed, "a verified Chrome PID should be signalled"


class TestLooksLikeOurChrome:
    def test_requires_matching_debug_port(self, monkeypatch):
        # Right binary, WRONG port => a different debug Chrome, not ours.
        monkeypatch.setattr(
            chrome_cdp.subprocess,
            "run",
            lambda *a, **kw: type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": "Google Chrome --remote-debugging-port=9999",
                },
            )(),
        )
        instance = chrome_cdp.CdpInstance(
            pid=1, port=9333, user_data_dir="", started_at=0.0
        )
        assert chrome_cdp._looks_like_our_chrome(instance) is False

    def test_matches_our_chrome(self, monkeypatch):
        monkeypatch.setattr(
            chrome_cdp.subprocess,
            "run",
            lambda *a, **kw: type(
                "P",
                (),
                {
                    "returncode": 0,
                    "stdout": "Google Chrome --remote-debugging-port=9333",
                },
            )(),
        )
        instance = chrome_cdp.CdpInstance(
            pid=1, port=9333, user_data_dir="", started_at=0.0
        )
        assert chrome_cdp._looks_like_our_chrome(instance) is True

    def test_dead_process_is_false(self, monkeypatch):
        monkeypatch.setattr(
            chrome_cdp.subprocess,
            "run",
            lambda *a, **kw: type("P", (), {"returncode": 1, "stdout": ""})(),
        )
        instance = chrome_cdp.CdpInstance(
            pid=1, port=9333, user_data_dir="", started_at=0.0
        )
        assert chrome_cdp._looks_like_our_chrome(instance) is False
