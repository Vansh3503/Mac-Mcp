"""Who controls the mouse and keyboard right now: the kill switch, yield-to-user, and the
single "virtual hand".

Every input tool runs inside :meth:`ControlState.input_turn`::

    async with control.input_turn(label="click"):
        await anyio.to_thread.run_sync(engine.mouse_click, point)

A turn

1. fails fast with ``PAUSED_BY_USER`` while AXPilot is paused (kill switch), and with
   ``NEEDS_USER`` while one of AXPilot's questions waits for the person
   (:meth:`ControlState.asking_user`, or :meth:`ControlState.hold_question` for a question the
   MCP client shows between two rounds of a call): an agent must never answer its own
   confirmation;
2. waits for the one input lock (input actions never interleave; observations do not take
   the lock) and fails with ``BUSY`` after ``timeout_s``;
3. with yield-to-user on, fails with ``USER_ACTIVE`` if the user touched the mouse or
   keyboard within ``idle_threshold_s`` (``engine.hid_idle_seconds()``, read in a worker
   thread);
4. is cancelled by :meth:`ControlState.pause` while it runs, and then raises
   ``PAUSED_BY_USER``. :meth:`~ControlState.pause` may be called from any thread (for
   example the main thread's global hotkey handler).

Turns are not re-entrant: do not open a turn inside another one.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import anyio.from_thread
import anyio.lowlevel

from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["ControlState"]

_log = logging.getLogger(__name__)

_current_token = getattr(anyio.lowlevel, "current_token", None)  # anyio >= 4.11


@dataclass(eq=False, slots=True)
class _Turn:
    """One running input turn, cancellable by the kill switch."""

    scope: anyio.CancelScope
    token: Any
    thread_id: int
    label: str | None


def _cancel_from_other_thread(turn: _Turn) -> None:
    try:
        anyio.from_thread.run_sync(turn.scope.cancel, token=turn.token)
    except Exception as exc:  # the event loop is gone: the turn is over, nothing to cancel
        _log.debug("Pausing did not cancel %s: %r", turn.label or "an input turn", exc)


class ControlState:
    """Pause state, yield-to-user and serialization of input actions.

    ``paused`` and ``pause_reason`` are public for status reporting; change them with
    :meth:`pause` and :meth:`resume`, which also stop the input action in progress.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        yield_to_user: bool = True,
        idle_threshold_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.yield_to_user = yield_to_user
        self.idle_threshold_s = idle_threshold_s
        self.paused: bool = False
        self.pause_reason: str | None = None
        self._clock = clock
        self._lock = anyio.Lock()
        self._state = threading.Lock()
        self._turns: set[_Turn] = set()
        self._holder: str | None = None
        self._owner: int | None = None
        self._held = False
        self._questions = 0
        self._held_questions: dict[str, float] = {}

    # -------------------------------------------------------------- pause / resume
    def pause(self, reason: str = "kill switch") -> None:
        """Pause all input and stop the input action in progress. Thread-safe."""
        with self._state:
            self.paused = True
            self.pause_reason = reason
            turns = list(self._turns)
        for turn in turns:
            self._cancel(turn)

    def resume(self) -> None:
        """Allow input again. Thread-safe."""
        with self._state:
            self.paused = False
            self.pause_reason = None

    def ensure_running(self) -> None:
        """Raise ``PAUSED_BY_USER`` if paused, or ``NEEDS_USER`` while a question to the user is
        open. Long actions call this between steps."""
        if self.paused:
            raise self._paused_error()
        if self.waiting_for_user:
            raise AXPilotError(
                ErrorCode.NEEDS_USER,
                "AXPilot is waiting for the person to answer one of its questions; other actions "
                "wait until they do.",
                hint="Wait for their answer (or ask them in the chat), then try again.",
            )

    # -------------------------------------------------------------- user questions
    @contextmanager
    def asking_user(self) -> Iterator[None]:
        """Mark a question to the user (a confirmation, a pairing request) as open.

        Until it closes, :meth:`ensure_running` refuses new actions and the next step of running
        ones, so no tool call can click, type into or otherwise answer the dialog for the user.
        Thread-safe; questions may overlap."""
        with self._state:
            self._questions += 1
        try:
            yield
        finally:
            with self._state:
                self._questions -= 1

    def hold_question(self, key: str, ttl_s: float) -> None:
        """Mark a question that the MCP client shows between two rounds of one call (MCP
        elicitation through ``InputRequiredResult``) as open for at most ``ttl_s`` seconds.

        It counts like :meth:`asking_user` until :meth:`release_question` is called with the same
        ``key`` (the client's retry carries the answer) or the time runs out (the client gave up
        without telling us). Thread-safe."""
        with self._state:
            self._held_questions[key] = self._clock() + max(ttl_s, 0.0)

    def release_question(self, key: str) -> bool:
        """Close a question opened with :meth:`hold_question`. Returns True if it was open (and
        not expired). Thread-safe."""
        with self._state:
            deadline = self._held_questions.pop(key, None)
        return deadline is not None and deadline > self._clock()

    @property
    def waiting_for_user(self) -> bool:
        """True while a question to the user is open."""
        if self._questions > 0:
            return True
        if not self._held_questions:
            return False
        now = self._clock()
        with self._state:
            for key in [k for k, deadline in self._held_questions.items() if deadline <= now]:
                del self._held_questions[key]
            return bool(self._held_questions)

    # ------------------------------------------------------------------- status
    @property
    def busy(self) -> bool:
        """True while an input turn holds the lock."""
        return self._held

    @property
    def current_action(self) -> str | None:
        """The ``label`` of the turn holding the lock, if any."""
        return self._holder if self._held else None

    def to_dict(self) -> dict[str, Any]:
        """State for the ``status`` tool."""
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "busy": self.busy,
            "current_action": self.current_action,
            "yield_to_user": self.yield_to_user,
            "idle_threshold_s": self.idle_threshold_s,
        }

    # -------------------------------------------------------------------- turns
    @asynccontextmanager
    async def input_turn(self, timeout_s: float = 5.0, *, label: str | None = None) -> AsyncIterator[None]:
        """Hold the input lock for one action. See the module docstring for the errors.

        ``label`` (e.g. the tool name) is reported to callers that get ``BUSY``.
        """
        self.ensure_running()
        self._refuse_nesting()
        await self._acquire(timeout_s)
        self._held, self._holder, self._owner = True, label, anyio.get_current_task().id
        try:
            turn = self._begin(label)
            try:
                with turn.scope:
                    await self._yield_to_user()
                    # The idle check ran shielded in a worker thread: deliver a pause that
                    # happened meanwhile before the action starts.
                    await anyio.lowlevel.checkpoint_if_cancelled()
                    self.ensure_running()
                    yield
            finally:
                with self._state:
                    self._turns.discard(turn)
            if turn.scope.cancelled_caught:
                reason = self.pause_reason or "kill switch"
                raise AXPilotError(
                    ErrorCode.PAUSED_BY_USER,
                    f"The action was stopped because AXPilot was paused ({reason}).",
                )
        finally:
            self._held, self._holder, self._owner = False, None, None
            self._lock.release()

    # ---------------------------------------------------------------- internals
    def _paused_error(self) -> AXPilotError:
        return AXPilotError(
            ErrorCode.PAUSED_BY_USER,
            f"AXPilot is paused ({self.pause_reason or 'kill switch'}); actions are blocked until the "
            "user resumes it.",
        )

    def _refuse_nesting(self) -> None:
        if self._held and self._owner == anyio.get_current_task().id:
            raise AXPilotError(
                ErrorCode.INTERNAL,
                "An input turn was opened inside another input turn of the same task.",
                hint="This is an AXPilot bug: input turns are not re-entrant.",
            )

    async def _acquire(self, timeout_s: float) -> None:
        try:
            self._lock.acquire_nowait()
            return
        except anyio.WouldBlock:
            pass
        holder = self._holder
        with anyio.move_on_after(max(timeout_s, 0.0)):
            await self._lock.acquire()
            return
        running = f" ({holder})" if holder else ""
        raise AXPilotError(
            ErrorCode.BUSY,
            f"Another input action{running} is still running; waited {timeout_s:g} s.",
            hint="AXPilot performs one input action at a time. Retry in a moment.",
            details={"waited_s": timeout_s, "current_action": holder},
        )

    def _begin(self, label: str | None) -> _Turn:
        token = _current_token() if _current_token is not None else None
        turn = _Turn(anyio.CancelScope(), token, threading.get_ident(), label)
        with self._state:
            self.ensure_running()  # paused, or a question opened, while we waited for the lock
            self._turns.add(turn)
        return turn

    def _cancel(self, turn: _Turn) -> None:
        if threading.get_ident() == turn.thread_id:
            with contextlib.suppress(RuntimeError):  # its event loop is gone
                turn.scope.cancel()
        elif turn.token is not None:
            # Never block the caller (often the UI thread) on the event loop.
            threading.Thread(
                target=_cancel_from_other_thread, args=(turn,), name="axpilot-pause", daemon=True
            ).start()

    async def _yield_to_user(self) -> None:
        if not self.yield_to_user or self.idle_threshold_s <= 0:
            return
        try:
            idle = float(await anyio.to_thread.run_sync(self.engine.hid_idle_seconds))
        except AXPilotError:
            raise
        except Exception as exc:
            raise AXPilotError(
                ErrorCode.INTERNAL,
                f"Could not read how long the user has been idle: {exc}",
                hint="Yield-to-user needs the HID idle time. The user can turn it off with "
                "[safety] yield_to_user = false.",
            ) from exc
        if math.isfinite(idle) and idle < self.idle_threshold_s:
            raise AXPilotError(
                ErrorCode.USER_ACTIVE,
                f"The user is using the mouse or keyboard (last input {idle:.1f} s ago); AXPilot "
                f"waits for {self.idle_threshold_s:g} s without user input before acting.",
                details={"idle_s": round(idle, 3), "threshold_s": self.idle_threshold_s},
            )
