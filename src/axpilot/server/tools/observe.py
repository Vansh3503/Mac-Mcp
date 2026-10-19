"""Observe tools: look at the Mac without changing anything.

``snapshot``, ``screenshot``, ``inspect``, ``ocr``, ``list_apps``, ``list_windows``,
``list_displays``, ``wait_for`` and ``status``. (``find``, ``explore`` and ``read`` live in
:mod:`axpilot.server.tools.navigate`.) Every tool here is read-only: it applies the
``observe`` (accessibility) or ``capture`` (pixels) policy and never takes the input lock.

Helpers other tool modules may reuse: :func:`capture_screen` (policy-aware screenshot),
:func:`fit_capture` (downscale/re-encode) and :func:`annotate_capture` (draw refs on an image).
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import dataclasses
import functools
import io
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp_types import CallToolResult
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
from pydantic import Field

from axpilot import SERVER_NAME, __version__
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, Capture, ImageFormat, OcrLine, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception.find import find_elements
from axpilot.perception.format import (
    element_payload,
    format_element_line,
    format_point,
    format_rect,
    format_snapshot,
    quote,
    snapshot_payload,
)
from axpilot.perception.model import Element, Snapshot
from axpilot.perception.resolve import live_handle
from axpilot.perception.snapshot import truncate
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import (
    AppArg,
    AppListModel,
    AppModel,
    AppRefModel,
    CaptureModel,
    ControlModel,
    DisplayListModel,
    DisplayModel,
    ElementBriefModel,
    ElementModel,
    InspectModel,
    OcrLineModel,
    OcrModel,
    PermissionsModel,
    RateLimitModel,
    RectArg,
    RefArg,
    ScreenshotModel,
    SnapshotIdArg,
    SnapshotModel,
    StatusModel,
    WaitForModel,
    WindowIdArg,
    WindowListModel,
    WindowModel,
)

__all__ = [
    "OCR_MAX_EDGE",
    "annotate_capture",
    "capture_screen",
    "fit_capture",
]

_log = logging.getLogger(__name__)

OCR_MAX_EDGE = 2560
"""Captures used for OCR keep up to this many pixels on the longest edge (accuracy)."""
WAIT_MAX_MS = 60_000
IDLE_QUIET_S = 0.5
"""``wait_for(condition="idle")``: the UI must stay unchanged this long."""
_POLL_START_S = 0.1
_POLL_MAX_S = 1.0
_POLL_GROWTH = 1.5
_ELEMENT_MIN_SCORE = 0.5
"""Minimum ``find_elements`` score (a substring match or better) for wait_for's element checks."""
_ATTRIBUTE_MAX_CHARS = 2000
_OCR_TEXT_MAX_LINES = 300
_LABEL_PADDING = 2
_MARK_COLORS: tuple[tuple[int, int, int], ...] = (
    (230, 25, 75),
    (60, 140, 60),
    (0, 110, 220),
    (245, 130, 48),
    (145, 30, 180),
    (0, 150, 150),
)
_NOT_YET = frozenset({ErrorCode.APP_NOT_FOUND, ErrorCode.WINDOW_NOT_FOUND, ErrorCode.STALE_REF})
"""Errors that only mean "not there yet" while wait_for polls."""

SnapshotModeArg = Literal["auto", "interactive", "full", "text", "vision"]
WaitCondition = Literal[
    "element", "element_gone", "window", "app_running", "text_visible", "idle", "duration"
]
CaptureTarget = Literal["display", "window", "app", "region"]
_CAPTURE_TARGETS: dict[str, CaptureTarget] = {"window_id": "window", "app": "app", "rect": "region"}

DisplayIdArg = Annotated[
    int | None, Field(description="Display id from list_displays. Default: the main display.", ge=0)
]
CaptureAppArg = Annotated[
    str | None,
    Field(
        description="Capture the front window of this app (name, bundle id or pid).",
        min_length=1,
        max_length=256,
    ),
]


# =========================================================================== image helpers
def _encode(image: PILImage.Image, fmt: ImageFormat, quality: int) -> bytes:
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, format="PNG", optimize=False)
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _decode(capture: Capture) -> PILImage.Image:
    with PILImage.open(io.BytesIO(capture.data)) as opened:
        opened.load()
        return opened.convert("RGB")


def fit_capture(capture: Capture, max_edge: int, fmt: ImageFormat, quality: int = 80) -> Capture:
    """``capture`` downscaled so its longest edge is at most ``max_edge`` pixels and encoded as
    ``fmt``; the screen region is unchanged (so pixel↔point mapping stays exact)."""
    if max(capture.width, capture.height) <= max_edge and capture.format == fmt:
        return capture
    image = _decode(capture)
    longest = max(image.width, image.height)
    if longest > max_edge:
        factor = max_edge / longest
        size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
        image = image.resize(size, PILImage.Resampling.LANCZOS)
    return Capture(
        data=_encode(image, fmt, quality),
        format=fmt,
        width=image.width,
        height=image.height,
        region=capture.region,
    )


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except (OSError, ValueError, TypeError):  # pragma: no cover - Pillow without FreeType
        return ImageFont.load_default()


def annotate_capture(capture: Capture, elements: Sequence[Element], *, quality: int = 80) -> Capture:
    """Draw the frame and ref of each interactive element onto ``capture`` (set-of-mark prompting).

    Containers (lists, toolbars…), elements without a frame and elements outside the captured
    region are skipped, so the marks stay readable."""
    image = _decode(capture)
    draw = ImageDraw.Draw(image)
    font = _font(max(11, min(18, round(12 * max(capture.scale_x, 1.0)))))
    for index, el in enumerate(elements):
        if not el.interactive or el.frame is None or el.frame.w <= 0 or el.frame.h <= 0:
            continue
        if not el.frame.intersects(capture.region):
            continue
        color = _MARK_COLORS[index % len(_MARK_COLORS)]
        x0, y0 = _pixels(capture, el.frame)
        x1, y1 = x0 + el.frame.w * capture.scale_x, y0 + el.frame.h * capture.scale_y
        draw.rectangle((x0, y0, max(x0, x1 - 1), max(y0, y1 - 1)), outline=color, width=2)
        left, top, right, bottom = draw.textbbox((0, 0), el.ref, font=font)
        width, height = right - left + 2 * _LABEL_PADDING, bottom - top + 2 * _LABEL_PADDING
        lx = max(0.0, x0)
        ly = y0 - height if y0 - height >= 0 else max(0.0, y0)  # above the box, or inside at the top
        draw.rectangle((lx, ly, lx + width, ly + height), fill=color)
        draw.text(
            (lx + _LABEL_PADDING - left, ly + _LABEL_PADDING - top), el.ref, fill=(255, 255, 255), font=font
        )
    return Capture(
        data=_encode(image, capture.format, quality),
        format=capture.format,
        width=image.width,
        height=image.height,
        region=capture.region,
    )


