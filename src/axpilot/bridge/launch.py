"""Starting the host and telling it who the client is.

:func:`ensure_host_running` makes sure a host serves the socket: on macOS it opens the
``AXPilot.app`` launcher in the background (macOS then attributes the host's permissions to the
launcher) and waits for the socket; elsewhere there is no host, and the error says how to run
the server in-process instead.

:func:`identify_parent` walks up the bridge's parent processes to the app that started it
(``Claude.app``, ``Cursor.app``, ``Terminal.app`` for command-line clients…), best effort: it
names the client in pairing questions, audit records and confirmations.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from axpilot import paths
from axpilot.bridge.protocol import ClientIdentity
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.host.daemon import socket_is_live
from axpilot.system.bundles import bundle_id_of, enclosing_app
from axpilot.system.runner import CommandRunner

__all__ = ["HOST_START_TIMEOUT_S", "ensure_host_running", "identify_parent", "parse_ps_parent"]

HOST_START_TIMEOUT_S = 10.0
_POLL_S = 0.1
_MAX_ANCESTORS = 16
_PS_TIMEOUT_S = 2.0


def ensure_host_running(
    socket_path: Path | None = None,
    *,
    timeout_s: float = HOST_START_TIMEOUT_S,
    runner: CommandRunner | None = None,
    platform: str = sys.platform,
    app_path: Path | None = None,
    live: Callable[[Path], bool] = socket_is_live,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Path:
    """Return the socket path once a host serves it, starting ``AXPilot.app`` if needed.

    Raises ``HOST_UNAVAILABLE`` when there is no host on this platform, the launcher is not
    installed or cannot be opened, or the host does not come up within ``timeout_s``."""
    path = socket_path or paths.socket_path()
    if live(path):
        return path
    if platform != "darwin":
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"No AXPilot host is running at {path}, and the host only runs on macOS.",
            hint="Run the server in this process instead: `axpilot stdio --direct` (macOS) or "
            "`axpilot stdio --engine fake` (a simulated desktop, anywhere).",
        )
    app = app_path or paths.launcher_app_path()
    if not app.exists():
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"AXPilot.app is not installed at {app}.",
            hint="Run `axpilot setup` once (or use `axpilot stdio --direct`).",
        )
    result = (runner or CommandRunner()).run(["open", "-g", "-a", str(app)], timeout_s=10)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"Could not open {app}" + (f": {detail}" if detail else "."),
            hint="Run `axpilot doctor` to check the installation.",
        )
    deadline = clock() + timeout_s
    while clock() < deadline:
        if live(path):
            return path
        sleep(_POLL_S)
    raise AXPilotError(
        ErrorCode.HOST_UNAVAILABLE,
        f"AXPilot.app started but its host did not open {path} within {timeout_s:g} s.",
        hint="Check AXPilot's menu-bar icon for a message, or run `axpilot doctor`.",
    )


def parse_ps_parent(output: str) -> tuple[int, str] | None:
    """``(ppid, command)`` from ``ps -o ppid=,comm= -p <pid>`` output, or ``None``."""
    line = output.strip()
    if not line:
        return None
    ppid, _, command = line.partition(" ")
    try:
        return int(ppid), command.strip()
    except ValueError:
        return None


def identify_parent(
    *,
    runner: CommandRunner | None = None,
    start_pid: int | None = None,
    own_pid: int | None = None,
) -> ClientIdentity:
    """Who started this bridge: the outermost ``.app`` among its ancestor processes (its name
    and bundle id), else the command of the nearest ancestor. Never raises."""
    runner = runner or CommandRunner()
    pid = start_pid if start_pid is not None else os.getppid()
    nearest: str | None = None
    for _ in range(_MAX_ANCESTORS):
        if pid <= 1:
            break
        try:
            result = runner.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], timeout_s=_PS_TIMEOUT_S)
        except AXPilotError:
            break
        parsed = parse_ps_parent(result.stdout) if result.ok else None
        if parsed is None:
            break
        ppid, command = parsed
        nearest = nearest or command or None
        app = enclosing_app(command)
        if app is not None:
            return ClientIdentity(
                parent_app=app.name.removesuffix(".app"),
                parent_bundle_id=bundle_id_of(app),
                pid=own_pid if own_pid is not None else os.getpid(),
            )
        pid = ppid
    name = PurePosixPath(nearest).name if nearest else None
    return ClientIdentity(parent_app=name or None, pid=own_pid if own_pid is not None else os.getpid())
