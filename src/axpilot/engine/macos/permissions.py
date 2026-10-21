"""Permission status (Accessibility, Screen Recording, Automation) and the responsible app.

Nothing here prompts the user:

* Accessibility: ``AXIsProcessTrusted()``.
* Screen Recording: ``CGPreflightScreenCaptureAccess()``. macOS caches the answer for the
  life of the process, so a grant made while AXPilot runs shows up after a restart.
* Automation: ``AEDeterminePermissionToAutomateTarget(…, askUserIfNeeded=false)`` for a few
  common, *running* targets (System Events, Finder). Targets that are not running are omitted.
* Responsible app: macOS charges permissions to the "responsible" app of a process (the
  terminal for a shell, Claude for an MCP server it spawned, AXPilot.app for the host). We
  walk our parent processes (``ps``) to the nearest app bundle, which is what TCC uses in
  practice. It is best effort and cached for the life of the engine.

:func:`request_accessibility` and :func:`request_screen_recording` *do* prompt; they are for
the onboarding UI only.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from collections.abc import Iterable

import HIServices as HI
import Quartz as Q

from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.ax import AX
from axpilot.models import PermissionStatus
from axpilot.system.runner import CommandRunner

_log = logging.getLogger(__name__)

CORE_SERVICES_PATH = "/System/Library/Frameworks/CoreServices.framework/CoreServices"
TYPE_APPLICATION_BUNDLE_ID = 0x62756E64  # 'bund'
TYPE_WILDCARD = 0x2A2A2A2A  # '****'
NO_ERR = 0
ERR_AE_EVENT_NOT_PERMITTED = -1743
ERR_AE_WOULD_REQUIRE_USER_CONSENT = -1744
PROC_NOT_FOUND = -600
AUTOMATION_TARGETS: tuple[str, ...] = ("com.apple.systemevents", "com.apple.finder")


class _AEDesc(ctypes.Structure):
    _fields_ = (("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p))


class _AppleEvents:
    def __init__(self) -> None:
        lib = ctypes.CDLL(CORE_SERVICES_PATH)
        lib.AECreateDesc.restype = ctypes.c_int16
        lib.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc)]
        lib.AEDisposeDesc.restype = ctypes.c_int16
        lib.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
        lib.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
        lib.AEDeterminePermissionToAutomateTarget.argtypes = [
            ctypes.POINTER(_AEDesc),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_ubyte,
        ]
        self.lib = lib

    def status(self, bundle_id: str) -> int:
        raw = bundle_id.encode("utf-8")
        buffer = ctypes.create_string_buffer(raw, len(raw))
        desc = _AEDesc()
        err = self.lib.AECreateDesc(TYPE_APPLICATION_BUNDLE_ID, buffer, len(raw), ctypes.byref(desc))
        if err != NO_ERR:
            return int(err)
        try:
            return int(
                self.lib.AEDeterminePermissionToAutomateTarget(
                    ctypes.byref(desc), TYPE_WILDCARD, TYPE_WILDCARD, 0
                )
            )
        finally:
            self.lib.AEDisposeDesc(ctypes.byref(desc))


_apple_events: _AppleEvents | None = None
_apple_events_failed = False
_ae_lock = threading.Lock()


def automation_status(bundle_ids: Iterable[str], running: set[str]) -> tuple[dict[str, bool], list[str]]:
    """Whether we may send Apple events to each *running* target (no prompts), and the targets
    macOS has not asked the user about yet (``-1744``; ``-1743`` means denied)."""
    global _apple_events, _apple_events_failed  # noqa: PLW0603 - one process-wide set of bindings
    result: dict[str, bool] = {}
    unasked: list[str] = []
    with _ae_lock:
        if _apple_events_failed:
            return result, unasked
        try:
            if _apple_events is None:
                _apple_events = _AppleEvents()
        except (OSError, AttributeError) as exc:
            _apple_events_failed = True
            _log.warning("cannot check Automation permission: %s", exc)
            return result, unasked
        events = _apple_events
    for bundle_id in bundle_ids:
        if bundle_id not in running:
            continue
        code = events.status(bundle_id)
        if code == NO_ERR:
            result[bundle_id] = True
        elif code in (ERR_AE_EVENT_NOT_PERMITTED, ERR_AE_WOULD_REQUIRE_USER_CONSENT):
            result[bundle_id] = False
            if code == ERR_AE_WOULD_REQUIRE_USER_CONSENT:
                unasked.append(bundle_id)
        elif code != PROC_NOT_FOUND:
            _log.debug("AEDeterminePermissionToAutomateTarget(%s) returned %d", bundle_id, code)
    return result, unasked


def responsible_app(runner: CommandRunner) -> str | None:
    """Best-effort name of the app macOS attributes our permission checks to."""
    result = runner.run(["ps", "-axo", "pid=,ppid=,comm="], timeout_s=5.0, max_output=2_000_000)
    if not result.ok:
        _log.debug("ps failed: %s", result.stderr.strip()[:200])
        return None
    table = h.parse_ps_table(result.stdout)
    return h.responsible_app_name(executable for _, executable in h.ancestry(os.getpid(), table))


def screen_recording_allowed() -> bool:
    return bool(Q.CGPreflightScreenCaptureAccess())


def permission_status(ax: AX, *, responsible: str | None, running_bundle_ids: set[str]) -> PermissionStatus:
    """Current status (never prompts)."""
    accessibility = ax.is_trusted()
    screen = screen_recording_allowed()
    automation, unasked = automation_status(AUTOMATION_TARGETS, running_bundle_ids)
    who = responsible or "the app that runs AXPilot"
    notes: list[str] = []
    if not accessibility:
        notes.append(f"Accessibility is not granted to {who}; the UI cannot be read or controlled.")
    if not screen:
        notes.append(
            f"Screen Recording is not granted to {who}; screenshots and OCR are unavailable. "
            "macOS only notices a new grant after AXPilot restarts."
        )
    notes.extend(h.automation_notes(automation, unasked))
    return PermissionStatus(
        accessibility=accessibility,
        screen_recording=screen,
        automation=automation,
        responsible_app=responsible,
        notes=notes,
        automation_unasked=sorted(unasked),
    )


def request_accessibility() -> bool:
    """Show the system Accessibility prompt if needed (onboarding only). Returns the status."""
    return bool(HI.AXIsProcessTrustedWithOptions({HI.kAXTrustedCheckOptionPrompt: True}))


def request_screen_recording() -> bool:
    """Show the system Screen Recording prompt if needed (onboarding only)."""
    return bool(Q.CGRequestScreenCaptureAccess())
