"""Building blocks of the MCP client configuration writers behind ``axpilot connect``.

A :class:`ClientTarget` describes one MCP client as data: where its configuration lives (per
scope), the file format, the key under which MCP servers are stored, and the exact shape of a
server entry. :mod:`axpilot.clients.connect` turns that description into careful file edits, and
:mod:`axpilot.clients.targets` defines the supported clients.

Every path is computed from :meth:`pathlib.Path.home` when it is needed, never at import time, so
``HOME`` can be redirected (tests do this) and the module is safe to import anywhere.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from axpilot import SERVER_NAME
from axpilot.errors import AXPilotError, ErrorCode

__all__ = [
    "SCOPES",
    "SYSTEM_APPLICATIONS_DIR",
    "ClientTarget",
    "ConfigFormat",
    "ConnectResult",
    "DocumentBuilder",
    "EntryBuilder",
    "EntryStatus",
    "Scope",
    "application_dirs",
    "continue_document",
    "continue_server",
    "goose_extension",
    "stdio_entry",
    "typed_stdio_entry",
]

Scope = Literal["user", "project"]
"""``user``: the client's per-user configuration. ``project``: a file inside a project folder."""

SCOPES: tuple[Scope, ...] = ("user", "project")

ConfigFormat = Literal["json", "jsonc", "toml", "yaml-file", "manual"]
"""How a client stores its MCP servers.

* ``json``: a JSON object; AXPilot edits one key and rewrites the file.
* ``jsonc``: JSON that may contain comments; AXPilot never silently drops them (see
  :func:`axpilot.clients.connect.connect`).
* ``toml``: a TOML document (OpenAI Codex CLI).
* ``yaml-file``: a standalone YAML file that belongs to AXPilot (Continue's block files).
* ``manual``: AXPilot cannot edit the file safely and prints a snippet instead (Goose).
"""

EntryBuilder = Callable[[str, str, Sequence[str], Mapping[str, str] | None], dict[str, Any]]
"""``(server_name, command, args, env) -> entry``: the client-specific server entry."""

DocumentBuilder = Callable[[str, dict[str, Any]], dict[str, Any]]
"""``(server_name, entry) -> document``: wraps an entry into a whole file (YAML targets)."""

SYSTEM_APPLICATIONS_DIR = Path("/Applications")


def application_dirs() -> list[Path]:
    """Folders searched for client app bundles: ``/Applications`` and ``~/Applications``."""
    return [SYSTEM_APPLICATIONS_DIR, Path.home() / "Applications"]


# --------------------------------------------------------------------------------------------
# Entry shapes


def stdio_entry(
    name: str, command: str, args: Sequence[str], env: Mapping[str, str] | None
) -> dict[str, Any]:
    """The common ``{"command": ..., "args": [...], "env": {...}}`` shape (``env`` only if set)."""
    del name  # the name is the key of the entry, not part of it
    entry: dict[str, Any] = {"command": command, "args": list(args)}
    if env:
        entry["env"] = dict(env)
    return entry


def typed_stdio_entry(
    name: str, command: str, args: Sequence[str], env: Mapping[str, str] | None
) -> dict[str, Any]:
    """``stdio_entry`` with an explicit ``"type": "stdio"`` (VS Code, Claude Code)."""
    return {"type": "stdio", **stdio_entry(name, command, args, env)}


def goose_extension(
    name: str, command: str, args: Sequence[str], env: Mapping[str, str] | None
) -> dict[str, Any]:
    """A Goose ``extensions`` entry (``cmd``/``args``/``envs``)."""
    return {
        "name": name,
        "type": "stdio",
        "cmd": command,
        "args": list(args),
        "enabled": True,
        "timeout": 300,
        "envs": dict(env or {}),
    }


def continue_server(
    name: str, command: str, args: Sequence[str], env: Mapping[str, str] | None
) -> dict[str, Any]:
    """One item of a Continue block file's ``mcpServers`` list."""
    entry: dict[str, Any] = {"name": name, "type": "stdio", "command": command, "args": list(args)}
    if env:
        entry["env"] = dict(env)
    return entry


