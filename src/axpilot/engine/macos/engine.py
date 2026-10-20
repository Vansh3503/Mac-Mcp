"""``MacEngine``: the real macOS engine, built on PyObjC.

Permissions each method needs (granted to the *responsible app*, see
:mod:`~axpilot.engine.macos.permissions`):

=====================================================  ===================================
method                                                 permission
=====================================================  ===================================
``permissions``, ``os_version``, ``list_displays``,    none
``cursor_position``, ``hid_idle_seconds``,
``secure_input_active``, ``clipboard_*``, ``ocr``
``list_apps``, ``frontmost_app``, ``app_at_point``,    none (Accessibility improves
``app_control``, ``open_target``, ``list_windows``     frontmost detection, window titles
                                                       and states)
``capture_tree``, ``element_*``, ``refresh_element``,  Accessibility
``perform_action``, ``set_value``, ``focus``,
``insert_text``, ``scroll_to_visible``,
``window_control``, ``menu_bar``, ``menu_select``
``mouse_*``, ``scroll``, ``press_key``, ``type_text``  Accessibility (to post events)
``screenshot``                                         Screen Recording
``notify``                                             none (shown as Script Editor)
=====================================================  ===================================

Threading: every method is blocking and safe to call from worker threads (the server runs
them through ``anyio.to_thread``). Accessibility, Quartz events, CGWindowList,
ScreenCaptureKit and Vision are thread-safe. AppKit objects used here (``NSWorkspace``,
``NSRunningApplication``, ``NSScreen``, ``NSPasteboard``, ``NSBitmapImageRep``) are read from
worker threads, which AppKit supports for these classes, but they are *refreshed* by the main
run loop, which the AXPilot host runs. The only main-thread-only API, the keyboard layout
(TIS), is marshalled to the main thread (see :mod:`~axpilot.engine.macos.carbon`).

Errors: every public method raises :class:`~axpilot.errors.AXPilotError` only. AX failures are
mapped by :func:`~axpilot.engine.macos.ax.ax_failure`; anything unexpected from the PyObjC
bridge becomes ``INTERNAL`` (logged with its traceback). Following the base contract, the
engine does not apply safety policy, but it refuses to automate system authentication
prompts (``NEEDS_USER``).
"""

from __future__ import annotations

import functools
import logging
import math
import os
import platform
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import AppKit
import Foundation
import Quartz as Q

from axpilot.engine.base import Engine
from axpilot.engine.macos import actions as actions_
from axpilot.engine.macos import attributes as A
from axpilot.engine.macos import capture as capture_
from axpilot.engine.macos import carbon, vision
from axpilot.engine.macos import clipboard as clipboard_
from axpilot.engine.macos import helpers as h
from axpilot.engine.macos import notify as notify_
from axpilot.engine.macos import permissions as perms
from axpilot.engine.macos.apps import LAUNCH_TIMEOUT_S, AppService, ElectronEnhancer
from axpilot.engine.macos.ax import AX, AXCallError, AXElement, ax_failure
from axpilot.engine.macos.displays import DisplayCache
from axpilot.engine.macos.input import EventPoster, Mouse, cursor_position
from axpilot.engine.macos.keyboard import Keyboard
from axpilot.engine.macos.menus import MenuService
from axpilot.engine.macos.sync import call_with_completion, ns_error_code, ns_error_text
from axpilot.engine.macos.tree import Budget, NodeReader, capture_subtree
from axpilot.engine.macos.windows import WindowService, cg_window, cg_windows
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import modifier_mask
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
from axpilot.system.runner import CommandRunner

_log = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

TRUST_CACHE_S = 1.0
"""A positive ``AXIsProcessTrusted`` answer is reused this long."""
LS_APPLICATION_NOT_FOUND = -10814
"""``kLSApplicationNotFoundErr``: no app can open the target."""
_UNSET = object()


