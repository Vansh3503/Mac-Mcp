#!/usr/bin/env python3
"""Spike S1 probe: what may this process do, and which app does macOS hold responsible for it?

Run it twice to answer ADR-2's go/no-go question:

* directly from a terminal or MCP client (the *control*: permissions belong to that app), and
* as the child of ``AXPilot.app`` via ``run_s1.sh`` (the *treatment*: they should belong to
  AXPilot.app, through the launcher's posix_spawn).

It uses only the standard library and ctypes, so any Python 3.9+ runs it, including the Command
Line Tools' ``/usr/bin/python3``; no PyObjC. It records:

* ``AXIsProcessTrusted()`` (optionally prompting) and a real AX read: the frontmost app's focused
  window title via ``AXUIElementCreateApplication`` + ``AXUIElementCopyAttributeValue``;
* ``CGPreflightScreenCaptureAccess()`` (optionally requesting) and whether other apps' window
  titles are visible in ``CGWindowListCopyWindowInfo`` (they are only with Screen Recording);
* the responsible process (``responsibility_get_pid_responsible_for_pid``, a private libSystem
  call that may be missing) and the parent chain, with executable paths;
* the launcher bundle's extended attributes (``com.apple.quarantine``), code signature and
  executable bit.

Results go to ``results/<timestamp>-<label>.json`` next to this file (or ``--out-file``) and to
``~/Library/Application Support/AXPilot/spike-s1.json``. The exit status is 0 unless ``--strict``
is given and the verdict is "fail", so the launcher never restarts the probe.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import os
import platform
import plistlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import c_bool, c_int, c_long, c_uint32, c_void_p
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s1_launcher_tcc"
SPIKE_DIR = Path(__file__).resolve().parent
PROBE_VERSION = 1
LAUNCHER_BUNDLE_ID = "io.github.vansh3503.axpilot"
QUARANTINE_XATTR = "com.apple.quarantine"
FRAMEWORKS = "/System/Library/Frameworks"
LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
ENV_KEYS = ("__CFBundleIdentifier", "XPC_SERVICE_NAME", "TERM_PROGRAM", "PATH", "VIRTUAL_ENV", "PYTHONPATH")
MAX_CHAIN = 32
MAX_BUNDLE_ENTRIES = 5000

AX_ERRORS = {
    0: "kAXErrorSuccess",
    -25200: "kAXErrorFailure",
    -25201: "kAXErrorIllegalArgument",
    -25202: "kAXErrorInvalidUIElement",
    -25203: "kAXErrorInvalidUIElementObserver",
    -25204: "kAXErrorCannotComplete",
    -25205: "kAXErrorAttributeUnsupported",
    -25206: "kAXErrorActionUnsupported",
    -25207: "kAXErrorNotificationUnsupported",
    -25208: "kAXErrorNotImplemented",
    -25209: "kAXErrorNotificationAlreadyRegistered",
    -25210: "kAXErrorNotificationNotRegistered",
    -25211: "kAXErrorAPIDisabled",
    -25212: "kAXErrorNoValue",
    -25213: "kAXErrorParameterizedAttributeUnsupported",
    -25214: "kAXErrorNotEnoughPrecision",
}
AX_API_DISABLED = -25211

UTF8 = 0x08000100  # kCFStringEncodingUTF8
CF_NUMBER_SINT64 = 4  # kCFNumberSInt64Type
PROC_PIDTBSDINFO = 3
PROC_PIDPATHINFO_MAXSIZE = 4096
XATTR_NOFOLLOW = 0x0001
CG_WINDOW_LIST_ON_SCREEN_ONLY = 1 << 0
CG_WINDOW_LIST_EXCLUDE_DESKTOP = 1 << 4


def _declare(lib: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    function = getattr(lib, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


# --- native bindings (macOS only) ----------------------------------------------------------------


class CoreFoundation:
    """The few CoreFoundation calls the probe needs."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(f"{FRAMEWORKS}/CoreFoundation.framework/CoreFoundation")
        self.release = _declare(lib, "CFRelease", None, c_void_p)
        self.type_id = _declare(lib, "CFGetTypeID", ctypes.c_ulong, c_void_p)
        self.string_type_id = _declare(lib, "CFStringGetTypeID", ctypes.c_ulong)()
        self.number_type_id = _declare(lib, "CFNumberGetTypeID", ctypes.c_ulong)()
        self._create_string = _declare(
            lib, "CFStringCreateWithCString", c_void_p, c_void_p, ctypes.c_char_p, c_uint32
        )
        self._length = _declare(lib, "CFStringGetLength", c_long, c_void_p)
        self._max_size = _declare(lib, "CFStringGetMaximumSizeForEncoding", c_long, c_long, c_uint32)
        self._get_cstring = _declare(
            lib, "CFStringGetCString", c_bool, c_void_p, ctypes.c_char_p, c_long, c_uint32
        )
        self._number_value = _declare(
            lib, "CFNumberGetValue", c_bool, c_void_p, c_int, ctypes.POINTER(ctypes.c_int64)
        )
        self.array_count = _declare(lib, "CFArrayGetCount", c_long, c_void_p)
        self.array_item = _declare(lib, "CFArrayGetValueAtIndex", c_void_p, c_void_p, c_long)
        self.dict_get = _declare(lib, "CFDictionaryGetValue", c_void_p, c_void_p, c_void_p)
        self._dict_create = _declare(
            lib,
            "CFDictionaryCreate",
            c_void_p,
            c_void_p,
            ctypes.POINTER(c_void_p),
            ctypes.POINTER(c_void_p),
            c_long,
            c_void_p,
            c_void_p,
        )
        self.true = c_void_p.in_dll(lib, "kCFBooleanTrue").value
        self._key_callbacks = ctypes.addressof(ctypes.c_char.in_dll(lib, "kCFTypeDictionaryKeyCallBacks"))
        self._value_callbacks = ctypes.addressof(ctypes.c_char.in_dll(lib, "kCFTypeDictionaryValueCallBacks"))

    def string(self, text: str) -> int:
        """A new CFStringRef (release it with ``release``)."""
        return self._create_string(None, text.encode("utf-8"), UTF8)

    def to_str(self, ref: int | None) -> str | None:
        if not ref or self.type_id(ref) != self.string_type_id:
            return None
        size = self._max_size(self._length(ref), UTF8) + 1
        buffer = ctypes.create_string_buffer(size)
        if not self._get_cstring(ref, buffer, size, UTF8):
            return None
        return buffer.value.decode("utf-8", errors="replace")

    def to_int(self, ref: int | None) -> int | None:
        if not ref or self.type_id(ref) != self.number_type_id:
            return None
        value = ctypes.c_int64()
        return value.value if self._number_value(ref, CF_NUMBER_SINT64, ctypes.byref(value)) else None

    def dictionary(self, items: dict[int, int]) -> int:
        """A new CFDictionaryRef from CF keys and values (release it with ``release``)."""
        keys = (c_void_p * len(items))(*items.keys())
        values = (c_void_p * len(items))(*items.values())
        return self._dict_create(None, keys, values, len(items), self._key_callbacks, self._value_callbacks)


