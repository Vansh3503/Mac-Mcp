"""The ``computer`` tool: screenshot-pixel coordinates mapped to screen points, every action, the
policy hit-test, the input lock, rate limit, pause and yield-to-user, and per-server state.

The demo's main display is 1440×900 points; screenshots are 1280×800 pixels, so a pixel is 1.125
points."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp_types import ImageContent

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.computer import SCROLL_LINES_PER_CLICK, parse_modifiers
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio

PT = 1.125
"""Points per screenshot pixel on the demo's main display."""


def make_ctx(*, engine: Any = None, **overrides: Any) -> ServerContext:
    ctx = make_test_context({"tools.profile": "computer", **overrides}, engine=engine)
    ctx.slept = []  # type: ignore[attr-defined]

    async def sleep(seconds: float) -> None:
        ctx.slept.append(seconds)  # type: ignore[attr-defined]
        await anyio.lowlevel.checkpoint()

    ctx.sleep = sleep
    return ctx


def engine_of(ctx: ServerContext) -> FakeEngine:
    engine = getattr(ctx.engine, "inner", ctx.engine)
    assert isinstance(engine, FakeEngine)
    return engine


@pytest.fixture
def ctx() -> ServerContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


async def act(client: Client, action: str, **args: Any) -> tuple[Any, str | None]:
    result = await client.call_tool("computer", {"action": action, **args})
    return result, error_code_of(result)


async def ok(client: Client, action: str, **args: Any) -> dict[str, Any]:
    result, error = await act(client, action, **args)
    assert error is None, text_of(result)
    return structured(result)


def events(ctx: ServerContext, kind: str) -> list[dict[str, Any]]:
    return engine_of(ctx).events_of(kind)


# =========================================================================== screenshot
async def test_the_profile_has_only_computer_and_status(client: Client) -> None:
    names = [tool.name for tool in (await client.list_tools()).tools]
    assert names == ["status", "computer"]


async def test_screenshot_returns_the_image_and_its_size(client: Client) -> None:
    result, error = await act(client, "screenshot")
    assert error is None
    data = structured(result)
    shot = data["screenshot"]
    assert (shot["width"], shot["height"]) == (1280, 800)
    assert shot["region"] == [0, 0, 1440, 900]
    assert any(isinstance(block, ImageContent) for block in result.content)
    assert shot["screenshot_id"] in data["message"]


async def test_coordinates_need_a_screenshot_first(ctx: ServerContext, client: Client) -> None:
    result, error = await act(client, "left_click", coordinate=[100, 100])
    assert error == "SNAPSHOT_NOT_FOUND"
    assert 'action="screenshot"' in text_of(result)
    assert events(ctx, "mouse_click") == []


async def test_screenshot_permission_errors() -> None:
    engine = FaultyEngine(
        FakeEngine.demo(), {"screenshot": Fault.error(ErrorCode.PERMISSION_SCREEN_RECORDING)}
    )
    async with connected_client(make_ctx(engine=engine)) as client:
        _, error = await act(client, "screenshot")
    assert error == "PERMISSION_SCREEN_RECORDING"


# =========================================================================== clicks
@pytest.mark.parametrize(
    ("action", "button", "count"),
    [
        ("left_click", "left", 1),
        ("right_click", "right", 1),
        ("middle_click", "middle", 1),
        ("double_click", "left", 2),
        ("triple_click", "left", 3),
    ],
)
async def test_clicks_map_pixels_to_points(
    ctx: ServerContext, client: Client, action: str, button: str, count: int
) -> None:
    await ok(client, "screenshot")
    data = await ok(client, action, coordinate=[400, 300])
    (click,) = events(ctx, "mouse_click")
    assert click["point"] == [400 * PT, 300 * PT]
    assert (click["button"], click["count"]) == (button, count)
    assert data["coordinate"] == [400, 300]
    assert data["point"] == [450.0, 337.5]
    assert data["app"]


async def test_modifier_keys_are_held_during_clicks(ctx: ServerContext, client: Client) -> None:
    await ok(client, "screenshot")
    await ok(client, "left_click", coordinate=[400, 300], text="cmd+shift")
    assert events(ctx, "mouse_click")[-1]["modifiers"] == ["cmd", "shift"]
    _, error = await act(client, "left_click", coordinate=[400, 300], text="cmd+q")
    assert error == "INVALID_ARGUMENT"


async def test_without_a_coordinate_clicks_happen_at_the_pointer(ctx: ServerContext, client: Client) -> None:
    await ok(client, "screenshot")
    await ok(client, "mouse_move", coordinate=[200, 200])
    await ok(client, "left_click")
    assert events(ctx, "mouse_click")[-1]["point"] == [200 * PT, 200 * PT]


