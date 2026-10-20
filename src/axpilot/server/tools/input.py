"""Input tools: act on the Mac like a careful user.

``click``, ``type_text``, ``press_keys``, ``drag``, ``move_mouse``, ``set_value``,
``perform_action``, ``select_menu`` and ``fill_form``. (``scroll`` lives in
:mod:`axpilot.server.tools.navigate`.)

Every tool here is an *input action*: the guard rate-limits it and runs it inside the single input
turn (pause, yield-to-user, ``BUSY``). Each tool then:

1. **resolves its target**: an element ``ref`` from a snapshot, or a point (``x``/``y`` in screen
   points, or in pixels of a screenshot with ``coordinate_space="screenshot"``);
2. **applies the policy** to the app (and window) actually under the target: a coordinate click
   is hit-tested, so it cannot reach a deny-listed app; typing into Terminal-like apps needs the
   ``shell`` tool; secure fields need ``allow_secure_fields``;
3. **acts with a strategy ladder**: accessibility actions first (they work in the background and
   don't move the cursor), synthesized mouse/keyboard events as the fallback. Every rung tried is
   reported in ``attempts``; input is **never retried** with the same strategy;
4. **observes the result**: after a short settle it takes a new snapshot and returns what changed
   (``report="diff"``, the default), the whole snapshot, or nothing.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.
import contextlib
import dataclasses
import functools
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp_types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import parse_sequence
from axpilot.models import (
    AppInfo,
    Capture,
    ClipboardContent,
    KeyCombo,
    MenuNode,
    MouseButton,
    Point,
    Rect,
    WindowInfo,
)
from axpilot.perception.appkind import detect_app_kind
from axpilot.perception.diff import diff_snapshots
from axpilot.perception.format import diff_payload, format_diff, format_snapshot, quote, snapshot_payload
from axpilot.perception.model import Element, Snapshot
from axpilot.perception.navigate import scroll_container_for, viewport_of
from axpilot.perception.refs import ROW_AX_ROLES
from axpilot.perception.resolve import live_handle
from axpilot.perception.vision import is_vision_element
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import (
    ActionResult,
    AppArg,
    DiffModel,
    ElementBriefModel,
    OutputModel,
    RefArg,
    SnapshotIdArg,
    SnapshotModel,
)

__all__ = [
    "KEYSTROKE_FIRST_KINDS",
    "Target",
    "element_brief",
    "observe_after",
    "resolve_point",
    "resolve_target",
]

PASTE_THRESHOLD = 200
"""``type_text(strategy="auto")`` pastes text longer than this instead of typing it."""
TYPE_CHUNK = 400
"""Long text is typed in chunks of this many characters, checking the kill switch in between."""
PASTE_SETTLE_S = 0.15
"""Wait after ⌘V before the clipboard is restored (the app reads it asynchronously)."""
STEP_LIMIT = 200
"""``set_value`` on a stepper presses AXIncrement/AXDecrement at most this many times."""
FORM_LIMIT = 50
KEY_REPEAT_LIMIT = 50

KEYSTROKE_FIRST_KINDS = frozenset(
    {"electron", "cef", "chromium_browser", "safari", "firefox", "java", "qt", "unknown"}
)
"""App kinds whose text fields often ignore an AX value change (web views, Java, Qt): type
keystrokes into them instead."""

TOGGLE_ROLES = frozenset({"checkbox", "switch", "toggle", "radio"})
NUMERIC_ROLES = frozenset({"slider", "stepper", "indicator"})
TEXT_ROLES = frozenset({"field", "search", "secure", "textarea", "combobox", "date"})
DISCLOSURE_ROLES = frozenset({"disclosure"})
_TRUE = frozenset({"true", "on", "yes", "1", "checked", "selected", "expanded"})
_FALSE = frozenset({"false", "off", "no", "0", "unchecked", "deselected", "collapsed"})
_AX_ACTION = re.compile(r"^AX[A-Za-z]{2,40}$")

CoordinateSpace = Literal["points", "screenshot"]
Report = Literal["diff", "snapshot", "none"]
Button = Literal["left", "right", "middle"]
Modifier = Literal["cmd", "shift", "option", "ctrl", "fn"]

# --------------------------------------------------------------------------- shared arguments
OptionalRefArg = Annotated[
    str | None,
    Field(
        description='Element ref from the latest snapshot, e.g. "e12" (or an OCR ref "o3"). '
        "Give either ref or x/y.",
        min_length=2,
        max_length=32,
    ),
]
XArg = Annotated[float | None, Field(description="X coordinate (with y). Screen points by default.")]
YArg = Annotated[float | None, Field(description="Y coordinate (with x). Screen points by default.")]
SpaceArg = Annotated[
    CoordinateSpace,
    Field(
        description='"points" (screen points) or "screenshot" (pixels of the screenshot given by '
        "screenshot_id, or the latest one)."
    ),
]
ScreenshotIdArg = Annotated[
    str | None,
    Field(
        description='Screenshot the pixel coordinates refer to ("c_ab12"). Default: the latest.',
        max_length=16,
    ),
]
ReportArg = Annotated[
    Report,
    Field(
        description='What to return after acting: "diff" (what changed, default), "snapshot" '
        '(the whole new snapshot) or "none" (fastest).'
    ),
]
ModifiersArg = Annotated[
    list[Modifier],
    Field(
        description='Modifier keys held during the action, e.g. ["cmd"] to add to a selection.', max_length=5
    ),
]


# --------------------------------------------------------------------------- targets
@dataclass
class Target:
    """What an input action is aimed at."""

    point: Point
    app: AppInfo | None
    window: WindowInfo | None
    snap: Snapshot | None = None
    el: Element | None = None
    handle: Any = None

    def describe(self) -> str:
        if self.el is not None:
            name = f" {quote(self.el.name, 40)}" if self.el.name else ""
            return f"{self.el.ref} {self.el.role}{name}"
        return f"({round(self.point.x)},{round(self.point.y)})"


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


async def _on_a_display(ctx: ServerContext, point: Point) -> None:
    displays = await ctx.run_engine(ctx.engine.list_displays)
    if displays and not any(d.frame.contains(point) for d in displays):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"The point ({point.x:g}, {point.y:g}) is outside every display.",
            hint="Use coordinates from a snapshot (screen points) or a screenshot "
            '(coordinate_space="screenshot" with its screenshot_id).',
        )


def _capture(ctx: ServerContext, screenshot_id: str | None) -> Capture:
    try:
        return ctx.captures.get(screenshot_id)
    except AXPilotError as err:
        raise AXPilotError(
            err.code,
            err.message,
            hint='Take a screenshot first (or pass coordinate_space="points" with screen points).',
        ) from err


async def _window_at(ctx: ServerContext, point: Point, app: AppInfo | None) -> WindowInfo | None:
    windows = await ctx.run_engine(ctx.engine.list_windows)
    for window in windows:  # front to back
        if window.frame.contains(point) and (app is None or window.pid == app.pid):
            return window
    return None


async def resolve_point(
    ctx: ServerContext,
    x: float,
    y: float,
    *,
    space: CoordinateSpace = "points",
    screenshot_id: str | None = None,
    check_policy: bool = True,
) -> Target:
    """A point target: map screenshot pixels to points, check it lies on a display, find the app
    and window under it and apply the input policy to them."""
    if not _finite(x, y):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "x and y must be finite numbers.")
    if space == "screenshot":
        capture = _capture(ctx, screenshot_id)
        if not (0 <= x < capture.width and 0 <= y < capture.height):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Pixel ({x:g}, {y:g}) is outside the {capture.width}×{capture.height} screenshot.",
                hint="Use pixel coordinates of that screenshot (origin at its top-left corner).",
            )
        point = capture.to_points(x, y)
    else:
        point = Point(float(x), float(y))
    await _on_a_display(ctx, point)
    app = await ctx.run_engine(ctx.engine.app_at_point, point)
    window = await _window_at(ctx, point, app)
    if check_policy:
        ctx.policy.check_app(app, kind="input", window_title=window.title if window else None)
    return Target(point=point, app=app, window=window)


def _visible_rect(snap: Snapshot, el: Element) -> Rect | None:
    """The on-screen part of ``el``: clipped by its scroll container's viewport and the window."""
    if el.frame is None:
        return None
    rect: Rect | None = el.frame
    container = scroll_container_for(snap, el)
    if container is not None and container is not el:
        viewport = viewport_of(snap, container)
        if viewport is not None and rect is not None:
            rect = rect.intersection(viewport)
    if snap.window is not None and rect is not None:
        rect = rect.intersection(snap.window.frame)
    return rect