class Accessibility:
    """AX calls from ApplicationServices (HIServices)."""

    def __init__(self, cf: CoreFoundation) -> None:
        lib = ctypes.CDLL(f"{FRAMEWORKS}/ApplicationServices.framework/ApplicationServices")
        self.cf = cf
        self.is_trusted = _declare(lib, "AXIsProcessTrusted", c_bool)
        self.is_trusted_with_options = _declare(lib, "AXIsProcessTrustedWithOptions", c_bool, c_void_p)
        self.create_application = _declare(lib, "AXUIElementCreateApplication", c_void_p, c_int)
        self.create_system_wide = _declare(lib, "AXUIElementCreateSystemWide", c_void_p)
        self._copy_value = _declare(
            lib, "AXUIElementCopyAttributeValue", ctypes.c_int32, c_void_p, c_void_p, ctypes.POINTER(c_void_p)
        )
        self.set_timeout = _declare(
            lib, "AXUIElementSetMessagingTimeout", ctypes.c_int32, c_void_p, ctypes.c_float
        )
        self._get_pid = _declare(lib, "AXUIElementGetPid", ctypes.c_int32, c_void_p, ctypes.POINTER(c_int))
        self.prompt_option = c_void_p.in_dll(lib, "kAXTrustedCheckOptionPrompt").value

    def copy_attribute(self, element: int, name: str) -> tuple[int, int | None]:
        """``(AXError, value)``; the caller releases a non-None value."""
        key = self.cf.string(name)
        value = c_void_p()
        try:
            error = self._copy_value(element, key, ctypes.byref(value))
        finally:
            self.cf.release(key)
        return error, value.value

    def pid_of(self, element: int) -> int | None:
        pid = c_int()
        return pid.value if self._get_pid(element, ctypes.byref(pid)) == 0 else None