def _pixels(capture: Capture, frame: Rect) -> tuple[float, float]:
    return ((frame.x - capture.region.x) * capture.scale_x, (frame.y - capture.region.y) * capture.scale_y)


# =========================================================================== shared capture
def _region(rect: list[float] | None) -> Rect | None:
    if rect is None:
        return None
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"rect must have a positive width and height, got {w:g}×{h:g}.",
            hint="Pass [x, y, width, height] in screen points, e.g. a frame from a snapshot.",
        )
    return Rect(x, y, w, h)


def _one_target(**candidates: Any) -> str | None:
    given = [name for name, value in candidates.items() if value is not None]
    if len(given) > 1:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Pass at most one of {', '.join(candidates)} (got {', '.join(given)}).",
            hint="Each call captures one target: a display, a window, an app's front window or a region.",
        )
    return given[0] if given else None


async def _front_window(ctx: ServerContext, app: AppInfo) -> WindowInfo:
    windows = await ctx.run_engine(ctx.engine.list_windows, app.bundle_id or str(app.pid))
    visible = [w for w in windows if not w.minimized]
    if not visible:
        raise AXPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"{app.name} has no open window on this Space.",
            hint="Open or restore a window first (the `window` or `app` tool), or capture a display.",
        )
    return next((w for w in visible if w.focused or w.main), visible[0])


async def _exclusions(ctx: ServerContext) -> list[str]:
    running = await ctx.run_engine(ctx.engine.list_apps)
    return ctx.policy.capture_exclusions(running)


async def capture_screen(
    ctx: ServerContext,
    *,
    display_id: int | None = None,
    window_id: int | None = None,
    rect: Rect | None = None,
    max_edge: int | None = None,
    fmt: ImageFormat | None = None,
) -> tuple[Capture, list[str]]:
    """Take a screenshot with deny-listed apps left out; returns ``(capture, excluded apps)``.

    The caller has already applied the ``capture`` policy to a window target. Raises
    ``PERMISSION_SCREEN_RECORDING``, ``DISPLAY_NOT_FOUND``, ``WINDOW_NOT_FOUND`` or ``TIMEOUT``."""
    settings = ctx.config.capture
    excluded = await _exclusions(ctx)
    shoot = functools.partial(
        ctx.engine.screenshot,
        display_id=display_id,
        window_id=window_id,
        rect=rect,
        max_edge=max_edge or settings.max_edge,
        fmt=fmt or settings.format,
        quality=settings.quality,
        exclude_bundle_ids=excluded,
    )
    try:
        capture = await ctx.run_engine(shoot)
    except AXPilotError as err:
        if err.code is ErrorCode.PERMISSION_SCREEN_RECORDING:
            raise AXPilotError(
                ErrorCode.PERMISSION_SCREEN_RECORDING,
                "Taking a screenshot needs the Screen Recording permission, which AXPilot does not have.",
                details=err.details,
            ) from err
        raise
    return capture, excluded


# =========================================================================== snapshot
SNAPSHOT_DESCRIPTION = """\
Capture an app's UI as a numbered list of elements (refs e1, e2…) that other tools act on.
Use it before acting and whenever the UI may have changed; always act with refs from the latest snapshot.
Params: app / window_id (default: the frontmost app's focused window); mode: auto (default: the
accessibility tree, plus OCR text elements o1… when the tree is poor), interactive (actionable
elements only), full (every element), text (reading view), vision (always add OCR elements);
max_elements / offset to page (or snapshot_id + offset to page an existing snapshot without
re-capturing); include_screenshot / annotate (image with refs drawn on it).
Returns one line per element (ref, role, name, value, states, center point), the focused element,
the menu bar, app kind and AX quality, and paging info; structuredContent has every field.
Errors: APP_NOT_FOUND, WINDOW_NOT_FOUND, SNAPSHOT_NOT_FOUND, PERMISSION_ACCESSIBILITY,
BLOCKED_BY_POLICY, TIMEOUT."""


@dataclass(frozen=True, slots=True)
class _SnapshotRequest:
    app: str | None
    window_id: int | None
    mode: SnapshotModeArg
    max_elements: int
    offset: int
    snapshot_id: str | None
    include_menus: bool
    include_screenshot: bool
    annotate: bool


@dataclass(frozen=True, slots=True)
class _Shot:
    capture: Capture
    screenshot_id: str
    annotated: bool


async def _app_profile(ctx: ServerContext, tree: RawTree, warnings: list[str], *, needed: bool) -> Any:
    """The app's compatibility profile, or ``None`` (with a warning when it was needed)."""
    try:
        from axpilot.perception.appkind import app_profile  # noqa: PLC0415 - optional, written in parallel
    except ImportError:
        if needed:
            warnings.append("App compatibility detection is unavailable; using the accessibility tree only.")
        return None
    try:
        return await ctx.run_engine(app_profile, tree.app, tree)
    except Exception as exc:  # an optional enhancement must not fail the snapshot (A8: logged + warned)
        _log.warning(
            "App profile for %s failed: %s", tree.app.name, exc, exc_info=not isinstance(exc, AXPilotError)
        )
        warnings.append(
            f"App compatibility detection failed ({type(exc).__name__}); using the accessibility tree only."
        )
        return None


async def _window_capture(ctx: ServerContext, snap: Snapshot, max_edge: int, fmt: ImageFormat) -> Capture:
    """Capture the snapshot's window (or the main display when it has none), with the policy."""
    title = snap.window.title if snap.window is not None else None
    ctx.policy.check_app(snap.app, kind="capture", window_title=title)
    window_id = snap.window.window_id if snap.window is not None else None
    capture, _ = await capture_screen(ctx, window_id=window_id, max_edge=max_edge, fmt=fmt)
    return capture


async def _add_vision(
    ctx: ServerContext, snap: Snapshot, *, forced: bool, warnings: list[str]
) -> tuple[Snapshot, Capture | None]:
    """Append OCR pseudo-elements to ``snap``. In auto mode problems only add a warning."""
    try:
        from axpilot.perception.vision import merge_vision, ocr_elements  # noqa: PLC0415 - optional
    except ImportError:
        warnings.append(
            "The vision fallback (OCR elements) is unavailable; showing accessibility elements only."
        )
        return snap, None
    try:
        capture = await _window_capture(ctx, snap, OCR_MAX_EDGE, "png")
        lines = await ctx.run_engine(ctx.engine.ocr, capture, None)
    except AXPilotError as err:
        if forced:
            raise
        warnings.append(f"Vision fallback skipped: {err.message}")
        return snap, None
    return merge_vision(snap, ocr_elements(lines)), capture


