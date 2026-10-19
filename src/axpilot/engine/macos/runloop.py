"""One pass of the main thread's Cocoa run loop, for programs that run asyncio instead.

Some macOS APIs only make progress while the **main** run loop runs: work scheduled with
``AppHelper.callAfter`` (the keyboard-layout lookup in :mod:`axpilot.engine.macos.carbon`) and
the ``NSWorkspace``/``NSRunningApplication`` state that AppKit refreshes from notifications.
AXPilot's CLI, host and SDK run an asyncio loop (or block) on the main thread instead of
``NSApplication.run``, so :mod:`axpilot.runloop` calls :func:`pump` every few dozen
milliseconds.

Permissions: none. Threading: call it on the main thread only.
"""

from __future__ import annotations

from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode


def pump(seconds: float = 0.0) -> None:
    """Handle the current run loop's ready sources (timers, ``performSelector…``, the main
    dispatch queue), waiting at most ``seconds`` for one. Returns at once if there is nothing
    to do; ``seconds=0`` never blocks."""
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, max(0.0, seconds), False)