@pytest.mark.parametrize("coordinate", [[1280, 10], [-1, 10], [10, 800]])
async def test_pixels_outside_the_screenshot(client: Client, coordinate: list[int]) -> None:
    await ok(client, "screenshot")
    _, error = await act(client, "left_click", coordinate=coordinate)
    assert error == "INVALID_ARGUMENT"


async def test_protected_apps_under_the_pointer_are_refused() -> None:
    ctx = make_ctx(**{"safety.deny_apps": ["com.apple.finder"]})
    async with connected_client(ctx) as client:
        await ok(client, "screenshot")
        _, error = await act(client, "left_click", coordinate=[200, 200])  # Finder's Downloads window
        assert error == "BLOCKED_BY_POLICY"
        _, error = await act(client, "scroll", coordinate=[200, 200], scroll_direction="down")
        assert error == "BLOCKED_BY_POLICY"
        _, error = await act(client, "left_click_drag", start_coordinate=[200, 200], coordinate=[1200, 700])
        assert error == "BLOCKED_BY_POLICY"
        await ok(client, "mouse_move", coordinate=[200, 200])  # hovering is not acting
    assert events(ctx, "mouse_click") == []
    assert events(ctx, "mouse_drag") == []


# =========================================================================== drags and presses
async def test_left_click_drag(ctx: ServerContext, client: Client) -> None:
    await ok(client, "screenshot")
    await ok(client, "left_click_drag", start_coordinate=[400, 300], coordinate=[600, 500])
    (drag,) = events(ctx, "mouse_drag")
    assert (drag["start"], drag["end"]) == ([450.0, 337.5], [675.0, 562.5])
    for args in ({"start_coordinate": [1, 1]}, {"coordinate": [1, 2, 3]}):
        _, error = await act(client, "left_click_drag", **args)
        assert error == "INVALID_ARGUMENT", args
    _, error = await act(client, "left_click", coordinate=[1, 1], start_coordinate=[2, 2])
    assert error == "INVALID_ARGUMENT"


async def test_mouse_down_and_up_become_a_click_or_a_drag(ctx: ServerContext, client: Client) -> None:
    await ok(client, "screenshot")
    _, error = await act(client, "left_mouse_up")
    assert error == "INVALID_ARGUMENT"
    await ok(client, "left_mouse_down", coordinate=[400, 300])
    await ok(client, "left_mouse_up")
    assert events(ctx, "mouse_click")[-1]["point"] == [450.0, 337.5]
    await ok(client, "left_mouse_down", coordinate=[400, 300])
    await ok(client, "mouse_move", coordinate=[500, 300])
    data = await ok(client, "left_mouse_up")
    (drag,) = events(ctx, "mouse_drag")
    assert (drag["start"], drag["end"]) == ([450.0, 337.5], [562.5, 337.5])
    assert data["message"].startswith("dragged")
    _, error = await act(client, "left_mouse_up")  # the press was used up
    assert error == "INVALID_ARGUMENT"


# =========================================================================== keyboard
async def test_type_and_key(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "type", text="Hello, world")
    assert events(ctx, "type_text")[-1]["text"] == "Hello, world"
    assert data["app"]
    await ok(client, "key", text="cmd+a")
    await ok(client, "key", text="Return")
    await ok(client, "key", text="ctrl+shift+Tab BackSpace Page_Down")  # xdotool spellings
    pressed = [(event["key"], event["modifiers"]) for event in events(ctx, "press_key")]
    assert pressed == [
        ("a", ["cmd"]),
        ("return", []),
        ("tab", ["ctrl", "shift"]),
        ("delete", []),
        ("page_down", []),
    ]
    for args in ({}, {"text": ""}):
        _, error = await act(client, "type", **args)
        assert error == "INVALID_ARGUMENT"
    _, error = await act(client, "key", text="cmd+nosuchkey")
    assert error == "INVALID_ARGUMENT"


async def test_keys_go_through_the_terminal_rule(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).app_control("activate", "Terminal")
    _, error = await act(client, "type", text="rm -rf ~")
    assert error == "BLOCKED_BY_POLICY"
    _, error = await act(client, "key", text="Return")
    assert error == "BLOCKED_BY_POLICY"
    assert events(ctx, "type_text") == []


async def test_secure_input_blocks_typing(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).secure_input = True
    _, error = await act(client, "type", text="secret")
    assert error == "SECURE_INPUT_ACTIVE"


