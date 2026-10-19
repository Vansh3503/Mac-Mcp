"""Navigation tools: get around an app by scrolling (PLAN §6.9).

``explore`` maps an unfamiliar app in one call. ``find`` searches a window and, with
``scroll=true``, scrolls its list or page until the element appears. ``scroll`` scrolls one
area by lines, pages or to an edge, or scrolls an element into view. ``read`` collects the
full content of a long list, table, document or web page, joining pages without repeats.

All scrolling goes through one loop (:class:`_Scroller`): scroll, let the UI settle,
re-snapshot the window (refs are carried, so elements that stay on screen keep their refs)
and compare :func:`~axpilot.perception.navigate.page_signature` before and after. An
unchanged signature means the area did not move: the end was reached. The loop measures how
far the content actually moved, so a "page" stays a page whatever the app's line height.

Scrolling is input: every scroll applies the ``input`` policy (the Terminal rule, deny
lists) and holds the input lock (``scroll`` through the guard; ``find`` and ``read``, which are
read-only tools, take one input turn per scroll, so the user can take over between scrolls).
Loops are bounded by a scroll count and a deadline (≤ 60 s): when one runs out, the results
so far come back with ``complete=false`` and a warning instead of an error. Intermediate
snapshots are not stored (a long read must not evict the agent's snapshots); the final one is.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import math
import statistics
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import anyio
from mcp_types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, MenuNode, Point, Rect, WindowInfo
from axpilot.perception.diff import diff_snapshots
from axpilot.perception.find import find_elements
from axpilot.perception.format import diff_payload, format_diff, format_snapshot, quote, snapshot_payload
from axpilot.perception.model import Element, Snapshot, SnapshotMode
from axpilot.perception.navigate import (
    COLLECTION_ROLES,
    CONTAINER_ROLES,
    Item,
    collect_items,
    descendants,
    overlap_length,
    page_signature,
    scroll_container_for,
    viewport_of,
)
from axpilot.perception.resolve import live_handle
from axpilot.perception.store import SnapshotStore
from axpilot.perception.vision import is_vision_element
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import (
    ActionResult,
    AppArg,
    AppRefModel,
    DiffModel,
    ElementBriefModel,
    OutputModel,
    SnapshotIdArg,
    SnapshotModel,
    WindowIdArg,
    WindowModel,
    WindowRefModel,
)
from axpilot.server.tools.input import (
    ReportArg,
    ScreenshotIdArg,
    SpaceArg,
    XArg,
    YArg,
    element_brief,
    resolve_point,
)
from axpilot.server.tools.observe import app_profile_of

__all__ = ["DEFAULT_LINE_POINTS", "MAX_SCROLLS", "NAV_DEADLINE_MAX_S"]

MAX_SCROLLS = 50
"""Upper bound on scrolls in one call of any navigation tool."""
NAV_DEADLINE_MAX_S = 60.0
"""No navigation loop runs longer than this."""
NAV_DEADLINE_DEFAULT_S = 30.0
"""Default time budget of ``find`` and ``read`` (well under typical client timeouts)."""
DEFAULT_LINE_POINTS = 10.0
"""Assumed height of one scroll "line" until it is measured (NSScrollView's default)."""
PAGE_FRACTION = 0.8
"""A page scroll moves this share of the visible area, so successive pages overlap."""
PROBE_LINES = 3
"""The first page of ``find``/``read`` in an app starts with a scroll this small, to measure the
line height."""
MAX_STEP_LINES = 2000
"""Largest single scroll (jumps to an edge double until they reach it)."""
READ_MAX_PAGES = 30
READ_MAX_CHARS = 60_000
"""``read`` returns at most this much text (the rest is cut, with ``truncated=true``)."""
FIND_FOUND_SCORE = 0.48
"""A match this good (the text is a substring of the name or value, or better) counts as found
and stops scrolling; weaker matches (some of the words) are returned but keep the search going."""
FIND_LIMIT_MAX = 50
EXPLORE_LIST_MAX = 25
"""``explore`` lists at most this many items per section (toolbar, tabs, sidebar…)."""
RESTORE_GRACE_S = 10.0
"""``read(restore=true)`` may scroll back for this long, even after the read used up its time."""
_VISIBLE_SHARE = 0.98
_EDGE_TOLERANCE = 1.0

Axis = Literal["x", "y"]
Direction = Literal["up", "down", "left", "right"]
ScrollUnit = Literal["lines", "pages"]
ScrollTo = Literal["top", "bottom"]
ReadMode = Literal["auto", "rows", "text"]

# Errors that end a loop early with partial results (after at least one scroll); any other
# error (policy, permissions, a pause) is raised.
_PARTIAL_CODES = frozenset(
    {
        ErrorCode.TIMEOUT,
        ErrorCode.USER_ACTIVE,
        ErrorCode.BUSY,
        ErrorCode.APP_NOT_FOUND,
        ErrorCode.WINDOW_NOT_FOUND,
        ErrorCode.STALE_REF,
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
    }
)
_GONE_CODES = frozenset({ErrorCode.APP_NOT_FOUND, ErrorCode.WINDOW_NOT_FOUND})

# Measured line heights, per session (snapshot store) and app: a "page" in a later call uses
# what an earlier call measured.
_LINE_POINTS: "weakref.WeakKeyDictionary[SnapshotStore, dict[str, float]]" = weakref.WeakKeyDictionary()

ContainerArg = Annotated[
    str | None,
    Field(
        description="Ref of a scroll area, list, table, web area or text area (or of an element inside "
        'one), e.g. "e13".',
        min_length=2,
        max_length=32,
    ),
]
TimeoutArg = Annotated[
    int,
    Field(
        description="Time budget in milliseconds; partial results come back when it runs out.",
        ge=1000,
        le=60_000,
    ),
]


# =========================================================================== output models
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindMatchModel(ElementBriefModel):
    """One match of ``find``."""

    score: float = Field(description="Match quality 0…1 (≥ 0.48: the text is part of the name, or better).")


class FindModel(OutputModel):
    """Output of ``find``."""

    found: bool = Field(description="True if a good match exists (see score).")
    matches: list[FindMatchModel]
    count: int
    snapshot_id: str = Field(description="Snapshot the match refs belong to.")
    scrolls: int = Field(default=0, description="Scrolls performed while searching.")
    at_end: bool = Field(default=False, description="True if scrolling reached the end of the area.")
    searched_all: bool = Field(
        default=False, description="True if the whole scrollable content was searched (no more to look at)."
    )
    container: ElementBriefModel | None = Field(default=None, description="The area that was scrolled.")


class ScrollModel(ActionResult):
    """Output of ``scroll``."""

    moved: bool = Field(description="True if the content moved.")
    at_end: bool = Field(
        description="True if the area cannot scroll further in that direction (the end, or the top)."
    )
    scrolls: int = Field(default=0, description="Scroll events sent (an edge jump may take several).")
    container: ElementBriefModel | None = Field(default=None, description="The area that was scrolled.")
    visible: bool | None = Field(
        default=None, description="scroll_into_view: True if the element is now visible."
    )


class MenuSummaryModel(_Model):
    """A top-level menu of the menu bar."""

    title: str
    items: int = Field(description="Number of items (separators excluded).")
    enabled: bool = True


class ScrollAreaModel(ElementBriefModel):
    """A scrollable area found by ``explore``."""

    rows: int | None = Field(
        default=None, description="Rows of a list/table/outline (all of them, not only visible)."
    )
    scrollable: bool = False


class ExploreModel(OutputModel):
    """Output of ``explore``: a map of an app."""

    app: AppRefModel
    window: WindowRefModel | None = None
    snapshot_id: str = Field(description="Snapshot the refs below belong to.")
    app_kind: str | None = None
    ax_quality: Literal["good", "partial", "poor"] | None = None
    strategies: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    windows: list[WindowModel] = Field(default_factory=list)
    menus: list[MenuSummaryModel] = Field(
        default_factory=list, description="Top-level menus (use select_menu)."
    )
    toolbar: list[ElementBriefModel] = Field(default_factory=list)
    tabs: list[ElementBriefModel] = Field(default_factory=list)
    sections: list[ElementBriefModel] = Field(default_factory=list, description="Sidebar / outline entries.")
    scroll_areas: list[ScrollAreaModel] = Field(default_factory=list)
    dialogs: list[ElementBriefModel] = Field(
        default_factory=list, description="Open sheets, dialogs, popovers."
    )
    focused: ElementBriefModel | None = None
    elements: int = Field(description="Interactive elements in the window (see snapshot for all of them).")


class ReadModel(OutputModel):
    """Output of ``read``."""

    container: ElementBriefModel | None = Field(
        default=None, description="What was read (None: the whole window)."
    )
    snapshot_id: str
    mode: Literal["rows", "text"]
    lines: list[str] = Field(description="The content, one row or text line per entry.")
    content: str = Field(description="The lines joined with newlines.")
    count: int
    pages: int = Field(description="Screens of content collected.")
    scrolls: int = 0
    complete: bool = Field(description="True if everything from the top to the end was collected.")
    at_end: bool = Field(description="True if the end of the content was reached.")
    truncated: bool = Field(
        default=False, description=f"True if the text was cut at {READ_MAX_CHARS} characters."
    )
    restored: bool | None = Field(
        default=None, description="restore=true: whether the old position was restored."
    )


# =========================================================================== geometry
def _pos(rect: Rect, axis: Axis) -> float:
    return rect.y if axis == "y" else rect.x


def _size(rect: Rect, axis: Axis) -> float:
    return rect.h if axis == "y" else rect.w


def _content_frame(snap: Snapshot, container: Element | None, axis: Axis) -> tuple[Rect, Rect] | None:
    """``(content frame, viewport)`` when ``container``'s frame spans its whole content (a table
    or outline inside a clip view reports its full height), else ``None``."""
    if container is None or container.frame is None:
        return None
    viewport = viewport_of(snap, container)
    if viewport is None or _size(container.frame, axis) <= _size(viewport, axis) + _EDGE_TOLERANCE:
        return None
    return container.frame, viewport


def edges(snap: Snapshot, container: Element | None, axis: Axis = "y") -> tuple[bool | None, bool | None]:
    """``(at start, at end)`` of ``container``'s content along ``axis``, or ``None`` each when
    the snapshot cannot tell (only containers that report their content frame can)."""
    found = _content_frame(snap, container, axis)
    if found is None:
        return None, None
    content, viewport = found
    at_start = _pos(content, axis) >= _pos(viewport, axis) - _EDGE_TOLERANCE
    at_end = (
        _pos(content, axis) + _size(content, axis)
        <= _pos(viewport, axis) + _size(viewport, axis) + _EDGE_TOLERANCE
    )
    return at_start, at_end


def displacement(
    before: Snapshot, c_before: Element | None, after: Snapshot, c_after: Element | None, axis: Axis
) -> float | None:
    """How far the content of a container moved between two snapshots, in points: positive when
    it moved toward its start (the view scrolled down/right). ``None`` if it cannot be measured.

    Uses the container's content frame when it reports one, else the median movement of the
    elements inside it that both snapshots list under the same ref."""
    if c_before is None or c_after is None:
        return None
    frames = _content_frame(before, c_before, axis), _content_frame(after, c_after, axis)
    if frames[0] is not None and frames[1] is not None:
        return _pos(frames[0][0], axis) - _pos(frames[1][0], axis)
    old = {
        el.ref: el.frame
        for el in descendants(before, c_before)
        if el.frame is not None and not is_vision_element(el)
    }
    deltas = [
        _pos(old[el.ref], axis) - _pos(el.frame, axis)
        for el in descendants(after, c_after)
        if el.ref in old and el.frame is not None
    ]
    return statistics.median(deltas) if deltas else None


def _visible(snap: Snapshot, el: Element, container: Element | None) -> bool:
    """True if ``el`` lies inside ``container``'s viewport (and the window): entirely, or as
    much of it as fits."""
    if el.frame is None:
        return False
    viewport = viewport_of(snap, container) if container is not None else None
    if viewport is None and snap.window is not None:
        viewport = snap.window.frame
    if viewport is None:
        return True
    clipped = el.frame.intersection(viewport)
    if clipped is None:
        return False
    fits = min(el.frame.w, viewport.w) * min(el.frame.h, viewport.h)
    return clipped.area >= _VISIBLE_SHARE * fits


def _container_at(snap: Snapshot, point: Point) -> Element | None:
    """The smallest container whose visible part contains ``point``."""
    best: tuple[float, Element] | None = None
    for el in snap.all_elements:
        if el.role not in CONTAINER_ROLES:
            continue
        viewport = viewport_of(snap, el)
        if viewport is not None and viewport.contains(point) and (best is None or viewport.area < best[0]):
            best = (viewport.area, el)
    return best[1] if best is not None else None


def _content_area(snap: Snapshot, area: Element | None) -> Element | None:
    """A scroll area's document, when it has one: the list, table, outline, web area or text
    area inside it (it has the name, the rows, and a frame spanning the whole content). The
    area itself otherwise."""
    if area is None or area.role != "scrollarea":
        return area
    best: Element | None = None
    for el in descendants(snap, area):
        if el.role not in CONTAINER_ROLES or el.role == "scrollarea":
            continue
        nearest = next((a for a in _ancestors(snap, el) if a.role in CONTAINER_ROLES), None)
        if nearest is area and (
            best is None or (el.frame.area if el.frame else 0) > (best.frame.area if best.frame else 0)
        ):
            best = el
    return best or area


def _scroll_ancestor(snap: Snapshot, el: Element) -> Element | None:
    """The container that scrolls ``el`` into view (not ``el`` itself)."""
    for candidate in _ancestors(snap, el):
        if candidate.role in CONTAINER_ROLES:
            return candidate
    container = scroll_container_for(snap, el)
    return container if container is not el else None


def _ancestors(snap: Snapshot, el: Element) -> list[Element]:
    chain: list[Element] = []
    seen = {el.ref}
    parent = el.parent_ref
    while parent is not None and parent not in seen:
        seen.add(parent)
        found = snap.element(parent)
        if found is None:
            break
        chain.append(found)
        parent = found.parent_ref
    return chain


def _label(el: Element | None) -> str:
    if el is None:
        return "the window"
    return f"{el.ref} {el.role} {quote(el.name, 40)}" if el.name else f"{el.ref} {el.role}"


def _app_key(app: AppInfo) -> str:
    return app.bundle_id or str(app.pid)


def _window_id(snap: Snapshot) -> int | None:
    return snap.window.window_id if snap.window is not None else None


def _check_input(ctx: ServerContext, snap: Snapshot) -> None:
    """Scrolling is input: apply the input policy to the snapshot's app and window."""
    ctx.policy.check_app(snap.app, kind="input", window_title=snap.window.title if snap.window else None)


# =========================================================================== the scroll loop
class _Stop(Exception):
    """Ends a navigation loop early; what was collected so far is returned with a warning."""


def _known_line_points(ctx: ServerContext, app: AppInfo) -> float | None:
    return _LINE_POINTS.get(ctx.snapshots, {}).get(_app_key(app))


def _remember_line_points(ctx: ServerContext, app: AppInfo, points: float) -> None:
    _LINE_POINTS.setdefault(ctx.snapshots, {})[_app_key(app)] = points


@dataclass
class _Scroller:
    """Scrolls one container and re-snapshots its window after every scroll."""

    ctx: ServerContext
    snap: Snapshot
    container: Element | None
    label: str
    """Tool name, for the input lock and progress messages."""
    deadline: float
    mode: SnapshotMode = "full"
    own_turns: bool = True
    """Take an input turn per scroll (tools the guard does not already hold one for)."""
    auto: bool = False
    """The container was picked automatically: pick again if it disappears."""
    at: Point | None = None
    """Scroll at this point instead of the container's center."""
    line_points: float = DEFAULT_LINE_POINTS
    calibrated: bool = False
    probed: bool = False
    scrolls: int = 0
    moved: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    """Net points scrolled (positive: down/right), for restoring the position."""
    measured: bool = True
    """False once a movement could not be measured (restoring is then approximate)."""

    def __post_init__(self) -> None:
        known = _known_line_points(self.ctx, self.snap.app)
        if known is not None:
            self.line_points, self.calibrated = known, True

    # ------------------------------------------------------------------ geometry
    def viewport(self) -> Rect | None:
        return viewport_of(self.snap, self.container)

    def point(self) -> Point:
        if self.at is not None:
            return self.at
        viewport = self.viewport()
        if viewport is None or viewport.area <= 0:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{_label(self.container)} has no visible area to scroll at.",
                hint="Pass x/y of a point inside the area to scroll, or another ref.",
            )
        return viewport.center

    def signature(self) -> str:
        return page_signature(self.snap, self.container)

    def edges(self, axis: Axis = "y") -> tuple[bool | None, bool | None]:
        return edges(self.snap, self.container, axis)

    def page_lines(self, axis: Axis = "y") -> float:
        viewport = self.viewport()
        size = _size(viewport, axis) if viewport is not None else 0.0
        if size <= 0:
            return 10.0
        return float(max(1, math.floor(size * PAGE_FRACTION / self.line_points)))

    def remaining(self) -> float:
        return self.deadline - self.ctx.clock()

    # ------------------------------------------------------------------ scrolling
    async def step(self, dx: float, dy: float) -> bool:
        """Scroll by ``dx``/``dy`` lines, re-snapshot, and return whether the content moved.

        Raises :class:`_Stop` when the time budget is used up."""
        if self.remaining() <= 0:
            raise _Stop("the time limit was reached")
        point = self.point()
        before, c_before, signature = self.snap, self.container, self.signature()
        after: Snapshot | None = None
        with anyio.move_on_after(self.remaining()):
            await self._scroll(point, dx, dy)
            await self.ctx.sleep(self.ctx.config.perception.settle_ms / 1000)
            after = await self.ctx.take_snapshot(
                app=_app_key(before.app),
                window_id=_window_id(before),
                mode=self.mode,
                store=False,
                previous=before,
            )
        if after is None:
            raise _Stop("the time limit was reached")
        c_after = self.follow(after)
        self.snap, self.container = after, c_after
        if c_before is not None and c_after is None:
            raise _Stop(f"{_label(c_before)} disappeared while scrolling")
        moved = page_signature(after, c_after) != signature
        self._measure(before, c_before, dx, dy, moved)
        await self.ctx.report_progress(self.scrolls, None, f"{self.label}: {self.scrolls} scrolls")
        return moved

    async def _scroll(self, point: Point, dx: float, dy: float) -> None:
        ctx = self.ctx
        if self.own_turns:
            async with ctx.control.input_turn(timeout_s=ctx.input_lock_timeout_s, label=self.label):
                await ctx.run_engine(ctx.engine.scroll, point, dx, dy)
        else:
            ctx.control.ensure_running()
            await ctx.run_engine(ctx.engine.scroll, point, dx, dy)
        self.scrolls += 1

    def follow(self, after: Snapshot) -> Element | None:
        """The container in the new snapshot: same ref (refs are carried), else re-picked."""
        if self.container is None:
            return None
        found = after.element(self.container.ref)
        if found is not None:
            return found
        if self.auto:
            return _content_area(after, scroll_container_for(after, None))
        name, role = self.container.name, self.container.ax_role
        same = [el for el in after.all_elements if name and el.name == name and el.ax_role == role]
        return same[0] if len(same) == 1 else None

    def _measure(self, before: Snapshot, c_before: Element | None, dx: float, dy: float, moved: bool) -> None:
        for axis, lines in (("x", dx), ("y", dy)):
            if not lines or not moved:
                continue
            delta = displacement(before, c_before, self.snap, self.container, axis)  # type: ignore[arg-type]
            if delta is None or delta * lines <= 0:
                self.measured = False
                self.moved[axis] += lines * self.line_points
                continue
            self.moved[axis] += delta
            ratio = abs(delta) / abs(lines)
            at_start, at_end = self.edges(axis)  # type: ignore[arg-type]
            clamped = at_end if lines > 0 else at_start
            if clamped is False:  # it certainly moved the full amount: trust the measurement
                self.line_points, self.calibrated = ratio, True
                _remember_line_points(self.ctx, self.snap.app, ratio)
            elif ratio > self.line_points:  # possibly cut short at an edge: only ever raise it
                self.line_points = ratio

    async def advance(self, sign: int, axis: Axis = "y") -> tuple[bool, bool]:
        """Scroll one page forward (``sign=+1``) or back; returns ``(moved, reached the edge)``.

        The first page in an app whose line height is not known yet starts with a small probe
        (:data:`PROBE_LINES`) that measures it, then scrolls the rest of the page, so the page
        does not skip content however tall the app's lines are."""
        if not self.calibrated and not self.probed:
            self.probed = True
            if not await self.step(*_vector(axis, sign * PROBE_LINES)):
                return False, True
            rest = self.page_lines(axis) - PROBE_LINES
            if rest < 1:
                return True, False
            return True, not await self.step(*_vector(axis, sign * rest))
        moved = await self.step(*_vector(axis, sign * self.page_lines(axis)))
        return moved, not moved

    async def to_edge(self, sign: int, max_scrolls: int, axis: Axis = "y") -> bool:
        """Scroll to the start (``sign=-1``) or end (``+1``) with growing jumps. Returns True
        once the edge is reached."""
        lines = self.page_lines(axis)
        while True:
            at_start, at_end = self.edges(axis)
            if (at_end if sign > 0 else at_start) is True:
                return True
            if self.scrolls >= max_scrolls:
                return False
            if not await self.step(*_vector(axis, sign * lines)):
                return True
            lines = min(lines * 2, MAX_STEP_LINES)

    async def restore(self, original: Snapshot, container: Element | None) -> bool:
        """Scroll back by the net distance scrolled. True if the original page is back: the
        same content in the same place, or (when the area reports its content frame) within
        one line of the old position (scrolls move whole lines)."""
        for axis in ("y", "x"):
            lines = -self.moved[axis] / self.line_points
            while abs(lines) >= 0.5:
                step = max(-MAX_STEP_LINES, min(MAX_STEP_LINES, round(lines)))
                await self.step(*_vector(axis, step))  # type: ignore[arg-type]
                lines -= step
        if self.signature() == page_signature(original, container):
            return True
        for axis in ("y", "x"):
            before = _content_frame(original, container, axis)  # type: ignore[arg-type]
            now = _content_frame(self.snap, self.container, axis)  # type: ignore[arg-type]
            if before is None or now is None:
                if axis == "y":
                    return False
                continue
            if abs(_pos(before[0], axis) - _pos(now[0], axis)) > self.line_points + _EDGE_TOLERANCE:  # type: ignore[arg-type]
                return False
        return True


