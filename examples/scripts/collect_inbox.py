"""Collect every message line of the Mail inbox into a CSV file.

    python examples/scripts/collect_inbox.py --fake --out inbox.csv

`read` scrolls the message list page by page (restoring the scroll position afterwards) and
reports whether it got everything (`complete`).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from axpilot import Desktop
from axpilot.errors import AXPilotError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="use the simulated desktop")
    parser.add_argument("--out", type=Path, default=Path("inbox.csv"))
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args(argv)
    mac = Desktop.fake() if args.fake else Desktop.connect()
    try:
        with mac:
            mac.app("Mail").launch().activate()
            inbox = mac.read("Mail", mode="rows", max_pages=args.max_pages, restore=True)
    except AXPilotError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["message"])
        writer.writerows([row] for row in inbox.rows)
    state = "all" if inbox.complete else "part of"
    print(f"saved {state} the inbox: {len(inbox.rows)} messages in {inbox.pages} pages → {args.out}")
    return 0 if inbox.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
