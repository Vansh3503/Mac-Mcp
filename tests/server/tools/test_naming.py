"""The ``windows-mcp`` naming: Windows-MCP's tool names and parameter shapes mapped onto AXPilot's
tools. Contract tests call every mapped tool with Windows-MCP-style arguments."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp_types import ImageContent

from axpilot.engine.fake import FakeEngine
from axpilot.models import Point
from axpilot.policy.confirm import ConfirmRequest
from axpilot.policy.profiles import TOOL_ORDER
from axpilot.server.app import load_tool_modules
from axpilot.server.context import ServerContext
from axpilot.server.instructions import instructions
from axpilot.server.naming import apply_naming, canonical_for
from axpilot.server.naming.windows_mcp import EXTRA_NAMES, NAMES, ref_of, rename_references
from axpilot.server.registry import REGISTRY
from axpilot.server.results import error_code_of, text_of
from axpilot.system.runner import FakeRunner
from axpilot.testing import connected_client, make_test_context, structured

pytestmark = pytest.mark.anyio

WINDOWS_MCP_PARAMETERS: dict[str, set[str]] = {
    "Click": {"loc", "label", "button", "clicks"},
    "Type": {"text", "loc", "label", "clear", "caret_position", "press_enter"},
    "Scroll": {"loc", "label", "type", "direction", "wheel_times"},
    "Move": {"loc", "label", "drag", "from_loc", "duration"},
    "Shortcut": {"shortcut"},
    "Wait": {"duration"},
    "WaitFor": {"condition", "text", "window_name", "timeout", "interval", "use_dom"},
    "MultiSelect": {"locs", "labels", "press_ctrl"},
    "MultiEdit": {"locs", "labels"},
    "Screenshot": {"use_annotation", "width_reference_line", "height_reference_line", "display", "region"},
    "Snapshot": {
        "use_vision",
        "use_dom",
        "use_annotation",
        "use_ui_tree",
        "width_reference_line",
        "height_reference_line",
        "display",
        "region",
    },
    "App": {"mode", "name", "window_loc", "window_size", "executable", "args", "cwd"},
    "Process": {"mode", "name", "pid", "sort_by", "limit", "force"},
    "Clipboard": {"mode", "text"},
    "Notification": {"title", "message", "app_id"},
    "ControlStatus": set(),
    "DisplayInventory": set(),
    "Shell": {"command", "timeout"},
    "Defaults": {"mode", "path", "name", "value", "type"},
    "Scrape": {"url", "query", "use_dom", "use_sampling"},
}
"""Windows-MCP's parameters per tool (PowerShell → Shell, Registry → Defaults)."""


class Recorder:
    def __init__(self) -> None:
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return True


def make_ctx(**overrides: Any) -> ServerContext:
    ctx = make_test_context({"tools.profile": "full", **overrides}, confirm=Recorder())

    async def no_sleep(_: float) -> None:
        await anyio.lowlevel.checkpoint()

    ctx.sleep = no_sleep
    return ctx


def engine_of(ctx: ServerContext) -> FakeEngine:
    assert isinstance(ctx.engine, FakeEngine)
    return ctx.engine


def runner_of(ctx: ServerContext) -> FakeRunner:
    assert isinstance(ctx.runner, FakeRunner)
    return ctx.runner


@pytest.fixture
def ctx() -> ServerContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx, naming="windows-mcp") as connected:
        yield connected


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args or {})
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


def events(ctx: ServerContext, kind: str) -> list[dict[str, Any]]:
    return engine_of(ctx).events_of(kind)


async def ref_named(client: Client, name: str, app: str = "Notes") -> str:
    snap = await ok(client, "Snapshot", {"app": app})
    return next(el["ref"] for el in snap["elements"] if el["name"] == name)