# =========================================================================== scroll, wait, cursor
@pytest.mark.parametrize(
    ("direction", "dx", "dy"),
    [("down", 0, 1), ("up", 0, -1), ("right", 1, 0), ("left", -1, 0)],
)
async def test_scroll(ctx: ServerContext, client: Client, direction: str, dx: int, dy: int) -> None:
    await ok(client, "screenshot")
    await ok(client, "scroll", coordinate=[400, 300], scroll_direction=direction, scroll_amount=2)
    event = events(ctx, "scroll")[-1]
    lines = 2 * SCROLL_LINES_PER_CLICK
    assert (event["dx"], event["dy"]) == (dx * lines, dy * lines)
    assert event["point"] == [450.0, 337.5]


async def test_scroll_errors(client: Client) -> None:
    await ok(client, "screenshot")
    for args in ({"coordinate": [1, 1]}, {"coordinate": [1, 1], "scroll_direction": "down", "text": "shift"}):
        _, error = await act(client, "scroll", **args)
        assert error == "INVALID_ARGUMENT", args


async def test_wait(ctx: ServerContext, client: Client) -> None:
    await ok(client, "wait", duration=2.5)
    await ok(client, "wait")
    assert ctx.slept[-2:] == [2.5, 1.0]  # type: ignore[attr-defined]
    _, error = await act(client, "wait", duration=61)
    assert error == "INVALID_ARGUMENT"


async def test_cursor_position(client: Client) -> None:
    before = await ok(client, "cursor_position")
    assert before["coordinate"] is None  # no screenshot yet: points only
    assert before["point"] is not None
    await ok(client, "screenshot")
    await ok(client, "mouse_move", coordinate=[640, 400])
    data = await ok(client, "cursor_position")
    assert data["coordinate"] == [640, 400]
    assert "X=640, Y=400" in data["message"]


async def test_return_screenshot(ctx: ServerContext, client: Client) -> None:
    first = (await ok(client, "screenshot"))["screenshot"]["screenshot_id"]
    result, error = await act(client, "left_click", coordinate=[400, 300], return_screenshot=True)
    assert error is None
    data = structured(result)
    assert data["screenshot"]["screenshot_id"] != first
    assert any(isinstance(block, ImageContent) for block in result.content)
    assert ctx.slept  # type: ignore[attr-defined]  # settled before capturing


# =========================================================================== the safety rails
async def test_input_actions_are_rate_limited_but_screenshots_are_not() -> None:
    ctx = make_ctx(**{"safety.max_actions_per_minute": 2})
    async with connected_client(ctx) as client:
        await ok(client, "screenshot")
        await ok(client, "left_click", coordinate=[400, 300])
        await ok(client, "left_click", coordinate=[400, 300])
        _, error = await act(client, "left_click", coordinate=[400, 300])
        assert error == "RATE_LIMITED"
        await ok(client, "screenshot")
        await ok(client, "cursor_position")


async def test_the_kill_switch_stops_input(ctx: ServerContext, client: Client) -> None:
    await ok(client, "screenshot")
    ctx.control.pause("kill switch")
    for action, args in (("left_click", {"coordinate": [400, 300]}), ("type", {"text": "x"}), ("wait", {})):
        _, error = await act(client, action, **args)
        assert error == "PAUSED_BY_USER", action


async def test_input_yields_to_an_active_user(ctx: ServerContext, client: Client) -> None:
    await ok(client, "screenshot")
    engine_of(ctx).hid_idle = 0.1
    for action in ("left_click", "mouse_move", "left_mouse_down"):
        _, error = await act(client, action, coordinate=[400, 300])
        assert error == "USER_ACTIVE", action
    assert events(ctx, "mouse_move") == []  # the user's pointer was left alone
    await ok(client, "screenshot")  # looking is fine


async def test_each_server_has_its_own_screenshot(ctx: ServerContext) -> None:
    async with connected_client(ctx) as first, connected_client(ctx) as second:
        await ok(first, "screenshot")
        await ok(first, "left_click", coordinate=[10, 10])
        _, error = await act(second, "left_click", coordinate=[10, 10])
        assert error == "SNAPSHOT_NOT_FOUND"


async def test_engine_faults_surface() -> None:
    engine = FaultyEngine(FakeEngine.demo(), {"mouse_click": Fault.error(ErrorCode.TIMEOUT)})
    async with connected_client(make_ctx(engine=engine)) as client:
        await ok(client, "screenshot")
        _, error = await act(client, "left_click", coordinate=[400, 300])
    assert error == "TIMEOUT"


def test_parse_modifiers() -> None:
    assert parse_modifiers(None) == frozenset()
    assert parse_modifiers("Shift") == {"shift"}
    assert parse_modifiers("super+alt ctrl") == {"cmd", "option", "ctrl"}
    with pytest.raises(AXPilotError):
        parse_modifiers("shift+a")
