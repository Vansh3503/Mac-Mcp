"""Keyboard layout and Secure Event Input through Carbon (HIToolbox), called with ``ctypes``.

PyObjC's Carbon coverage is partial, so the few functions needed are loaded from
``Carbon.framework`` directly:

* ``TISCopyCurrentKeyboardLayoutInputSource`` + ``TISGetInputSourceProperty(…,
  kTISPropertyUnicodeKeyLayoutData)`` + ``UCKeyTranslate`` + ``LMGetKbdType`` build a
  character → (key code, shift, option) map of the user's current layout, so shortcuts such as
  ``cmd+z`` press the right physical key on AZERTY, QWERTZ or Dvorak.
* ``IsSecureEventInputEnabled`` reports Secure Event Input (password fields, "Secure Keyboard
  Entry" in terminals), during which macOS discards synthetic keystrokes.

Permissions: none.

Threading: on macOS 14+ the Text Input Sources (TIS/TSM) functions assert that they run on
the main thread and **crash the process** otherwise. :class:`LayoutProvider` therefore only
calls them on the main thread: directly when it is already there, otherwise by scheduling the
work on the main run loop (``PyObjCTools.AppHelper.callAfter``) and waiting briefly. If the
main thread does not run a Cocoa run loop (e.g. a plain script), the built-in US-ANSI map is
used and a warning is logged once. ``UCKeyTranslate`` itself is a pure function and is safe
anywhere.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable

from axpilot.engine.macos import helpers as h

_log = logging.getLogger(__name__)

CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
CORE_FOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
CORE_SERVICES_PATH = "/System/Library/Frameworks/CoreServices.framework/CoreServices"

K_UC_KEY_ACTION_DOWN = 0
K_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK = 1
K_CF_STRING_ENCODING_UTF8 = 0x08000100
MAX_TRANSLATED_UNITS = 8

LAYOUT_TTL_S = 5.0
"""How long a layout map is reused before it is rebuilt (the user may switch layouts)."""
MAIN_THREAD_WAIT_S = 0.5
"""How long a worker thread waits for the main thread to build the layout map."""


class _Carbon:
    """The ctypes bindings (loaded once, on first use)."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(CARBON_PATH)
        cf = ctypes.CDLL(CORE_FOUNDATION_PATH)
        services = ctypes.CDLL(CORE_SERVICES_PATH)
        void_p = ctypes.c_void_p

        lib.TISCopyCurrentKeyboardLayoutInputSource.restype = void_p
        lib.TISCopyCurrentKeyboardLayoutInputSource.argtypes = []
        lib.TISCopyCurrentASCIICapableKeyboardLayoutInputSource.restype = void_p
        lib.TISCopyCurrentASCIICapableKeyboardLayoutInputSource.argtypes = []
        lib.TISGetInputSourceProperty.restype = void_p
        lib.TISGetInputSourceProperty.argtypes = [void_p, void_p]
        lib.LMGetKbdType.restype = ctypes.c_uint8
        lib.LMGetKbdType.argtypes = []
        services.UCKeyTranslate.restype = ctypes.c_int32
        services.UCKeyTranslate.argtypes = [
            void_p,  # const UCKeyboardLayout *keyLayoutPtr
            ctypes.c_uint16,  # UInt16 virtualKeyCode
            ctypes.c_uint16,  # UInt16 keyAction
            ctypes.c_uint32,  # UInt32 modifierKeyState
            ctypes.c_uint32,  # UInt32 keyboardType
            ctypes.c_uint32,  # OptionBits keyTranslateOptions
            ctypes.POINTER(ctypes.c_uint32),  # UInt32 *deadKeyState
            ctypes.c_ulong,  # UniCharCount maxStringLength
            ctypes.POINTER(ctypes.c_ulong),  # UniCharCount *actualStringLength
            ctypes.POINTER(ctypes.c_uint16),  # UniChar unicodeString[]
        ]
        lib.IsSecureEventInputEnabled.restype = ctypes.c_ubyte
        lib.IsSecureEventInputEnabled.argtypes = []

        cf.CFDataGetBytePtr.restype = void_p
        cf.CFDataGetBytePtr.argtypes = [void_p]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

        self.lib = lib
        self.cf = cf
        self.uc_key_translate = services.UCKeyTranslate
        self.layout_data_key = void_p.in_dll(lib, "kTISPropertyUnicodeKeyLayoutData").value
        self.source_id_key = void_p.in_dll(lib, "kTISPropertyInputSourceID").value


_carbon: _Carbon | None = None
_carbon_lock = threading.Lock()


def _bindings() -> _Carbon:
    global _carbon  # noqa: PLW0603 - one process-wide set of ctypes bindings
    with _carbon_lock:
        if _carbon is None:
            _carbon = _Carbon()
        return _carbon


_secure_input_warned = False


def secure_input_enabled() -> bool | None:
    """``IsSecureEventInputEnabled()``, or ``None`` if Carbon could not be loaded."""
    global _secure_input_warned  # noqa: PLW0603 - warn once per process
    try:
        return bool(_bindings().lib.IsSecureEventInputEnabled())
    except (OSError, AttributeError, ValueError) as exc:
        if not _secure_input_warned:
            _secure_input_warned = True
            _log.warning("IsSecureEventInputEnabled is unavailable: %s", exc)
        return None


# ------------------------------------------------------------------ layout (main thread only)


