"""The automation SDK: :class:`AsyncDesktop` and its synchronous twin :class:`Desktop`.

Both call AXPilot's MCP tools through an MCP client, either in this process
(:meth:`~AsyncDesktop.direct`, :meth:`~AsyncDesktop.fake`) or through the AXPilot host
(:meth:`~AsyncDesktop.connect`, using AXPilot.app's permissions). There is no second code path:
scripts get the same tools, policy, confirmations, audit records and error codes as agents.
A failed tool call raises :class:`~axpilot.errors.AXPilotError` with the tool's code, message and
hint.

:class:`Desktop` runs an :class:`AsyncDesktop` on its own event loop in a background thread
(an anyio blocking portal), so it works in plain scripts, threads and Jupyter alike. Use either
as a context manager, or call :meth:`~Desktop.close` when done::

    from axpilot import Desktop

    with Desktop.fake() as mac:  # the simulated desktop; Desktop.connect() on a Mac
        notes = mac.app("Notes").activate()
        mac.find("New Note").click()
        mac.type_text("Groceries")
"""

from __future__ import annotations

import base64
import contextlib
import functools
import os
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

import anyio
import anyio.from_thread
from mcp import Client
from mcp_types import CallToolResult, ImageContent, Implementation, TextContent

from axpilot import __version__
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.runloop import main_thread_pump, start_ticking, wait_pumping
from axpilot.sdk.results import (
    ActionResult,
    AppHandle,
    AsyncAppHandle,
    AsyncElementHandle,
    Element,
    ElementHandle,
    FindResult,
    ReadResult,
    ScreenshotResult,
    SnapshotResult,
    ToolResult,
)

__all__ = ["AsyncDesktop", "Desktop"]

P = ParamSpec("P")
R = TypeVar("R")
_ERROR_META_KEY = "axpilot/error"
SDK_CLIENT = "AXPilot SDK"

Opener = Callable[[AsyncExitStack], Awaitable[Client]]


def _client_label() -> str:
    script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return f"{SDK_CLIENT} ({script})" if script and script not in ("-c", "-m") else SDK_CLIENT


def _confirmer(confirm: Any, *, fake: bool) -> Any:
    from axpilot.policy.confirm import AutoConfirmer, DenyConfirmer  # noqa: PLC0415

    if isinstance(confirm, bool):
        return AutoConfirmer(allow=confirm)
    if confirm is not None:
        return confirm
    if sys.platform == "darwin" and not fake:
        from axpilot.host.dialogs import DialogConfirmer  # noqa: PLC0415

        return DialogConfirmer()
    return DenyConfirmer()