async def resolve_target(
    ctx: ServerContext,
    *,
    ref: str | None,
    snapshot_id: str | None,
    x: float | None,
    y: float | None,
    space: CoordinateSpace = "points",
    screenshot_id: str | None = None,
    need_handle: bool = False,
) -> Target:
    """Resolve ``ref`` (preferred) or ``x``/``y`` to a :class:`Target` and apply the input policy.

    Exactly one form must be given (``INVALID_ARGUMENT``). For refs, the element's live handle is
    looked up (re-finding it if the UI changed; ``STALE_REF`` if gone) unless it is an OCR
    element, and the point is the center of its visible part."""
    has_point = x is not None or y is not None
    if ref is not None and has_point:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            "Give either ref or x/y, not both.",
            hint="Prefer ref (from the latest snapshot); use x/y only for things snapshots don't list.",
        )
    if ref is None and not has_point:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            "No target: give ref (from a snapshot) or x and y.",
            hint="Call snapshot first and pass the ref of the element to act on.",
        )
    if ref is None:
        if x is None or y is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Give both x and y.")
        return await resolve_point(ctx, x, y, space=space, screenshot_id=screenshot_id)
    snap, el = ctx.snapshots.resolve(ref, snapshot_id)
    window = snap.window
    ctx.policy.check_app(snap.app, kind="input", window_title=window.title if window else None)
    handle = None
    if not is_vision_element(el):
        handle = await ctx.run_engine(
            live_handle, ctx.engine, snap, el, timeout_s=ctx.capture_timeout_s + ctx.engine_timeout_s
        )
    elif need_handle:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{el.ref} is OCR text, not an accessibility element.",
            hint='Click it (by ref or its center), or use an e-ref from snapshot(mode="interactive").',
        )
    visible, frame = await _current_geometry(ctx, snap, el, handle)
    if visible is not None:
        point = visible.center
    elif frame is not None:
        point = frame.center
    else:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{el.ref} has no position on screen.",
            hint="Use perform_action or set_value on it, or pick another element.",
        )
    target = Target(point=point, app=snap.app, window=window, snap=snap, el=el, handle=handle)
    if visible is None and frame is not None:
        await _scroll_into_view(ctx, target)
    return target


def _row_of(snap: Snapshot, el: Element) -> Element | None:
    """The list row ``el`` is, or lies in (``None`` outside rows)."""
    seen: set[str] = set()
    current: Element | None = el
    while current is not None and current.ref not in seen:
        if current.ax_role in ROW_AX_ROLES:
            return current
        seen.add(current.ref)
        current = snap.element(current.parent_ref) if current.parent_ref else None
    return None


async def _current_geometry(
    ctx: ServerContext, snap: Snapshot, el: Element, handle: Any
) -> tuple[Rect | None, Rect | None]:
    """``(visible part, frame)`` of ``el`` as of the newest snapshot of its window.

    A ref may resolve through an older snapshot (an explicit ``snapshot_id``, or a bare ref that
    newer snapshots of the window no longer list: scrolled out, filtered by their mode, or
    gone). Its frame there may be outdated, and a mouse fallback would hit whatever sits there
    now. So: when a newer snapshot lists the same session ref (the same element), its geometry
    is used; otherwise the element is read again through its live handle. Rows (and elements in
    them) that newer snapshots no longer list are refused: table views reuse a row that left the
    screen for other content, so its handle no longer proves it shows the same item."""
    newer: Snapshot | None = None
    window_id = snap.window.window_id if snap.window is not None else None
    for other in ctx.snapshots.snapshots_of(snap.app.pid, window_id):
        if other is snap:
            break
        same_session = snap.ref_session is not None and other.ref_session == snap.ref_session
        listed = other.element(el.ref) if same_session else None
        if listed is not None:
            return _visible_rect(other, listed), listed.frame
        newer = newer or other
    if newer is None or handle is None:
        return _visible_rect(snap, el), el.frame
    row = _row_of(snap, el)
    if row is not None:
        place = "is a list row" if row is el else f"is in list row {row.ref}"
        raise AXPilotError(
            ErrorCode.STALE_REF,
            f"{el.ref} {place} that snapshot {newer.snapshot_id} no longer shows; lists reuse rows "
            "for other content, so the ref cannot be trusted any more.",
            hint="Scroll back to it (or use find with scroll=true), then use the ref a new snapshot gives.",
        )
    node = await ctx.run_engine(ctx.engine.refresh_element, handle)
    if node.frame is None:
        return None, None
    window = newer.window
    return (node.frame.intersection(window.frame) if window is not None else node.frame), node.frame


async def _scroll_into_view(ctx: ServerContext, target: Target) -> None:
    """Off-screen element: ask AX to scroll it into view and take its new center."""
    if target.handle is None:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{target.describe()} is scrolled out of view.",
            hint="Scroll its list (scroll tool) and take a new snapshot.",
        )
    moved = await ctx.run_engine(ctx.engine.scroll_to_visible, target.handle)
    if moved:
        node = await ctx.run_engine(ctx.engine.refresh_element, target.handle)
        if node.frame is not None:
            target.point = node.frame.center
            return
    raise AXPilotError(
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        f"{target.describe()} is scrolled out of view and could not be scrolled into view.",
        hint="Scroll its list with the scroll tool, take a new snapshot and use the new ref.",
    )


# --------------------------------------------------------------------------- observing results
def element_brief(el: Element | None) -> ElementBriefModel | None:
    """The short form of an element in tool results (``None`` for ``None``)."""
    if el is None:
        return None
    center = el.center
    return ElementBriefModel(
        ref=el.ref,
        role=el.role,
        name=el.name,
        value=None if el.secure else el.value,
        center=center.to_list() if center is not None else None,
        states=list(el.states),
    )


