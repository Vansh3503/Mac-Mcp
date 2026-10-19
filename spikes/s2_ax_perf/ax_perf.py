#!/usr/bin/env python3
"""Spike S2: how fast can Python walk an app's accessibility tree through PyObjC?

Requires macOS, ``pip install pyobjc-framework-ApplicationServices`` (it pulls in Cocoa and
Quartz), and Accessibility permission for the app that runs this script (your terminal or IDE).

    python ax_perf.py                              # the frontmost app, after a 3 s countdown
    python ax_perf.py --app Finder --app "System Settings" --runs 5
    python ax_perf.py --suite                      # every running app of the S2 suite
    python ax_perf.py --app "Google Chrome" --electron   # also measure AXManualAccessibility

Each run walks the tree depth-first from the focused window (``--scope app``: the whole app)
twice, alternating the order:

* ``naive``: one ``AXUIElementCopyAttributeValue`` call per attribute per element;
* ``batched``: one ``AXUIElementCopyMultipleAttributeValues`` call per element.

It reports nodes, calls, nodes/s, the total time (p50/p95 over runs) and per-node latency
(p50/p95 over all nodes), and writes everything to ``results/<timestamp>.json``.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s2_ax_perf"
SPIKE_DIR = Path(__file__).resolve().parent

# What a snapshot needs per element (see docs/dev/CONTRACTS.md, perception model).
ATTRIBUTES = (
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXIdentifier",
    "AXEnabled",
    "AXFocused",
    "AXPosition",
    "AXSize",
    "AXChildren",
)
CHILDREN_INDEX = ATTRIBUTES.index("AXChildren")

# Heavy and representative apps named in PLAN.md §15 (S2), plus two light baselines.
SUITE_BUNDLE_IDS = (
    "com.apple.finder",
    "com.apple.systempreferences",
    "com.apple.Safari",
    "com.apple.dt.Xcode",
    "com.google.Chrome",
    "com.tinyspeck.slackmacgap",
    "com.microsoft.VSCode",
    "com.apple.TextEdit",
    "com.apple.Notes",
)

# element -> (children, AX calls made, attributes that returned an error)
Reader = Callable[[Any], tuple[list[Any], int, int]]


@dataclass
class Walk:
    """One traversal of one tree."""

    mode: str
    nodes: int = 0
    calls: int = 0
    attribute_errors: int = 0
    max_depth_seen: int = 0
    truncated: bool = False
    total_ms: float = 0.0
    node_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        seconds = self.total_ms / 1000
        return {
            "mode": self.mode,
            "nodes": self.nodes,
            "calls": self.calls,
            "attribute_errors": self.attribute_errors,
            "max_depth_seen": self.max_depth_seen,
            "truncated": self.truncated,
            "total_ms": round(self.total_ms, 2),
            "nodes_per_s": round(self.nodes / seconds, 1) if seconds > 0 else None,
            "node_ms": spikelib.summarize(self.node_ms),
        }


def walk(root: Any, read: Reader, *, mode: str, max_nodes: int, max_depth: int) -> Walk:
    """Depth-first pre-order traversal from ``root`` within the node and depth budgets."""
    result = Walk(mode)
    stack: list[tuple[Any, int]] = [(root, 0)]
    started = time.perf_counter()
    while stack:
        if result.nodes >= max_nodes:
            result.truncated = True
            break
        element, depth = stack.pop()
        node_started = time.perf_counter()
        children, calls, errors = read(element)
        result.node_ms.append((time.perf_counter() - node_started) * 1000)
        result.nodes += 1
        result.calls += calls
        result.attribute_errors += errors
        result.max_depth_seen = max(result.max_depth_seen, depth)
        if depth >= max_depth:
            result.truncated = result.truncated or bool(children)
            continue
        stack.extend((child, depth + 1) for child in reversed(children))
    result.total_ms = (time.perf_counter() - started) * 1000
    return result


def compare_modes(walks: Sequence[Walk]) -> dict[str, Any]:
    """Per-mode aggregates over several runs, plus the batched speed-up."""
    by_mode: dict[str, list[Walk]] = {}
    for item in walks:
        by_mode.setdefault(item.mode, []).append(item)
    modes: dict[str, Any] = {}
    for mode, items in by_mode.items():
        totals = [w.total_ms for w in items]
        nodes = max(w.nodes for w in items)
        median_s = spikelib.percentile(totals, 50) / 1000
        modes[mode] = {
            "runs": len(items),
            "nodes": nodes,
            "calls": max(w.calls for w in items),
            "truncated": any(w.truncated for w in items),
            "total_ms": spikelib.summarize(totals),
            "nodes_per_s_p50": round(nodes / median_s, 1) if median_s > 0 else None,
            "node_ms": spikelib.summarize([ms for w in items for ms in w.node_ms]),
        }
    if "naive" in modes and "batched" in modes:
        batched = modes["batched"]["total_ms"]["p50"]
        modes["speedup_batched_vs_naive"] = (
            round(modes["naive"]["total_ms"]["p50"] / batched, 2) if batched else None
        )
    return modes


# --- PyObjC side (macOS only) --------------------------------------------------------------------


class AXApi:
    """Thin wrapper over PyObjC's ApplicationServices bindings (imported lazily)."""

    def __init__(self, timeout_s: float) -> None:
        import ApplicationServices as ax
        from CoreFoundation import CFGetTypeID

        self.ax = ax
        self._type_id = CFGetTypeID
        self._ax_value_type = ax.AXValueGetTypeID()
        self._error_type = getattr(ax, "kAXValueAXErrorType", getattr(ax, "kAXValueTypeAXError", 5))
        ax.AXUIElementSetMessagingTimeout(ax.AXUIElementCreateSystemWide(), timeout_s)

    def trusted(self) -> bool:
        return bool(self.ax.AXIsProcessTrusted())

    def is_error_value(self, value: Any) -> bool:
        """Batched reads return an AXValue of type kAXValueAXErrorType for failed attributes."""
        if value is None:
            return False
        try:
            return (
                self._type_id(value) == self._ax_value_type
                and self.ax.AXValueGetType(value) == self._error_type
            )
        except (TypeError, ValueError):
            return False

    def attribute(self, element: Any, name: str) -> tuple[int, Any]:
        return self.ax.AXUIElementCopyAttributeValue(element, name, None)

    def naive(self, element: Any) -> tuple[list[Any], int, int]:
        children: list[Any] = []
        errors = 0
        for name in ATTRIBUTES:
            error, value = self.attribute(element, name)
            if error != 0:
                errors += 1
            elif name == "AXChildren" and value:
                children = list(value)
        return children, len(ATTRIBUTES), errors

    def batched(self, element: Any) -> tuple[list[Any], int, int]:
        error, values = self.ax.AXUIElementCopyMultipleAttributeValues(element, ATTRIBUTES, 0, None)
        if error != 0 or values is None:
            return [], 1, len(ATTRIBUTES)
        errors = sum(1 for value in values if self.is_error_value(value))
        children = values[CHILDREN_INDEX]
        if children is None or self.is_error_value(children):
            return [], 1, errors
        return list(children), 1, errors

    def application(self, pid: int) -> Any:
        return self.ax.AXUIElementCreateApplication(pid)

    def root(self, app_element: Any, scope: str) -> tuple[Any, str]:
        """The traversal root: the focused (or main, or first) window, or the app itself."""
        if scope == "app":
            return app_element, "app"
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            error, window = self.attribute(app_element, attribute)
            if error == 0 and window is not None:
                return window, attribute
        error, windows = self.attribute(app_element, "AXWindows")
        if error == 0 and windows:
            return windows[0], "AXWindows[0]"
        return app_element, "app (no window)"

    def set_flag(self, element: Any, name: str, value: bool) -> int:
        return self.ax.AXUIElementSetAttributeValue(element, name, value)


