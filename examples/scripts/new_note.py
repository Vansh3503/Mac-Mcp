"""Create a note with a title and a body, then check that it appears in the list.

    python examples/scripts/new_note.py --fake
    python examples/scripts/new_note.py "Groceries" "Milk, eggs"

Uses the menu bar (File → New Note), which is often the most reliable way to drive a Mac app.
"""

from __future__ import annotations

import argparse
import sys

from axpilot import Desktop
from axpilot.errors import AXPilotError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("title", nargs="?", default="Groceries")
    parser.add_argument("body", nargs="?", default="Milk, eggs, coffee")
    parser.add_argument("--fake", action="store_true", help="use the simulated desktop")
    args = parser.parse_args(argv)
    mac = Desktop.fake() if args.fake else Desktop.connect()
    try:
        with mac:
            mac.app("Notes").activate()
            before = len(mac.read("Notes", mode="rows").rows)
            mac.menu("File", "New Note", app="Notes")
            mac.type_text(f"{args.title}\n{args.body}")
            after = mac.read("Notes", mode="rows").rows
    except AXPilotError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if len(after) <= before:
        print("The note list did not grow; check Notes.", file=sys.stderr)
        return 1
    print(f"created a note; the list now has {len(after)} notes (first: {after[0]!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
