"""Synthetic mouse input and HID idle time through Quartz event services.

Permission: posting events needs **Accessibility**; without it macOS silently drops them.

Events come from a private ``CGEventSource`` (``kCGEventSourceStatePrivate``) so the user's
physically held modifiers never leak into them, carry :data:`EVENT_TAG` in
``kCGEventSourceUserData`` (so AXPilot's own event taps can recognize them) and are posted at
``kCGHIDEventTap``. Local-event suppression is disabled on the source, so the user's own
mouse and keyboard are never frozen after a synthetic event.

All coordinates are global points with the origin at the top-left of the main display.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import Quartz as Q

from axpilot.engine.macos import helpers as h
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import MouseButton, Point

_log = logging.getLogger(__name__)

EVENT_TAG = 0x41585049
"""``"AXPI"``: stored in ``kCGEventSourceUserData`` of every event AXPilot posts."""

MOVE_SETTLE_S = 0.02
"""Pause after moving the pointer before pressing a button (lets hover state update)."""
PRESS_HOLD_S = 0.01
"""How long a button stays down during a click."""
CLICK_GAP_S = 0.03
"""Pause between the clicks of a double/triple click (well under the double-click time)."""
DRAG_HOLD_S = 0.05
"""Pause after pressing before a drag starts moving (some apps need it to begin a drag)."""
SCROLL_GAP_S = 0.01
"""Pause between scroll-wheel events."""
MAX_CLICKS = 3

_BUTTONS: dict[str, tuple[int, int, int, int]] = {
    "left": (
        Q.kCGEventLeftMouseDown,
        Q.kCGEventLeftMouseUp,
        Q.kCGEventLeftMouseDragged,
        Q.kCGMouseButtonLeft,
    ),
    "right": (
        Q.kCGEventRightMouseDown,
        Q.kCGEventRightMouseUp,
        Q.kCGEventRightMouseDragged,
        Q.kCGMouseButtonRight,
    ),
    "middle": (
        Q.kCGEventOtherMouseDown,
        Q.kCGEventOtherMouseUp,
        Q.kCGEventOtherMouseDragged,
        Q.kCGMouseButtonCenter,
    ),
}


def _hid_idle() -> float:
    return float(
        Q.CGEventSourceSecondsSinceLastEventType(Q.kCGEventSourceStateHIDSystemState, Q.kCGAnyInputEventType)
    )


class EventPoster:
    """Creates and posts tagged events; tracks the user's idle time around them."""

    def __init__(self) -> None:
        source = Q.CGEventSourceCreate(Q.kCGEventSourceStatePrivate)
        if source is not None:
            Q.CGEventSourceSetUserData(source, EVENT_TAG)
            Q.CGEventSourceSetLocalEventsSuppressionInterval(source, 0.0)
        else:  # pragma: no cover - CGEventSourceCreate only fails without a window server
            _log.warning("CGEventSourceCreate failed; posting events without a private source")
        self.source = source
        self._lock = threading.Lock()
        self.idle = h.IdleEstimator(_hid_idle, time.monotonic)

    def post(self, event: Any) -> None:
        """Tag and post ``event`` at the HID event tap."""
        if event is None:
            raise AXPilotError(ErrorCode.INTERNAL, "Quartz could not create the input event.")
        Q.CGEventSetIntegerValueField(event, Q.kCGEventSourceUserData, EVENT_TAG)
        with self._lock:
            self.idle.before_post()
            Q.CGEventPost(Q.kCGHIDEventTap, event)
            self.idle.after_post()

    def idle_seconds(self) -> float:
        """Seconds since the user's last hardware mouse/keyboard event."""
        with self._lock:
            return self.idle.idle_seconds()


def cursor_position() -> Point:
    """Current pointer location in global points."""
    location = Q.CGEventGetLocation(Q.CGEventCreate(None))
    return Point(float(location.x), float(location.y))


def _button(button: str) -> tuple[int, int, int, int]:
    try:
        return _BUTTONS[button]
    except KeyError:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unknown mouse button {button!r}.",
            hint="Use left, right or middle.",
        ) from None


class Mouse:
    """Pointer moves, clicks, drags and scrolling."""

    def __init__(self, poster: EventPoster) -> None:
        self._poster = poster

    def _event(self, event_type: int, point: Point, button: int, flags: int) -> Any:
        event = Q.CGEventCreateMouseEvent(self._poster.source, event_type, (point.x, point.y), button)
        if event is not None:
            Q.CGEventSetFlags(event, flags)
        return event

    def _post_move(self, point: Point, flags: int = 0) -> None:
        self._poster.post(self._event(Q.kCGEventMouseMoved, point, Q.kCGMouseButtonLeft, flags))

    def move(self, point: Point, duration_s: float = 0.0) -> None:
        """Move the pointer, gliding over ``duration_s`` seconds when it is positive."""
        if duration_s > 0:
            path = h.interpolate(cursor_position(), point, h.motion_steps(duration_s))
            pause = duration_s / len(path)
            for step in path:
                self._post_move(step)
                time.sleep(pause)
        else:
            self._post_move(point)

    def click(self, point: Point, button: MouseButton = "left", count: int = 1, flags: int = 0) -> None:
        """Click ``count`` times (2 = double click, 3 = triple click) at ``point``."""
        if not 1 <= count <= MAX_CLICKS:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"Click count must be between 1 and {MAX_CLICKS}, got {count}."
            )
        down_type, up_type, _, number = _button(button)
        self._post_move(point, flags)
        time.sleep(MOVE_SETTLE_S)
        for click_state in range(1, count + 1):
            for event_type in (down_type, up_type):
                event = self._event(event_type, point, number, flags)
                if event is not None:
                    Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventClickState, click_state)
                self._poster.post(event)
                if event_type == down_type:
                    time.sleep(PRESS_HOLD_S)
            if click_state < count:
                time.sleep(CLICK_GAP_S)

    def drag(
        self, start: Point, end: Point, duration_s: float, button: MouseButton = "left", flags: int = 0
    ) -> None:
        """Press at ``start``, move to ``end`` over ``duration_s`` seconds, release."""
        down_type, up_type, drag_type, number = _button(button)
        self._post_move(start, flags)
        time.sleep(MOVE_SETTLE_S)
        self._poster.post(self._event(down_type, start, number, flags))
        time.sleep(DRAG_HOLD_S)
        try:
            path = h.interpolate(start, end, h.motion_steps(duration_s))
            pause = max(duration_s, 0.0) / len(path)
            for step in path:
                self._poster.post(self._event(drag_type, step, number, flags))
                time.sleep(pause)
        finally:
            # Always release the button, even if a move failed, so it is never left stuck down.
            self._poster.post(self._event(up_type, end, number, flags))

    def scroll(self, point: Point | None, dx: float, dy: float) -> None:
        """Scroll by lines at ``point`` (or where the pointer is). Positive ``dy`` reveals
        content further down, positive ``dx`` further right."""
        vertical, horizontal = h.wheel_deltas(dx, dy)
        if point is not None:
            self._post_move(point)
            time.sleep(MOVE_SETTLE_S)
        for wheel_y, wheel_x in zip(vertical, horizontal, strict=True):
            event = Q.CGEventCreateScrollWheelEvent(
                self._poster.source, Q.kCGScrollEventUnitLine, 2, int(wheel_y), int(wheel_x)
            )
            if event is not None:
                if point is not None:
                    Q.CGEventSetLocation(event, (point.x, point.y))
                Q.CGEventSetFlags(event, 0)
            self._poster.post(event)
            time.sleep(SCROLL_GAP_S)
