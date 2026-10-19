#!/usr/bin/env python3
"""Spike S4: ScreenCaptureKit from Python (PyObjC): latency, exclusion filters, permission prompts.

Requires macOS 14+, ``pip install pyobjc-framework-ScreenCaptureKit pyobjc-framework-Quartz``,
and Screen Recording permission for the app running this script.

    python capture_probe.py latency --runs 10                      # display, full size and max-edge 1280
    python capture_probe.py latency --window-of TextEdit           # plus one window
    python capture_probe.py exclude --app TextEdit --save          # is an excluded app really gone?
    python capture_probe.py status [--request] [--read-approvals]  # permission state and prompt records

``latency`` times ``SCScreenshotManager.captureImageWithFilter`` (from the call to the completion
handler) and JPEG encoding through ImageIO. ``exclude`` captures the display with and without
``SCContentFilter(display:excludingApplications:)`` and compares pixels inside the app's window
(they must change) and outside it (they should not). Results go to ``results/``; ``--save`` also
writes the PNGs there (do not commit them: they show your screen).
"""

from __future__ import annotations

import argparse
import json
import plistlib
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s4_capture"
SPIKE_DIR = Path(__file__).resolve().parent
APPROVALS_PLIST = (
    Path.home() / "Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist"
)
EXCLUSION_THRESHOLD = 8.0  # mean absolute difference per channel (0-255) that counts as "changed"

Rect = tuple[int, int, int, int]  # x, y, width, height in pixels


# --- pure helpers (unit-tested on any OS) --------------------------------------------------------


def scaled_size(width: int, height: int, max_edge: int | None) -> tuple[int, int]:
    """``(width, height)`` scaled down so the longer edge is at most ``max_edge``."""
    if not max_edge or max(width, height) <= max_edge:
        return width, height
    factor = max_edge / max(width, height)
    return max(1, round(width * factor)), max(1, round(height * factor))


def to_pixel_rect(
    frame: Rect | Sequence[float], origin: Sequence[float], scale: float, size: Sequence[int]
) -> Rect:
    """A frame in global points to a pixel rectangle in an image of the display at ``origin``, clamped."""
    x = round((frame[0] - origin[0]) * scale)
    y = round((frame[1] - origin[1]) * scale)
    right = min(size[0], x + round(frame[2] * scale))
    bottom = min(size[1], y + round(frame[3] * scale))
    x, y = max(0, x), max(0, y)
    return (x, y, max(0, right - x), max(0, bottom - y))


def region_mean_abs_diff(
    first: bytes,
    second: bytes,
    *,
    bytes_per_row: int,
    bytes_per_pixel: int,
    size: Sequence[int],
    rect: Rect,
    inside: bool = True,
    step: int = 8,
) -> float | None:
    """Mean absolute per-channel difference of two same-format bitmaps, sampled every ``step``
    pixels inside (or outside) ``rect``. ``None`` if no pixel was sampled."""
    x0, y0, w, h = rect
    total, count = 0, 0
    for y in range(0, size[1], step):
        for x in range(0, size[0], step):
            if (x0 <= x < x0 + w and y0 <= y < y0 + h) != inside:
                continue
            offset = y * bytes_per_row + x * bytes_per_pixel
            for channel in range(min(3, bytes_per_pixel)):
                total += abs(first[offset + channel] - second[offset + channel])
                count += 1
    return total / count if count else None


def exclusion_verdict(inside: float | None, outside: float | None) -> dict[str, Any]:
    changed = inside is not None and inside >= EXCLUSION_THRESHOLD
    quiet = outside is None or (inside is not None and outside < inside / 3)
    return {
        "inside_diff": inside,
        "outside_diff": outside,
        "excluded_region_changed": changed,
        "rest_stable": quiet,
    }


# --- macOS side ----------------------------------------------------------------------------------


