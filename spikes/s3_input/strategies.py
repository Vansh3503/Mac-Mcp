#!/usr/bin/env python3
"""Spike S3a: which input strategy works in which app?

Clicks: ``AXPress`` (background-safe) vs a CGEvent click at the element's centre (after
activating the app) vs the same events sent with ``CGEventPostToPid`` (app left in the background).
Typing: setting ``AXValue`` / ``AXSelectedText`` vs Unicode CGEvent keystrokes, posted globally or
to the pid.

Requires macOS, ``pip install pyobjc-framework-ApplicationServices``, and Accessibility for the
app running this script.

    python strategies.py click --app Calculator --target 7
    python strategies.py type --app TextEdit
    python strategies.py plan plan.example.json

It acts only on the element you name (``click``) or on the app's focused text element (``type``),
shows it, and asks before acting unless ``--yes``. Effects are detected, not assumed: for clicks,
the text content of the app's window (roles, titles, values) is compared before and after; for
typing, the focused element's value must contain a marker unique to the strategy. Results go to
``results/<timestamp>-<label>.json``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s3_input"
SPIKE_DIR = Path(__file__).resolve().parent
CLICK_STRATEGIES = ("ax_press", "cg_post_to_pid", "cg_click")  # background-safe ones first
TYPE_STRATEGIES = ("ax_set_value", "ax_selected_text", "cg_unicode_pid", "cg_unicode")
READ_ATTRIBUTES = ("AXRole", "AXTitle", "AXDescription", "AXIdentifier", "AXValue", "AXChildren")
SETTLE_S = 0.4
CHUNK = 20  # UTF-16 units per keyboard event (CGEventKeyboardSetUnicodeString is limited)


# --- pure helpers (unit-tested on any OS) --------------------------------------------------------


def label_candidates(info: dict[str, Any]) -> list[str]:
    """The strings a user could call an element by."""
    values = [info.get("AXTitle"), info.get("AXDescription"), info.get("AXIdentifier")]
    if isinstance(info.get("AXValue"), str):
        values.append(info["AXValue"])
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def matches(info: dict[str, Any], needle: str) -> bool:
    wanted = needle.strip().casefold()
    return any(candidate.casefold() == wanted for candidate in label_candidates(info))


def rank_matches(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Buttons first, then other controls, in document order."""
    return sorted(found, key=lambda info: 0 if info.get("AXRole") == "AXButton" else 1)


