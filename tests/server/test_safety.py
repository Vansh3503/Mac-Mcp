"""Safety rules that span every tool: while AXPilot asks the user something, no tool may act (so an
agent can't answer its own confirmation with a parallel call), and the kill switch stops every tool
that changes something."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.models import Point, Rect
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
    outcome: dict[str, Any] = {}
    async with connected_client(ctx) as client:
        async with anyio.create_task_group() as tg:

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


async def test_the_audit_log_keeps_typed_text_out_in_both_namings() -> None:
    ctx = make_ctx(confirm=True)
    async with connected_client(ctx) as client:
        await client.call_tool("app", {"action": "activate", "name": "Notes"})
        snap = structured(await client.call_tool("snapshot", {"app": "Notes"}))
        field = next(el["ref"] for el in snap["elements"] if el["role"] in ("field", "textarea"))
        await client.call_tool("fill_form", {"fields": [{"ref": field, "value": "SECRET-1"}]})
        await client.call_tool("press_keys", {"keys": "S E C R E T"})
    async with connected_client(ctx, naming="windows-mcp") as client:
        await client.call_tool("MultiEdit", {"labels": [[field, "SECRET-2"]]})
        await client.call_tool("Shortcut", {"shortcut": "S"})
    records = ctx.audit.tail(20)
    assert "SECRET" not in json.dumps(records)
    filled = [record["args"] for record in records if record["tool"] == "fill_form"]
    assert filled == [
        {"fields": [{"ref": field, "value": "<redacted 8 chars>"}], "snapshot_id": None, "report": "diff"},
        {"labels": [["<redacted 3 chars>", "<redacted 8 chars>"]], "locs": None},  # MultiEdit
    ]


def cover_with_1password(ctx: ServerContext, point: list[float]) -> None:
    """Move 1Password (deny-listed) over ``point`` and bring it to the front."""
    engine = ctx.engine
    assert isinstance(engine, FakeEngine)
    window = next(w for w in engine.list_windows() if w.app == "1Password")
    engine.window_control(window.window_id, "move", Rect(point[0] - 100, point[1] - 100, 0, 0))
    engine.app_control("activate", "1Password")
    under = engine.app_at_point(Point(point[0], point[1]))
    assert under is not None
    assert under.name == "1Password"


async def test_drops_and_scrolls_never_land_on_a_covering_protected_app() -> None:
    ctx = make_ctx(confirm=True)
    engine = ctx.engine
    assert isinstance(engine, FakeEngine)
    async with connected_client(ctx) as client:
        elements = structured(await client.call_tool("snapshot", {"app": "Notes", "mode": "full"}))[
            "elements"
        ]
        search = next(el for el in elements if el["name"] == "Search")
        scroller = next(el for el in elements if el["role"] in ("list", "scrollarea", "textarea"))
        cover_with_1password(ctx, search["center"])
        start = next(
            (x, y)
            for x, y in ((130, 120), (200, 200), (300, 300))
            if (app := engine.app_at_point(Point(x, y))) is not None and app.name != "1Password"
        )
        result = await client.call_tool(
            "drag", {"from_x": start[0], "from_y": start[1], "to_ref": search["ref"]}
        )
        assert error_code_of(result) == "BLOCKED_BY_POLICY", text_of(result)
        assert not engine.events_of("mouse_drag")

        cover_with_1password(ctx, scroller["center"])
        result = await client.call_tool("scroll", {"ref": scroller["ref"], "direction": "down"})
        assert error_code_of(result) is None, text_of(result)  # Notes was brought forward first
    assert engine.events_of("scroll")
    for scroll in engine.events_of("scroll"):
        under = engine.app_at_point(Point(*scroll["point"]))
        assert under is not None
        assert under.name != "1Password"