def continue_document(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """A complete Continue block file (``schema: v1``) holding one MCP server."""
    title = "AXPilot" if name == SERVER_NAME else name
    return {"name": title, "version": "0.0.1", "schema": "v1", "mcpServers": [entry]}


# --------------------------------------------------------------------------------------------
# Targets


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


@dataclass(frozen=True, slots=True)
class ClientTarget:
    """One MCP client that ``axpilot connect`` can configure.

    Paths are tuples of path components relative to the home folder (``user_path``) or to the
    project folder (``project_path``); ``None`` means the client has no such scope.
    """

    name: str
    """Stable slug used on the command line (``claude-desktop``)."""
    display_name: str
    format: ConfigFormat
    servers_key: tuple[str, ...]
    """Key path of the object holding MCP servers (``("mcpServers",)``, ``("mcp_servers",)``…)."""
    user_path: tuple[str, ...] | None
    project_path: tuple[str, ...] | None = None
    build_entry: EntryBuilder = stdio_entry
    document: DocumentBuilder | None = None
    """For ``yaml-file``/``manual`` targets: wraps the entry into the whole document."""
    detect_paths: tuple[tuple[str, ...], ...] = ()
    """Home-relative files or folders whose presence means the client is installed."""
    app_names: tuple[str, ...] = ()
    """App bundles (``"Claude.app"``) looked up in :func:`application_dirs`."""
    executables: tuple[str, ...] = ()
    """Command-line programs whose presence on ``PATH`` means the client is installed."""
    cli: str | None = None
    """The client's own CLI, used for user-scope changes when available (``claude``)."""
    cli_paths: tuple[tuple[str, ...], ...] = ()
    """Home-relative fallback locations of ``cli`` (GUI-launched shells may lack its PATH)."""
    verified: bool = False
    """True once paths and entry shape were checked against a real installation on a Mac."""
    notes: str = ""
    restart_hint: str = ""
    """What the user must do after a change for the client to pick it up."""
    aliases: tuple[str, ...] = ()

    @property
    def scopes(self) -> tuple[Scope, ...]:
        """The scopes this client supports, ``user`` first."""
        found: list[Scope] = []
        if self.user_path is not None:
            found.append("user")
        if self.project_path is not None:
            found.append("project")
        return tuple(found)

    def supports(self, scope: str) -> bool:
        return scope in self.scopes

    def config_path(self, scope: Scope = "user", project_dir: Path | None = None) -> Path:
        """The configuration file for ``scope``. ``project_dir`` defaults to the current folder.

        Raises ``INVALID_ARGUMENT`` if the client has no such scope.
        """
        if scope not in SCOPES:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown scope {scope!r}.",
                hint="Use `user` or `project`.",
            )
        if scope == "user" and self.user_path is not None:
            return Path.home().joinpath(*self.user_path)
        if scope == "project" and self.project_path is not None:
            base = Path(project_dir).expanduser() if project_dir is not None else Path.cwd()
            return base.absolute().joinpath(*self.project_path)
        supported = " or ".join(f"`{s}`" for s in self.scopes) or "no scope"
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{self.display_name} has no {scope}-level MCP configuration.",
            hint=f"Use --scope {self.scopes[0]}." if self.scopes else f"It supports {supported}.",
        )

    def entry(
        self,
        command: str,
        args: Sequence[str],
        env: Mapping[str, str] | None = None,
        *,
        name: str = SERVER_NAME,
    ) -> dict[str, Any]:
        """The server entry this client expects for ``command args`` (with ``env``)."""
        return self.build_entry(name, command, args, env)

    def find_cli(self) -> Path | None:
        """Absolute path of the client's CLI (``cli``), or ``None`` if it is not installed."""
        if self.cli is None:
            return None
        found = shutil.which(self.cli)
        if found:
            return Path(found).absolute()
        for parts in self.cli_paths:
            candidate = Path.home().joinpath(*parts)
            if _executable(candidate):
                return candidate
        return None

    def detect(self) -> bool:
        """True if the client looks installed: its config or data folder, app or CLI exists."""
        home = Path.home()
        candidates = [home.joinpath(*parts) for parts in self.detect_paths]
        if self.user_path is not None:
            candidates.append(self.config_path("user"))
        if any(path.exists() for path in candidates):
            return True
        if any((folder / app).exists() for folder in application_dirs() for app in self.app_names):
            return True
        if any(shutil.which(program) for program in self.executables):
            return True
        return self.find_cli() is not None


# --------------------------------------------------------------------------------------------
# Results


@dataclass(frozen=True, slots=True)
class ConnectResult:
    """What :func:`~axpilot.clients.connect.connect` or ``disconnect`` did (or would do)."""

    path: Path
    """The client configuration file concerned."""
    changed: bool
    """True if the file was changed (in a dry run: would be changed)."""
    backup_path: Path | None
    """The backup taken before the change, if any."""
    snippet: str
    """The configuration to add by hand: a JSON/TOML/YAML fragment or a shell command."""
    message: str
    """One or two sentences for the user."""
    target: str = ""
    dry_run: bool = False
    manual: bool = False
    """True if the user has to apply ``snippet`` themselves (nothing was written)."""
    warnings: tuple[str, ...] = ()
    preview: str | None = None
    """In a dry run: the complete new file content (``None`` if no file would be written)."""
    commands: tuple[tuple[str, ...], ...] = ()
    """External commands that were run (or, in a dry run, would be run)."""

    @property
    def backup(self) -> Path | None:
        """Alias of :attr:`backup_path`."""
        return self.backup_path


@dataclass(frozen=True, slots=True)
class EntryStatus:
    """Whether a client's configuration contains an AXPilot entry (used by ``axpilot doctor``)."""

    target: str
    path: Path
    exists: bool
    """The configuration file exists."""
    configured: bool | None
    """True/False if known; ``None`` when AXPilot cannot tell (unreadable or manual formats)."""
    entry: dict[str, Any] | None = None
    error: str | None = None

    @property
    def command(self) -> str | None:
        """The configured command, if the entry has a plain string one."""
        if not self.entry:
            return None
        value = self.entry.get("command", self.entry.get("cmd"))
        return value if isinstance(value, str) else None
