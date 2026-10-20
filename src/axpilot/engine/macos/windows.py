"""Windows: ids and frames from ``CGWindowList``, titles and state from Accessibility.

Permissions: ``CGWindowListCopyWindowInfo`` needs none for window ids, owners, layers and
bounds, but window *titles* (``kCGWindowName``) need Screen Recording, so titles and states
(main, focused, minimized, full screen) come from Accessibility. ``window_control`` needs
Accessibility.

AX windows and CG windows are paired by :func:`~axpilot.engine.macos.helpers.match_windows`
(same process, equal frames, then equal titles, then z-order), because the only API that maps
an ``AXUIElement`` to its ``CGWindowID`` is private (``_AXUIElementGetWindow``). Matching runs
in two phases: AX windows that are not minimized against on-screen CG windows, then the rest
(minimized windows) against off-screen ones, so a window on another Space with the same frame
cannot steal a match. Windows on other Spaces are not listed (AX does not report them).
Without Accessibility, windows are still listed from CGWindowList alone (titles may be empty).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import Quartz as Q

from axpilot.engine.macos import attributes as A
from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.ax import AX, AXCallError, AXElement, ax_failure
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, Rect, WindowAction, WindowInfo

if TYPE_CHECKING:
    from axpilot.engine.macos.apps import AppService, ElectronEnhancer
    from axpilot.engine.macos.displays import DisplayCache

_log = logging.getLogger(__name__)

MIN_WINDOW_SIZE = 20.0
"""Smaller layer-0 windows are helper surfaces, not user windows."""
SETTLE_S = 0.15
"""Pause after a window action before reading the window back (animations)."""
WINDOW_ATTRIBUTES: tuple[str, ...] = (
    A.ROLE,
    A.SUBROLE,
    A.TITLE,
    A.POSITION,
    A.SIZE,
    A.MINIMIZED,
    A.MAIN,
    A.FOCUSED,
    A.FULL_SCREEN,
    A.MODAL,
)
DIALOG_SUBROLES: frozenset[str] = frozenset({"AXDialog", "AXSystemDialog", "AXFloatingWindow"})
WINDOW_ACTIONS: frozenset[str] = frozenset(
    {"focus", "move", "resize", "minimize", "restore", "zoom", "fullscreen", "close"}
)


@dataclass(frozen=True, slots=True)
class CGWindow:
    """One entry of ``CGWindowListCopyWindowInfo``."""

    window_id: int
    pid: int
    owner: str
    title: str | None
    frame: Rect
    layer: int
    alpha: float
    on_screen: bool

    @property
    def is_user_window(self) -> bool:
        """A normal-level, visible, reasonably sized window."""
        return (
            self.layer == 0
            and self.alpha > 0
            and self.frame.w >= MIN_WINDOW_SIZE
            and self.frame.h >= MIN_WINDOW_SIZE
        )


@dataclass(slots=True)
class AXWindow:
    """An ``AXWindow`` element with the attributes AXPilot needs."""

    element: AXElement
    title: str | None
    frame: Rect | None
    subrole: str | None
    minimized: bool
    main: bool
    focused: bool
    fullscreen: bool
    modal: bool

    @property
    def geometry(self) -> h.WindowGeometry:
        return h.WindowGeometry(self.frame, self.title)


# ------------------------------------------------------------------ CGWindowList


def _number(info: Any, key: Any, default: float = 0.0) -> float:
    value = info.get(key)
    return float(value) if isinstance(value, int | float) else default


def _parse_cg(info: Any) -> CGWindow | None:
    bounds = info.get(Q.kCGWindowBounds)
    window_id = info.get(Q.kCGWindowNumber)
    pid = info.get(Q.kCGWindowOwnerPID)
    if bounds is None or window_id is None or pid is None:
        return None
    frame = Rect(
        float(bounds.get("X", 0.0)),
        float(bounds.get("Y", 0.0)),
        float(bounds.get("Width", 0.0)),
        float(bounds.get("Height", 0.0)),
    )
    title = info.get(Q.kCGWindowName)
    owner = info.get(Q.kCGWindowOwnerName)
    return CGWindow(
        window_id=int(window_id),
        pid=int(pid),
        owner=str(owner) if owner else "",
        title=str(title) if title else None,
        frame=frame,
        layer=int(_number(info, Q.kCGWindowLayer)),
        alpha=_number(info, Q.kCGWindowAlpha, 1.0),
        on_screen=bool(info.get(Q.kCGWindowIsOnscreen) or False),
    )


def _window_list(options: int, relative_to: int = 0) -> list[CGWindow]:
    infos = Q.CGWindowListCopyWindowInfo(options, relative_to)
    windows = []
    for info in list(infos or ()):
        parsed = _parse_cg(info)
        if parsed is not None:
            windows.append(parsed)
    return windows


def cg_windows(*, on_screen_only: bool = True) -> list[CGWindow]:
    """Windows front-to-back (without desktop elements). Off-screen windows include
    minimized ones, windows of hidden apps and windows on other Spaces."""
    options = Q.kCGWindowListExcludeDesktopElements | (
        Q.kCGWindowListOptionOnScreenOnly if on_screen_only else Q.kCGWindowListOptionAll
    )
    windows = _window_list(options, Q.kCGNullWindowID)
    if on_screen_only:
        return [w if w.on_screen else _with_on_screen(w) for w in windows]
    return windows


def _with_on_screen(window: CGWindow) -> CGWindow:
    return CGWindow(
        window.window_id,
        window.pid,
        window.owner,
        window.title,
        window.frame,
        window.layer,
        window.alpha,
        True,
    )


def cg_window(window_id: int) -> CGWindow | None:
    """One window by id (on or off screen), or ``None``."""
    windows = _window_list(Q.kCGWindowListOptionIncludingWindow, int(window_id))
    return next((w for w in windows if w.window_id == window_id), None)


# ------------------------------------------------------------------ service


class WindowService:
    """Lists windows and acts on them."""

    def __init__(
        self,
        ax: AX,
        apps: AppService,
        displays: DisplayCache,
        enhancer: ElectronEnhancer,
    ) -> None:
        self._ax = ax
        self._apps = apps
        self._displays = displays
        self._enhancer = enhancer
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[int, AXElement]] = {}

    # ------------------------------------------------------------------ AX side
    def ax_windows(self, app_element: AXElement) -> list[AXWindow]:
        """The app's ``AXWindows`` (only role ``AXWindow``). Raises :class:`AXCallError`."""
        result: list[AXWindow] = []
        for element in list(self._ax.get(app_element, A.WINDOWS) or ()):
            if not self._ax.is_element(element):
                continue
            try:
                window = self.read_window(element)
            except AXCallError as exc:
                if exc.permission:
                    raise
                continue
            if window is not None:
                result.append(window)
        return result

    def read_window(self, element: AXElement) -> AXWindow | None:
        attrs = self._ax.get_many(element, WINDOW_ATTRIBUTES)
        if A.text(attrs.get(A.ROLE)) != "AXWindow":
            return None
        return AXWindow(
            element=element,
            title=A.text(attrs.get(A.TITLE)),
            frame=A.frame_of(self._ax, attrs),
            subrole=A.text(attrs.get(A.SUBROLE)),
            minimized=A.flag(attrs.get(A.MINIMIZED)),
            main=A.flag(attrs.get(A.MAIN)),
            focused=A.flag(attrs.get(A.FOCUSED)),
            fullscreen=A.flag(attrs.get(A.FULL_SCREEN)),
            modal=A.flag(attrs.get(A.MODAL)),
        )

    def _ax_windows_quietly(self, app: AppInfo) -> list[AXWindow] | None:
        """AX windows, or ``None`` when AX is unavailable for this app."""
        if not self._ax.is_trusted():
            return None
        try:
            return self.ax_windows(self._ax.application(app.pid))
        except AXCallError as exc:
            _log.debug("cannot read the windows of %s: %s", app.name, exc)
            return None

    def focused_window(self, app: AppInfo) -> AXElement | None:
        """The app's focused window if the app is frontmost (else ``None``)."""
        return self._focused_window(app, self._apps.frontmost_pid())

    def _focused_window(self, app: AppInfo, frontmost_pid: int | None) -> AXElement | None:
        if app.pid != frontmost_pid or not self._ax.is_trusted():
            return None
        try:
            return self._ax.get(self._ax.application(app.pid), A.FOCUSED_WINDOW)
        except AXCallError:
            return None

    @staticmethod
    def match(
        ax_windows: list[AXWindow], on_screen: list[CGWindow], off_screen: list[CGWindow]
    ) -> dict[int, CGWindow]:
        """Pair AX windows (by index) with CG windows of the same app (two phases)."""
        pairs: dict[int, CGWindow] = {}
        visible = [i for i, w in enumerate(ax_windows) if not w.minimized]
        first = h.match_windows(
            [ax_windows[i].geometry for i in visible], [h.WindowGeometry(c.frame, c.title) for c in on_screen]
        )
        for local, cg_index in first.items():
            pairs[visible[local]] = on_screen[cg_index]
        rest = [i for i in range(len(ax_windows)) if i not in pairs]
        taken = {c.window_id for c in pairs.values()}
        spare = [c for c in off_screen if c.window_id not in taken]
        second = h.match_windows(
            [ax_windows[i].geometry for i in rest], [h.WindowGeometry(c.frame, c.title) for c in spare]
        )
        for local, cg_index in second.items():
            pairs[rest[local]] = spare[cg_index]
        return pairs

    # ------------------------------------------------------------------ info
    def info(
        self,
        app: AppInfo,
        window: AXWindow | None,
        cg: CGWindow | None,
        *,
        focused_element: AXElement | None = None,
    ) -> WindowInfo:
        frame = (window.frame if window is not None else None) or (
            cg.frame if cg is not None else Rect(0, 0, 0, 0)
        )
        display = h.display_for_rect(frame, self._displays.get())
        focused = (
            window is not None
            and focused_element is not None
            and self._ax.equal(window.element, focused_element)
        )
        return WindowInfo(
            window_id=cg.window_id if cg is not None else 0,
            pid=app.pid,
            app=app.name,
            title=(window.title if window is not None else None)
            or (cg.title if cg is not None else None)
            or "",
            frame=frame,
            bundle_id=app.bundle_id,
            display_id=display.display_id if display is not None else None,
            focused=focused,
            main=window.main if window is not None else False,
            minimized=window.minimized if window is not None else False,
            fullscreen=window.fullscreen if window is not None else False,
        )

    def list_windows(self, app: AppInfo | None = None) -> list[WindowInfo]:
        """Windows on the current Space front-to-back, then minimized windows."""
        own_pid = os.getpid()
        on_screen = [w for w in cg_windows(on_screen_only=True) if w.is_user_window and w.pid != own_pid]
        off_screen = [w for w in cg_windows(on_screen_only=False) if w.layer == 0 and not w.on_screen]
        frontmost = self._apps.frontmost_pid()
        if app is not None:
            targets = [app]
        else:
            targets = self._apps.running(extra_pids=frozenset(w.pid for w in on_screen))
            known = {t.pid for t in targets}
            # Accessory (menu-bar) apps can show real windows too, e.g. a settings window.
            for pid, owner in {w.pid: w.owner for w in on_screen if w.pid not in known}.items():
                targets.append(
                    self._apps.info_for_pid(pid, frontmost) or AppInfo(name=owner or f"pid {pid}", pid=pid)
                )
        order = {w.window_id: index for index, w in enumerate(on_screen)}
        shown: list[WindowInfo] = []
        minimized: list[WindowInfo] = []
        for target in targets:
            if target.hidden or target.pid == own_pid:
                continue
            mine = [w for w in on_screen if w.pid == target.pid]
            infos = self._app_windows(target, mine, [w for w in off_screen if w.pid == target.pid], frontmost)
            for info in infos:
                (minimized if info.minimized else shown).append(info)
        shown.sort(key=lambda info: order.get(info.window_id, len(order)))
        return shown + minimized

    def _app_windows(
        self, app: AppInfo, on_screen: list[CGWindow], off_screen: list[CGWindow], frontmost: int | None
    ) -> list[WindowInfo]:
        ax_windows = self._ax_windows_quietly(app)
        if ax_windows is None:
            return [self.info(app, None, cg) for cg in on_screen]
        focused = self._focused_window(app, frontmost)
        pairs = self.match(ax_windows, on_screen, off_screen)
        infos = []
        for index, window in enumerate(ax_windows):
            cg = pairs.get(index)
            if cg is None:
                continue  # not on this Space, or an invisible helper window
            infos.append(self.info(app, window, cg, focused_element=focused))
            self._remember(cg.window_id, app.pid, window.element)
        matched_ids = {cg.window_id for cg in pairs.values()}
        infos.extend(self.info(app, None, cg) for cg in on_screen if cg.window_id not in matched_ids)
        return infos

    # ------------------------------------------------------------------ lookup
    def _remember(self, window_id: int, pid: int, element: AXElement) -> None:
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[window_id] = (pid, element)

    def _cached(self, cg: CGWindow) -> AXWindow | None:
        with self._lock:
            entry = self._cache.get(cg.window_id)
        if entry is None or entry[0] != cg.pid:
            return None
        try:
            window = self.read_window(entry[1])
        except AXCallError:
            return None
        if window is None or window.frame is None:
            return None
        same_frame = max(
            abs(window.frame.x - cg.frame.x),
            abs(window.frame.y - cg.frame.y),
            abs(window.frame.w - cg.frame.w),
            abs(window.frame.h - cg.frame.h),
        )
        return window if same_frame <= 4.0 or window.minimized else None

    def locate(self, window_id: int) -> tuple[AppInfo, AXWindow, CGWindow]:
        """The app, AX window and CG entry of ``window_id``. Raises ``WINDOW_NOT_FOUND``
        (or ``PERMISSION_ACCESSIBILITY``)."""
        cg = cg_window(window_id)
        if cg is None or cg.layer != 0:
            raise AXPilotError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"No window has id {window_id}.",
                hint="Use `list_windows` to see current window ids.",
            )
        app = self._apps.info_for_pid(cg.pid) or AppInfo(name=cg.owner or f"pid {cg.pid}", pid=cg.pid)
        if not self._ax.is_trusted():
            raise AXPilotError(ErrorCode.PERMISSION_ACCESSIBILITY, "Window control needs Accessibility.")
        cached = self._cached(cg)
        if cached is not None:
            return app, cached, cg
        try:
            ax_windows = self.ax_windows(self._ax.application(cg.pid))
        except AXCallError as exc:
            raise ax_failure(exc, f"Reading the windows of {app.name}", timeout_s=self._ax.timeout_s) from exc
        for index, paired in self._pairs_for(cg.pid, ax_windows).items():
            self._remember(paired.window_id, cg.pid, ax_windows[index].element)
            if paired.window_id == window_id:
                return app, ax_windows[index], cg
        raise AXPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"Window {window_id} of {app.name} is not reachable through Accessibility.",
            hint="It may be on another Space or not a regular window. Use `list_windows`.",
        )

    def _pairs_for(self, pid: int, ax_windows: list[AXWindow]) -> dict[int, CGWindow]:
        on_screen = [w for w in cg_windows(on_screen_only=True) if w.pid == pid and w.layer == 0]
        off_screen = [
            w for w in cg_windows(on_screen_only=False) if w.pid == pid and w.layer == 0 and not w.on_screen
        ]
        return self.match(ax_windows, on_screen, off_screen)

    def root_context(self, app: AppInfo, root: AXElement) -> tuple[WindowInfo | None, list[AXElement]]:
        """For a snapshot root: its :class:`WindowInfo` (``window_id`` 0 if it cannot be
        paired with a CGWindow) and the app's other modal dialog windows to include."""
        try:
            ax_windows = self.ax_windows(self._ax.application(app.pid))
        except AXCallError as exc:
            if exc.permission:
                raise
            ax_windows = []
        focused = self._focused_window(app, self._apps.frontmost_pid())
        index = next((i for i, w in enumerate(ax_windows) if self._ax.equal(w.element, root)), None)
        if index is None:
            # AXWindows leaves out windows on another Space (seen on macOS 26 while a full-screen
            # app is in front), but the focused window still reads: pair that one window alone.
            try:
                window = self.read_window(root)
            except AXCallError:
                window = None
            if window is None:
                return None, []
            cg = self._pairs_for(app.pid, [window]).get(0)
            if cg is not None:
                self._remember(cg.window_id, app.pid, root)
            return self.info(app, window, cg, focused_element=focused), []
        cg = self._pairs_for(app.pid, ax_windows).get(index)
        if cg is not None:
            self._remember(cg.window_id, app.pid, root)
        dialogs = [
            w.element
            for i, w in enumerate(ax_windows)
            if i != index and w.modal and not w.minimized and (w.subrole or "") in DIALOG_SUBROLES
        ]
        return self.info(app, ax_windows[index], cg, focused_element=focused), dialogs

    # ------------------------------------------------------------------ control
    def control(self, window_id: int, action: WindowAction, rect: Rect | None) -> WindowInfo | None:
        """Perform a window action through Accessibility; returns the window afterwards."""
        if action not in WINDOW_ACTIONS:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown window action {action!r}.",
                hint="Use one of: " + ", ".join(sorted(WINDOW_ACTIONS)) + ".",
            )
        if action in ("move", "resize") and rect is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Window action {action!r} needs a rect.")
        if action == "resize" and rect is not None and (rect.w <= 0 or rect.h <= 0):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A window size must be positive.")
        app, window, _ = self.locate(window_id)
        element = window.element
        try:
            self._apply(app, window, action, rect)
        except AXCallError as exc:
            raise ax_failure(
                exc, f"{action} on window {window_id} of {app.name}", timeout_s=self._ax.timeout_s
            ) from exc
        if action == "close":
            return None
        time.sleep(SETTLE_S)
        try:
            after = self.read_window(element)
        except AXCallError:
            after = None
        cg = cg_window(window_id)
        focused = self._focused_window(app, self._apps.frontmost_pid())
        return self.info(app, after or window, cg, focused_element=focused)

    def _apply(self, app: AppInfo, window: AXWindow, action: str, rect: Rect | None) -> None:
        ax, element = self._ax, window.element
        if action == "focus":
            if window.minimized:
                ax.set(element, A.MINIMIZED, False)
            ax.perform(element, A.RAISE)
            self._try_set(element, A.MAIN, True)
            self._apps.activate(app.pid)
        elif action == "move" and rect is not None:
            with self._enhancer.geometry_change(app.pid):
                ax.set(element, A.POSITION, ax.make_point(rect.x, rect.y))
        elif action == "resize" and rect is not None:
            with self._enhancer.geometry_change(app.pid):
                ax.set(element, A.SIZE, ax.make_size(rect.w, rect.h))
        elif action == "minimize":
            ax.set(element, A.MINIMIZED, True)
        elif action == "restore":
            if window.minimized:
                ax.set(element, A.MINIMIZED, False)
            if window.fullscreen:
                ax.set(element, A.FULL_SCREEN, False)
        elif action == "zoom":
            self._press_button(window, A.ZOOM_BUTTON, "zoom")
        elif action == "fullscreen":
            if not window.fullscreen:
                if ax.settable(element, A.FULL_SCREEN):
                    ax.set(element, A.FULL_SCREEN, True)
                else:
                    self._press_button(window, A.FULL_SCREEN_BUTTON, "full screen")
        elif action == "close":
            self._press_button(window, A.CLOSE_BUTTON, "close")

    def _try_set(self, element: AXElement, attribute: str, value: Any) -> None:
        try:
            self._ax.set(element, attribute, value)
        except AXCallError as exc:
            _log.debug("could not set %s: %s", attribute, exc)

    def _press_button(self, window: AXWindow, attribute: str, what: str) -> None:
        button = self._ax.get(window.element, attribute)
        if button is None or not self._ax.is_element(button):
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"The window {window.title or ''!r} has no {what} button.",
                hint="Try the Window menu with `select_menu`.",
            )
        try:
            self._ax.perform(button, A.PRESS)
        except AXCallError as exc:
            if not (exc.code == h.AX_CANNOT_COMPLETE and exc.timed_out):
                raise
            _log.info("the %s button did not answer in time; assuming it was pressed", what)
