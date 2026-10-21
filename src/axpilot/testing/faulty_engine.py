"""Fault injection: wrap any engine and make chosen methods fail.

:class:`FaultyEngine` delegates every :class:`~axpilot.engine.base.Engine` method to the
engine it wraps, except the methods that have a :class:`Fault`::

    from axpilot.engine.fake import FakeEngine
    from axpilot.errors import ErrorCode
    from axpilot.testing import Fault, FaultyEngine

    engine = FaultyEngine(
        FakeEngine.demo(),
        {
            "capture_tree": Fault.error(ErrorCode.PERMISSION_ACCESSIBILITY),
            "screenshot": Fault.crash("simulated bug"),   # a raw RuntimeError
            "mouse_click": Fault.hang(5.0),               # sleeps past the tool's deadline
            "list_apps": Fault.flaky(times=2),            # the first 2 calls fail, then it works
        },
    )

Use it to prove that a tool (or your automation script) reports the right error code, never
crashes, releases the input lock and leaks nothing. Hung calls block only their worker thread;
call :meth:`FaultyEngine.release` (or :meth:`FaultyEngine.close`) at the end of a test so those
threads finish.
"""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio

from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import (
    AppAction,
    AppInfo,
    Capture,
    ClipboardContent,
    DisplayInfo,
    ImageFormat,
    KeyCombo,
    MenuNode,
    MouseButton,
    OcrLine,
    PermissionStatus,
    Point,
    RawNode,
    RawTree,
    Rect,
    WindowAction,
    WindowInfo,
)

__all__ = ["ENGINE_METHODS", "Fault", "FaultKind", "FaultyEngine"]

FaultKind = Literal["error", "crash", "hang", "flaky"]

ENGINE_METHODS: frozenset[str] = frozenset(
    {
        "permissions",
        "os_version",
        "list_apps",
        "frontmost_app",
        "find_app",
        "list_windows",
        "list_displays",
        "app_at_point",
        "capture_tree",
        "element_valid",
        "refresh_element",
        "element_attributes",
        "perform_action",
        "set_value",
        "focus",
        "insert_text",
        "scroll_to_visible",
        "mouse_move",
        "mouse_click",
        "mouse_drag",
        "scroll",
        "press_key",
        "type_text",
        "cursor_position",
        "hid_idle_seconds",
        "secure_input_active",
        "app_control",
        "window_control",
        "open_target",
        "menu_bar",
        "menu_select",
        "screenshot",
        "ocr",
        "clipboard_get",
        "clipboard_set",
        "clipboard_save",
        "element_text",
        "clipboard_restore",
        "notify",
    }
)
"""Engine methods that can carry a fault."""