@dataclass
class Target:
    name: str
    bundle_id: str | None
    pid: int


def running_applications() -> list[Any]:
    from AppKit import NSWorkspace

    return list(NSWorkspace.sharedWorkspace().runningApplications())


def to_target(app: Any) -> Target:
    return Target(str(app.localizedName()), app.bundleIdentifier(), int(app.processIdentifier()))


def resolve_targets(args: argparse.Namespace) -> list[Target]:
    apps = running_applications()
    targets: list[Target] = []
    for pid in args.pid:
        targets += [to_target(a) for a in apps if a.processIdentifier() == pid]
    for name in args.app:
        targets += [to_target(a) for a in apps if (a.localizedName() or "").lower() == name.lower()]
    for bundle_id in args.bundle_id:
        targets += [to_target(a) for a in apps if a.bundleIdentifier() == bundle_id]
    if args.suite:
        targets += [to_target(a) for a in apps if a.bundleIdentifier() in SUITE_BUNDLE_IDS]
    if not (args.pid or args.app or args.bundle_id or args.suite):
        for remaining in range(args.delay, 0, -1):
            print(f"Bring the app to measure to the front: {remaining} s", flush=True)
            time.sleep(1)
        from AppKit import NSWorkspace

        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            targets.append(to_target(front))
    unique = {t.pid: t for t in targets}
    return list(unique.values())


