from __future__ import annotations

import threading
from typing import cast

import anyio
import pytest

from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.control import ControlState

pytestmark = pytest.mark.anyio


class StubEngine:
    """Only what ControlState needs: ``hid_idle_seconds()``, recording the calling thread."""

    def __init__(self, idle: float = 999.0) -> None:
        self.idle = idle
        self.threads: list[int] = []
        self.error: BaseException | None = None
        self.gate: threading.Event | None = None
        self.called = threading.Event()

    def hid_idle_seconds(self) -> float:
        self.threads.append(threading.get_ident())
        self.called.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.idle


def make(stub: StubEngine | None = None, **kwargs: object) -> tuple[ControlState, StubEngine]:
    stub = stub or StubEngine()
    return ControlState(cast("Engine", stub), **kwargs), stub  # type: ignore[arg-type]


async def expect(code: ErrorCode, control: ControlState, **turn: object) -> AXPilotError:
    with pytest.raises(AXPilotError) as info:
        async with control.input_turn(**turn):  # type: ignore[arg-type]
            pass
    assert info.value.code is code
    return info.value


# --------------------------------------------------------------------------- basic turns


async def test_turn_runs_and_checks_idle_in_a_worker_thread() -> None:
    control, stub = make()
    ran = False
    async with control.input_turn():
        ran = True
        assert control.busy
    assert ran
    assert not control.busy
    assert len(stub.threads) == 1
    assert stub.threads[0] != threading.get_ident()


async def test_initial_state() -> None:
    control, _ = make()
    assert control.paused is False
    assert control.pause_reason is None
    assert control.to_dict() == {
        "paused": False,
        "pause_reason": None,
        "busy": False,
        "current_action": None,
        "yield_to_user": True,
        "idle_threshold_s": 1.0,
    }


async def test_current_action_label() -> None:
    control, _ = make()
    async with control.input_turn(label="click"):
        assert control.current_action == "click"
        assert control.to_dict()["busy"] is True
    assert control.current_action is None


async def test_exception_in_body_propagates_and_releases() -> None:
    control, _ = make()
    with pytest.raises(RuntimeError, match="boom"):
        async with control.input_turn():
            raise RuntimeError("boom")
    assert not control.busy
    async with control.input_turn():
        pass


async def test_axpilot_error_in_body_propagates_unchanged() -> None:
    control, _ = make()
    error = AXPilotError(ErrorCode.STALE_REF, "gone")
    with pytest.raises(AXPilotError) as info:
        async with control.input_turn():
            raise error
    assert info.value is error


# ------------------------------------------------------------------------ pause / resume


async def test_paused_turn_fails_fast_without_touching_the_engine() -> None:
    control, stub = make()
    control.pause()
    assert control.paused
    assert control.pause_reason == "kill switch"
    error = await expect(ErrorCode.PAUSED_BY_USER, control)
    assert "kill switch" in error.message
    assert stub.threads == []
    assert not control.busy


async def test_pause_reason_is_reported() -> None:
    control, _ = make()
    control.pause("menu bar Stop")
    error = await expect(ErrorCode.PAUSED_BY_USER, control)
    assert "menu bar Stop" in error.message
    assert control.to_dict()["pause_reason"] == "menu bar Stop"


async def test_resume_allows_input_again() -> None:
    control, _ = make()
    control.pause()
    control.resume()
    assert control.paused is False
    assert control.pause_reason is None
    async with control.input_turn():
        pass


async def test_ensure_running() -> None:
    control, _ = make()
    control.ensure_running()
    control.pause()
    with pytest.raises(AXPilotError) as info:
        control.ensure_running()
    assert info.value.code is ErrorCode.PAUSED_BY_USER


async def test_pause_stops_the_running_action() -> None:
    control, _ = make()
    started = anyio.Event()
    reached_end = False
    outcome: list[ErrorCode] = []

    async def act() -> None:
        nonlocal reached_end
        try:
            async with control.input_turn():
                started.set()
                await anyio.sleep(10)
                reached_end = True
        except AXPilotError as error:
            outcome.append(error.code)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(act)
            await started.wait()
            control.pause("hotkey")
    assert outcome == [ErrorCode.PAUSED_BY_USER]
    assert not reached_end
    assert not control.busy


async def test_pause_from_another_thread_stops_the_running_action() -> None:
    control, _ = make()
    started = anyio.Event()
    outcome: list[AXPilotError] = []

    async def act() -> None:
        try:
            async with control.input_turn():
                started.set()
                await anyio.sleep(10)
        except AXPilotError as error:
            outcome.append(error)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(act)
            await started.wait()
            thread = threading.Thread(target=control.pause, args=("global hotkey",))
            thread.start()
            await anyio.to_thread.run_sync(thread.join)
    assert len(outcome) == 1
    assert outcome[0].code is ErrorCode.PAUSED_BY_USER
    assert "global hotkey" in outcome[0].message


