"""Save a screenshot of an app's window with the element refs drawn on it.

    python examples/scripts/annotated_screenshot.py --fake --app Notes --out notes.jpg

Handy for documentation, bug reports and for checking what an agent "sees".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from axpilot import Desktop
from axpilot.errors import AXPilotError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="use the simulated desktop")
    parser.add_argument("--app", default="Notes")
    parser.add_argument("--out", type=Path, default=Path("annotated.jpg"))
    args = parser.parse_args(argv)
    mac = Desktop.fake() if args.fake else Desktop.connect()
    try:
        with mac:
            snap = mac.snapshot(args.app, include_screenshot=True, annotate=True)
    except AXPilotError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if not snap.images:
        print("No screenshot came back (Screen Recording permission?).", file=sys.stderr)
        return 1
    args.out.write_bytes(snap.images[0])
    print(f"saved {args.out} with {len(snap)} labelled elements of {snap.app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
