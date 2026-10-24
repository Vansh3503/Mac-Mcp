"""The kill switch and the on-screen indicator: what they do, without the macOS UI.

* :class:`KillSwitch` turns a press into :meth:`ControlState.toggle_pause
  <axpilot.policy.control.ControlState.toggle_pause>`: the first press pauses AXPilot and stops
  the input action in progress; the next one resumes. Presses less than 0.5 s apart count once
  (key repeat, a bounce). While AXPilot is paused no tool can press keys or run scripts, so an
  agent cannot resume itself with the hotkey.
* :class:`Indicator` decides what the overlay shows: "AXPilot is controlling this Mac" while an
  input action runs and for :data:`LINGER_S` after it, "AXPilot is paused" while paused, nothing
  otherwise. Control-state events may arrive on any thread; :meth:`Indicator.tick`, called on
  the main thread by the UI's run-loop pump, applies the change to the :class:`IndicatorView`.

The AppKit and Carbon parts live in :mod:`axpilot.host.ui` (macOS only; checked on macOS 26, see
there); everything here runs and is tested anywhere.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Literal, Protocol

from axpilot.input.hotkeys import Hotkey
from axpilot.policy.control import ControlState

__all__ = ["DEBOUNCE_S", "LINGER_S", "Indicator", "IndicatorView", "KillSwitch"]

_log = logging.getLogger(__name__)

DEBOUNCE_S = 0.5
"""Presses of the kill switch closer together than this count once."""
LINGER_S = 2.0
"""How long the indicator stays after the last input action."""
CONTROLLING = "AXPilot is controlling this Mac"
PAUSED = "AXPilot is paused"
_STOP_HINT = " · {hotkey} stops it"
_RESUME_HINT = " · {hotkey} resumes"


class KillSwitch:
    """Toggles AXPilot's pause state on each (debounced) press of the hotkey."""

    def __init__(
        self,
        control: ControlState,
        hotkey: Hotkey,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self.control = control
        self.hotkey = hotkey
        self._clock = clock
        self._on_change = on_change
        self._last: float | None = None
        self._lock = threading.Lock()

    def pressed(self) -> bool:
        """Handle one press; returns the ``paused`` state afterwards. Safe from any thread and
        never raises (it runs inside the UI's event handler)."""
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < DEBOUNCE_S:
                return self.control.paused
            self._last = now
        try:
            paused = self.control.toggle_pause(f"kill switch {self.hotkey.label}")
        except Exception:  # an event handler must not raise into AppKit (A7: logged)
            _log.exception("The kill switch could not change AXPilot's pause state")
            return self.control.paused
        _log.warning(
            "AXPilot %s by the kill switch (%s).", "paused" if paused else "resumed", self.hotkey.label
        )
        if self._on_change is not None:
            try:
                self._on_change(paused)
            except Exception:
                _log.warning("Reporting the kill switch change failed", exc_info=True)
        return paused


class IndicatorView(Protocol):
    """What draws the indicator (the AppKit overlay on macOS). Called on the main thread."""

    def show(self, message: str, *, paused: bool) -> None: ...

    def hide(self) -> None: ...


Shown = Literal["hidden", "controlling", "paused"]


class Indicator:
    """Follows a :class:`ControlState` and keeps an :class:`IndicatorView` in step (see the
    module docstring). Attach it with ``control.add_listener(indicator.on_event)``."""

    def __init__(
        self,
        view: IndicatorView,
        hotkey_label: str | None,
        *,
        linger_s: float = LINGER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.view = view
        self.hotkey_label = hotkey_label
        self.linger_s = linger_s
        self._clock = clock
        self._lock = threading.Lock()
        self._running = 0
        self._paused = False
        self._last_end: float | None = None
        self._shown: Shown = "hidden"

    def on_event(self, event: str, detail: str | None) -> None:
        """A control-state event (any thread)."""
        with self._lock:
            if event == "turn_start":
                self._running += 1
            elif event == "turn_end":
                self._running = max(0, self._running - 1)
                self._last_end = self._clock()
            elif event == "paused":
                self._paused = True
            elif event == "resumed":
                self._paused = False
                self._last_end = None

    def wanted(self) -> Shown:
        """What should be on screen now."""
        with self._lock:
            if self._paused:
                return "paused"
            if self._running:
                return "controlling"
            if self._last_end is not None and self._clock() - self._last_end < self.linger_s:
                return "controlling"
            return "hidden"

    def message(self, shown: Shown) -> str:
        """The pill's text for ``shown`` (with the hotkey when there is one)."""
        if shown == "paused":
            text, hint = PAUSED, _RESUME_HINT
        else:
            text, hint = CONTROLLING, _STOP_HINT
        return text + hint.format(hotkey=self.hotkey_label) if self.hotkey_label else text

    def tick(self) -> None:
        """Bring the view in line with :meth:`wanted` (main thread). Never raises."""
        wanted = self.wanted()
        if wanted == self._shown:
            return
        try:
            if wanted == "hidden":
                self.view.hide()
            else:
                self.view.show(self.message(wanted), paused=wanted == "paused")
        except Exception:  # the indicator is a side channel (A7): log once per change, go on
            _log.warning("The on-screen indicator could not be updated", exc_info=True)
        self._shown = wanted
