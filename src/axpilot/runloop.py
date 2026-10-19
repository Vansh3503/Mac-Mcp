"""Keep the main thread's Cocoa run loop turning while AXPilot runs asyncio or blocks on it.

On macOS, some work only happens on the main thread's run loop: the keyboard-layout lookup
(Text Input Sources must run there) and the refresh of ``NSWorkspace``'s app list. The CLI,
the host and the SDK keep the main thread busy with an asyncio loop or a blocking wait, so
nothing would ever turn it. This module turns it from the outside:

* :func:`run_with_pump` runs a coroutine function while a plain loop callback calls the pump
  every :data:`PUMP_INTERVAL_S` (no task group, so the coroutine's exceptions and Ctrl-C
  propagate unchanged).
* :func:`start_ticking` does the same on an already running loop (``AsyncDesktop``).
* :func:`wait_pumping` waits for a ``concurrent.futures.Future`` while pumping (the sync
  ``Desktop``, whose calls block the main thread).

The pump itself (:func:`axpilot.engine.macos.runloop.pump`) is the only PyObjC part;
:func:`main_thread_pump` returns it on macOS when called on the main thread and ``None``
everywhere else, so everything here also runs (with a fake pump) in Linux tests.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

__all__ = ["PUMP_INTERVAL_S", "Pump", "main_thread_pump", "run_with_pump", "start_ticking", "wait_pumping"]

_log = logging.getLogger(__name__)

T = TypeVar("T")

Pump = Callable[[float], None]
"""Runs pending main-run-loop work, waiting at most the given number of seconds."""

PUMP_INTERVAL_S = 0.05
"""How often the run loop is turned: well below the 0.5 s the keyboard-layout lookup waits."""


def main_thread_pump() -> Pump | None:
    """The Cocoa run-loop pump when called on the main thread on macOS, else ``None``."""
    if sys.platform != "darwin" or threading.current_thread() is not threading.main_thread():
        return None
    try:
        from axpilot.engine.macos.runloop import pump  # noqa: PLC0415 - PyObjC is macOS-only
    except ImportError as exc:
        _log.warning("Cannot turn the Cocoa run loop (%s); the keyboard layout may fall back to US.", exc)
        return None
    return pump


class _Ticker:
    """Calls the pump on a running asyncio loop until stopped or until the pump fails."""

    def __init__(self, loop: asyncio.AbstractEventLoop, pump: Pump, interval: float) -> None:
        self._loop = loop
        self._pump = pump
        self._interval = interval
        self._handle: asyncio.TimerHandle | asyncio.Handle | None = loop.call_soon(self._tick)

    def _tick(self) -> None:
        self._handle = None
        try:
            self._pump(0.0)
        except Exception:
            _log.warning("Turning the Cocoa run loop failed; it is no longer turned.", exc_info=True)
            return
        self._handle = self._loop.call_later(self._interval, self._tick)

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


def start_ticking(pump: Pump | None, interval: float = PUMP_INTERVAL_S) -> Callable[[], None]:
    """Call ``pump(0)`` every ``interval`` seconds on the running asyncio loop.

    Returns a function that stops it. Does nothing (and returns a no-op) without a pump or
    outside an asyncio loop (e.g. under trio)."""
    if pump is None:
        return _noop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.debug("Not in an asyncio loop: the Cocoa run loop is not turned.")
        return _noop
    return _Ticker(loop, pump, interval).stop


async def run_with_pump(
    main: Callable[[], Awaitable[T]], pump: Pump | None = None, interval: float = PUMP_INTERVAL_S
) -> T:
    """``await main()`` while the run loop is turned every ``interval`` seconds."""
    stop = start_ticking(pump, interval)
    try:
        return await main()
    finally:
        stop()


def wait_pumping(
    future: concurrent.futures.Future[T], pump: Pump | None = None, interval: float = PUMP_INTERVAL_S
) -> T:
    """``future.result()``, turning the run loop at least every ``interval`` seconds meanwhile.

    If the wait is interrupted (Ctrl-C, or the pump raises), the future is cancelled before the
    exception propagates, so the work it stands for does not continue unobserved."""
    if pump is None:
        return future.result()
    try:
        while not future.done():
            pump(0.0)
            concurrent.futures.wait([future], timeout=interval)
    except BaseException:
        future.cancel()
        raise
    return future.result()


def _noop() -> None:
    return None