class Graphics:
    """CoreGraphics: Screen Recording preflight/request and the window list."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(f"{FRAMEWORKS}/CoreGraphics.framework/CoreGraphics")
        self.preflight = _declare(lib, "CGPreflightScreenCaptureAccess", c_bool)
        self.request = _declare(lib, "CGRequestScreenCaptureAccess", c_bool)
        self.window_list = _declare(lib, "CGWindowListCopyWindowInfo", c_void_p, c_uint32, c_uint32)
        self.key_owner_pid = c_void_p.in_dll(lib, "kCGWindowOwnerPID").value
        self.key_name = c_void_p.in_dll(lib, "kCGWindowName").value
        self.key_layer = c_void_p.in_dll(lib, "kCGWindowLayer").value


class ProcBSDInfo(ctypes.Structure):
    """``struct proc_bsdinfo`` from <sys/proc_info.h>."""

    _fields_ = [
        ("pbi_flags", c_uint32),
        ("pbi_status", c_uint32),
        ("pbi_xstatus", c_uint32),
        ("pbi_pid", c_uint32),
        ("pbi_ppid", c_uint32),
        ("pbi_uid", c_uint32),
        ("pbi_gid", c_uint32),
        ("pbi_ruid", c_uint32),
        ("pbi_rgid", c_uint32),
        ("pbi_svuid", c_uint32),
        ("pbi_svgid", c_uint32),
        ("rfu_1", c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", c_uint32),
        ("pbi_pgid", c_uint32),
        ("pbi_pjobc", c_uint32),
        ("e_tdev", c_uint32),
        ("e_tpgid", c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class LibSystem:
    """libproc, extended attributes, and the private responsibility call."""

    def __init__(self) -> None:
        lib = ctypes.CDLL(LIBSYSTEM, use_errno=True)
        self._pidpath = _declare(lib, "proc_pidpath", c_int, c_int, ctypes.c_char_p, c_uint32)
        self._pidinfo = _declare(lib, "proc_pidinfo", c_int, c_int, c_int, ctypes.c_uint64, c_void_p, c_int)
        self._listxattr = _declare(
            lib, "listxattr", ctypes.c_ssize_t, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, c_int
        )
        self._getxattr = _declare(
            lib,
            "getxattr",
            ctypes.c_ssize_t,
            ctypes.c_char_p,
            ctypes.c_char_p,
            c_void_p,
            ctypes.c_size_t,
            c_uint32,
            c_int,
        )
        try:
            self.responsible_pid: Callable[[int], int] | None = _declare(
                lib, "responsibility_get_pid_responsible_for_pid", c_int, c_int
            )
        except AttributeError:
            self.responsible_pid = None

    def pid_path(self, pid: int) -> str | None:
        buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        length = self._pidpath(pid, buffer, PROC_PIDPATHINFO_MAXSIZE)
        return buffer.value.decode("utf-8", errors="replace") if length > 0 else None

    def bsd_info(self, pid: int) -> ProcBSDInfo | None:
        info = ProcBSDInfo()
        size = self._pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
        return info if size == ctypes.sizeof(info) else None

    def list_xattrs(self, path: str) -> list[str] | None:
        encoded = os.fsencode(path)
        size = self._listxattr(encoded, None, 0, XATTR_NOFOLLOW)
        if size < 0:
            return None
        if size == 0:
            return []
        buffer = ctypes.create_string_buffer(size)
        size = self._listxattr(encoded, buffer, size, XATTR_NOFOLLOW)
        return [n.decode("utf-8", errors="replace") for n in buffer.raw[:size].split(b"\0") if n]

    def get_xattr(self, path: str, name: str) -> bytes | None:
        encoded, key = os.fsencode(path), name.encode("utf-8")
        size = self._getxattr(encoded, key, None, 0, 0, XATTR_NOFOLLOW)
        if size < 0:
            return None
        buffer = ctypes.create_string_buffer(max(size, 1))
        size = self._getxattr(encoded, key, buffer, size, 0, XATTR_NOFOLLOW)
        return buffer.raw[:size] if size >= 0 else None


class Native:
    """Whatever native bindings could be loaded; each is ``None`` when unavailable."""

    def __init__(self) -> None:
        self.errors: dict[str, str] = {}
        self.cf = self._load("CoreFoundation", CoreFoundation)
        self.ax = self._load("ApplicationServices", lambda: Accessibility(self.cf)) if self.cf else None
        self.cg = self._load("CoreGraphics", Graphics)
        self.system = self._load("libSystem", LibSystem)

    def _load(self, name: str, factory: Callable[[], Any]) -> Any:
        try:
            return factory()
        except (OSError, AttributeError, ValueError) as exc:
            self.errors[name] = f"{type(exc).__name__}: {exc}"
            return None


# --- pure helpers (unit-tested on any OS) --------------------------------------------------------


def ax_error(code: int) -> dict[str, Any]:
    return {"code": code, "name": AX_ERRORS.get(code, "unknown")}


def bundle_of(executable: str | None) -> str | None:
    """The innermost ``.app`` bundle whose main executable is ``executable``."""
    if not executable:
        return None
    match = re.match(r"^(.*\.app)/Contents/MacOS/[^/]+$", executable)
    return match.group(1) if match else None


def parse_lsappinfo_pid(text: str | None) -> int | None:
    """The pid from ``lsappinfo info -only pid <ASN>`` output such as ``"pid"=1234``."""
    match = re.search(r'"?pid"?\s*=\s*(\d+)', text or "")
    return int(match.group(1)) if match else None


def parse_quarantine(raw: bytes | None) -> dict[str, Any] | None:
    """Decode ``flags;hex-timestamp;agent;uuid`` from a com.apple.quarantine value."""
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    parts = text.split(";")
    decoded: dict[str, Any] = {"raw": text}
    if len(parts) >= 3:
        decoded["flags"] = parts[0]
        decoded["agent"] = parts[2] or None
        try:
            stamp = _dt.datetime.fromtimestamp(int(parts[1], 16), _dt.timezone.utc)  # noqa: UP017 - py3.9
            decoded["time"] = stamp.isoformat()
        except (ValueError, OverflowError, OSError):
            decoded["time"] = None
    return decoded


def parse_codesign(text: str | None) -> dict[str, str]:
    """``key=value`` lines of ``codesign -dvvv`` output (stderr)."""
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("Identifier", "Format", "Signature", "TeamIdentifier", "CDHash", "Executable"):
            fields[key] = value.strip()
    return fields


def parent_chain(pid: int, lookup: Callable[[int], dict[str, Any] | None]) -> list[dict[str, Any]]:
    """``lookup(pid)`` for ``pid`` and each ancestor up to launchd (pid 1), bounded and cycle-safe."""
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    while pid and pid not in seen and len(chain) < MAX_CHAIN:
        seen.add(pid)
        entry = lookup(pid) or {"pid": pid, "ppid": None, "path": None, "name": None}
        chain.append(entry)
        if pid == 1 or not entry.get("ppid"):
            break
        pid = entry["ppid"]
    return chain


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    """The verdict for ADR-2. A run outside the launcher is a "control" and never passes or fails."""
    ax = report["accessibility"]
    screen = report["screen_recording"]
    responsibility = report["responsibility"]
    bundle = report["bundle"]
    under_launcher = report["launcher"]["under_launcher"]
    expected = bundle.get("path")
    responsible_bundle = responsibility.get("bundle")
    if not responsibility.get("available") or not expected:
        responsible_is_launcher = None
    else:
        responsible_is_launcher = bool(responsible_bundle) and os.path.realpath(
            responsible_bundle
        ) == os.path.realpath(expected)
    summary: dict[str, Any] = {
        "under_launcher": under_launcher,
        "ax_trusted": ax.get("trusted"),
        "ax_read_ok": ax.get("read_ok"),
        "screen_recording_preflight": screen.get("preflight"),
        "other_app_window_titles_visible": screen.get("other_app_window_titles_visible"),
        "responsible_path": responsibility.get("path"),
        "responsible_is_launcher": responsible_is_launcher,
        "bundle_quarantined": bundle.get("quarantined"),
    }
    reasons: list[str] = []
    if not ax.get("available"):
        verdict = "inconclusive"
        reasons.append("the Accessibility API could not be loaded (not macOS?)")
    elif not under_launcher:
        verdict = "control"
    else:
        if not ax.get("trusted"):
            reasons.append(
                "AXIsProcessTrusted() is false: grant Accessibility to AXPilot, or it was not inherited"
            )
        elif not ax.get("read_ok"):
            reasons.append("trusted, but reading the frontmost app over AX failed")
        if responsible_is_launcher is False:
            reasons.append(f"macOS holds {responsibility.get('path')} responsible, not the AXPilot bundle")
        if bundle.get("quarantined"):
            reasons.append("the AXPilot bundle carries com.apple.quarantine")
        verdict = "fail" if reasons else "pass"
    summary["verdict"] = verdict
    summary["reasons"] = reasons
    return summary


# --- probes --------------------------------------------------------------------------------------


def process_entry(native: Native, pid: int) -> dict[str, Any] | None:
    """pid, ppid, name and executable path, via libproc (macOS) or ``ps`` (elsewhere)."""
    if native.system is not None:
        info = native.system.bsd_info(pid)
        if info is None:
            return None
        return {
            "pid": pid,
            "ppid": int(info.pbi_ppid),
            "name": (info.pbi_name or info.pbi_comm).decode("utf-8", errors="replace"),
            "path": native.system.pid_path(pid),
        }
    text = spikelib.run_text(["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)])
    if not text:
        return None
    ppid, _, command = text.strip().partition(" ")
    return {
        "pid": pid,
        "ppid": int(ppid) if ppid.isdigit() else None,
        "name": Path(command.strip()).name,
        "path": None,
    }


def probe_process(native: Native) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uid": os.getuid(),
        "executable": sys.executable,
        "executable_realpath": os.path.realpath(sys.executable),
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "env": {k: v for k, v in os.environ.items() if k in ENV_KEYS or k.startswith("AXPILOT_")},
        "parent_chain": parent_chain(os.getpid(), lambda pid: process_entry(native, pid)),
    }


def probe_responsibility(native: Native) -> dict[str, Any]:
    if native.system is None or native.system.responsible_pid is None:
        return {"available": False, "error": "responsibility_get_pid_responsible_for_pid is unavailable"}
    pid = int(native.system.responsible_pid(os.getpid()))
    if pid <= 0:
        return {"available": False, "error": f"responsibility_get_pid_responsible_for_pid returned {pid}"}
    path = native.system.pid_path(pid)
    return {
        "available": True,
        "pid": pid,
        "path": path,
        "bundle": bundle_of(path),
        "is_self": pid == os.getpid(),
    }


def frontmost_pid(native: Native) -> tuple[int | None, str]:
    """The frontmost app's pid: ``lsappinfo`` needs no permission; AX is the fallback."""
    asn = spikelib.run_text(["lsappinfo", "front"])
    if asn:
        pid = parse_lsappinfo_pid(spikelib.run_text(["lsappinfo", "info", "-only", "pid", asn]))
        if pid:
            return pid, "lsappinfo"
    if native.ax is not None:
        system_wide = native.ax.create_system_wide()
        error, app = native.ax.copy_attribute(system_wide, "AXFocusedApplication")
        native.cf.release(system_wide)
        if error == 0 and app:
            pid = native.ax.pid_of(app)
            native.cf.release(app)
            return pid, "AXFocusedApplication"
    return None, "unavailable"


