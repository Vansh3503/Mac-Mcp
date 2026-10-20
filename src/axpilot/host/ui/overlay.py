"""The on-screen indicator: a click-through frame around every screen with a status pill.

One borderless, transparent window per screen, above normal windows and on every Space, that
ignores the mouse and is left out of screen sharing (``NSWindowSharingNone``). It draws a
coloured border and a pill at the top centre with the indicator's text (blue while AXPilot acts,
orange while it is paused). :class:`axpilot.host.killswitch.Indicator` decides when it shows.

Threading: main thread only. **Not run on a real Mac yet**: drawing, Spaces and full-screen
apps, multiple displays, and whether ScreenCaptureKit leaves the windows out of AXPilot's own
screenshots on macOS 15+ (``sharingType`` no longer guarantees it there; the capture filter
should exclude AXPilot's windows too) are open checks in the handoff. Click ripples are planned.
"""

from __future__ import annotations

from typing import Any

import objc
from AppKit import (
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakePoint, NSMakeRect

__all__ = ["Overlay"]

BORDER_WIDTH = 6.0
PILL_HEIGHT = 30.0
PILL_TOP_MARGIN = 36.0  # below the menu bar


class AXPilotIndicatorView(NSView):
    """Draws the border and the pill; ``message`` and ``paused`` are set by :class:`Overlay`."""

    def initWithFrame_(self, frame: Any) -> Any:
        self = objc.super(AXPilotIndicatorView, self).initWithFrame_(frame)  # noqa: PLW0642 - the PyObjC init idiom
        if self is None:
            return None
        self.message = ""
        self.paused = False
        return self

    def isOpaque(self) -> bool:
        return False

    def drawRect_(self, _dirty: Any) -> None:
        bounds = self.bounds()
        color = (
            NSColor.systemOrangeColor() if self.paused else NSColor.systemBlueColor()
        ).colorWithAlphaComponent_(0.85)
        color.setStroke()
        inset = BORDER_WIDTH / 2
        frame = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(inset, inset, bounds.size.width - BORDER_WIDTH, bounds.size.height - BORDER_WIDTH),
            12,
            12,
        )
        frame.setLineWidth_(BORDER_WIDTH)
        frame.stroke()
        if not self.message:
            return
        attributes = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(13),
            NSForegroundColorAttributeName: NSColor.whiteColor(),
        }
        text = NSAttributedString.alloc().initWithString_attributes_(self.message, attributes)
        size = text.size()
        width = size.width + 32
        x = (bounds.size.width - width) / 2
        y = bounds.size.height - PILL_TOP_MARGIN - PILL_HEIGHT
        color.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y, width, PILL_HEIGHT), PILL_HEIGHT / 2, PILL_HEIGHT / 2
        ).fill()
        text.drawAtPoint_(NSMakePoint(x + 16, y + (PILL_HEIGHT - size.height) / 2))


class Overlay:
    """The :class:`~axpilot.host.killswitch.IndicatorView` on macOS (main thread only)."""

    def __init__(self) -> None:
        self._windows: list[Any] = []

    def _ensure_windows(self) -> None:
        if self._windows:
            return
        behavior = (
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        for screen in NSScreen.screens():
            frame = screen.frame()
            window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
            )
            window.setOpaque_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            window.setHasShadow_(False)
            window.setIgnoresMouseEvents_(True)
            window.setLevel_(NSStatusWindowLevel)
            window.setCollectionBehavior_(behavior)
            window.setSharingType_(NSWindowSharingNone)
            window.setReleasedWhenClosed_(False)
            view = AXPilotIndicatorView.alloc().initWithFrame_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height)
            )
            window.setContentView_(view)
            self._windows.append(window)

    def show(self, message: str, *, paused: bool) -> None:
        self._ensure_windows()
        for window in self._windows:
            view = window.contentView()
            view.message = message
            view.paused = paused
            view.setNeedsDisplay_(True)
            window.orderFrontRegardless()

    def hide(self) -> None:
        for window in self._windows:
            window.orderOut_(None)

    def close(self) -> None:
        for window in self._windows:
            window.close()
        self._windows = []
