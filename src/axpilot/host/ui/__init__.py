"""The macOS UI of the host and of the direct-mode server: the kill switch and the indicator.

:func:`start` brings up ``NSApplication`` as an accessory app (:mod:`.app`), registers the
kill-switch hotkey (:mod:`.hotkey`, ``[safety] kill_switch``) and shows the overlay while
AXPilot acts (:mod:`.overlay`, ``[safety] indicator``). What they do is decided by
:mod:`axpilot.host.killswitch`, which is tested everywhere; the modules here are the thin AppKit
and Carbon side, imported only on macOS.

The UI is a side channel (QUALITY.md A7): if AppKit cannot start or a part fails, a warning is
logged and the server runs without it. Only this package and :mod:`axpilot.engine.macos` import
PyObjC. Checked on macOS 26 in direct mode and in the host (2026-10-24, #9, #59); other macOS versions
are open checks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from axpilot.host.killswitch import Indicator, KillSwitch
from axpilot.input.hotkeys import parse_hotkey
from axpilot.policy.control import ControlState

__all__ = ["UI", "start"]

_log = logging.getLogger(__name__)


@dataclass
class UI:
    """A running UI: :attr:`pump` replaces the plain run-loop pump; call :meth:`close` at exit."""

    pump: Callable[[float], None]
    closers: list[Callable[[], None]] = field(default_factory=list)
    kill_switch: KillSwitch | None = None
    indicator: Indicator | None = None

    def close(self) -> None:
        for closer in reversed(self.closers):
            try:
                closer()
            except Exception:  # shutting down: log and go on (A7)
                _log.warning("Closing part of the UI failed", exc_info=True)
        self.closers.clear()


def start(control: ControlState, *, kill_switch: str, indicator: bool) -> UI | None:
    """Start the UI on the main thread; ``None`` when both parts are off or AppKit fails."""
    if not kill_switch and not indicator:
        return None
    try:
        from axpilot.host.ui.app import AppKitPump

        app = AppKitPump()
    except Exception:
        _log.warning(
            "The kill switch and the indicator are unavailable: AppKit did not start.", exc_info=True
        )
        return None
    ui = UI(pump=app.pump)
    hotkey = None
    if kill_switch:
        try:
            from axpilot.host.ui.hotkey import CarbonHotkey

            hotkey = parse_hotkey(kill_switch)
            ui.kill_switch = KillSwitch(control, hotkey)
            carbon = CarbonHotkey(hotkey, ui.kill_switch.pressed)
            ui.closers.append(carbon.close)
        except Exception:
            _log.warning("The kill switch (%s) could not be registered.", kill_switch, exc_info=True)
            ui.kill_switch = None
    if indicator:
        try:
            from axpilot.host.ui.overlay import Overlay

            overlay = Overlay()
            ui.indicator = Indicator(overlay, hotkey.label if ui.kill_switch and hotkey else None)
            control.add_listener(ui.indicator.on_event)
            app.ticks.append(ui.indicator.tick)
            ui.closers.append(overlay.close)
        except Exception:
            _log.warning("The on-screen indicator could not start.", exc_info=True)
            ui.indicator = None
    if ui.kill_switch is None and ui.indicator is None:
        ui.close()
        return None
    return ui