def read_string_attribute(native: Native, element: int, name: str) -> tuple[str | None, dict[str, Any]]:
    error, value = native.ax.copy_attribute(element, name)
    try:
        return (native.cf.to_str(value) if error == 0 else None), ax_error(error)
    finally:
        if value:
            native.cf.release(value)


def probe_accessibility(
    native: Native, *, prompt: bool, wait_for_trust: float, pid: int | None
) -> dict[str, Any]:
    ax, cf = native.ax, native.cf
    if ax is None or cf is None:
        return {
            "available": False,
            "error": native.errors.get("ApplicationServices") or native.errors.get("CoreFoundation"),
        }
    result: dict[str, Any] = {"available": True, "prompted": prompt}
    if prompt:
        options = cf.dictionary({ax.prompt_option: cf.true})
        ax.is_trusted_with_options(options)
        cf.release(options)
    trusted = bool(ax.is_trusted())
    waited = 0.0
    started = time.monotonic()
    while not trusted and waited < wait_for_trust:
        time.sleep(1.0)
        waited = time.monotonic() - started
        trusted = bool(ax.is_trusted())
    result["trusted"] = trusted
    result["waited_for_trust_s"] = round(waited, 1)

    system_wide = ax.create_system_wide()
    ax.set_timeout(system_wide, 2.0)  # applies to every AX call of this process; hung apps cannot block us
    cf.release(system_wide)
    source = "argument"
    if pid is None:
        pid, source = frontmost_pid(native)
    result["target_pid"], result["target_source"] = pid, source
    if pid is None:
        result["read_ok"] = False
        return result

    errors: dict[str, Any] = {}
    app = ax.create_application(pid)
    try:
        result["app_title"], errors["app.AXTitle"] = read_string_attribute(native, app, "AXTitle")
        code, window = ax.copy_attribute(app, "AXFocusedWindow")
        errors["app.AXFocusedWindow"] = ax_error(code)
        if code == 0 and window:
            try:
                result["window_title"], errors["window.AXTitle"] = read_string_attribute(
                    native, window, "AXTitle"
                )
                result["window_role"], errors["window.AXRole"] = read_string_attribute(
                    native, window, "AXRole"
                )
            finally:
                cf.release(window)
    finally:
        cf.release(app)
    result["errors"] = errors
    codes = [entry["code"] for entry in errors.values()]
    result["api_disabled"] = AX_API_DISABLED in codes
    result["read_ok"] = 0 in codes and not result["api_disabled"]
    return result