@dataclass(frozen=True, slots=True)
class Fault:
    """What a faulty method does instead of (or before) its real work. Build one with the
    class methods: :meth:`error`, :meth:`crash`, :meth:`hang`, :meth:`flaky`."""

    kind: FaultKind
    code: ErrorCode = ErrorCode.INTERNAL
    message: str = "injected fault"
    hint: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    exception: type[BaseException] = RuntimeError
    seconds: float = 30.0
    times: int = 1
    failure: Fault | None = None

    @classmethod
    def error(
        cls,
        code: ErrorCode,
        message: str = "injected fault",
        *,
        hint: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Fault:
        """Raise ``AXPilotError(code, message)`` (a fresh instance on every call)."""
        return cls(kind="error", code=code, message=message, hint=hint, details=dict(details or {}))

    @classmethod
    def crash(cls, message: str = "injected crash", exception: type[BaseException] = RuntimeError) -> Fault:
        """Raise a raw exception (default ``RuntimeError``): what an engine bug looks like."""
        return cls(kind="crash", message=message, exception=exception)

    @classmethod
    def hang(cls, seconds: float = 30.0) -> Fault:
        """Block the calling thread for ``seconds`` (or until :meth:`FaultyEngine.release`),
        then run the real method: a call that runs past every deadline."""
        if seconds < 0:
            raise ValueError("seconds must be 0 or more")
        return cls(kind="hang", seconds=seconds)

    @classmethod
    def flaky(cls, times: int = 1, failure: Fault | None = None) -> Fault:
        """Fail the first ``times`` calls with ``failure`` (default: :meth:`crash`), then work."""
        if times < 1:
            raise ValueError("times must be at least 1")
        if failure is not None and failure.kind in ("flaky", "hang"):
            raise ValueError("a flaky fault fails with an error or a crash")
        return cls(kind="flaky", times=times, failure=failure or cls.crash("transient failure"))

    def make_exception(self) -> BaseException:
        """The exception this fault raises (for ``error``, ``crash`` and ``flaky``)."""
        if self.kind == "flaky":
            assert self.failure is not None
            return self.failure.make_exception()
        if self.kind == "error":
            return AXPilotError(self.code, self.message, hint=self.hint, details=dict(self.details))
        return self.exception(self.message)


class FaultyEngine(Engine):
    """An engine that delegates to ``engine`` and injects ``faults`` (method name → :class:`Fault`).

    :attr:`calls` counts every call per method (faulted or not). Attributes the wrapper does
    not define are read from the wrapped engine (``faulty.events`` of a ``FakeEngine``); use
    :attr:`inner` to change them. Thread-safe.
    """

    def __init__(self, engine: Engine, faults: Mapping[str, Fault] | None = None) -> None:
        self.inner = engine
        self.name = f"faulty({engine.name})"
        self.calls: collections.Counter[str] = collections.Counter()
        self._faults: dict[str, Fault] = {}
        self._failures: collections.Counter[str] = collections.Counter()
        self._lock = threading.Lock()
        self._release = threading.Event()
        self._hanging = 0
        for method, fault in (faults or {}).items():
            self.set_fault(method, fault)

    def __getattr__(self, name: str) -> Any:
        if name == "inner":  # not set yet (during unpickling/copying)
            raise AttributeError(name)
        return getattr(self.inner, name)

    # ------------------------------------------------------------------ fault control
    def set_fault(self, method: str, fault: Fault | None) -> None:
        """Install (or with ``None`` remove) the fault of ``method``. Raises ``ValueError`` for
        a name that is not an engine method."""
        if method not in ENGINE_METHODS:
            raise ValueError(f"{method!r} is not an Engine method; choose from {sorted(ENGINE_METHODS)}")
        with self._lock:
            if fault is None:
                self._faults.pop(method, None)
            else:
                self._faults[method] = fault
            self._failures[method] = 0

    def clear_faults(self) -> None:
        """Remove every fault (and release hung calls)."""
        with self._lock:
            self._faults.clear()
            self._failures.clear()
        self.release()

    @property
    def hanging(self) -> int:
        """Number of calls currently blocked by a ``hang`` fault."""
        with self._lock:
            return self._hanging

    def release(self) -> None:
        """Let every currently hung call continue (later hangs block again)."""
        with self._lock:
            event, self._release = self._release, threading.Event()
        event.set()

    async def wait_hanging(self, count: int = 1, timeout_s: float = 5.0) -> None:
        """Wait (asynchronously) until ``count`` calls are hung. Raises ``TimeoutError``."""
        with anyio.fail_after(timeout_s):
            while self.hanging < count:  # noqa: ASYNC110 - the counter changes in worker threads
                await anyio.sleep(0.005)

    def wait_hanging_sync(self, count: int = 1, timeout_s: float = 5.0) -> None:
        """Blocking variant of :meth:`wait_hanging` for synchronous tests."""
        deadline = time.monotonic() + timeout_s
        while self.hanging < count:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{count} hung call(s) expected, {self.hanging} found")
            time.sleep(0.005)

    # ------------------------------------------------------------------ dispatch
    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self.calls[method] += 1
            fault = self._faults.get(method)
            failing = fault is not None and (fault.kind != "flaky" or self._failures[method] < fault.times)
            if failing and fault is not None and fault.kind == "flaky":
                self._failures[method] += 1
            release = self._release
        if fault is not None and failing:
            if fault.kind == "hang":
                self._hang(release, fault.seconds)
            else:
                raise fault.make_exception()
        return getattr(self.inner, method)(*args, **kwargs)

    def _hang(self, release: threading.Event, seconds: float) -> None:
        with self._lock:
            self._hanging += 1
        try:
            release.wait(seconds)
        finally:
            with self._lock:
                self._hanging -= 1

    # ------------------------------------------------------------------ environment
    def permissions(self) -> PermissionStatus:
        return self._call("permissions")

    def os_version(self) -> str:
        return self._call("os_version")

    # ------------------------------------------------------------------ apps, windows, displays
    def list_apps(self, include_installed: bool = False) -> list[AppInfo]:
        return self._call("list_apps", include_installed)

    def frontmost_app(self) -> AppInfo | None:
        return self._call("frontmost_app")

    def find_app(self, query: str) -> AppInfo:
        return self._call("find_app", query)

    def list_windows(self, app: str | None = None) -> list[WindowInfo]:
        return self._call("list_windows", app)

    def list_displays(self) -> list[DisplayInfo]:
        return self._call("list_displays")

    def app_at_point(self, point: Point) -> AppInfo | None:
        return self._call("app_at_point", point)

    # ------------------------------------------------------------------ accessibility tree
    def capture_tree(
        self,
        *,
        app: str | None = None,
        window_id: int | None = None,
        include_menus: bool = False,
        max_depth: int = 40,
        max_nodes: int = 4000,
        timeout_s: float = 5.0,
        enhance: bool = True,
    ) -> RawTree:
        return self._call(
            "capture_tree",
            app=app,
            window_id=window_id,
            include_menus=include_menus,
            max_depth=max_depth,
            max_nodes=max_nodes,
            timeout_s=timeout_s,
            enhance=enhance,
        )

    def element_valid(self, handle: Any) -> bool:
        return self._call("element_valid", handle)

    def refresh_element(self, handle: Any) -> RawNode:
        return self._call("refresh_element", handle)

    def element_attributes(self, handle: Any) -> dict[str, Any]:
        return self._call("element_attributes", handle)

    # ------------------------------------------------------------------ AX actions
    def perform_action(self, handle: Any, action: str) -> None:
        return self._call("perform_action", handle, action)

    def set_value(self, handle: Any, value: str | float | bool) -> None:
        return self._call("set_value", handle, value)

    def focus(self, handle: Any) -> None:
        return self._call("focus", handle)

    def insert_text(self, handle: Any, text: str) -> bool:
        return self._call("insert_text", handle, text)

    def scroll_to_visible(self, handle: Any) -> bool:
        return self._call("scroll_to_visible", handle)

    # ------------------------------------------------------------------ synthetic input
    def mouse_move(self, point: Point, duration_s: float = 0.0) -> None:
        return self._call("mouse_move", point, duration_s)

    def mouse_click(
        self,
        point: Point,
        button: MouseButton = "left",
        count: int = 1,
        modifiers: frozenset[str] = frozenset(),
    ) -> None:
        return self._call("mouse_click", point, button, count, modifiers)

    def mouse_drag(
        self,
        start: Point,
        end: Point,
        duration_s: float = 0.3,
        modifiers: frozenset[str] = frozenset(),
        button: MouseButton = "left",
    ) -> None:
        return self._call("mouse_drag", start, end, duration_s, modifiers, button)

    def scroll(self, point: Point | None, dx: float, dy: float) -> None:
        return self._call("scroll", point, dx, dy)

    def press_key(self, combo: KeyCombo) -> None:
        return self._call("press_key", combo)

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        return self._call("type_text", text, interval_s)

    def cursor_position(self) -> Point:
        return self._call("cursor_position")

    def hid_idle_seconds(self) -> float:
        return self._call("hid_idle_seconds")

    def secure_input_active(self) -> bool:
        return self._call("secure_input_active")

    # ------------------------------------------------------------------ app & window control
    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        return self._call("app_control", action, app)

    def window_control(
        self, window_id: int, action: WindowAction, rect: Rect | None = None
    ) -> WindowInfo | None:
        return self._call("window_control", window_id, action, rect)

    def open_target(self, target: str, with_app: str | None = None, reveal: bool = False) -> None:
        return self._call("open_target", target, with_app, reveal)

    # ------------------------------------------------------------------ menus
    def menu_bar(self, app: str | None = None, depth: int = 2, *, extras: bool = False) -> MenuNode:
        return self._call("menu_bar", app, depth, extras=extras)

    def menu_select(self, app: str | None, path: Sequence[str], *, extras: bool = False) -> list[str]:
        return self._call("menu_select", app, path, extras=extras)

    # ------------------------------------------------------------------ capture & vision
    def screenshot(
        self,
        *,
        display_id: int | None = None,
        window_id: int | None = None,
        rect: Rect | None = None,
        max_edge: int = 1280,
        fmt: ImageFormat = "jpeg",
        quality: int = 80,
        exclude_bundle_ids: Sequence[str] = (),
    ) -> Capture:
        return self._call(
            "screenshot",
            display_id=display_id,
            window_id=window_id,
            rect=rect,
            max_edge=max_edge,
            fmt=fmt,
            quality=quality,
            exclude_bundle_ids=exclude_bundle_ids,
        )

    def ocr(self, capture: Capture, languages: Sequence[str] | None = None) -> list[OcrLine]:
        return self._call("ocr", capture, languages)

    # ------------------------------------------------------------------ clipboard & notifications
    def clipboard_get(self) -> ClipboardContent:
        return self._call("clipboard_get")

    def clipboard_set(
        self,
        *,
        text: str | None = None,
        image_png: bytes | None = None,
        files: Sequence[str] | None = None,
    ) -> None:
        return self._call("clipboard_set", text=text, image_png=image_png, files=files)

    def element_text(self, handle: Any, max_chars: int) -> str | None:
        return self._call("element_text", handle, max_chars)

    def clipboard_save(self) -> Any:
        return self._call("clipboard_save")

    def clipboard_restore(self, saved: Any) -> None:
        return self._call("clipboard_restore", saved)

    def notify(self, title: str, message: str, subtitle: str | None = None) -> None:
        return self._call("notify", title, message, subtitle)

    # ------------------------------------------------------------------ cleanup
    def close(self) -> None:
        """Release hung calls and close the wrapped engine."""
        self.release()
        self.inner.close()
