from __future__ import annotations

import threading

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def fill(limiter: RateLimiter, count: int) -> None:
    for _ in range(count):
        limiter.check()


def test_allows_up_to_the_limit_then_raises() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, clock)
    fill(limiter, 3)
    with pytest.raises(AXPilotError) as info:
        limiter.check()
    assert info.value.code is ErrorCode.RATE_LIMITED
    assert "3 input actions" in info.value.message
    assert info.value.details["limit"] == 3


def test_retry_after_reports_when_the_oldest_action_expires() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, clock)
    limiter.check()
    clock.advance(10)
    limiter.check()
    clock.advance(5)
    with pytest.raises(AXPilotError) as info:
        limiter.check()
    assert info.value.details["retry_after_s"] == pytest.approx(45.0)
    assert "Retry in 45.0 s" in info.value.message


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, clock)
    limiter.check()  # t=0
    clock.advance(30)
    limiter.check()  # t=30
    clock.advance(29.9)  # t=59.9: both still count
    with pytest.raises(AXPilotError):
        limiter.check()
    clock.advance(0.1)  # t=60: the first one has expired
    limiter.check()
    with pytest.raises(AXPilotError):
        limiter.check()
    clock.advance(30)  # t=90: the t=30 action expired
    limiter.check()


def test_rejected_calls_do_not_count() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, clock)
    limiter.check()
    for _ in range(5):
        clock.advance(10)
        with pytest.raises(AXPilotError):
            limiter.check()
    clock.advance(10)  # 60 s after the only recorded action
    limiter.check()


def test_full_window_recovers_completely() -> None:
    clock = FakeClock()
    limiter = RateLimiter(120, clock)
    fill(limiter, 120)
    assert limiter.remaining() == 0
    clock.advance(60)
    assert limiter.remaining() == 120
    fill(limiter, 120)


def test_remaining_and_reset() -> None:
    clock = FakeClock()
    limiter = RateLimiter(5, clock)
    assert limiter.remaining() == 5
    fill(limiter, 2)
    assert limiter.remaining() == 3
    limiter.reset()
    assert limiter.remaining() == 5


def test_custom_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, clock, window_s=1.0)
    limiter.check()
    clock.advance(0.5)
    with pytest.raises(AXPilotError):
        limiter.check()
    clock.advance(0.5)
    limiter.check()


@pytest.mark.parametrize(("limit", "window"), [(0, 60.0), (-1, 60.0), (5, 0.0), (5, -1.0)])
def test_invalid_settings(limit: int, window: float) -> None:
    with pytest.raises(AXPilotError) as info:
        RateLimiter(limit, window_s=window)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_default_clock_is_monotonic() -> None:
    limiter = RateLimiter(2)
    fill(limiter, 2)
    with pytest.raises(AXPilotError):
        limiter.check()


def test_thread_safety_never_exceeds_the_limit() -> None:
    limiter = RateLimiter(100, FakeClock())
    allowed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(50):
            try:
                limiter.check()
            except AXPilotError:
                continue
            with lock:
                allowed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 100