def _vector(axis: Axis, lines: float) -> tuple[float, float]:
    return (lines, 0.0) if axis == "x" else (0.0, lines)


def _new_scroller(
    ctx: ServerContext,
    snap: Snapshot,
    container: Element | None,
    *,
    label: str,
    timeout_s: float,
    **options: Any,
) -> _Scroller:
    deadline = ctx.clock() + min(max(timeout_s, 0.0), NAV_DEADLINE_MAX_S)
    return _Scroller(ctx=ctx, snap=snap, container=container, label=label, deadline=deadline, **options)


def _begin_scrolling(ctx: ServerContext, scroller: _Scroller) -> None:
    """Before a read-only tool scrolls: the input policy and the rate limit (once per call)."""
    _check_input(ctx, scroller.snap)
    ctx.limiter.check()


async def _guarded_loop(scroller: _Scroller, loop: Callable[[], Any], warnings: list[str]) -> bool:
    """Run ``loop``; on :class:`_Stop` or a partial-result error, add a warning and return
    False (stopped early). Errors before the first scroll, and every other error, propagate."""
    try:
        await loop()
    except _Stop as stop:
        warnings.append(f"Stopped after {scroller.scrolls} scrolls: {stop}.")
        return False
    except AXPilotError as err:
        if err.code not in _PARTIAL_CODES or scroller.scrolls == 0:
            raise
        warnings.append(f"Stopped after {scroller.scrolls} scrolls: {err.code.value}: {err.message}")
        return False
    return True


