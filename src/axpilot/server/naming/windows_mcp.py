"""The ``windows-mcp`` naming: AXPilot's tools under Windows-MCP's names and parameter shapes.

Agents and prompts written for Windows-MCP (github.com/CursorTouch/Windows-MCP) can drive a Mac
without changes to their tool calls: ``Click(loc=[x, y])``, ``Type(text, label=12)``,
``Shortcut("cmd+c")``, ``App(mode="switch", name="Notes")``… Each Windows-MCP tool is an adapter
around one AXPilot tool (``Type`` with ``loc`` clicks first, ``Move`` with ``drag=true`` drags,
``MultiSelect`` cmd-clicks each item); the AXPilot tool's policy, confirmations, audit and errors
apply unchanged, and results keep AXPilot's structured content. Tools without a Windows-MCP
counterpart keep their parameters under a PascalCase name. Each server exposes one naming.

Conventions that differ from Windows:

* ``loc`` is ``[x, y]`` in macOS screen points (what snapshots report), not physical pixels.
* ``label`` is the number of a snapshot ref: ``label=12`` is ref ``e12``; a string such as
  ``"o3"`` or ``"s_7f3a:e12"`` is used as the ref itself.
* Shortcuts use macOS modifiers: ``cmd+c`` copies (``ctrl+c`` is Control-C); ``win`` means ``cmd``.

=======================  ======================  ===============================================
Windows-MCP name          AXPilot tool            Parameters
=======================  ======================  ===============================================
Click                     click                   loc | label, button, clicks (1-3)
Type                      type_text (+ click)     text, loc | label, clear, caret_position,
                                                  press_enter
Scroll                    scroll                  loc | label (default: the pointer), type,
                                                  direction, wheel_times (3 lines each)
Move                      move_mouse / drag       loc | label, drag, from_loc, duration (seconds)
Shortcut                  press_keys              shortcut ("cmd+shift+t", "cmd+a delete")
Wait                      wait_for (duration)     duration (whole seconds, at most 60)
WaitFor                   wait_for                condition (text_exists | active_window |
                                                  element_exists | element_enabled |
                                                  focused_element), text, window_name, timeout
MultiSelect               click (cmd-click)       locs | labels, press_ctrl (holds cmd)
MultiEdit                 fill_form / type_text   labels [[label, text]] | locs [[x, y, text]]
Screenshot                screenshot              display ([index]), region [l, t, r, b]
Snapshot                  snapshot                use_vision, use_annotation, app
DisplayInventory          list_displays           —
App                       app / window            mode (launch | launch_executable | resize |
                                                  switch), name, window_loc, window_size,
                                                  executable
Process                   processes               mode (list | kill), name, pid, limit, force
Clipboard                 clipboard               mode (get | set), text
Notification              notify                  title, message, app_id (ignored)
ControlStatus             status                  —
Shell                     shell                   command, timeout (PowerShell → zsh)
Defaults                  defaults                mode (get | set | list), path (domain), name
                                                  (key), value, type (Registry → defaults)
Scrape                    browser_read            url (must be the page shown), use_dom,
                                                  query and use_sampling (ignored)
Find, Explore, Read,      find, explore, read,    AXPilot's parameters (Mac-only tools)
Inspect, OCR, ListApps,   inspect, ocr,
ListWindows, SetValue,    list_apps, list_windows,
PerformAction,            set_value,
SelectMenu, Window,       perform_action,
Open, Spotlight,          select_menu, window,
SystemInfo,               open, spotlight_search,
SystemControl,            system_info,
AppleScript, Shortcuts,   system_control,
BrowserTabs,              applescript, shortcuts,
BrowserEvalJS, Computer   browser_tabs,
                          browser_eval_js, computer
Drag                      drag                    only when move_mouse is disabled (else Move)
=======================  ======================  ===============================================

Windows-MCP's ``FileSystem`` has no counterpart: use a filesystem MCP server, ``Spotlight`` or
``Shell``. ``Registry`` delete is not supported (``defaults delete`` is not exposed).
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import dataclasses
import json
import re
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from mcp_types import CallToolResult
from pydantic import Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.context import ServerContext
from axpilot.server.registry import REGISTRY, ToolFn, ToolSpec
from axpilot.server.schemas import AppModel, OutputModel, WindowModel
from axpilot.server.tools.apps import AppActionArg, WindowActionArg
from axpilot.server.tools.input import FieldValue

__all__ = ["NAMES", "Adapter", "apply_windows_mcp", "canonical_name", "public_name", "rename_references"]

Adapter = Callable[[ToolFn, ServerContext], ToolFn]
"""Turns an AXPilot tool function into one with the Windows-MCP parameter shape."""

NAMES: dict[str, str] = {
    "snapshot": "Snapshot",
    "screenshot": "Screenshot",
    "find": "Find",
    "explore": "Explore",
    "read": "Read",
    "inspect": "Inspect",
    "ocr": "OCR",
    "list_apps": "ListApps",
    "list_windows": "ListWindows",
    "list_displays": "DisplayInventory",
    "wait_for": "WaitFor",
    "status": "ControlStatus",
    "click": "Click",
    "type_text": "Type",
    "press_keys": "Shortcut",
    "scroll": "Scroll",
    "drag": "Drag",
    "move_mouse": "Move",
    "set_value": "SetValue",
    "perform_action": "PerformAction",
    "select_menu": "SelectMenu",
    "fill_form": "MultiEdit",
    "app": "App",
    "window": "Window",
    "open": "Open",
    "clipboard": "Clipboard",
    "notify": "Notification",
    "spotlight_search": "Spotlight",
    "system_info": "SystemInfo",
    "system_control": "SystemControl",
    "processes": "Process",
    "shell": "Shell",
    "applescript": "AppleScript",
    "shortcuts": "Shortcuts",
    "defaults": "Defaults",
    "browser_tabs": "BrowserTabs",
    "browser_read": "Scrape",
    "browser_eval_js": "BrowserEvalJS",
    "computer": "Computer",
}
"""Canonical tool name → its primary name under the ``windows-mcp`` naming."""

EXTRA_NAMES: dict[str, str] = {"MultiSelect": "click", "Wait": "wait_for"}
"""Further Windows-MCP tools built on an AXPilot tool that already has a primary name."""

_CANONICAL: dict[str, str] = {**{public: name for name, public in NAMES.items()}, **EXTRA_NAMES}

WAIT_MAX_S = 60
LINES_PER_WHEEL = 3
_REF_RE = re.compile(r"^(?:s_[0-9a-z]+:)?[a-z]\d+$")
_BACKTICKED_RE = re.compile(r"`([a-z_]+)(?=[`(])")


def public_name(canonical: str) -> str:
    """The ``windows-mcp`` name of a canonical tool."""
    return NAMES[canonical]


def canonical_name(public: str) -> str | None:
    """The canonical tool behind a ``windows-mcp`` name, or ``None``."""
    return _CANONICAL.get(public)


def rename_references(text: str) -> str:
    """Replace backticked canonical tool names (```snapshot```, ```find(scroll=true)```) with their
    ``windows-mcp`` names."""
    return _BACKTICKED_RE.sub(lambda m: f"`{NAMES.get(m.group(1), m.group(1))}", text)


# =========================================================================== argument helpers
def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def _parsed(value: Any, what: str) -> Any:
    """``value``, or the JSON it holds when it is a string (Windows-MCP accepts both)."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError as exc:
        raise _invalid(f"{what} must be a JSON list, got {value!r}.") from exc