def _app_key(app: AppInfo | None) -> str | None:
    if app is None:
        return None
    return app.bundle_id or str(app.pid)


@dataclass
class Observation:
    """What :func:`observe_after` saw."""

    text: str = ""
    diff: DiffModel | None = None
    snapshot: SnapshotModel | None = None
    snapshot_id: str | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)


async def snapshot_before(ctx: ServerContext, target: Target | None, report: Report) -> Snapshot | None:
    """The snapshot to diff against: the target's own, or (for point targets) a fresh one."""
    if report != "diff" or target is None:
        return None
    if target.snap is not None:
        return target.snap
    if target.app is None:
        return None
    try:
        return await ctx.take_snapshot(app=_app_key(target.app), store=False)
    except AXPilotError:
        return None


async def observe_after(
    ctx: ServerContext, *, app: AppInfo | None, before: Snapshot | None, report: Report
) -> Observation:
    """Let the UI settle, snapshot ``app`` (default: the frontmost app) and describe the change.

    The new snapshot carries its refs from ``before`` (the snapshot the agent acted from), so
    every ref the agent holds keeps naming its element. Failing to observe never fails the
    action: it becomes a warning."""
    if report == "none":
        return Observation()
    await ctx.sleep(ctx.config.perception.settle_ms / 1000)
    try:
        after = await ctx.take_snapshot(app=_app_key(app), previous=before)
    except AXPilotError as err:
        if err.code is ErrorCode.APP_NOT_FOUND and app is not None:
            try:  # the app quit or another app came forward: report the frontmost one
                after = await ctx.take_snapshot(previous=before)
            except AXPilotError as again:
                return Observation(warnings=[f"Could not observe the result: {again.message}"])
        else:
            return Observation(warnings=[f"Could not observe the result: {err.message}"])
    if report == "snapshot" or before is None:
        observation = Observation(
            text=format_snapshot(after),
            snapshot=SnapshotModel.model_validate(snapshot_payload(after)),
            snapshot_id=after.snapshot_id,
        )
        if report == "diff":
            observation.warnings.append("No earlier snapshot to compare with; this is the new snapshot.")
        return observation
    diff = diff_snapshots(before, after)
    text = "no visible change" if diff.is_empty() else format_diff(diff)
    return Observation(
        text=f"{text}\n(snapshot {after.snapshot_id})",
        diff=DiffModel.model_validate(diff_payload(diff)),
        snapshot_id=after.snapshot_id,
    )


def action_result(
    action: str,
    summary: str,
    observation: Observation,
    *,
    strategy: str | None = None,
    attempts: Sequence[str] = (),
    target: Target | None = None,
    message: str | None = None,
    warnings: Sequence[str] = (),
) -> CallToolResult:
    """The standard result of an input tool."""
    model = ActionResult(
        action=action,
        strategy=strategy,
        attempts=list(attempts),
        target=element_brief(target.el) if target is not None else None,
        point=target.point.to_list() if target is not None else None,
        message=message,
        diff=observation.diff,
        snapshot=observation.snapshot,
        snapshot_id=observation.snapshot_id,
    )
    text = summary if not observation.text else f"{summary}\n{observation.text}"
    return ok_result(text, model, warnings=[*warnings, *observation.warnings])


def _not_actionable(
    target: Target, verb: str, attempts: Sequence[str], errors: Sequence[str]
) -> AXPilotError:
    tried = ", ".join(attempts) or "nothing"
    reason = f" ({'; '.join(errors)})" if errors else ""
    return AXPilotError(
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        f"Could not {verb} {target.describe()}: tried {tried}{reason}.",
        hint="Take a new snapshot; the element may be disabled, covered or gone. "
        "Try another element, select_menu, or press_keys.",
        details={"attempts": list(attempts), "errors": list(errors)},
    )


# --------------------------------------------------------------------------- activation & mouse
async def activate_app(ctx: ServerContext, app: AppInfo | None) -> None:
    """Bring ``app`` to the front before synthesized input (events go to the frontmost app)."""
    if app is None:
        return
    front = await ctx.run_engine(ctx.engine.frontmost_app)
    if front is not None and front.pid == app.pid:
        return
    await ctx.run_engine(ctx.engine.app_control, "activate", _app_key(app) or app.name)
    await ctx.sleep(ctx.config.perception.settle_ms / 1000)


async def ensure_uncovered(ctx: ServerContext, target: Target) -> None:
    """A synthesized click lands on whatever is on top: make sure that is the target's app."""
    if target.app is None:
        return
    under = await ctx.run_engine(ctx.engine.app_at_point, target.point)
    if under is not None and under.pid != target.app.pid:
        await activate_app(ctx, target.app)
        under = await ctx.run_engine(ctx.engine.app_at_point, target.point)
        if under is not None and under.pid != target.app.pid:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{target.describe()} is covered by {under.name}.",
                hint="Move or minimize the covering window (window tool), or act on the element by ref "
                "with an accessibility action.",
            )


# --------------------------------------------------------------------------- click
CLICK_DESCRIPTION = """\
Click an element (by ref) or a point.
Prefer ref from the latest snapshot: AXPilot presses it through accessibility (works even when the
window is in the background, without moving the pointer) and falls back to a real mouse click.
Params: ref | x+y (coordinate_space "points" or "screenshot" + screenshot_id), button
(left|right|middle), count (2 = double-click), modifiers (e.g. ["cmd"] to add to a selection),
strategy (auto|ax|mouse), report (diff|snapshot|none).
Returns how it clicked and what changed (new/removed elements, focus and value changes) with the
new snapshot_id. Errors: STALE_REF (take a new snapshot), ELEMENT_NOT_ACTIONABLE (disabled or
covered), TIMEOUT (the app did not answer; the press may still land, so snapshot before retrying),
BLOCKED_BY_POLICY, USER_ACTIVE, PAUSED_BY_USER, INVALID_ARGUMENT."""

ClickStrategy = Literal["auto", "ax", "mouse"]


def _ax_click_action(target: Target, button: str, count: int, modifiers: Sequence[str]) -> str | None:
    """The AX action equivalent to this click, if any."""
    el = target.el
    if el is None or target.handle is None or modifiers or count != 1:
        return None
    if button == "left":
        for action in ("AXPress", "AXPick", "AXConfirm"):
            if action in el.actions:
                return action
        return None
    if button == "right" and "AXShowMenu" in el.actions:
        return "AXShowMenu"
    return None


