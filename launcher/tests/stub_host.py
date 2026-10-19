"""A stand-in for the AXPilot host, used to test the launcher's process contract.

The launcher starts this script instead of ``python -m axpilot.host``. Every start, and every
signal it handles, appends one JSON line to ``--record``, so tests can see how often the host
was started, with which environment, descriptors and signal dispositions, and what it received.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

MARKER_ENV_PREFIXES = ("AXPILOT_", "STUB_")


def record(path: Path, event: str, **data: Any) -> None:
    """Append one event as a JSON line (opened per event, so nothing stays open)."""
    line = json.dumps({"event": event, "time": time.time(), "pid": os.getpid(), **data})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def previous_starts(path: Path) -> int:
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if json.loads(line)["event"] == "start")


def disposition(signum: int) -> str:
    handler = signal.getsignal(signum)
    if handler == signal.SIG_DFL:
        return "default"
    if handler == signal.SIG_IGN:
        return "ignored"
    if handler is signal.default_int_handler:
        return "python-default"
    return "custom"


def open_descriptors(limit: int = 256) -> list[int]:
    found = []
    for fd in range(limit):
        try:
            os.fstat(fd)
        except OSError:
            continue
        found.append(fd)
    return found


def start_facts() -> dict[str, Any]:
    """What the launcher is responsible for: parentage, environment, descriptors, signals."""
    devnull = os.stat(os.devnull)
    stdin = os.fstat(0)
    return {
        "ppid": os.getppid(),
        "pgid": os.getpgid(0),
        "cwd": os.getcwd(),
        "argv": sys.argv,
        "env": {k: v for k, v in os.environ.items() if k.startswith(MARKER_ENV_PREFIXES)},
        "fds": open_descriptors(),
        "stdin_is_devnull": (stdin.st_dev, stdin.st_ino) == (devnull.st_dev, devnull.st_ino),
        "signals": {
            name: disposition(getattr(signal, name)) for name in ("SIGTERM", "SIGHUP", "SIGINT", "SIGQUIT")
        },
        "blocked_signals": sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="JSON-lines event file")
    parser.add_argument(
        "--exit-sequence",
        default="0",
        help="comma-separated exit statuses for successive starts; the last one repeats",
    )
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to run before exiting")
    parser.add_argument("--on-term", choices=["default", "exit", "ignore"], default="default")
    parser.add_argument("--on-hup", choices=["default", "record"], default="default")
    parser.add_argument("--die-by", type=int, help="on the first start, kill ourselves with this signal")
    parser.add_argument("--say", help="write this marker to stdout and stderr")
    return parser.parse_args(argv)


def install_handlers(args: argparse.Namespace) -> None:
    if args.on_term == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif args.on_term == "exit":

        def on_term(signum: int, _frame: object) -> None:
            record(args.record, "signal", signum=signum)
            sys.exit(0)

        signal.signal(signal.SIGTERM, on_term)
    if args.on_hup == "record":
        signal.signal(signal.SIGHUP, lambda signum, _frame: record(args.record, "signal", signum=signum))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start_index = previous_starts(args.record)
    record(args.record, "start", index=start_index, **start_facts())
    if args.say:
        print(f"{args.say} on stdout", flush=True)
        print(f"{args.say} on stderr", file=sys.stderr, flush=True)
    if args.die_by and start_index == 0:
        os.kill(os.getpid(), args.die_by)
        time.sleep(5)  # the signal is delivered before this returns
    install_handlers(args)
    deadline = time.monotonic() + args.sleep
    while time.monotonic() < deadline:
        time.sleep(0.05)  # short naps so Python runs signal handlers promptly
    codes = [int(code) for code in args.exit_sequence.split(",")]
    return codes[min(start_index, len(codes) - 1)]


if __name__ == "__main__":
    sys.exit(main())