# =========================================================================== the surface
def test_every_tool_has_a_windows_mcp_name() -> None:
    load_tool_modules()
    assert set(NAMES) == set(TOOL_ORDER)
    publics = [*NAMES.values(), *EXTRA_NAMES]
    assert len(publics) == len(set(publics))
    for name, spec in REGISTRY.items():
        if spec.windows_mcp_name is not None:
            assert spec.windows_mcp_name == NAMES[name], name
    for public in publics:
        assert canonical_for(public, "windows-mcp") in TOOL_ORDER
    assert canonical_for("Nope", "windows-mcp") is None
    assert canonical_for("click", "default") == "click"


async def test_the_windows_mcp_tool_list(client: Client) -> None:
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(WINDOWS_MCP_PARAMETERS) <= set(tools)
    assert not set(tools) & set(TOOL_ORDER)  # no default names leak through
    assert "Drag" not in tools  # Move covers dragging
    for name, expected in WINDOWS_MCP_PARAMETERS.items():
        params = set(tools[name].input_schema.get("properties", {}))
        assert expected <= params, (name, expected - params)
        assert tools[name].output_schema is not None, name
        assert tools[name].description, name
    for name in ("Find", "SelectMenu", "SetValue", "AppleScript", "BrowserTabs", "Explore", "Read"):
        assert name in tools
    assert "`snapshot`" not in (tools["Find"].description or "")


async def test_drag_is_exposed_when_move_is_disabled() -> None:
    ctx = make_ctx(**{"tools.disable": ["move_mouse"]})
    async with connected_client(ctx, naming="windows-mcp") as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "Drag" in names
    assert "Move" not in names


async def test_instructions_follow_the_naming(client: Client) -> None:
    text = instructions("windows-mcp")
    assert "`Snapshot`" in text
    assert "`snapshot`" not in text
    assert "label=12" in text
    assert "coordinate_space" not in text
    assert client.instructions == text
    assert rename_references("use `find(scroll=true)` or `nope`") == "use `Find(scroll=true)` or `nope`"


async def test_disabled_tools_keep_their_windows_mcp_error() -> None:
    async with connected_client(make_test_context(), naming="windows-mcp") as client:
        _, error = await call(client, "Shell", {"command": "ls"})
        assert error == "TOOL_DISABLED"
        _, error = await call(client, "Drag", {})
        assert error == "NOT_SUPPORTED"
        _, error = await call(client, "Nope", {})
        assert error == "INVALID_ARGUMENT"


def test_default_naming_is_unchanged() -> None:
    load_tool_modules()
    specs = [REGISTRY["click"], REGISTRY["snapshot"]]
    assert apply_naming(specs, "default") == [("click", specs[0], None), ("snapshot", specs[1], None)]
    with pytest.raises(Exception, match="Unknown tool naming"):
        apply_naming(specs, "klingon")


@pytest.mark.parametrize(
    ("label", "ref"),
    [(12, "e12"), ("12", "e12"), ("e4", "e4"), ("o3", "o3"), ("s_7f3a:e12", "s_7f3a:e12"), (None, None)],
)
def test_labels_are_refs(label: Any, ref: str | None) -> None:
    assert ref_of(label) == ref


@pytest.mark.parametrize("label", ["button", "e", "12a", True])
def test_bad_labels(label: Any) -> None:
    with pytest.raises(Exception, match="label"):
        ref_of(label)


# =========================================================================== input
async def test_click(ctx: ServerContext, client: Client) -> None:
    new_note = await ref_named(client, "New Note")
    data = await ok(client, "Click", {"label": int(new_note[1:])})
    assert data["strategy"].startswith("ax:")
    await ok(client, "Click", {"loc": [200, 200], "button": "right"})
    await ok(client, "Click", {"loc": "[210, 210]", "clicks": 2})
    clicks = events(ctx, "mouse_click")
    assert (clicks[-2]["point"], clicks[-2]["button"]) == ([200.0, 200.0], "right")
    assert (clicks[-1]["point"], clicks[-1]["count"]) == ([210.0, 210.0], 2)
    for args in ({}, {"loc": [1, 2], "label": 3}, {"loc": [1, 2, 3]}, {"loc": "nonsense"}, {"label": "x"}):
        _, error = await call(client, "Click", args)
        assert error == "INVALID_ARGUMENT", args