def _plain(value: str | bool | float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class AsyncDesktop:
    """Operate the Mac (or the simulated desktop) from async code. See the module docstring."""

    def __init__(self, opener: Opener, *, owned_context: Any = None) -> None:
        """Prefer the :meth:`direct`, :meth:`fake` and :meth:`connect` constructors."""
        self._opener = opener
        self._context = owned_context
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._lock = anyio.Lock()
        self._handle_factory: Callable[[str, str | None, Element | None], Any] = lambda ref, sid, el: (
            AsyncElementHandle(self, ref, sid, el)
        )
        self._app_factory: Callable[[str], Any] = lambda name: AsyncAppHandle(self, name)

    # ------------------------------------------------------------------ constructors
    @classmethod
    def from_context(
        cls, ctx: Any, *, profile: str | None = None, client_name: str | None = None, own: bool = False
    ) -> AsyncDesktop:
        """Serve the tools of an existing :class:`~axpilot.server.context.ServerContext` in this
        process (``own``: close it with the desktop)."""
        from axpilot.server.app import build_server  # noqa: PLC0415

        label = client_name or _client_label()

        async def open_client(stack: AsyncExitStack) -> Client:
            server = build_server(ctx, profile=profile, naming="default", client_name=label)
            return await stack.enter_async_context(
                Client(server, client_info=Implementation(name=label, version=__version__))
            )

        return cls(open_client, owned_context=ctx if own else None)

    @classmethod
    def direct(
        cls,
        config: Any = None,
        engine: Any = None,
        *,
        profile: str | None = None,
        confirm: Any = None,
    ) -> AsyncDesktop:
        """Run the tools in this process with this process's macOS permissions.

        ``config`` defaults to the user's configuration, ``engine`` to the macOS engine.
        ``confirm``: ``True`` approves confirmations, ``False`` declines them, a
        :class:`~axpilot.policy.confirm.Confirmer` decides; by default a dialog asks on a Mac."""
        from axpilot.config import load_config  # noqa: PLC0415
        from axpilot.engine import create_engine  # noqa: PLC0415
        from axpilot.server.context import ServerContext, default_log_file  # noqa: PLC0415

        engine = engine if engine is not None else create_engine("macos")
        is_fake = type(engine).__name__ == "FakeEngine"
        ctx = ServerContext.create(
            config if config is not None else load_config(),
            engine,
            confirmer=_confirmer(confirm, fake=is_fake),
            log_path=default_log_file(),
        )
        return cls.from_context(ctx, profile=profile, own=True)

    @classmethod
    def fake(
        cls,
        fixture: str | Path | None = None,
        *,
        config: Mapping[str, Any] | None = None,
        profile: str | None = "full",
        confirm: Any = True,
    ) -> AsyncDesktop:
        """The simulated desktop (``fixture``, default: the demo desktop), for tests and examples.
        Uses no real config, home directory or commands. ``config`` takes dotted overrides
        (``{"safety.confirm": "never"}``); ``profile`` defaults to ``full``."""
        from axpilot.engine.fake import FakeEngine  # noqa: PLC0415
        from axpilot.testing import make_test_context  # noqa: PLC0415

        engine = FakeEngine.from_fixture(fixture) if fixture is not None else FakeEngine.demo()
        ctx = make_test_context(dict(config or {}), engine=engine, confirm=_confirmer(confirm, fake=True))
        return cls.from_context(ctx, profile=profile, own=True)

    @classmethod
    def connect(cls, socket_path: str | Path | None = None, *, profile: str | None = None) -> AsyncDesktop:
        """Talk to the AXPilot host (AXPilot.app) over its socket, with its permissions. The host
        may ask the user once to allow this script. Raises ``HOST_UNAVAILABLE`` when it is not
        running."""
        from axpilot.bridge.client import host_transport  # noqa: PLC0415
        from axpilot.bridge.protocol import ClientIdentity, Hello  # noqa: PLC0415

        label = _client_label()
        hello = Hello(
            client=ClientIdentity(parent_app=label, pid=os.getpid()), profile=profile, naming="default"
        )

        async def open_client(stack: AsyncExitStack) -> Client:
            streams = await stack.enter_async_context(
                host_transport(Path(socket_path) if socket_path is not None else None, hello)
            )

            @contextlib.asynccontextmanager
            async def opened() -> AsyncIterator[Any]:
                yield streams

            return await stack.enter_async_context(
                Client(opened(), client_info=Implementation(name=label, version=__version__))  # type: ignore[arg-type]
            )

        return cls(open_client)

    # ------------------------------------------------------------------ lifecycle
    @property
    def context(self) -> Any:
        """The in-process :class:`~axpilot.server.context.ServerContext` (``None`` for
        :meth:`connect`); tests use it to look at the fake engine."""
        return self._context

    async def __aenter__(self) -> AsyncDesktop:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def open(self) -> None:
        """Connect (done automatically by the first call)."""
        async with self._lock:
            if self._client is not None:
                return
            stack = AsyncExitStack()
            # On macOS's main thread, keep its Cocoa run loop turning while this is open.
            stack.callback(start_ticking(main_thread_pump()))
            try:
                self._client = await self._opener(stack)
            except BaseException:
                await stack.aclose()
                raise
            self._stack = stack

    async def aclose(self) -> None:
        """Disconnect and release the engine. Safe to call more than once."""
        stack, self._stack, self._client = self._stack, None, None
        try:
            if stack is not None:
                await stack.aclose()
        finally:
            if self._context is not None:
                context, self._context = self._context, None
                context.close()

    # ------------------------------------------------------------------ calling tools
    async def call(
        self, tool: str, arguments: Mapping[str, Any] | None = None, /, **kwargs: Any
    ) -> ToolResult:
        """Call any tool by name (``None`` arguments are left out). Raises :class:`AXPilotError`."""
        result = await self._call(tool, {**(arguments or {}), **kwargs})
        return _parse(ToolResult, tool, result)

    async def _call(self, tool: str, arguments: Mapping[str, Any]) -> CallToolResult:
        if self._client is None:
            await self.open()
        assert self._client is not None
        args = {key: value for key, value in arguments.items() if value is not None}
        result = await self._client.call_tool(tool, args)
        if result.is_error:
            raise _error_of(result)
        return result

    async def _typed(self, kind: type[Any], tool: str, **arguments: Any) -> Any:
        return _parse(kind, tool, await self._call(tool, arguments))

    # ------------------------------------------------------------------ observe
    async def status(self) -> ToolResult:
        return await self.call("status")

    async def snapshot(
        self,
        app: str | None = None,
        *,
        window_id: int | None = None,
        mode: str | None = None,
        max_elements: int | None = None,
        offset: int | None = None,
        include_menus: bool | None = None,
        include_screenshot: bool | None = None,
        annotate: bool | None = None,
    ) -> SnapshotResult[AsyncElementHandle]:
        """The UI of ``app`` (default: the frontmost app) as elements with refs."""
        result = await self._typed(
            SnapshotResult,
            "snapshot",
            app=app,
            window_id=window_id,
            mode=mode,
            max_elements=max_elements,
            offset=offset,
            include_menus=include_menus,
            include_screenshot=include_screenshot,
            annotate=annotate,
        )
        result.handles = self._handle_factory
        return result

    async def screenshot(
        self,
        app: str | None = None,
        *,
        window_id: int | None = None,
        display_id: int | None = None,
        rect: Sequence[float] | None = None,
        max_edge: int | None = None,
        format: str | None = None,
    ) -> ScreenshotResult:
        return await self._typed(
            ScreenshotResult,
            "screenshot",
            app=app,
            window_id=window_id,
            display_id=display_id,
            rect=list(rect) if rect is not None else None,
            max_edge=max_edge,
            format=format,
        )

    async def find(
        self,
        text: str | None = None,
        *,
        role: str | None = None,
        identifier: str | None = None,
        locator: Mapping[str, Any] | None = None,
        app: str | None = None,
        scroll: bool | None = None,
        limit: int | None = None,
        container: str | None = None,
    ) -> FindResult[AsyncElementHandle]:
        """Elements matching ``text``/``role``/``identifier``, best first; ``scroll=True`` scrolls a
        long list until a match appears. ``find(...).click()`` clicks the best match.

        ``locator`` (a locator object from the ``locate`` tool) matches exactly instead, in the
        app and window it names; ``find(locator=...).click()`` then refuses to guess: it raises
        ``ELEMENT_NOT_FOUND`` when nothing matches and ``LOCATOR_AMBIGUOUS`` when several do."""
        result = await self._typed(
            FindResult,
            "find",
            text=text,
            role=role,
            identifier=identifier,
            locator=dict(locator) if locator is not None else None,
            app=app,
            scroll=scroll,
            limit=limit,
            container=container,
        )
        result.handles = self._handle_factory
        return result

    async def explore(self, app: str | None = None) -> ToolResult:
        return await self.call("explore", app=app)

    async def read(
        self,
        app: str | None = None,
        target: str | None = None,
        *,
        mode: str | None = None,
        max_pages: int | None = None,
        restore: bool | None = None,
    ) -> ReadResult:
        """The whole content of a list, table, document or page (``target``: ``"auto"`` or a ref)."""
        return await self._typed(
            ReadResult, "read", app=app, target=target, mode=mode, max_pages=max_pages, restore=restore
        )

    async def inspect(self, ref: str, *, snapshot_id: str | None = None) -> ToolResult:
        return await self.call("inspect", ref=_qualified(ref, snapshot_id))

    async def ocr(self, app: str | None = None, *, window_id: int | None = None) -> ToolResult:
        return await self.call("ocr", app=app, window_id=window_id)

    async def list_apps(self, include_installed: bool = False) -> list[dict[str, Any]]:
        return list((await self.call("list_apps", include_installed=include_installed)).get("apps", []))

    async def list_windows(self, app: str | None = None) -> list[dict[str, Any]]:
        return list((await self.call("list_windows", app=app)).get("windows", []))

    async def list_displays(self) -> list[dict[str, Any]]:
        return list((await self.call("list_displays")).get("displays", []))

    async def wait_for(
        self,
        condition: str,
        query: str | None = None,
        *,
        role: str | None = None,
        app: str | None = None,
        timeout_s: float = 10.0,
    ) -> ToolResult:
        """Wait until ``condition`` (element, element_gone, window, app_running, text_visible, idle,
        duration) holds, up to ``timeout_s`` seconds."""
        return await self.call(
            "wait_for",
            condition=condition,
            query=query,
            role=role,
            app=app,
            timeout_ms=round(timeout_s * 1000),
        )

    # ------------------------------------------------------------------ input
    async def click(
        self,
        ref: str | None = None,
        *,
        x: float | None = None,
        y: float | None = None,
        snapshot_id: str | None = None,
        button: str | None = None,
        count: int | None = None,
        modifiers: Sequence[str] | None = None,
        coordinate_space: str | None = None,
        screenshot_id: str | None = None,
        strategy: str | None = None,
    ) -> ActionResult:
        return await self._typed(
            ActionResult,
            "click",
            ref=ref,
            x=x,
            y=y,
            snapshot_id=snapshot_id,
            button=button,
            count=count,
            modifiers=list(modifiers) if modifiers is not None else None,
            coordinate_space=coordinate_space,
            screenshot_id=screenshot_id,
            strategy=strategy,
        )

    async def type_text(
        self,
        text: str,
        ref: str | None = None,
        *,
        snapshot_id: str | None = None,
        clear: bool | None = None,
        submit: bool | None = None,
        strategy: str | None = None,
    ) -> ActionResult:
        return await self._typed(
            ActionResult,
            "type_text",
            text=text,
            ref=_qualified(ref, snapshot_id),
            clear=clear,
            submit=submit,
            strategy=strategy,
        )

    async def press_keys(
        self, keys: str | Sequence[str], *, repeat: int | None = None, app: str | None = None
    ) -> ActionResult:
        """Press a combo (``"cmd+n"``) or a sequence (``"cmd+a delete"`` or ``["cmd+a", "delete"]``)."""
        combo = keys if isinstance(keys, str) else " ".join(keys)
        return await self._typed(ActionResult, "press_keys", keys=combo, repeat=repeat, app=app)

    async def scroll(
        self,
        ref: str | None = None,
        *,
        x: float | None = None,
        y: float | None = None,
        direction: str | None = None,
        amount: float | None = None,
        unit: str | None = None,
        to: str | None = None,
        app: str | None = None,
        snapshot_id: str | None = None,
    ) -> ActionResult:
        return await self._typed(
            ActionResult,
            "scroll",
            ref=ref,
            x=x,
            y=y,
            direction=direction,
            amount=amount,
            unit=unit,
            to=to,
            app=app,
            snapshot_id=snapshot_id,
        )

    async def scroll_into_view(self, ref: str, *, snapshot_id: str | None = None) -> ActionResult:
        return await self._typed(ActionResult, "scroll", scroll_into_view=_qualified(ref, snapshot_id))

    async def drag(
        self,
        from_ref: str | None = None,
        to_ref: str | None = None,
        *,
        from_point: Sequence[float] | None = None,
        to_point: Sequence[float] | None = None,
        duration_ms: int | None = None,
        modifiers: Sequence[str] | None = None,
    ) -> ActionResult:
        start = {"from_x": from_point[0], "from_y": from_point[1]} if from_point is not None else {}
        end = {"to_x": to_point[0], "to_y": to_point[1]} if to_point is not None else {}
        return await self._typed(
            ActionResult,
            "drag",
            from_ref=from_ref,
            to_ref=to_ref,
            **start,
            **end,
            duration_ms=duration_ms,
            modifiers=list(modifiers) if modifiers is not None else None,
        )

    async def move_mouse(
        self, ref: str | None = None, *, x: float | None = None, y: float | None = None
    ) -> ActionResult:
        return await self._typed(ActionResult, "move_mouse", ref=ref, x=x, y=y)

    async def set_value(
        self, ref: str, value: str | bool | float, *, snapshot_id: str | None = None
    ) -> ActionResult:
        return await self._typed(
            ActionResult, "set_value", ref=_qualified(ref, snapshot_id), value=_plain(value)
        )

    async def perform_action(self, ref: str, action: str, *, snapshot_id: str | None = None) -> ActionResult:
        return await self._typed(
            ActionResult, "perform_action", ref=_qualified(ref, snapshot_id), action=action
        )

    async def select_option(
        self,
        ref: str,
        option: str | None = None,
        *,
        index: int | None = None,
        snapshot_id: str | None = None,
    ) -> ActionResult:
        """Choose an option of a pop-up, combo box, radio/tab group or list by title or 1-based index."""
        return await self._typed(
            ActionResult, "select_option", ref=ref, option=option, index=index, snapshot_id=snapshot_id
        )

    async def handle_dialog(
        self,
        action: str = "inspect",
        *,
        app: str | None = None,
        button: str | None = None,
        text: str | None = None,
    ) -> ActionResult:
        """Read (``inspect``) or answer (``accept``, ``cancel``, ``press`` + ``button``) the sheet
        or dialog in front of ``app``; ``text`` fills its first text field."""
        return await self._typed(
            ActionResult, "handle_dialog", action=action, app=app, button=button, text=text
        )

    async def menu(self, *path: str, app: str | None = None) -> ActionResult:
        """Choose a menu-bar item by its path: ``menu("File", "New Note")``."""
        if not path:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "menu() needs a path, e.g. menu('File', 'New').")
        return await self._typed(ActionResult, "select_menu", path=list(path), app=app)

    async def menus(self, app: str | None = None, *, depth: int | None = None) -> ToolResult:
        """The app's menu bar: its menus and items, ``depth`` levels deep."""
        return await self.call("select_menu", app=app, depth=depth)

    async def fill_form(
        self, fields: Mapping[str, str | bool | float], *, snapshot_id: str | None = None
    ) -> ActionResult:
        """Set several fields at once: ``{"e4": "Ada", "e7": True}``."""
        values = [{"ref": ref, "value": _plain(value)} for ref, value in fields.items()]
        return await self._typed(ActionResult, "fill_form", fields=values, snapshot_id=snapshot_id)

    # ------------------------------------------------------------------ apps and system
    def app(self, name: str) -> AsyncAppHandle:
        """A handle on an app (nothing happens until you call one of its methods)."""
        return self._app_factory(name)

    async def app_action(self, action: str, name: str) -> ToolResult:
        """launch | activate | hide | unhide | quit | force_quit ``name``."""
        return await self.call("app", action=action, name=name)

    async def window(self, action: str, **options: Any) -> ToolResult:
        return await self.call("window", action=action, **options)

    async def open_target(
        self, target: str, *, with_app: str | None = None, reveal: bool | None = None
    ) -> ToolResult:
        """Open a URL, file or folder (the ``open`` tool)."""
        return await self.call("open", target=target, with_app=with_app, reveal=reveal)

    async def get_clipboard(self) -> str | None:
        return (await self.call("clipboard", action="get")).get("text")

    async def set_clipboard(self, text: str) -> None:
        await self.call("clipboard", action="set", text=text)

    async def notify(self, title: str, message: str, *, subtitle: str | None = None) -> None:
        await self.call("notify", title=title, message=message, subtitle=subtitle)

    async def shell(
        self, command: str, *, cwd: str | None = None, timeout_s: float | None = None
    ) -> ToolResult:
        return await self.call("shell", command=command, cwd=cwd, timeout_s=timeout_s)

    async def applescript(
        self, script: str, *, language: str | None = None, timeout_s: float | None = None
    ) -> str:
        result = await self.call("applescript", script=script, language=language, timeout_s=timeout_s)
        return str(result.get("result", ""))

    async def browser_read(
        self, *, browser: str | None = None, include_links: bool | None = None
    ) -> ToolResult:
        return await self.call("browser_read", browser=browser, include_links=include_links)