async def _snapshot_screenshot(
    ctx: ServerContext, snap: Snapshot, capture: Capture | None, *, annotate: bool, warnings: list[str]
) -> _Shot | None:
    settings = ctx.config.capture
    try:
        if capture is None:
            capture = await _window_capture(ctx, snap, settings.max_edge, settings.format)
    except AXPilotError as err:
        reason = (
            "no Screen Recording permission"
            if err.code is ErrorCode.PERMISSION_SCREEN_RECORDING
            else err.message
        )
        warnings.append(f"Screenshot skipped: {reason}")
        return None
    image = await ctx.run_engine(fit_capture, capture, settings.max_edge, settings.format, settings.quality)
    annotated = False
    if annotate:
        try:
            image = await ctx.run_engine(annotate_capture, image, snap.elements, quality=settings.quality)
            annotated = True
        except (OSError, ValueError) as exc:
            _log.warning("Could not annotate the screenshot: %s", exc)
            warnings.append("Could not draw refs on the screenshot; it is returned without them.")
    return _Shot(capture=image, screenshot_id=ctx.captures.put(image), annotated=annotated)


def _vision_count(snap: Snapshot) -> int:
    return sum(1 for el in snap.all_elements if el.role == "ocrtext")


def _profile_line(profile: Any, vision: int) -> str | None:
    if profile is None and not vision:
        return None
    parts: list[str] = []
    if profile is not None:
        parts.append(f"app kind {profile.kind}")
        if profile.ax_quality is not None:
            parts.append(f"AX quality {profile.ax_quality}")
        if profile.ax_quality == "good" and not vision:
            return None
    if vision:
        parts.append(f"{vision} OCR text elements (o-refs, acted on at their center)")
    line = " · ".join(parts)
    notes = list(getattr(profile, "notes", []) or []) if profile is not None else []
    if notes and getattr(profile, "ax_quality", None) != "good":
        line += f". Tip: {notes[0]}"
    return line


def _snapshot_result(
    snap: Snapshot, req: _SnapshotRequest, profile: Any, shot: _Shot | None, warnings: list[str]
) -> CallToolResult:
    vision = _vision_count(snap)
    lines = [format_snapshot(snap)]
    line = _profile_line(profile, vision)
    if line:
        lines.append(line)
    if shot is not None:
        drawn = " with refs drawn" if shot.annotated else ""
        lines.append(f"screenshot {shot.screenshot_id}: {shot.capture.width}×{shot.capture.height} px{drawn}")
    data = {
        **snapshot_payload(snap),
        "requested_mode": req.mode,
        "app_kind": getattr(profile, "kind", None),
        "ax_quality": getattr(profile, "ax_quality", None),
        "strategies": list(getattr(profile, "strategies", []) or []),
        "notes": list(getattr(profile, "notes", []) or []),
        "vision_elements": vision,
        "screenshot": (
            CaptureModel.from_capture(shot.capture, shot.screenshot_id, annotated=shot.annotated)
            if shot is not None
            else None
        ),
    }
    model = SnapshotModel.model_validate(data)
    return ok_result("\n".join(lines), model, images=[shot.capture] if shot else [], warnings=warnings)


async def _page_existing(ctx: ServerContext, req: _SnapshotRequest, warnings: list[str]) -> CallToolResult:
    if req.app is not None or req.window_id is not None:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            "snapshot_id pages an existing snapshot; do not combine it with app or window_id.",
            hint="Omit snapshot_id to capture a new snapshot of another app or window.",
        )
    assert req.snapshot_id is not None
    snap = ctx.snapshots.get(req.snapshot_id)
    ctx.policy.check_app(snap.app, kind="observe", window_title=snap.window.title if snap.window else None)
    page = dataclasses.replace(
        snap, elements=snap.all_elements[req.offset : req.offset + req.max_elements], offset=req.offset
    )
    shot = None
    if req.include_screenshot or req.annotate:
        shot = await _snapshot_screenshot(ctx, page, None, annotate=req.annotate, warnings=warnings)
    return _snapshot_result(page, req, None, shot, warnings)


async def _snapshot(ctx: ServerContext, req: _SnapshotRequest) -> CallToolResult:
    warnings: list[str] = []
    if req.snapshot_id is not None:
        return await _page_existing(ctx, req, warnings)
    tree = await ctx.capture_tree(app=req.app, window_id=req.window_id, include_menus=req.include_menus)
    base_mode = "interactive" if req.mode in ("auto", "vision") else req.mode
    snap = ctx.build_snapshot(
        tree, mode=base_mode, max_elements=req.max_elements, offset=req.offset, store=False
    )
    profile = await _app_profile(ctx, tree, warnings, needed=req.mode in ("auto", "vision"))
    capture: Capture | None = None
    poor = profile is not None and profile.ax_quality == "poor"
    if req.mode == "vision" or (req.mode == "auto" and poor):
        snap, capture = await _add_vision(ctx, snap, forced=req.mode == "vision", warnings=warnings)
    ctx.snapshots.put(snap)
    shot = None
    if req.include_screenshot or req.annotate:
        shot = await _snapshot_screenshot(ctx, snap, capture, annotate=req.annotate, warnings=warnings)
    return _snapshot_result(snap, req, profile, shot, warnings)


