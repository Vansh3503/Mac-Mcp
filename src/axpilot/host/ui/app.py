"""AppKit for a process that runs asyncio on the main thread: one pass of the event queue.

The hotkey and the overlay need ``NSApplication``: its event queue delivers the Carbon hotkey
events, and its windows draw. AXPilot's host and direct-mode server keep the main thread in an
asyncio loop, so instead of ``NSApp.run()`` :meth:`AppKitPump.pump` is called every 50 ms by
:mod:`axpilot.runloop`: it dispatches the queued events, turns the run loop (as the plain pump
in :mod:`axpilot.engine.macos.runloop` does) and runs the tick callbacks (the indicator).

The application is an accessory: no Dock icon, no menu bar. Threading: main thread only.
Checked on macOS 26 (2026-10-24): Carbon hotkey events arrive through this pump.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSEventMaskAny
from Foundation import NSDate, NSDefaultRunLoopMode

from axpilot.engine.macos.runloop import pump as runloop_pump

__all__ = ["AppKitPump"]

_log = logging.getLogger(__name__)
MAX_EVENTS_PER_PASS = 64


class AppKitPump:
    """Starts ``NSApplication`` as an accessory app and pumps it (see the module docstring)."""

    def __init__(self) -> None:
        self.app = NSApplication.sharedApplication()
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.app.finishLaunching()
        self.ticks: list[Callable[[], None]] = []

    def pump(self, seconds: float = 0.0) -> None:
        """Dispatch up to 64 queued events, turn the run loop, then run the tick callbacks."""
        past = NSDate.distantPast()
        for _ in range(MAX_EVENTS_PER_PASS):
            event = self.app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                NSEventMaskAny, past, NSDefaultRunLoopMode, True
            )
            if event is None:
                break
            self.app.sendEvent_(event)
        self.app.updateWindows()
        runloop_pump(seconds)
        for tick in self.ticks:
            try:
                tick()
            except Exception:  # a broken tick must not stop the pump (A7)
                _log.warning("A UI tick failed", exc_info=True)