def _qualified(ref: str | None, snapshot_id: str | None) -> str | None:
    """``"e4"`` with snapshot ``s_7f3a`` → ``"s_7f3a:e4"`` (refs that name a snapshot already stay)."""
    if ref is None or snapshot_id is None or ":" in ref:
        return ref
    return f"{snapshot_id}:{ref}"


def _error_of(result: CallToolResult) -> AXPilotError:
    meta = (result.meta or {}).get(_ERROR_META_KEY)
    if isinstance(meta, Mapping):
        return AXPilotError.from_dict(meta)
    text = " ".join(block.text for block in result.content if isinstance(block, TextContent))
    return AXPilotError(ErrorCode.INTERNAL, text or "The tool failed without details.")


def _parse(kind: type[Any], tool: str, result: CallToolResult) -> Any:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    images = [base64.b64decode(block.data) for block in result.content if isinstance(block, ImageContent)]
    return kind(tool=tool, data=dict(result.structured_content or {}), text="\n".join(texts), images=images)


# =========================================================================== the sync twin
def _sync(
    method: Callable[Concatenate[AsyncDesktop, P], Awaitable[R]],
) -> Callable[Concatenate[Desktop, P], R]:
    """A blocking :class:`Desktop` method running ``method`` on the desktop's event loop."""

    @functools.wraps(method)
    def blocking(self: Desktop, *args: P.args, **kwargs: P.kwargs) -> R:
        return self._run(method, *args, **kwargs)

    return blocking


