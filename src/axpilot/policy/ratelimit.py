"""A sliding-window rate limiter for input actions (runaway-agent protection)."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["RateLimiter"]


class RateLimiter:
    """Allow at most ``max_per_minute`` actions in any sliding window of ``window_s`` seconds.

    :meth:`check` records an action, or raises ``RATE_LIMITED`` without recording it. An
    action at time ``t`` stops counting at ``t + window_s``. ``clock`` returns seconds and
    is injectable for tests. Thread-safe.
    """

    def __init__(
        self,
        max_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        *,
        window_s: float = 60.0,
    ) -> None:
        if max_per_minute < 1:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"max_per_minute must be at least 1, got {max_per_minute}.",
            )
        if window_s <= 0:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"window_s must be positive, got {window_s}.")
        self.max_per_minute = max_per_minute
        self.window_s = window_s
        self._clock = clock
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()

    def check(self) -> None:
        """Record one action, or raise ``RATE_LIMITED`` if the window is full."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            if len(self._times) >= self.max_per_minute:
                retry_after = max(self._times[0] + self.window_s - now, 0.0)
                raise AXPilotError(
                    ErrorCode.RATE_LIMITED,
                    f"Rate limit reached: at most {self.max_per_minute} input actions per "
                    f"{self.window_s:g} s. Retry in {retry_after:.1f} s.",
                    hint="Slow down: check the result of each action before the next one. The user "
                    "can raise [safety] max_actions_per_minute.",
                    details={"limit": self.max_per_minute, "retry_after_s": round(retry_after, 3)},
                )
            self._times.append(now)

    def remaining(self) -> int:
        """Actions still allowed in the current window."""
        with self._lock:
            self._expire(self._clock())
            return self.max_per_minute - len(self._times)

    def reset(self) -> None:
        """Forget all recorded actions."""
        with self._lock:
            self._times.clear()
