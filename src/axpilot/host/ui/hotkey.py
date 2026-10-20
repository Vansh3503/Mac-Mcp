"""A system-wide hotkey through Carbon's ``RegisterEventHotKey``, called with ``ctypes``.

``RegisterEventHotKey`` needs no permission (unlike event taps and global ``NSEvent`` monitors),
takes the key combination away from other apps while AXPilot runs, and delivers
``kEventHotKeyPressed`` to the application event target. The events are dispatched while
AppKit's event queue is drained, which :class:`axpilot.host.ui.app.AppKitPump` does on the main
thread.

Permissions: none. Threading: main thread only. **Not run on a real Mac yet** (Linux CI only
checks that it compiles): the event dispatch through the pump, behaviour on macOS 14 to 27, and
the key position on non-US layouts are open checks in the handoff.
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable

from axpilot.input.hotkeys import Hotkey

__all__ = ["CarbonHotkey"]

_log = logging.getLogger(__name__)

CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
K_EVENT_CLASS_KEYBOARD = int.from_bytes(b"keyb", "big")
K_EVENT_HOT_KEY_PRESSED = 5
SIGNATURE = int.from_bytes(b"AXPL", "big")
NO_ERR = 0


class _EventTypeSpec(ctypes.Structure):
    _fields_ = (("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32))


class _EventHotKeyID(ctypes.Structure):
    _fields_ = (("signature", ctypes.c_uint32), ("id", ctypes.c_uint32))


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class CarbonHotkey:
    """Calls ``callback`` on the main thread each time ``hotkey`` is pressed, until :meth:`close`."""

    def __init__(self, hotkey: Hotkey, callback: Callable[[], object]) -> None:
        self.hotkey = hotkey
        self._callback = callback
        lib = ctypes.CDLL(CARBON_PATH)
        void_p = ctypes.c_void_p
        lib.GetApplicationEventTarget.restype = void_p
        lib.GetApplicationEventTarget.argtypes = []
        lib.InstallEventHandler.restype = ctypes.c_int32
        lib.InstallEventHandler.argtypes = [
            void_p, _HANDLER, ctypes.c_ulong, ctypes.POINTER(_EventTypeSpec), void_p, ctypes.POINTER(void_p)
        ]  # fmt: skip
        lib.RegisterEventHotKey.restype = ctypes.c_int32
        lib.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, void_p, ctypes.c_uint32, ctypes.POINTER(void_p)
        ]  # fmt: skip
        lib.UnregisterEventHotKey.restype = ctypes.c_int32
        lib.UnregisterEventHotKey.argtypes = [void_p]
        lib.RemoveEventHandler.restype = ctypes.c_int32
        lib.RemoveEventHandler.argtypes = [void_p]
        self._lib = lib
        self._handler = _HANDLER(self._on_event)  # kept alive for as long as it is installed
        self._handler_ref = ctypes.c_void_p()
        self._hotkey_ref = ctypes.c_void_p()
        target = lib.GetApplicationEventTarget()
        spec = _EventTypeSpec(K_EVENT_CLASS_KEYBOARD, K_EVENT_HOT_KEY_PRESSED)
        status = lib.InstallEventHandler(
            target, self._handler, 1, ctypes.byref(spec), None, ctypes.byref(self._handler_ref)
        )
        if status != NO_ERR:
            raise OSError(f"InstallEventHandler failed with status {status}")
        hotkey_id = _EventHotKeyID(SIGNATURE, 1)
        status = lib.RegisterEventHotKey(
            hotkey.key_code, hotkey.carbon_modifiers, hotkey_id, target, 0, ctypes.byref(self._hotkey_ref)
        )
        if status != NO_ERR:
            lib.RemoveEventHandler(self._handler_ref)
            raise OSError(
                f"RegisterEventHotKey({hotkey.label}) failed with status {status}; another app may own it"
            )

    def _on_event(self, _next: int | None, _event: int | None, _data: int | None) -> int:
        try:
            self._callback()
        except Exception:  # never raise into Carbon's dispatcher (QUALITY.md A7)
            _log.exception("The kill-switch handler failed")
        return NO_ERR

    def close(self) -> None:
        """Unregister the hotkey and remove the handler. Safe to call twice."""
        if self._hotkey_ref.value:
            self._lib.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = ctypes.c_void_p()
        if self._handler_ref.value:
            self._lib.RemoveEventHandler(self._handler_ref)
            self._handler_ref = ctypes.c_void_p()
