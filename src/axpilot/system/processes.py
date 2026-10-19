"""List and signal processes.

Listing uses ``ps -axo pid=,ppid=,%cpu=,%mem=,comm=``: ``comm`` is the executable path without
arguments, so command-line secrets (tokens, passwords in flags) never reach the model. The last
column may contain spaces (``/Applications/Google Chrome.app/…``), so each line is split into at
most five fields.

:func:`kill_process` refuses targets that would hurt the system or AXPilot itself: pid ≤ 1
(``0`` and negative ids address process groups, ``1`` is launchd), AXPilot's own process and its
parent (the launcher or the MCP client).
"""

from __future__ import annotations

import logging
import os
import signal
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandRunner, ensure_success, output_records

__all__ = [
    "MAX_LIMIT",
    "PS_ARGV",
    "ProcessInfo",
    "check_kill_allowed",
    "kill_process",
    "list_processes",
    "parse_ps_line",
    "process_exists",
]

_log = logging.getLogger(__name__)

PS_ARGV: tuple[str, ...] = ("ps", "-axo", "pid=,ppid=,%cpu=,%mem=,comm=")
MAX_LIMIT = 1000
PS_TIMEOUT_S = 10.0
_PS_MAX_OUTPUT = 2_000_000


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """One running process. ``cpu`` and ``mem`` are percentages as reported by ``ps``."""

    pid: int
    ppid: int
    cpu: float
    mem: float
    command: str

    @property
    def name(self) -> str:
        """The executable's base name (``"Safari"`` for ``…/Safari.app/Contents/MacOS/Safari``)."""
        return PurePosixPath(self.command).name or self.command

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dict, including :attr:`name`."""
        return {**asdict(self), "name": self.name}


def _parse_percent(text: str) -> float:
    return float(text.replace(",", "."))  # some locales print 0,5


def parse_ps_line(line: str) -> ProcessInfo | None:
    """Parse one line of ``ps -o pid=,ppid=,%cpu=,%mem=,comm=`` output; ``None`` if malformed."""
    parts = line.strip().split(None, 4)
    if len(parts) < 5:
        return None
    try:
        return ProcessInfo(
            pid=int(parts[0]),
            ppid=int(parts[1]),
            cpu=_parse_percent(parts[2]),
            mem=_parse_percent(parts[3]),
            command=parts[4].strip(),
        )
    except ValueError:
        return None


def list_processes(
    runner: CommandRunner,
    *,
    name: str | None = None,
    limit: int = 50,
) -> list[ProcessInfo]:
    """Running processes, busiest first (CPU, then memory, then pid).

    ``name`` keeps processes whose executable path contains it (case-insensitive). At most
    ``limit`` (1–1000) entries are returned. Unparseable lines are skipped.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"limit must be an integer between 1 and {MAX_LIMIT}, got {limit!r}."
        )
    if name is not None and not isinstance(name, str):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "name must be text.")
    result = runner.run(list(PS_ARGV), timeout_s=PS_TIMEOUT_S, max_output=_PS_MAX_OUTPUT)
    ensure_success(result, what="Listing processes (ps)", timeout_s=PS_TIMEOUT_S)

    processes: list[ProcessInfo] = []
    for line in output_records(result.stdout):
        if not line.strip():
            continue
        info = parse_ps_line(line)
        if info is None:
            _log.debug("skipping unparseable ps line: %r", line[:200])
            continue
        processes.append(info)

    needle = name.strip().casefold() if name else ""
    if needle:
        processes = [p for p in processes if needle in p.command.casefold()]
    processes.sort(key=lambda p: (-p.cpu, -p.mem, p.pid))
    return processes[:limit]


def kill_process(pid: int, *, force: bool = False) -> str:
    """Send ``SIGTERM`` (or ``SIGKILL`` with ``force``) to ``pid``; returns the signal name.

    Raises ``BLOCKED_BY_POLICY`` for pid ≤ 1, AXPilot's own pid and its parent's pid,
    ``INVALID_ARGUMENT`` if there is no such process and ``COMMAND_FAILED`` if the OS refuses
    (another user's or a protected process).
    """
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"pid must be an integer, got {pid!r}.")
    check_kill_allowed(pid)
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"No process with pid {pid}.",
            hint="It may have exited already. Call `processes` to list current processes.",
        ) from exc
    except PermissionError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"macOS did not allow signalling pid {pid} (it belongs to another user or is protected).",
            hint="AXPilot can only stop processes owned by the current user.",
        ) from exc
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED, f"Could not signal pid {pid}: {exc.strerror or exc}"
        ) from exc
    _log.info("sent %s to pid %d", sig.name, pid)
    return sig.name


def process_exists(pid: int) -> bool:
    """True if a process with ``pid`` exists (signal 0: nothing is sent). A process of another
    user counts as existing."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def check_kill_allowed(pid: int) -> None:
    """Raise ``BLOCKED_BY_POLICY`` if :func:`kill_process` would refuse ``pid`` (pid ≤ 1,
    AXPilot itself or its parent), so callers can refuse before asking the user."""
    hint = "This safety rule cannot be turned off. Quit the app normally or ask the user."
    if pid <= 1:
        raise AXPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Refusing to signal pid {pid}: pid 1 is launchd, and 0 or negative ids address "
            "whole process groups.",
            hint=hint,
        )
    if pid == os.getpid():
        raise AXPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Refusing to signal pid {pid}: that is AXPilot itself.",
            hint="To stop AXPilot, use the menu-bar Stop button or quit the MCP client.",
        )
    if pid == os.getppid():
        raise AXPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Refusing to signal pid {pid}: that is AXPilot's parent (its launcher or MCP client).",
            hint=hint,
        )
