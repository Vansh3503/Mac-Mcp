"""Scripting tools (the ``scripting`` toolset, off by default): ``shell``, ``applescript``,
``shortcuts`` and ``defaults``.

They run code or change settings, so every risky call asks the user first, showing exactly what
will run: the command line, the script, the shortcut and its input, or the ``defaults write``
command. Arguments are validated before anyone is asked. Commands run through
:attr:`ServerContext.runner` (a ``FakeRunner`` in tests) in a worker thread.

Apps named in an AppleScript or JXA script (``tell application "Notes"``, ``application id
"com.apple.Notes"``, ``Application("Notes")``) and the domain of a ``defaults`` call are checked
against the app policy, and a script that talks to System Events (GUI scripting) is checked
against the frontmost app. This is a best-effort guard against mistakes, not a sandbox: once the
user enables scripting, a script can reach anything the user can.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import functools
import re
import shlex
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp_types import CallToolResult
from pydantic import Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import OutputModel
from axpilot.system.defaults import GLOBAL_DOMAIN, defaults_read, defaults_write, defaults_write_argv
from axpilot.system.osascript import output_text, run_applescript
from axpilot.system.runner import truncate_output
from axpilot.system.shell import MAX_COMMAND_CHARS, MAX_SHELL_TIMEOUT_S, run_shell, shell_argv
from axpilot.system.shortcuts import MAX_TIMEOUT_S as SHORTCUT_MAX_TIMEOUT_S
from axpilot.system.shortcuts import list_shortcuts, run_shortcut

__all__ = ["CONFIRM_DETAIL_CHARS", "script_targets"]

CONFIRM_DETAIL_CHARS = 1500
"""Longest command or script shown in a confirmation (the rest is summarized)."""
SCRIPT_MAX_CHARS = 100_000
SCRIPT_TIMEOUT_DEFAULT_S = 30.0
SHORTCUT_INPUT_MAX_CHARS = 100_000
SHORTCUT_LIST_MAX = 500
_RUN_MARGIN_S = 10.0
"""Extra time the worker thread gets beyond a command's own timeout (the runner kills it)."""

ScriptLanguage = Literal["applescript", "javascript"]
ShortcutsAction = Literal["list", "run"]
DefaultsAction = Literal["read", "write"]
DefaultsType = Literal["string", "int", "float", "bool"]


