"""Screenshots through ScreenCaptureKit, with a CoreGraphics fallback.

Permission: **Screen Recording** (System Settings → Privacy & Security → Screen & System Audio
Recording). It is checked with ``CGPreflightScreenCaptureAccess()``, which never prompts.
macOS answers that per process and remembers it, so after the user grants access AXPilot
has to be restarted. macOS 15+ also asks again about once a month.

How a capture works (macOS 14+):

1. ``SCShareableContent`` lists displays, windows and apps (completion handler, bounded wait).
2. An ``SCContentFilter`` selects what to show: a display without the windows of the apps in
   ``exclude_bundle_ids`` and of AXPilot itself (its overlay), or one window on its own
   (``desktopIndependentWindow``, without its shadow, so the image matches its frame).
3. ``SCScreenshotManager.captureImageWithFilter:configuration:completionHandler:`` renders it
   directly at the output size (the region at the display's backing scale, downscaled so the
   longest edge is at most ``max_edge``) and, for rect captures, only the ``sourceRect``.
4. The ``CGImage`` is encoded with ``NSBitmapImageRep`` as PNG or JPEG. JPEG images are first
   flattened onto white (window corners are transparent).

If ScreenCaptureKit is unavailable, fails unexpectedly or times out, ``CGWindowListCreateImage`` is used
instead (deprecated, but present); windows of excluded apps are then painted over in black.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any

import AppKit
import Quartz as Q

from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.sync import call_with_completion, ns_error_code, ns_error_text
from axpilot.engine.macos.windows import cg_window, cg_windows
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Capture, DisplayInfo, ImageFormat, Rect

_log = logging.getLogger(__name__)

SHAREABLE_TIMEOUT_S = 5.0
CAPTURE_TIMEOUT_S = 10.0
SC_USER_DECLINED = -3801
"""``SCStreamErrorUserDeclined``: Screen Recording was denied."""


def screen_capture_allowed() -> bool:
    """``CGPreflightScreenCaptureAccess()`` (never prompts)."""
    return bool(Q.CGPreflightScreenCaptureAccess())


def _screen_capture_kit() -> Any | None:
    try:
        import ScreenCaptureKit as SCK
    except ImportError:
        return None
    try:
        SCK.SCScreenshotManager  # noqa: B018 - macOS 14+ only; AttributeError before that
    except AttributeError:
        return None
    return SCK


def _set_if_supported(obj: Any, selector: str, value: Any) -> None:
    """Call an optional setter (``"setScalesToFit:"``) only where the OS implements it."""
    if obj.respondsToSelector_(selector):
        getattr(obj, selector.replace(":", "_"))(value)


def _sck_error(error: Any, what: str) -> AXPilotError:
    if ns_error_code(error) == SC_USER_DECLINED:
        return AXPilotError(
            ErrorCode.PERMISSION_SCREEN_RECORDING, f"{what} failed: Screen Recording was denied."
        )
    return AXPilotError(ErrorCode.INTERNAL, f"{what} failed: {ns_error_text(error)}")


# ------------------------------------------------------------------ encoding


def _cg_rect(rect: Rect) -> Any:
    return Q.CGRectMake(rect.x, rect.y, rect.w, rect.h)


def _redraw(image: Any, width: int, height: int, *, opaque: bool, redact: Sequence[Rect] = ()) -> Any:
    """Draw ``image`` into a new sRGB bitmap of ``width``×``height`` (scaling it), optionally
    flattened onto white and with ``redact`` rects (image-relative, top-left origin, in output
    pixels) painted black."""
    space = Q.CGColorSpaceCreateWithName(Q.kCGColorSpaceSRGB)
    info = Q.kCGImageAlphaNoneSkipLast if opaque else Q.kCGImageAlphaPremultipliedLast
    context = Q.CGBitmapContextCreate(None, width, height, 8, 0, space, info)
    if context is None:
        raise AXPilotError(ErrorCode.INTERNAL, f"Could not create a {width}×{height} bitmap.")
    Q.CGContextSetInterpolationQuality(context, Q.kCGInterpolationHigh)
    full = Q.CGRectMake(0, 0, width, height)
    if opaque:
        Q.CGContextSetRGBFillColor(context, 1.0, 1.0, 1.0, 1.0)
        Q.CGContextFillRect(context, full)
    Q.CGContextDrawImage(context, full, image)
    if redact:
        Q.CGContextSetRGBFillColor(context, 0.0, 0.0, 0.0, 1.0)
        for rect in redact:
            # Bitmap contexts have their origin at the bottom-left.
            Q.CGContextFillRect(context, Q.CGRectMake(rect.x, height - rect.y - rect.h, rect.w, rect.h))
    result = Q.CGBitmapContextCreateImage(context)
    if result is None:
        raise AXPilotError(ErrorCode.INTERNAL, "Could not render the screenshot.")
    return result


def encode_image(
    image: Any, fmt: ImageFormat, quality: int, max_edge: int, *, redact: Sequence[Rect] = ()
) -> tuple[bytes, int, int]:
    """Encode a ``CGImage`` as PNG/JPEG no larger than ``max_edge``. Returns
    ``(data, width, height)``. ``redact`` rects are in source-image pixels."""
    source_w, source_h = int(Q.CGImageGetWidth(image)), int(Q.CGImageGetHeight(image))
    width, height = h.fit_size(source_w, source_h, max_edge)
    scale_x, scale_y = width / max(source_w, 1), height / max(source_h, 1)
    scaled_redact = [Rect(r.x * scale_x, r.y * scale_y, r.w * scale_x, r.h * scale_y) for r in redact]
    if fmt == "jpeg" or (width, height) != (source_w, source_h) or scaled_redact:
        image = _redraw(image, width, height, opaque=fmt == "jpeg", redact=scaled_redact)
    rep = AppKit.NSBitmapImageRep.alloc().initWithCGImage_(image)
    if fmt == "png":
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    else:
        properties = {AppKit.NSImageCompressionFactor: max(1, min(quality, 100)) / 100.0}
        data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypeJPEG, properties)
    if data is None:
        raise AXPilotError(ErrorCode.INTERNAL, f"Could not encode the screenshot as {fmt}.")
    return bytes(data), width, height


# ------------------------------------------------------------------ ScreenCaptureKit


def _shareable_content(SCK: Any) -> Any:
    content, error = call_with_completion(
        lambda handler: (
            SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                False, True, handler
            )
        ),
        SHAREABLE_TIMEOUT_S,
        "Listing the screen content",
    )
    if error is not None or content is None:
        raise _sck_error(error, "Listing the screen content")
    return content


def _sck_capture(
    SCK: Any,
    plan: h.CapturePlan,
    window_id: int | None,
    exclude_bundle_ids: frozenset[str],
) -> Any:
    content = _shareable_content(SCK)
    config = SCK.SCStreamConfiguration.alloc().init()
    config.setWidth_(plan.width)
    config.setHeight_(plan.height)
    config.setShowsCursor_(False)
    _set_if_supported(config, "setScalesToFit:", True)
    _set_if_supported(config, "setPreservesAspectRatio:", True)
    if window_id is not None:
        window = next((w for w in list(content.windows() or ()) if int(w.windowID()) == window_id), None)
        if window is None:
            raise AXPilotError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"Window {window_id} is not on screen.",
                hint="Restore or show the window first (the `window` tool), then retry.",
            )
        content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        _set_if_supported(config, "setIgnoreShadowsSingleWindow:", True)
    else:
        display = next(
            (d for d in list(content.displays() or ()) if int(d.displayID()) == plan.display.display_id), None
        )
        if display is None:
            raise AXPilotError(
                ErrorCode.DISPLAY_NOT_FOUND, f"Display {plan.display.display_id} is not available."
            )
        own_pid = os.getpid()
        excluded = [
            app
            for app in list(content.applications() or ())
            if int(app.processID()) == own_pid or (app.bundleIdentifier() or "") in exclude_bundle_ids
        ]
        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
            display, excluded, []
        )
        _set_if_supported(content_filter, "setIncludeMenuBar:", True)
        if plan.source_rect is not None:
            config.setSourceRect_(_cg_rect(plan.source_rect))
    image, error = call_with_completion(
        lambda handler: SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter, config, handler
        ),
        CAPTURE_TIMEOUT_S,
        "Taking the screenshot",
    )
    if error is not None or image is None:
        raise _sck_error(error, "Taking the screenshot")
    return image


# ------------------------------------------------------------------ CoreGraphics fallback


def _legacy_capture(plan: h.CapturePlan, window_id: int | None) -> Any:
    if window_id is not None:
        image = Q.CGWindowListCreateImage(
            Q.CGRectNull,
            Q.kCGWindowListOptionIncludingWindow,
            window_id,
            Q.kCGWindowImageBoundsIgnoreFraming | Q.kCGWindowImageBestResolution,
        )
    else:
        image = Q.CGWindowListCreateImage(
            _cg_rect(plan.region),
            Q.kCGWindowListOptionOnScreenOnly,
            Q.kCGNullWindowID,
            Q.kCGWindowImageBestResolution,
        )
    if image is None:
        raise AXPilotError(
            ErrorCode.PERMISSION_SCREEN_RECORDING,
            "CoreGraphics returned no image (Screen Recording may be denied).",
        )
    return image


def _redaction_rects(
    image: Any,
    region: Rect,
    exclude_bundle_ids: frozenset[str],
    bundle_of: Callable[[int], str | None],
) -> list[Rect]:
    """Frames of excluded apps' on-screen windows, in image pixels."""
    if not exclude_bundle_ids:
        return []
    own_pid = os.getpid()
    scale_x = int(Q.CGImageGetWidth(image)) / max(region.w, 1.0)
    scale_y = int(Q.CGImageGetHeight(image)) / max(region.h, 1.0)
    rects = []
    for window in cg_windows(on_screen_only=True):
        if window.pid != own_pid and (bundle_of(window.pid) or "") not in exclude_bundle_ids:
            continue
        overlap = window.frame.intersection(region)
        if overlap is not None:
            rects.append(
                Rect(
                    (overlap.x - region.x) * scale_x,
                    (overlap.y - region.y) * scale_y,
                    overlap.w * scale_x,
                    overlap.h * scale_y,
                )
            )
    return rects