async def _fresh_snapshot(ctx: ServerContext, snap: Snapshot, mode: SnapshotMode) -> Snapshot:
    """A new snapshot of ``snap``'s window (refs carried). Its window or app being gone is ``STALE_REF``."""
    try:
        return await ctx.take_snapshot(
            app=_app_key(snap.app), window_id=_window_id(snap), mode=mode, store=False
        )
    except AXPilotError as err:
        if err.code in _GONE_CODES:
            raise AXPilotError(
                ErrorCode.STALE_REF,
                f"The window of snapshot {snap.snapshot_id} ({snap.app.name}) is no longer open.",
                hint="Take a new snapshot and use its refs.",
            ) from err
        raise


async def _element_now(
    ctx: ServerContext, ref: str, snapshot_id: str | None, mode: SnapshotMode
) -> tuple[Snapshot, Element]:
    """A fresh (unstored) snapshot of the window ``ref`` belongs to, and ``ref``'s element in it."""
    snap, el = ctx.snapshots.resolve(ref, snapshot_id)
    ctx.policy.check_app(snap.app, kind="observe", window_title=snap.window.title if snap.window else None)
    if is_vision_element(el):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{el.ref} is OCR text, not an accessibility element; it cannot be scrolled or read.",
            hint='Use the ref of a list, table, web area or text area (snapshot mode="interactive").',
        )
    fresh = await _fresh_snapshot(ctx, snap, mode)
    current = fresh.element(el.ref)
    if current is None:
        raise AXPilotError(
            ErrorCode.STALE_REF,
            f"{_label(el)} is no longer in its window (it is gone, or scrolled out of a list).",
            hint="Take a new snapshot and use its refs; find(text=…, scroll=true) scrolls to an element.",
        )
    return fresh, current


