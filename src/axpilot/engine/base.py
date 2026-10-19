"""The engine interface: every macOS primitive AXPilot needs, behind one ABC.

Two implementations exist:

* ``axpilot.engine.macos.MacEngine`` drives the real OS through PyObjC
  (Accessibility, CGEvent, ScreenCaptureKit, Vision, AppKit).
* ``axpilot.engine.fake.FakeEngine`` is an in-memory desktop loaded from JSON
  fixtures. Tests and CI use it; it runs on any OS.

Rules for implementations:

* Methods are **blocking** and are called from worker threads
  (``anyio.to_thread.run_sync``). Never block on the main thread's run loop.
* Failures raise :class:`axpilot.errors.AXPilotError` with a precise code
  (``APP_NOT_FOUND``, ``PERMISSION_ACCESSIBILITY``, ``STALE_REF``…), never bare exceptions.
* Coordinates are global screen points (see :mod:`axpilot.models`).
* Secure text fields never expose their value.
* ``app`` arguments accept an app name, a bundle id or a pid (as text). ``None`` means
  the frontmost app.
* Engines do **not** enforce safety policy; the server's policy layer does that before
  calling in. Engines must still never automate system authentication dialogs.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from typing import Any

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


class Engine(abc.ABC):
    """Abstract macOS automation engine."""

    name: str = "abstract"

    # ------------------------------------------------------------------ environment
    @abc.abstractmethod
    def permissions(self) -> PermissionStatus:
        """Current Accessibility / Screen Recording status. Must not prompt the user."""

    @abc.abstractmethod
    def os_version(self) -> str:
        """macOS product version, e.g. ``"15.3.1"`` (fake engines return a fixed value)."""

    # ------------------------------------------------------- apps, windows, displays
    @abc.abstractmethod
    def list_apps(self, include_installed: bool = False) -> list[AppInfo]:
        """Running regular (Dock) apps. With ``include_installed``, also installed but not
        running apps (``pid`` = 0)."""

    @abc.abstractmethod
    def frontmost_app(self) -> AppInfo | None: ...

    def find_app(self, query: str) -> AppInfo:
        """Resolve a running app by name, bundle id or pid. Raises ``APP_NOT_FOUND``."""
        for app in self.list_apps():
            if app.pid and app.matches(query):
                return app
        q = query.casefold()
        candidates = [a for a in self.list_apps() if a.pid and q in a.name.casefold()]
        if len(candidates) == 1:
            return candidates[0]
        raise AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"No running app matches {query!r}.",
            hint="Use `list_apps` to see running apps, or `app` with action='launch'.",
        )

    @abc.abstractmethod
    def list_windows(self, app: str | None = None) -> list[WindowInfo]:
        """Windows on the current Space, front-to-back. ``app`` filters by app."""

    @abc.abstractmethod
    def list_displays(self) -> list[DisplayInfo]: ...

    @abc.abstractmethod
    def app_at_point(self, point: Point) -> AppInfo | None:
        """The app owning the topmost window under ``point`` (used for policy hit-testing)."""

    # --------------------------------------------------------- accessibility tree
    @abc.abstractmethod
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
        """Capture the accessibility tree.

        ``window_id`` scopes to one window. Otherwise the root is the app's focused (or
        main) window; sheets and dialogs attached to it are included. If the app has no
        windows, the root is the application element. ``include_menus`` adds the app's
        menu bar (top level + first level of items) as ``RawTree.menu_bar``. The engine
        marks the focused element with ``RawNode.focused = True``.
        """

    @abc.abstractmethod
    def element_valid(self, handle: Any) -> bool:
        """False if the element no longer exists (window closed, view replaced…)."""

    @abc.abstractmethod
    def refresh_element(self, handle: Any) -> RawNode:
        """Re-read one element's attributes (``children`` left empty). Raises ``STALE_REF``."""

    @abc.abstractmethod
    def element_attributes(self, handle: Any) -> dict[str, Any]:
        """Every readable attribute of the element as JSON-safe values (for ``inspect``)."""

    # -------------------------------------------------------------- AX actions
    @abc.abstractmethod
    def perform_action(self, handle: Any, action: str) -> None:
        """Perform an AX action such as ``AXPress`` or ``AXShowMenu``.

        Raises ``ELEMENT_NOT_ACTIONABLE`` if the element does not support it."""

    @abc.abstractmethod
    def set_value(self, handle: Any, value: str | float | bool) -> None:
        """Set ``AXValue`` (text fields, sliders, checkboxes…)."""

    @abc.abstractmethod
    def focus(self, handle: Any) -> None:
        """Give keyboard focus to the element (``AXFocused = true``)."""

    @abc.abstractmethod
    def insert_text(self, handle: Any, text: str) -> bool:
        """Insert text at the element's insertion point via ``AXSelectedText``.

        Returns False (without raising) if the element does not support it, so callers can
        fall back to keystrokes."""

    @abc.abstractmethod
    def scroll_to_visible(self, handle: Any) -> bool:
        """``AXScrollToVisible``. Returns False if unsupported."""

    # ------------------------------------------------------- synthetic input
    @abc.abstractmethod
    def mouse_move(self, point: Point, duration_s: float = 0.0) -> None: ...

    @abc.abstractmethod
    def mouse_click(
        self,
        point: Point,
        button: MouseButton = "left",
        count: int = 1,
        modifiers: frozenset[str] = frozenset(),
    ) -> None: ...

    @abc.abstractmethod
    def mouse_drag(
        self,
        start: Point,
        end: Point,
        duration_s: float = 0.3,
        modifiers: frozenset[str] = frozenset(),
        button: MouseButton = "left",
    ) -> None: ...

    @abc.abstractmethod
    def scroll(self, point: Point | None, dx: float, dy: float) -> None:
        """Scroll by lines at ``point`` (or the current cursor position).

        Positive ``dy`` reveals content further **down**; positive ``dx`` further **right**."""

    @abc.abstractmethod
    def press_key(self, combo: KeyCombo) -> None:
        """Press and release a key with modifiers, mapped through the current keyboard layout."""

    @abc.abstractmethod
    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        """Type Unicode text as keyboard events (layout-independent)."""

    @abc.abstractmethod
    def cursor_position(self) -> Point: ...

    @abc.abstractmethod
    def hid_idle_seconds(self) -> float:
        """Seconds since the last *hardware* mouse/keyboard event (our synthetic events must
        not reset it)."""

    @abc.abstractmethod
    def secure_input_active(self) -> bool:
        """True while Secure Event Input is on (password fields, secure terminals)."""

    # ----------------------------------------------------- app & window control
    @abc.abstractmethod
    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        """Launch (name, bundle id or .app path), activate, hide, unhide, quit or force-quit.

        Returns the app's info after the action (``None`` after quitting)."""

    @abc.abstractmethod
    def window_control(
        self, window_id: int, action: WindowAction, rect: Rect | None = None
    ) -> WindowInfo | None:
        """``move`` uses ``rect.x/y``; ``resize`` uses ``rect.w/h``; both may use the full rect.

        Returns the window afterwards (``None`` after ``close``)."""

    @abc.abstractmethod
    def open_target(self, target: str, with_app: str | None = None, reveal: bool = False) -> None:
        """Open a URL or file path (like ``open``), optionally with a given app, or reveal it in
        Finder."""

    # ------------------------------------------------------------------- menus
    @abc.abstractmethod
    def menu_bar(self, app: str | None = None, depth: int = 2) -> MenuNode:
        """The app's menu bar. The root's children are the top-level menus (Apple menu first)."""

    @abc.abstractmethod
    def menu_select(self, app: str | None, path: Sequence[str]) -> list[str]:
        """Select a menu item by path (case-insensitive, ``…``/``...`` tolerant, prefix-matching
        allowed when unambiguous). Returns the exact titles that were matched. Raises
        ``MENU_NOT_FOUND`` or ``ELEMENT_NOT_ACTIONABLE`` for disabled items."""

    # ---------------------------------------------------------- capture & vision
    @abc.abstractmethod
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
        """Capture a display (default: the main display), a window, or a rect (global points).

        The image is downscaled so its longest edge is at most ``max_edge`` pixels. Windows of
        ``exclude_bundle_ids`` (and AXPilot's own overlay) are left out of the image. Raises
        ``PERMISSION_SCREEN_RECORDING`` without permission."""

    @abc.abstractmethod
    def ocr(self, capture: Capture, languages: Sequence[str] | None = None) -> list[OcrLine]:
        """Recognize text in a capture. Frames are returned in global screen points."""

    # ------------------------------------------------- clipboard & notifications
    @abc.abstractmethod
    def clipboard_get(self) -> ClipboardContent: ...

    @abc.abstractmethod
    def clipboard_set(
        self,
        *,
        text: str | None = None,
        image_png: bytes | None = None,
        files: Sequence[str] | None = None,
    ) -> None: ...

    @abc.abstractmethod
    def notify(self, title: str, message: str, subtitle: str | None = None) -> None: ...

    # ------------------------------------------------------------------ cleanup
    def close(self) -> None:  # noqa: B027 - optional hook, intentionally empty
        """Release resources (observers, caches). Safe to call more than once."""


def iter_nodes(node: RawNode):
    """Pre-order traversal helper usable on any :class:`RawNode`."""
    return node.walk()