@tool(
    "snapshot",
    title="Snapshot an app's UI",
    description=SNAPSHOT_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="Snapshot",
)
def snapshot_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``snapshot`` tool."""

    async def snapshot(
        *,
        app: AppArg = None,
        window_id: WindowIdArg = None,
        mode: Annotated[
            SnapshotModeArg,
            Field(description="auto | interactive | full | text | vision (see the tool description)."),
        ] = "auto",
        max_elements: Annotated[
            int | None, Field(description="Elements per page (default from config, 150).", ge=1, le=2000)
        ] = None,
        offset: Annotated[int, Field(description="Index of the first element to return (paging).", ge=0)] = 0,
        snapshot_id: Annotated[
            str | None,
            Field(
                description="Page through this existing snapshot instead of capturing again.", max_length=16
            ),
        ] = None,
        include_menus: Annotated[bool, Field(description="List the app's top-level menus.")] = True,
        include_screenshot: Annotated[
            bool, Field(description="Also return a screenshot of the window.")
        ] = False,
        annotate: Annotated[
            bool,
            Field(
                description="Return a screenshot with element refs drawn on it (implies include_screenshot)."
            ),
        ] = False,
    ) -> Annotated[CallToolResult, SnapshotModel]:
        request = _SnapshotRequest(
            app=app,
            window_id=window_id,
            mode=mode,
            max_elements=max_elements or ctx.config.perception.max_elements,
            offset=offset,
            snapshot_id=snapshot_id,
            include_menus=include_menus,
            include_screenshot=include_screenshot,
            annotate=annotate,
        )
        return await _snapshot(ctx, request)

    return snapshot


# =========================================================================== screenshot
SCREENSHOT_DESCRIPTION = """\
Take a screenshot of a display, a window, an app's front window or a screen region.
Use it to see what the accessibility tree cannot show (images, canvases, layout); prefer snapshot
for acting. Params: display_id | window_id | app | rect [x, y, width, height] in points (at most
one; default: the main display), max_edge (longest edge in pixels, default 1280), format.
Returns the image plus a screenshot_id and its scale: to click something you see in it, pass its
pixel x/y with coordinate_space="screenshot" and that screenshot_id. Apps on the deny list are
left out of the image. Errors: PERMISSION_SCREEN_RECORDING, DISPLAY_NOT_FOUND, WINDOW_NOT_FOUND,
APP_NOT_FOUND, BLOCKED_BY_POLICY, TIMEOUT."""


async def _screenshot_target(
    ctx: ServerContext, *, window_id: int | None, app: str | None
) -> tuple[int | None, str | None]:
    """Policy-check a window/app target; returns the window id to capture and the app name."""
    if window_id is not None:
        target, window = await ctx.check_target(app=None, window_id=window_id, kind="capture")
        return window_id, window.app if window is not None else (target.name if target else None)
    if app is not None:
        target, _ = await ctx.check_target(app=app, window_id=None, kind="capture")
        assert target is not None
        window = await _front_window(ctx, target)
        ctx.policy.check_app(target, kind="capture", window_title=window.title)
        return window.window_id, target.name
    return None, None


@tool(
    "screenshot",
    title="Take a screenshot",
    description=SCREENSHOT_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="Screenshot",
)
def screenshot_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``screenshot`` tool."""

    async def screenshot(
        *,
        display_id: DisplayIdArg = None,
        window_id: WindowIdArg = None,
        app: CaptureAppArg = None,
        rect: RectArg = None,
        max_edge: Annotated[
            int | None,
            Field(description="Longest image edge in pixels (default from config, 1280).", ge=256, le=8192),
        ] = None,
        format: Annotated[
            Literal["png", "jpeg"] | None, Field(description="Image format (default from config, jpeg).")
        ] = None,
    ) -> Annotated[CallToolResult, ScreenshotModel]:
        chosen = _one_target(display_id=display_id, window_id=window_id, app=app, rect=rect)
        region = _region(rect)
        capture_window, app_name = await _screenshot_target(ctx, window_id=window_id, app=app)
        capture, excluded = await capture_screen(
            ctx, display_id=display_id, window_id=capture_window, rect=region, max_edge=max_edge, fmt=format
        )
        screenshot_id = ctx.captures.put(capture)
        target = _CAPTURE_TARGETS.get(chosen or "", "display")
        model = ScreenshotModel(
            **CaptureModel.from_capture(capture, screenshot_id).model_dump(),
            target=target,
            display_id=display_id,
            window_id=capture_window,
            app=app_name,
            excluded_apps=excluded,
        )
        what = f"{target} {app_name}" if app_name else target
        text = (
            f"screenshot {screenshot_id}: {capture.width}×{capture.height} px of {what} "
            f"{format_rect(capture.region)} pt (scale {capture.scale_x:.3g} px/pt). To act on a pixel, pass "
            f'x/y with coordinate_space="screenshot", screenshot_id="{screenshot_id}".'
        )
        return ok_result(text, model, images=[capture])

    return screenshot


# =========================================================================== inspect
INSPECT_DESCRIPTION = """\
Show everything about one element: all accessibility attributes, supported actions, its
ancestors (path from the window) and children.
Use it when a snapshot line is not enough: to learn which actions an element supports, why it
is disabled, its identifier or exact value. Params: ref (from a snapshot), snapshot_id (default:
the latest snapshot). Errors: STALE_REF (the element is gone: take a new snapshot),
SNAPSHOT_NOT_FOUND, INVALID_ARGUMENT (malformed ref), BLOCKED_BY_POLICY."""


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return truncate(value, _ATTRIBUTE_MAX_CHARS)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_value(v) for v in value]
    return truncate(str(value), _ATTRIBUTE_MAX_CHARS)


def _clean_attributes(attributes: dict[str, Any], *, secure: bool) -> dict[str, Any]:
    """JSON-safe, truncated attributes; a secure field never shows its value."""
    cleaned = {str(key): _json_value(value) for key, value in attributes.items()}
    if secure:
        for key in ("AXValue", "AXSelectedText", "AXValueDescription"):
            cleaned.pop(key, None)
    return cleaned


def _brief(el: Element) -> ElementBriefModel:
    center = el.center
    return ElementBriefModel(
        ref=el.ref,
        role=el.role,
        name=el.name,
        value=el.value,
        center=center.to_list() if center is not None else None,
        states=list(el.states),
    )


def _ancestors(snap: Snapshot, el: Element) -> list[Element]:
    chain: list[Element] = []
    seen: set[str] = set()
    parent_ref = el.parent_ref
    while parent_ref and parent_ref not in seen:
        seen.add(parent_ref)
        parent = snap.element(parent_ref)
        if parent is None:
            break
        chain.append(parent)
        parent_ref = parent.parent_ref
    return chain


def _inspect_text(
    snap: Snapshot,
    el: Element,
    *,
    attributes: dict[str, Any],
    ancestors: list[Element],
    children: list[Element],
    live: bool,
) -> str:
    lines = [f"{format_element_line(el)}  · snapshot {snap.snapshot_id}"]
    if ancestors:
        path = " › ".join(
            f"{a.ref} {a.role}" + (f" {quote(a.name, 40)}" if a.name else "") for a in reversed(ancestors)
        )
        lines.append(f"path: {path} › {el.ref}")
    if el.identifier:
        lines.append(f"identifier: {el.identifier}")
    if live:
        lines.append("attributes:")
        lines.extend(f"  {key}: {truncate(str(value), 200)}" for key, value in attributes.items())
    else:
        lines.append("no live attributes (OCR text is acted on by its position).")
    if children:
        shown = ", ".join(
            f"{c.ref} {c.role}" + (f" {quote(c.name, 30)}" if c.name else "") for c in children[:20]
        )
        more = f" (+{len(children) - 20} more)" if len(children) > 20 else ""
        lines.append(f"children: {shown}{more}")
    return "\n".join(lines)