def probe_screen_recording(native: Native, *, request: bool) -> dict[str, Any]:
    cg, cf = native.cg, native.cf
    if cg is None or cf is None:
        return {
            "available": False,
            "error": native.errors.get("CoreGraphics") or native.errors.get("CoreFoundation"),
        }
    result: dict[str, Any] = {"available": True, "preflight": bool(cg.preflight()), "requested": False}
    if request and not result["preflight"]:
        result["requested"] = True
        result["request_result"] = bool(cg.request())
    windows = cg.window_list(CG_WINDOW_LIST_ON_SCREEN_ONLY | CG_WINDOW_LIST_EXCLUDE_DESKTOP, 0)
    other, named = 0, 0
    if windows:
        try:
            for index in range(cf.array_count(windows)):
                entry = cf.array_item(windows, index)
                owner = cf.to_int(cf.dict_get(entry, cg.key_owner_pid))
                layer = cf.to_int(cf.dict_get(entry, cg.key_layer))
                if owner == os.getpid() or layer != 0:
                    continue
                other += 1
                if cf.to_str(cf.dict_get(entry, cg.key_name)):
                    named += 1
        finally:
            cf.release(windows)
    result["other_app_windows"] = other
    result["other_app_windows_with_titles"] = named
    result["other_app_window_titles_visible"] = named > 0 if other else None
    return result