async def test_type(ctx: ServerContext, client: Client) -> None:
    await ok(client, "Type", {"text": "hello", "loc": [700, 400]})
    assert events(ctx, "mouse_click")[-1]["point"] == [700.0, 400.0]
    search = await ref_named(client, "Search")
    await ok(client, "Type", {"text": "groceries", "label": search, "clear": True, "press_enter": True})
    await ok(client, "Type", {"text": "!", "label": int(search[1:]), "caret_position": "end"})
    pressed = [(event["key"], event["modifiers"]) for event in events(ctx, "press_key")]
    assert ("down", ["cmd"]) in pressed  # caret to the end before typing
    _, error = await call(client, "Type", {"text": "x", "caret_position": "middle"})
    assert error == "INVALID_ARGUMENT"


async def test_scroll(ctx: ServerContext, client: Client) -> None:
    await ok(client, "Scroll", {"loc": [700, 500], "direction": "down", "wheel_times": 2})
    down = events(ctx, "scroll")
    assert down
    assert down[-1]["dy"] > 0
    await ok(client, "Scroll", {"loc": [700, 500], "type": "horizontal", "direction": "down"})
    assert events(ctx, "scroll")[-1]["dx"] > 0  # horizontal "down" scrolls right
    engine_of(ctx).mouse_move(Point(700, 500))
    await ok(client, "Scroll", {"direction": "up"})  # at the pointer
    assert events(ctx, "scroll")[-1]["dy"] < 0


async def test_move_and_drag(ctx: ServerContext, client: Client) -> None:
    await ok(client, "Move", {"loc": [500, 500]})
    assert events(ctx, "mouse_move")[-1]["point"] == [500.0, 500.0]
    await ok(client, "Move", {"loc": [600, 500], "drag": True, "from_loc": [500, 520], "duration": 0.5})
    drag = events(ctx, "mouse_drag")[-1]
    assert (drag["start"], drag["end"], drag["duration_s"]) == ([500.0, 520.0], [600.0, 500.0], 0.5)
    await ok(client, "Move", {"loc": [650, 500], "drag": True})  # from the pointer
    assert events(ctx, "mouse_drag")[-1]["start"] == [600.0, 500.0]
    _, error = await call(client, "Move", {"loc": [1, 1], "from_loc": [2, 2]})
    assert error == "INVALID_ARGUMENT"


async def test_dragging_needs_the_drag_tool() -> None:
    ctx = make_ctx(**{"tools.disable": ["drag"]})
    async with connected_client(ctx, naming="windows-mcp") as client:
        await ok(client, "Move", {"loc": [500, 500]})
        _, error = await call(client, "Move", {"loc": [600, 500], "drag": True})
    assert error == "TOOL_DISABLED"


async def test_shortcut(ctx: ServerContext, client: Client) -> None:
    await ok(client, "Shortcut", {"shortcut": "cmd+shift+n"})
    await ok(client, "Shortcut", {"shortcut": "win+space"})
    pressed = [(e["key"], e["modifiers"]) for e in events(ctx, "press_key")]
    assert pressed == [("n", ["cmd", "shift"]), ("space", ["cmd"])]


async def test_multi_select(ctx: ServerContext, client: Client) -> None:
    await ok(client, "MultiSelect", {"locs": [[200, 200], [250, 250]]})
    clicks = events(ctx, "mouse_click")[-2:]
    assert [c["modifiers"] for c in clicks] == [["cmd"], ["cmd"]]
    await ok(client, "MultiSelect", {"locs": "[[200, 200]]", "press_ctrl": False})
    assert events(ctx, "mouse_click")[-1]["modifiers"] == []
    for args in ({}, {"locs": [], "labels": [1]}, {"locs": []}):
        _, error = await call(client, "MultiSelect", args)
        assert error == "INVALID_ARGUMENT", args