@tool(
    "inspect",
    title="Inspect an element",
    description=INSPECT_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="Inspect",
)
def inspect_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``inspect`` tool."""

    async def inspect(
        ref: RefArg, snapshot_id: SnapshotIdArg = None
    ) -> Annotated[CallToolResult, InspectModel]:
        snap, el = ctx.snapshots.resolve(ref, snapshot_id)
        ctx.policy.check_app(
            snap.app, kind="observe", window_title=snap.window.title if snap.window else None
        )
        attributes: dict[str, Any] = {}
        live = el.handle is not None
        if live:
            handle = await ctx.run_engine(
                live_handle, ctx.engine, snap, el, timeout_s=ctx.capture_timeout_s + ctx.engine_timeout_s
            )
            raw = await ctx.run_engine(ctx.engine.element_attributes, handle)
            attributes = _clean_attributes(raw, secure=el.secure)
        ancestors = _ancestors(snap, el)
        children = [child for child in snap.all_elements if child.parent_ref == el.ref]
        model = InspectModel(
            snapshot_id=snap.snapshot_id,
            element=ElementModel.model_validate(element_payload(el)),
            live=live,
            attributes=attributes,
            identifier=el.identifier,
            locator_path=[f"{role}[{index}]" for role, index in el.locator.path],
            ancestors=[_brief(a) for a in ancestors],
            children=[_brief(c) for c in children],
        )
        text = _inspect_text(
            snap, el, attributes=attributes, ancestors=ancestors, children=children, live=live
        )
        return ok_result(text, model)

    return inspect


# =========================================================================== ocr
OCR_DESCRIPTION = """\
Read the text on screen with OCR, with the position of each line.
Use it for text the accessibility tree does not expose (images, canvases, remote desktops,
games, some custom-drawn apps). For text in normal apps, snapshot(mode="text") is faster and exact.
Params: window_id | app | rect | display_id (at most one; default: the frontmost window),
languages (e.g. ["en-US"]). Returns lines top-to-bottom with their center points and a
screenshot_id for pixel mapping. To click a line, use snapshot(mode="vision") and its o-ref, or
the center point. Errors: PERMISSION_SCREEN_RECORDING, WINDOW_NOT_FOUND, APP_NOT_FOUND,
DISPLAY_NOT_FOUND, BLOCKED_BY_POLICY, TIMEOUT."""


async def _ocr_target(
    ctx: ServerContext, *, window_id: int | None, app: str | None, display_id: int | None, rect: Rect | None
) -> int | None:
    """Policy-check the OCR target; returns the window id to capture (``None``: display/region)."""
    if window_id is not None or app is not None:
        capture_window, _ = await _screenshot_target(ctx, window_id=window_id, app=app)
        return capture_window
    if display_id is not None or rect is not None:
        return None
    front = await ctx.resolve_app(None)
    if front is None:
        return None
    try:
        window = await _front_window(ctx, front)
    except AXPilotError as err:
        if err.code in (ErrorCode.WINDOW_NOT_FOUND, ErrorCode.APP_NOT_FOUND):
            return None
        raise
    ctx.policy.check_app(front, kind="capture", window_title=window.title)
    return window.window_id


def _ocr_line(line: OcrLine) -> OcrLineModel:
    return OcrLineModel(
        text=line.text,
        confidence=round(float(line.confidence), 3),
        frame=line.frame.to_list(),
        center=line.frame.center.to_list(),
    )


@tool(
    "ocr",
    title="Read text with OCR",
    description=OCR_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="OCR",
)
def ocr_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``ocr`` tool."""

    async def ocr(
        window_id: WindowIdArg = None,
        app: CaptureAppArg = None,
        rect: RectArg = None,
        display_id: DisplayIdArg = None,
        languages: Annotated[
            list[Annotated[str, Field(min_length=2, max_length=16)]] | None,
            Field(
                description='Recognition languages, e.g. ["en-US", "de-DE"]. Default: automatic.',
                max_length=8,
            ),
        ] = None,
    ) -> Annotated[CallToolResult, OcrModel]:
        _one_target(window_id=window_id, app=app, rect=rect, display_id=display_id)
        region = _region(rect)
        capture_window = await _ocr_target(
            ctx, window_id=window_id, app=app, display_id=display_id, rect=region
        )
        capture, _ = await capture_screen(
            ctx,
            display_id=display_id,
            window_id=capture_window,
            rect=region,
            max_edge=OCR_MAX_EDGE,
            fmt="png",
        )
        recognized = await ctx.run_engine(ctx.engine.ocr, capture, list(languages) if languages else None)
        screenshot_id = ctx.captures.put(capture)
        lines = [line for line in recognized if line.text.strip()]
        models = [_ocr_line(line) for line in lines]
        model = OcrModel(
            screenshot_id=screenshot_id,
            region=capture.region.to_list(),
            lines=models,
            count=len(models),
            text="\n".join(m.text for m in models),
        )
        plural = "s" if len(models) != 1 else ""
        header = f"ocr {screenshot_id}: {len(models)} line{plural} in {format_rect(capture.region)}"
        body = [f"{quote(line.text, 200)}  {format_point(line.frame.center)}" for line in lines]
        if len(body) > _OCR_TEXT_MAX_LINES:
            body = [
                *body[:_OCR_TEXT_MAX_LINES],
                f"… {len(body) - _OCR_TEXT_MAX_LINES} more lines in structuredContent",
            ]
        if not models:
            body = ["No text recognized."]
        return ok_result("\n".join([header, *body]), model)

    return ocr


# =========================================================================== list_apps / windows / displays
LIST_APPS_DESCRIPTION = """\
List running apps (name, bundle id, pid, frontmost/hidden), optionally with installed apps that
are not running. Use it to find the exact name or bundle id to pass as `app`, or to check
whether an app is running. Apps blocked by the safety policy are marked. Params: include_installed."""

LIST_WINDOWS_DESCRIPTION = """\
List on-screen windows front-to-back: window_id, app, title, frame, focused/minimized state.
Use it to pick a window_id for snapshot, screenshot or the window tool, or to see which windows
an app has open. Params: app (default: all apps). Titles of windows blocked by the safety policy
are withheld. Errors: APP_NOT_FOUND."""

LIST_DISPLAYS_DESCRIPTION = """\
List displays with their id, frame and work area (in points, origin top-left of the main
display) and scale factor. Use it before capturing or moving windows on multi-monitor setups."""


def _blocked(ctx: ServerContext, app: AppInfo, title: str | None = None) -> bool:
    try:
        ctx.policy.check_app(app, kind="observe", window_title=title)
    except AXPilotError:
        return True
    return False


