"""Turning the main thread's Cocoa run loop (``axpilot.runloop``), with a fake pump on any OS."""

from __future__ import annotations

import concurrent.futures
import logging
import sys
import threading
import types
from collections.abc import Callable

import anyio
import pytest

from axpilot import runloop
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.runloop import main_thread_pump, run_with_pump, start_ticking, wait_pumping

INTERVAL = 0.01


class FakePump:
    """Counts calls, then runs ``notify`` (if set); optionally raises on the n-th call."""

    def __init__(self, *, fail_on: int | None = None, error: BaseException | None = None) -> None:
        self.calls: list[float] = []
        self.fail_on = fail_on
        self.error = error or RuntimeError("the run loop is broken")
        self.threads: set[str] = set()
        self.notify: Callable[[], None] | None = None

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.threads.add(threading.current_thread().name)
        if self.notify is not None:
            self.notify()
        if self.fail_on is not None and len(self.calls) >= self.fail_on:
            raise self.error


async def until_pumped(pump: FakePump, times: int) -> None:
    """Wait for the pump's ``times``-th call instead of a fixed sleep (slow CI runners)."""
    reached = anyio.Event()
    pump.notify = lambda: reached.set() if len(pump.calls) >= times else None
    if len(pump.calls) < times:
        with anyio.fail_after(5):
            await reached.wait()


# --------------------------------------------------------------------------- main_thread_pump
@pytest.fixture
def fake_macos(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Pretend to be macOS with a fake ``axpilot.engine.macos.runloop`` module."""
    module = types.ModuleType("axpilot.engine.macos.runloop")
    module.pump = FakePump()  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "axpilot.engine.macos.runloop", module)
    return module


def test_no_pump_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert main_thread_pump() is None


def test_the_macos_pump_on_the_main_thread(fake_macos: types.ModuleType) -> None:
    assert main_thread_pump() is fake_macos.pump


def test_no_pump_off_the_main_thread(fake_macos: types.ModuleType) -> None:
    found: list[object] = []
    worker = threading.Thread(target=lambda: found.append(main_thread_pump()))
    worker.start()
    worker.join()
    assert found == [None]


def test_missing_pyobjc_is_a_warning_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "axpilot.engine.macos.runloop", None)  # import raises ImportError
    with caplog.at_level(logging.WARNING, logger="axpilot.runloop"):
        assert main_thread_pump() is None
    assert "Cannot turn the Cocoa run loop" in caplog.text


# --------------------------------------------------------------------------- run_with_pump
@pytest.mark.anyio
async def test_the_pump_turns_while_main_runs_and_stops_after() -> None:
    pump = FakePump()

    async def main() -> str:
        await until_pumped(pump, 3)
        return "done"

    assert await run_with_pump(main, pump, INTERVAL) == "done"
    turned = len(pump.calls)
    assert turned >= 3
    assert all(seconds == 0.0 for seconds in pump.calls), "the asyncio loop must never block in the pump"
    await anyio.sleep(0.05)
    assert len(pump.calls) == turned, "the pump kept turning after main returned"


@pytest.mark.anyio
async def test_errors_from_main_pass_through_unwrapped() -> None:
    pump = FakePump()

    async def main() -> None:
        await anyio.sleep(0.03)
        raise AXPilotError(ErrorCode.APP_NOT_FOUND, "No app named 'Nope'.")

    with pytest.raises(AXPilotError) as caught:  # not an ExceptionGroup: exit codes depend on it
        await run_with_pump(main, pump, INTERVAL)
    assert caught.value.code is ErrorCode.APP_NOT_FOUND
    turned = len(pump.calls)
    await anyio.sleep(0.05)
    assert len(pump.calls) == turned


@pytest.mark.anyio
async def test_cancellation_reaches_main_and_stops_the_pump() -> None:
    pump = FakePump()
    reached: list[str] = []

    async def main() -> None:
        try:
            await anyio.sleep(10)
        finally:
            reached.append("cancelled")

    with anyio.move_on_after(0.05):
        await run_with_pump(main, pump, INTERVAL)
    assert reached == ["cancelled"]
    turned = len(pump.calls)
    await anyio.sleep(0.05)
    assert len(pump.calls) == turned


@pytest.mark.anyio
async def test_a_failing_pump_is_logged_once_and_main_still_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pump = FakePump(fail_on=2)

    async def main() -> int:
        await until_pumped(pump, 2)
        await anyio.sleep(5 * INTERVAL)  # time for calls that must not happen
        return 7

    with caplog.at_level(logging.WARNING, logger="axpilot.runloop"):
        assert await run_with_pump(main, pump, INTERVAL) == 7
    assert len(pump.calls) == 2, "a broken pump must not be called again"
    assert caplog.text.count("Turning the Cocoa run loop failed") == 1


@pytest.mark.anyio
async def test_without_a_pump_main_just_runs() -> None:
    async def main() -> str:
        return "plain"

    assert await run_with_pump(main, None) == "plain"


def test_start_ticking_outside_asyncio_is_a_no_op() -> None:
    pump = FakePump()
    stop = start_ticking(pump, INTERVAL)
    stop()
    assert pump.calls == []


@pytest.mark.anyio
async def test_stopping_twice_is_harmless() -> None:
    pump = FakePump()
    stop = start_ticking(pump, INTERVAL)
    await anyio.sleep(0.03)
    stop()
    stop()
    turned = len(pump.calls)
    await anyio.sleep(0.03)
    assert turned >= 1
    assert len(pump.calls) == turned


# --------------------------------------------------------------------------- wait_pumping
def test_waiting_pumps_until_the_result_arrives() -> None:
    third_call = threading.Event()
    pump = FakePump()
    pump.notify = lambda: third_call.set() if len(pump.calls) >= 3 else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: 42 if third_call.wait(5) else None)  # done only after 3 pumps
        assert wait_pumping(future, pump, INTERVAL) == 42
    assert len(pump.calls) >= 3
    assert pump.threads == {threading.current_thread().name}


def test_waiting_re_raises_the_calls_error() -> None:
    def fail() -> None:
        raise AXPilotError(ErrorCode.STALE_REF, "e12 is gone.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fail)
        with pytest.raises(AXPilotError) as caught:
            wait_pumping(future, FakePump(), INTERVAL)
    assert caught.value.code is ErrorCode.STALE_REF


def test_an_interrupted_wait_cancels_the_call() -> None:
    future: concurrent.futures.Future[int] = concurrent.futures.Future()
    pump = FakePump(fail_on=2, error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        wait_pumping(future, pump, INTERVAL)
    assert future.cancelled()


def test_waiting_without_a_pump_is_a_plain_result() -> None:
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    future.set_result("ok")
    assert wait_pumping(future, None) == "ok"


def test_the_interval_is_well_below_the_layout_wait() -> None:
    # The keyboard-layout lookup waits 0.5 s for the main thread (engine/macos/carbon.py).
    assert runloop.PUMP_INTERVAL_S <= 0.1