def _point(loc: Any, what: str = "loc") -> tuple[float, float] | None:
    """``[x, y]`` (a list or a JSON string) → ``(x, y)``."""
    if loc is None:
        return None
    value = _parsed(loc, what)
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
    ):
        raise _invalid(f"{what} must be [x, y], got {loc!r}.", hint="Coordinates are macOS screen points.")
    return float(value[0]), float(value[1])


def ref_of(label: int | str | None) -> str | None:
    """A Windows-MCP label as an AXPilot ref: ``12`` → ``"e12"``; refs pass through."""
    if label is None:
        return None
    if isinstance(label, bool):
        raise _invalid("label must be a number or a ref.")
    if isinstance(label, int):
        return f"e{label}"
    text = label.strip()
    if text.isdigit():
        return f"e{int(text)}"
    if not _REF_RE.match(text.lower()):
        raise _invalid(
            f"label {label!r} is neither a number nor a snapshot ref.", hint="Use label=12 for ref e12."
        )
    return text


def _target(loc: Any, label: Any, *, required: bool = True) -> dict[str, Any]:
    point, ref = _point(loc), ref_of(label)
    if point is not None and ref is not None:
        raise _invalid("Pass loc or label, not both.")
    if point is not None:
        return {"x": point[0], "y": point[1]}
    if ref is not None:
        return {"ref": ref}
    if required:
        raise _invalid("Pass loc=[x, y] or label (the number of a snapshot ref).")
    return {}


def _require_tool(ctx: ServerContext, canonical: str) -> None:
    """Adapters that also use another tool need that tool to be enabled."""
    if canonical not in ctx.enabled:
        ctx.policy.check_tool(canonical)


def _inner(ctx: ServerContext, canonical: str) -> ToolFn:
    """The (unguarded) function of another tool, for adapters that combine tools. It runs inside
    the calling tool's guard: same confirmation, input turn and audit record."""
    _require_tool(ctx, canonical)
    return REGISTRY[canonical].factory(ctx)