def _app_line(app: AppModel) -> str:
    ident = f"{app.bundle_id}, pid {app.pid}" if app.bundle_id else f"pid {app.pid}"
    if not app.running:
        ident = app.bundle_id or app.path or "installed"
    flags = {
        "frontmost": app.frontmost,
        "hidden": app.hidden,
        "not running": not app.running,
        "blocked by policy": app.blocked,
    }
    states = [state for state, on in flags.items() if on]
    return f"{app.name} ({ident})" + (f"  {', '.join(states)}" if states else "")


@tool(
    "list_apps",
    title="List apps",
    description=LIST_APPS_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="ListApps",
)
def list_apps_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``list_apps`` tool."""

    async def list_apps(
        include_installed: Annotated[
            bool, Field(description="Also list installed apps that are not running.")
        ] = False,
    ) -> Annotated[CallToolResult, AppListModel]:
        apps = await ctx.run_engine(ctx.engine.list_apps, include_installed)
        models = [AppModel.from_info(app, blocked=_blocked(ctx, app)) for app in apps]
        models.sort(key=lambda a: (not a.running, not a.frontmost, a.name.casefold()))
        running = sum(1 for a in models if a.running)
        header = f"{running} running app{'s' if running != 1 else ''}" + (
            f", {len(models) - running} installed" if include_installed else ""
        )
        text = "\n".join([header + ":", *(_app_line(a) for a in models)])
        return ok_result(text, AppListModel(apps=models, count=len(models)))

    return list_apps


def _window_line(window: WindowModel) -> str:
    title = quote(window.title, 80) if not window.blocked else "(title hidden by policy)"
    flags = {"focused": window.focused, "minimized": window.minimized, "fullscreen": window.fullscreen}
    states = [state for state, on in flags.items() if on]
    frame = format_rect(Rect(*window.frame))
    return f"{window.window_id}  {window.app}  {title}  {frame}" + (
        f"  {', '.join(states)}" if states else ""
    )


@tool(
    "list_windows",
    title="List windows",
    description=LIST_WINDOWS_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="ListWindows",
)
def list_windows_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``list_windows`` tool."""

    async def list_windows(app: AppArg = None) -> Annotated[CallToolResult, WindowListModel]:
        windows = await ctx.run_engine(ctx.engine.list_windows, app)
        models = []
        for window in windows:
            owner = AppInfo(name=window.app, pid=window.pid, bundle_id=window.bundle_id)
            models.append(WindowModel.from_info(window, blocked=_blocked(ctx, owner, window.title)))
        header = f"{len(models)} window{'s' if len(models) != 1 else ''}" + (f" of {app}" if app else "")
        lines = [header + " (front to back):" if models else header + ".", *(_window_line(w) for w in models)]
        return ok_result("\n".join(lines), WindowListModel(windows=models, count=len(models)))

    return list_windows


