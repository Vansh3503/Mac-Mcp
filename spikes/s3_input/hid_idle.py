#!/usr/bin/env python3
"""Spike S3b: do AXPilot's own synthetic events reset the "user is active" idle timer?

Yield-to-user (PLAN.md §5.3) pauses when the human used the mouse or keyboard recently, measured
with ``CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, ...)``. That only
works if events *we* post do not count as human input. This script posts harmless synthetic
events (a mouse move to the current cursor position; with ``--keys`` also F18 key presses) for
every combination of event tap and event-source state, while you keep your hands off, and records
both idle clocks:

* HID system state: should keep growing (our events are invisible to it) for the combo we adopt;
* combined session state: is expected to reset (it includes synthetic events).

``--interactive`` adds a positive control: you move the mouse and the HID clock must reset.

Requires macOS, ``pip install pyobjc-framework-Quartz``, and Accessibility for the app running it
(posting events needs it).
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s3_input"
SPIKE_DIR = Path(__file__).resolve().parent
TAPS = ("hid", "session", "annotated_session")
SOURCES = ("none", "private", "combined_session", "hid_system")
F18_KEYCODE = 79

Sample = tuple[float, float, float]  # (seconds since start, HID idle, combined-session idle)


def analyze(samples: Sequence[Sample], tolerance: float = 0.25) -> dict[str, Any]:
    """Whether each idle clock was reset during a hands-off series.

    An idle clock that nobody resets grows with wall time, so between two samples it must grow
    by the elapsed time (minus ``tolerance`` for sampling jitter). Any larger drop is a reset.
    """
    resets = {"hid": 0, "combined": 0}
    for (t0, hid0, comb0), (t1, hid1, comb1) in itertools.pairwise(samples):
        elapsed = t1 - t0
        if hid1 < hid0 + elapsed - tolerance:
            resets["hid"] += 1
        if comb1 < comb0 + elapsed - tolerance:
            resets["combined"] += 1
    return {
        "samples": len(samples),
        "hid_resets": resets["hid"],
        "combined_resets": resets["combined"],
        "hid_unaffected": resets["hid"] == 0,
        "hid_min_idle": round(min(s[1] for s in samples), 3) if samples else None,
        "combined_min_idle": round(min(s[2] for s in samples), 3) if samples else None,
    }


def saw_human_input(samples: Sequence[Sample], threshold: float = 1.0) -> bool:
    """Positive control: during real input the HID idle time must drop below ``threshold``."""
    return any(hid < threshold for _, hid, _ in samples)


class Events:
    """Quartz event helpers (PyObjC, imported lazily)."""

    def __init__(self) -> None:
        import Quartz

        self.q = Quartz
        self.taps = {
            "hid": Quartz.kCGHIDEventTap,
            "session": Quartz.kCGSessionEventTap,
            "annotated_session": Quartz.kCGAnnotatedSessionEventTap,
        }
        self.states = {
            "private": Quartz.kCGEventSourceStatePrivate,
            "combined_session": Quartz.kCGEventSourceStateCombinedSessionState,
            "hid_system": Quartz.kCGEventSourceStateHIDSystemState,
        }

    def idle(self) -> tuple[float, float]:
        q = self.q
        return (
            q.CGEventSourceSecondsSinceLastEventType(
                q.kCGEventSourceStateHIDSystemState, q.kCGAnyInputEventType
            ),
            q.CGEventSourceSecondsSinceLastEventType(
                q.kCGEventSourceStateCombinedSessionState, q.kCGAnyInputEventType
            ),
        )

    def source(self, name: str) -> Any:
        return None if name == "none" else self.q.CGEventSourceCreate(self.states[name])

    def post_noop(self, tap: str, source_name: str, keys: bool) -> None:
        q = self.q
        source = self.source(source_name)
        location = q.CGEventGetLocation(q.CGEventCreate(None))
        events = [q.CGEventCreateMouseEvent(source, q.kCGEventMouseMoved, location, q.kCGMouseButtonLeft)]
        if keys:
            events += [q.CGEventCreateKeyboardEvent(source, F18_KEYCODE, down) for down in (True, False)]
        for event in events:
            q.CGEventPost(self.taps[tap], event)


def series(events: Events, duration: float, interval: float, post: Any = None) -> list[Sample]:
    samples: list[Sample] = []
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < duration:
        if post is not None:
            post()
        time.sleep(min(0.05, interval))
        hid, combined = events.idle()
        samples.append((round(elapsed, 3), round(hid, 3), round(combined, 3)))
        time.sleep(max(0.0, interval - 0.05))
    return samples


def countdown(message: str, seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"{message} ({remaining})", flush=True)
        time.sleep(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--duration", type=float, default=8.0, help="seconds per combination")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between synthetic events")
    parser.add_argument("--taps", default=",".join(TAPS))
    parser.add_argument("--sources", default=",".join(SOURCES))
    parser.add_argument(
        "--keys", action="store_true", help="also post F18 key presses (check F18 is unmapped)"
    )
    parser.add_argument("--interactive", action="store_true", help="add the move-the-mouse positive control")
    parser.add_argument("--label")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    spikelib.require_macos("S3")
    try:
        events = Events()
    except ImportError as exc:
        sys.exit(f"PyObjC is missing ({exc}). Run: pip install pyobjc-framework-Quartz")
    report = spikelib.result_header(SPIKE + "_hid_idle", args.label)
    report["parameters"] = vars(args)
    report["combos"] = []
    print("Keep your hands off the mouse, trackpad and keyboard until told otherwise.")
    countdown("starting", 3)
    baseline = series(events, args.duration / 2, args.interval)
    report["baseline"] = {"samples": baseline, **analyze(baseline)}
    for tap in args.taps.split(","):
        for source in args.sources.split(","):
            samples = series(
                events,
                args.duration,
                args.interval,
                lambda t=tap, s=source: events.post_noop(t, s, args.keys),
            )
            result = {"tap": tap, "source": source, **analyze(samples), "series": samples}
            report["combos"].append(result)
            print(f"tap={tap:<18} source={source:<17} HID unaffected={result['hid_unaffected']}")
    if args.interactive:
        print("Now move the mouse continuously.")
        countdown("go", 1)
        human = series(events, 3.0, 0.2)
        report["positive_control"] = {"samples": human, "saw_human_input": saw_human_input(human)}
        saw = report["positive_control"]["saw_human_input"]
        print(f"Positive control: real input reset the HID clock = {saw}")
    rows = [
        [c["tap"], c["source"], c["hid_resets"], c["combined_resets"], c["hid_unaffected"]]
        for c in report["combos"]
    ]
    spikelib.print_table(rows, ["tap", "source", "HID resets", "combined resets", "HID unaffected"])
    for path in spikelib.write_results(SPIKE_DIR, report, label=args.label or "hid-idle"):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