# ------------------------------------------------------------------ entry point


def take_screenshot(
    displays: list[DisplayInfo],
    *,
    display_id: int | None,
    window_id: int | None,
    rect: Rect | None,
    max_edge: int,
    fmt: ImageFormat,
    quality: int,
    exclude_bundle_ids: Sequence[str],
    bundle_of: Callable[[int], str | None],
) -> Capture:
    """Capture a rect, a window or a display (see the module docstring)."""
    if fmt not in ("png", "jpeg"):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown image format {fmt!r}; use png or jpeg.")
    if max_edge < 1 or not 1 <= quality <= 100:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "max_edge must be ≥ 1 and quality between 1 and 100.")
    if not screen_capture_allowed():
        raise AXPilotError(
            ErrorCode.PERMISSION_SCREEN_RECORDING, "Screen Recording permission is not granted."
        )
    excluded = frozenset(exclude_bundle_ids)
    window_frame: Rect | None = None
    capture_window = window_id if rect is None else None
    if capture_window is not None:
        cg = cg_window(capture_window)
        if cg is None:
            raise AXPilotError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"No window has id {capture_window}.",
                hint="Use `list_windows` to see current window ids.",
            )
        if (bundle_of(cg.pid) or "") in excluded:
            raise AXPilotError(
                ErrorCode.BLOCKED_BY_POLICY,
                f"Window {capture_window} belongs to {cg.owner or 'an app'}, "
                "which is excluded from screenshots.",
            )
        window_frame = cg.frame
    plan = h.capture_plan(
        displays, display_id=display_id, window_frame=window_frame, rect=rect, max_edge=max_edge
    )
    Q.CGMainDisplayID()  # makes sure this process has a window-server connection before SCK runs
    SCK = _screen_capture_kit()
    image: Any = None
    redact: list[Rect] = []
    if SCK is not None:
        try:
            image = _sck_capture(SCK, plan, capture_window, excluded)
        except AXPilotError as exc:
            # ponytail: a hung ScreenCaptureKit (seen on macOS 26, #51) falls back too, but CoreGraphics
            # was slow in that state as well (30 s); the tool's engine_timeout_s still bounds the call.
            if exc.code not in (ErrorCode.INTERNAL, ErrorCode.TIMEOUT):
                raise
            _log.warning("ScreenCaptureKit failed (%s); using CGWindowListCreateImage", exc.message)
    if image is None:
        image = _legacy_capture(plan, capture_window)
        if capture_window is None:
            redact = _redaction_rects(image, plan.region, excluded, bundle_of)
    data, width, height = encode_image(image, fmt, quality, max_edge, redact=redact)
    return Capture(data=data, format=fmt, width=width, height=height, region=plan.region)