async def click_target(
    ctx: ServerContext,
    target: Target,
    *,
    button: MouseButton,
    count: int,
    modifiers: Sequence[str],
    strategy: ClickStrategy,
) -> tuple[str, list[str]]:
    """Click ``target`` with the strategy ladder; returns ``(strategy used, attempts)``."""
    el = target.el
    if el is not None and not el.enabled:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{target.describe()} is disabled.",
            hint="Something else must happen first (fill a required field, select an item…).",
        )
    attempts: list[str] = []
    errors: list[str] = []
    action = _ax_click_action(target, button, count, modifiers) if strategy != "mouse" else None
    if action is not None:
        attempts.append(f"ax:{action}")
        try:
            await ctx.run_engine(ctx.engine.perform_action, target.handle, action)
            return f"ax:{action}", attempts
        except AXPilotError as err:
            if err.code is ErrorCode.TIMEOUT:  # the press may still land: clicking too could press twice
                raise AXPilotError(
                    err.code,
                    f"Pressing {target.describe()} did not finish in time; it may still have happened.",
                    hint="Take a snapshot to see whether it did before you try again.",
                    details=err.details,
                ) from err
            if strategy == "ax" or err.code is not ErrorCode.ELEMENT_NOT_ACTIONABLE:
                raise
            errors.append(err.message)
    elif strategy == "ax":
        raise _not_actionable(target, "press", ["ax"], ["no matching accessibility action"])
    if strategy == "ax":
        raise _not_actionable(target, "press", attempts, errors)
    attempts.append("mouse")
    await ensure_uncovered(ctx, target)
    await activate_app(ctx, target.app)
    await ctx.run_engine(
        functools.partial(
            ctx.engine.mouse_click, target.point, button=button, count=count, modifiers=frozenset(modifiers)
        )
    )
    return "mouse", attempts