def fingerprint_changes(before: Sequence[str], after: Sequence[str]) -> int:
    """How many text facts of a window appeared or disappeared."""
    return len(set(before) ^ set(after))


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split ``text`` into pieces of at most ``size`` UTF-16 units without breaking surrogate pairs."""
    chunks: list[str] = []
    current = ""
    for char in text:
        if current and utf16_length(current + char) > size:
            chunks.append(current)
            current = ""
        current += char
    if current:
        chunks.append(current)
    return chunks


def marker_for(strategy: str, base: str) -> str:
    return f"{base} [{strategy}]"


def parse_point(text: str) -> tuple[float, float] | None:
    """Fallback parser for ``str(AXValue)``: ``... x:12.0 y:34.5 ...`` or ``w:.. h:..``."""
    match = re.search(r"(?:x|w):(-?[\d.]+)\s+(?:y|h):(-?[\d.]+)", text)
    return (float(match.group(1)), float(match.group(2))) if match else None


# --- macOS side ----------------------------------------------------------------------------------


class Mac:
    """PyObjC bindings, imported lazily so the helpers above import anywhere."""

    def __init__(self) -> None:
        import AppKit
        import ApplicationServices as ax
        import Quartz

        self.ax, self.appkit, self.quartz = ax, AppKit, Quartz
        ax.AXUIElementSetMessagingTimeout(ax.AXUIElementCreateSystemWide(), 1.0)
        self._point_type = getattr(ax, "kAXValueCGPointType", getattr(ax, "kAXValueTypeCGPoint", 1))
        self._size_type = getattr(ax, "kAXValueCGSizeType", getattr(ax, "kAXValueTypeCGSize", 2))

    # AX reads
    def attribute(self, element: Any, name: str) -> tuple[int, Any]:
        return self.ax.AXUIElementCopyAttributeValue(element, name, None)

    def info(self, element: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name in READ_ATTRIBUTES:
            error, value = self.attribute(element, name)
            if error == 0:
                result[name] = value if name == "AXChildren" else (str(value) if value is not None else None)
        return result

    def iterate(self, root: Any, max_nodes: int = 3000) -> list[tuple[Any, dict[str, Any]]]:
        found: list[tuple[Any, dict[str, Any]]] = []
        stack = [root]
        while stack and len(found) < max_nodes:
            element = stack.pop()
            info = self.info(element)
            found.append((element, info))
            stack.extend(reversed(list(info.get("AXChildren") or [])))
        return found

    def fingerprint(self, root: Any) -> list[str]:
        return [
            f"{info.get('AXRole')}|{info.get('AXTitle')}|{info.get('AXValue')}"
            for _, info in self.iterate(root)
        ]

    def vector(self, element: Any, attribute: str, value_type: int) -> tuple[float, float] | None:
        error, value = self.attribute(element, attribute)
        if error != 0 or value is None:
            return None
        try:
            ok, struct = self.ax.AXValueGetValue(value, value_type, None)
            if ok:
                return (float(struct[0]), float(struct[1]))
        except (TypeError, ValueError, IndexError):
            pass
        return parse_point(str(value))

    def centre(self, element: Any) -> tuple[float, float] | None:
        position = self.vector(element, "AXPosition", self._point_type)
        size = self.vector(element, "AXSize", self._size_type)
        if position is None or size is None:
            return None
        return (position[0] + size[0] / 2, position[1] + size[1] / 2)

    # apps
    def running_app(self, name: str) -> Any:
        for app in self.appkit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.localizedName() or "").casefold() == name.casefold() or app.bundleIdentifier() == name:
                return app
        return None

    def frontmost_bundle_id(self) -> str | None:
        front = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return front.bundleIdentifier() if front is not None else None

    def activate(self, app: Any) -> bool:
        if hasattr(app, "activate"):
            app.activate()  # macOS 14+: cooperative activation
        else:
            app.activateWithOptions_(self.appkit.NSApplicationActivateIgnoringOtherApps)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self.frontmost_bundle_id() == app.bundleIdentifier():
                return True
            time.sleep(0.05)
        return False

    def window(self, app_element: Any) -> Any:
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            error, window = self.attribute(app_element, attribute)
            if error == 0 and window is not None:
                return window
        return app_element

    # input
    def _mouse(self, kind: int, point: tuple[float, float]) -> Any:
        q = self.quartz
        event = q.CGEventCreateMouseEvent(None, kind, point, q.kCGMouseButtonLeft)
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
        return event

    def click_events(self, point: tuple[float, float]) -> list[Any]:
        q = self.quartz
        return [
            self._mouse(k, point)
            for k in (q.kCGEventMouseMoved, q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp)
        ]

    def key_events(self, text: str) -> list[Any]:
        q = self.quartz
        events = []
        for chunk in utf16_chunks(text):
            for down in (True, False):
                event = q.CGEventCreateKeyboardEvent(None, 0, down)
                q.CGEventKeyboardSetUnicodeString(event, utf16_length(chunk), chunk)
                events.append(event)
        return events

    def post(self, events: list[Any], pid: int | None) -> None:
        for event in events:
            if pid is None:
                self.quartz.CGEventPost(self.quartz.kCGHIDEventTap, event)
            else:
                self.quartz.CGEventPostToPid(pid, event)
            time.sleep(0.03)

    def cursor(self) -> tuple[float, float]:
        location = self.quartz.CGEventGetLocation(self.quartz.CGEventCreate(None))
        return (location.x, location.y)

    def warp(self, point: tuple[float, float]) -> None:
        self.quartz.CGWarpMouseCursorPosition(point)
        self.quartz.CGAssociateMouseAndMouseCursorPosition(True)


def secure_input_enabled() -> bool | None:
    """``IsSecureEventInputEnabled()`` from Carbon: when true, synthetic keystrokes are dropped."""
    try:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    except OSError:
        return None
    function = carbon.IsSecureEventInputEnabled
    function.restype = ctypes.c_bool
    return bool(function())


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def attempt_click(mac: Mac, app: Any, element: Any, strategy: str) -> dict[str, Any]:
    pid = int(app.processIdentifier())
    window = mac.window(mac.ax.AXUIElementCreateApplication(pid))
    before = mac.fingerprint(window)
    record: dict[str, Any] = {
        "strategy": strategy,
        "frontmost_before": mac.frontmost_bundle_id(),
        "app_was_frontmost": mac.frontmost_bundle_id() == app.bundleIdentifier(),
    }
    started = time.perf_counter()
    if strategy == "ax_press":
        record["ax_error"] = mac.ax.AXUIElementPerformAction(element, "AXPress")
    else:
        point = mac.centre(element)
        if point is None:
            return {**record, "skipped": "element has no position/size"}
        saved = mac.cursor()
        if strategy == "cg_click":
            record["activated"] = mac.activate(app)
        mac.post(mac.click_events(point), pid if strategy == "cg_post_to_pid" else None)
        mac.warp(saved)
        record["point"] = point
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    time.sleep(SETTLE_S)
    changes = fingerprint_changes(before, mac.fingerprint(window))
    record.update(window_changes=changes, effect=changes > 0)
    return record


def attempt_type(mac: Mac, app: Any, element: Any, strategy: str, base: str) -> dict[str, Any]:
    pid = int(app.processIdentifier())
    marker = marker_for(strategy, base)
    record: dict[str, Any] = {
        "strategy": strategy,
        "marker": marker,
        "app_was_frontmost": mac.frontmost_bundle_id() == app.bundleIdentifier(),
    }
    started = time.perf_counter()
    if strategy == "ax_set_value":
        error, current = mac.attribute(element, "AXValue")
        prefix = str(current) if error == 0 and current is not None else ""
        record["ax_error"] = mac.ax.AXUIElementSetAttributeValue(element, "AXValue", prefix + marker)
    elif strategy == "ax_selected_text":
        record["ax_error"] = mac.ax.AXUIElementSetAttributeValue(element, "AXSelectedText", marker)
    else:
        if strategy == "cg_unicode":
            record["activated"] = mac.activate(app)
        mac.post(mac.key_events(marker), pid if strategy == "cg_unicode_pid" else None)
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    time.sleep(SETTLE_S)
    error, value = mac.attribute(element, "AXValue")
    record["effect"] = error == 0 and marker in str(value)
    return record


def find_target(mac: Mac, app: Any, needle: str) -> tuple[Any, dict[str, Any]] | None:
    window = mac.window(mac.ax.AXUIElementCreateApplication(int(app.processIdentifier())))
    found = [(element, info) for element, info in mac.iterate(window) if matches(info, needle)]
    ranked = rank_matches([info for _, info in found])
    if not ranked:
        return None
    best = ranked[0]
    return next((element, info) for element, info in found if info is best)


def describe(info: dict[str, Any]) -> str:
    return f"{info.get('AXRole')} title={info.get('AXTitle')!r} description={info.get('AXDescription')!r}"


def run_click(mac: Mac, spec: dict[str, Any], assume_yes: bool) -> dict[str, Any]:
    app = mac.running_app(spec["app"])
    if app is None:
        return {"app": spec["app"], "action": "click", "error": "app not running"}
    target = find_target(mac, app, spec["target"])
    if target is None:
        return {"app": spec["app"], "action": "click", "error": f"no element named {spec['target']!r}"}
    element, info = target
    print(f"{spec['app']}: will click {describe(info)} with {', '.join(spec['strategies'])}")
    if not confirm("Proceed?", assume_yes):
        return {"app": spec["app"], "action": "click", "skipped": "declined"}
    attempts = [attempt_click(mac, app, element, s) for s in spec["strategies"]]
    return {"app": spec["app"], "bundle_id": app.bundleIdentifier(), "action": "click", "attempts": attempts}


def run_type(mac: Mac, spec: dict[str, Any], assume_yes: bool) -> dict[str, Any]:
    app = mac.running_app(spec["app"])
    if app is None:
        return {"app": spec["app"], "action": "type", "error": "app not running"}
    if spec.get("field"):
        target = find_target(mac, app, spec["field"])
        if target is None:
            return {"app": spec["app"], "action": "type", "error": f"no element named {spec['field']!r}"}
        element, info = target
    else:
        error, element = mac.attribute(
            mac.ax.AXUIElementCreateApplication(int(app.processIdentifier())), "AXFocusedUIElement"
        )
        if error != 0 or element is None:
            return {"app": spec["app"], "action": "type", "error": f"no focused element (AXError {error})"}
        info = mac.info(element)
    print(f"{spec['app']}: will type into {describe(info)} with {', '.join(spec['strategies'])}")
    if not confirm("Proceed? (use a scratch document: text is inserted)", assume_yes):
        return {"app": spec["app"], "action": "type", "skipped": "declined"}
    base = spec.get("text") or "AXPilot S3 é✓"
    attempts = [attempt_type(mac, app, element, s, base) for s in spec["strategies"]]
    return {"app": spec["app"], "bundle_id": app.bundleIdentifier(), "action": "type", "attempts": attempts}


def load_plan(path: Path) -> list[dict[str, Any]]:
    """Plan entries with defaults filled in; see plan.example.json."""
    entries = json.loads(path.read_text(encoding="utf-8"))["apps"]
    plan = []
    for entry in entries:
        action = entry.get("action", "click")
        if action not in ("click", "type"):
            raise ValueError(f"unknown action {action!r} in {path}")
        if action == "click" and not entry.get("target"):
            raise ValueError(f"click entry for {entry.get('app')!r} needs a target")
        defaults = CLICK_STRATEGIES if action == "click" else TYPE_STRATEGIES
        plan.append({**entry, "action": action, "strategies": list(entry.get("strategies") or defaults)})
    return plan


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--yes", action="store_true", help="do not ask before acting")
    parser.add_argument("--label", help="scenario name recorded in the result")
    commands = parser.add_subparsers(dest="command", required=True)
    click = commands.add_parser("click", help="click one named element with each strategy")
    click.add_argument("--app", required=True)
    click.add_argument("--target", required=True, help="title, description or identifier of the element")
    click.add_argument("--strategies", default=",".join(CLICK_STRATEGIES))
    type_ = commands.add_parser("type", help="type into the focused (or named) text element")
    type_.add_argument("--app", required=True)
    type_.add_argument("--field", help="title/description of the text element (default: the focused one)")
    type_.add_argument("--text", help="base text; each strategy appends its own marker")
    type_.add_argument("--strategies", default=",".join(TYPE_STRATEGIES))
    plan = commands.add_parser("plan", help="run a JSON plan (see plan.example.json)")
    plan.add_argument("path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spikelib.require_macos("S3")
    try:
        mac = Mac()
    except ImportError as exc:
        sys.exit(f"PyObjC is missing ({exc}). Run: pip install pyobjc-framework-ApplicationServices")
    if not mac.ax.AXIsProcessTrusted():
        sys.exit("Grant Accessibility to the app running this script, then rerun.")
    if args.command == "plan":
        plan = load_plan(args.path)
    else:
        spec = {k: v for k, v in vars(args).items() if k in ("app", "target", "field", "text")}
        plan = [{**spec, "action": args.command, "strategies": args.strategies.split(",")}]
    report = spikelib.result_header(SPIKE + "_strategies", args.label)
    report["secure_input_enabled"] = secure_input_enabled()
    if report["secure_input_enabled"]:
        print(
            "WARNING: Secure Event Input is on (a password field or terminal setting); keystrokes will fail."
        )
    report["runs"] = [
        run_click(mac, spec, args.yes) if spec["action"] == "click" else run_type(mac, spec, args.yes)
        for spec in plan
    ]
    rows = [
        [
            run["app"],
            run["action"],
            a["strategy"],
            a.get("ax_error", ""),
            a.get("effect"),
            a.get("elapsed_ms"),
        ]
        for run in report["runs"]
        for a in run.get("attempts", [])
    ]
    spikelib.print_table(rows, ["app", "action", "strategy", "AXError", "effect", "ms"])
    for path in spikelib.write_results(SPIKE_DIR, report, label=args.label or "strategies"):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