def _shown(text: str, limit: int = CONFIRM_DETAIL_CHARS) -> str:
    """``text`` for a confirmation dialog: cut at ``limit`` with a note of how much is hidden."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… ({len(text) - limit:,} more characters not shown)"


def _output(ctx: ServerContext, text: str) -> tuple[str, bool]:
    """``text`` cut to ``[scripting] output_max_chars``; returns ``(text, truncated)``."""
    return truncate_output(text, ctx.config.scripting.output_max_chars)


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


# =========================================================================== shell
SHELL_DESCRIPTION = """\
Run a shell command line with zsh (a login shell, so the user's PATH applies) and return its exit
code, stdout and stderr. The user approves each command; it sees the exact command line.
Use it for what the GUI does poorly: files, command-line tools, builds, git.
Params: command; cwd (working directory, default the home folder); timeout_s (default and maximum:
[scripting] shell_timeout_s, 30 s unless the user raised it, at most 120).
Stdin is empty, so interactive commands see end-of-file. A non-zero exit code is a normal result
(read stderr), and so is a timeout (timed_out=true with the output so far). Output is cut at
[scripting] output_max_chars characters (truncated=true).
Errors: USER_DENIED (the user declined), INVALID_ARGUMENT (empty command, missing cwd, timeout
above the limit)."""


class ShellModel(OutputModel):
    """Output of ``shell``."""

    exit_code: int = Field(description="The command's exit status (-1 after a timeout).")
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = Field(default=False, description="Output was cut to [scripting] output_max_chars.")
    cwd: str
    duration_ms: int


def _shell_text(model: ShellModel) -> str:
    status = "timed out" if model.timed_out else f"exit {model.exit_code}"
    lines = [f"{status} after {model.duration_ms / 1000:.1f} s in {model.cwd}"]
    if model.stdout:
        lines += ["--- stdout", model.stdout.rstrip("\n")]
    if model.stderr:
        lines += ["--- stderr", model.stderr.rstrip("\n")]
    if not model.stdout and not model.stderr:
        lines.append("(no output)")
    if model.truncated:
        lines.append(f"(output cut at {len(model.stdout) + len(model.stderr):,} characters)")
    return "\n".join(lines)


def _working_directory(cwd: str | None) -> Path:
    path = Path(cwd or "~").expanduser()
    if "\x00" in str(path) or not path.is_dir():
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"The working directory does not exist: {path}",
            hint="Pass an existing folder as cwd (absolute, or starting with ~).",
        )
    return path


def _shell_timeout(ctx: ServerContext, timeout_s: float | None) -> float:
    limit = float(ctx.config.scripting.shell_timeout_s)
    if timeout_s is None:
        return limit
    if timeout_s > limit:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"timeout_s {timeout_s:g} is above the limit of {limit:g} s.",
            hint=f"Use at most {limit:g}. The user can raise [scripting] shell_timeout_s (up to "
            f"{MAX_SHELL_TIMEOUT_S:g}).",
        )
    return timeout_s


@tool(
    "shell",
    title="Run a shell command",
    description=SHELL_DESCRIPTION,
    destructive=True,
    open_world=True,
    windows_mcp_name="Shell",
    confirms_itself=True,
)
def shell_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``shell`` tool."""

    async def shell(
        command: Annotated[
            str,
            Field(
                description="The command line, run with zsh -lc.", min_length=1, max_length=MAX_COMMAND_CHARS
            ),
        ],
        cwd: Annotated[
            str | None, Field(description="Working directory (default: the home folder).", max_length=4096)
        ] = None,
        timeout_s: Annotated[
            float | None,
            Field(description="Seconds before the command is stopped.", gt=0, le=MAX_SHELL_TIMEOUT_S),
        ] = None,
    ) -> Annotated[CallToolResult, ShellModel]:
        settings = ctx.config.scripting
        shell_argv(command, shell=settings.shell)  # validates the command and the configured shell
        folder = _working_directory(cwd)
        limit = _shell_timeout(ctx, timeout_s)
        await ctx.confirm_action(
            "Run this shell command?",
            f"{_shown(command)}\n\nFolder: {folder}\nStopped after {limit:g} s.",
            destructive=True,
        )
        started = time.monotonic()
        result = await ctx.run_engine(
            functools.partial(
                run_shell,
                ctx.runner,
                command,
                shell=settings.shell,
                cwd=str(folder),
                timeout_s=limit,
                max_output=settings.output_max_chars,
            ),
            timeout_s=limit + _RUN_MARGIN_S,
        )
        model = ShellModel(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            truncated=result.truncated,
            cwd=str(folder),
            duration_ms=_elapsed_ms(started),
        )
        return ok_result(_shell_text(model), model)

    return shell


# =========================================================================== applescript
APPLESCRIPT_DESCRIPTION = """\
Run an AppleScript or JavaScript for Automation (JXA) script with osascript and return its result
as text. The user approves each script and sees it in full.
Use it for apps with a scripting dictionary (Mail, Calendar, Notes, Finder, Music, Safari…) when
that is more reliable than clicking, and for data the UI hides.
Params: script; language (applescript | javascript); timeout_s (default 30, at most 120).
Apps the script names are checked against the safety policy (deny_apps, and the Terminal rule:
scripting a terminal needs the shell tool), and System Events scripts against the frontmost app.
macOS asks the user once per target app for the Automation permission.
Errors: PERMISSION_AUTOMATION (not allowed to control that app), APP_NOT_FOUND, TIMEOUT,
COMMAND_FAILED (the script's own error, quoted), BLOCKED_BY_POLICY, USER_DENIED."""


class ScriptModel(OutputModel):
    """Output of ``applescript``."""

    language: ScriptLanguage
    result: str = Field(description="The script's result as text (empty if it returns nothing).")
    truncated: bool = False
    apps: list[str] = Field(default_factory=list, description="Apps the script names.")
    duration_ms: int


_APPLESCRIPT_APP_RE = re.compile(r"\bapp(?:lication)?\s+(id\s+)?\"((?:[^\"\\]|\\.)+)\"", re.IGNORECASE)
_JXA_APP_RE = re.compile(r"\bApplication\s*\(\s*([\"'`])((?:(?!\1)[^\\]|\\.)+)\1\s*\)")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+){2,}$")
_SYSTEM_EVENTS = "system events"