@tool(
    "click",
    title="Click",
    description=CLICK_DESCRIPTION,
    input_action=True,
    windows_mcp_name="Click",
)
def click_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``click`` tool."""

    async def click(
        *,
        ref: OptionalRefArg = None,
        x: XArg = None,
        y: YArg = None,
        snapshot_id: SnapshotIdArg = None,
        coordinate_space: SpaceArg = "points",
        screenshot_id: ScreenshotIdArg = None,
        button: Annotated[Button, Field(description="Mouse button.")] = "left",
        count: Annotated[
            int, Field(description="Clicks: 2 = double-click, 3 = triple-click.", ge=1, le=3)
        ] = 1,
        modifiers: ModifiersArg = [],  # noqa: B006 - pydantic copies defaults
        strategy: Annotated[
            ClickStrategy, Field(description='"auto" (AX, then mouse), "ax" only, or "mouse" only.')
        ] = "auto",
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        target = await resolve_target(
            ctx,
            ref=ref,
            snapshot_id=snapshot_id,
            x=x,
            y=y,
            space=coordinate_space,
            screenshot_id=screenshot_id,
        )
        before = await snapshot_before(ctx, target, report)
        used, attempts = await click_target(
            ctx, target, button=button, count=count, modifiers=modifiers, strategy=strategy
        )
        observation = await observe_after(ctx, app=target.app, before=before, report=report)
        verb = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}[count]
        if button != "left":
            verb = f"{button}-{verb}"
        return action_result(
            "click", f"{verb} {target.describe()} via {used}", observation,
            strategy=used, attempts=attempts, target=target,
        )  # fmt: skip

    return click


# --------------------------------------------------------------------------- typing
TYPE_DESCRIPTION = """\
Type text into a field (by ref) or into whatever has keyboard focus.
With ref, AXPilot focuses the field first. It sets the text through accessibility in native apps,
types real keystrokes in web views, Electron, Java and Qt apps (they ignore accessibility edits),
and pastes long text (restoring your clipboard afterwards).
Params: text, ref (optional), clear (replace the field's content), submit (press Return after),
strategy (auto|ax_value|keystrokes|paste), report.
Errors: SECURE_INPUT_ACTIVE (a password prompt or secure terminal blocks typing),
BLOCKED_BY_POLICY (password fields, Terminal unless the shell tool is enabled), STALE_REF,
ELEMENT_NOT_ACTIONABLE, USER_ACTIVE, PAUSED_BY_USER."""

TypeStrategy = Literal["auto", "ax_value", "keystrokes", "paste"]


async def keystroke_target(ctx: ServerContext) -> tuple[AppInfo | None, WindowInfo | None]:
    """The app (and its front window) that receives keystrokes: the frontmost one."""
    app = await ctx.run_engine(ctx.engine.frontmost_app)
    window = None
    if app is not None:
        windows = await ctx.run_engine(ctx.engine.list_windows, _app_key(app))
        window = windows[0] if windows else None
    ctx.policy.check_app(app, kind="input", window_title=window.title if window else None)
    return app, window


def produces_text(combos: Sequence[KeyCombo]) -> bool:
    """True if pressing ``combos`` types characters (a key without ⌘/⌃/fn, or space)."""
    return any(
        (len(combo.key) == 1 or combo.key == "space") and not combo.modifiers & {"cmd", "ctrl", "fn"}
        for combo in combos
    )


async def check_focus_not_secure(
    ctx: ServerContext, app: AppInfo | None, snap: Snapshot | None = None
) -> None:
    """Apply the secure-field rule to keystrokes sent without a ref: they go to the focused element.

    Uses ``snap`` (a snapshot of ``app`` taken just before) when given. When the focus can't be
    read, macOS Secure Event Input remains the guard for native password fields."""
    if ctx.config.safety.allow_secure_fields or app is None:
        return
    if snap is None:
        try:
            snap = await ctx.take_snapshot(app=_app_key(app), store=False)
        except AXPilotError as err:
            ctx.logger.debug("Could not read the focused element of %s: %s", app.name, err.message)
            return
    focused = snap.focused_element
    ctx.policy.check_secure_target(bool(focused is not None and focused.secure))


async def front_window_title(ctx: ServerContext, app: AppInfo | None) -> str | None:
    """The title of ``app``'s front window (for the ``deny_windows`` rule), if it has one."""
    if app is None:
        return None
    windows = await ctx.run_engine(ctx.engine.list_windows, _app_key(app))
    return windows[0].title if windows else None


async def require_keyboard(ctx: ServerContext) -> None:
    if await ctx.run_engine(ctx.engine.secure_input_active):
        raise AXPilotError(
            ErrorCode.SECURE_INPUT_ACTIVE,
            "Secure keyboard entry is on (a password field or a secure terminal has focus), so "
            "macOS blocks synthetic keystrokes.",
        )


async def type_keystrokes(ctx: ServerContext, text: str) -> None:
    await require_keyboard(ctx)
    for start in range(0, len(text), TYPE_CHUNK):
        ctx.control.ensure_running()
        await ctx.run_engine(ctx.engine.type_text, text[start : start + TYPE_CHUNK])


async def _press(ctx: ServerContext, combo: KeyCombo) -> None:
    await ctx.run_engine(ctx.engine.press_key, combo)


async def _paste(ctx: ServerContext, text: str) -> None:
    """Paste ``text`` with ⌘V, restoring the previous clipboard afterwards (even on failure)."""
    await require_keyboard(ctx)
    saved: ClipboardContent = await ctx.run_engine(ctx.engine.clipboard_get)
    try:
        await ctx.run_engine(functools.partial(ctx.engine.clipboard_set, text=text))
        await _press(ctx, KeyCombo("v", frozenset({"cmd"})))
        await ctx.sleep(PASTE_SETTLE_S)
    finally:
        with contextlib.suppress(AXPilotError):
            await ctx.run_engine(
                functools.partial(
                    ctx.engine.clipboard_set,
                    text=saved.text,
                    image_png=saved.image_png,
                    files=saved.files or None,
                )
            )


def _prefers_keystrokes(app: AppInfo | None) -> bool:
    if app is None:
        return True
    try:
        return detect_app_kind(app.bundle_id, app.path) in KEYSTROKE_FIRST_KINDS
    except Exception:  # detection is a heuristic; never fail typing because of it
        return False


async def _focus(ctx: ServerContext, target: Target) -> str:
    """Give the field keyboard focus: AX focus, else a click on it."""
    if target.handle is not None:
        try:
            await ctx.run_engine(ctx.engine.focus, target.handle)
            return "ax_focus"
        except AXPilotError as err:
            if err.code is not ErrorCode.ELEMENT_NOT_ACTIONABLE:
                raise
    await ensure_uncovered(ctx, target)
    await activate_app(ctx, target.app)
    await ctx.run_engine(ctx.engine.mouse_click, target.point)
    return "click_focus"


async def _ax_text(ctx: ServerContext, target: Target, text: str, *, clear: bool) -> bool:
    """Set or insert text through accessibility; False if the element does not support it."""
    try:
        if clear:
            await ctx.run_engine(ctx.engine.set_value, target.handle, text)
            return True
        return bool(await ctx.run_engine(ctx.engine.insert_text, target.handle, text))
    except AXPilotError as err:
        if err.code is ErrorCode.ELEMENT_NOT_ACTIONABLE:
            return False
        raise


async def _type(
    ctx: ServerContext,
    target: Target | None,
    text: str,
    *,
    clear: bool,
    strategy: TypeStrategy,
    app: AppInfo | None,
) -> tuple[str, list[str]]:
    attempts: list[str] = []
    ax_first = strategy == "ax_value" or (strategy == "auto" and not _prefers_keystrokes(app))
    if target is not None and target.handle is not None and ax_first:
        attempts.append("ax_value")
        if await _ax_text(ctx, target, text, clear=clear):
            return "ax_value", attempts
        if strategy == "ax_value":
            raise _not_actionable(target, "set the text of", attempts, ["the element is not editable"])
    if target is not None:
        attempts.append(await _focus(ctx, target))
    elif app is not None:
        await activate_app(ctx, app)
    if clear:
        await require_keyboard(ctx)
        await _press(ctx, KeyCombo("a", frozenset({"cmd"})))
        await _press(ctx, KeyCombo("delete"))
    if not text:
        return "clear", attempts
    if strategy == "paste" or (strategy == "auto" and len(text) > PASTE_THRESHOLD):
        attempts.append("paste")
        await _paste(ctx, text)
        return "paste", attempts
    attempts.append("keystrokes")
    await type_keystrokes(ctx, text)
    return "keystrokes", attempts


@tool(
    "type_text",
    title="Type text",
    description=TYPE_DESCRIPTION,
    input_action=True,
    windows_mcp_name="Type",
)
def type_text_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``type_text`` tool."""

    async def type_text(
        *,
        text: Annotated[str, Field(description="Text to type (may contain newlines).", max_length=20_000)],
        ref: OptionalRefArg = None,
        snapshot_id: SnapshotIdArg = None,
        clear: Annotated[bool, Field(description="Replace the field's current content.")] = False,
        submit: Annotated[bool, Field(description="Press Return after typing.")] = False,
        strategy: Annotated[
            TypeStrategy, Field(description="How to enter the text (default auto).")
        ] = "auto",
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        if not text and not clear and not submit:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "Nothing to type: text is empty.",
                hint="Pass text, or clear=true to empty the field.",
            )
        target: Target | None = None
        if ref is not None:
            target = await resolve_target(ctx, ref=ref, snapshot_id=snapshot_id, x=None, y=None)
            ctx.policy.check_secure_target(bool(target.el is not None and target.el.secure))
            if target.el is not None and not target.el.enabled:
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{target.describe()} is disabled and cannot take text."
                )
            app = target.app
        else:
            app, _ = await keystroke_target(ctx)
        before = await snapshot_before(ctx, target, report) if target is not None else None
        if before is None and report == "diff" and app is not None:
            with contextlib.suppress(AXPilotError):
                before = await ctx.take_snapshot(app=_app_key(app), store=False)
        if target is None:
            await check_focus_not_secure(ctx, app, before)
        warnings: list[str] = []
        if target is not None and target.el is not None and target.el.role not in TEXT_ROLES:
            warnings.append(
                f"{target.describe()} is not a text field; the keystrokes went to whatever it focuses."
            )
        used, attempts = await _type(ctx, target, text, clear=clear, strategy=strategy, app=app)
        if submit:
            await require_keyboard(ctx)
            await _press(ctx, KeyCombo("return"))
            attempts.append("return")
        observation = await observe_after(ctx, app=app, before=before, report=report)
        where = f" into {target.describe()}" if target is not None else ""
        what = f"{len(text)} characters" if text else "cleared the field"
        summary = f"typed {what}{where} via {used}" + (" and pressed Return" if submit else "")
        return action_result(
            "type_text",
            summary,
            observation,
            strategy=used,
            attempts=attempts,
            target=target,
            warnings=warnings,
        )

    return type_text


# --------------------------------------------------------------------------- keys
KEYS_DESCRIPTION = """\
Press keyboard shortcuts or keys, e.g. "cmd+s", "cmd+shift+t", "escape", "tab", or a sequence
"cmd+a delete". Keys go to the frontmost app (pass app to bring one forward first).
Names: cmd, option, ctrl, shift, fn; return, escape, tab, space, delete, forward_delete, up, down,
left, right, home, end, page_up, page_down, f1-f20, plus, minus, any character.
Params: keys, repeat (1-50), app, report. Prefer select_menu for menu commands (it works without
knowing the shortcut). Errors: SECURE_INPUT_ACTIVE, INVALID_ARGUMENT (unknown key),
BLOCKED_BY_POLICY, USER_ACTIVE, PAUSED_BY_USER."""


@tool(
    "press_keys",
    title="Press keys",
    description=KEYS_DESCRIPTION,
    input_action=True,
    windows_mcp_name="Shortcut",
)
def press_keys_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``press_keys`` tool."""

    async def press_keys(
        keys: Annotated[
            str,
            Field(
                description='A combo or a space-separated sequence, e.g. "cmd+shift+t" or "cmd+a delete".',
                min_length=1,
                max_length=400,
            ),
        ],
        repeat: Annotated[
            int, Field(description="Press the whole sequence this many times.", ge=1, le=KEY_REPEAT_LIMIT)
        ] = 1,
        app: AppArg = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        combos = parse_sequence(keys)
        if app is not None:
            wanted = await ctx.resolve_app(app)
            ctx.policy.check_app(wanted, kind="input")
            await activate_app(ctx, wanted)
            target_app, _ = await keystroke_target(ctx)  # keys go to whatever is in front now
            if wanted is not None and (target_app is None or target_app.pid != wanted.pid):
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"Could not bring {wanted.name} to the front, so no keys were sent.",
                    hint="Activate it with the app tool (or click its window), then try again.",
                )
        else:
            target_app, _ = await keystroke_target(ctx)
        await require_keyboard(ctx)
        before = None
        if report == "diff" and target_app is not None:
            with contextlib.suppress(AXPilotError):
                before = await ctx.take_snapshot(app=_app_key(target_app), store=False)
        if produces_text(combos):
            await check_focus_not_secure(ctx, target_app, before)
        for _ in range(repeat):
            for combo in combos:
                ctx.control.ensure_running()
                await _press(ctx, combo)
        observation = await observe_after(ctx, app=target_app, before=before, report=report)
        pressed = " ".join(str(c) for c in combos)
        times = f" ×{repeat}" if repeat > 1 else ""
        app_name = f" in {target_app.name}" if target_app is not None else ""
        return action_result(
            "press_keys", f"pressed {pressed}{times}{app_name}", observation, strategy="keys"
        )

    return press_keys


# --------------------------------------------------------------------------- drag / move
DRAG_DESCRIPTION = """\
Drag from one element or point to another (move files, reorder items, resize, select a range).
Params: from_ref | from_x+from_y, to_ref | to_x+to_y, coordinate_space, screenshot_id,
duration_ms (default 300), modifiers (e.g. ["option"] to copy in Finder), report.
Both ends are checked against the safety policy. Errors: STALE_REF, INVALID_ARGUMENT,
BLOCKED_BY_POLICY, ELEMENT_NOT_ACTIONABLE, USER_ACTIVE, PAUSED_BY_USER."""


@tool("drag", title="Drag", description=DRAG_DESCRIPTION, input_action=True, windows_mcp_name="Drag")
def drag_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``drag`` tool."""

    async def drag(
        *,
        from_ref: OptionalRefArg = None,
        from_x: XArg = None,
        from_y: YArg = None,
        to_ref: OptionalRefArg = None,
        to_x: XArg = None,
        to_y: YArg = None,
        snapshot_id: SnapshotIdArg = None,
        coordinate_space: SpaceArg = "points",
        screenshot_id: ScreenshotIdArg = None,
        duration_ms: Annotated[int, Field(description="How long the drag takes.", ge=0, le=5000)] = 300,
        modifiers: ModifiersArg = [],  # noqa: B006 - pydantic copies defaults
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        start = await resolve_target(
            ctx, ref=from_ref, snapshot_id=snapshot_id, x=from_x, y=from_y,
            space=coordinate_space, screenshot_id=screenshot_id,
        )  # fmt: skip
        end = await resolve_target(
            ctx, ref=to_ref, snapshot_id=snapshot_id, x=to_x, y=to_y,
            space=coordinate_space, screenshot_id=screenshot_id,
        )  # fmt: skip
        before = await snapshot_before(ctx, start, report)
        await ensure_uncovered(ctx, start)
        await activate_app(ctx, start.app)
        await resolve_point(ctx, end.point.x, end.point.y)  # the drop lands on whatever is on top there
        await ctx.run_engine(
            functools.partial(
                ctx.engine.mouse_drag, start.point, end.point,
                duration_s=duration_ms / 1000, modifiers=frozenset(modifiers),
            ),
            timeout_s=ctx.engine_timeout_s + duration_ms / 1000,
        )  # fmt: skip
        observation = await observe_after(ctx, app=end.app or start.app, before=before, report=report)
        summary = f"dragged {start.describe()} to {end.describe()}"
        return action_result("drag", summary, observation, strategy="mouse", attempts=["mouse"], target=start)

    return drag


MOVE_DESCRIPTION = """\
Move the mouse pointer onto an element or point without clicking (shows tooltips, hover menus,
toolbar labels). Params: ref | x+y, coordinate_space, screenshot_id, duration_ms, report.
Errors: STALE_REF, INVALID_ARGUMENT, BLOCKED_BY_POLICY, USER_ACTIVE, PAUSED_BY_USER."""


@tool(
    "move_mouse",
    title="Move the mouse",
    description=MOVE_DESCRIPTION,
    input_action=True,
    windows_mcp_name="Move",
)
def move_mouse_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``move_mouse`` tool."""

    async def move_mouse(
        *,
        ref: OptionalRefArg = None,
        x: XArg = None,
        y: YArg = None,
        snapshot_id: SnapshotIdArg = None,
        coordinate_space: SpaceArg = "points",
        screenshot_id: ScreenshotIdArg = None,
        duration_ms: Annotated[int, Field(description="How long the movement takes.", ge=0, le=5000)] = 0,
        report: ReportArg = "none",
    ) -> Annotated[CallToolResult, ActionResult]:
        target = await resolve_target(
            ctx,
            ref=ref,
            snapshot_id=snapshot_id,
            x=x,
            y=y,
            space=coordinate_space,
            screenshot_id=screenshot_id,
        )
        before = await snapshot_before(ctx, target, report)
        await ctx.run_engine(
            functools.partial(ctx.engine.mouse_move, target.point, duration_s=duration_ms / 1000),
            timeout_s=ctx.engine_timeout_s + duration_ms / 1000,
        )
        observation = await observe_after(ctx, app=target.app, before=before, report=report)
        return action_result(
            "move_mouse", f"moved the pointer to {target.describe()}", observation,
            strategy="mouse", attempts=["mouse"], target=target,
        )  # fmt: skip

    return move_mouse


# --------------------------------------------------------------------------- set_value
SET_VALUE_DESCRIPTION = """\
Set a control to a value, the way its role expects:
- checkbox / switch / toggle / radio: "true" or "false" (on/off). Idempotent: nothing happens if
  it is already in that state.
- slider / stepper: a number (clamped to the control's range; a warning says so).
- disclosure triangle: "true" (expanded) or "false" (collapsed).
- text field / search field / text area / combo box / date field: the new text (replaces it).
- pop-up button: the title of the option to choose.
Params: ref, value (always a string), snapshot_id, report. The result says whether anything
changed. Errors: ELEMENT_NOT_ACTIONABLE (read-only or disabled), INVALID_ARGUMENT (e.g. not a
number for a slider), BLOCKED_BY_POLICY (secure fields), STALE_REF."""


def parse_bool(value: str) -> bool:
    """``"true"``/``"on"``/``"1"``… → True, ``"false"``/``"off"``/``"0"``… → False."""
    folded = value.strip().casefold()
    if folded in _TRUE:
        return True
    if folded in _FALSE:
        return False
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Expected true or false, got {value!r}.",
        hint='Use "true"/"false" (also on/off, yes/no, 1/0).',
    )