# =========================================================================== find
FIND_DESCRIPTION = """\
Find elements by text, role or identifier in an app window; with scroll=true, scroll its list or page
until a match appears (lazily loaded lists, long settings panes, feeds).
Use it instead of scrolling and snapshotting by hand. Params: text (fuzzy: exact > prefix > substring >
words), role (button, row, field… or an AX role), identifier (AXIdentifier); app / window_id; container
(ref of the list/page to search and scroll; default: the whole window, scrolling its largest scroll
area); scroll; max_scrolls (≤ 50); limit.
Returns matches (ref, role, name, score) from the final snapshot, found, scrolls, at_end and
searched_all. Not finding anything is not an error: found=false with what was searched.
Errors: INVALID_ARGUMENT, APP_NOT_FOUND, STALE_REF (container), BLOCKED_BY_POLICY, USER_ACTIVE."""


@dataclass(frozen=True, slots=True)
class _Query:
    text: str | None
    role: str | None
    identifier: str | None
    limit: int

    def describe(self) -> str:
        parts = [quote(self.text, 60)] if self.text else []
        if self.role:
            parts.append(f"role={self.role}")
        if self.identifier:
            parts.append(f"identifier={self.identifier}")
        return " ".join(parts)


def _search(snap: Snapshot, within: Element | None, query: _Query) -> list[tuple[Element, float]]:
    found = find_elements(
        snap, text=query.text, role=query.role, identifier=query.identifier, limit=max(query.limit * 4, 50)
    )
    if within is not None:
        inside = {el.ref for el in descendants(snap, within)} | {within.ref}
        found = [(el, score) for el, score in found if el.ref in inside]
    return found[: query.limit]


def _good(matches: Sequence[tuple[Element, float]]) -> bool:
    return any(score >= FIND_FOUND_SCORE for _, score in matches)


def _texts(snap: Snapshot, container: Element | None) -> set[str]:
    return {text for _, text in collect_items(snap, container)}


@dataclass
class _Scan:
    matches: list[tuple[Element, float]]
    found: bool = False
    at_end: bool = False
    searched_all: bool = False


async def _scan(scroller: _Scroller, within: Element | None, query: _Query, max_scrolls: int) -> _Scan:
    """Scroll down to the end looking for ``query``; then, if the area did not start at the top,
    from the top down to where it started."""
    first_page = _texts(scroller.snap, scroller.container)
    started_at_top = scroller.edges()[0] is True

    def search() -> list[tuple[Element, float]]:
        if within is None:
            return _search(scroller.snap, None, query)
        scope = scroller.snap.element(within.ref)
        return _search(scroller.snap, scope, query) if scope is not None else []

    result = _Scan(matches=search())
    while scroller.scrolls < max_scrolls:
        moved, reached = await scroller.advance(+1)
        result.matches = search()
        if _good(result.matches):
            result.found = True
            return result
        if not moved or reached or scroller.edges()[1] is True:
            result.at_end = True
            break
    if not result.at_end:
        return result
    if started_at_top:
        result.searched_all = True
        return result
    if not await scroller.to_edge(-1, max_scrolls):
        return result
    while True:
        result.matches = search()
        if _good(result.matches):
            result.found = True
            return result
        if _texts(scroller.snap, scroller.container) & first_page:
            result.searched_all = True
            return result
        if scroller.scrolls >= max_scrolls:
            return result
        moved, reached = await scroller.advance(+1)
        if not moved:
            result.searched_all = True
            return result
        if reached:
            result.matches = search()
            result.found = _good(result.matches)
            result.searched_all = True
            return result


def _match_model(el: Element, score: float) -> FindMatchModel:
    brief = element_brief(el)
    assert brief is not None
    return FindMatchModel(**brief.model_dump(), score=round(score, 3))


def _find_text(query: _Query, model: FindModel, container: Element | None) -> str:
    where = f" in {_label(container)}" if container is not None else ""
    scrolled = f" after {model.scrolls} scrolls" if model.scrolls else ""
    if not model.matches:
        reach = (
            " (searched everything)" if model.searched_all else " (reached the end)" if model.at_end else ""
        )
        return f"no match for {query.describe()}{where}{scrolled}{reach} · snapshot {model.snapshot_id}"
    head = "found" if model.found else "no good match; closest"
    lines = [f"{head} {query.describe()}{where}{scrolled} · snapshot {model.snapshot_id}"]
    for match in model.matches:
        name = f" {quote(match.name, 60)}" if match.name else ""
        center = f" ({round(match.center[0])},{round(match.center[1])})" if match.center else ""
        lines.append(f"{match.ref} {match.role}{name}{center}  score {match.score:.2f}")
    return "\n".join(lines)