async def test_pause_during_the_idle_check_skips_the_body() -> None:
    stub = StubEngine()
    stub.gate = threading.Event()
    control, _ = make(stub)
    body_ran = False
    outcome: list[ErrorCode] = []

    async def act() -> None:
        nonlocal body_ran
        try:
            async with control.input_turn():
                body_ran = True
        except AXPilotError as error:
            outcome.append(error.code)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(act)
            assert await anyio.to_thread.run_sync(stub.called.wait, 5)
            control.pause()
            stub.gate.set()
    assert outcome == [ErrorCode.PAUSED_BY_USER]
    assert not body_ran


async def test_pause_while_waiting_for_the_lock() -> None:
    control, _ = make()
    release = anyio.Event()
    holder_started = anyio.Event()
    holder_codes: list[ErrorCode] = []
    waiter_codes: list[ErrorCode] = []

    async def holder() -> None:
        try:
            async with control.input_turn(label="drag"):
                holder_started.set()
                await release.wait()
        except AXPilotError as error:
            holder_codes.append(error.code)

    async def waiter() -> None:
        try:
            async with control.input_turn(timeout_s=5):
                pass
        except AXPilotError as error:
            waiter_codes.append(error.code)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(holder)
            await holder_started.wait()
            tg.start_soon(waiter)
            await anyio.wait_all_tasks_blocked()  # the waiter is queued on the lock
            control.pause()
            release.set()
    # The pause stops the running action, and the waiter must not start after it.
    assert holder_codes == [ErrorCode.PAUSED_BY_USER]
    assert waiter_codes == [ErrorCode.PAUSED_BY_USER]
    assert not control.busy


async def test_pause_after_the_turn_finished_is_harmless() -> None:
    control, _ = make()
    async with control.input_turn():
        pass
    control.pause()
    control.resume()
    async with control.input_turn():
        pass


async def test_outer_cancellation_is_not_reported_as_pause() -> None:
    control, _ = make()
    with anyio.move_on_after(0.05) as scope:
        async with control.input_turn():
            await anyio.sleep(10)
    assert scope.cancelled_caught
    assert not control.paused
    assert not control.busy


# ----------------------------------------------------------------------- yield to user


async def test_user_activity_blocks_input() -> None:
    control, _ = make(StubEngine(idle=0.2))
    error = await expect(ErrorCode.USER_ACTIVE, control)
    assert error.details == {"idle_s": 0.2, "threshold_s": 1.0}
    assert "0.2 s ago" in error.message
    assert not control.busy


async def test_custom_idle_threshold() -> None:
    control, stub = make(StubEngine(idle=2.0), idle_threshold_s=3.0)
    await expect(ErrorCode.USER_ACTIVE, control)
    stub.idle = 3.0
    async with control.input_turn():
        pass


async def test_yield_to_user_can_be_turned_off() -> None:
    control, stub = make(StubEngine(idle=0.0), yield_to_user=False)
    async with control.input_turn():
        pass
    assert stub.threads == []


async def test_zero_threshold_skips_the_check() -> None:
    control, stub = make(StubEngine(idle=0.0), idle_threshold_s=0.0)
    async with control.input_turn():
        pass
    assert stub.threads == []


async def test_user_becomes_idle_again() -> None:
    control, stub = make(StubEngine(idle=0.1))
    await expect(ErrorCode.USER_ACTIVE, control)
    stub.idle = 5.0
    async with control.input_turn():
        pass


async def test_idle_check_errors() -> None:
    stub = StubEngine()
    stub.error = AXPilotError(ErrorCode.PERMISSION_ACCESSIBILITY, "no access")
    control, _ = make(stub)
    await expect(ErrorCode.PERMISSION_ACCESSIBILITY, control)
    stub.error = OSError("HID unavailable")
    error = await expect(ErrorCode.INTERNAL, control)
    assert "HID unavailable" in error.message
    assert "yield_to_user" in (error.hint or "")
    assert not control.busy


# --------------------------------------------------------------------- one virtual hand


async def test_turns_are_serialized() -> None:
    control, _ = make()
    active = 0
    max_active = 0
    order: list[str] = []

    async def act(name: str) -> None:
        nonlocal active, max_active
        async with control.input_turn(timeout_s=10, label=name):
            active += 1
            max_active = max(max_active, active)
            order.append(f"{name}:start")
            await anyio.sleep(0.01)
            order.append(f"{name}:end")
            active -= 1

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            for i in range(20):
                tg.start_soon(act, f"t{i}")
    assert max_active == 1
    assert len(order) == 40
    for start, end in zip(order[::2], order[1::2], strict=True):
        assert start.split(":")[0] == end.split(":")[0]