def xattr_names(native: Native, path: Path) -> list[str] | None:
    if hasattr(os, "listxattr"):  # Linux
        try:
            return list(os.listxattr(path, follow_symlinks=False))
        except OSError:
            return None
    return native.system.list_xattrs(str(path)) if native.system else None


def xattr_value(native: Native, path: Path, name: str) -> bytes | None:
    if hasattr(os, "getxattr"):  # Linux
        try:
            return os.getxattr(path, name, follow_symlinks=False)
        except OSError:
            return None
    return native.system.get_xattr(str(path), name) if native.system else None


def probe_bundle(native: Native, app: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(app), "exists": app.is_dir()}
    if not app.is_dir():
        return result
    result["xattrs"] = xattr_names(native, app)
    result["quarantine"] = parse_quarantine(xattr_value(native, app, QUARANTINE_XATTR))
    quarantined_files: list[str] = []
    for index, path in enumerate(app.rglob("*")):
        if index >= MAX_BUNDLE_ENTRIES:
            break
        if QUARANTINE_XATTR in (xattr_names(native, path) or []):
            quarantined_files.append(str(path.relative_to(app)))
    result["quarantined_files"] = quarantined_files[:20]
    result["quarantined"] = bool(result["quarantine"]) or bool(quarantined_files)
    executable = app / "Contents" / "MacOS" / "AXPilot"
    result["executable_is_executable"] = os.access(executable, os.X_OK)
    try:
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        result["bundle_id"] = info.get("CFBundleIdentifier")
        result["version"] = info.get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        result["info_plist_error"] = str(exc)
    if sys.platform == "darwin":
        result["codesign"] = {
            arch: parse_codesign(codesign_details(app, arch)) for arch in ("arm64", "x86_64")
        }
        result["codesign_verify_ok"] = codesign_verify(app)
    return result


