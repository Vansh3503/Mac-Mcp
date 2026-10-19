"""Safety rules that span every tool: while AXPilot asks the user something, no tool may act (so an
agent can't answer its own confirmation with a parallel call), and the kill switch stops every tool
that changes something."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.system.runner import FakeRunner
from axpilot.testing import connected_client, make_test_context, structured

pytestmark = pytest.mark.anyio

ACTING_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("click", {"x": 100, "y": 100}),
    ("press_keys", {"keys": "return"}),
    ("type_text", {"text": "hi"}),
    ("scroll", {"direction": "down"}),
    ("app", {"action": "activate", "name": "Notes"}),
    ("window", {"action": "minimize"}),
    ("open", {"target": "https://example.com"}),
    ("clipboard", {"action": "set", "text": "x"}),
    ("computer", {"action": "left_click", "coordinate": [100, 100]}),
]


class HeldConfirmer:
    """Keeps its question open until ``answer`` is set."""

    def __init__(self) -> None:
        self.asked = anyio.Event()
        self.answered = anyio.Event()
        self.answer = False
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        self.asked.set()
        await self.answered.wait()
        return self.answer


def make_ctx(**options: Any) -> ServerContext:
    ctx = make_test_context(
        {"tools.profile": "full", "tools.enable": ["computer"], "safety.yield_to_user": False}, **options
    )

    async def no_sleep(_: float) -> None:
        await anyio.lowlevel.checkpoint()

    ctx.sleep = no_sleep
    return ctx


async def test_nothing_acts_while_a_confirmation_is_open() -> None:
    confirmer = HeldConfirmer()
    runner = FakeRunner()
    ctx = make_ctx(confirm=confirmer, runner=runner)
    codes: dict[str, str | None] = {}
    async with connected_client(ctx) as client:
        async with anyio.create_task_group() as tg:
            outcome: dict[str, Any] = {}

            async def run_shell() -> None:
                outcome["shell"] = await client.call_tool("shell", {"command": "echo hi"})

            tg.start_soon(run_shell)
            await confirmer.asked.wait()
            for tool, args in ACTING_CALLS:
                codes[tool] = error_code_of(await client.call_tool(tool, args))
            snapshot = await client.call_tool("snapshot", {"app": "Notes"})  # reading still works
            assert error_code_of(snapshot) is None, text_of(snapshot)
            confirmer.answered.set()
        assert error_code_of(outcome["shell"]) == "USER_DENIED"
        assert error_code_of(await client.call_tool("click", {"x": 100, "y": 100})) is None
    assert codes == dict.fromkeys(codes, "NEEDS_USER"), codes
    assert not runner.calls


async def test_the_kill_switch_stops_every_tool_that_changes_something() -> None:
    ctx = make_ctx(confirm=True)
    async with connected_client(ctx) as client:
        ctx.control.pause("kill switch")
        for tool, args in ACTING_CALLS:
            result = await client.call_tool(tool, args)
            assert error_code_of(result) == "PAUSED_BY_USER", (tool, text_of(result))
        status = structured(await client.call_tool("status", {}))
        assert status["control"]["paused"] is True
        assert error_code_of(await client.call_tool("snapshot", {"app": "Notes"})) is None
        ctx.control.resume()
        assert error_code_of(await client.call_tool("app", {"action": "activate", "name": "Notes"})) is None
