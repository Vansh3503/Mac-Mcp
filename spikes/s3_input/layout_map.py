#!/usr/bin/env python3
"""Spike S3c: map characters to virtual key codes on the current keyboard layout (UCKeyTranslate).

Shortcuts such as ⌘C must press the key that *produces* "c" on the user's layout (on AZERTY the
"a" key is where QWERTY has "q"). PLAN.md §3 calls Text Input Sources and ``UCKeyTranslate``
through ctypes, because PyObjC's Carbon coverage is partial; this script proves that path:

    python layout_map.py                 # current layout vs US ANSI, plus the shortcut letters
    python layout_map.py --ascii-capable # the layout macOS uses for shortcuts under non-Latin input
    python layout_map.py --check-thread  # does calling TIS from a worker thread crash?

Stdlib + ctypes only (no PyObjC). Switch layouts in System Settings → Keyboard → Input Sources
(add e.g. French, German, Dvorak, Russian, Japanese) and run it once per layout.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s3_input"
SPIKE_DIR = Path(__file__).resolve().parent
FRAMEWORKS = "/System/Library/Frameworks"
UTF8 = 0x08000100
K_UC_KEY_ACTION_DISPLAY = 3
K_UC_KEY_TRANSLATE_NO_DEAD_KEYS = 1 << 0

# Carbon modifier bits (Events.h) shifted right by 8, as UCKeyTranslate expects.
MODIFIERS = {"cmd": 0x01, "shift": 0x02, "caps": 0x04, "option": 0x08, "control": 0x10}
MODIFIER_COMBOS = ((), ("shift",), ("option",), ("shift", "option"))

# kVK_ANSI_* from Events.h: the US layout, which the virtual key codes are named after.
US_ANSI = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50, " ": 49,
}  # fmt: skip
SHORTCUT_CHARS = "acvxzsqwnotfl,.[]/"


# --- pure helpers (unit-tested on any OS) --------------------------------------------------------


def modifier_state(names: tuple[str, ...]) -> int:
    state = 0
    for name in names:
        state |= MODIFIERS[name]
    return state


def build_map(translate: Any, keycodes: range | None = None) -> dict[str, dict[str, Any]]:
    """First (keycode, modifiers) producing each single printable character.

    ``translate(keycode, modifier_state) -> str`` wraps UCKeyTranslate. Combos are tried in order
    (no modifier first), so each character maps to the simplest way to type it.
    """
    mapping: dict[str, dict[str, Any]] = {}
    for combo in MODIFIER_COMBOS:
        state = modifier_state(combo)
        for keycode in keycodes or range(128):
            text = translate(keycode, state)
            if len(text) == 1 and text.isprintable() and text not in mapping:
                mapping[text] = {"keycode": keycode, "modifiers": list(combo)}
    return mapping


def diff_against_us(mapping: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Characters whose unmodified key differs from the US ANSI layout (or that are missing)."""
    differences: dict[str, dict[str, Any]] = {}
    for char, us_keycode in US_ANSI.items():
        entry = mapping.get(char)
        if entry is None:
            differences[char] = {"us": us_keycode, "here": None}
        elif entry["keycode"] != us_keycode or entry["modifiers"]:
            differences[char] = {"us": us_keycode, "here": entry["keycode"], "modifiers": entry["modifiers"]}
    return differences