def _returning(fn: ToolFn) -> Callable[[ToolFn], ToolFn]:
    """Give the adapter ``fn``'s return annotation, so the output schema stays AXPilot's."""
    hint = typing.get_type_hints(fn, include_extras=True).get("return", CallToolResult)

    def apply(adapter: ToolFn) -> ToolFn:
        adapter.__annotations__["return"] = hint
        return adapter

    return apply


async def _cursor(ctx: ServerContext) -> dict[str, float]:
    here = await ctx.run_engine(ctx.engine.cursor_position)
    return {"x": here.x, "y": here.y}


LocArg = Annotated[
    list[float] | str | None, Field(description="[x, y] in macOS screen points (from a snapshot).")
]
LabelArg = Annotated[
    int | str | None, Field(description="Element label: the number of a snapshot ref (12 for e12).")
]


# =========================================================================== input
def _click(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def click(
        *,
        loc: LocArg = None,
        label: LabelArg = None,
        button: Annotated[Literal["left", "right", "middle"], Field(description="Mouse button.")] = "left",
        clicks: Annotated[int, Field(description="1 click, 2 double-click, 3 triple-click.", ge=1, le=3)] = 1,
    ) -> CallToolResult:
        return await fn(**_target(loc, label), button=button, count=clicks)

    return click


CaretPosition = Literal["start", "idle", "end"]
_CARET_KEYS = {"start": "cmd+up", "end": "cmd+down"}


def _type(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def type_(
        *,
        text: Annotated[str, Field(description="Text to type.", max_length=20_000)],
        loc: LocArg = None,
        label: LabelArg = None,
        clear: Annotated[bool, Field(description="Replace the field's text.")] = False,
        caret_position: Annotated[
            CaretPosition, Field(description="Move the caret to the start or end first (idle: leave it).")
        ] = "idle",
        press_enter: Annotated[bool, Field(description="Press Return afterwards.")] = False,
    ) -> CallToolResult:
        target = _target(loc, label, required=False)
        ref = target.get("ref")
        if "x" in target or (ref is not None and caret_position != "idle"):
            clicked = await _inner(ctx, "click")(**target)
            if clicked.is_error:
                return clicked
            ref = None  # the field has focus now
        if caret_position != "idle" and not clear:
            pressed = await _inner(ctx, "press_keys")(keys=_CARET_KEYS[caret_position], report="none")
            if pressed.is_error:
                return pressed
        return await fn(text=text, ref=ref, clear=clear, submit=press_enter)

    return type_


def _scroll(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def scroll(
        *,
        loc: LocArg = None,
        label: LabelArg = None,
        type: Annotated[
            Literal["horizontal", "vertical"], Field(description="vertical | horizontal.")
        ] = "vertical",
        direction: Annotated[
            Literal["up", "down", "left", "right"], Field(description="up | down | left | right.")
        ] = "down",
        wheel_times: Annotated[
            int, Field(description="Mouse wheel notches (3 lines each).", ge=1, le=100)
        ] = 1,
    ) -> CallToolResult:
        target = _target(loc, label, required=False) or await _cursor(ctx)
        if type == "horizontal" and direction in ("up", "down"):
            direction = "left" if direction == "up" else "right"
        return await fn(**target, direction=direction, amount=wheel_times * LINES_PER_WHEEL, unit="lines")

    return scroll


def _move(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def move(
        *,
        loc: LocArg = None,
        label: LabelArg = None,
        drag: Annotated[bool, Field(description="Drag (button held) instead of moving.")] = False,
        from_loc: Annotated[
            list[float] | str | None, Field(description="drag: [x, y] to start from (default: the pointer).")
        ] = None,
        duration: Annotated[
            float | None, Field(description="drag: seconds the drag takes.", ge=0, le=5)
        ] = None,
    ) -> CallToolResult:
        target = _target(loc, label)
        if not drag:
            if from_loc is not None or duration is not None:
                raise _invalid("from_loc and duration only go with drag=true.")
            return await fn(**target)
        start = _point(from_loc, "from_loc")
        if start is None:
            here = await _cursor(ctx)
            start = (here["x"], here["y"])
        start_args = {"from_x": start[0], "from_y": start[1]}
        end_args = (
            {"to_ref": target["ref"]} if "ref" in target else {"to_x": target["x"], "to_y": target["y"]}
        )
        extra = {"duration_ms": round(duration * 1000)} if duration is not None else {}
        return await _inner(ctx, "drag")(**start_args, **end_args, **extra)

    return move


def _shortcut(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def shortcut(
        *,
        shortcut: Annotated[
            str,
            Field(
                description='Keys joined by +, e.g. "cmd+c" (cmd, not ctrl); several: separate by spaces.',
                min_length=1,
                max_length=400,
            ),
        ],
    ) -> CallToolResult:
        return await fn(keys=shortcut)

    return shortcut


def _wait(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def wait(
        *,
        duration: Annotated[int, Field(description="Seconds to wait.", ge=0, le=WAIT_MAX_S)],
    ) -> CallToolResult:
        return await fn(condition="duration", timeout_ms=duration * 1000)

    return wait


WaitForCondition = Literal[
    "text_exists", "active_window", "element_exists", "element_enabled", "focused_element"
]
_CONDITIONS = {
    "text_exists": "text_visible",
    "active_window": "window",
    "element_exists": "element",
    "element_enabled": "element",
    "focused_element": "element",
}


def _wait_for(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def wait_for(
        *,
        condition: Annotated[WaitForCondition, Field(description="What to wait for.")],
        text: Annotated[
            str | None, Field(description="Text, element name or window title.", max_length=500)
        ] = None,
        window_name: Annotated[
            str | None, Field(description="Window title (active_window).", max_length=500)
        ] = None,
        timeout: Annotated[  # noqa: ASYNC109 - Windows-MCP's parameter name
            float, Field(description="Seconds before giving up.", gt=0, le=WAIT_MAX_S)
        ] = 10.0,
        interval: Annotated[
            float, Field(description="Ignored: AXPilot chooses the polling interval.", ge=0)
        ] = 0.25,
        use_dom: Annotated[
            bool, Field(description="Ignored: web content is in the accessibility tree.")
        ] = False,
    ) -> CallToolResult:
        query = (window_name or text) if condition == "active_window" else (text or window_name)
        if not query:
            raise _invalid(f"{condition} needs text or window_name.")
        return await fn(condition=_CONDITIONS[condition], query=query, timeout_ms=round(timeout * 1000))

    return wait_for


def _multi_select(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def multi_select(
        *,
        locs: Annotated[
            list[list[float]] | str | None, Field(description="[[x, y], …] in screen points.")
        ] = None,
        labels: Annotated[
            list[int | str] | str | None, Field(description="[12, 13, …]: snapshot ref numbers.")
        ] = None,
        press_ctrl: Annotated[bool, Field(description="Hold cmd to add each item to the selection.")] = True,
    ) -> CallToolResult:
        targets = _many_targets(locs, labels)
        modifiers = ["cmd"] if press_ctrl else []
        result: CallToolResult | None = None
        for target in targets:
            result = await fn(**target, modifiers=modifiers)
            if result.is_error:
                return result
        assert result is not None
        return result

    return multi_select


def _many_targets(locs: Any, labels: Any) -> list[dict[str, Any]]:
    if (locs is None) == (labels is None):
        raise _invalid("Pass locs or labels.")
    items = _parsed(locs if locs is not None else labels, "locs" if locs is not None else "labels")
    if not isinstance(items, list) or not items or len(items) > 50:
        raise _invalid("Pass a list of 1 to 50 items.")
    if locs is not None:
        return [_target(item, None) for item in items]
    return [_target(None, item) for item in items]


def _multi_edit(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def multi_edit(
        *,
        locs: Annotated[
            list[list[Any]] | str | None, Field(description="[[x, y, text], …]: click each point and type.")
        ] = None,
        labels: Annotated[
            list[list[Any]] | str | None, Field(description="[[label, text], …]: set each field.")
        ] = None,
    ) -> CallToolResult:
        if (locs is None) == (labels is None):
            raise _invalid("Pass locs or labels.")
        if labels is not None:
            items = _parsed(labels, "labels")
            if not isinstance(items, list) or not items:
                raise _invalid("labels must be [[label, text], …].")
            fields = []
            for item in items:
                if not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], str):
                    raise _invalid(f"Each labels item is [label, text], got {item!r}.")
                fields.append(FieldValue(ref=ref_of(item[0]) or "", value=item[1]))
            return await fn(fields=fields)
        items = _parsed(locs, "locs")
        if not isinstance(items, list) or not items:
            raise _invalid("locs must be [[x, y, text], …].")
        click, type_text = _inner(ctx, "click"), _inner(ctx, "type_text")
        result: CallToolResult | None = None
        for item in items:
            if not isinstance(item, list) or len(item) != 3 or not isinstance(item[2], str):
                raise _invalid(f"Each locs item is [x, y, text], got {item!r}.")
            clicked = await click(**_target(item[:2], None), report="none")
            if clicked.is_error:
                return clicked
            result = await type_text(text=item[2], clear=True)
            if result.is_error:
                return result
        assert result is not None
        return result

    return multi_edit


# =========================================================================== observe
async def _display_id(ctx: ServerContext, display: Any) -> int | None:
    if display is None:
        return None
    indices = _parsed(display, "display")
    if not isinstance(indices, list) or len(indices) != 1 or not isinstance(indices[0], int):
        raise _invalid("display takes one zero-based index, e.g. [0].", hint="Capture one display at a time.")
    displays = await ctx.run_engine(ctx.engine.list_displays)
    if not 0 <= indices[0] < len(displays):
        raise AXPilotError(
            ErrorCode.DISPLAY_NOT_FOUND,
            f"There is no display {indices[0]}; there are {len(displays)}.",
            hint="DisplayInventory lists them.",
        )
    return displays[indices[0]].display_id


def _region(region: Any) -> list[float] | None:
    if region is None:
        return None
    value = _parsed(region, "region")
    if not isinstance(value, list) or len(value) != 4 or not all(isinstance(v, int | float) for v in value):
        raise _invalid("region is [left, top, right, bottom] in screen points.")
    left, top, right, bottom = (float(v) for v in value)
    if right <= left or bottom <= top:
        raise _invalid("region must have right > left and bottom > top.")
    return [left, top, right - left, bottom - top]


def _screenshot(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def screenshot(
        *,
        use_annotation: Annotated[
            bool, Field(description="Ignored (use Snapshot with use_vision for labels).")
        ] = False,
        width_reference_line: Annotated[int | None, Field(description="Ignored.")] = None,
        height_reference_line: Annotated[int | None, Field(description="Ignored.")] = None,
        display: Annotated[
            list[int] | str | None, Field(description="[index] of one display (from 0).")
        ] = None,
        region: Annotated[
            list[float] | str | None, Field(description="[left, top, right, bottom] in points.")
        ] = None,
    ) -> CallToolResult:
        rect = _region(region)
        if rect is not None:
            return await fn(rect=rect)
        return await fn(display_id=await _display_id(ctx, display))

    return screenshot


def _snapshot(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def snapshot(
        *,
        use_vision: Annotated[bool, Field(description="Include a screenshot of the window.")] = False,
        use_dom: Annotated[
            bool, Field(description="Ignored: web content is in the accessibility tree.")
        ] = False,
        use_annotation: Annotated[
            bool, Field(description="With use_vision: draw the labels on the image.")
        ] = True,
        use_ui_tree: Annotated[
            bool, Field(description="Ignored: the element list is always returned.")
        ] = True,
        width_reference_line: Annotated[int | None, Field(description="Ignored.")] = None,
        height_reference_line: Annotated[int | None, Field(description="Ignored.")] = None,
        display: Annotated[
            list[int] | str | None, Field(description="Ignored: AXPilot snapshots one app.")
        ] = None,
        region: Annotated[
            list[float] | str | None, Field(description="Ignored: AXPilot snapshots one app.")
        ] = None,
        app: Annotated[
            str | None,
            Field(description="App name or bundle id (default: the frontmost app).", max_length=256),
        ] = None,
    ) -> CallToolResult:
        return await fn(app=app, include_screenshot=use_vision, annotate=use_vision and use_annotation)

    return snapshot


# =========================================================================== apps and system
AppMode = Literal["launch", "launch_executable", "resize", "switch"]


class AppResultModel(OutputModel):
    """Output of the ``windows-mcp`` ``App``: the ``app`` tool's result (launch, switch), or the
    ``window`` tool's after a resize (``window``, ``closed``)."""

    action: AppActionArg | WindowActionArg
    message: str
    app: AppModel | None = None
    window: WindowModel | None = None
    closed: bool = False


def _app(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    async def app(
        *,
        mode: Annotated[
            AppMode, Field(description="launch | launch_executable | resize | switch.")
        ] = "launch",
        name: Annotated[
            str | None, Field(description="App name, bundle id or .app path.", max_length=1024)
        ] = None,
        window_loc: Annotated[
            list[float] | None, Field(description="[x, y] for the window's top-left corner.")
        ] = None,
        window_size: Annotated[
            list[float] | None, Field(description="[width, height] of the window.")
        ] = None,
        executable: Annotated[
            str | None, Field(description="launch_executable: an .app path.", max_length=1024)
        ] = None,
        args: Annotated[
            list[str] | str | None, Field(description="Not supported on macOS (use Shell).")
        ] = None,
        cwd: Annotated[str | None, Field(description="Not supported on macOS (use Shell).")] = None,
    ) -> CallToolResult:
        if mode != "launch_executable" and (executable is not None or args is not None or cwd is not None):
            raise _invalid("executable, args and cwd only go with mode=launch_executable.")
        if mode == "launch_executable":
            if args or cwd:
                raise AXPilotError(
                    ErrorCode.NOT_SUPPORTED,
                    "Launching with arguments or a working directory is not supported here.",
                    hint="Use Shell (e.g. open -a App --args …) when the scripting tools are enabled.",
                )
            if not executable or name is not None:
                raise _invalid("launch_executable needs executable (and no name).")
            mode, name = "launch", executable
        if mode == "resize":
            return await _resize(ctx, name, window_loc, window_size)
        if not name:
            raise _invalid(f"{mode} needs name.")
        result = await fn(action="activate" if mode == "switch" else "launch", name=name)
        if result.is_error or (window_loc is None and window_size is None):
            return result
        resized = await _resize(ctx, name, window_loc, window_size)
        return result if not resized.is_error else resized

    app.__annotations__["return"] = Annotated[CallToolResult, AppResultModel]
    return app


async def _resize(
    ctx: ServerContext, name: str | None, window_loc: list[float] | None, window_size: list[float] | None
) -> CallToolResult:
    if window_loc is None and window_size is None:
        raise _invalid("resize needs window_loc and/or window_size.")
    for value, what in ((window_loc, "window_loc"), (window_size, "window_size")):
        if value is not None and len(value) != 2:
            raise _invalid(f"{what} is a list of two numbers.")
    target = await ctx.resolve_app(name)
    if target is None:
        raise AXPilotError(ErrorCode.APP_NOT_FOUND, "No app is frontmost.", hint="Pass name.")
    windows = await ctx.run_engine(ctx.engine.list_windows, target.bundle_id or target.name)
    if not windows:
        raise AXPilotError(ErrorCode.WINDOW_NOT_FOUND, f"{target.name} has no open window.")
    window = _inner(ctx, "window")
    result: CallToolResult | None = None
    if window_loc is not None:
        result = await window(action="move", window_id=windows[0].window_id, x=window_loc[0], y=window_loc[1])
        if result.is_error:
            return result
    if window_size is not None:
        result = await window(
            action="resize", window_id=windows[0].window_id, width=window_size[0], height=window_size[1]
        )
    assert result is not None
    return result


def _process(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def process(
        *,
        mode: Annotated[Literal["list", "kill"], Field(description="list | kill.")],
        name: Annotated[
            str | None, Field(description="list: filter; kill: the process to stop.", max_length=256)
        ] = None,
        pid: Annotated[int | None, Field(description="kill: the process id.")] = None,
        sort_by: Annotated[
            Literal["memory", "cpu", "name"], Field(description="Ignored: sorted by CPU.")
        ] = "memory",
        limit: Annotated[int, Field(description="list: most processes to return.", ge=1, le=500)] = 20,
        force: Annotated[bool, Field(description="kill: force (SIGKILL).")] = False,
    ) -> CallToolResult:
        if mode == "list":
            return await fn(action="list", name=name, limit=limit)
        if pid is None:
            if not name:
                raise _invalid("kill needs pid or name.")
            listed = await fn(action="list", name=name, limit=10)
            if listed.is_error:
                return listed
            found = (listed.structured_content or {}).get("processes", [])
            if len(found) != 1:
                pids = ", ".join(f"{p['pid']} {p['name']}" for p in found) or "none"
                raise _invalid(f"{len(found)} processes match {name!r} ({pids}).", hint="Pass the pid.")
            pid = int(found[0]["pid"])
        return await fn(action="kill", pid=pid, force=force)

    return process


def _clipboard(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def clipboard(
        *,
        mode: Annotated[Literal["get", "set"], Field(description="get | set.")],
        text: Annotated[str | None, Field(description="set: the text.", max_length=1_000_000)] = None,
    ) -> CallToolResult:
        return await fn(action=mode, text=text)

    return clipboard


def _notification(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def notification(
        *,
        title: Annotated[str, Field(description="Notification title.", min_length=1, max_length=256)],
        message: Annotated[str, Field(description="Notification body.", min_length=1, max_length=2000)],
        app_id: Annotated[str | None, Field(description="Ignored on macOS.", max_length=256)] = None,
    ) -> CallToolResult:
        return await fn(title=title, message=message)

    return notification


def _shell(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def shell(
        *,
        command: Annotated[
            str, Field(description="Command line, run with zsh -lc.", min_length=1, max_length=100_000)
        ],
        timeout: Annotated[  # noqa: ASYNC109 - Windows-MCP's parameter name
            int,
            Field(
                description="Seconds before it is stopped (at most [scripting] shell_timeout_s).",
                ge=1,
                le=120,
            ),
        ] = 30,
    ) -> CallToolResult:
        return await fn(command=command, timeout_s=float(min(timeout, ctx.config.scripting.shell_timeout_s)))

    return shell


RegistryType = Literal["String", "ExpandString", "DWord", "QWord", "Binary", "MultiString", "Bool", "Float"]
_DEFAULTS_TYPES: dict[str, str] = {
    "String": "string",
    "ExpandString": "string",
    "DWord": "int",
    "QWord": "int",
    "Bool": "bool",
    "Float": "float",
}


def _defaults(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def defaults(
        *,
        mode: Annotated[
            Literal["get", "set", "delete", "list"],
            Field(description="get | set | list (delete: not supported)."),
        ],
        path: Annotated[
            str,
            Field(
                description="Preferences domain, e.g. com.apple.dock or NSGlobalDomain.",
                min_length=1,
                max_length=1024,
            ),
        ],
        name: Annotated[str | None, Field(description="The key.", max_length=1024)] = None,
        value: Annotated[str | None, Field(description="set: the new value.", max_length=100_000)] = None,
        type: Annotated[
            RegistryType, Field(description="set: String, DWord/QWord (integer), Bool or Float.")
        ] = "String",
    ) -> CallToolResult:
        if mode == "delete":
            raise AXPilotError(
                ErrorCode.NOT_SUPPORTED,
                "Deleting preferences is not supported.",
                hint="Set the key to its default value instead.",
            )
        if mode in ("get", "list"):
            if mode == "get" and not name:
                raise _invalid("get needs name (the key); use mode=list for the whole domain.")
            return await fn(domain=path, key=name if mode == "get" else None)
        if not name or value is None:
            raise _invalid("set needs name and value.")
        if type not in _DEFAULTS_TYPES:
            raise _invalid(
                f"Type {type} has no macOS preference equivalent.", hint="Use String, DWord, Bool or Float."
            )
        return await fn(domain=path, action="write", key=name, value=value, type=_DEFAULTS_TYPES[type])

    return defaults


def _same_page(wanted: str, shown: str) -> bool:
    def key(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        return (parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/") or "/", parts.query)

    return key(wanted) == key(shown)


def _scrape(fn: ToolFn, ctx: ServerContext) -> ToolFn:
    @_returning(fn)
    async def scrape(
        *,
        url: Annotated[
            str | None,
            Field(description="The page you expect (it must be open in the browser).", max_length=8192),
        ] = None,
        query: Annotated[str | None, Field(description="Ignored.", max_length=1000)] = None,
        use_dom: Annotated[
            bool, Field(description="Ignored: the page is always read from the browser.")
        ] = False,
        use_sampling: Annotated[bool, Field(description="Ignored: the text is returned as is.")] = False,
        browser: Annotated[
            str | None,
            Field(description="Safari, Chrome, Arc, Edge, Brave… (default: frontmost).", max_length=100),
        ] = None,
    ) -> CallToolResult:
        result = await fn(browser=browser)
        if result.is_error or url is None:
            return result
        shown = (result.structured_content or {}).get("url")
        if shown and not _same_page(url, shown):
            raise _invalid(
                f"The browser shows {shown}, not {url}.",
                hint="AXPilot reads pages in the browser and does not fetch URLs itself: open the page "
                "first (BrowserTabs action=open), then Scrape.",
            )
        return result

    return scrape


# =========================================================================== the table
@dataclass(frozen=True, slots=True)
class _Mapping:
    public: str
    canonical: str
    adapter: Adapter | None = None
    description: str | None = None
    unless: str | None = None
    """Leave this tool out when that canonical tool is enabled (it covers this one)."""


_LABELS = "label is the number of a snapshot ref (12 for e12); loc is [x, y] in macOS screen points."

MAPPINGS: tuple[_Mapping, ...] = (
    _Mapping(
        "Snapshot",
        "snapshot",
        _snapshot,
        "Capture the frontmost app's UI as a numbered list of elements: e12 is label 12 for Click, Type, "
        "Scroll and Move. Params: use_vision (add a screenshot), use_annotation (draw the labels on it), "
        "app (another app). Call it first and after the UI changes.",
    ),
    _Mapping(
        "Screenshot",
        "screenshot",
        _screenshot,
        "Capture a display (display=[index]) or a region ([left, top, right, bottom] in screen points) as an "
        "image. Coordinates on macOS are points; the result gives the scale.",
    ),
    _Mapping("DisplayInventory", "list_displays"),
    _Mapping("Wait", "wait_for", _wait, "Pause for duration seconds (at most 60)."),
    _Mapping(
        "WaitFor",
        "wait_for",
        _wait_for,
        "Wait (polling) until text_exists, active_window, element_exists, element_enabled or focused_element "
        "holds for text / window_name in the frontmost app, up to timeout seconds (at most 60).",
    ),
    _Mapping("ControlStatus", "status"),
    _Mapping(
        "Click",
        "click",
        _click,
        f"Click an element (label) or a point (loc). {_LABELS} button left|right|middle; clicks 1-3. Returns "
        "what changed.",
    ),
    _Mapping(
        "MultiSelect",
        "click",
        _multi_select,
        "Select several items (files, rows, checkboxes): clicks each of locs ([[x, y], …]) or labels "
        "([12, 13, …]) while holding cmd (press_ctrl=true), or plain clicks (press_ctrl=false).",
    ),
    _Mapping(
        "Type",
        "type_text",
        _type,
        f"Type text into a field (label or loc; default: the focused field). {_LABELS} clear replaces the "
        "text; caret_position start|end moves the caret first; press_enter presses Return.",
    ),
    _Mapping(
        "Shortcut",
        "press_keys",
        _shortcut,
        'Press keys: "cmd+c", "cmd+shift+t", "return", or several separated by spaces ("cmd+a delete"). '
        "macOS shortcuts use cmd where Windows uses ctrl; win means cmd.",
    ),
    _Mapping(
        "Scroll",
        "scroll",
        _scroll,
        f"Scroll at a label, a loc or the pointer. {_LABELS} direction up|down|left|right (type horizontal "
        "turns up/down into left/right); wheel_times notches of 3 lines.",
    ),
    _Mapping("Drag", "drag", unless="move_mouse"),
    _Mapping(
        "Move",
        "move_mouse",
        _move,
        f"Move the pointer to a label or loc (hover), or drag=true to drag to it from from_loc (default: the "
        f"pointer) over duration seconds. {_LABELS}",
    ),
    _Mapping("SetValue", "set_value"),
    _Mapping("PerformAction", "perform_action"),
    _Mapping("SelectMenu", "select_menu"),
    _Mapping(
        "MultiEdit",
        "fill_form",
        _multi_edit,
        "Fill several fields: labels [[label, text], …] sets each field (checkboxes take true/false), or "
        "locs [[x, y, text], …] clicks each point and replaces its text.",
    ),
    _Mapping(
        "App",
        "app",
        _app,
        "Launch (name: app name, bundle id or .app path), switch to (bring to front) or resize apps. resize "
        "moves/resizes the app's front window to window_loc [x, y] / window_size [w, h] (screen points); "
        "launch takes them too. launch_executable opens an .app path (no args or cwd on macOS).",
    ),
    _Mapping(
        "Process",
        "processes",
        _process,
        "List processes (mode=list, name filter, limit) or stop one (mode=kill by pid, or by name when "
        "exactly one matches; force for SIGKILL). Stopping asks the user; system processes are refused.",
    ),
    _Mapping(
        "Clipboard", "clipboard", _clipboard, "Read (mode=get) or set (mode=set, text) the clipboard text."
    ),
    _Mapping("Notification", "notify", _notification, "Show a macOS notification with title and message."),
    _Mapping(
        "Shell",
        "shell",
        _shell,
        "Run a command line with zsh (PowerShell's counterpart on macOS) and return exit code, stdout and "
        "stderr; timeout in seconds. The user approves each command.",
    ),
    _Mapping(
        "Defaults",
        "defaults",
        _defaults,
        "Read or write macOS preferences (the Registry's counterpart): path is the domain "
        "(com.apple.dock, NSGlobalDomain), name the key. mode get | list | set (value, type String, "
        "DWord, Bool or Float). Writing asks the user.",
    ),
    _Mapping(
        "Scrape",
        "browser_read",
        _scrape,
        "Read the text of the page in the browser's front window (Safari, Chrome, Arc, Edge, Brave…). "
        "AXPilot does not fetch URLs: url, if given, must be the page shown (open it with BrowserTabs).",
    ),
)


_BY_CANONICAL: dict[str, list[_Mapping]] = {}
for _mapping in MAPPINGS:
    _BY_CANONICAL.setdefault(_mapping.canonical, []).append(_mapping)


def apply_windows_mcp(specs: Sequence[ToolSpec]) -> list[tuple[str, ToolSpec, Adapter | None]]:
    """``(public name, spec, adapter)`` for every enabled tool under the ``windows-mcp`` naming, in
    ``TOOL_ORDER`` (extra Windows-MCP tools right after the tool they are built on)."""
    enabled = {spec.name for spec in specs}
    entries: list[tuple[str, ToolSpec, Adapter | None]] = []
    for spec in specs:
        mappings = _BY_CANONICAL.get(spec.name) or [_Mapping(NAMES[spec.name], spec.name)]
        for mapping in mappings:
            if mapping.unless is not None and mapping.unless in enabled:
                continue
            description = mapping.description or rename_references(spec.description)
            entries.append(
                (mapping.public, dataclasses.replace(spec, description=description), mapping.adapter)
            )
    return entries
