"""Apps: running and installed apps, the frontmost app, launch/activate/hide/quit, and the
Chromium/Electron accessibility switch.

Permissions: listing and launching need none. Detecting the frontmost app prefers
Accessibility (``AXFocusedApplication`` of the system-wide element, which is always current)
and falls back to ``NSWorkspace``. Activation also sets ``AXFrontmost`` (Accessibility), which
macOS 14+ honours even from a background process.

Freshness: ``NSWorkspace.runningApplications`` and ``NSRunningApplication`` properties are
only updated while the **main run loop** runs (AppKit documents this). The CLI, the host and
the SDK keep it turning (:mod:`axpilot.runloop`); where nothing does, the list can be stale, so
terminated apps are filtered by pid liveness and apps owning on-screen windows are merged in
from ``CGWindowList``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import AppKit

from axpilot.engine.macos import attributes as A
from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.actions import process_alive
from axpilot.engine.macos.ax import AX, AXCallError, AXElement
from axpilot.engine.macos.sync import call_with_completion, ns_error_text
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppAction, AppInfo
from axpilot.system.runner import CommandRunner

_log = logging.getLogger(__name__)

LAUNCH_TIMEOUT_S = 20.0
"""How long ``launch`` waits for LaunchServices to start the app."""
ACTIVATE_TIMEOUT_S = 1.5
QUIT_TIMEOUT_S = 6.0
FORCE_QUIT_TIMEOUT_S = 3.0
POLL_S = 0.05
INSTALLED_TTL_S = 60.0
ENHANCE_SETTLE_S = 0.15
"""Interval between readings of a Chromium/Electron tree that is being built."""
ENHANCE_POLLS = 10
"""At most this many readings (1.5 s): Chrome needed more than one 150 ms pause (macOS 26)."""
ENHANCE_COUNT_LIMIT = 200
"""Nodes counted per reading; a tree this big is built enough to read."""


def _running_app(pid: int) -> Any:
    return AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))


def _wait(predicate: Any, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def app_info(app: Any, frontmost_pid: int | None) -> AppInfo:
    """An :class:`AppInfo` from an ``NSRunningApplication``."""
    pid = int(app.processIdentifier())
    url = app.bundleURL()
    path = str(url.path()) if url is not None and url.path() else None
    bundle_id = app.bundleIdentifier()
    name = app.localizedName() or (Path(path).stem if path else None) or f"pid {pid}"
    return AppInfo(
        name=str(name),
        pid=pid,
        bundle_id=str(bundle_id) if bundle_id else None,
        path=path,
        frontmost=frontmost_pid is not None and pid == frontmost_pid,
        hidden=bool(app.isHidden()),
    )


class ElectronEnhancer:
    """Switches on the full accessibility tree of Chromium and Electron apps, once per pid.

    Chromium builds its AX tree lazily (only for assistive technology), so a plain AX client
    sees an almost empty window. Setting ``AXManualAccessibility`` (Electron, Chromium) and
    ``AXEnhancedUserInterface`` (Chromium browsers) on the application element turns it on.
    ``AXEnhancedUserInterface`` makes window moves animate slowly, so it is switched off
    around window geometry changes (:meth:`geometry_change`).
    """

    def __init__(self, ax: AX, enabled: bool = True) -> None:
        self._ax = ax
        self.enabled = enabled
        self._lock = threading.Lock()
        self._seen: dict[int, h.ChromiumKind | None] = {}
        self._enhanced_ui: set[int] = set()

    def ensure(self, app: AppInfo) -> bool:
        """Enable the tree for ``app`` if needed. True if it was switched on just now."""
        if not self.enabled or app.pid <= 0:
            return False
        with self._lock:
            if app.pid in self._seen:
                return False
            kind = h.chromium_kind(app.path, app.bundle_id)
            self._seen[app.pid] = kind
            self._prune()
        if kind is None:
            return False
        element = self._ax.application(app.pid)
        self._set(element, A.MANUAL_ACCESSIBILITY, True)
        if kind == "chromium" and self._set(element, A.ENHANCED_USER_INTERFACE, True):
            with self._lock:
                self._enhanced_ui.add(app.pid)
        _log.info("enabled the accessibility tree of %s (%s)", app.name, kind)
        h.wait_until_stable(
            lambda: self._tree_size(element), time.sleep, interval_s=ENHANCE_SETTLE_S, polls=ENHANCE_POLLS
        )
        return True

    def _tree_size(self, element: AXElement) -> int:
        """Nodes under ``element``, counted up to :data:`ENHANCE_COUNT_LIMIT`."""
        count, stack = 0, [element]
        while stack and count < ENHANCE_COUNT_LIMIT:
            node = stack.pop()
            count += 1
            try:
                stack.extend(list(self._ax.get(node, A.CHILDREN) or ()))
            except AXCallError as exc:
                _log.debug("cannot count the tree being built: %s", exc)
        return count

    @contextlib.contextmanager
    def geometry_change(self, pid: int) -> Iterator[None]:
        """Temporarily switch ``AXEnhancedUserInterface`` off (window moves stay instant)."""
        with self._lock:
            active = pid in self._enhanced_ui
        element = self._ax.application(pid) if active else None
        if element is not None:
            self._set(element, A.ENHANCED_USER_INTERFACE, False)
        try:
            yield
        finally:
            if element is not None:
                self._set(element, A.ENHANCED_USER_INTERFACE, True)

    def _set(self, element: Any, attribute: str, value: bool) -> bool:
        try:
            self._ax.set(element, attribute, value)
        except AXCallError as exc:
            _log.debug("could not set %s: %s", attribute, exc)
            return False
        return True

    def _prune(self) -> None:
        if len(self._seen) > 256:
            for pid in [p for p in self._seen if not process_alive(p)]:
                self._seen.pop(pid, None)
                self._enhanced_ui.discard(pid)


class AppService:
    """Running apps, installed apps and app control."""

    def __init__(self, ax: AX, runner: CommandRunner) -> None:
        self._ax = ax
        self._runner = runner
        self._installed: list[h.BundleInfo] = []
        self._installed_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def workspace() -> Any:
        return AppKit.NSWorkspace.sharedWorkspace()

    # ------------------------------------------------------------------ queries
    def frontmost_pid(self) -> int | None:
        """Pid of the app that receives keyboard input."""
        if self._ax.is_trusted():
            try:
                element = self._ax.get(self._ax.system_wide, A.FOCUSED_APPLICATION)
            except AXCallError as exc:
                _log.debug("AXFocusedApplication failed: %s", exc)
                element = None
            if element is not None:
                pid = self._ax.pid(element)
                if pid:
                    return pid
        app = self.workspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None

    def _running(self, *, regular_only: bool) -> list[Any]:
        apps = []
        for app in list(self.workspace().runningApplications() or ()):
            if app.isTerminated() or not process_alive(int(app.processIdentifier())):
                continue
            if regular_only and app.activationPolicy() != AppKit.NSApplicationActivationPolicyRegular:
                continue
            apps.append(app)
        return apps

    def running(self, *, extra_pids: set[int] | frozenset[int] = frozenset()) -> list[AppInfo]:
        """Running regular (Dock) apps, plus regular apps owning ``extra_pids`` that the
        possibly stale workspace list misses."""
        front = self.frontmost_pid()
        apps = self._running(regular_only=True)
        infos = [app_info(app, front) for app in apps]
        known = {info.pid for info in infos}
        for pid in sorted(extra_pids - known):
            app = _running_app(pid)
            if app is not None and app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
                infos.append(app_info(app, front))
        return infos

    @staticmethod
    def is_running(bundle_id: str) -> bool:
        """True if an app with ``bundle_id`` is running."""
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        return any(not app.isTerminated() for app in list(apps or ()))

    @staticmethod
    def bundle_id_of(pid: int) -> str | None:
        """Bundle id of a running process (``None`` for plain processes)."""
        app = _running_app(pid)
        bundle_id = app.bundleIdentifier() if app is not None else None
        return str(bundle_id) if bundle_id else None

    def frontmost(self) -> AppInfo | None:
        pid = self.frontmost_pid()
        return self.info_for_pid(pid, pid) if pid else None

    def info_for_pid(self, pid: int, frontmost_pid: int | None = None) -> AppInfo | None:
        """Info for any running process that LaunchServices knows (any activation policy)."""
        app = _running_app(pid)
        if app is None or not process_alive(pid):
            return None
        return app_info(app, frontmost_pid if frontmost_pid is not None else self.frontmost_pid())

    def installed(self) -> list[h.BundleInfo]:
        """App bundles in the standard folders (cached for a minute)."""
        with self._lock:
            if not self._installed or time.monotonic() - self._installed_at > INSTALLED_TTL_S:
                self._installed = [h.read_bundle_info(path) for path in h.scan_app_bundles()]
                self._installed_at = time.monotonic()
            return list(self._installed)

    def list_apps(
        self, include_installed: bool = False, *, extra_pids: frozenset[int] = frozenset()
    ) -> list[AppInfo]:
        """Running regular apps; with ``include_installed``, also apps that are not running
        (``pid`` 0), without duplicates."""
        infos = self.running(extra_pids=extra_pids)
        if include_installed:
            running_ids = {i.bundle_id for i in infos if i.bundle_id}
            running_paths = {i.path for i in infos if i.path}
            for bundle in self.installed():
                if bundle.bundle_id in running_ids or bundle.path in running_paths:
                    continue
                infos.append(AppInfo(name=bundle.name, pid=0, bundle_id=bundle.bundle_id, path=bundle.path))
        return infos

    def find(self, query: str, *, extra_pids: frozenset[int] = frozenset()) -> AppInfo:
        """A running app by name, bundle id, pid or bundle path. Raises ``APP_NOT_FOUND``."""
        q = query.strip()
        if not q:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The app name is empty.")
        apps = self.running(extra_pids=extra_pids)
        for app in apps:
            if app.matches(q):
                return app
        wanted_path = os.path.normpath(os.path.expanduser(q)).casefold()
        for app in apps:
            if app.path and os.path.normpath(app.path).casefold() == wanted_path:
                return app
        if q.isdigit():
            info = self.info_for_pid(int(q))
            if info is not None:
                return info
        front = self.frontmost_pid()
        for app in self._running(regular_only=False):
            info = app_info(app, front)
            if info.matches(q):
                return info
        partial = [a for a in apps if q.casefold() in a.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        raise AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"No running app matches {query!r}.",
            hint="Use `list_apps` to see running apps, or `app` with action='launch'.",
        )

    # ------------------------------------------------------------------ resolution
    def resolve_app_url(self, query: str) -> tuple[Any, h.BundleInfo | None]:
        """The bundle URL for a launch target: a ``.app`` path, a bundle id or an app name."""
        q = query.strip()
        if not q:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The app name is empty.")
        if h.looks_like_app_path(q):
            path = Path(q).expanduser()
            if path.is_dir():
                return AppKit.NSURL.fileURLWithPath_(str(path)), h.read_bundle_info(path)
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, f"No app bundle at {path}.")
        if h.looks_like_bundle_id(q):
            url = self.workspace().URLForApplicationWithBundleIdentifier_(q)
            if url is not None:
                return url, h.read_bundle_info(str(url.path()))
        for bundle in self.installed():
            if bundle.matches_name(q) or (bundle.bundle_id and bundle.bundle_id.casefold() == q.casefold()):
                return AppKit.NSURL.fileURLWithPath_(bundle.path), bundle
        full_path = self.workspace().fullPathForApplication_(q.removesuffix(".app"))
        if full_path:
            return AppKit.NSURL.fileURLWithPath_(str(full_path)), h.read_bundle_info(str(full_path))
        raise AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"No installed app matches {query!r}.",
            hint="Use `list_apps` with include_installed=true to see installed apps.",
        )

    def _running_instance(self, bundle: h.BundleInfo | None, url: Any) -> AppInfo | None:
        path = str(url.path()) if url is not None else None
        front = self.frontmost_pid()
        for app in self._running(regular_only=False):
            info = app_info(app, front)
            if (bundle and bundle.bundle_id and info.bundle_id == bundle.bundle_id) or (
                path and info.path == path
            ):
                return info
        return None

    # ------------------------------------------------------------------ control
    def control(self, action: AppAction, query: str) -> AppInfo | None:
        """``launch``/``activate``/``hide``/``unhide``/``quit``/``force_quit``."""
        if action == "launch":
            return self.launch(query)
        if action not in ("activate", "hide", "unhide", "quit", "force_quit"):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown app action {action!r}.",
                hint="Use one of: activate, force_quit, hide, launch, quit, unhide.",
            )
        target = self.find(query)
        if target.pid == os.getpid():
            raise AXPilotError(ErrorCode.BLOCKED_BY_POLICY, "AXPilot does not control its own process.")
        if action == "activate":
            return self.activate(target.pid)
        if action == "hide":
            return self._set_hidden(target.pid, True)
        if action == "unhide":
            return self._set_hidden(target.pid, False)
        return self._quit(target, force=action == "force_quit")

    def launch(self, query: str) -> AppInfo:
        """Launch (or, if running, activate) an app and return it."""
        url, bundle = self.resolve_app_url(query)
        running = self._running_instance(bundle, url)
        if running is not None:
            return self.activate(running.pid)
        label = bundle.name if bundle else query
        pid = self._open_application(url, label)
        if pid is None:
            pid = self._launch_with_open(url, bundle, label)
        _wait(lambda: self._ax_ready(pid), 5.0)
        info = self.info_for_pid(pid)
        if info is None:
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, f"{label} quit right after launching.")
        return info

    def _ax_ready(self, pid: int) -> bool:
        if not self._ax.is_trusted():
            return True
        try:
            return self._ax.get(self._ax.application(pid), A.ROLE) is not None
        except AXCallError:
            return False

    def _open_application(self, url: Any, label: str) -> int | None:
        config = AppKit.NSWorkspaceOpenConfiguration.configuration()
        config.setActivates_(True)
        try:
            app, error = call_with_completion(
                lambda handler: self.workspace().openApplicationAtURL_configuration_completionHandler_(
                    url, config, handler
                ),
                LAUNCH_TIMEOUT_S,
                f"Launching {label}",
            )
        except (TypeError, ValueError, AttributeError) as exc:  # bridge problems: use `open`
            _log.warning("openApplicationAtURL failed (%s); falling back to `open`", exc)
            return None
        if error is not None:
            raise AXPilotError(
                ErrorCode.COMMAND_FAILED,
                f"macOS could not launch {label}: {ns_error_text(error)}",
                hint="Check that the app opens when you double-click it in Finder.",
            )
        return int(app.processIdentifier()) if app is not None else None

    def _launch_with_open(self, url: Any, bundle: h.BundleInfo | None, label: str) -> int:
        path = str(url.path())
        result = self._runner.run(["open", "-a", path], timeout_s=LAUNCH_TIMEOUT_S)
        if not result.ok:
            raise AXPilotError(
                ErrorCode.COMMAND_FAILED,
                f"`open` could not launch {label}: {result.stderr.strip()[:300] or 'no error output'}",
            )
        found: list[AppInfo] = []

        def started() -> bool:
            info = self._running_instance(bundle, url)
            if info is not None:
                found.append(info)
            return info is not None

        if not _wait(started, LAUNCH_TIMEOUT_S):
            raise AXPilotError(ErrorCode.TIMEOUT, f"{label} did not start within {LAUNCH_TIMEOUT_S:g} s.")
        return found[0].pid

    def activate(self, pid: int) -> AppInfo:
        """Bring an app to the front: ``activateWithOptions:`` and ``AXFrontmost`` together
        (see :func:`~axpilot.engine.macos.helpers.bring_to_front` for why both at once)."""
        app = _running_app(pid)
        if app is None:
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, f"No running app has pid {pid}.")
        if app.isHidden():
            app.unhide()
        options = AppKit.NSApplicationActivateAllWindows | AppKit.NSApplicationActivateIgnoringOtherApps

        def ax_raise() -> None:
            try:
                self._ax.set(self._ax.application(pid), A.FRONTMOST, True)
            except AXCallError as exc:
                _log.debug("AXFrontmost failed: %s", exc)

        front = h.bring_to_front(
            request=lambda: app.activateWithOptions_(options),
            ax_raise=ax_raise if self._ax.is_trusted() else None,
            is_front=lambda: self.frontmost_pid() == pid,
            wait=_wait,
            timeout_s=ACTIVATE_TIMEOUT_S,
        )
        if not front:
            _log.warning("%s did not come to the front", app.localizedName())
        info = self.info_for_pid(pid)
        if info is None:
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, f"The app with pid {pid} quit.")
        return info

    def _set_hidden(self, pid: int, hidden: bool) -> AppInfo:
        app = _running_app(pid)
        if app is None:
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, f"No running app has pid {pid}.")
        if hidden:
            app.hide()
        else:
            app.unhide()
        if self._ax.is_trusted():
            element = self._ax.application(pid)

            def settled() -> bool:
                try:
                    return A.flag(self._ax.get(element, A.HIDDEN)) == hidden
                except AXCallError:
                    return True

            _wait(settled, ACTIVATE_TIMEOUT_S)
        info = self.info_for_pid(pid)
        if info is None:
            raise AXPilotError(ErrorCode.APP_NOT_FOUND, f"The app with pid {pid} quit.")
        info.hidden = hidden
        return info

    def _quit(self, target: AppInfo, *, force: bool) -> None:
        app = _running_app(target.pid)
        if app is None:
            return None
        if force:
            app.forceTerminate()
        else:
            app.terminate()
        timeout = FORCE_QUIT_TIMEOUT_S if force else QUIT_TIMEOUT_S
        if not _wait(lambda: not process_alive(target.pid), timeout):
            hint = (
                "The app may be asking to save changes: take a snapshot and answer it, or use force_quit."
                if not force
                else "The app is not responding to termination. Ask the user to check it."
            )
            raise AXPilotError(
                ErrorCode.TIMEOUT,
                f"{target.name} did not quit within {timeout:g} s.",
                hint=hint,
                details={"pid": target.pid},
            )
        return None