def shortcut_keys(mapping: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Key codes to press for ⌘+<char>, per character used in common shortcuts."""
    return {char: mapping.get(char, {}).get("keycode") for char in SHORTCUT_CHARS}


# --- Carbon via ctypes (macOS only) --------------------------------------------------------------


class TextInput:
    """Text Input Sources and UCKeyTranslate from Carbon (HIToolbox)."""

    def __init__(self) -> None:
        carbon = ctypes.CDLL(f"{FRAMEWORKS}/Carbon.framework/Carbon")
        cf = ctypes.CDLL(f"{FRAMEWORKS}/CoreFoundation.framework/CoreFoundation")
        self._current = self._fn(carbon, "TISCopyCurrentKeyboardLayoutInputSource", ctypes.c_void_p)
        self._ascii = self._fn(carbon, "TISCopyCurrentASCIICapableKeyboardLayoutInputSource", ctypes.c_void_p)
        self._property = self._fn(
            carbon, "TISGetInputSourceProperty", ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )
        self._kbd_type = self._fn(carbon, "LMGetKbdType", ctypes.c_uint8)
        self._translate = self._fn(
            carbon,
            "UCKeyTranslate",
            ctypes.c_int32,
            ctypes.c_void_p,  # const UCKeyboardLayout *
            ctypes.c_uint16,  # virtualKeyCode
            ctypes.c_uint16,  # keyAction
            ctypes.c_uint32,  # modifierKeyState
            ctypes.c_uint32,  # keyboardType
            ctypes.c_uint32,  # keyTranslateOptions
            ctypes.POINTER(ctypes.c_uint32),  # deadKeyState
            ctypes.c_ulong,  # maxStringLength
            ctypes.POINTER(ctypes.c_ulong),  # actualStringLength
            ctypes.POINTER(ctypes.c_uint16),  # unicodeString
        )
        self._data_bytes = self._fn(cf, "CFDataGetBytePtr", ctypes.c_void_p, ctypes.c_void_p)
        self._release = self._fn(cf, "CFRelease", None, ctypes.c_void_p)
        self._cstring = self._fn(
            cf,
            "CFStringGetCString",
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        )
        self.key_layout_data = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData").value
        self.key_source_id = ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID").value
        self.key_name = ctypes.c_void_p.in_dll(carbon, "kTISPropertyLocalizedName").value

    @staticmethod
    def _fn(lib: Any, name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    def _string(self, ref: int | None) -> str | None:
        if not ref:
            return None
        buffer = ctypes.create_string_buffer(512)
        return buffer.value.decode("utf-8") if self._cstring(ref, buffer, 512, UTF8) else None

    def layout(self, ascii_capable: bool) -> dict[str, Any]:
        """Describe the current (or ASCII-capable) layout and map its characters."""
        source = (self._ascii if ascii_capable else self._current)()
        if not source:
            return {"error": "no keyboard layout input source"}
        try:
            info: dict[str, Any] = {
                "source_id": self._string(self._property(source, self.key_source_id)),
                "name": self._string(self._property(source, self.key_name)),
                "keyboard_type": int(self._kbd_type()),
            }
            data = self._property(source, self.key_layout_data)
            if not data:
                info["error"] = "this input source has no 'uchr' layout data (an input method?)"
                return info
            layout_ptr = self._data_bytes(data)
            keyboard_type = info["keyboard_type"]
            info["map"] = build_map(lambda code, mods: self.translate(layout_ptr, code, mods, keyboard_type))
            return info
        finally:
            self._release(source)

    def translate(self, layout_ptr: int, keycode: int, modifiers: int, keyboard_type: int) -> str:
        dead_keys = ctypes.c_uint32(0)
        length = ctypes.c_ulong(0)
        chars = (ctypes.c_uint16 * 8)()
        status = self._translate(
            layout_ptr,
            keycode,
            K_UC_KEY_ACTION_DISPLAY,
            modifiers,
            keyboard_type,
            K_UC_KEY_TRANSLATE_NO_DEAD_KEYS,
            ctypes.byref(dead_keys),
            len(chars),
            ctypes.byref(length),
            chars,
        )
        if status != 0:
            return ""
        return ctypes.string_at(ctypes.addressof(chars), length.value * 2).decode(
            "utf-16-le", errors="replace"
        )


def check_worker_thread() -> dict[str, Any]:
    """Call TIS from a background thread in a child process and report whether it survived."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_from-worker-thread"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "crashed": result.returncode < 0,
        "ok": result.returncode == 0 and "worker-thread-ok" in result.stdout,
        "stderr_tail": result.stderr[-2000:],
    }


def run_in_worker_thread() -> int:
    outcome: dict[str, Any] = {}

    def work() -> None:
        outcome["layout"] = TextInput().layout(ascii_capable=False).get("source_id")

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    print(f"worker-thread-ok {outcome.get('layout')}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ascii-capable", action="store_true", help="map the ASCII-capable layout instead")
    parser.add_argument("--check-thread", action="store_true", help="also call TIS from a worker thread")
    parser.add_argument("--label", help="scenario name, e.g. the layout under test")
    parser.add_argument("--_from-worker-thread", dest="worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spikelib.require_macos("S3")
    if args.worker:
        return run_in_worker_thread()
    text_input = TextInput()
    report = spikelib.result_header(SPIKE + "_layout", args.label)
    layouts = {"current": text_input.layout(ascii_capable=False), "ascii_capable": text_input.layout(True)}
    report["layouts"] = layouts
    chosen = layouts["ascii_capable" if args.ascii_capable else "current"]
    if "map" in chosen:
        report["differences_from_us"] = diff_against_us(chosen["map"])
        report["shortcut_keys"] = shortcut_keys(chosen["map"])
        print(f"layout: {chosen['name']} ({chosen['source_id']}), keyboard type {chosen['keyboard_type']}")
        differing = len(report["differences_from_us"])
        print(f"characters mapped: {len(chosen['map'])}; differing from US ANSI: {differing}")
        rows = [[c, k, US_ANSI.get(c)] for c, k in report["shortcut_keys"].items()]
        spikelib.print_table(rows, ["char", "keycode here", "keycode on US"])
    else:
        print(f"layout: {chosen.get('name')}: {chosen.get('error')}")
    if args.check_thread:
        report["worker_thread"] = check_worker_thread()
        print(f"TIS from a worker thread: {report['worker_thread']}")
    for path in spikelib.write_results(SPIKE_DIR, report, label=args.label or "layout"):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