class Capture:
    """ScreenCaptureKit, Quartz and Foundation via PyObjC (imported lazily)."""

    def __init__(self) -> None:
        import Foundation
        import Quartz
        import ScreenCaptureKit

        self.sck, self.q, self.foundation = ScreenCaptureKit, Quartz, Foundation

    def call(
        self, start: Callable[[Callable[..., None]], None], timeout: float = 15.0
    ) -> tuple[float, tuple]:
        """Run an async API; returns (seconds until the completion handler ran, its arguments)."""
        done = threading.Event()
        box: dict[str, Any] = {}

        def handler(*results: Any) -> None:
            box["at"] = time.perf_counter()
            box["results"] = results
            done.set()

        started = time.perf_counter()
        start(handler)
        deadline = time.monotonic() + timeout
        run_loop = self.foundation.NSRunLoop.currentRunLoop()
        while not done.is_set():
            if time.monotonic() > deadline:
                raise TimeoutError("ScreenCaptureKit did not call back")
            run_loop.runUntilDate_(self.foundation.NSDate.dateWithTimeIntervalSinceNow_(0.005))
        return box["at"] - started, box["results"]

    def content(self) -> Any:
        sc = self.sck.SCShareableContent
        _, (content, error) = self.call(
            lambda h: sc.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                True, True, h
            )
        )
        if content is None:
            raise RuntimeError(f"no shareable content (Screen Recording permission?): {error}")
        return content

    def display_filter(self, display: Any, exclude_apps: Sequence[Any] = ()) -> Any:
        if exclude_apps:
            return self.sck.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                display, list(exclude_apps), []
            )
        return self.sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])

    def window_filter(self, window: Any) -> Any:
        return self.sck.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)

    def config(self, content_filter: Any, max_edge: int | None) -> tuple[Any, tuple[int, int]]:
        rect = content_filter.contentRect()
        scale = content_filter.pointPixelScale()
        size = scaled_size(round(rect.size.width * scale), round(rect.size.height * scale), max_edge)
        config = self.sck.SCStreamConfiguration.alloc().init()
        config.setWidth_(size[0])
        config.setHeight_(size[1])
        config.setShowsCursor_(False)
        return config, size

    def screenshot(self, content_filter: Any, max_edge: int | None) -> tuple[float, Any]:
        config, _ = self.config(content_filter, max_edge)
        manager = self.sck.SCScreenshotManager
        seconds, (image, error) = self.call(
            lambda h: manager.captureImageWithFilter_configuration_completionHandler_(
                content_filter, config, h
            )
        )
        if image is None:
            raise RuntimeError(f"capture failed: {error}")
        return seconds, image

    def bitmap(self, image: Any) -> dict[str, Any]:
        q = self.q
        data = q.CGDataProviderCopyData(q.CGImageGetDataProvider(image))
        return {
            "bytes": bytes(data),
            "bytes_per_row": q.CGImageGetBytesPerRow(image),
            "bytes_per_pixel": q.CGImageGetBitsPerPixel(image) // 8,
            "size": (q.CGImageGetWidth(image), q.CGImageGetHeight(image)),
        }

    def encode_jpeg(self, image: Any, quality: float = 0.8) -> tuple[float, int]:
        q = self.q
        data = self.foundation.NSMutableData.data()
        started = time.perf_counter()
        destination = q.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
        q.CGImageDestinationAddImage(
            destination, image, {q.kCGImageDestinationLossyCompressionQuality: quality}
        )
        q.CGImageDestinationFinalize(destination)
        return time.perf_counter() - started, data.length()

    def save_png(self, image: Any, path: Path) -> None:
        q = self.q
        path.parent.mkdir(parents=True, exist_ok=True)
        url = self.foundation.NSURL.fileURLWithPath_(str(path))
        destination = q.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        q.CGImageDestinationAddImage(destination, image, None)
        q.CGImageDestinationFinalize(destination)


def frame_tuple(frame: Any) -> tuple[float, float, float, float]:
    return (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)


def find_windows(content: Any, app_name: str) -> list[Any]:
    wanted = app_name.casefold()
    windows = []
    for window in content.windows():
        owner = window.owningApplication()
        if owner is None or window.windowLayer() != 0:
            continue
        if wanted in (
            (owner.applicationName() or "").casefold(),
            (owner.bundleIdentifier() or "").casefold(),
        ):
            windows.append(window)
    return sorted(windows, key=lambda w: w.frame().size.width * w.frame().size.height, reverse=True)


def display_for(content: Any, window: Any | None) -> Any:
    """The display containing the window's centre (else the first display)."""
    displays = list(content.displays())
    if window is not None:
        x, y, w, h = frame_tuple(window.frame())
        for display in displays:
            dx, dy, dw, dh = frame_tuple(display.frame())
            if dx <= x + w / 2 < dx + dw and dy <= y + h / 2 < dy + dh:
                return display
    return displays[0]


def measure(capture: Capture, content_filter: Any, runs: int, max_edge: int | None) -> dict[str, Any]:
    capture.screenshot(content_filter, max_edge)  # warm-up: the first call sets up the capture session
    capture_ms, encode_ms, sizes = [], [], []
    image = None
    for _ in range(runs):
        seconds, image = capture.screenshot(content_filter, max_edge)
        capture_ms.append(seconds * 1000)
        encode_seconds, size = capture.encode_jpeg(image)
        encode_ms.append(encode_seconds * 1000)
        sizes.append(size)
    q = capture.q
    return {
        "max_edge": max_edge,
        "image_size": [q.CGImageGetWidth(image), q.CGImageGetHeight(image)] if image is not None else None,
        "capture_ms": spikelib.summarize(capture_ms),
        "jpeg_encode_ms": spikelib.summarize(encode_ms),
        "jpeg_bytes": spikelib.summarize(sizes, digits=0),
    }