def _is_on(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return str(value).strip().casefold() in _TRUE


def parse_number(value: str) -> float:
    try:
        number = float(value.strip())
    except ValueError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"Expected a number, got {value!r}.", hint='Pass e.g. "42" or "0.5".'
        ) from exc
    if not math.isfinite(number):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Expected a finite number, got {value!r}.")
    return number


@dataclass
class _SetOutcome:
    strategy: str
    changed: bool
    message: str
    warnings: list[str] = dataclasses.field(default_factory=list)


async def _current(ctx: ServerContext, target: Target) -> Any:
    node = await ctx.run_engine(ctx.engine.refresh_element, target.handle)
    return node.expanded if target.el is not None and target.el.role in DISCLOSURE_ROLES else node.value


async def _set_toggle(ctx: ServerContext, target: Target, desired: bool) -> _SetOutcome:
    current = await _current(ctx, target)
    if _is_on(current) == desired:
        return _SetOutcome("none", False, f"already {'on' if desired else 'off'}")
    if target.el is not None and target.el.role == "radio" and not desired:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{target.describe()} is a radio button; turn it off by choosing another option of its group.",
        )
    strategy = "ax:AXPress"
    if target.el is not None and "AXPress" in target.el.actions:
        await ctx.run_engine(ctx.engine.perform_action, target.handle, "AXPress")
    else:
        strategy = "ax_value"
        await ctx.run_engine(ctx.engine.set_value, target.handle, desired)
    after = await _current(ctx, target)
    if _is_on(after) != desired:
        raise _not_actionable(
            target, f"turn {'on' if desired else 'off'}", [strategy], ["its state did not change"]
        )
    return _SetOutcome(strategy, True, f"turned {'on' if desired else 'off'}")