def measure_target(api: AXApi, target: Target, args: argparse.Namespace) -> dict[str, Any]:
    app_element = api.application(target.pid)
    root, root_source = api.root(app_element, args.scope)
    walks: list[Walk] = []
    for run in range(args.runs):
        order = ("naive", "batched") if run % 2 == 0 else ("batched", "naive")
        for mode in order:
            reader = api.naive if mode == "naive" else api.batched
            walks.append(walk(root, reader, mode=mode, max_nodes=args.max_nodes, max_depth=args.max_depth))
    result: dict[str, Any] = {
        "app": target.name,
        "bundle_id": target.bundle_id,
        "pid": target.pid,
        "scope": args.scope,
        "root": root_source,
        "modes": compare_modes(walks),
        "walks": [w.summary() for w in walks],
    }
    if args.electron:
        result["manual_accessibility"] = manual_accessibility_test(api, app_element, args)
    return result


def manual_accessibility_test(api: AXApi, app_element: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Node counts before and after setting AXManualAccessibility, then AXEnhancedUserInterface."""

    def count() -> int:
        root, _ = api.root(app_element, args.scope)
        return walk(
            root, api.batched, mode="batched", max_nodes=args.max_nodes, max_depth=args.max_depth
        ).nodes

    original = {
        name: api.attribute(app_element, name)
        for name in ("AXManualAccessibility", "AXEnhancedUserInterface")
    }
    report: dict[str, Any] = {
        "original": {
            name: {"error": err, "value": bool(val) if err == 0 else None}
            for name, (err, val) in original.items()
        },
        "nodes_before": count(),
    }
    for flag in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        report[f"set_{flag}_error"] = api.set_flag(app_element, flag, True)
        time.sleep(args.settle)
        report[f"nodes_after_{flag}"] = count()
    if not args.keep_flags:
        for name, (err, value) in original.items():
            api.set_flag(app_element, name, bool(value) if err == 0 else False)
        report["restored"] = True
    return report


def print_report(results: Sequence[dict[str, Any]]) -> None:
    rows = []
    for item in results:
        for mode in ("naive", "batched"):
            stats = item["modes"].get(mode)
            if not stats:
                continue
            rows.append(
                [
                    item["app"],
                    mode,
                    stats["nodes"],
                    stats["calls"],
                    stats["total_ms"]["p50"],
                    stats["total_ms"]["p95"],
                    stats["nodes_per_s_p50"],
                    stats["node_ms"]["p50"],
                    stats["node_ms"]["p95"],
                    "yes" if stats["truncated"] else "",
                ]
            )
    header = [
        "app",
        "mode",
        "nodes",
        "calls",
        "total p50 ms",
        "total p95 ms",
        "nodes/s",
        "node p50",
        "node p95",
        "trunc",
    ]
    spikelib.print_table(rows, header)
    for item in results:
        speedup = item["modes"].get("speedup_batched_vs_naive")
        print(f"{item['app']}: batched is {speedup}x faster than naive (p50 totals)")
        if "manual_accessibility" in item:
            print(f"{item['app']}: AXManualAccessibility test: {item['manual_accessibility']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--app", action="append", default=[], help="app name as shown in the Dock (repeatable)"
    )
    parser.add_argument("--bundle-id", action="append", default=[], help="bundle identifier (repeatable)")
    parser.add_argument("--pid", action="append", type=int, default=[], help="process id (repeatable)")
    parser.add_argument("--suite", action="store_true", help="every running app of the S2 suite")
    parser.add_argument("--delay", type=int, default=3, help="countdown before reading the frontmost app")
    parser.add_argument("--scope", choices=("window", "app"), default="window")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-nodes", type=int, default=5000)
    parser.add_argument("--max-depth", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=1.0, help="AX messaging timeout in seconds")
    parser.add_argument("--electron", action="store_true", help="also test AXManualAccessibility")
    parser.add_argument("--settle", type=float, default=2.0, help="seconds to wait after setting a flag")
    parser.add_argument("--keep-flags", action="store_true", help="do not restore the flags afterwards")
    parser.add_argument("--label", help="scenario name recorded in the result")
    parser.add_argument("--out-file", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spikelib.require_macos("S2")
    try:
        api = AXApi(args.timeout)
    except ImportError as exc:
        sys.exit(f"PyObjC is missing ({exc}). Run: pip install pyobjc-framework-ApplicationServices")
    if not api.trusted():
        sys.exit(
            "This process has no Accessibility permission. Grant it to the app running this script "
            "(System Settings → Privacy & Security → Accessibility) and rerun."
        )
    targets = resolve_targets(args)
    if not targets:
        sys.exit("No matching running app.")
    results = []
    for target in targets:
        print(f"Measuring {target.name} (pid {target.pid}) ...", flush=True)
        results.append(measure_target(api, target, args))
    report = spikelib.result_header(SPIKE, args.label)
    report["parameters"] = {k: v for k, v in vars(args).items() if k != "out_file"}
    report["attributes"] = list(ATTRIBUTES)
    report["targets"] = results
    print_report(results)
    for path in spikelib.write_results(SPIKE_DIR, report, label=args.label, out_file=args.out_file):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