def codesign_details(app: Path, arch: str) -> str | None:
    try:
        result = subprocess.run(
            ["codesign", "-dvvv", "-a", arch, str(app)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stderr


def codesign_verify(app: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--strict", str(app)], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def default_app_path() -> Path:
    bundle = os.environ.get("AXPILOT_LAUNCHER_BUNDLE")
    return Path(bundle) if bundle else Path.home() / "Applications" / "AXPilot.app"


def collect(args: argparse.Namespace, native: Native | None = None) -> dict[str, Any]:
    """Run every probe and return the report (never raises for missing native APIs)."""
    native = native or Native()
    report = spikelib.result_header(SPIKE, args.label)
    report["probe_version"] = PROBE_VERSION
    report["native_load_errors"] = native.errors
    report["launcher"] = {
        "under_launcher": "AXPILOT_LAUNCHER_VERSION" in os.environ,
        "version": os.environ.get("AXPILOT_LAUNCHER_VERSION"),
        "bundle": os.environ.get("AXPILOT_LAUNCHER_BUNDLE"),
    }
    report["process"] = probe_process(native)
    report["responsibility"] = probe_responsibility(native)
    report["accessibility"] = probe_accessibility(
        native, prompt=args.prompt, wait_for_trust=args.wait_for_trust, pid=args.pid
    )
    report["screen_recording"] = probe_screen_recording(native, request=args.request_screen)
    report["bundle"] = probe_bundle(native, Path(args.app) if args.app else default_app_path())
    report["summary"] = summarize_report(report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", help="scenario name recorded in the result, e.g. fresh-grant")
    parser.add_argument("--prompt", action="store_true", help="ask for Accessibility (system prompt)")
    parser.add_argument(
        "--request-screen", action="store_true", help="ask for Screen Recording (system prompt)"
    )
    parser.add_argument(
        "--wait-for-trust",
        type=float,
        default=0.0,
        metavar="S",
        help="poll AXIsProcessTrusted for up to S seconds",
    )
    parser.add_argument("--pid", type=int, help="read this app over AX instead of the frontmost one")
    parser.add_argument(
        "--app", help="the AXPilot bundle to inspect (default: the launcher's, else ~/Applications)"
    )
    parser.add_argument("--out-file", type=Path, help="write the result here instead of results/<stamp>.json")
    parser.add_argument(
        "--no-app-support-copy", action="store_true", help="skip the copy in Application Support"
    )
    parser.add_argument("--strict", action="store_true", help="exit 1 when the verdict is 'fail'")
    return parser.parse_args(argv)


def print_summary(report: dict[str, Any], written: list[Path]) -> None:
    summary = report["summary"]
    macos = report["environment"].get("macos")
    print(f"S1 probe: verdict={summary['verdict']} label={report['label']!r} macOS={macos}")
    for key in (
        "under_launcher",
        "ax_trusted",
        "ax_read_ok",
        "screen_recording_preflight",
        "other_app_window_titles_visible",
        "responsible_path",
        "responsible_is_launcher",
        "bundle_quarantined",
    ):
        print(f"  {key}: {summary[key]}")
    ax = report["accessibility"]
    if ax.get("available"):
        titles = f"{ax.get('app_title')!r} / {ax.get('window_title')!r}"
        print(f"  frontmost app / window: {titles} (pid {ax.get('target_pid')})")
    for reason in summary["reasons"]:
        print(f"  reason: {reason}")
    for path in written:
        print(f"  wrote {path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = collect(args)
    extra = [] if args.no_app_support_copy else [spikelib.app_support_dir() / "spike-s1.json"]
    written = spikelib.write_results(
        SPIKE_DIR, report, label=args.label, out_file=args.out_file, extra_paths=extra
    )
    print_summary(report, written)
    sys.stdout.flush()
    return 1 if args.strict and report["summary"]["verdict"] == "fail" else 0


if __name__ == "__main__":
    if platform.system() != "Darwin":
        print("note: not macOS; native probes are reported as unavailable", file=sys.stderr)
    sys.exit(main())