async def _range(ctx: ServerContext, target: Target) -> tuple[float | None, float | None]:
    try:
        attributes = await ctx.run_engine(ctx.engine.element_attributes, target.handle)
    except AXPilotError:
        return None, None

    def number(key: str) -> float | None:
        value = attributes.get(key)
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    return number("AXMinValue"), number("AXMaxValue")


async def _set_number(ctx: ServerContext, target: Target, wanted: float) -> _SetOutcome:
    warnings: list[str] = []
    low, high = await _range(ctx, target)
    clamped = wanted
    if low is not None and clamped < low:
        clamped = low
    if high is not None and clamped > high:
        clamped = high
    if clamped != wanted:
        warnings.append(f"{wanted:g} is outside the range {low:g}…{high:g}; used {clamped:g}.")
    value: float | int = int(clamped) if clamped.is_integer() else clamped
    try:
        await ctx.run_engine(ctx.engine.set_value, target.handle, value)
        return _SetOutcome("ax_value", True, f"set to {value:g}", warnings)
    except AXPilotError as err:
        if err.code is not ErrorCode.ELEMENT_NOT_ACTIONABLE or target.el is None:
            raise
        if not {"AXIncrement", "AXDecrement"} <= set(target.el.actions):
            raise
    return await _step_to(ctx, target, clamped, warnings)


async def _step_to(ctx: ServerContext, target: Target, wanted: float, warnings: list[str]) -> _SetOutcome:
    """Reach ``wanted`` with AXIncrement/AXDecrement (steppers whose value is read-only)."""
    current = float(await _current(ctx, target) or 0)
    steps = 0
    while current != wanted and steps < STEP_LIMIT:
        ctx.control.ensure_running()
        action = "AXIncrement" if current < wanted else "AXDecrement"
        await ctx.run_engine(ctx.engine.perform_action, target.handle, action)
        new = float(await _current(ctx, target) or 0)
        steps += 1
        if (
            new == current
            or (action == "AXIncrement" and new > wanted)
            or (action == "AXDecrement" and new < wanted)
        ):
            current = new
            break
        current = new
    if current != wanted:
        warnings.append(f"Stopped at {current:g} after {steps} steps (the control's step size).")
    return _SetOutcome("ax:step", steps > 0, f"stepped to {current:g}", warnings)


async def set_control(ctx: ServerContext, target: Target, value: str) -> _SetOutcome:
    """Set the control at ``target`` to ``value`` according to its role (see SET_VALUE_DESCRIPTION)."""
    el = target.el
    if el is None or target.handle is None:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            "set_value needs an accessibility element (an e-ref), not OCR text or a point.",
        )
    if not el.enabled:
        raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{target.describe()} is disabled.")
    role = el.role
    if role in TOGGLE_ROLES or role in DISCLOSURE_ROLES:
        return await _set_toggle(ctx, target, parse_bool(value))
    if role in NUMERIC_ROLES:
        return await _set_number(ctx, target, parse_number(value))
    if role in TEXT_ROLES or role == "secure":
        ctx.policy.check_secure_target(el.secure)
    await ctx.run_engine(ctx.engine.set_value, target.handle, value)
    if el.secure:
        return _SetOutcome("ax_value", True, "set (secure field)")
    after = await _current(ctx, target)
    if role == "popup" and str(after).casefold() != value.casefold():
        raise _not_actionable(target, f"choose {value!r} in", ["ax_value"], [f"it shows {after!r}"])
    return _SetOutcome("ax_value", True, f"set to {quote(str(after) if after is not None else value, 60)}")


@tool(
    "set_value",
    title="Set a control's value",
    description=SET_VALUE_DESCRIPTION,
    input_action=True,
    idempotent=True,
    windows_mcp_name="SetValue",
)
def set_value_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``set_value`` tool."""

    async def set_value(
        ref: RefArg,
        value: Annotated[
            str, Field(description='The value as text: "true"/"false", a number, or text.', max_length=20_000)
        ],
        snapshot_id: SnapshotIdArg = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        target = await resolve_target(ctx, ref=ref, snapshot_id=snapshot_id, x=None, y=None, need_handle=True)
        before = await snapshot_before(ctx, target, report)
        outcome = await set_control(ctx, target, value)
        observation = await observe_after(ctx, app=target.app, before=before, report=report)
        return action_result(
            "set_value", f"{target.describe()}: {outcome.message}", observation,
            strategy=outcome.strategy, attempts=[outcome.strategy], target=target,
            message=outcome.message, warnings=outcome.warnings,
        )  # fmt: skip

    return set_value


# --------------------------------------------------------------------------- perform_action
ACTION_DESCRIPTION = """\
Perform an accessibility action on an element: AXPress, AXShowMenu (context menu), AXIncrement,
AXDecrement, AXConfirm, AXCancel, AXRaise (bring a window forward), AXPick, AXOpen…
The element's supported actions are listed in its snapshot entry and in inspect.
Params: ref, action, snapshot_id, report. Errors: ELEMENT_NOT_ACTIONABLE (unsupported action or
disabled element), STALE_REF, BLOCKED_BY_POLICY, INVALID_ARGUMENT."""


@tool(
    "perform_action",
    title="Perform an accessibility action",
    description=ACTION_DESCRIPTION,
    input_action=True,
    windows_mcp_name="PerformAction",
)
def perform_action_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``perform_action`` tool."""

    async def perform_action(
        ref: RefArg,
        action: Annotated[
            str, Field(description='AX action name, e.g. "AXShowMenu".', min_length=4, max_length=42)
        ],
        snapshot_id: SnapshotIdArg = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        if not _AX_ACTION.match(action):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{action!r} is not an accessibility action name.",
                hint='Action names look like "AXPress" or "AXShowMenu"; see the element\'s actions.',
            )
        target = await resolve_target(ctx, ref=ref, snapshot_id=snapshot_id, x=None, y=None, need_handle=True)
        before = await snapshot_before(ctx, target, report)
        await ctx.run_engine(ctx.engine.perform_action, target.handle, action)
        observation = await observe_after(ctx, app=target.app, before=before, report=report)
        return action_result(
            "perform_action", f"{action} on {target.describe()}", observation,
            strategy=f"ax:{action}", attempts=[f"ax:{action}"], target=target,
        )  # fmt: skip

    return perform_action