@tool(
    "find",
    title="Find elements",
    description=FIND_DESCRIPTION,
    read_only=True,
    windows_mcp_name="Find",
)
def find_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``find`` tool."""

    async def find(
        *,
        text: Annotated[
            str | None, Field(description="Text to look for (fuzzy).", min_length=1, max_length=500)
        ] = None,
        role: Annotated[
            str | None,
            Field(description='Role filter: "button", "row", "field"… or "AXButton".', max_length=64),
        ] = None,
        identifier: Annotated[str | None, Field(description="Exact AXIdentifier.", max_length=256)] = None,
        app: AppArg = None,
        window_id: WindowIdArg = None,
        container: ContainerArg = None,
        snapshot_id: SnapshotIdArg = None,
        scroll: Annotated[bool, Field(description="Scroll the container until a match appears.")] = False,
        max_scrolls: Annotated[
            int, Field(description="Most scrolls to try (scroll=true).", ge=1, le=MAX_SCROLLS)
        ] = 20,
        limit: Annotated[int, Field(description="Most matches to return.", ge=1, le=FIND_LIMIT_MAX)] = 10,
        timeout_ms: TimeoutArg = int(NAV_DEADLINE_DEFAULT_S * 1000),
    ) -> Annotated[CallToolResult, FindModel]:
        query = _Query(text=text, role=role, identifier=identifier, limit=limit)
        if (
            not (text and text.strip())
            and not (role and role.strip())
            and not (identifier and identifier.strip())
        ):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "find needs at least one of text, role or identifier.",
                hint='Example: find(text="Save") or find(role="button", scroll=true).',
            )
        if container is not None and (app is not None or window_id is not None):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "container already names the window; do not combine it with app or window_id.",
            )
        warnings: list[str] = []
        within: Element | None = None
        if container is not None:
            snap, within = await _element_now(ctx, container, snapshot_id, "full")
            area = within if within.role in CONTAINER_ROLES else scroll_container_for(snap, within)
        else:
            snap = await ctx.take_snapshot(app=app, window_id=window_id, mode="full", store=False)
            area = scroll_container_for(snap, None)
        area = _content_area(snap, area)
        scan = _Scan(matches=_search(snap, within, query))
        scan.found = _good(scan.matches)
        scroller: _Scroller | None = None
        if not scan.found and scroll:
            if area is None:
                warnings.append("Nothing scrollable in this window; searched what is on screen.")
            else:
                scroller = _new_scroller(
                    ctx, snap, area, label="find", timeout_s=timeout_ms / 1000, auto=container is None
                )
                _begin_scrolling(ctx, scroller)
                outcome: list[_Scan] = []

                async def loop() -> None:
                    outcome.append(await _scan(scroller, within, query, max_scrolls))

                await _guarded_loop(scroller, loop, warnings)
                snap = scroller.snap
                if outcome:
                    scan = outcome[0]
                else:  # stopped early: search what is on screen now
                    scope = snap.element(within.ref) if within is not None else None
                    scan = _Scan(
                        matches=_search(snap, scope, query) if scope is not None or within is None else []
                    )
                    scan.found = _good(scan.matches)
                if not scan.found and not scan.searched_all and outcome:
                    warnings.append(
                        f"Searched {scroller.scrolls} scrolls without reaching everything; "
                        "raise max_scrolls to look further."
                    )
        ctx.snapshots.put(snap)
        model = FindModel(
            found=scan.found,
            matches=[_match_model(el, score) for el, score in scan.matches],
            count=len(scan.matches),
            snapshot_id=snap.snapshot_id,
            scrolls=scroller.scrolls if scroller is not None else 0,
            at_end=scan.at_end,
            searched_all=scan.searched_all,
            container=element_brief(scroller.container if scroller is not None else area if scroll else None),
        )
        shown = scroller.container if scroller is not None else None
        return ok_result(_find_text(query, model, shown), model, warnings=warnings)

    return find


# =========================================================================== scroll
SCROLL_DESCRIPTION = """\
Scroll a list, table, web page or text area: by lines or pages, to the top or bottom, or until an
element is visible.
Target: ref (a scroll area, or any element inside one), x/y (a point; coordinate_space / screenshot_id
as for click), or neither (the largest scroll area of app's window). direction (up|down|left|right),
amount + unit (lines|pages; a page is ~80% of the visible area), to (top|bottom), scroll_into_view
(ref of an element to bring into view).
Returns moved, at_end (cannot scroll further that way), scrolls, and what changed (report:
diff|snapshot|none) with the new snapshot_id. For long content prefer read (collects everything)
or find(scroll=true). Errors: STALE_REF, ELEMENT_NOT_ACTIONABLE (nothing scrollable),
BLOCKED_BY_POLICY, USER_ACTIVE, PAUSED_BY_USER, INVALID_ARGUMENT."""


@dataclass
class _ScrollOutcome:
    moved: bool
    at_end: bool
    message: str
    visible: bool | None = None
    strategy: str = "scroll"


async def _scroll_area(
    ctx: ServerContext,
    *,
    ref: str | None,
    snapshot_id: str | None,
    x: float | None,
    y: float | None,
    space: Any,
    screenshot_id: str | None,
    app: str | None,
) -> tuple[Snapshot, Element | None, Point | None, bool]:
    """``(snapshot, container, point to scroll at, auto-picked)`` for the scroll target; the input
    policy is checked."""
    if ref is not None:
        snap, el = await _element_now(ctx, ref, snapshot_id, "full")
        _check_input(ctx, snap)
        container = _content_area(snap, el if el.role in CONTAINER_ROLES else scroll_container_for(snap, el))
        if container is None:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{_label(el)} is not inside a scrollable area.",
                hint="Pass the ref of a list, table, scroll area or web area, or x/y of a point inside it.",
            )
        return snap, container, None, False
    if x is not None or y is not None:
        if x is None or y is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Give both x and y.")
        target = await resolve_point(ctx, x, y, space=space, screenshot_id=screenshot_id)
        if target.app is None:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"No app window is under ({x:g}, {y:g}).",
                hint="Use coordinates inside an app window, or a ref.",
            )
        snap = await ctx.take_snapshot(
            app=_app_key(target.app),
            window_id=target.window.window_id if target.window is not None else None,
            mode="full",
            store=False,
        )
        return snap, _content_area(snap, _container_at(snap, target.point)), target.point, False
    snap = await ctx.take_snapshot(app=app, mode="full", store=False)
    _check_input(ctx, snap)
    container = _content_area(snap, scroll_container_for(snap, None))
    if container is None:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{snap.app.name}'s window has no scrollable area.",
            hint="Pass x/y of the area to scroll, or the ref of a list, table or web area.",
        )
    return snap, container, None, True


def _axis_sign(direction: Direction) -> tuple[Axis, int]:
    return {"up": ("y", -1), "down": ("y", 1), "left": ("x", -1), "right": ("x", 1)}[direction]  # type: ignore[return-value]


async def _scroll_by(
    scroller: _Scroller, direction: Direction, amount: float, unit: ScrollUnit
) -> _ScrollOutcome:
    axis, sign = _axis_sign(direction)
    lines = amount * (scroller.page_lines(axis) if unit == "pages" else 1.0)
    lines = max(1.0, min(round(lines), MAX_STEP_LINES))
    moved = await scroller.step(*_vector(axis, sign * lines))
    at_start, at_end = scroller.edges(axis)
    edge = at_end if sign > 0 else at_start
    reached = (not moved) or edge is True
    what = f"{amount:g} {unit[:-1] if amount == 1 else unit}"
    message = (
        f"scrolled {_label(scroller.container)} {direction} by {what}"
        if moved
        else (f"{_label(scroller.container)} did not move {direction}")
    )
    return _ScrollOutcome(moved=moved, at_end=reached, message=message)


async def _scroll_to(scroller: _Scroller, to: ScrollTo) -> _ScrollOutcome:
    signature = scroller.signature()
    reached = await scroller.to_edge(1 if to == "bottom" else -1, MAX_SCROLLS)
    moved = scroller.signature() != signature
    where = "already at the" if not moved else "scrolled to the"
    message = f"{where} {to} of {_label(scroller.container)}"
    if not reached:
        message += f" (stopped after {scroller.scrolls} scrolls before reaching it)"
    return _ScrollOutcome(moved=moved, at_end=reached, message=message)


async def _into_view(
    ctx: ServerContext, ref: str, snapshot_id: str | None
) -> tuple[_Scroller, _ScrollOutcome, Snapshot]:
    """Bring ``ref``'s element into view: AXScrollToVisible, else scrolling its container toward it."""
    snap, el = await _element_now(ctx, ref, snapshot_id, "full")
    _check_input(ctx, snap)
    container = _scroll_ancestor(snap, el)
    scroller = _new_scroller(
        ctx, snap, container, label="scroll", timeout_s=NAV_DEADLINE_DEFAULT_S, own_turns=False
    )
    first = snap
    if _visible(snap, el, container):
        return scroller, _ScrollOutcome(False, False, f"{_label(el)} is already visible", visible=True), first
    handle = await ctx.run_engine(
        live_handle, ctx.engine, snap, el, timeout_s=ctx.capture_timeout_s + ctx.engine_timeout_s
    )
    ctx.control.ensure_running()
    if await ctx.run_engine(ctx.engine.scroll_to_visible, handle):
        await ctx.sleep(ctx.config.perception.settle_ms / 1000)
        scroller.snap = await _fresh_snapshot(ctx, snap, "full")
        scroller.container = scroller.follow(scroller.snap)
        now = scroller.snap.element(el.ref)
        if now is not None and _visible(scroller.snap, now, scroller.container):
            return (
                scroller,
                _ScrollOutcome(True, False, f"scrolled {_label(el)} into view", True, "ax"),
                first,
            )
    if container is None:
        return (
            scroller,
            _ScrollOutcome(False, False, f"{_label(el)} is not visible and not in a scroll area", False),
            first,
        )
    stopped = ""
    try:
        await _approach(scroller, el.ref)
    except _Stop as stop:
        stopped = f" (stopped: {stop})"
    now = scroller.snap.element(el.ref)
    visible = now is not None and _visible(scroller.snap, now, scroller.container)
    moved = scroller.scrolls > 0 and scroller.signature() != page_signature(first, container)
    message = f"scrolled {_label(el)} into view" if visible else f"could not bring {_label(el)} into view"
    return scroller, _ScrollOutcome(moved, not visible, message + stopped, visible), first


async def _approach(scroller: _Scroller, ref: str) -> None:
    """Scroll toward the element ``ref`` (at most a page at a time) until it is visible, it is
    gone, or the area stops moving."""
    for _ in range(MAX_SCROLLS):
        now = scroller.snap.element(ref)
        if now is None or now.frame is None or _visible(scroller.snap, now, scroller.container):
            return
        viewport = scroller.viewport()
        if viewport is None:
            return
        inside_y = (
            now.frame.y >= viewport.y - _EDGE_TOLERANCE
            and now.frame.y + now.frame.h <= viewport.y + viewport.h + _EDGE_TOLERANCE
        )
        axis: Axis = "x" if inside_y else "y"
        start, end = _pos(viewport, axis), _pos(viewport, axis) + _size(viewport, axis)
        low, high = _pos(now.frame, axis), _pos(now.frame, axis) + _size(now.frame, axis)
        distance = high - end if high > end else low - start
        page = scroller.page_lines(axis)
        lines = max(-page, min(page, distance / scroller.line_points))
        lines = math.copysign(max(1.0, abs(round(lines))), lines)
        if not await scroller.step(*_vector(axis, lines)):
            return


def _scroll_observation(
    before: Snapshot, after: Snapshot, report: str
) -> tuple[str, DiffModel | None, SnapshotModel | None]:
    if report == "none":
        return "", None, None
    if report == "snapshot":
        return format_snapshot(after), None, SnapshotModel.model_validate(snapshot_payload(after))
    diff = diff_snapshots(before, after)
    text = "no visible change" if diff.is_empty() else format_diff(diff)
    return f"{text}\n(snapshot {after.snapshot_id})", DiffModel.model_validate(diff_payload(diff)), None


@tool(
    "scroll",
    title="Scroll",
    description=SCROLL_DESCRIPTION,
    input_action=True,
    windows_mcp_name="Scroll",
)
def scroll_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``scroll`` tool."""

    async def scroll(
        *,
        ref: ContainerArg = None,
        x: XArg = None,
        y: YArg = None,
        app: AppArg = None,
        snapshot_id: SnapshotIdArg = None,
        coordinate_space: SpaceArg = "points",
        screenshot_id: ScreenshotIdArg = None,
        direction: Annotated[Direction, Field(description="up | down | left | right.")] = "down",
        amount: Annotated[float, Field(description="How far, in units.", gt=0, le=1000)] = 1,
        unit: Annotated[
            ScrollUnit, Field(description='"pages" (~80% of the visible area) or "lines".')
        ] = "pages",
        to: Annotated[
            ScrollTo | None,
            Field(description='"top" or "bottom": scroll all the way (ignores direction/amount).'),
        ] = None,
        scroll_into_view: Annotated[
            str | None,
            Field(
                description="Ref of an element to scroll into view (no other target needed).",
                min_length=2,
                max_length=32,
            ),
        ] = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ScrollModel]:
        has_point = x is not None or y is not None
        if scroll_into_view is not None:
            if ref is not None or has_point or to is not None or app is not None:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    "scroll_into_view brings one element into view; do not combine it with ref, x/y, app "
                    "or to.",
                )
            scroller, outcome, before = await _into_view(ctx, scroll_into_view, snapshot_id)
        else:
            if ref is not None and has_point:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Give either ref or x/y, not both.")
            if app is not None and (ref is not None or has_point):
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    "app picks the area to scroll only without ref or x/y; a ref or point already names it.",
                )
            snap, container, point, auto = await _scroll_area(
                ctx, ref=ref, snapshot_id=snapshot_id, x=x, y=y, space=coordinate_space,
                screenshot_id=screenshot_id, app=app,
            )  # fmt: skip
            scroller = _new_scroller(
                ctx,
                snap,
                container,
                label="scroll",
                timeout_s=NAV_DEADLINE_MAX_S,
                own_turns=False,
                auto=auto,
                at=point,
            )
            before = snap
            try:
                if to is not None:
                    outcome = await _scroll_to(scroller, to)
                else:
                    outcome = await _scroll_by(scroller, direction, amount, unit)
            except _Stop as stop:
                moved = scroller.signature() != page_signature(before, container)
                outcome = _ScrollOutcome(
                    moved=moved, at_end=False, message=f"stopped after {scroller.scrolls} scrolls: {stop}"
                )
        after = scroller.snap
        ctx.snapshots.put(after)
        text, diff, snapshot = _scroll_observation(before, after, report)
        model = ScrollModel(
            action="scroll",
            strategy=outcome.strategy,
            message=outcome.message,
            diff=diff,
            snapshot=snapshot,
            snapshot_id=after.snapshot_id,
            moved=outcome.moved,
            at_end=outcome.at_end,
            scrolls=scroller.scrolls,
            container=element_brief(scroller.container),
            visible=outcome.visible,
            point=scroller.at.to_list() if scroller.at is not None else None,
        )
        edge = "the top" if direction in ("up", "left") else "the end"
        reached = outcome.at_end and outcome.moved and to is None and scroll_into_view is None
        summary = outcome.message + (f" (reached {edge})" if reached else "")
        return ok_result(f"{summary}\n{text}" if text else summary, model)

    return scroll


# =========================================================================== explore
EXPLORE_DESCRIPTION = """\
Map an app in one call: its windows, menu bar (top-level menus with item counts), toolbar buttons,
tabs, sidebar sections, scroll areas, open dialogs and the focused element, plus how well AXPilot can
drive it (app kind, accessibility quality, strategies, tips).
Call it first for an unfamiliar app, then snapshot for the full element list. Params: app / window_id.
Returns a compact overview with refs (usable with click, read, scroll…) and the snapshot_id they belong
to. Errors: APP_NOT_FOUND, WINDOW_NOT_FOUND, PERMISSION_ACCESSIBILITY, BLOCKED_BY_POLICY, TIMEOUT."""


def _in_path(el: Element, ax_role: str) -> bool:
    return any(role == ax_role for role, _ in el.locator.path[:-1])


def _menus(root: MenuNode) -> list[MenuSummaryModel]:
    menus: list[MenuSummaryModel] = []
    for index, item in enumerate(root.children):
        title = item.title.strip()
        if not title or (index == 0 and title == "Apple"):
            continue
        items = sum(1 for child in item.children if child.title.strip())
        menus.append(MenuSummaryModel(title=title, items=items, enabled=item.enabled))
    return menus


def _window_model(ctx: ServerContext, window: WindowInfo) -> WindowModel:
    owner = AppInfo(name=window.app, pid=window.pid, bundle_id=window.bundle_id)
    try:
        ctx.policy.check_app(owner, kind="observe", window_title=window.title)
    except AXPilotError:
        return WindowModel.from_info(window, blocked=True)
    return WindowModel.from_info(window)


def _briefs(elements: Sequence[Element]) -> list[ElementBriefModel]:
    return [brief for el in elements[:EXPLORE_LIST_MAX] if (brief := element_brief(el)) is not None]


def _explore_sections(snap: Snapshot) -> dict[str, list[Element]]:
    elements = snap.all_elements
    toolbar = [el for el in elements if el.interactive and _in_path(el, "AXToolbar")]
    tabs = [el for el in elements if el.role == "tab"]
    outline_rows = [el for el in elements if el.role == "row" and _in_path(el, "AXOutline") and el.name]
    dialogs = [el for el in elements if el.role in ("sheet", "dialog", "popover")]
    areas = [
        el
        for el in elements
        if el.role in CONTAINER_ROLES and (el.extra.get("scrollable") or "rows" in el.extra)
    ]
    return {"toolbar": toolbar, "tabs": tabs, "sections": outline_rows, "dialogs": dialogs, "areas": areas}


def _area_model(el: Element) -> ScrollAreaModel:
    brief = element_brief(el)
    assert brief is not None
    rows = el.extra.get("rows")
    return ScrollAreaModel(
        **brief.model_dump(),
        rows=rows if isinstance(rows, int) else None,
        scrollable=bool(el.extra.get("scrollable")),
    )


def _names(items: Sequence[ElementBriefModel]) -> str:
    return " · ".join(
        f"{item.ref} {quote(item.name, 30)}" if item.name else f"{item.ref} {item.role}" for item in items
    )


def _explore_text(model: ExploreModel, more: dict[str, int]) -> str:
    app = model.app
    lines = [
        f"app {app.name} ({app.bundle_id or 'no bundle id'}, pid {app.pid}) · snapshot {model.snapshot_id}"
    ]
    if model.app_kind:
        quality = f", AX quality {model.ax_quality}" if model.ax_quality else ""
        lines.append(
            f"kind {model.app_kind}{quality} · strategies: {', '.join(model.strategies) or 'default'}"
        )
    for window in model.windows[:EXPLORE_LIST_MAX]:
        state = " (focused)" if window.focused else " (minimized)" if window.minimized else ""
        title = quote(window.title, 60) if window.title else "(hidden by policy)" if window.blocked else '""'
        lines.append(f"window {window.window_id} {title}{state}")
    if model.menus:
        lines.append(
            "menus: " + " · ".join(f"{m.title} ({m.items})" for m in model.menus) + "   (use select_menu)"
        )
    for label, items, key in (
        ("toolbar", model.toolbar, "toolbar"),
        ("tabs", model.tabs, "tabs"),
        ("sidebar", model.sections, "sections"),
        ("dialogs", model.dialogs, "dialogs"),
    ):
        if items:
            extra = f" (+{more[key]} more)" if more.get(key) else ""
            lines.append(f"{label}: {_names(items)}{extra}")
    for area in model.scroll_areas:
        rows = f" {area.rows} rows" if area.rows is not None else ""
        scrollable = ", scrollable" if area.scrollable else ""
        name = f" {quote(area.name, 40)}" if area.name else ""
        lines.append(f"area {area.ref} {area.role}{name}{rows}{scrollable}")
    if model.focused is not None:
        name = f" {quote(model.focused.name, 40)}" if model.focused.name else ""
        lines.append(f"focused: {model.focused.ref} {model.focused.role}{name}")
    lines.extend(f"tip: {note}" for note in model.notes[:3])
    lines.append(
        f"{model.elements} interactive elements: snapshot for all of them; read(target=<area ref>) for long "
        "content; select_menu for menu commands."
    )
    return "\n".join(lines)


@tool(
    "explore",
    title="Explore an app",
    description=EXPLORE_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="Explore",
)
def explore_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``explore`` tool."""

    async def explore(
        *, app: AppArg = None, window_id: WindowIdArg = None
    ) -> Annotated[CallToolResult, ExploreModel]:
        warnings: list[str] = []
        tree = await ctx.capture_tree(app=app, window_id=window_id, include_menus=True)
        snap = ctx.build_snapshot(tree, mode="interactive", max_elements=ctx.config.perception.max_elements)
        profile = await app_profile_of(ctx, tree, warnings, needed=True)
        key = _app_key(snap.app)
        windows: list[WindowModel] = []
        try:
            listed = await ctx.run_engine(ctx.engine.list_windows, key)
            windows = [_window_model(ctx, window) for window in listed if window.pid == snap.app.pid]
        except AXPilotError as err:
            warnings.append(f"Could not list the app's windows: {err.message}")
        menus: list[MenuSummaryModel] = []
        try:
            menus = _menus(await ctx.run_engine(ctx.engine.menu_bar, key, 2))
        except AXPilotError as err:
            warnings.append(f"Could not read the menu bar: {err.message}")
        sections = _explore_sections(snap)
        more = {name: max(0, len(items) - EXPLORE_LIST_MAX) for name, items in sections.items()}
        payload = snapshot_payload(snap)
        model = ExploreModel(
            app=AppRefModel.model_validate(payload["app"]),
            window=WindowRefModel.model_validate(payload["window"]) if payload.get("window") else None,
            snapshot_id=snap.snapshot_id,
            app_kind=getattr(profile, "kind", None),
            ax_quality=getattr(profile, "ax_quality", None),
            strategies=list(getattr(profile, "strategies", []) or []),
            notes=list(getattr(profile, "notes", []) or []),
            windows=windows,
            menus=menus,
            toolbar=_briefs(sections["toolbar"]),
            tabs=_briefs(sections["tabs"]),
            sections=_briefs(sections["sections"]),
            dialogs=_briefs(sections["dialogs"]),
            scroll_areas=[_area_model(el) for el in sections["areas"][:EXPLORE_LIST_MAX]],
            focused=element_brief(snap.focused_element),
            elements=sum(1 for el in snap.all_elements if el.interactive),
        )
        return ok_result(_explore_text(model, more), model, warnings=warnings)

    return explore


# =========================================================================== read
READ_DESCRIPTION = """\
Read the whole content of a long list, table, document, text view or web page: AXPilot scrolls through
it page by page and joins the pages without repeats.
Use it for long chats, documents, logs, mailboxes and search results instead of scrolling by hand.
Params: target ("auto": the window's main scrollable area, or the ref of a list/table/web area/text
area); app / window_id (with "auto"); mode (auto | rows | text); from_top (start at the top, default
true); max_pages (≤ 30); restore (scroll back to where it was); timeout_ms.
Returns the lines (rows: cells joined by " | "), pages, scrolls, complete (everything from the top to
the end) and at_end. Partial content comes back with complete=false and a warning when a limit is
reached. Errors: STALE_REF, APP_NOT_FOUND, BLOCKED_BY_POLICY, USER_ACTIVE, PAUSED_BY_USER."""


def _read_mode(snap: Snapshot, container: Element | None, mode: ReadMode) -> Literal["rows", "text"]:
    if mode != "auto":
        return mode
    if container is None:
        return "text"
    if container.role in COLLECTION_ROLES:
        return "rows"
    inside = descendants(snap, container)
    return "rows" if any(el.role in COLLECTION_ROLES for el in inside) else "text"


def _page_items(snap: Snapshot, container: Element | None, mode: Literal["rows", "text"]) -> list[Item]:
    if mode == "text" and container is not None and container.role in COLLECTION_ROLES:
        items: list[Item] = []
        for el in descendants(snap, container):
            if el.role in CONTAINER_ROLES or el.role in ("row", "cell", "group"):
                continue
            text = el.name or (None if el.secure else el.value)
            if text and (not items or items[-1][1] != text):
                items.append((el.ref, text))
        return items
    return collect_items(snap, container)


def merge_items(pages: Sequence[Sequence[Item]]) -> tuple[list[str], int]:
    """Join pages of ``(ref, text)`` items into lines; returns ``(lines, possible gaps)``.

    A page that shares refs with what came before is joined by ref (every element once, exact
    even when lines repeat). Otherwise, e.g. in apps that rebuild their elements on scroll,
    the longest overlap of text lines is dropped; a page without any overlap may mean a skipped
    part (counted as a gap)."""
    lines: list[str] = []
    seen: set[str] = set()
    gaps = 0
    for number, page in enumerate(pages):
        refs = {ref for ref, _ in page if ref is not None}
        if number == 0:
            new = list(page)
        elif refs & seen:
            new = [(ref, text) for ref, text in page if ref is None or ref not in seen]
        else:
            texts = [text for _, text in page]
            skip = overlap_length(lines, texts)
            if skip == 0 and lines and texts:
                gaps += 1
            new = list(page[skip:])
        lines.extend(text for _, text in new)
        seen |= refs
    return lines, gaps


def _cut(lines: list[str]) -> tuple[list[str], bool]:
    kept: list[str] = []
    total = 0
    for line in lines:
        total += len(line) + 1
        if total > READ_MAX_CHARS:
            return kept, True
        kept.append(line)
    return kept, False


async def _full_text(ctx: ServerContext, snap: Snapshot, area: Element) -> list[str]:
    """Every line of a text area (snapshots truncate values): its live AXValue."""
    if area.secure:
        return []
    handle = await ctx.run_engine(
        live_handle, ctx.engine, snap, area, timeout_s=ctx.capture_timeout_s + ctx.engine_timeout_s
    )
    attributes = await ctx.run_engine(ctx.engine.element_attributes, handle)
    value = attributes.get("AXValue")
    if value is None:
        return (area.value or "").splitlines()
    return str(value).splitlines()


@dataclass
class _ReadState:
    pages: list[list[Item]] = field(default_factory=list)
    at_end: bool = False
    from_top: bool = False


async def _read_pages(
    scroller: _Scroller, mode: Literal["rows", "text"], *, from_top: bool, max_pages: int, state: _ReadState
) -> None:
    """Fill ``state.pages`` (the caller keeps what was collected if this stops early)."""
    if from_top:
        state.from_top = await scroller.to_edge(-1, MAX_SCROLLS)
        state.pages[:] = [_page_items(scroller.snap, scroller.container, mode)]
        if not state.from_top:
            raise _Stop("could not reach the top")
    while len(state.pages) < max_pages:
        if scroller.edges()[1] is True:
            state.at_end = True
            return
        if scroller.scrolls >= MAX_SCROLLS:
            raise _Stop(f"the limit of {MAX_SCROLLS} scrolls was reached")
        moved, reached = await scroller.advance(+1)
        if not moved:
            state.at_end = True
            return
        state.pages.append(_page_items(scroller.snap, scroller.container, mode))
        if reached:
            state.at_end = True
            return
    state.at_end = scroller.edges()[1] is True


def _read_text(model: ReadModel, shown: int) -> str:
    what = _label_brief(model.container)
    status = "complete" if model.complete else "partial"
    end = ", reached the end" if model.at_end else ""
    head = (
        f"read {what}: {model.count} {'rows' if model.mode == 'rows' else 'lines'}, {model.pages} pages, "
        f"{model.scrolls} scrolls, {status}{end} · snapshot {model.snapshot_id}"
    )
    body = model.lines[:shown]
    tail = [f"… {model.count - shown} more lines in structuredContent"] if model.count > shown else []
    return "\n".join([head, *body, *tail])


def _label_brief(brief: ElementBriefModel | None) -> str:
    if brief is None:
        return "the window"
    return f"{brief.ref} {brief.role} {quote(brief.name, 40)}" if brief.name else f"{brief.ref} {brief.role}"


@tool(
    "read",
    title="Read long content",
    description=READ_DESCRIPTION,
    read_only=True,
    windows_mcp_name="Read",
)
def read_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``read`` tool."""

    async def read(
        *,
        target: Annotated[
            str,
            Field(
                description='"auto" (the main scrollable area) or the ref of a list, table, web area or '
                "text area.",
                min_length=2,
                max_length=32,
            ),
        ] = "auto",
        app: AppArg = None,
        window_id: WindowIdArg = None,
        snapshot_id: SnapshotIdArg = None,
        mode: Annotated[
            ReadMode, Field(description='"rows" (one line per row), "text" (every text), or "auto".')
        ] = "auto",
        from_top: Annotated[
            bool, Field(description="Scroll to the top first, so the whole content is read.")
        ] = True,
        max_pages: Annotated[
            int, Field(description="Most screens to collect.", ge=1, le=READ_MAX_PAGES)
        ] = 15,
        restore: Annotated[
            bool, Field(description="Scroll back to the original position afterwards.")
        ] = False,
        timeout_ms: TimeoutArg = int(NAV_DEADLINE_DEFAULT_S * 1000),
    ) -> Annotated[CallToolResult, ReadModel]:
        warnings: list[str] = []
        auto = target.strip().lower() == "auto"
        if not auto and (app is not None or window_id is not None):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "target is a ref, which already names the window; do not combine it with app or window_id.",
            )
        if auto:
            snap = await ctx.take_snapshot(app=app, window_id=window_id, mode="full", store=False)
            area = scroll_container_for(snap, None)
        else:
            snap, el = await _element_now(ctx, target.strip(), snapshot_id, "full")
            area = el if el.role in CONTAINER_ROLES else scroll_container_for(snap, el)
        area = _content_area(snap, area)
        chosen = _read_mode(snap, area, mode)
        scrolls, restored = 0, None
        state = _ReadState(pages=[_page_items(snap, area, chosen)])
        if area is not None and area.role == "textarea":
            state.pages = [[(None, line) for line in await _full_text(ctx, snap, area)]]
            state.at_end = state.from_top = True
            complete_collection = True
        elif area is None:
            warnings.append("Nothing scrollable in this window; read what is on screen.")
            state.at_end = state.from_top = True
            complete_collection = True
        else:
            scroller = _new_scroller(ctx, snap, area, label="read", timeout_s=timeout_ms / 1000, auto=auto)
            original = (scroller.snap, scroller.container)
            at_start, at_end = scroller.edges()
            needs_scrolling = not (at_start is True and at_end is True)
            completed = True
            if needs_scrolling:
                _begin_scrolling(ctx, scroller)

                async def loop() -> None:
                    await _read_pages(
                        scroller,
                        chosen,
                        from_top=from_top and at_start is not True,
                        max_pages=max_pages,
                        state=state,
                    )

                completed = await _guarded_loop(scroller, loop, warnings)
                if not from_top or at_start is True:
                    state.from_top = at_start is True
            else:
                state.at_end = state.from_top = True
            if restore and scroller.scrolls:
                scroller.deadline = max(scroller.deadline, ctx.clock() + RESTORE_GRACE_S)
                try:
                    restored = await scroller.restore(*original)
                except (_Stop, AXPilotError) as err:
                    restored = False
                    warnings.append(f"Could not restore the scroll position: {err}")
                if restored is False and not any("restore" in w for w in warnings):
                    warnings.append("The original scroll position was restored only approximately.")
            snap, area, scrolls = scroller.snap, scroller.container, scroller.scrolls
            complete_collection = completed and state.at_end and state.from_top
            if completed and not state.at_end:
                warnings.append(f"Stopped after max_pages={max_pages} pages; raise it to read further.")
            if not state.from_top and not from_top:
                warnings.append("Started in the middle (from_top=false): content above was not read.")
        lines, gaps = merge_items(state.pages)
        if gaps:
            warnings.append(f"{gaps} page(s) did not overlap the previous one; some lines may be missing.")
        lines, truncated = _cut(lines)
        if truncated:
            warnings.append(f"The content was cut at {READ_MAX_CHARS} characters.")
        ctx.snapshots.put(snap)
        model = ReadModel(
            container=element_brief(area),
            snapshot_id=snap.snapshot_id,
            mode=chosen,
            lines=lines,
            content="\n".join(lines),
            count=len(lines),
            pages=len(state.pages),
            scrolls=scrolls,
            complete=complete_collection and not gaps and not truncated,
            at_end=state.at_end,
            truncated=truncated,
            restored=restored,
        )
        return ok_result(_read_text(model, shown=min(len(lines), 400)), model, warnings=warnings)

    return read