def _cf_string(c: _Carbon, ref: int | None) -> str | None:
    if not ref:
        return None
    buffer = ctypes.create_string_buffer(256)
    if not c.cf.CFStringGetCString(ref, buffer, len(buffer), K_CF_STRING_ENCODING_UTF8):
        return None
    return buffer.value.decode("utf-8", errors="replace")


def _translator(c: _Carbon, layout_ptr: int, keyboard_type: int) -> Callable[[int, int], str]:
    dead_keys = ctypes.c_uint32(0)
    length = ctypes.c_ulong(0)
    chars = (ctypes.c_uint16 * MAX_TRANSLATED_UNITS)()

    def translate(keycode: int, modifier_state: int) -> str:
        dead_keys.value = 0
        status = c.uc_key_translate(
            layout_ptr,
            keycode,
            K_UC_KEY_ACTION_DOWN,
            modifier_state,
            keyboard_type,
            K_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK,
            ctypes.byref(dead_keys),
            MAX_TRANSLATED_UNITS,
            ctypes.byref(length),
            chars,
        )
        if status != 0 or length.value == 0:
            return ""
        units = min(length.value, MAX_TRANSLATED_UNITS)
        return ctypes.string_at(chars, units * 2).decode("utf-16-le", errors="ignore")

    return translate


def read_current_layout() -> h.KeyboardLayout:
    """Build the map of the current keyboard layout. **Main thread only** (see module doc)."""
    c = _bindings()
    source = c.lib.TISCopyCurrentKeyboardLayoutInputSource()
    data = c.lib.TISGetInputSourceProperty(source, c.layout_data_key) if source else None
    if not data:
        # Input methods without 'uchr' data: use the ASCII-capable layout they type with.
        if source:
            c.cf.CFRelease(source)
        source = c.lib.TISCopyCurrentASCIICapableKeyboardLayoutInputSource()
        data = c.lib.TISGetInputSourceProperty(source, c.layout_data_key) if source else None
    try:
        if not data:
            raise OSError("the current keyboard layout has no Unicode key layout data")
        layout_ptr = c.cf.CFDataGetBytePtr(data)
        if not layout_ptr:
            raise OSError("the keyboard layout data is empty")
        translate = _translator(c, layout_ptr, int(c.lib.LMGetKbdType()))
        chars = h.build_char_map(translate)
        source_id = _cf_string(c, c.lib.TISGetInputSourceProperty(source, c.source_id_key))
    finally:
        if source:
            c.cf.CFRelease(source)
    return h.KeyboardLayout(source_id=source_id, chars=chars)


FALLBACK_LAYOUT = h.KeyboardLayout(
    source_id="com.apple.keylayout.US", chars=h.us_ansi_char_map(), fallback=True
)


class LayoutProvider:
    """Caches the current layout map and refreshes it on the main thread."""

    def __init__(self, ttl_s: float = LAYOUT_TTL_S, wait_s: float = MAIN_THREAD_WAIT_S) -> None:
        self._ttl = ttl_s
        self._wait = wait_s
        self._lock = threading.Lock()
        self._layout: h.KeyboardLayout | None = None
        self._built_at = 0.0
        self._pending: threading.Event | None = None
        self._warned = False

    def current(self) -> h.KeyboardLayout:
        """The current layout (the US-ANSI fallback if it cannot be read in time)."""
        with self._lock:
            layout, fresh = self._layout, time.monotonic() - self._built_at < self._ttl
        if layout is not None and fresh:
            return layout
        if threading.current_thread() is threading.main_thread():
            self._build()
            return self._layout or FALLBACK_LAYOUT
        done, scheduled_now = self._schedule()
        if layout is not None:
            return layout  # slightly stale; the refresh runs in the background
        # Wait only for a job scheduled by this call: if an earlier one is still pending, the
        # main thread is not running a run loop and waiting again would only add latency.
        if done is not None and scheduled_now and done.wait(self._wait) and self._layout is not None:
            return self._layout
        with self._lock:
            if self._layout is not None:
                return self._layout
        self._warn_once("the main thread did not answer in time (is a Cocoa run loop running?)")
        return FALLBACK_LAYOUT

    def _build(self) -> None:
        try:
            layout = read_current_layout()
        except (OSError, AttributeError, ValueError) as exc:
            self._warn_once(str(exc))
            layout = FALLBACK_LAYOUT
        with self._lock:
            self._layout = layout
            self._built_at = time.monotonic()

    def _build_on_main(self) -> None:
        try:
            self._build()
        finally:
            with self._lock:
                pending, self._pending = self._pending, None
            if pending is not None:
                pending.set()

    def _schedule(self) -> tuple[threading.Event | None, bool]:
        """Schedule a rebuild on the main thread. Returns ``(event, scheduled_now)``."""
        with self._lock:
            if self._pending is not None:
                return self._pending, False
            self._pending = threading.Event()
            pending = self._pending
        try:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(self._build_on_main)
        except Exception as exc:  # PyObjC raises objc.error subclasses
            self._warn_once(f"cannot schedule work on the main thread: {exc}")
            with self._lock:
                self._pending = None
            return None, False
        return pending, True

    def _warn_once(self, reason: str) -> None:
        if not self._warned:
            self._warned = True
            _log.warning("Using the US-ANSI key map: could not read the keyboard layout (%s).", reason)
