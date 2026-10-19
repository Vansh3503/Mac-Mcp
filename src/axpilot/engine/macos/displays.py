"""Displays: ids, frames, work areas, scale factors and names.

Permission: none.

CoreGraphics provides the display ids and frames (``CGDisplayBounds`` is already in global
points with the origin at the top-left of the main display). ``NSScreen`` adds what only
AppKit knows: the work area (``visibleFrame``, without the menu bar and Dock), the backing
scale factor and the localized name. Cocoa rects have their origin at the *bottom*-left of
the primary display with y pointing up, so they are flipped using the main display's height.

Threading: ``NSScreen`` is read from worker threads, which AppKit tolerates for these
read-only properties. AppKit refreshes the screen list on the main run loop, so in a process
without one (a plain script) the work areas can be stale after a display change; the CG
frames never are.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import AppKit
import Quartz as Q

from axpilot.engine.macos import helpers as h
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import DisplayInfo, Rect

_log = logging.getLogger(__name__)

MAX_DISPLAYS = 32
CACHE_TTL_S = 1.0
"""Display lists are reused this long (hit tests and screenshots need them often)."""


def _cg_rect(rect: Any) -> Rect:
    return Rect(float(rect.origin.x), float(rect.origin.y), float(rect.size.width), float(rect.size.height))


def _active_display_ids() -> list[int]:
    err, ids, count = Q.CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
    if err != 0:
        raise AXPilotError(ErrorCode.INTERNAL, f"CGGetActiveDisplayList failed with CGError {err}.")
    active = [int(display_id) for display_id in list(ids or ())[: int(count)]]
    # Software mirroring lists the mirror too; keep one display per mirror set.
    return [d for d in active if not (Q.CGDisplayIsInMirrorSet(d) and Q.CGDisplayMirrorsDisplay(d))]


def _screens_by_id() -> dict[int, Any]:
    screens: dict[int, Any] = {}
    for screen in list(AppKit.NSScreen.screens() or ()):
        number = screen.deviceDescription().objectForKey_("NSScreenNumber")
        if number is not None:
            screens[int(number)] = screen
    return screens


def _mode_scale(display_id: int) -> float:
    mode = Q.CGDisplayCopyDisplayMode(display_id)
    if mode is None:
        return 1.0
    points = Q.CGDisplayModeGetWidth(mode)
    pixels = Q.CGDisplayModeGetPixelWidth(mode)
    return float(pixels) / float(points) if points else 1.0


def read_displays() -> list[DisplayInfo]:
    """All active displays, the main display first, then left-to-right, top-to-bottom."""
    main_id = int(Q.CGMainDisplayID())
    primary_height = float(Q.CGDisplayBounds(main_id).size.height)
    screens = _screens_by_id()
    displays: list[DisplayInfo] = []
    for display_id in _active_display_ids():
        frame = _cg_rect(Q.CGDisplayBounds(display_id))
        screen = screens.get(display_id)
        work_area, scale, name = frame, _mode_scale(display_id), None
        if screen is not None:
            visible = screen.visibleFrame()
            work_area = h.cocoa_to_global(
                float(visible.origin.x),
                float(visible.origin.y),
                float(visible.size.width),
                float(visible.size.height),
                primary_height,
            )
            scale = float(screen.backingScaleFactor())
            if screen.respondsToSelector_("localizedName"):
                localized = screen.localizedName()
                name = str(localized) if localized else None
        displays.append(
            DisplayInfo(
                display_id=display_id,
                frame=frame,
                work_area=work_area,
                scale=scale,
                main=display_id == main_id,
                name=name,
            )
        )
    displays.sort(key=lambda d: (not d.main, d.frame.x, d.frame.y))
    return displays


class DisplayCache:
    """:func:`read_displays` with a short time-to-live."""

    def __init__(self, ttl_s: float = CACHE_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._displays: list[DisplayInfo] = []
        self._read_at = 0.0

    def get(self, *, fresh: bool = False) -> list[DisplayInfo]:
        with self._lock:
            if fresh or not self._displays or time.monotonic() - self._read_at > self._ttl:
                self._displays = read_displays()
                self._read_at = time.monotonic()
            return list(self._displays)

    def by_id(self, display_id: int) -> DisplayInfo:
        for display in self.get():
            if display.display_id == display_id:
                return display
        raise AXPilotError(
            ErrorCode.DISPLAY_NOT_FOUND,
            f"No display has id {display_id}.",
            hint="Use `list_displays` to see display ids.",
        )

    def main(self) -> DisplayInfo:
        displays = self.get()
        for display in displays:
            if display.main:
                return display
        if displays:
            return displays[0]
        raise AXPilotError(ErrorCode.DISPLAY_NOT_FOUND, "No display is connected.")