class Desktop:
    """Operate the Mac (or the simulated desktop) from ordinary, blocking code. Every method of
    :class:`AsyncDesktop` is available and blocks until the tool finishes; methods may be called
    from several threads."""

    def __init__(self, desktop: AsyncDesktop) -> None:
        """Prefer the :meth:`direct`, :meth:`fake` and :meth:`connect` constructors."""
        self._async = desktop
        desktop._handle_factory = lambda ref, sid, el: ElementHandle(self, ref, sid, el)
        desktop._app_factory = lambda name: AppHandle(self, name)
        self._portal_cm: Any = None
        self._session_cm: Any = None
        self._portal: anyio.from_thread.BlockingPortal | None = None
        self._lock = threading.Lock()

    @classmethod
    def direct(
        cls, config: Any = None, engine: Any = None, *, profile: str | None = None, confirm: Any = None
    ) -> Desktop:
        """See :meth:`AsyncDesktop.direct`."""
        return cls(AsyncDesktop.direct(config, engine, profile=profile, confirm=confirm))

    @classmethod
    def fake(
        cls,
        fixture: str | Path | None = None,
        *,
        config: Mapping[str, Any] | None = None,
        profile: str | None = "full",
        confirm: Any = True,
    ) -> Desktop:
        """See :meth:`AsyncDesktop.fake`."""
        return cls(AsyncDesktop.fake(fixture, config=config, profile=profile, confirm=confirm))

    @classmethod
    def connect(cls, socket_path: str | Path | None = None, *, profile: str | None = None) -> Desktop:
        """See :meth:`AsyncDesktop.connect`."""
        return cls(AsyncDesktop.connect(socket_path, profile=profile))

    @property
    def context(self) -> Any:
        return self._async.context

    def __enter__(self) -> Desktop:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self) -> None:
        """Start the event loop thread and connect (done automatically by the first call)."""
        with self._lock:
            if self._portal is not None:
                return
            portal_cm = anyio.from_thread.start_blocking_portal()
            portal = portal_cm.__enter__()
            # The MCP session lives in one task of the portal for its whole life (its task group
            # must be entered and exited by the same task); calls run in their own tasks.
            session_cm = portal.wrap_async_context_manager(self._async)
            try:
                session_cm.__enter__()
            except BaseException:
                portal_cm.__exit__(*sys.exc_info())
                raise
            self._portal_cm, self._session_cm, self._portal = portal_cm, session_cm, portal

    def close(self) -> None:
        """Disconnect, release the engine and stop the thread. Safe to call more than once."""
        with self._lock:
            portal_cm, session_cm = self._portal_cm, self._session_cm
            self._portal = self._portal_cm = self._session_cm = None
        if portal_cm is None:
            return
        try:
            session_cm.__exit__(None, None, None)
        finally:
            portal_cm.__exit__(None, None, None)

    def _run(self, method: Callable[..., Awaitable[R]], *args: Any, **kwargs: Any) -> R:
        if self._portal is None:
            self.open()
        portal = self._portal
        if portal is None:  # pragma: no cover - closed by another thread meanwhile
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "This Desktop is closed.")
        future = portal.start_task_soon(functools.partial(method, self._async, *args, **kwargs))
        # The caller (often the main thread) blocks here; on macOS its run loop keeps turning.
        return wait_pumping(future, main_thread_pump())

    def app(self, name: str) -> AppHandle:
        """A handle on an app (nothing happens until you call one of its methods)."""
        return AppHandle(self, name)

    # Handle-returning methods are spelled out so type checkers see the sync handle types.
    def snapshot(self, app: str | None = None, **options: Any) -> SnapshotResult[ElementHandle]:
        """See :meth:`AsyncDesktop.snapshot`."""
        return cast("SnapshotResult[ElementHandle]", self._run(AsyncDesktop.snapshot, app, **options))

    def find(self, text: str | None = None, **options: Any) -> FindResult[ElementHandle]:
        """See :meth:`AsyncDesktop.find`."""
        return cast("FindResult[ElementHandle]", self._run(AsyncDesktop.find, text, **options))

    call = _sync(AsyncDesktop.call)
    status = _sync(AsyncDesktop.status)
    screenshot = _sync(AsyncDesktop.screenshot)
    explore = _sync(AsyncDesktop.explore)
    read = _sync(AsyncDesktop.read)
    inspect = _sync(AsyncDesktop.inspect)
    ocr = _sync(AsyncDesktop.ocr)
    list_apps = _sync(AsyncDesktop.list_apps)
    list_windows = _sync(AsyncDesktop.list_windows)
    list_displays = _sync(AsyncDesktop.list_displays)
    wait_for = _sync(AsyncDesktop.wait_for)
    click = _sync(AsyncDesktop.click)
    type_text = _sync(AsyncDesktop.type_text)
    press_keys = _sync(AsyncDesktop.press_keys)
    scroll = _sync(AsyncDesktop.scroll)
    scroll_into_view = _sync(AsyncDesktop.scroll_into_view)
    drag = _sync(AsyncDesktop.drag)
    move_mouse = _sync(AsyncDesktop.move_mouse)
    set_value = _sync(AsyncDesktop.set_value)
    perform_action = _sync(AsyncDesktop.perform_action)
    select_option = _sync(AsyncDesktop.select_option)
    handle_dialog = _sync(AsyncDesktop.handle_dialog)
    menu = _sync(AsyncDesktop.menu)
    menus = _sync(AsyncDesktop.menus)
    fill_form = _sync(AsyncDesktop.fill_form)
    app_action = _sync(AsyncDesktop.app_action)
    window = _sync(AsyncDesktop.window)
    open_target = _sync(AsyncDesktop.open_target)
    get_clipboard = _sync(AsyncDesktop.get_clipboard)
    set_clipboard = _sync(AsyncDesktop.set_clipboard)
    notify = _sync(AsyncDesktop.notify)
    shell = _sync(AsyncDesktop.shell)
    applescript = _sync(AsyncDesktop.applescript)
    browser_read = _sync(AsyncDesktop.browser_read)