def _guarded(what: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Turn every non-:class:`AXPilotError` failure of a method into one (QUALITY A1)."""

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return func(*args, **kwargs)
            except AXPilotError:
                raise
            except AXCallError as exc:
                raise ax_failure(exc, what) from exc
            except Exception as exc:  # PyObjC raises objc.error, TypeError, ValueError…
                _log.exception("%s failed", what)
                detail = h.truncate_text(str(exc), 200)
                raise AXPilotError(
                    ErrorCode.INTERNAL,
                    f"{what} failed unexpectedly ({type(exc).__name__}: {detail}).",
                ) from exc

        return wrapper

    return decorate


class MacEngine(Engine):
    """Drives macOS through Accessibility, Quartz events, ScreenCaptureKit, Vision and AppKit.

    ``ax_timeout_s`` is the Accessibility messaging timeout (per call), ``enhance_electron_chrome``
    switches on the full accessibility tree of Chromium/Electron apps, ``max_value_chars``
    caps how much of an element's value is read. ``runner`` runs ``osascript`` and ``ps``
    (tests pass a fake one).
    """

    name = "macos"

    def __init__(
        self,
        ax_timeout_s: float = 1.0,
        enhance_electron_chrome: bool = True,
        max_value_chars: int = 2000,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        if not (isinstance(ax_timeout_s, int | float) and math.isfinite(ax_timeout_s) and ax_timeout_s > 0):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"ax_timeout_s must be > 0, got {ax_timeout_s!r}.")
        if max_value_chars < 1:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"max_value_chars must be ≥ 1, got {max_value_chars}."
            )
        self.max_value_chars = int(max_value_chars)
        self._runner = runner or CommandRunner()
        self._ax = AX(float(ax_timeout_s))
        self._reader = NodeReader(self._ax, self.max_value_chars)
        self._displays = DisplayCache()
        self._apps = AppService(self._ax, self._runner)
        self._enhancer = ElectronEnhancer(self._ax, enabled=enhance_electron_chrome)
        self._windows = WindowService(self._ax, self._apps, self._displays, self._enhancer)
        self._poster = EventPoster()
        self._mouse = Mouse(self._poster)
        self._keyboard = Keyboard(self._poster, carbon.LayoutProvider())
        self._menus = MenuService(self._ax, self._press_escape)
        self._lock = threading.Lock()
        self._responsible_lock = threading.Lock()
        self._trusted_until = 0.0
        self._asked: set[str] = set()
        self._ask_lock = threading.Lock()
        self._responsible: Any = _UNSET
        self._auth_pids: dict[int, bool] = {}

    # ================================================================== internals
    def _require_ax(self) -> None:
        now = time.monotonic()
        if now < self._trusted_until:
            return
        if not self._ax.is_trusted():
            who = self._responsible_app() or "the app that runs AXPilot"
            self._ask_once("accessibility", perms.request_accessibility)
            raise AXPilotError(
                ErrorCode.PERMISSION_ACCESSIBILITY,
                f"Accessibility permission is not granted to {who}.",
            )
        self._trusted_until = now + TRUST_CACHE_S

    def _ask_once(self, permission: str, request: Callable[[], bool]) -> None:
        """Show macOS's own prompt for ``permission`` the first time a tool finds it missing: it
        also lists AXPilot in System Settings, ready to be switched on. Without it a new user had
        to add AXPilot.app there by hand (macOS 26). Once per process; macOS itself asks only once."""
        with self._ask_lock:
            if permission in self._asked:
                return
            self._asked.add(permission)
        try:
            request()
        except Exception as exc:  # a missing prompt must not hide the real error (A8)
            _log.warning("could not ask macOS for the %s permission: %s", permission, exc)

    def _responsible_app(self) -> str | None:
        with self._responsible_lock:
            if self._responsible is _UNSET:
                try:
                    self._responsible = perms.responsible_app(self._runner)
                except AXPilotError as exc:
                    _log.debug("cannot determine the responsible app: %s", exc)
                    self._responsible = None
            return self._responsible

    def _extra_pids(self) -> frozenset[int]:
        """Pids owning on-screen windows (catches apps a stale workspace list misses)."""
        own = os.getpid()
        return frozenset(w.pid for w in cg_windows(on_screen_only=True) if w.is_user_window and w.pid != own)

    def _target_app(self, app: str | None) -> AppInfo:
        if app is not None:
            return self.find_app(app)
        front = self._apps.frontmost()
        if front is None:
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, "No app is frontmost.")
        return front

    def _is_auth_pid(self, pid: int | None) -> bool:
        if not pid:
            return False
        with self._lock:
            known = self._auth_pids.get(pid)
        if known is None:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            name = str(app.localizedName()) if app is not None and app.localizedName() else None
            bundle_id = str(app.bundleIdentifier()) if app is not None and app.bundleIdentifier() else None
            known = h.is_auth_agent(name, bundle_id)
            with self._lock:
                if len(self._auth_pids) > 1024:
                    self._auth_pids.clear()
                self._auth_pids[pid] = known
        return known

    def _refuse_auth(self, pid: int | None) -> None:
        if self._is_auth_pid(pid):
            raise AXPilotError(
                ErrorCode.NEEDS_USER,
                "This is a system authentication prompt; AXPilot never operates those.",
            )

    def _refuse_auth_element(self, handle: Any) -> None:
        if self._ax.is_element(handle):
            self._refuse_auth(self._ax.pid(handle))

    def _require_keyboard(self) -> None:
        if self.secure_input_active():
            raise AXPilotError(
                ErrorCode.SECURE_INPUT_ACTIVE,
                "Secure Event Input is on; macOS blocks synthetic keystrokes.",
            )
        self._refuse_auth(self._apps.frontmost_pid())

    def _check_point(self, point: Point) -> None:
        if not (math.isfinite(point.x) and math.isfinite(point.y)):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Invalid point ({point.x}, {point.y}).")
        if not h.point_on_any_display(point, self._displays.get()):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"The point ({point.x:g}, {point.y:g}) is not on any display.",
                hint="Use `list_displays` for the screen bounds (global points, origin at the top-left).",
            )

    def _press_escape(self) -> None:
        self._keyboard.press(KeyCombo("escape"))

    def _focused_element(self, pid: int | None) -> AXElement | None:
        if not pid:
            return None
        try:
            focused = self._ax.get(self._ax.application(pid), A.FOCUSED_UI_ELEMENT)
        except AXCallError as exc:
            if exc.permission:
                raise
            return None
        return focused if self._ax.is_element(focused) else None

    # ================================================================== environment
    @_guarded("Checking permissions")
    def permissions(self) -> PermissionStatus:
        running = {bundle for bundle in perms.AUTOMATION_TARGETS if self._apps.is_running(bundle)}
        return perms.permission_status(
            self._ax, responsible=self._responsible_app(), running_bundle_ids=running
        )

    @_guarded("Reading the macOS version")
    def os_version(self) -> str:
        try:
            version = Foundation.NSProcessInfo.processInfo().operatingSystemVersion()
            major, minor, patch = (
                int(version.majorVersion),
                int(version.minorVersion),
                int(version.patchVersion),
            )
        except (AttributeError, TypeError, ValueError):
            return platform.mac_ver()[0] or "unknown"
        return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"

    # ================================================================== apps, windows, displays
    @_guarded("Listing apps")
    def list_apps(self, include_installed: bool = False) -> list[AppInfo]:
        return self._apps.list_apps(include_installed, extra_pids=self._extra_pids())

    @_guarded("Finding the frontmost app")
    def frontmost_app(self) -> AppInfo | None:
        return self._apps.frontmost()

    @_guarded("Finding the app")
    def find_app(self, query: str) -> AppInfo:
        return self._apps.find(query, extra_pids=self._extra_pids())

    @_guarded("Listing windows")
    def list_windows(self, app: str | None = None) -> list[WindowInfo]:
        target = self.find_app(app) if app is not None else None
        return self._windows.list_windows(target)

    @_guarded("Listing displays")
    def list_displays(self) -> list[DisplayInfo]:
        return self._displays.get(fresh=True)

    @_guarded("Finding the app under the point")
    def app_at_point(self, point: Point) -> AppInfo | None:
        hit = h.hit_test(
            cg_windows(on_screen_only=True), point, own_pid=os.getpid(), displays=self._displays.get()
        )
        if hit is None:
            return None
        if isinstance(hit, str):  # the menu bar belongs to the frontmost app
            return self._apps.frontmost()
        info = self._apps.info_for_pid(hit.pid)
        return info or AppInfo(name=hit.owner or f"pid {hit.pid}", pid=hit.pid)

    # ================================================================== accessibility tree
    def _root_window(self, app_element: AXElement) -> AXElement | None:
        for attribute in (A.FOCUSED_WINDOW, A.MAIN_WINDOW):
            element = self._ax.get(app_element, attribute)
            if element is not None and self._ax.is_element(element):
                return element
        for element in list(self._ax.get(app_element, A.WINDOWS) or ()):
            if self._ax.is_element(element):
                return element
        return None

    @_guarded("Capturing the accessibility tree")
    def capture_tree(
        self,
        *,
        app: str | None = None,
        window_id: int | None = None,
        include_menus: bool = False,
        max_depth: int = 40,
        max_nodes: int = 4000,
        timeout_s: float = 5.0,
    ) -> RawTree:
        self._require_ax()
        if max_depth < 0 or max_nodes < 1 or not timeout_s > 0:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "max_depth must be ≥ 0, max_nodes ≥ 1 and timeout_s > 0."
            )
        started = time.monotonic()
        budget = Budget.create(max_depth, max_nodes, timeout_s)
        window: WindowInfo | None
        dialogs: list[AXElement] = []
        if window_id is not None:
            target, ax_window, cg = self._windows.locate(window_id)
            if app is not None and not target.matches(app):
                raise AXPilotError(
                    ErrorCode.WINDOW_NOT_FOUND, f"Window {window_id} belongs to {target.name}, not {app!r}."
                )
            self._enhancer.ensure(target)
            app_element = self._ax.application(target.pid)
            root_element: AXElement | None = ax_window.element
            focused_window = self._windows.focused_window(target)
            window = self._windows.info(target, ax_window, cg, focused_element=focused_window)
        else:
            target = self._target_app(app)
            self._enhancer.ensure(target)
            app_element = self._ax.application(target.pid)
            try:
                root_element = self._root_window(app_element)
            except AXCallError as exc:
                raise ax_failure(
                    exc, f"Reading the windows of {target.name}", timeout_s=self._ax.timeout_s
                ) from exc
            window = None
            if root_element is not None:
                window, dialogs = self._windows.root_context(target, root_element)
        focused = self._focused_element(target.pid)
        menus = self._open_menus(app_element, target) if root_element is not None else []
        try:
            root, count, truncated = capture_subtree(
                self._reader,
                root_element if root_element is not None else app_element,
                budget=budget,
                focused=focused,
                extra_children=[*dialogs, *menus],
            )
        except AXCallError as exc:
            if exc.stale and window_id is not None:
                raise AXPilotError(
                    ErrorCode.WINDOW_NOT_FOUND, f"Window {window_id} closed while it was read."
                ) from exc
            raise ax_failure(exc, f"Reading the UI of {target.name}", timeout_s=self._ax.timeout_s) from exc
        menu_bar = self._menu_bar_raw(app_element, target) if include_menus else None
        return RawTree(
            app=target,
            window=window,
            root=root,
            menu_bar=menu_bar,
            node_count=count,
            truncated=truncated,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    def _open_menus(self, app_element: AXElement, target: AppInfo) -> list[AXElement]:
        """Menus the app shows outside its windows right now (context menus, and pop-up menus
        that belong to the application): its ``AXMenu`` children. They are added to the captured
        window like attached dialogs, so snapshots list their items. Best effort: a failure is
        logged and the capture goes on without them."""
        try:
            children = self._ax.get(app_element, A.CHILDREN) or []
            return [
                child
                for child in list(children)
                if self._ax.is_element(child) and A.text(self._ax.get(child, A.ROLE)) == A.MENU
            ]
        except AXCallError as exc:
            if exc.permission:
                raise ax_failure(
                    exc, f"Reading the menus of {target.name}", timeout_s=self._ax.timeout_s
                ) from exc
            _log.debug("cannot read the open menus of %s: %s", target.name, exc)
            return []

    def _menu_bar_raw(self, app_element: AXElement, target: AppInfo) -> RawNode | None:
        try:
            return self._menus.raw_menu_bar(app_element, target.name)
        except AXPilotError as exc:
            if exc.code == ErrorCode.MENU_NOT_FOUND:
                return None
            raise
        except AXCallError as exc:
            if exc.permission:
                raise
            _log.debug("cannot read the menu bar of %s: %s", target.name, exc)
            return None

    @_guarded("Checking an element")
    def element_valid(self, handle: Any) -> bool:
        return actions_.element_valid(self._ax, handle)

    @_guarded("Reading the element")
    def refresh_element(self, handle: Any) -> RawNode:
        self._require_ax()
        element = actions_.require_element(self._ax, handle)
        return actions_.refresh_element(self._reader, element, self._focused_element(self._ax.pid(element)))

    @_guarded("Inspecting the element")
    def element_attributes(self, handle: Any) -> dict[str, Any]:
        self._require_ax()
        return actions_.element_attributes(self._ax, handle, self.max_value_chars)

    @_guarded("Reading the text")
    def element_text(self, handle: Any, max_chars: int) -> str | None:
        self._require_ax()
        return actions_.element_text(self._ax, handle, max_chars)

    # ================================================================== AX actions
    @_guarded("Performing the action")
    def perform_action(self, handle: Any, action: str) -> None:
        self._require_ax()
        self._refuse_auth_element(handle)
        actions_.perform_action(self._ax, handle, action)

    @_guarded("Setting the value")
    def set_value(self, handle: Any, value: str | float | bool) -> None:
        self._require_ax()
        self._refuse_auth_element(handle)
        actions_.set_value(self._ax, handle, value)

    @_guarded("Focusing the element")
    def focus(self, handle: Any) -> None:
        self._require_ax()
        self._refuse_auth_element(handle)
        actions_.focus(self._ax, handle)
        pid = self._ax.pid(handle)
        if pid and pid != self._apps.frontmost_pid():
            self._raise_window_of(handle)
            self._apps.activate(pid)

    def _raise_window_of(self, element: AXElement) -> None:
        try:
            window = self._ax.get(element, A.WINDOW)
            if window is not None and self._ax.is_element(window):
                self._ax.perform(window, A.RAISE)
        except AXCallError as exc:
            _log.debug("could not raise the window of the focused element: %s", exc)

    @_guarded("Inserting text")
    def insert_text(self, handle: Any, text: str) -> bool:
        self._require_ax()
        self._refuse_auth_element(handle)
        return actions_.insert_text(self._ax, handle, text)

    @_guarded("Scrolling the element into view")
    def scroll_to_visible(self, handle: Any) -> bool:
        self._require_ax()
        return actions_.scroll_to_visible(self._ax, handle)

    # ================================================================== synthetic input
    @_guarded("Moving the mouse")
    def mouse_move(self, point: Point, duration_s: float = 0.0) -> None:
        self._require_ax()
        self._check_point(point)
        self._mouse.move(point, duration_s if math.isfinite(duration_s) else 0.0)

    @_guarded("Clicking")
    def mouse_click(
        self,
        point: Point,
        button: MouseButton = "left",
        count: int = 1,
        modifiers: frozenset[str] = frozenset(),
    ) -> None:
        self._require_ax()
        self._check_point(point)
        hit = self.app_at_point(point)
        self._refuse_auth(hit.pid if hit is not None else None)
        self._mouse.click(point, button, count, modifier_mask(modifiers))

    @_guarded("Dragging")
    def mouse_drag(
        self,
        start: Point,
        end: Point,
        duration_s: float = 0.3,
        modifiers: frozenset[str] = frozenset(),
        button: MouseButton = "left",
    ) -> None:
        self._require_ax()
        self._check_point(start)
        self._check_point(end)
        for point in (start, end):
            hit = self.app_at_point(point)
            self._refuse_auth(hit.pid if hit is not None else None)
        duration = duration_s if math.isfinite(duration_s) and duration_s >= 0 else 0.3
        self._mouse.drag(start, end, duration, button, modifier_mask(modifiers))

    @_guarded("Scrolling")
    def scroll(self, point: Point | None, dx: float, dy: float) -> None:
        self._require_ax()
        if point is not None:
            self._check_point(point)
        self._mouse.scroll(point, dx, dy)

    @_guarded("Pressing keys")
    def press_key(self, combo: KeyCombo) -> None:
        self._require_ax()
        self._require_keyboard()
        self._keyboard.press(combo)

    @_guarded("Typing text")
    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        if not text:
            return
        if not (math.isfinite(interval_s) and interval_s >= 0):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"interval_s must be ≥ 0, got {interval_s!r}.")
        self._require_ax()
        self._require_keyboard()
        self._keyboard.type_text(text, interval_s)

    @_guarded("Reading the pointer position")
    def cursor_position(self) -> Point:
        return cursor_position()

    @_guarded("Reading the HID idle time")
    def hid_idle_seconds(self) -> float:
        return self._poster.idle_seconds()

    @_guarded("Checking Secure Event Input")
    def secure_input_active(self) -> bool:
        enabled = carbon.secure_input_enabled()
        if enabled is not None:
            return enabled
        session = Q.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("kCGSSessionSecureInputPID"))

    # ================================================================== app & window control
    @_guarded("Controlling the app")
    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        return self._apps.control(action, app)

    @_guarded("Controlling the window")
    def window_control(
        self, window_id: int, action: WindowAction, rect: Rect | None = None
    ) -> WindowInfo | None:
        self._require_ax()
        cg = cg_window(window_id)
        self._refuse_auth(cg.pid if cg is not None else None)
        return self._windows.control(window_id, action, rect)

    @_guarded("Opening")
    def open_target(self, target: str, with_app: str | None = None, reveal: bool = False) -> None:
        text = (target or "").strip()
        if not text:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Nothing to open: target is empty.")
        url = self._target_url(text)
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        if reveal:
            if not url.isFileURL():
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, "Only files and folders can be revealed in Finder."
                )
            workspace.activateFileViewerSelectingURLs_([url])
            return
        config = AppKit.NSWorkspaceOpenConfiguration.configuration()
        config.setActivates_(True)
        if with_app:
            app_url, bundle = self._apps.resolve_app_url(with_app)
            label = bundle.name if bundle is not None else with_app
            _, error = call_with_completion(
                lambda handler: workspace.openURLs_withApplicationAtURL_configuration_completionHandler_(
                    [url], app_url, config, handler
                ),
                LAUNCH_TIMEOUT_S,
                f"Opening with {label}",
            )
        else:
            _, error = call_with_completion(
                lambda handler: workspace.openURL_configuration_completionHandler_(url, config, handler),
                LAUNCH_TIMEOUT_S,
                "Opening",
            )
        if error is not None:
            code = (
                ErrorCode.APP_NOT_FOUND
                if ns_error_code(error) == LS_APPLICATION_NOT_FOUND
                else ErrorCode.COMMAND_FAILED
            )
            raise AXPilotError(
                code, f"macOS could not open {h.truncate_text(text, 200)}: {ns_error_text(error)}"
            )

    @staticmethod
    def _target_url(text: str) -> Any:
        if h.looks_like_url(text):
            url = Foundation.NSURL.URLWithString_(text)
            if url is None:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, f"Not a valid URL: {h.truncate_text(text, 200)}"
                )
            if url.isFileURL() and not Path(str(url.path())).exists():
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"No such file or folder: {url.path()}")
            return url
        path = Path(text).expanduser()
        if not path.exists():
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"No such file or folder: {path}")
        return Foundation.NSURL.fileURLWithPath_(str(path.resolve()))

    # ================================================================== menus
    @_guarded("Reading the menu bar")
    def menu_bar(self, app: str | None = None, depth: int = 2, *, extras: bool = False) -> MenuNode:
        self._require_ax()
        target = self._target_app(app)
        return self._menus.read(self._ax.application(target.pid), target.name, depth, extras=extras)

    @_guarded("Choosing the menu item")
    def menu_select(self, app: str | None, path: Sequence[str], *, extras: bool = False) -> list[str]:
        self._require_ax()
        if not path:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A menu path needs at least one title.")
        target = self._target_app(app)
        self._refuse_auth(target.pid)
        return self._menus.select(
            self._ax.application(target.pid), target.name, [str(p) for p in path], extras=extras
        )

    # ================================================================== capture & vision
    @_guarded("Taking a screenshot")
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
        try:
            return capture_.take_screenshot(
                self._displays.get(),
                display_id=display_id,
                window_id=window_id,
                rect=rect,
                max_edge=max_edge,
                fmt=fmt,
                quality=quality,
                exclude_bundle_ids=exclude_bundle_ids,
                bundle_of=self._apps.bundle_id_of,
            )
        except AXPilotError as exc:
            if exc.code is ErrorCode.PERMISSION_SCREEN_RECORDING:
                self._ask_once("screen recording", perms.request_screen_recording)
            raise

    @_guarded("Recognizing text")
    def ocr(self, capture: Capture, languages: Sequence[str] | None = None) -> list[OcrLine]:
        return vision.recognize_text(capture, languages)

    # ================================================================== clipboard & notifications
    @_guarded("Reading the clipboard")
    def clipboard_get(self) -> ClipboardContent:
        return clipboard_.read_clipboard()

    @_guarded("Writing the clipboard")
    def clipboard_set(
        self,
        *,
        text: str | None = None,
        image_png: bytes | None = None,
        files: Sequence[str] | None = None,
    ) -> None:
        clipboard_.write_clipboard(text=text, image_png=image_png, files=files)

    @_guarded("Saving the clipboard")
    def clipboard_save(self) -> Any:
        return clipboard_.save_clipboard()

    @_guarded("Restoring the clipboard")
    def clipboard_restore(self, saved: Any) -> None:
        clipboard_.restore_clipboard(saved)

    @_guarded("Posting a notification")
    def notify(self, title: str, message: str, subtitle: str | None = None) -> None:
        notify_.post_notification(self._runner, title, message, subtitle)

    # ================================================================== cleanup
    def close(self) -> None:
        """Drop caches. The engine stays usable; calling this twice is harmless."""
        with self._lock:
            self._auth_pids.clear()
            self._trusted_until = 0.0
