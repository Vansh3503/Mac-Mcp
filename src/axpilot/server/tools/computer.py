"""The ``computer`` tool (the ``computer`` profile): one tool with the common computer-use actions,
in the pixel coordinates of the latest screenshot it took, so vision-first agent loops written
for other computer-use tools work without prompt changes.

Coordinates are pixels of the last ``screenshot`` action of this tool (of this client's server),
mapped to screen points with :meth:`Capture.to_points`. Clicks, drags, typing and scrolling go
through the same checks as the other input tools: the app under the point (or the frontmost app
for keys) is hit-tested against the policy (moving the pointer is not), and everything that moves
the pointer or sends keys is rate limited and takes the input lock (screenshots, the cursor
position and waiting do not).

``left_mouse_down`` and ``left_mouse_up`` are joined: the press is performed when the release
comes, as a click when the pointer did not move and as a drag otherwise (the engines have no
separate press and release).
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import contextlib
import functools
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Literal

from mcp_types import CallToolResult
from pydantic import Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import MODIFIER_ALIASES, parse_sequence
from axpilot.models import Capture, MouseButton, Point
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import CaptureModel, OutputModel
from axpilot.server.tools.input import (
    Target,
    activate_app,
    keystroke_target,
    require_keyboard,
    resolve_point,
    type_keystrokes,
)
from axpilot.server.tools.observe import capture_screen

__all__ = ["SCROLL_LINES_PER_CLICK", "parse_modifiers"]

SCROLL_LINES_PER_CLICK = 3
"""Lines scrolled per ``scroll_amount`` unit (one notch of a mouse wheel)."""
WAIT_MAX_S = 60.0
TYPE_MAX_CHARS = 20_000

ComputerAction = Literal[
    "screenshot",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "mouse_move",
    "left_click_drag",
    "left_mouse_down",
    "left_mouse_up",
    "type",
    "key",
    "scroll",
    "wait",
    "cursor_position",
]
ScrollDirection = Literal["up", "down", "left", "right"]

_CLICKS: dict[str, tuple[MouseButton, int]] = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}
_POINTER_ACTIONS = frozenset(
    {*_CLICKS, "mouse_move", "left_click_drag", "left_mouse_down", "left_mouse_up", "scroll"}
)


COMPUTER_DESCRIPTION = """\
Control the computer like a person, with screenshots and pixel coordinates.
Start with action="screenshot"; every coordinate is [x, y] in pixels of the latest screenshot from
this tool. Take a new screenshot after the screen changes.
Actions: screenshot; left_click | right_click | middle_click | double_click | triple_click
(coordinate, default the pointer position; text = modifier keys to hold, e.g. "shift" or
"cmd+shift"); mouse_move (coordinate); left_click_drag (start_coordinate → coordinate);
left_mouse_down / left_mouse_up (performed together on release: a click, or a drag if the pointer
moved); type (text); key (text: a key or combo such as "Return", "cmd+c", "ctrl+shift+Tab", or
several separated by spaces); scroll (coordinate, scroll_direction, scroll_amount in wheel notches);
wait (duration seconds, at most 60); cursor_position.
return_screenshot=true returns a fresh screenshot after the action.
Prefer the accessibility tools (snapshot, click by ref) when available: they are faster and more
reliable. Errors: SNAPSHOT_NOT_FOUND (take a screenshot first), INVALID_ARGUMENT (pixel outside the
screenshot, missing parameter), BLOCKED_BY_POLICY (protected app under the pointer, Terminal),
SECURE_INPUT_ACTIVE, USER_ACTIVE, PAUSED_BY_USER, PERMISSION_SCREEN_RECORDING."""


class ComputerModel(OutputModel):
    """Output of ``computer``."""

    action: ComputerAction
    message: str
    coordinate: list[int] | None = Field(
        default=None, description="Pixel position acted on (screenshot space)."
    )
    point: list[float] | None = Field(default=None, description="The same position in screen points.")
    app: str | None = Field(default=None, description="The app under the pointer or receiving keys.")
    screenshot: CaptureModel | None = Field(default=None, description="The screenshot taken, if any.")


@dataclass
class _State:
    """Per-server state: the latest screenshot and a pending ``left_mouse_down``."""

    capture_id: str | None = None
    pressed: Target | None = None


def parse_modifiers(text: str | None) -> frozenset[str]:
    """``"shift"``, ``"cmd+shift"``, ``"ctrl alt"`` → canonical modifier names."""
    if not text or not text.strip():
        return frozenset()
    found: set[str] = set()
    for word in text.replace("+", " ").split():
        name = MODIFIER_ALIASES.get(word.casefold().replace("_", ""))
        if name is None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{word!r} is not a modifier key.",
                hint='For clicks, text holds modifier keys only: "shift", "cmd", "option", "ctrl".',
            )
        found.add(name)
    return frozenset(found)


def _pixels(capture: Capture | None, point: Point) -> list[int] | None:
    if capture is None:
        return None
    x, y = capture.to_pixels(point)
    return [round(x), round(y)]


@tool(
    "computer",
    title="Computer use",
    description=COMPUTER_DESCRIPTION,
    open_world=False,
    windows_mcp_name="Computer",
)
def computer_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``computer`` tool (its state lives as long as this server)."""
    state = _State()

    def latest() -> Capture | None:
        if state.capture_id is None:
            return None
        try:
            return ctx.captures.get(state.capture_id)
        except AXPilotError:
            return None

    def require_screenshot() -> str:
        if state.capture_id is None or latest() is None:
            raise AXPilotError(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                "There is no screenshot to take coordinates from (or it expired).",
                hint='Call computer with action="screenshot" first, then use pixels of that image.',
            )
        return state.capture_id

    async def target_at(coordinate: list[int] | None, *, check_policy: bool = True) -> Target:
        if coordinate is None:
            here = await ctx.run_engine(ctx.engine.cursor_position)
            return await resolve_point(ctx, here.x, here.y, space="points", check_policy=check_policy)
        if len(coordinate) != 2:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A coordinate is [x, y].")
        return await resolve_point(
            ctx,
            coordinate[0],
            coordinate[1],
            space="screenshot",
            screenshot_id=require_screenshot(),
            check_policy=check_policy,
        )

    async def take_screenshot() -> tuple[Capture, CaptureModel]:
        capture, _ = await capture_screen(ctx)
        state.capture_id = ctx.captures.put(capture)
        return capture, CaptureModel.from_capture(capture, state.capture_id)

    @contextlib.asynccontextmanager
    async def turn() -> AsyncIterator[None]:
        ctx.limiter.check()
        async with ctx.control.input_turn(timeout_s=ctx.input_lock_timeout_s, label="computer"):
            yield

    def done(action: ComputerAction, message: str, target: Target | None = None) -> ComputerModel:
        point = target.point if target is not None else None
        return ComputerModel(
            action=action,
            message=message,
            coordinate=_pixels(latest(), point) if point is not None else None,
            point=[round(point.x, 1), round(point.y, 1)] if point is not None else None,
            app=target.app.name if target is not None and target.app is not None else None,
        )

    async def pointer(
        action: ComputerAction,
        coordinate: list[int] | None,
        start_coordinate: list[int] | None,
        modifiers: frozenset[str],
    ) -> ComputerModel:
        if action in _CLICKS:
            button, count = _CLICKS[action]
            target = await target_at(coordinate)
            async with turn():
                await activate_app(ctx, target.app)
                await ctx.run_engine(
                    functools.partial(
                        ctx.engine.mouse_click, target.point, button=button, count=count, modifiers=modifiers
                    )
                )
            return done(action, f"{action.replace('_', ' ')} at {target.describe()}", target)
        if action == "mouse_move":
            target = await target_at(coordinate, check_policy=False)  # hovering is not acting on an app
            async with turn():  # but it moves the user's pointer: yield to them, obey the kill switch
                await ctx.run_engine(ctx.engine.mouse_move, target.point)
            return done(action, f"moved the pointer to {target.describe()}", target)
        if action == "left_click_drag":
            if coordinate is None:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "left_click_drag needs coordinate (the end).")
            start = await target_at(start_coordinate)
            end = await target_at(coordinate)
            async with turn():
                await activate_app(ctx, start.app)
                await ctx.run_engine(
                    functools.partial(ctx.engine.mouse_drag, start.point, end.point, modifiers=modifiers)
                )
            return done(action, f"dragged from {start.describe()} to {end.describe()}", end)
        if action == "left_mouse_down":
            state.pressed = await target_at(coordinate)
            async with turn():
                await ctx.run_engine(ctx.engine.mouse_move, state.pressed.point)
            return done(
                action,
                f"left button down at {state.pressed.describe()} (performed with left_mouse_up)",
                state.pressed,
            )
        if action == "left_mouse_up":
            start = state.pressed
            if start is None:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    "left_mouse_up without left_mouse_down.",
                    hint="Use left_mouse_down first, or left_click / left_click_drag.",
                )
            end = await target_at(coordinate)
            state.pressed = None
            async with turn():
                await activate_app(ctx, start.app)
                if abs(end.point.x - start.point.x) < 1 and abs(end.point.y - start.point.y) < 1:
                    await ctx.run_engine(
                        functools.partial(ctx.engine.mouse_click, start.point, modifiers=modifiers)
                    )
                    return done(action, f"clicked at {start.describe()}", start)
                await ctx.run_engine(
                    functools.partial(ctx.engine.mouse_drag, start.point, end.point, modifiers=modifiers)
                )
            return done(action, f"dragged from {start.describe()} to {end.describe()}", end)
        raise AssertionError(action)  # pragma: no cover - every pointer action is handled above

    async def computer(
        action: Annotated[ComputerAction, Field(description="What to do (see the tool description).")],
        *,
        coordinate: Annotated[
            list[int] | None,
            Field(description="[x, y] in pixels of the latest screenshot.", min_length=2, max_length=2),
        ] = None,
        start_coordinate: Annotated[
            list[int] | None,
            Field(description="left_click_drag: where the drag starts.", min_length=2, max_length=2),
        ] = None,
        text: Annotated[
            str | None,
            Field(
                description="type: the text; key: keys such as cmd+c; clicks: modifier keys to hold.",
                max_length=TYPE_MAX_CHARS,
            ),
        ] = None,
        scroll_direction: Annotated[
            ScrollDirection | None, Field(description="scroll: up|down|left|right.")
        ] = None,
        scroll_amount: Annotated[int, Field(description="scroll: wheel notches.", ge=1, le=50)] = 3,
        duration: Annotated[
            float | None, Field(description="wait: seconds to wait.", ge=0, le=WAIT_MAX_S)
        ] = None,
        return_screenshot: Annotated[
            bool, Field(description="Return a fresh screenshot after the action.")
        ] = False,
    ) -> Annotated[CallToolResult, ComputerModel]:
        if action == "screenshot":
            capture, info = await take_screenshot()
            model = ComputerModel(
                action=action,
                message=f"screenshot {info.screenshot_id}: {capture.width}×{capture.height} px",
                screenshot=info,
            )
            return ok_result(
                model.message + ". Coordinates are pixels of this image.", model, images=[capture]
            )
        if start_coordinate is not None and action != "left_click_drag":
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "start_coordinate only goes with left_click_drag.")
        if action in _POINTER_ACTIONS and action != "scroll":
            modifiers = parse_modifiers(text)
            model = await pointer(action, coordinate, start_coordinate, modifiers)
        elif action == "scroll":
            if scroll_direction is None:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "scroll needs scroll_direction.")
            if text:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "scroll does not take modifier keys.")
            target = await target_at(coordinate)
            lines = scroll_amount * SCROLL_LINES_PER_CLICK
            dx = {"left": -lines, "right": lines}.get(scroll_direction, 0)
            dy = {"up": -lines, "down": lines}.get(scroll_direction, 0)
            async with turn():
                await ctx.run_engine(ctx.engine.scroll, target.point, dx, dy)
            model = done(
                action, f"scrolled {scroll_direction} {scroll_amount}× at {target.describe()}", target
            )
        elif action in ("type", "key"):
            if not text:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"{action} needs text.")
            combos = parse_sequence(text) if action == "key" else None
            app, _ = await keystroke_target(ctx)
            async with turn():
                await require_keyboard(ctx)
                if combos is None:
                    await type_keystrokes(ctx, text)
                else:
                    for combo in combos:
                        ctx.control.ensure_running()
                        await ctx.run_engine(ctx.engine.press_key, combo)
            what = (
                f"typed {len(text)} characters" if combos is None else "pressed " + " ".join(map(str, combos))
            )
            model = ComputerModel(
                action=action,
                message=what + (f" in {app.name}" if app else ""),
                app=app.name if app else None,
            )
        elif action == "wait":
            seconds = 1.0 if duration is None else duration
            ctx.control.ensure_running()
            await ctx.sleep(seconds)
            model = ComputerModel(action=action, message=f"waited {seconds:g} s")
        else:  # cursor_position
            here = await ctx.run_engine(ctx.engine.cursor_position)
            pixels = _pixels(latest(), here)
            where = f"X={pixels[0]}, Y={pixels[1]} (pixels)" if pixels else f"({here.x:g}, {here.y:g}) points"
            model = ComputerModel(
                action=action,
                message=f"the pointer is at {where}",
                coordinate=pixels,
                point=[round(here.x, 1), round(here.y, 1)],
            )
        if not return_screenshot:
            return ok_result(model.message, model)
        await ctx.sleep(ctx.config.perception.settle_ms / 1000)
        capture, info = await take_screenshot()
        model.screenshot = info
        return ok_result(
            f"{model.message}\nscreenshot {info.screenshot_id}: {capture.width}×{capture.height} px",
            model,
            images=[capture],
        )

    return computer