def script_targets(script: str, language: ScriptLanguage) -> list[AppInfo]:
    """The apps ``script`` names literally, as :class:`AppInfo` stand-ins for the policy (name or
    bundle id; ``pid`` 0). Best effort: names built at run time are not seen."""
    found: dict[tuple[str, str | None], AppInfo] = {}
    if language == "applescript":
        for match in _APPLESCRIPT_APP_RE.finditer(script):
            value = match.group(2).replace('\\"', '"')
            is_id = bool(match.group(1)) or bool(_BUNDLE_ID_RE.match(value))
            info = AppInfo(name=value, pid=0, bundle_id=value if is_id else None)
            found.setdefault((info.name.casefold(), info.bundle_id), info)
    else:
        for match in _JXA_APP_RE.finditer(script):
            value = match.group(2)
            is_id = bool(_BUNDLE_ID_RE.match(value))
            info = AppInfo(name=value, pid=0, bundle_id=value if is_id else None)
            found.setdefault((info.name.casefold(), info.bundle_id), info)
    return list(found.values())


async def _as_known(ctx: ServerContext, app: AppInfo) -> AppInfo:
    """The running or installed app ``app`` stands for (with its real name and bundle id), else
    ``app`` itself."""
    known = await ctx.run_engine(functools.partial(ctx.engine.list_apps, include_installed=True))
    return next((candidate for candidate in known if candidate.matches(app.bundle_id or app.name)), app)


async def _check_script_targets(ctx: ServerContext, apps: list[AppInfo]) -> None:
    for app in apps:
        ctx.policy.check_app(await _as_known(ctx, app), kind="input")
    if any(app.name.casefold() == _SYSTEM_EVENTS for app in apps):
        front = await ctx.run_engine(ctx.engine.frontmost_app)
        if front is not None:
            ctx.policy.check_app(front, kind="input")


