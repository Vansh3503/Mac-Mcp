"""Export the titles of your notes (the Notes list) to Markdown.

    python examples/scripts/export_notes.py --fake            # the simulated desktop
    python examples/scripts/export_notes.py --out notes.md    # your Mac, through AXPilot.app

`read` scrolls through the whole list and joins the pages without repeats, so long lists work too.
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
    parser.add_argument("--out", type=Path, help="write the Markdown here (default: print it)")
    args = parser.parse_args(argv)
    mac = Desktop.fake() if args.fake else Desktop.connect()
    try:
        with mac:
            mac.app("Notes").activate()
            notes = mac.read("Notes", mode="rows")
    except AXPilotError as err:
        print(f"error: {err}", file=sys.stderr)
        if err.hint:
            print(f"hint: {err.hint}", file=sys.stderr)
        return 1
    lines = ["# Notes", "", *(f"- {row}" for row in notes.rows)]
    if not notes.complete:
        lines += ["", f"_Partial list: {'; '.join(notes.warnings)}_"]
    markdown = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
        print(f"wrote {len(notes.rows)} notes to {args.out}")
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
