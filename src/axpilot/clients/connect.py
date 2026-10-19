"""``axpilot connect``: add AXPilot to an MCP client's configuration, and remove it again.

Guarantees of :func:`connect` and :func:`disconnect`:

* **Only our entry changes.** Other servers, other settings and the key order of the file stay
  as they are. Keys of an existing AXPilot entry that AXPilot does not manage (for example Cline's
  ``autoApprove``) are kept as well.
* **Idempotent.** If the configuration already holds the identical entry, nothing is written.
* **Backed up.** Before an existing file is modified, it is copied to
  ``<name>.bak-YYYYmmdd-HHMMSS`` (timestamp from the injectable ``clock``).
* **Atomic.** Files are replaced through a temporary file and ``os.replace``, keeping their
  permission bits, line endings and byte-order mark; missing parent folders are created.
* **No silent loss of comments.** A JSONC file with comments is not rewritten unless
  ``force=True``; the result then carries the snippet to paste instead (``manual=True``). TOML
  files keep their comments when AXPilot only appends or removes its own table; when a full
  rewrite is unavoidable the result warns that comments were dropped (the backup keeps them).
* **Dry runs** report what would change (``changed=True``, ``preview`` with the new file) and
  touch nothing.

All failures are :class:`~axpilot.errors.AXPilotError`: ``INVALID_ARGUMENT`` for unknown clients,
unsupported scopes and unparsable files, ``COMMAND_FAILED`` for I/O errors and failing client CLIs.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from axpilot import SERVER_NAME
from axpilot.clients import jsonc
from axpilot.clients.base import ClientTarget, ConnectResult, EntryStatus, Scope
from axpilot.clients.fileops import (
    ConfigText,
    atomic_write,
    backup_file,
    display_path,
    read_config_text,
    remove_file,
)
from axpilot.clients.formats import (
    append_toml_table,
    detect_indent,
    dump_json,
    dump_toml,
    dump_yaml,
    nest,
    remove_toml_table,
    toml_has_comments,
    toml_text_matches,
)
from axpilot.clients.targets import get_target
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandRunner, ensure_success

__all__ = [
    "connect",
    "disconnect",
    "find_axpilot_executable",
    "inspect_entry",
    "merge_entry",
    "render_snippet",
    "resolve_command",
    "resolve_target",
]

Clock = Callable[[], datetime]

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CLI_TIMEOUT_S = 60.0

_REPLACED_KEYS = frozenset({"command", "args", "type", "url", "serverUrl", "httpUrl", "headers", "transport"})
"""Keys of an existing entry that describe how to start or reach the server. A new stdio entry
replaces them all, so a stale HTTP ``url`` cannot linger next to the new ``command``."""

_YAML_HEADER = (
    "Written by `axpilot connect {client}`. AXPilot rewrites this file when you connect again;\n"
    "delete it to disconnect AXPilot from {display}."
)

# --------------------------------------------------------------------------------------------
# Inputs


def resolve_target(target: ClientTarget | str) -> ClientTarget:
    """Accept a :class:`ClientTarget` or a client name/alias."""
    return target if isinstance(target, ClientTarget) else get_target(target)


def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def _validate_name(server_name: str) -> None:
    if not isinstance(server_name, str) or not _NAME_RE.fullmatch(server_name):
        raise _invalid(
            f"Invalid server name {server_name!r}.",
            hint="Use 1-64 letters, digits, '-' or '_', starting with a letter or digit.",
        )


def _validate(
    command: str, args: Sequence[str], env: Mapping[str, str] | None, server_name: str
) -> list[str]:
    _validate_name(server_name)
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise _invalid("The server command must be a non-empty string.")
    if isinstance(args, str | bytes):
        raise _invalid("`args` must be a list of strings, not a single string.")
    args_list = list(args)
    if not all(isinstance(arg, str) and "\0" not in arg for arg in args_list):
        raise _invalid("Every server argument must be a string without NUL characters.")
    for key, value in (env or {}).items():
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
            raise _invalid(f"Invalid environment variable name {key!r}.")
        if not isinstance(value, str) or "\0" in value:
            raise _invalid(f"The value of {key} must be a string without NUL characters.")
    return args_list


def _path_for(client: ClientTarget, scope: Scope, project_dir: Path | None, server_name: str) -> Path:
    path = client.config_path(scope, project_dir)
    if client.format == "yaml-file" and server_name != SERVER_NAME:
        path = path.with_name(f"{server_name}{path.suffix}")
    return path


# --------------------------------------------------------------------------------------------
# Entries and snippets


def merge_entry(existing: Any, new: Mapping[str, Any], *, replace_env: bool) -> dict[str, Any]:
    """Combine an existing server entry with a new one.

    Values from ``new`` win; launch keys (``command``, ``args``, ``type``, ``url``, …) missing
    from ``new`` are dropped; other keys of ``existing`` are kept in their original order.
    ``env`` is replaced only if ``replace_env`` (an empty new ``env`` then removes it).
    """
    if not isinstance(existing, Mapping):
        return dict(new)
    drop = _REPLACED_KEYS | ({"env"} if replace_env else frozenset())
    merged: dict[str, Any] = {}
    for key, value in existing.items():
        if key in new:
            merged[key] = new[key]
        elif key not in drop:
            merged[key] = value
    for key, value in new.items():
        merged.setdefault(key, value)
    return merged


def _yaml_document(client: ClientTarget, server_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    if client.document is not None:
        return client.document(server_name, entry)
    return nest(client.servers_key, {server_name: entry})


def _yaml_file_text(client: ClientTarget, server_name: str, entry: dict[str, Any]) -> str:
    header = _YAML_HEADER.format(client=client.name, display=client.display_name)
    return dump_yaml(_yaml_document(client, server_name, entry), header=header)


def render_snippet(client: ClientTarget, entry: dict[str, Any], *, server_name: str = SERVER_NAME) -> str:
    """The configuration fragment a user would paste for ``entry`` (in the client's format)."""
    wrapped = nest(client.servers_key, {server_name: entry})
    if client.format in ("json", "jsonc"):
        return dump_json(wrapped)
    if client.format == "toml":
        return dump_toml(wrapped)
    if client.format == "yaml-file":
        return _yaml_file_text(client, server_name, entry)
    return dump_yaml(_yaml_document(client, server_name, entry))


# --------------------------------------------------------------------------------------------
# Reading configuration files


def _load_json(snap: ConfigText) -> dict[str, Any]:
    if snap.text is None or not snap.text.strip():
        return {}
    try:
        data = jsonc.loads(snap.text)
    except jsonc.JSONCError as exc:
        raise _invalid(
            f"{display_path(snap.path)} is not valid JSON: {exc}.",
            hint="Fix the syntax error (or move the file away), then try again.",
        ) from exc
    if not isinstance(data, dict):
        raise _invalid(
            f"{display_path(snap.path)} must contain a JSON object at the top level.",
            hint="Fix the file (or move it away), then try again.",
        )
    return data


def _load_toml(snap: ConfigText) -> dict[str, Any]:
    if snap.text is None or not snap.text.strip():
        return {}
    try:
        return tomllib.loads(snap.text)
    except tomllib.TOMLDecodeError as exc:
        raise _invalid(
            f"{display_path(snap.path)} is not valid TOML: {exc}.",
            hint="Fix the syntax error (or move the file away), then try again.",
        ) from exc


def _servers_table(
    data: dict[str, Any], keys: Sequence[str], path: Path, *, create: bool
) -> dict[str, Any] | None:
    node = data
    for depth, key in enumerate(keys):
        child = node.get(key)
        if child is None:
            if not create:
                return None
            child = {}
            node[key] = child
        elif not isinstance(child, dict):
            dotted = ".".join(keys[: depth + 1])
            raise _invalid(
                f'"{dotted}" in {display_path(path)} is not an object, so AXPilot cannot add servers to it.',
                hint="Fix that key by hand, then try again.",
            )
        node = child
    return node


def _prune_empty(data: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """A copy of ``data`` without the (now empty) tables along ``keys``."""
    pruned = copy.deepcopy(data)
    for depth in range(len(keys), 0, -1):
        parent: Any = pruned
        for key in keys[: depth - 1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        leaf = keys[depth - 1]
        if isinstance(parent, dict) and parent.get(leaf) == {}:
            del parent[leaf]
        else:
            break
    return pruned


def _yaml_mapping_has_key(text: str, parent: str, child: str) -> bool:
    """Heuristic for block-style YAML: does top-level ``parent:`` contain the key ``child``?"""
    parent_re = re.compile(rf"^{re.escape(parent)}\s*:")
    child_re = re.compile(rf"^\s*(['\"]?){re.escape(child)}\1\s*:")
    in_parent = False
    child_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_parent = parent_re.match(line) is not None
            child_indent = None
            continue
        if in_parent:
            if child_indent is None:
                child_indent = indent
            if indent == child_indent and child_re.match(line):
                return True
    return False


_YAML_COMMAND_RE = re.compile(r"^\s*-?\s*command:\s*(?P<value>.+?)\s*$", re.MULTILINE)


def _yaml_command(text: str) -> str | None:
    match = _YAML_COMMAND_RE.search(text)
    if not match:
        return None
    value = match.group("value")
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, str) else None
    return value.strip("'")


# --------------------------------------------------------------------------------------------
# Plans: what to write


_Action = Literal["add", "update", "remove", "none"]


@dataclass(slots=True)
class _Plan:
    action: _Action
    new_text: str | None = None
    delete_file: bool = False
    blocked: bool = False
    """A change is needed, but AXPilot will not make it automatically (comments would be lost)."""
    warnings: list[str] = field(default_factory=list)


def _comments_dropped(path: Path) -> str:
    return f"{display_path(path)} had comments; the rewritten file no longer has them (the backup does)."


def _toml_text(
    snap: ConfigText,
    keys: Sequence[str],
    name: str,
    entry: dict[str, Any] | None,
    expected: dict[str, Any],
) -> tuple[str, list[str]]:
    """New TOML text for ``expected``: a minimal text edit when it verifiably works, else a rewrite."""
    old = snap.text
    if old is None or not old.strip():
        return dump_toml(expected), []
    candidate = remove_toml_table(old, (*keys, name))
    if entry is not None:
        candidate = append_toml_table(candidate, keys, name, entry)
    accepted = [expected] if entry is not None else [expected, _prune_empty(expected, keys)]
    if any(toml_text_matches(candidate, wanted) for wanted in accepted):
        return candidate, []
    warnings = [_comments_dropped(snap.path)] if toml_has_comments(old) else []
    return dump_toml(expected), warnings


def _plan_upsert(
    client: ClientTarget,
    snap: ConfigText,
    name: str,
    entry: dict[str, Any],
    *,
    replace_env: bool,
    force: bool,
) -> _Plan:
    if client.format == "yaml-file":
        text = _yaml_file_text(client, name, entry)
        if snap.text == text:
            return _Plan("none")
        return _Plan("update" if snap.exists else "add", text)

    is_toml = client.format == "toml"
    data = _load_toml(snap) if is_toml else _load_json(snap)
    servers = _servers_table(data, client.servers_key, snap.path, create=True)
    assert servers is not None  # create=True always returns a table
    existing = servers.get(name)
    merged = merge_entry(existing, entry, replace_env=replace_env)
    if existing == merged:
        return _Plan("none")
    action: _Action = "update" if existing is not None else "add"
    if is_toml:
        servers[name] = merged
        text, warnings = _toml_text(snap, client.servers_key, name, merged, data)
        return _Plan(action, text, warnings=warnings)
    commented = snap.text is not None and jsonc.has_comments(snap.text)
    if commented and not force:
        return _Plan(action, blocked=True)
    servers[name] = merged
    warnings = [_comments_dropped(snap.path)] if commented else []
    return _Plan(action, dump_json(data, detect_indent(snap.text)), warnings=warnings)


def _plan_remove(client: ClientTarget, snap: ConfigText, name: str, *, force: bool) -> _Plan:
    if not snap.exists:
        return _Plan("none")
    if client.format == "yaml-file":
        return _Plan("remove", delete_file=True)
    is_toml = client.format == "toml"
    data = _load_toml(snap) if is_toml else _load_json(snap)
    servers = _servers_table(data, client.servers_key, snap.path, create=False)
    if servers is None or name not in servers:
        return _Plan("none")
    if is_toml:
        del servers[name]
        text, warnings = _toml_text(snap, client.servers_key, name, None, data)
        return _Plan("remove", text, warnings=warnings)
    assert snap.text is not None
    commented = jsonc.has_comments(snap.text)
    if commented and not force:
        return _Plan("remove", blocked=True)
    del servers[name]
    warnings = [_comments_dropped(snap.path)] if commented else []
    return _Plan("remove", dump_json(data, detect_indent(snap.text)), warnings=warnings)


# --------------------------------------------------------------------------------------------
# Applying plans


def _with_hint(message: str, client: ClientTarget) -> str:
    return f"{message} {client.restart_hint}".strip() if client.restart_hint else message


def _describe(plan: _Plan, client: ClientTarget, name: str, label: str, *, dry_run: bool) -> str:
    where = f"{client.display_name} ({label})"
    if dry_run:
        verbs = {
            "add": f"Would add {name} to",
            "update": f"Would update {name} in",
            "remove": f"Would remove {name} from",
        }
        return f"{verbs[plan.action]} {where}. Dry run: nothing was written."
    if plan.action == "remove":
        return f"Removed {name} from {where}. Restart {client.display_name} if it is running."
    verb = f"Added {name} to" if plan.action == "add" else f"Updated {name} in"
    return _with_hint(f"{verb} {where}.", client)


def _blocked_message(plan: _Plan, name: str, label: str) -> str:
    what = "remove its entry" if plan.action == "remove" else "add it"
    todo = (
        f"Remove the {name} entry from the file yourself"
        if plan.action == "remove"
        else "Paste the snippet into the file yourself"
    )
    return (
        f"{label} contains comments that rewriting it would delete, so AXPilot did not {what} "
        f"automatically. {todo}, or run again with --force to rewrite it (a backup is kept)."
    )


def _apply(
    client: ClientTarget,
    snap: ConfigText,
    plan: _Plan,
    *,
    path: Path,
    name: str,
    snippet: str,
    dry_run: bool,
    clock: Clock,
    removing: bool,
) -> ConnectResult:
    label = display_path(path)
    common: dict[str, Any] = {"target": client.name, "dry_run": dry_run}
    if plan.blocked:
        message = _blocked_message(plan, name, label)
        return ConnectResult(path, False, None, snippet, message, manual=True, **common)
    if plan.action == "none":
        state = "has no" if removing else "already has this"
        message = f"{client.display_name} {state} {name} entry ({label}); nothing changed."
        return ConnectResult(path, False, None, snippet, message, **common)
    warnings = tuple(plan.warnings)
    if dry_run:
        message = _describe(plan, client, name, label, dry_run=True)
        return ConnectResult(
            path, True, None, snippet, message, preview=plan.new_text, warnings=warnings, **common
        )
    backup = backup_file(snap.path, clock) if snap.exists else None
    if plan.delete_file:
        remove_file(snap.path)
    else:
        assert plan.new_text is not None
        atomic_write(snap.path, snap.encode(plan.new_text), mode=snap.mode)
    message = _describe(plan, client, name, label, dry_run=False)
    return ConnectResult(path, True, backup, snippet, message, warnings=warnings, **common)


def _portability_warnings(scope: Scope, command: str, path: Path) -> list[str]:
    if scope != "project" or not Path(command).is_absolute():
        return []
    try:
        Path(command).relative_to(Path.home())
    except ValueError:
        return []
    return [
        f"{display_path(path)} is usually shared with a project, but {command} is inside your home "
        "folder; other people may need a different command (for example `axpilot` on their PATH)."
    ]


# --------------------------------------------------------------------------------------------
# Clients managed through their own CLI (Claude Code)


def _cli_add_argv(
    cli: str, name: str, command: str, args: Sequence[str], env: Mapping[str, str]
) -> list[str]:
    argv = [cli, "mcp", "add", "--scope", "user", name]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    return [*argv, "--", command, *args]


def _cli_remove_argv(cli: str, name: str) -> list[str]:
    return [cli, "mcp", "remove", "--scope", "user", name]


def _cli_existing(client: ClientTarget, path: Path, name: str) -> dict[str, Any] | None:
    servers = _servers_table(_load_json(read_config_text(path)), client.servers_key, path, create=False)
    entry = servers.get(name) if servers else None
    if entry is None:
        return None
    return entry if isinstance(entry, dict) else {}


def _str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _run_cli(runner: CommandRunner, commands: Sequence[Sequence[str]], client: ClientTarget) -> None:
    for argv in commands:
        result = runner.run(argv, timeout_s=_CLI_TIMEOUT_S)
        ensure_success(
            result,
            what=f"`{client.cli} mcp {argv[2]}`",
            hint=f"Run `{shlex.join([client.cli or 'cli', *argv[1:]])}` yourself to see the full error.",
            timeout_s=_CLI_TIMEOUT_S,
        )


def _connect_with_cli(
    client: ClientTarget,
    cli: Path,
    *,
    path: Path,
    name: str,
    command: str,
    args: list[str],
    env: Mapping[str, str] | None,
    dry_run: bool,
    runner: CommandRunner | None,
) -> ConnectResult:
    existing = _cli_existing(client, path, name)
    env_now = dict(env) if env is not None else _str_dict((existing or {}).get("env"))
    shown = client.cli or cli.name
    snippet = shlex.join(_cli_add_argv(shown, name, command, args, env_now)) + "\n"
    common: dict[str, Any] = {"target": client.name, "dry_run": dry_run}
    if existing is not None and (
        existing.get("command") == command
        and existing.get("args", []) == args
        and _str_dict(existing.get("env")) == env_now
        and existing.get("type", "stdio") == "stdio"
    ):
        message = (
            f"{client.display_name} already has this {name} entry ({display_path(path)}); nothing changed."
        )
        return ConnectResult(path, False, None, snippet, message, **common)
    commands: list[tuple[str, ...]] = []
    if existing is not None:
        commands.append(tuple(_cli_remove_argv(str(cli), name)))
    commands.append(tuple(_cli_add_argv(str(cli), name, command, args, env_now)))
    if dry_run:
        planned = "; ".join(shlex.join([shown, *argv[1:]]) for argv in commands)
        message = f"Would run: {planned}. Dry run: nothing was changed."
        return ConnectResult(path, True, None, snippet, message, commands=tuple(commands), **common)
    _run_cli(runner or CommandRunner(), commands, client)
    verb = "Updated" if existing is not None else "Added"
    message = _with_hint(
        f"{verb} {name} in {client.display_name} (user scope) with `{shown} mcp add`.", client
    )
    return ConnectResult(path, True, None, snippet, message, commands=tuple(commands), **common)


def _disconnect_with_cli(
    client: ClientTarget,
    cli: Path,
    *,
    path: Path,
    name: str,
    dry_run: bool,
    runner: CommandRunner | None,
) -> ConnectResult:
    shown = client.cli or cli.name
    snippet = shlex.join(_cli_remove_argv(shown, name)) + "\n"
    common: dict[str, Any] = {"target": client.name, "dry_run": dry_run}
    if _cli_existing(client, path, name) is None:
        message = f"{client.display_name} has no {name} entry ({display_path(path)}); nothing changed."
        return ConnectResult(path, False, None, snippet, message, **common)
    commands = (tuple(_cli_remove_argv(str(cli), name)),)
    if dry_run:
        message = f"Would run: {snippet.strip()}. Dry run: nothing was changed."
        return ConnectResult(path, True, None, snippet, message, commands=commands, **common)
    _run_cli(runner or CommandRunner(), commands, client)
    message = f"Removed {name} from {client.display_name} (user scope) with `{shown} mcp remove`."
    return ConnectResult(path, True, None, snippet, message, commands=commands, **common)


# --------------------------------------------------------------------------------------------
# Manual clients (Goose)


def _manual_result(
    client: ClientTarget, path: Path, snippet: str, name: str, *, removing: bool
) -> ConnectResult:
    label = display_path(path)
    key = client.servers_key[0]
    try:
        text = read_config_text(path).text
        readable = True
    except AXPilotError:
        # Only used to word the message; the user edits the file by hand either way.
        text, readable = None, False
    present = text is not None and _yaml_mapping_has_key(text, key, name)
    if removing:
        if readable and not present:
            message = f"{client.display_name} has no {name} entry ({label}); nothing changed."
            return ConnectResult(path, False, None, "", message, target=client.name)
        message = f"AXPilot does not edit {label}. Remove the `{name}` entry under `{key}:` yourself."
        return ConnectResult(path, False, None, "", message, target=client.name, manual=True)
    already = f" It already lists an `{name}` extension; compare it with the snippet." if present else ""
    message = f"{client.display_name} is configured by hand: add the snippet below to {label}.{already}"
    if client.notes:
        message += f" {client.notes}"
    return ConnectResult(path, False, None, snippet, message, target=client.name, manual=True)


# --------------------------------------------------------------------------------------------
# Public API


def connect(
    target: ClientTarget | str,
    *,
    command: str,
    args: Sequence[str],
    scope: Scope = "user",
    project_dir: Path | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    force: bool = False,
    server_name: str = SERVER_NAME,
    use_cli: bool = True,
    runner: CommandRunner | None = None,
    clock: Clock = datetime.now,
) -> ConnectResult:
    """Add (or update) the AXPilot server entry in a client's configuration.

    ``command``/``args`` start the server (see :func:`resolve_command`); ``env`` adds environment
    variables (``None`` keeps any the entry already has, ``{}`` removes them). ``force`` rewrites
    a JSONC file even though its comments will be lost. For Claude Code's user scope the
    ``claude`` CLI is used when it is installed and ``use_cli`` is true; ``runner`` runs it.
    """
    client = resolve_target(target)
    args_list = _validate(command, args, env, server_name)
    path = _path_for(client, scope, project_dir, server_name)
    entry = client.entry(command, args_list, env, name=server_name)
    snippet = render_snippet(client, entry, server_name=server_name)
    if client.format == "manual":
        return _manual_result(client, path, snippet, server_name, removing=False)
    if scope == "user" and use_cli and (cli := client.find_cli()) is not None:
        return _connect_with_cli(
            client,
            cli,
            path=path,
            name=server_name,
            command=command,
            args=args_list,
            env=env,
            dry_run=dry_run,
            runner=runner,
        )
    snap = read_config_text(path)
    plan = _plan_upsert(client, snap, server_name, entry, replace_env=env is not None, force=force)
    if plan.action != "none" and not plan.blocked:
        plan.warnings.extend(_portability_warnings(scope, command, path))
    return _apply(
        client,
        snap,
        plan,
        path=path,
        name=server_name,
        snippet=snippet,
        dry_run=dry_run,
        clock=clock,
        removing=False,
    )


def disconnect(
    target: ClientTarget | str,
    *,
    scope: Scope = "user",
    project_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    server_name: str = SERVER_NAME,
    use_cli: bool = True,
    runner: CommandRunner | None = None,
    clock: Clock = datetime.now,
) -> ConnectResult:
    """Remove the AXPilot server entry from a client's configuration (same guarantees as
    :func:`connect`; a Continue block file is deleted after being backed up)."""
    client = resolve_target(target)
    _validate_name(server_name)
    path = _path_for(client, scope, project_dir, server_name)
    if client.format == "manual":
        return _manual_result(client, path, "", server_name, removing=True)
    if scope == "user" and use_cli and (cli := client.find_cli()) is not None:
        return _disconnect_with_cli(client, cli, path=path, name=server_name, dry_run=dry_run, runner=runner)
    snap = read_config_text(path)
    plan = _plan_remove(client, snap, server_name, force=force)
    snippet = ""
    if plan.blocked:
        assert snap.text is not None
        servers = _servers_table(_load_json(snap), client.servers_key, snap.path, create=False) or {}
        snippet = dump_json(nest(client.servers_key, {server_name: servers.get(server_name)}))
    return _apply(
        client,
        snap,
        plan,
        path=path,
        name=server_name,
        snippet=snippet,
        dry_run=dry_run,
        clock=clock,
        removing=True,
    )


def inspect_entry(
    target: ClientTarget | str,
    *,
    scope: Scope = "user",
    project_dir: Path | None = None,
    server_name: str = SERVER_NAME,
) -> EntryStatus:
    """Report whether a client's configuration has an AXPilot entry. Never raises for file
    problems: they are reported in :attr:`EntryStatus.error` with ``configured=None``."""
    client = resolve_target(target)
    path = _path_for(client, scope, project_dir, server_name)
    try:
        snap = read_config_text(path)
    except AXPilotError as exc:
        return EntryStatus(client.name, path, True, None, error=exc.message)
    if snap.text is None:
        return EntryStatus(client.name, path, False, False)
    if client.format == "manual":
        found = _yaml_mapping_has_key(snap.text, client.servers_key[0], server_name)
        return EntryStatus(client.name, path, True, found)
    if client.format == "yaml-file":
        command = _yaml_command(snap.text)
        return EntryStatus(client.name, path, True, True, {"command": command} if command else None)
    try:
        data = _load_toml(snap) if client.format == "toml" else _load_json(snap)
        servers = _servers_table(data, client.servers_key, path, create=False)
    except AXPilotError as exc:
        return EntryStatus(client.name, path, True, None, error=exc.message)
    entry = servers.get(server_name) if servers else None
    if entry is None:
        return EntryStatus(client.name, path, True, False)
    return EntryStatus(client.name, path, True, True, entry if isinstance(entry, dict) else None)


# --------------------------------------------------------------------------------------------
# The command clients should run


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _tool_bin_dirs() -> list[Path]:
    """Where ``uv tool install`` (and pipx) put executables, most specific first."""
    dirs = [
        Path(value).expanduser()
        for var in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "PIPX_BIN_DIR")
        if (value := os.environ.get(var))
    ]
    dirs.append(Path.home() / ".local" / "bin")
    return dirs


def find_axpilot_executable(*, argv0: str | None = None) -> Path | None:
    """Absolute path of the installed ``axpilot`` program, or ``None``.

    Looks on ``PATH``, then at ``argv0`` (default ``sys.argv[0]``) if that is an ``axpilot``
    entry point, then in the uv/pipx tool folders (``~/.local/bin`` by default). Symlinks are
    not resolved: ``~/.local/bin/axpilot`` stays valid across upgrades, its target does not.
    """
    found = shutil.which("axpilot")
    if found:
        return Path(found).absolute()
    entry = argv0 if argv0 is not None else (sys.argv[0] if sys.argv else "")
    if entry:
        candidate = Path(entry).expanduser()
        if candidate.name == "axpilot" and _is_executable(candidate):
            return candidate.absolute()
    for folder in _tool_bin_dirs():
        candidate = folder / "axpilot"
        if _is_executable(candidate):
            return candidate.absolute()
    return None


def resolve_command(*, argv0: str | None = None) -> tuple[str, list[str]]:
    """The ``(command, args)`` an MCP client should run to start AXPilot's stdio bridge.

    Prefers the absolute path of the installed ``axpilot`` (GUI apps such as Claude Desktop do
    not inherit the shell's ``PATH``) and falls back to ``uvx axpilot`` (with ``uvx``'s absolute
    path when it can be found). The arguments always end with ``stdio``.
    """
    executable = find_axpilot_executable(argv0=argv0)
    if executable is not None:
        return str(executable), ["stdio"]
    uvx = shutil.which("uvx")
    return (str(Path(uvx).absolute()) if uvx else "uvx"), ["axpilot", "stdio"]