@tool(
    "applescript",
    title="Run AppleScript or JXA",
    description=APPLESCRIPT_DESCRIPTION,
    destructive=True,
    open_world=True,
    confirms_itself=True,
)
def applescript_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``applescript`` tool."""

    async def applescript(
        script: Annotated[
            str, Field(description="The script source.", min_length=1, max_length=SCRIPT_MAX_CHARS)
        ],
        language: Annotated[
            ScriptLanguage, Field(description="applescript | javascript (JXA).")
        ] = "applescript",
        timeout_s: Annotated[
            float, Field(description="Seconds before the script is stopped.", gt=0, le=MAX_SHELL_TIMEOUT_S)
        ] = SCRIPT_TIMEOUT_DEFAULT_S,
    ) -> Annotated[CallToolResult, ScriptModel]:
        if not script.strip() or "\x00" in script:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The script is empty or contains NUL characters.")
        apps = script_targets(script, language)
        await _check_script_targets(ctx, apps)
        kind = "AppleScript" if language == "applescript" else "JavaScript (JXA)"
        named = ", ".join(app.name for app in apps)
        await ctx.confirm_action(
            f"Run this {kind} script" + (f" (it addresses {named})?" if named else "?"),
            _shown(script),
            destructive=True,
        )
        started = time.monotonic()
        result = await ctx.run_engine(
            functools.partial(
                run_applescript,
                ctx.runner,
                script,
                language=language,
                timeout_s=timeout_s,
                max_output=ctx.config.scripting.output_max_chars,
            ),
            timeout_s=timeout_s + _RUN_MARGIN_S,
        )
        text, cut = _output(ctx, output_text(result))
        model = ScriptModel(
            language=language,
            result=text,
            truncated=cut or result.truncated,
            apps=[app.name for app in apps],
            duration_ms=_elapsed_ms(started),
        )
        return ok_result(text if text else "The script finished without a result.", model)

    return applescript


# =========================================================================== shortcuts
SHORTCUTS_DESCRIPTION = """\
List or run the user's Apple Shortcuts (the shortcuts command, macOS 12+).
Use it to reuse automations the user already built. Params: action list | run; list: query
(case-insensitive filter), limit; run: name (exact, from list), input (text passed to the
shortcut), timeout_s (default 30, at most 600). Running asks the user first.
Returns the names, or the shortcut's output as text (cut at [scripting] output_max_chars).
Errors: INVALID_ARGUMENT (no shortcut with that name), TIMEOUT (it may wait for the user),
COMMAND_FAILED, NOT_SUPPORTED (no shortcuts command), USER_DENIED."""


class ShortcutsModel(OutputModel):
    """Output of ``shortcuts``."""

    action: ShortcutsAction
    shortcuts: list[str] = Field(default_factory=list, description="list: shortcut names.")
    count: int = Field(default=0, description="list: how many matched (before the limit).")
    name: str | None = None
    output: str | None = Field(default=None, description="run: what the shortcut returned, as text.")
    truncated: bool = False


@tool(
    "shortcuts",
    title="List or run Shortcuts",
    description=SHORTCUTS_DESCRIPTION,
    destructive=True,
    open_world=True,
    confirms_itself=True,
)
def shortcuts_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``shortcuts`` tool."""

    async def shortcuts(
        *,
        action: Annotated[ShortcutsAction, Field(description="list | run.")] = "list",
        name: Annotated[
            str | None, Field(description="run: the shortcut's exact name.", max_length=255)
        ] = None,
        input: Annotated[
            str | None,
            Field(description="run: text given to the shortcut.", max_length=SHORTCUT_INPUT_MAX_CHARS),
        ] = None,
        query: Annotated[
            str | None, Field(description="list: keep names containing this.", max_length=255)
        ] = None,
        limit: Annotated[
            int, Field(description="list: most names to return.", ge=1, le=SHORTCUT_LIST_MAX)
        ] = 100,
        timeout_s: Annotated[
            float, Field(description="run: seconds before it is stopped.", gt=0, le=SHORTCUT_MAX_TIMEOUT_S)
        ] = 30.0,
    ) -> Annotated[CallToolResult, ShortcutsModel]:
        if action == "list":
            if name is not None or input is not None:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "name and input only go with action=run.")
            await ctx.confirm_action("List your Shortcuts?", destructive=False)
            names = await ctx.run_engine(functools.partial(list_shortcuts, ctx.runner))
            if query:
                names = [entry for entry in names if query.casefold() in entry.casefold()]
            shown = names[:limit]
            model = ShortcutsModel(action="list", shortcuts=shown, count=len(names))
            more = f" (showing {len(shown)})" if len(shown) < len(names) else ""
            head = f"{len(names)} shortcut{'s' if len(names) != 1 else ''}{more}"
            return ok_result("\n".join([head + (":" if shown else "."), *shown]), model)
        if not name or not name.strip():
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "run needs the shortcut's name.",
                hint="Call with action=list first.",
            )
        if query is not None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "query only goes with action=list.")
        detail = f"Input: {_shown(input, 500)}" if input else None
        await ctx.confirm_action(f"Run the shortcut {name!r}?", detail, destructive=True)
        result = await ctx.run_engine(
            functools.partial(
                run_shortcut,
                ctx.runner,
                name,
                input,
                timeout_s,
                max_output=ctx.config.scripting.output_max_chars,
            ),
            timeout_s=timeout_s + _RUN_MARGIN_S,
        )
        text, cut = _output(ctx, result.stdout.removesuffix("\n"))
        model = ShortcutsModel(action="run", name=name, output=text, truncated=cut or result.truncated)
        return ok_result(text if text else f"The shortcut {name!r} finished without output.", model)

    return shortcuts


