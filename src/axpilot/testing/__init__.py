"""Testing helpers for AXPilot and for automation scripts built on it (public API).

* :func:`make_test_context` / :func:`connected_client` / :func:`call`: run tools in-process
  against the simulated desktop (:class:`FakeEngine`), with no real config, commands or
  home directory involved.
* :class:`FaultyEngine` + :class:`Fault`: make any engine method raise an
  :class:`~axpilot.errors.AXPilotError`, crash with a raw exception, hang past its deadline or
  fail only the first few times, to test error handling.

Example (pytest with the anyio plugin)::

    import pytest
    from axpilot.errors import ErrorCode
    from axpilot.testing import Fault, FaultyEngine, FakeEngine, call, make_test_context

    @pytest.mark.anyio
    async def test_snapshot_without_permission():
        fault = Fault.error(ErrorCode.PERMISSION_ACCESSIBILITY)
        engine = FaultyEngine(FakeEngine.demo(), {"capture_tree": fault})
        result, error = await call(make_test_context(engine=engine), "snapshot")
        assert error == "PERMISSION_ACCESSIBILITY"
"""

from __future__ import annotations

from axpilot.engine.fake import FakeEngine
from axpilot.testing.faulty_engine import ENGINE_METHODS, Fault, FaultKind, FaultyEngine
from axpilot.testing.harness import (
    call,
    connected_client,
    error_code_of,
    make_test_context,
    structured,
    text_of,
)

__all__ = [
    "ENGINE_METHODS",
    "FakeEngine",
    "Fault",
    "FaultKind",
    "FaultyEngine",
    "call",
    "connected_client",
    "error_code_of",
    "make_test_context",
    "structured",
    "text_of",
]