def command_latency(capture: Capture, args: argparse.Namespace) -> dict[str, Any]:
    content = capture.content()
    display = display_for(content, None)
    display_filter = capture.display_filter(display)
    result: dict[str, Any] = {
        "display": {"id": display.displayID(), "frame": frame_tuple(display.frame())},
        "point_pixel_scale": display_filter.pointPixelScale(),
        "display_full": measure(capture, display_filter, args.runs, None),
        "display_max_edge": measure(capture, display_filter, args.runs, args.max_edge),
    }
    if args.window_of:
        windows = find_windows(content, args.window_of)
        if windows:
            result["window"] = measure(capture, capture.window_filter(windows[0]), args.runs, args.max_edge)
            result["window"]["title"] = windows[0].title()
        else:
            result["window"] = {"error": f"no on-screen window of {args.window_of}"}
    return result


def command_exclude(capture: Capture, args: argparse.Namespace) -> dict[str, Any]:
    content = capture.content()
    windows = find_windows(content, args.app)
    if not windows:
        return {"error": f"no on-screen window of {args.app}; bring one to the front"}
    window = windows[0]
    app = window.owningApplication()
    display = display_for(content, window)
    plain = capture.display_filter(display)
    excluded = capture.display_filter(display, [app])
    _, with_app = capture.screenshot(plain, None)
    _, without_app = capture.screenshot(excluded, None)
    a, b = capture.bitmap(with_app), capture.bitmap(without_app)
    if a["size"] != b["size"] or a["bytes_per_row"] != b["bytes_per_row"]:
        return {"error": f"the two captures differ in format: {a['size']} vs {b['size']}"}
    rect = to_pixel_rect(
        frame_tuple(window.frame()), frame_tuple(display.frame()), plain.pointPixelScale(), a["size"]
    )
    common = {"bytes_per_row": a["bytes_per_row"], "bytes_per_pixel": a["bytes_per_pixel"], "size": a["size"]}
    inside = region_mean_abs_diff(a["bytes"], b["bytes"], rect=rect, inside=True, **common)
    outside = region_mean_abs_diff(a["bytes"], b["bytes"], rect=rect, inside=False, **common)
    result = {"app": app.applicationName(), "bundle_id": app.bundleIdentifier(), "window_pixels": rect}
    result.update(exclusion_verdict(inside, outside))
    if args.save:
        stamp = spikelib.utc_stamp()
        for name, image in (("with-app", with_app), ("without-app", without_app)):
            path = SPIKE_DIR / "results" / f"{stamp}-{name}.png"
            capture.save_png(image, path)
            result.setdefault("saved", []).append(str(path))
    return result


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    import Quartz

    result: dict[str, Any] = {"preflight": bool(Quartz.CGPreflightScreenCaptureAccess())}
    if args.request and not result["preflight"]:
        result["request_result"] = bool(Quartz.CGRequestScreenCaptureAccess())
    if args.read_approvals:
        result["approvals"] = read_approvals()
    return result


def read_approvals() -> Any:
    """macOS 15+ records the per-app "Allow for one month" approvals here (reading it may prompt)."""
    try:
        with APPROVALS_PLIST.open("rb") as handle:
            return plistlib.load(handle)
    except FileNotFoundError:
        return {"error": f"{APPROVALS_PLIST} does not exist (macOS 14, or no approval yet)"}
    except (OSError, plistlib.InvalidFileException) as exc:
        return {"error": f"cannot read {APPROVALS_PLIST}: {exc}"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label")
    commands = parser.add_subparsers(dest="command", required=True)
    latency = commands.add_parser("latency", help="time display (and window) screenshots")
    latency.add_argument("--runs", type=int, default=10)
    latency.add_argument("--max-edge", type=int, default=1280)
    latency.add_argument("--window-of", help="also capture this app's largest window")
    exclude = commands.add_parser("exclude", help="check SCContentFilter app exclusion")
    exclude.add_argument("--app", required=True, help="app name or bundle id with a visible window")
    exclude.add_argument("--save", action="store_true", help="write both captures as PNG to results/")
    status = commands.add_parser("status", help="Screen Recording permission state")
    status.add_argument("--request", action="store_true", help="call CGRequestScreenCaptureAccess()")
    status.add_argument("--read-approvals", action="store_true", help="read replayd's approvals plist")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spikelib.require_macos("S4")
    report = spikelib.result_header(SPIKE + "_" + args.command, args.label)
    try:
        if args.command == "status":
            report["result"] = command_status(args)
        else:
            capture = Capture()
            command = command_latency if args.command == "latency" else command_exclude
            report["result"] = command(capture, args)
    except ImportError as exc:
        sys.exit(f"PyObjC is missing ({exc}). Run: pip install pyobjc-framework-ScreenCaptureKit")
    except (RuntimeError, TimeoutError) as exc:
        report["result"] = {"error": str(exc)}
    print(json.dumps(report["result"], indent=2, default=str))
    for path in spikelib.write_results(SPIKE_DIR, report, label=args.label or args.command):
        print(f"wrote {path}")
    return 0 if "error" not in report["result"] else 1


if __name__ == "__main__":
    sys.exit(main())
