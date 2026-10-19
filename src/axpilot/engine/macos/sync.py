"""Wait for Objective-C completion handlers with a deadline.

Several APIs (``NSWorkspace`` launching, ScreenCaptureKit) report results through a block
called later on a private dispatch queue. :func:`call_with_completion` passes a Python
callable as that block and blocks the calling worker thread on a :class:`threading.Event`
until it fires or the deadline passes (QUALITY A5). It never touches the main thread, so it
cannot deadlock against the host's run loop.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from axpilot.errors import AXPilotError, ErrorCode


def call_with_completion(
    start: Callable[[Callable[[Any, Any], None]], Any],
    timeout_s: float,
    what: str,
    *,
    hint: str | None = None,
) -> tuple[Any, Any]:
    """Run ``start(handler)`` and wait for ``handler(result, error)``.

    The handler takes exactly two arguments (PyObjC checks a block's arity against the
    method's metadata). Returns ``(result, error)``. Raises ``TIMEOUT`` naming ``what`` if
    the handler is not called within ``timeout_s`` seconds.
    """
    done = threading.Event()
    box: list[tuple[Any, Any]] = []

    def handler(result: Any, error: Any) -> None:
        box.append((result, error))
        done.set()

    start(handler)
    if not done.wait(timeout_s):
        raise AXPilotError(
            ErrorCode.TIMEOUT,
            f"{what} did not finish within {timeout_s:g} s.",
            hint=hint or "macOS is busy or waiting for a permission prompt. Try again in a moment.",
        )
    return box[0]


def ns_error_text(error: Any) -> str:
    """``localizedDescription`` of an ``NSError`` (or ``str(error)``), for messages."""
    try:
        return str(error.localizedDescription())
    except AttributeError:
        return str(error)


def ns_error_code(error: Any) -> int | None:
    """``NSError.code()`` as an int, if available."""
    try:
        return int(error.code())
    except (AttributeError, TypeError, ValueError):
        return None