async def test_multi_edit(ctx: ServerContext, client: Client) -> None:
    search = await ref_named(client, "Search")
    await ok(client, "MultiEdit", {"labels": [[int(search[1:]), "milk"]]})
    snap = await ok(client, "Snapshot", {"app": "Notes"})
    assert any(el["name"] == "Search" and el.get("value") == "milk" for el in snap["elements"])
    await ok(client, "MultiEdit", {"locs": [[700, 400, "bread"]]})
    assert events(ctx, "mouse_click")[-1]["point"] == [700.0, 400.0]
    for args in ({}, {"labels": [[1]]}, {"locs": [[1, 2]]}, {"labels": "[[1, 2]]"}):
        _, error = await call(client, "MultiEdit", args)
        assert error == "INVALID_ARGUMENT", args


async def test_wait_and_wait_for(client: Client) -> None:
    await ok(client, "Wait", {"duration": 0})
    _, error = await call(client, "Wait", {"duration": 61})
    assert error == "INVALID_ARGUMENT"
    await ok(client, "App", {"mode": "switch", "name": "Notes"})  # WaitFor looks at the frontmost app
    data = await ok(client, "WaitFor", {"condition": "active_window", "window_name": "Notes", "timeout": 1})
    assert data["satisfied"] is True
    await ok(client, "WaitFor", {"condition": "element_exists", "text": "New Note", "timeout": 1})
    await ok(client, "WaitFor", {"condition": "text_exists", "text": "New Note", "timeout": 1})
    _, error = await call(client, "WaitFor", {"condition": "element_exists"})
    assert error == "INVALID_ARGUMENT"


# =========================================================================== observe
async def test_screenshot(client: Client) -> None:
    result, error = await call(client, "Screenshot", {"display": [1]})
    assert error is None
    assert structured(result)["display_id"] == 2
    assert any(isinstance(block, ImageContent) for block in result.content)
    region = await ok(client, "Screenshot", {"region": [0, 0, 400, 300]})
    assert region["region"] == [0, 0, 400, 300]
    _, error = await call(client, "Screenshot", {"display": [9]})
    assert error == "DISPLAY_NOT_FOUND"
    for args in ({"display": [0, 1]}, {"region": [10, 10, 5, 5]}, {"region": "[1, 2]"}):
        _, error = await call(client, "Screenshot", args)
        assert error == "INVALID_ARGUMENT", args


async def test_snapshot(client: Client) -> None:
    plain, _ = await call(client, "Snapshot", {"app": "Notes"})
    assert not any(isinstance(block, ImageContent) for block in plain.content)
    vision, error = await call(client, "Snapshot", {"use_vision": True, "use_dom": True, "display": [0]})
    assert error is None
    assert any(isinstance(block, ImageContent) for block in vision.content)


async def test_inventory_and_status(client: Client) -> None:
    displays = await ok(client, "DisplayInventory")
    assert len(displays["displays"]) == 2
    status = await ok(client, "ControlStatus")
    assert status["naming"] == "windows-mcp"


# =========================================================================== apps and system
async def test_app(ctx: ServerContext, client: Client) -> None:
    launched = await ok(client, "App", {"name": "Calculator"})
    assert launched["action"] == "launch"
    switched = await ok(client, "App", {"mode": "switch", "name": "Notes"})
    assert switched["action"] == "activate"
    resized = await ok(
        client, "App", {"mode": "resize", "name": "Notes", "window_loc": [10, 30], "window_size": [800, 600]}
    )
    assert resized["window"]["frame"] == [10, 30, 800, 600]
    await ok(
        client, "App", {"mode": "launch_executable", "executable": "/System/Applications/Calculator.app"}
    )
    result, error = await call(
        client, "App", {"mode": "launch_executable", "executable": "/x.app", "args": ["-v"]}
    )
    assert error == "NOT_SUPPORTED", text_of(result)
    for args in (
        {"mode": "switch"},
        {"mode": "resize", "name": "Notes"},
        {"mode": "launch", "name": "Notes", "cwd": "/tmp"},
        {"mode": "launch_executable", "name": "Notes"},
        {"mode": "resize", "name": "Notes", "window_size": [1]},
    ):
        _, error = await call(client, "App", args)
        assert error == "INVALID_ARGUMENT", args


