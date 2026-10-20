"""The shared state behind every MCP tool: :class:`ServerContext`.

One context exists per process. It owns the engine, the safety policy, the snapshot and
screenshot stores, the rate limiter, the audit log, the confirmer, the kill switch / input
lock (:class:`~axpilot.policy.control.ControlState`) and the command runner. Each MCP server
built from it (one per client connection in host mode) gets a :meth:`ServerContext.scoped`
copy that shares all of that state but has its own profile, enabled tools and client name.

Tools use the helpers here instead of calling the engine directly:

* :meth:`ServerContext.run_engine` runs a blocking engine call in a worker thread with a
  deadline (``TIMEOUT``) and never blocks the event loop; a cancelled call is abandoned.
* :meth:`ServerContext.capture_tree` / :meth:`ServerContext.take_snapshot` apply the
  ``observe`` policy before and after reading an app's accessibility tree;
  :meth:`ServerContext.build_snapshot` numbers every snapshot with session refs that stay
  with their element across snapshots (see :mod:`axpilot.perception.refs`).
* :meth:`ServerContext.confirm_action` asks the user (bounded wait, ``USER_DENIED``): in direct
  mode through the MCP client when it supports elicitation (:mod:`axpilot.server.elicit`),
  otherwise through the context's confirmer.
* :meth:`ServerContext.report_progress` sends MCP progress notifications for the current call.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import logging
import os
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import anyio

from axpilot import paths
from axpilot.config import Config
from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawTree, WindowInfo
from axpilot.perception.model import Snapshot, SnapshotMode
from axpilot.perception.refs import HISTORY_LIMIT, carry_refs
from axpilot.perception.snapshot import build_snapshot
from axpilot.perception.store import CaptureStore, SnapshotStore
from axpilot.policy.audit import AuditLog
from axpilot.policy.confirm import Confirmer, ConfirmRequest, DenyConfirmer
from axpilot.policy.control import ControlState
from axpilot.policy.profiles import enabled_tools
from axpilot.policy.ratelimit import RateLimiter
from axpilot.policy.rules import AccessKind, Policy
from axpilot.server import elicit
from axpilot.system.runner import CommandRunner

__all__ = [
    "LOG_FILE_NAME",
    "CallState",
    "ProgressFn",
    "ServerContext",
    "call_scope",
    "current_call",
    "default_log_file",
    "new_incident_id",
    "setup_file_logging",
]

T = TypeVar("T")

ProgressFn = Callable[[float, float | None, str | None], Awaitable[None]]
"""Sends one MCP progress notification: ``(progress, total, message)``."""

LOG_FILE_NAME = "axpilot.log"
LOGGER_NAME = "axpilot"
CONFIRM_TIMEOUT_MAX_S = 120.0
"""Confirmations never wait longer than this (QUALITY.md A5)."""

_log = logging.getLogger(__name__)
_file_handlers: dict[Path, logging.Handler] = {}
_file_handlers_lock = threading.Lock()


def new_incident_id() -> str:
    """A short id (``err_`` + 8 hex digits) that ties an ``INTERNAL`` error to its logged traceback."""
    return "err_" + secrets.token_hex(4)


def default_log_file() -> Path:
    """``~/Library/Logs/AXPilot/axpilot.log`` (or ``$AXPILOT_LOG_DIR/axpilot.log``)."""
    return paths.log_dir() / LOG_FILE_NAME


def setup_file_logging(path: Path | None = None, *, level: int = logging.INFO) -> logging.Handler | None:
    """Send the ``axpilot`` logger (tracebacks of ``INTERNAL`` errors included) to ``path``.

    Idempotent per path. The file is created with mode ``0600`` in a ``0700`` directory. Never
    raises: if the file cannot be opened, a warning is logged and ``None`` returned.
    """
    target = (path or default_log_file()).expanduser()
    with _file_handlers_lock:
        if target in _file_handlers:
            return _file_handlers[target]
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.close(fd)
            handler: logging.Handler = logging.FileHandler(target, encoding="utf-8")
        except OSError as exc:
            _log.warning("Cannot open the AXPilot log file %s: %s", target, exc)
            return None
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger = logging.getLogger(LOGGER_NAME)
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > level:
            logger.setLevel(level)
        _file_handlers[target] = handler
        return handler


# --------------------------------------------------------------------------- per-call state
@dataclass(slots=True)
class CallState:
    """Facts about the tool call in progress (set by the guard, read through :func:`current_call`)."""

    tool: str
    """Canonical tool name (``snapshot``), even when exposed under another name."""
    public_name: str
    """The name the client called (``Snapshot`` under the Windows-MCP naming)."""
    client: str | None = None
    progress: ProgressFn | None = None
    confirmed: bool = False
    """True once the user approved this call (so a tool does not ask twice)."""
    mcp: Any = None
    """The MCP SDK's request context of this call (client capabilities, protocol version and the
    answers of an elicitation retry), when the call came through MCP."""


_CURRENT_CALL: ContextVar[CallState | None] = ContextVar("axpilot_current_call", default=None)


def current_call() -> CallState | None:
    """The :class:`CallState` of the tool call running in this task, if any."""
    return _CURRENT_CALL.get()


@contextlib.contextmanager
def call_scope(state: CallState) -> Iterator[CallState]:
    """Make ``state`` the :func:`current_call` for the duration of the ``with`` block."""
    token = _CURRENT_CALL.set(state)
    try:
        yield state
    finally:
        _CURRENT_CALL.reset(token)


# --------------------------------------------------------------------------- the context
@dataclass(eq=False)
class ServerContext:
    """Everything a tool needs. Create it with :meth:`create`; see the module docstring."""

    config: Config
    engine: Engine
    policy: Policy
    snapshots: SnapshotStore
    captures: CaptureStore
    limiter: RateLimiter
    audit: AuditLog
    confirmer: Confirmer
    control: ControlState
    runner: CommandRunner
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(f"{LOGGER_NAME}.server"))
    incident_ids: Callable[[], str] = new_incident_id
    """Factory of incident ids for ``INTERNAL`` errors (injectable for tests)."""
    enabled: tuple[str, ...] = ()
    """Tools enabled for this server, in ``TOOL_ORDER``."""
    profile: str = "standard"
    naming: str = "default"
    client_name: str | None = None
    """Display name of the connected client (``"Claude"``), when known."""
    mode: Literal["direct", "host"] = "direct"
    """``direct``: tools run in this process; ``host``: behind AXPilot.app's socket."""
    host_info: Callable[[], Mapping[str, Any]] | None = None
    """Host mode only: returns ``{"pairing": str, "clients": [...]}`` for ``status``."""
    elicitation: bool = False
    """Direct mode: ask confirmations through the MCP client when it supports elicitation
    (``[safety] elicitation``; off with ``--yes``, which approves without asking)."""
    questions: elicit.QuestionBook = field(default_factory=elicit.QuestionBook)
    """Open elicitation questions (shared by every scoped copy)."""
    engine_timeout_s: float = 20.0
    capture_timeout_s: float = 5.0
    confirm_timeout_s: float = CONFIRM_TIMEOUT_MAX_S
    input_lock_timeout_s: float = 5.0
    clock: Callable[[], float] = time.monotonic
    """Monotonic clock used by polling tools (injectable for tests)."""
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep
    """Async sleep used by polling tools (injectable for tests)."""

    # ------------------------------------------------------------------ construction
    @classmethod
    def create(
        cls,
        config: Config,
        engine: Engine,
        *,
        confirmer: Confirmer | None = None,
        runner: CommandRunner | None = None,
        audit_path: Path | None = None,
        log_path: Path | None = None,
        elicitation: bool | None = None,
    ) -> ServerContext:
        """Build the context for ``config`` and ``engine``.

        ``confirmer`` defaults to :class:`DenyConfirmer` (nobody can be asked, so calls that
        need a confirmation fail with ``USER_DENIED``). ``elicitation`` (default
        ``[safety] elicitation``) lets direct mode ask through the MCP client first; pass
        ``False`` when the confirmer approves on the user's behalf (``--yes``). ``audit_path``
        overrides ``config.audit_path()``. ``log_path`` additionally sends AXPilot's log
        (including tracebacks of ``INTERNAL`` errors) to that file; the CLI passes
        :func:`default_log_file`. Raises ``INVALID_ARGUMENT`` for an invalid tool selection
        or policy entry in ``config``.
        """
        tools = config.tools
        enabled = tuple(enabled_tools(tools.profile, tools.enable, tools.disable))
        if log_path is not None:
            setup_file_logging(log_path)
        return cls(
            config=config,
            engine=engine,
            policy=Policy(config.safety, enabled),
            snapshots=SnapshotStore(),
            captures=CaptureStore(),
            limiter=RateLimiter(config.safety.max_actions_per_minute),
            audit=AuditLog(
                audit_path if audit_path is not None else config.audit_path(),
                enabled=config.audit.enabled,
                log_args=config.audit.log_args,
            ),
            confirmer=confirmer if confirmer is not None else DenyConfirmer(),
            control=ControlState(
                engine,
                yield_to_user=config.safety.yield_to_user,
                idle_threshold_s=config.safety.user_idle_threshold_s,
            ),
            runner=runner if runner is not None else CommandRunner(),
            enabled=enabled,
            profile=tools.profile,
            naming=tools.naming,
            engine_timeout_s=config.server.engine_timeout_s,
            capture_timeout_s=config.perception.capture_timeout_s,
            confirm_timeout_s=min(config.safety.confirm_timeout_s, CONFIRM_TIMEOUT_MAX_S),
            input_lock_timeout_s=config.server.input_lock_timeout_s,
            elicitation=config.safety.elicitation if elicitation is None else elicitation,
        )

    def scoped(
        self,
        *,
        profile: str | None = None,
        enabled: tuple[str, ...] | list[str] | None = None,
        naming: str | None = None,
        client_name: str | None = None,
    ) -> ServerContext:
        """A copy for one MCP server: same engine, stores, limiter, audit, confirmer and input
        lock, but its own profile, enabled tools (and so its own :class:`Policy`) and client."""
        tools = tuple(enabled) if enabled is not None else self.enabled
        return dataclasses.replace(
            self,
            policy=Policy(self.config.safety, tools),
            enabled=tools,
            profile=profile or self.profile,
            naming=naming or self.naming,
            client_name=client_name if client_name is not None else self.client_name,
        )

    # ------------------------------------------------------------------ helpers
    def new_incident_id(self) -> str:
        """A fresh incident id (``err_xxxxxxxx``)."""
        return self.incident_ids()

    async def run_engine(
        self, fn: Callable[..., T], *args: Any, timeout_s: float | None = None, **kwargs: Any
    ) -> T:
        """Run a blocking call (normally an engine method) in a worker thread with a deadline.

        ``timeout_s`` defaults to :attr:`engine_timeout_s`; a call that runs longer raises
        ``TIMEOUT`` (the worker thread is abandoned and its result ignored). When the client
        cancels, the call is abandoned the same way and cancellation propagates. Keyword
        arguments go to ``fn``; to pass a ``timeout_s`` to ``fn`` itself, bind it with
        :func:`functools.partial`.
        """
        deadline = self.engine_timeout_s if timeout_s is None else timeout_s
        call = functools.partial(fn, *args, **kwargs) if kwargs else (lambda: fn(*args))
        with anyio.move_on_after(max(deadline, 0.0)):
            return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        name = getattr(fn, "__name__", None) or getattr(getattr(fn, "func", None), "__name__", "engine call")
        raise AXPilotError(
            ErrorCode.TIMEOUT,
            f"{name} did not finish within {deadline:g} s.",
            hint="The app may be busy or unresponsive. Retry, or narrow the request (app, window_id).",
            details={"operation": name, "timeout_s": deadline},
        )

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        """Send a progress notification for the current call (no-op if the client did not ask
        for progress). Never raises."""
        state = current_call()
        if state is None or state.progress is None:
            return
        try:
            await state.progress(progress, total, message)
        except Exception as exc:  # a progress notification must never fail the tool
            self.logger.debug("progress notification failed: %s", exc)

    async def ask_user(self, request: ConfirmRequest) -> None:
        """Ask the user; raise ``USER_DENIED`` on a "no" or when nobody answers within
        :attr:`confirm_timeout_s` (at most 120 s).

        In direct mode, a client that supports elicitation is asked first (see
        :mod:`axpilot.server.elicit`): during the call on older protocols, or by ending this
        round with an ``InputRequiredResult`` (:class:`~axpilot.server.elicit.InputRequired`)
        on 2026-07-28 and newer. Otherwise, and when the client fails the request, the
        context's confirmer decides."""
        timeout = min(self.confirm_timeout_s, CONFIRM_TIMEOUT_MAX_S)
        state = current_call()
        mcp = state.mcp if state is not None else None
        if self.elicitation and self.mode == "direct" and mcp is not None and elicit.client_can_elicit(mcp):
            if elicit.uses_input_required(mcp):
                if elicit.answer_from_retry(mcp, request, self.questions, self.clock()):
                    return
                nonce = self.questions.issue(self.clock(), timeout)
                self.control.hold_question(nonce, timeout)  # no tool may answer it meanwhile
                raise elicit.InputRequired(elicit.question_result(request, nonce))
            answer: bool | None = None
            with self.control.asking_user(), anyio.move_on_after(timeout) as scope:
                answer = await elicit.ask_during_call(mcp, request)
            if scope.cancelled_caught:
                raise self._unanswered(request, timeout)
            if answer:
                return
            if answer is not None:
                raise AXPilotError(
                    ErrorCode.USER_DENIED,
                    f"The user did not approve: {request.summary}",
                    hint="The user declined in the client. Do not retry the same action; ask them what "
                    "to do instead.",
                    details={"tool": request.tool, "via": "elicitation"},
                )
        approved = False
        with self.control.asking_user(), anyio.move_on_after(timeout) as scope:
            approved = await self.confirmer.confirm(request)
        if scope.cancelled_caught:
            raise self._unanswered(request, timeout)
        if not approved:
            hint = getattr(self.confirmer, "denial_hint", None) or (
                "The user declined. Do not retry the same action; ask them what to do instead."
            )
            raise AXPilotError(
                ErrorCode.USER_DENIED,
                f"The user did not approve: {request.summary}",
                hint=hint,
                details={"tool": request.tool},
            )

    @staticmethod
    def _unanswered(request: ConfirmRequest, timeout: float) -> AXPilotError:
        return AXPilotError(
            ErrorCode.USER_DENIED,
            f"Nobody answered the confirmation within {timeout:g} s: {request.summary}",
            hint="The user did not respond. Ask them in the chat before trying again.",
            details={"tool": request.tool, "timed_out": True},
        )

    async def confirm_action(
        self, summary: str, detail: str | None = None, *, destructive: bool = True, tool: str | None = None
    ) -> None:
        """Ask the user to approve one risky action of the current call, if the policy wants it.

        Use this for actions that are destructive only for some arguments (``force_quit``,
        killing a process). Does nothing when ``[safety] confirm`` does not require it or the
        user already approved this call. Raises ``USER_DENIED``.
        """
        state = current_call()
        name = tool or (state.tool if state is not None else "unknown")
        if state is not None and state.confirmed:
            return
        if not self.policy.requires_confirmation(name, destructive=destructive):
            return
        client = state.client if state is not None else self.client_name
        await self.ask_user(ConfirmRequest(tool=name, summary=summary, detail=detail, client=client))
        if state is not None:
            state.confirmed = True

    # ------------------------------------------------------------------ targets & trees
    async def resolve_app(self, app: str | None) -> AppInfo | None:
        """The running app named by ``app`` (name, bundle id or pid), or the frontmost app for
        ``None`` (which may itself be ``None``). Raises ``APP_NOT_FOUND``."""
        if app is None:
            return await self.run_engine(self.engine.frontmost_app)
        return await self.run_engine(self.engine.find_app, app)

    async def find_window(self, window_id: int) -> WindowInfo:
        """The on-screen window with ``window_id``. Raises ``WINDOW_NOT_FOUND``."""
        windows = await self.run_engine(self.engine.list_windows)
        for window in windows:
            if window.window_id == window_id:
                return window
        raise AXPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"No open window has id {window_id}.",
            hint="Use `list_windows` to see the current window ids.",
        )

    async def check_target(
        self, *, app: str | None, window_id: int | None, kind: AccessKind
    ) -> tuple[AppInfo | None, WindowInfo | None]:
        """Resolve the app/window a call is about to touch and apply the policy for ``kind``.

        Returns the resolved ``(app, window)``. Raises ``APP_NOT_FOUND``/``WINDOW_NOT_FOUND``,
        ``BLOCKED_BY_POLICY`` or ``NEEDS_USER``."""
        window = await self.find_window(window_id) if window_id is not None else None
        if window is not None:
            target: AppInfo | None = AppInfo(name=window.app, pid=window.pid, bundle_id=window.bundle_id)
            if app is not None and not target.matches(app):
                raise AXPilotError(
                    ErrorCode.WINDOW_NOT_FOUND,
                    f"Window {window_id} belongs to {window.app}, not {app!r}.",
                    hint="Pass only window_id, or a window of that app (see `list_windows`).",
                )
        else:
            target = await self.resolve_app(app)
        self.policy.check_app(target, kind=kind, window_title=window.title if window is not None else None)
        return target, window

    async def capture_tree(
        self,
        *,
        app: str | None = None,
        window_id: int | None = None,
        include_menus: bool = False,
        kind: AccessKind = "observe",
    ) -> RawTree:
        """Capture an app's accessibility tree with the policy checked before (target app and
        window) and after (the app and window actually captured)."""
        await self.check_target(app=app, window_id=window_id, kind=kind)
        perception = self.config.perception
        capture = functools.partial(
            self.engine.capture_tree,
            app=app,
            window_id=window_id,
            include_menus=include_menus,
            max_depth=perception.max_depth,
            max_nodes=perception.max_nodes,
            timeout_s=self.capture_timeout_s,
        )
        tree = await self.run_engine(capture, timeout_s=self.capture_timeout_s + self.engine_timeout_s)
        title = tree.window.title if tree.window is not None else None
        self.policy.check_app(tree.app, kind=kind, window_title=title)
        return tree

    def build_snapshot(
        self,
        tree: RawTree,
        *,
        mode: SnapshotMode = "interactive",
        max_elements: int | None = None,
        offset: int = 0,
        store: bool = True,
        previous: Snapshot | None = None,
    ) -> Snapshot:
        """Prune ``tree`` with the configured limits, give it session refs, and store it unless
        ``store`` is false (stored snapshots are what bare refs resolve against).

        Refs come from the store's counter whether or not the snapshot is stored: elements that
        match an element of ``previous`` (default: the latest stored snapshot of the same app
        window) keep its ref, elements seen in older stored snapshots of that window keep theirs
        when the engine handle is the same, and everything else gets a never-used number."""
        snap = build_snapshot(
            tree,
            snapshot_id=self.snapshots.new_id(),
            mode=mode,
            max_elements=max_elements or self.config.perception.max_elements,
            offset=offset,
            value_max_chars=self.config.perception.value_max_chars,
        )
        window_id = snap.window.window_id if snap.window is not None else None
        history = self.snapshots.snapshots_of(snap.app.pid, window_id, limit=HISTORY_LIMIT)
        snap = carry_refs(snap, previous, self.snapshots.refs, history=history)
        if store:
            self.snapshots.put(snap)
        return snap

    async def take_snapshot(
        self,
        *,
        app: str | None = None,
        window_id: int | None = None,
        mode: SnapshotMode = "interactive",
        max_elements: int | None = None,
        offset: int = 0,
        include_menus: bool = False,
        store: bool = True,
        previous: Snapshot | None = None,
    ) -> Snapshot:
        """:meth:`capture_tree` + :meth:`build_snapshot` (AX only; the ``snapshot`` tool adds
        the app profile, OCR and screenshots on top). ``previous`` is the snapshot whose refs
        to carry (default: the latest stored one of the same window)."""
        tree = await self.capture_tree(app=app, window_id=window_id, include_menus=include_menus)
        return self.build_snapshot(
            tree, mode=mode, max_elements=max_elements, offset=offset, store=store, previous=previous
        )

    def close(self) -> None:
        """Release the engine. Safe to call more than once."""
        with contextlib.suppress(AXPilotError):
            self.engine.close()