# --------------------------------------------------------------------------- menus
MENU_DESCRIPTION = """\
Use an app's menu bar: list its menus, or choose a command by path, e.g. ["File", "Export…"] or
["Format", "Font", "Bold"]. Matching ignores case and "…" vs "..." and accepts unambiguous
prefixes. This is often the most reliable way to run a command in a Mac app.
extras=true uses the status items at the right of the menu bar instead (Wi-Fi, Battery, an app's
icon; the system ones belong to Control Center, the default app then): a one-title path clicks the
item, opening its menu or panel (take a snapshot of that app to see a panel).
Params: path (omit to list the menus), app (default: the frontmost app), extras, depth (when
listing), report. Errors: MENU_NOT_FOUND (with the available titles), ELEMENT_NOT_ACTIONABLE
(disabled item), APP_NOT_FOUND, BLOCKED_BY_POLICY."""


CONTROL_CENTER = "com.apple.controlcenter"
"""Owner of the system status items (Wi-Fi, Battery, the clock…) since macOS 11."""


class MenuItemModel(BaseModel):
    """One menu or menu item."""

    model_config = ConfigDict(extra="forbid")

    title: str
    enabled: bool = True
    checked: bool = False
    shortcut: str | None = None
    children: list["MenuItemModel"] = Field(default_factory=list)


class MenuResult(OutputModel):
    """Result of select_menu: the chosen path, or the menu tree when listing."""

    app: str | None = None
    selected: list[str] = Field(default_factory=list, description="Exact titles chosen, when selecting.")
    menus: list[MenuItemModel] = Field(default_factory=list, description="Menu tree, when listing.")
    diff: DiffModel | None = None
    snapshot_id: str | None = None


def _menu_model(node: MenuNode, depth: int) -> MenuItemModel:
    children = [_menu_model(child, depth - 1) for child in node.children] if depth > 0 else []
    return MenuItemModel(
        title=node.title,
        enabled=node.enabled,
        checked=node.checked,
        shortcut=node.shortcut,
        children=children,
    )


def _menu_lines(items: Sequence[MenuItemModel], indent: int = 0) -> list[str]:
    lines: list[str] = []
    for item in items:
        flags = [f for f, on in (("disabled", not item.enabled), ("checked", item.checked)) if on]
        extra = f"  {item.shortcut}" if item.shortcut else ""
        extra += f"  ({', '.join(flags)})" if flags else ""
        lines.append(f"{'  ' * indent}{item.title or '—'}{extra}")
        lines.extend(_menu_lines(item.children, indent + 1))
    return lines


@tool(
    "select_menu",
    title="Use the menu bar",
    description=MENU_DESCRIPTION,
    input_action=True,
    windows_mcp_name="SelectMenu",
)
def select_menu_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``select_menu`` tool."""

    async def select_menu(
        path: Annotated[
            list[str],
            Field(
                description='Menu titles from the menu bar down, e.g. ["File", "Save As…"]. Empty = list.',
                max_length=8,
            ),
        ] = [],  # noqa: B006 - pydantic copies defaults
        app: AppArg = None,
        extras: Annotated[
            bool, Field(description="Use the menu bar's status items (default app: Control Center).")
        ] = False,
        depth: Annotated[
            int, Field(description="Levels to list (1 = top-level menus only).", ge=1, le=4)
        ] = 2,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, MenuResult]:
        if extras and app is None:
            app = CONTROL_CENTER
        target_app = await ctx.resolve_app(app)
        key = _app_key(target_app) if app is not None else None
        if not path:
            ctx.policy.check_app(target_app, kind="observe")
            root = await ctx.run_engine(functools.partial(ctx.engine.menu_bar, key, depth, extras=extras))
            menus = [_menu_model(child, depth - 1) for child in root.children]
            name = target_app.name if target_app is not None else "the frontmost app"
            text = f"menus of {name}:\n" + "\n".join(_menu_lines(menus))
            return ok_result(text, MenuResult(app=target_app.name if target_app else None, menus=menus))
        if any(not title.strip() for title in path):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Menu titles in path must not be empty.")
        # a menu command acts on the app's front window
        ctx.policy.check_app(target_app, kind="input", window_title=await front_window_title(ctx, target_app))
        before = None
        if report == "diff" and target_app is not None:
            with contextlib.suppress(AXPilotError):
                before = await ctx.take_snapshot(app=_app_key(target_app), store=False)
        selected = await ctx.run_engine(
            functools.partial(ctx.engine.menu_select, key, list(path), extras=extras)
        )
        observation = await observe_after(ctx, app=target_app, before=before, report=report)
        model = MenuResult(
            app=target_app.name if target_app else None,
            selected=list(selected),
            diff=observation.diff,
            snapshot_id=observation.snapshot_id,
        )
        text = f"chose {' > '.join(selected)}"
        if observation.text:
            text += f"\n{observation.text}"
        return ok_result(text, model, warnings=observation.warnings)

    return select_menu


# --------------------------------------------------------------------------- fill_form
FORM_DESCRIPTION = """\
Fill several fields of a form in one call: text fields, checkboxes ("true"/"false"), pop-ups
(option title), sliders (number). Every ref is checked before anything is changed; fields are then
filled in order, and on the first failure the result says which fields were already filled.
Params: fields ([{"ref": "e4", "value": "Ada"}, …], up to 50), snapshot_id, report.
Errors: STALE_REF (before anything changes), BLOCKED_BY_POLICY, ELEMENT_NOT_ACTIONABLE (with the
fields done so far), USER_ACTIVE, PAUSED_BY_USER."""


class FieldValue(BaseModel):
    """One field to fill."""

    model_config = ConfigDict(extra="forbid")

    ref: Annotated[str, Field(description='Element ref, e.g. "e4".', min_length=2, max_length=32)]
    value: Annotated[str, Field(description="Value as text (see set_value).", max_length=20_000)]


@tool(
    "fill_form",
    title="Fill a form",
    description=FORM_DESCRIPTION,
    input_action=True,
    windows_mcp_name="MultiEdit",
)
def fill_form_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``fill_form`` tool."""

    async def fill_form(
        fields: Annotated[
            list[FieldValue],
            Field(description="Fields to fill, in order.", min_length=1, max_length=FORM_LIMIT),
        ],
        snapshot_id: SnapshotIdArg = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        targets: list[Target] = []
        for entry in fields:  # resolve and check everything before changing anything
            target = await resolve_target(
                ctx, ref=entry.ref, snapshot_id=snapshot_id, x=None, y=None, need_handle=True
            )
            if target.el is not None and target.el.secure:
                ctx.policy.check_secure_target(True)
            targets.append(target)
        before = await snapshot_before(ctx, targets[0], report)
        done: list[str] = []
        warnings: list[str] = []
        for entry, target in zip(fields, targets, strict=True):
            ctx.control.ensure_running()
            try:
                outcome = await set_control(ctx, target, entry.value)
            except AXPilotError as err:
                raise AXPilotError(
                    err.code,
                    f"Filled {len(done)} of {len(fields)} fields; {entry.ref} failed: {err.message}",
                    hint=err.hint,
                    details={**err.details, "completed": done, "failed": entry.ref},
                ) from err
            warnings.extend(outcome.warnings)
            done.append(entry.ref)
        observation = await observe_after(ctx, app=targets[0].app, before=before, report=report)
        return action_result(
            "fill_form", f"filled {len(done)} fields: {', '.join(done)}", observation,
            strategy="set_value", attempts=["set_value"], target=targets[0], warnings=warnings,
        )  # fmt: skip

    return fill_form
