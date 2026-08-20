"""Guards for the failure mode where a green suite hides a broken command.

`debug start` once shipped crashing on every invocation because its test
replaced the exact boundary the bug lived on: the fake `chrome_cdp.launch`
returned a bare instance while the real one returns `(instance, reused)`, so the
fake and the call site agreed with each other and disagreed with reality.

The contract of the real `launch` is pinned here. The broader "don't reconstruct
behave, drive the real one" guard now lives in test_gate_runner.py (a genuine
behave process) and test_debug_gate.py (the debug commands driving it) — both
run real behave rather than a stub, so any divergence is a real bug.
"""

from __future__ import annotations

import inspect
import typing

from aitlc.core import chrome_cdp


class TestLaunchContract:
    """The shape `debug start` unpacks must be the shape `launch` returns."""

    def test_launch_returns_a_two_tuple(self):
        hints = typing.get_type_hints(chrome_cdp.launch)
        returned = hints["return"]
        assert typing.get_origin(returned) is tuple, returned
        args = typing.get_args(returned)
        assert len(args) == 2, f"launch returns {len(args)} values, not 2: {returned}"
        assert args[1] is bool

    def test_launch_accepts_the_keywords_the_debug_command_passes(self):
        parameters = inspect.signature(chrome_cdp.launch).parameters
        assert "port" in parameters
        assert parameters["port"].default is not inspect.Parameter.empty
        assert "window_size" in parameters