# =========================================================================== defaults
DEFAULTS_DESCRIPTION = """\
Read or write macOS preferences (the defaults command), e.g. com.apple.dock autohide.
Params: action read | write; domain (a bundle id such as com.apple.finder, NSGlobalDomain, or a
plist path); key (read: optional, the whole domain without it); write: value and type
(string | int | float | bool; inferred from the value when omitted). Writing asks the user first
and shows the exact command. Apps usually need a restart (or `killall Dock`) to notice a change.
Domains of protected apps (deny_apps) are off limits.
Errors: COMMAND_FAILED (details.reason "not_found" for a missing domain or key), INVALID_ARGUMENT,
BLOCKED_BY_POLICY, USER_DENIED."""


class DefaultsModel(OutputModel):
    """Output of ``defaults``."""

    action: DefaultsAction
    domain: str
    key: str | None = None
    value: str | None = Field(default=None, description="read: the value as `defaults read` prints it.")
    type: DefaultsType | None = Field(default=None, description="write: the type written.")
    command: str | None = Field(default=None, description="write: the command that ran.")
    truncated: bool = False


def _domain_app(domain: str) -> AppInfo | None:
    """The app a preferences domain belongs to, for the policy (``None`` for the global domain)."""
    name = domain.strip()
    if name in {GLOBAL_DOMAIN, "-g", "-globalDomain"}:
        return None
    if "/" in name:  # a plist path such as ~/Library/Preferences/com.example.app.plist
        name = Path(name).name.removesuffix(".plist")
    return AppInfo(name=name, pid=0, bundle_id=name)


_TYPE_FLAGS: dict[str, DefaultsType] = {
    "-string": "string",
    "-int": "int",
    "-float": "float",
    "-bool": "bool",
}


@tool(
    "defaults",
    title="Read or write preferences",
    description=DEFAULTS_DESCRIPTION,
    destructive=True,
    windows_mcp_name="Defaults",
    confirms_itself=True,
)
def defaults_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``defaults`` tool."""

    async def defaults(
        domain: Annotated[
            str,
            Field(description="Bundle id, NSGlobalDomain or a plist path.", min_length=1, max_length=1024),
        ],
        *,
        action: Annotated[DefaultsAction, Field(description="read | write.")] = "read",
        key: Annotated[str | None, Field(description="The preference key.", max_length=1024)] = None,
        value: Annotated[str | int | float | bool | None, Field(description="write: the new value.")] = None,
        type: Annotated[
            DefaultsType | None, Field(description="write: force a type (default: from the value).")
        ] = None,
    ) -> Annotated[CallToolResult, DefaultsModel]:
        owner = _domain_app(domain)
        if owner is not None:
            owner = await _as_known(ctx, owner)
        if action == "read":
            if value is not None or type is not None:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "value and type only go with action=write.")
            if owner is not None:
                ctx.policy.check_app(owner, kind="observe")
            shown_key = f" {key}" if key else ""
            await ctx.confirm_action(
                f"Read the preference{shown_key} of {domain.strip()}?", destructive=False
            )
            printed = await ctx.run_engine(functools.partial(defaults_read, ctx.runner, domain, key))
            text, cut = _output(ctx, printed)
            model = DefaultsModel(action="read", domain=domain.strip(), key=key, value=text, truncated=cut)
            return ok_result(text, model)
        if key is None or value is None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "write needs key and value.",
                hint='Example: domain="com.apple.dock", key="autohide", value=true.',
            )
        if owner is not None:
            ctx.policy.check_app(owner, kind="manage")
        argv = defaults_write_argv(domain, key, value, type)  # validates everything first
        command = shlex.join(argv)
        await ctx.confirm_action("Change a macOS preference?", _shown(command), destructive=True)
        await ctx.run_engine(functools.partial(defaults_write, ctx.runner, domain, key, value, type))
        model = DefaultsModel(
            action="write",
            domain=argv[2],
            key=argv[3],
            value=argv[5],
            type=_TYPE_FLAGS[argv[4]],
            command=command,
        )
        return ok_result(f"wrote {argv[3]} = {argv[5]} ({model.type}) in {argv[2]}", model)

    return defaults