async def test_process(ctx: ServerContext, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    runner_of(ctx).register(
        ["ps"],
        stdout="  812     1   3.5  1.2 /System/Applications/Notes.app/Contents/MacOS/Notes\n"
        " 4321     1   1.0  2.0 /Applications/Helper.app/Contents/MacOS/Helper\n"
        " 4322     1   1.0  2.0 /Applications/Helper.app/Contents/MacOS/Helper\n",
    )
    listed = await ok(client, "Process", {"mode": "list", "sort_by": "cpu", "limit": 5})
    assert listed["count"] == 3
    _, error = await call(client, "Process", {"mode": "kill", "name": "Helper"})
    assert error == "INVALID_ARGUMENT"  # two match: pass the pid
    _, error = await call(client, "Process", {"mode": "kill", "name": "Nothing"})
    assert error == "INVALID_ARGUMENT"
    _, error = await call(client, "Process", {"mode": "kill"})
    assert error == "INVALID_ARGUMENT"
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("axpilot.system.processes.os.kill", lambda pid, sig: signals.append((pid, sig)))
    killed = await ok(client, "Process", {"mode": "kill", "name": "Notes"})
    assert killed["pid"] == 812
    assert signals[-1][0] == 812


async def test_clipboard_and_notification(ctx: ServerContext, client: Client) -> None:
    await ok(client, "Clipboard", {"mode": "set", "text": "copied"})
    assert (await ok(client, "Clipboard", {"mode": "get"}))["text"] == "copied"
    await ok(client, "Notification", {"title": "Done", "message": "All good", "app_id": "Windows.App"})
    assert events(ctx, "notify")[-1]["title"] == "Done"


async def test_shell(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(["/bin/zsh", "-lc"], stdout="hi\n")
    data = await ok(client, "Shell", {"command": "echo hi", "timeout": 100})
    assert data["stdout"] == "hi\n"
    assert runner_of(ctx).last_call.timeout_s == 30  # capped at [scripting] shell_timeout_s


async def test_defaults(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(["defaults", "read"], stdout="1\n")
    runner_of(ctx).register(["defaults", "write"])
    assert (await ok(client, "Defaults", {"mode": "get", "path": "com.apple.dock", "name": "autohide"}))[
        "value"
    ] == "1"
    await ok(client, "Defaults", {"mode": "list", "path": "com.apple.dock"})
    assert runner_of(ctx).last_call.argv == ["defaults", "read", "com.apple.dock"]
    await ok(client, "Defaults", {"mode": "set", "path": "com.apple.dock", "name": "tilesize", "value": "48",
                                  "type": "DWord"})  # fmt: skip
    assert runner_of(ctx).last_call.argv[-2:] == ["-int", "48"]
    for args, code in (
        ({"mode": "delete", "path": "com.apple.dock", "name": "x"}, "NOT_SUPPORTED"),
        ({"mode": "get", "path": "com.apple.dock"}, "INVALID_ARGUMENT"),
        ({"mode": "set", "path": "com.apple.dock", "name": "x"}, "INVALID_ARGUMENT"),
        (
            {"mode": "set", "path": "com.apple.dock", "name": "x", "value": "1", "type": "Binary"},
            "INVALID_ARGUMENT",
        ),
    ):
        _, error = await call(client, "Defaults", args)
        assert error == code, args


async def test_scrape(client: Client) -> None:
    await ok(client, "App", {"name": "Safari"})
    data = await ok(client, "Scrape", {})
    assert "Favorites" in data["text"]
    await ok(
        client, "Scrape", {"url": "https://example.com", "query": "x", "use_sampling": True}
    )  # page URL unknown


async def test_audit_records_use_canonical_names(ctx: ServerContext, client: Client) -> None:
    await ok(client, "ControlStatus")
    await ok(client, "Shortcut", {"shortcut": "cmd+a"})
    assert [record["tool"] for record in ctx.audit.tail(10)] == ["status", "press_keys"]