@tool(
    "list_displays",
    title="List displays",
    description=LIST_DISPLAYS_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="DisplayInventory",
)
def list_displays_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``list_displays`` tool."""

    async def list_displays() -> Annotated[CallToolResult, DisplayListModel]:
        displays = await ctx.run_engine(ctx.engine.list_displays)
        models = [DisplayModel.from_info(d) for d in displays]
        lines = [f"{len(models)} display{'s' if len(models) != 1 else ''}:"]
        for d in displays:
            name = f" {quote(d.name)}" if d.name else ""
            main = "  main" if d.main else ""
            lines.append(
                f"{d.display_id}{name}  {format_rect(d.frame)}  scale {d.scale:g}{main}  "
                f"work area {format_rect(d.work_area)}"
            )
        return ok_result("\n".join(lines), DisplayListModel(displays=models, count=len(models)))

    return list_displays


# =========================================================================== wait_for
WAIT_FOR_DESCRIPTION = """\
Wait until something happens on screen (polling, at most 60 s), instead of sleeping and guessing.
Conditions: element (an element matching query/role appears; returns its ref), element_gone,
window (a window whose title contains query, or any window of app, opens), app_running (query =
app name or bundle id), text_visible (query appears as text; ocr=true also checks pixels), idle
(the app's UI stops changing for 0.5 s), duration (just wait timeout_ms).
Params: condition, query, role, app (default: frontmost app), timeout_ms (default 10000).
Returns what matched and how long it took. Errors: TIMEOUT (the condition never became true:
take a snapshot to see why), INVALID_ARGUMENT, BLOCKED_BY_POLICY, PERMISSION_ACCESSIBILITY."""


@dataclass(slots=True)
class _Match:
    element: ElementBriefModel | None = None
    snapshot_id: str | None = None
    window: WindowModel | None = None
    app: AppModel | None = None
    text: str | None = None
    detail: str = ""


Check = Callable[[float], Awaitable[_Match | None]]
"""One poll: given the remaining seconds, return a match or ``None`` (not yet)."""


def _require(condition: str, *, query: str | None, role: str | None, app: str | None) -> None:
    needs: dict[str, tuple[bool, str]] = {
        "element": (bool(query or role), "query (element text) or role"),
        "element_gone": (bool(query or role), "query (element text) or role"),
        "window": (bool(query or app), "query (window title) or app"),
        "app_running": (bool(query), "query (app name or bundle id)"),
        "text_visible": (bool(query), "query (the text)"),
    }
    ok, what = needs.get(condition, (True, ""))
    if not ok:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"wait_for(condition={condition!r}) needs {what}.",
            hint='Example: wait_for(condition="element", query="Save", role="button").',
        )


def _poll_timeout(ctx: ServerContext, remaining: float) -> float:
    return max(0.05, min(ctx.engine_timeout_s, remaining))


async def _poll_tree(ctx: ServerContext, app: str | None, remaining: float) -> RawTree | None:
    """The app's tree, or ``None`` while the app/window is not there yet."""
    try:
        await ctx.check_target(app=app, window_id=None, kind="observe")
        capture = functools.partial(
            ctx.engine.capture_tree,
            app=app,
            max_depth=ctx.config.perception.max_depth,
            max_nodes=ctx.config.perception.max_nodes,
            timeout_s=min(ctx.capture_timeout_s, max(remaining, 0.05)),
        )
        tree = await ctx.run_engine(capture, timeout_s=_poll_timeout(ctx, remaining) + ctx.capture_timeout_s)
    except AXPilotError as err:
        if err.code in _NOT_YET:
            return None
        raise
    ctx.policy.check_app(tree.app, kind="observe", window_title=tree.window.title if tree.window else None)
    return tree


def _element_check(
    ctx: ServerContext, *, query: str | None, role: str | None, app: str | None, gone: bool
) -> Check:
    async def check(remaining: float) -> _Match | None:
        tree = await _poll_tree(ctx, app, remaining)
        if tree is None:
            return _Match(detail="the app is not running") if gone else None
        snap = ctx.build_snapshot(tree, store=False)
        found = [(el, score) for el, score in find_elements(snap, text=query, role=role, limit=1)
                 if not query or score >= _ELEMENT_MIN_SCORE]  # fmt: skip
        if gone:
            return None if found else _Match(snapshot_id=None, detail="no matching element")
        if not found:
            return None
        ctx.snapshots.put(snap)
        el = found[0][0]
        return _Match(
            element=_brief(el),
            snapshot_id=snap.snapshot_id,
            detail=f"{format_element_line(el)} · snapshot {snap.snapshot_id}",
        )

    return check


def _window_check(ctx: ServerContext, *, query: str | None, app: str | None) -> Check:
    wanted = query.casefold() if query else None

    async def check(remaining: float) -> _Match | None:
        try:
            windows = await ctx.run_engine(
                ctx.engine.list_windows, app, timeout_s=_poll_timeout(ctx, remaining)
            )
        except AXPilotError as err:
            if err.code in _NOT_YET:
                return None
            raise
        for window in windows:
            owner = AppInfo(name=window.app, pid=window.pid, bundle_id=window.bundle_id)
            if _blocked(ctx, owner, window.title):
                continue
            if wanted is None or wanted in window.title.casefold():
                return _Match(
                    window=WindowModel.from_info(window), detail=_window_line(WindowModel.from_info(window))
                )
        return None

    return check


def _app_check(ctx: ServerContext, *, query: str) -> Check:
    wanted = query.strip().casefold()

    async def check(remaining: float) -> _Match | None:
        apps = await ctx.run_engine(ctx.engine.list_apps, timeout_s=_poll_timeout(ctx, remaining))
        for app in apps:
            if app.pid and (app.matches(query) or wanted in app.name.casefold()):
                model = AppModel.from_info(app, blocked=_blocked(ctx, app))
                return _Match(app=model, detail=_app_line(model))
        return None

    return check


def _node_texts(node: RawNode) -> list[str]:
    texts = [node.title, node.label, node.description, node.placeholder]
    if not node.secure and isinstance(node.value, str | int | float):
        texts.append(str(node.value))
    return [t for t in texts if isinstance(t, str) and t]


def _find_text(tree: RawTree, wanted: str) -> str | None:
    for node in tree.root.walk():
        if not node.visible:
            continue
        for text in _node_texts(node):
            if wanted in " ".join(text.split()).casefold():
                return text
    return None


def _text_check(ctx: ServerContext, *, query: str, app: str | None, ocr: bool) -> Check:
    wanted = " ".join(query.split()).casefold()

    async def check(remaining: float) -> _Match | None:
        tree = await _poll_tree(ctx, app, remaining)
        if tree is None:
            return None
        found = _find_text(tree, wanted)
        if found is None and ocr:
            snap = ctx.build_snapshot(tree, store=False)
            capture = await _window_capture(ctx, snap, OCR_MAX_EDGE, "png")
            lines = await ctx.run_engine(
                ctx.engine.ocr, capture, None, timeout_s=_poll_timeout(ctx, remaining)
            )
            found = next(
                (line.text for line in lines if wanted in " ".join(line.text.split()).casefold()), None
            )
        if found is None:
            return None
        return _Match(text=truncate(found, 500), detail=f"text {quote(found, 120)}")

    return check


def _tree_signature(tree: RawTree) -> int:
    parts = []
    for node in tree.root.walk():
        frame = node.frame.to_list() if node.frame is not None else None
        value = None if node.secure else node.value
        parts.append(
            (node.role, node.title, str(value), node.enabled, node.focused, node.selected, str(frame))
        )
    window = tree.window.window_id if tree.window is not None else None
    return hash((tree.app.pid, window, tuple(parts)))


def _idle_check(ctx: ServerContext, *, app: str | None) -> Check:
    last: list[Any] = [None, 0.0]  # signature, time it was first seen

    async def check(remaining: float) -> _Match | None:
        tree = await _poll_tree(ctx, app, remaining)
        if tree is None:
            return None
        signature, now = _tree_signature(tree), ctx.clock()
        if signature != last[0]:
            last[0], last[1] = signature, now
            return None
        quiet = now - last[1]
        if quiet >= IDLE_QUIET_S:
            return _Match(detail=f"{tree.app.name} unchanged for {quiet:.1f} s")
        return None

    return check


def _make_check(
    ctx: ServerContext, condition: str, *, query: str | None, role: str | None, app: str | None, ocr: bool
) -> Check | None:
    if condition in ("element", "element_gone"):
        return _element_check(ctx, query=query, role=role, app=app, gone=condition == "element_gone")
    if condition == "window":
        return _window_check(ctx, query=query, app=app)
    if condition == "app_running":
        assert query is not None
        return _app_check(ctx, query=query)
    if condition == "text_visible":
        assert query is not None
        return _text_check(ctx, query=query, app=app, ocr=ocr)
    if condition == "idle":
        return _idle_check(ctx, app=app)
    return None  # duration


async def _wait_duration(ctx: ServerContext, timeout_ms: int) -> int:
    start = ctx.clock()
    deadline = start + timeout_ms / 1000
    polls = 0
    while (remaining := deadline - ctx.clock()) > 0:
        polls += 1
        await ctx.report_progress((ctx.clock() - start) * 1000, timeout_ms, "waiting")
        await ctx.sleep(min(remaining, _POLL_MAX_S))
    return polls


async def _poll(
    ctx: ServerContext, check: Check, condition: str, query: str | None, timeout_ms: int
) -> tuple[_Match, int, float]:
    start = ctx.clock()
    deadline = start + timeout_ms / 1000
    interval, polls = _POLL_START_S, 0
    while True:
        polls += 1
        match = await check(max(deadline - ctx.clock(), 0.0))
        elapsed_ms = (ctx.clock() - start) * 1000
        if match is not None:
            return match, polls, elapsed_ms
        remaining = deadline - ctx.clock()
        if remaining <= 0:
            what = f"{condition} {quote(query)}" if query else condition
            raise AXPilotError(
                ErrorCode.TIMEOUT,
                f"wait_for timed out after {elapsed_ms / 1000:.1f} s: {what} did not happen.",
                hint="Take a snapshot to see the current state, then act or wait longer (up to 60000 ms).",
                details={
                    "condition": condition,
                    "query": query,
                    "waited_ms": round(elapsed_ms),
                    "polls": polls,
                },
            )
        await ctx.report_progress(elapsed_ms, timeout_ms, f"waiting for {condition}")
        await ctx.sleep(min(interval, remaining))
        interval = min(interval * _POLL_GROWTH, _POLL_MAX_S)


@tool(
    "wait_for",
    title="Wait for a condition",
    description=WAIT_FOR_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="WaitFor",
)
def wait_for_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``wait_for`` tool."""

    async def wait_for(
        *,
        condition: Annotated[
            WaitCondition, Field(description="What to wait for (see the tool description).")
        ],
        query: Annotated[
            str | None,
            Field(
                description="Element text, window title, app name/bundle id or text (depends on condition).",
                min_length=1,
                max_length=500,
            ),
        ] = None,
        role: Annotated[
            str | None,
            Field(
                description="Element role filter (button, field, row…) for element conditions.", max_length=64
            ),
        ] = None,
        app: AppArg = None,
        timeout_ms: Annotated[
            int,
            Field(
                description="Give up after this many ms (max 60000); for duration, how long to wait.",
                ge=0,
                le=WAIT_MAX_MS,
            ),
        ] = 10_000,
        ocr: Annotated[
            bool, Field(description="text_visible only: also look for the text with OCR (slower).")
        ] = False,
    ) -> Annotated[CallToolResult, WaitForModel]:
        _require(condition, query=query, role=role, app=app)
        check = _make_check(ctx, condition, query=query, role=role, app=app, ocr=ocr)
        if check is None:
            start = ctx.clock()
            polls = await _wait_duration(ctx, timeout_ms)
            waited = (ctx.clock() - start) * 1000
            model = WaitForModel(condition=condition, query=query, waited_ms=round(waited, 1), polls=polls)
            return ok_result(f"waited {waited / 1000:.1f} s", model)
        match, polls, waited = await _poll(ctx, check, condition, query, timeout_ms)
        model = WaitForModel(
            condition=condition,
            query=query,
            waited_ms=round(waited, 1),
            polls=polls,
            element=match.element,
            snapshot_id=match.snapshot_id,
            window=match.window,
            app=match.app,
            text=match.text,
        )
        text = f"{condition} after {waited / 1000:.1f} s ({polls} poll{'s' if polls != 1 else ''})"
        return ok_result(text + (f": {match.detail}" if match.detail else ""), model)

    return wait_for


# =========================================================================== status
STATUS_DESCRIPTION = """\
Report AXPilot's state: version, engine, macOS version, permissions (Accessibility, Screen
Recording), whether the user paused it (kill switch), the input lock, the active profile and
enabled tools, rate limit and confirmation mode.
Use it first when tools fail with permission errors, or to check what this server can do."""


async def _try(ctx: ServerContext, fn: Callable[[], Any], label: str, warnings: list[str]) -> Any:
    try:
        return await ctx.run_engine(fn)
    except AXPilotError as err:
        warnings.append(f"Could not read {label}: {err.code.value}: {err.message}")
        return None


def _permission_line(perms: PermissionsModel | None) -> str:
    if perms is None:
        return "permissions: unknown"

    def word(ok: bool) -> str:
        return "granted" if ok else "MISSING"

    line = (
        f"permissions: accessibility {word(perms.accessibility)} · "
        f"screen recording {word(perms.screen_recording)}"
    )
    if perms.responsible_app:
        line += f" (granted to {perms.responsible_app})"
    if not perms.accessibility:
        line += ". Grant Accessibility in System Settings → Privacy & Security, or run `axpilot doctor`."
    return line


def _host_info(ctx: ServerContext, warnings: list[str]) -> dict[str, Any]:
    if ctx.host_info is None:
        return {}
    try:
        return dict(ctx.host_info())
    except Exception as exc:  # host extras are best effort (A7): logged and warned
        _log.warning("host_info failed: %s", exc, exc_info=True)
        warnings.append("Could not read pairing and client information from the host.")
        return {}


def _status_text(model: StatusModel) -> str:
    control = model.control
    if control.paused:
        reason = control.pause_reason or "kill switch"
        state = f"PAUSED ({reason}): input actions are blocked until the user resumes"
    else:
        state = "running" + (" · yields to the user" if control.yield_to_user else "")
        state += f" · input busy ({control.current_action})" if control.busy else " · input idle"
    tools = ", ".join(model.enabled_tools)
    lines = [
        f"{model.server} {model.version} · engine {model.engine} · "
        f"macOS {model.os_version or '?'} · {model.mode} mode",
        _permission_line(model.permissions),
        f"control: {state}",
        f"safety: confirm {model.confirm} · {model.rate_limit.max_per_minute} input actions/min "
        f"({model.rate_limit.remaining} left) · audit {'on' if model.audit_enabled else 'off'}",
        f"profile {model.profile} (naming {model.naming}): {len(model.enabled_tools)} tools: {tools}",
    ]
    if model.client:
        lines.append(f"client: {model.client} · pairing {model.pairing}")
    if model.frontmost_app is not None:
        app = model.frontmost_app
        lines.append(f"frontmost: {app.name} ({app.bundle_id or 'pid ' + str(app.pid)})")
    return "\n".join(lines)


@tool(
    "status",
    title="AXPilot status",
    description=STATUS_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="ControlStatus",
)
def status_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``status`` tool."""

    async def status() -> Annotated[CallToolResult, StatusModel]:
        warnings: list[str] = []
        permissions = await _try(ctx, ctx.engine.permissions, "permissions", warnings)
        os_version = await _try(ctx, ctx.engine.os_version, "the macOS version", warnings)
        front = await _try(ctx, ctx.engine.frontmost_app, "the frontmost app", warnings)
        host = _host_info(ctx, warnings)
        model = StatusModel(
            server=SERVER_NAME,
            version=__version__,
            engine=ctx.engine.name,
            os_version=os_version,
            mode=ctx.mode,
            client=ctx.client_name,
            pairing=str(host.get("pairing", "unknown")),
            clients=host.get("clients"),
            permissions=PermissionsModel.from_status(permissions) if permissions is not None else None,
            control=ControlModel.model_validate(ctx.control.to_dict()),
            profile=ctx.profile,
            naming=ctx.naming,
            enabled_tools=list(ctx.enabled),
            confirm=ctx.config.safety.confirm,
            rate_limit=RateLimitModel(
                max_per_minute=ctx.limiter.max_per_minute, remaining=ctx.limiter.remaining()
            ),
            audit_enabled=ctx.audit.enabled,
            frontmost_app=AppRefModel(name=front.name, bundle_id=front.bundle_id, pid=front.pid)
            if front
            else None,
        )
        return ok_result(_status_text(model), model, warnings=warnings)

    return status
