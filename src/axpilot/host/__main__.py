"""``python -m axpilot.host``: what ``AXPilot.app`` starts (``runtime.json`` ``args``).

The same as ``axpilot host``; extra arguments are passed on (``python -m axpilot.host --engine
fake``)."""

from __future__ import annotations

import sys
from typing import NoReturn

from axpilot.cli import main


def run(argv: list[str] | None = None) -> NoReturn:
    """Run ``axpilot host`` with ``argv`` (default: this process's arguments)."""
    main(["host", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    run()
