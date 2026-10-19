"""MCP client configuration writers (``axpilot connect``).

Typical use (what the CLI does)::

    from axpilot.clients import connect, get_target, resolve_command

    command, args = resolve_command()
    result = connect(get_target("cursor"), command=command, args=args, dry_run=True)
    print(result.message)
    print(result.snippet)

See :mod:`axpilot.clients.connect` for the guarantees (backups, atomic writes, idempotency, no
silent loss of comments) and :mod:`axpilot.clients.targets` for the supported clients.
"""

from __future__ import annotations

from axpilot.clients.base import (
    SCOPES,
    ClientTarget,
    ConfigFormat,
    ConnectResult,
    EntryStatus,
    Scope,
)
from axpilot.clients.connect import (
    connect,
    disconnect,
    find_axpilot_executable,
    inspect_entry,
    merge_entry,
    render_snippet,
    resolve_command,
    resolve_target,
)
from axpilot.clients.targets import TARGETS, detect_installed, get_target, target_names

__all__ = [
    "SCOPES",
    "TARGETS",
    "ClientTarget",
    "ConfigFormat",
    "ConnectResult",
    "EntryStatus",
    "Scope",
    "connect",
    "detect_installed",
    "disconnect",
    "find_axpilot_executable",
    "get_target",
    "inspect_entry",
    "merge_entry",
    "render_snippet",
    "resolve_command",
    "resolve_target",
    "target_names",
]