async def test_busy_after_timeout_names_the_running_action() -> None:
    control, _ = make()
    release = anyio.Event()
    started = anyio.Event()
    errors: list[AXPilotError] = []

    async def holder() -> None:
        async with control.input_turn(label="drag"):
            started.set()
            await release.wait()

    async def contender() -> None:
        try:
            async with control.input_turn(timeout_s=0.05):
                pass
        except AXPilotError as error:
            errors.append(error)
        finally:
            release.set()

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(holder)
            await started.wait()
            tg.start_soon(contender)
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.BUSY
    assert "(drag)" in errors[0].message
    assert errors[0].details["current_action"] == "drag"
    assert errors[0].hint is not None


async def test_waiter_gets_the_turn_when_the_holder_finishes() -> None:
    control, _ = make()
    order: list[str] = []
    started = anyio.Event()

    async def holder() -> None:
        async with control.input_turn():
            started.set()
            await anyio.sleep(0.05)
            order.append("holder")

    async def waiter() -> None:
        async with control.input_turn(timeout_s=5):
            order.append("waiter")

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(holder)
            await started.wait()
            tg.start_soon(waiter)
    assert order == ["holder", "waiter"]


async def test_zero_timeout_still_takes_a_free_lock() -> None:
    control, _ = make()
    async with control.input_turn(timeout_s=0):
        pass


async def test_zero_timeout_on_a_held_lock_is_busy() -> None:
    control, _ = make()
    errors: list[ErrorCode] = []

    async def contender() -> None:
        try:
            async with control.input_turn(timeout_s=0):
                pass
        except AXPilotError as error:
            errors.append(error.code)

    async with control.input_turn(), anyio.create_task_group() as tg:
        tg.start_soon(contender)
    assert errors == [ErrorCode.BUSY]


async def test_nested_turn_in_the_same_task_is_a_clear_error() -> None:
    control, _ = make()
    async with control.input_turn():
        error = await expect(ErrorCode.INTERNAL, control, timeout_s=0)
        assert "re-entrant" in (error.hint or "")
        assert control.busy
    assert not control.busy


async def test_failed_turns_release_the_lock_for_waiters() -> None:
    control, stub = make(StubEngine(idle=0.0))
    for _ in range(3):
        await expect(ErrorCode.USER_ACTIVE, control)
    stub.idle = 10.0
    async with control.input_turn(timeout_s=0):
        pass


# --------------------------------------------------------------------------- user questions


async def test_an_open_question_refuses_input_until_it_closes() -> None:
    control, _ = make()
    assert not control.waiting_for_user
    with control.asking_user():
        with control.asking_user():  # questions may overlap
            assert control.waiting_for_user
        error = await expect(ErrorCode.NEEDS_USER, control)
        assert "answer" in error.message
        with pytest.raises(AXPilotError):
            control.ensure_running()
    assert not control.waiting_for_user
    async with control.input_turn():
        pass


async def test_a_question_opened_during_a_turn_stops_its_next_step() -> None:
    control, _ = make()
    async with control.input_turn():
        with control.asking_user(), pytest.raises(AXPilotError) as info:
            control.ensure_running()  # what long actions call between steps
    assert info.value.code is ErrorCode.NEEDS_USER
    assert not control.busy


# --------------------------------------------------------------------------- held questions
def test_a_held_question_blocks_actions_until_released() -> None:
    now = [0.0]
    control, _ = make(clock=lambda: now[0])
    control.hold_question("q1", 30.0)
    assert control.waiting_for_user
    with pytest.raises(AXPilotError) as info:
        control.ensure_running()
    assert info.value.code is ErrorCode.NEEDS_USER
    assert control.release_question("q1") is True
    assert not control.waiting_for_user
    control.ensure_running()
    assert control.release_question("q1") is False  # already closed


def test_a_held_question_expires() -> None:
    now = [0.0]
    control, _ = make(clock=lambda: now[0])
    control.hold_question("q1", 30.0)
    control.hold_question("q2", 60.0)
    now[0] = 31.0
    assert control.waiting_for_user  # q2 is still open
    assert control.release_question("q1") is False  # expired, and already forgotten
    now[0] = 61.0
    assert not control.waiting_for_user
    control.ensure_running()


async def test_input_turns_wait_for_held_questions() -> None:
    control, _ = make(yield_to_user=False)
    control.hold_question("q", 30.0)
    with pytest.raises(AXPilotError) as info:
        async with control.input_turn(label="click"):
            pass
    assert info.value.code is ErrorCode.NEEDS_USER
    control.release_question("q")
    async with control.input_turn(label="click"):
        pass
