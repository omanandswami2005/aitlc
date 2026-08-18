"""Guards for the failure mode where a green suite hides a broken command.

Twice now a bug shipped *while its own test passed*, and both times the
reason was the same: the test replaced the exact boundary the bug lived on,
so the stub asserted the buggy shape rather than the real one.

- `debug start` crashed on every real invocation because the call site did
  `instance = launch(...)` against a function that returns
  `(instance, reused)`. The test's fake returned a bare instance, so the
  fake and the call site agreed with each other and disagreed with reality.
- Placeholders were typed into the browser literally (`<category>`) because
  nothing asserted a property of what actually crossed the dispatch
  boundary -- only that it *was* crossed.

So these tests deliberately do not stub aitlc's own functions. They pin the
contract of the real ones, and they assert properties of what gets
dispatched. A stub may lie about a return shape; `inspect.signature` on the
real object cannot.
"""

from __future__ import annotations

import inspect
import typing

from aitlc.core import chrome_cdp, debug_session


class TestLaunchContract:
    """The shape `debug start` unpacks must be the shape `launch` returns."""

    def test_launch_returns_a_two_tuple(self):
        """A fake returning a bare instance is not a faithful fake."""
        hints = typing.get_type_hints(chrome_cdp.launch)
        returned = hints["return"]
        assert typing.get_origin(returned) is tuple, returned
        args = typing.get_args(returned)
        assert len(args) == 2, f"launch returns {len(args)} values, not 2: {returned}"
        assert args[1] is bool

    def test_launch_accepts_the_keywords_the_debug_command_passes(self):
        """`port=None` is how an isolated instance is requested."""
        parameters = inspect.signature(chrome_cdp.launch).parameters
        assert "port" in parameters
        assert parameters["port"].default is not inspect.Parameter.empty


class TestNothingUnboundIsDispatched:
    """A placeholder that reaches the browser gets typed in as literal text."""

    FEATURE = """Feature: f

  @TEST_PROJ-1
  Scenario Outline: s
    Given open the app
    When search for "<term>"
    Then see "<count>" results

    Examples:
      | term  | count |
      | shoes | 12    |
"""

    def test_binding_an_example_leaves_no_placeholder_behind(self):
        steps = debug_session.feature_steps(self.FEATURE, example=0)
        unbound = [s for s in steps if "<" in s and ">" in s]
        assert not unbound, f"placeholders would be typed literally: {unbound}"

    def test_every_placeholder_is_actually_substituted(self):
        steps = debug_session.feature_steps(self.FEATURE, example=0)
        joined = " ".join(steps)
        assert "shoes" in joined and "12" in joined

    MISSING_COLUMN = """Feature: f

  @TEST_PROJ-1
  Scenario Outline: s
    Given open the app
    When search for "<term>" in "<category>"

    Examples:
      | term  |
      | shoes |
"""

    def test_a_missing_examples_column_is_refused_not_typed(self):
        """Silence here is what produced `<category>` in a real text box.

        The refusal has to be asserted on `feature_steps`, the entry point the
        debug command actually calls. `bind_example` is a substitution
        primitive and cannot know whether a leftover placeholder is an error --
        asserting it there passes while the real path stays broken, which is
        the exact mistake this file exists to prevent.
        """
        try:
            steps = debug_session.feature_steps(self.MISSING_COLUMN, example=0)
        except debug_session.ExampleBindingError as exc:
            assert "category" in str(exc)
        else:
            raise AssertionError(
                f"unbound placeholder would be typed literally: {steps}"
            )


class TestDebugStartAgainstTheRealLauncher:
    """`debug start` driven through the real `chrome_cdp.launch`.

    Only the OS boundaries are replaced -- the port probe, the Chrome lookup
    and the process spawn. Everything aitlc owns runs for real, so a call site
    that unpacks `launch`'s result wrongly fails here no matter what any
    hand-written fake claims. The version of this command that shipped broken
    could not have survived this test.
    """

    FEATURE = """Feature: f

  @TEST_PROJ-1
  Scenario: s
    Given open the app
    When click the button
    Then validate the result
"""

    def test_start_completes_through_the_real_launch(self, monkeypatch, tmp_path):
        import json

        from aitlc.commands import debug_cmd
        from typer.testing import CliRunner

        feature = tmp_path / "f.feature"
        feature.write_text(self.FEATURE)

        class _Cfg:
            root_dir = tmp_path
            scenario_setup = None
            step_dir = "features/steps"
            browser_actions = None
            browser_factory = None

            def resolve_feature_path(self, _test_id):
                return feature

        monkeypatch.setattr(
            debug_cmd.AitlcConfig, "find_and_load", staticmethod(lambda: _Cfg())
        )
        monkeypatch.setattr(debug_cmd, "load_dotenv", lambda *_a, **_k: True)

        # --- OS boundaries only, from here down ---
        probes = {"n": 0}

        def fake_probe(_port):
            # Nothing listening when launch checks; answering once "Chrome"
            # has been spawned, which is what drives the real wait loop.
            probes["n"] += 1
            return None if probes["n"] == 1 else {"Browser": "Chrome/1.2.3"}

        class _Proc:
            pid = 4321

            def poll(self):
                return None

        monkeypatch.setattr(chrome_cdp, "probe", fake_probe)
        monkeypatch.setattr(chrome_cdp, "find_chrome", lambda *_a, **_k: "/bin/true")
        monkeypatch.setattr(chrome_cdp.subprocess, "Popen", lambda *_a, **_k: _Proc())

        monkeypatch.setattr(
            debug_cmd,
            "_run_steps",
            lambda _c, _s, steps: {
                "results": [
                    {"step": s, "status": "passed", "duration_s": 0.0, "error": None}
                    for s in steps
                ],
                "stderr_tail": "",
                "unhandled_events": [],
            },
        )

        result = CliRunner().invoke(debug_cmd.app, ["start", "PROJ-1", "--at", "1"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # A real CdpInstance carries a real port -- proof the tuple was
        # unpacked rather than an object being formatted into the URL.
        assert payload["cdp_url"].startswith("http://127.0.0.1:")
        assert payload["cdp_url"].rsplit(":", 1)[1].isdigit()
        assert payload["parked_at"] == 1
