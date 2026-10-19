"""System tools: ``clipboard``, ``notify``, ``spotlight_search``, ``system_info``,
``system_control`` (volume, appearance) and ``processes`` (list; stopping one asks the user).

Command-line integrations run through :attr:`ServerContext.runner` (a ``FakeRunner`` in tests)
in a worker thread, so a slow command never blocks the event loop. ``processes`` with
``action="kill"`` refuses pid ≤ 1, AXPilot itself and its parent, and processes of protected
apps, and asks the user before signalling anything.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import base64
import binascii
import functools
import io
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp_types import CallToolResult
from PIL import Image as PILImage
from PIL import UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.perception.format import quote
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import Image, ok_result
from axpilot.server.schemas import OutputModel
from axpilot.system.processes import (
    ProcessInfo,
    check_kill_allowed,
    kill_process,
    list_processes,
    process_exists,
)
from axpilot.system.spotlight import DEFAULT_TIMEOUT_S as SPOTLIGHT_TIMEOUT_S
from axpilot.system.spotlight import SPOTLIGHT_KINDS, spotlight_search
from axpilot.system.sysinfo import CONTROL_TIMEOUT_S, INFO_TIMEOUT_S, set_appearance, set_volume, system_info

__all__ = ["CLIPBOARD_IMAGE_MAX_BYTES", "CLIPBOARD_TEXT_MAX"]

CLIPBOARD_TEXT_MAX = 10_000
"""``clipboard`` get returns at most this much text (``truncated=true`` beyond)."""
CLIPBOARD_IMAGE_MAX_BYTES = 20_000_000
CLIPBOARD_FILES_MAX = 100
SPOTLIGHT_LIMIT_MAX = 200
PROCESS_LIMIT_MAX = 500
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

ClipboardAction = Literal["get", "set"]
ControlAction = Literal["volume", "mute", "unmute", "appearance"]
ProcessAction = Literal["list", "kill"]


# =========================================================================== clipboard
CLIPBOARD_DESCRIPTION = """\
Read or set the clipboard: text, an image (PNG) or files.
Use it to move data between apps or to read what the user copied. Params: action get|set; for set
exactly one of text, image_png_base64 (a PNG image) or files (absolute paths); include_image (get:
also return a copied image).
get returns the text (cut at 10,000 characters), whether an image or files are on it, and its data
types. The clipboard may hold private data: only read it when the task needs it.
Errors: INVALID_ARGUMENT (nothing or several things to set, bad image, missing file)."""


class ClipboardModel(OutputModel):
    """Output of ``clipboard``."""

    action: ClipboardAction
    text: str | None = Field(default=None, description="get: the text on the clipboard (maybe cut).")
    text_length: int = Field(default=0, description="get: length of the full text.")
    truncated: bool = False
    has_image: bool = False
    image_size: list[int] | None = Field(default=None, description="Pixel [width, height] of the image.")
    files: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list, description="Pasteboard data types (UTIs).")
    message: str | None = None


def _decode_png(data: str) -> tuple[bytes, list[int]]:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "image_png_base64 is not valid base64.") from exc
    if len(raw) > CLIPBOARD_IMAGE_MAX_BYTES:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"The image is larger than {CLIPBOARD_IMAGE_MAX_BYTES // 1_000_000} MB.",
        )
    if not raw.startswith(_PNG_MAGIC):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "image_png_base64 must be a PNG image.")
    try:
        with PILImage.open(io.BytesIO(raw)) as image:
            image.verify()
            size = [int(image.width), int(image.height)]
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, "image_png_base64 is not a readable PNG image."
        ) from exc
    return raw, size


def _png_size(data: bytes | None) -> list[int] | None:
    if not data:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as image:
            return [int(image.width), int(image.height)]
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _checked_files(files: list[str]) -> list[str]:
    paths: list[str] = []
    for entry in files:
        path = Path(os.path.expanduser(entry.strip()))
        if not path.is_absolute():
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"{entry!r} is not an absolute path.")
        if not path.exists():
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"{str(path)!r} does not exist.")
        paths.append(str(path))
    return paths


async def _clipboard_get(ctx: ServerContext, include_image: bool) -> CallToolResult:
    content = await ctx.run_engine(ctx.engine.clipboard_get)
    text = content.text
    cut = text[:CLIPBOARD_TEXT_MAX] if text is not None else None
    model = ClipboardModel(
        action="get",
        text=cut,
        text_length=len(text) if text is not None else 0,
        truncated=text is not None and len(text) > CLIPBOARD_TEXT_MAX,
        has_image=content.image_png is not None,
        image_size=_png_size(content.image_png),
        files=list(content.files),
        types=list(content.types),
    )
    parts: list[str] = []
    if cut:
        more = f" (first {CLIPBOARD_TEXT_MAX} of {model.text_length} characters)" if model.truncated else ""
        parts.append(f"clipboard text{more}:\n{cut}")
    if model.has_image:
        size = f" {model.image_size[0]}×{model.image_size[1]} px" if model.image_size else ""
        parts.append(f"clipboard image{size}" + ("" if include_image else " (include_image=true returns it)"))
    if model.files:
        parts.append("clipboard files:\n" + "\n".join(model.files))
    if not parts:
        parts.append("the clipboard is empty")
    images = [Image(content.image_png, "image/png")] if include_image and content.image_png else []
    return ok_result("\n".join(parts), model, images=images)


@tool(
    "clipboard",
    title="Clipboard",
    description=CLIPBOARD_DESCRIPTION,
    windows_mcp_name="Clipboard",
)
def clipboard_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``clipboard`` tool."""

    async def clipboard(
        *,
        action: Annotated[ClipboardAction, Field(description="get | set.")] = "get",
        text: Annotated[
            str | None, Field(description="set: text to put on the clipboard.", max_length=1_000_000)
        ] = None,
        image_png_base64: Annotated[
            str | None, Field(description="set: a PNG image, base64-encoded.", max_length=30_000_000)
        ] = None,
        files: Annotated[
            list[str] | None,
            Field(
                description="set: absolute paths of files to put on the clipboard.",
                max_length=CLIPBOARD_FILES_MAX,
            ),
        ] = None,
        include_image: Annotated[bool, Field(description="get: also return a copied image.")] = False,
    ) -> Annotated[CallToolResult, ClipboardModel]:
        candidates = (("text", text), ("image_png_base64", image_png_base64), ("files", files))
        given = [name for name, value in candidates if value is not None]
        if action == "get":
            if given:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, f"get takes no {', '.join(given)}; use action=set."
                )
            return await _clipboard_get(ctx, include_image)
        if len(given) != 1:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "set needs exactly one of text, image_png_base64 or files.",
                hint='Example: clipboard(action="set", text="hello").',
            )
        if files is not None:
            paths = _checked_files(files)
            if not paths:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "files is empty.")
            await ctx.run_engine(functools.partial(ctx.engine.clipboard_set, files=paths))
            model = ClipboardModel(action="set", files=paths, message=f"copied {len(paths)} file(s)")
        elif image_png_base64 is not None:
            raw, size = _decode_png(image_png_base64)
            await ctx.run_engine(functools.partial(ctx.engine.clipboard_set, image_png=raw))
            model = ClipboardModel(
                action="set",
                has_image=True,
                image_size=size,
                message=f"copied a {size[0]}×{size[1]} px image",
            )
        else:
            assert text is not None
            await ctx.run_engine(functools.partial(ctx.engine.clipboard_set, text=text))
            model = ClipboardModel(
                action="set", text_length=len(text), message=f"copied {len(text)} characters"
            )
        return ok_result(model.message or "done", model)

    return clipboard


# =========================================================================== notify
NOTIFY_DESCRIPTION = """\
Show a macOS notification to the user (for example when a long task finishes or needs them).
Params: title, message, subtitle. Returns once it was posted. Don't use it for chatter: the user
sees every notification."""


class NotifyModel(OutputModel):
    """Output of ``notify``."""

    title: str
    message: str
    subtitle: str | None = None


@tool(
    "notify",
    title="Show a notification",
    description=NOTIFY_DESCRIPTION,
    windows_mcp_name="Notification",
)
def notify_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``notify`` tool."""

    async def notify(
        *,
        title: Annotated[str, Field(description="Notification title.", min_length=1, max_length=256)],
        message: Annotated[str, Field(description="Notification body.", min_length=1, max_length=2000)],
        subtitle: Annotated[str | None, Field(description="Optional subtitle.", max_length=256)] = None,
    ) -> Annotated[CallToolResult, NotifyModel]:
        if not title.strip() or not message.strip():
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "title and message must not be blank.")
        await ctx.run_engine(ctx.engine.notify, title, message, subtitle)
        model = NotifyModel(title=title, message=message, subtitle=subtitle)
        return ok_result(f"notification shown: {quote(title, 80)}", model)

    return notify


# =========================================================================== spotlight
SPOTLIGHT_DESCRIPTION = f"""\
Find files, folders and apps with Spotlight (like typing into Spotlight, or mdfind).
Params: query (words; may be empty with a kind), kind ({", ".join(SPOTLIGHT_KINDS)}), scope (a folder
to search in, e.g. "~/Documents"), limit (≤ {SPOTLIGHT_LIMIT_MAX}).
Returns absolute paths, best first; open one with the open tool.
Errors: INVALID_ARGUMENT (bad kind, scope or query), TIMEOUT, NOT_SUPPORTED (Spotlight unavailable)."""


class SpotlightModel(OutputModel):
    """Output of ``spotlight_search``."""

    query: str
    kind: str | None = None
    scope: str | None = None
    paths: list[str]
    count: int


@tool(
    "spotlight_search",
    title="Spotlight search",
    description=SPOTLIGHT_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="Spotlight",
)
def spotlight_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``spotlight_search`` tool."""

    async def spotlight(
        *,
        query: Annotated[str, Field(description="What to look for.", max_length=1000)] = "",
        kind: Annotated[
            str | None, Field(description="app, pdf, image, folder, document…", max_length=32)
        ] = None,
        scope: Annotated[
            str | None, Field(description="Folder to search in (~ allowed).", max_length=1024)
        ] = None,
        limit: Annotated[int, Field(description="Most paths to return.", ge=1, le=SPOTLIGHT_LIMIT_MAX)] = 20,
    ) -> Annotated[CallToolResult, SpotlightModel]:
        if not query.strip() and not kind:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "Give a query, a kind, or both.",
                hint='Example: spotlight_search(query="invoice", kind="pdf").',
            )
        search = functools.partial(spotlight_search, ctx.runner, query, kind=kind, scope=scope, limit=limit)
        paths = await ctx.run_engine(search, timeout_s=SPOTLIGHT_TIMEOUT_S + 5)
        model = SpotlightModel(query=query, kind=kind, scope=scope, paths=paths, count=len(paths))
        head = f"{len(paths)} result{'s' if len(paths) != 1 else ''}"
        return ok_result("\n".join([head + (":" if paths else "."), *paths]), model)

    return spotlight


# =========================================================================== system_info
SYSTEM_INFO_DESCRIPTION = """\
Facts about this Mac: macOS version and build, model, CPU, memory, uptime and load, power source
and battery, light/dark appearance and volume.
Best effort: a source that is unavailable is left out and listed under "unavailable"."""


class SystemInfoModel(OutputModel):
    """Output of ``system_info`` (keys present when their source worked)."""

    info: dict[str, Any] = Field(
        description="os_name, os_version, os_build, model, cpu, memory_gb, uptime, …"
    )


def _info_lines(info: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if "os_version" in info:
        lines.append(f"{info.get('os_name', 'macOS')} {info['os_version']} ({info.get('os_build', '?')})")
    hardware = [str(info[key]) for key in ("model", "cpu") if key in info]
    if "memory_gb" in info:
        hardware.append(f"{info['memory_gb']} GB")
    if hardware:
        lines.append(" · ".join(hardware))
    if "uptime" in info:
        lines.append(f"up {info['uptime']}")
    battery = info.get("battery")
    if isinstance(battery, Mapping) and "percent" in battery:
        lines.append(f"battery {battery['percent']}% {battery.get('state', '')}".rstrip())
    if "appearance" in info:
        lines.append(f"appearance {info['appearance']}")
    volume = info.get("volume")
    if isinstance(volume, Mapping) and "output" in volume:
        muted = " (muted)" if volume.get("muted") else ""
        lines.append(f"volume {volume['output']}{muted}")
    if info.get("unavailable"):
        lines.append("unavailable: " + ", ".join(map(str, info["unavailable"])))
    return lines


@tool(
    "system_info",
    title="System information",
    description=SYSTEM_INFO_DESCRIPTION,
    read_only=True,
    idempotent=True,
    windows_mcp_name="SystemInfo",
)
def system_info_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``system_info`` tool."""

    async def info() -> Annotated[CallToolResult, SystemInfoModel]:
        facts = await ctx.run_engine(system_info, ctx.runner, timeout_s=INFO_TIMEOUT_S * 6)
        model = SystemInfoModel(info=facts)
        return ok_result("\n".join(_info_lines(facts)) or "no information available", model)

    return info


# =========================================================================== system_control
SYSTEM_CONTROL_DESCRIPTION = """\
Change simple system settings: output volume (0-100), mute / unmute, light or dark appearance.
Params: action (volume | mute | unmute | appearance); level (volume); mode (appearance: light | dark).
Returns the new state. Changing the appearance may trigger a one-time Automation permission prompt
(PERMISSION_AUTOMATION until the user allows it). Errors: INVALID_ARGUMENT, COMMAND_FAILED, TIMEOUT."""


class SystemControlModel(OutputModel):
    """Output of ``system_control``."""

    action: ControlAction
    volume: dict[str, Any] | None = Field(default=None, description="Volume settings afterwards.")
    appearance: Literal["light", "dark"] | None = None
    message: str


@tool(
    "system_control",
    title="Change volume or appearance",
    description=SYSTEM_CONTROL_DESCRIPTION,
    idempotent=True,
    windows_mcp_name="SystemControl",
)
def system_control_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``system_control`` tool."""

    async def system_control(
        *,
        action: Annotated[ControlAction, Field(description="volume | mute | unmute | appearance.")],
        level: Annotated[int | None, Field(description="volume: output volume 0-100.", ge=0, le=100)] = None,
        mode: Annotated[
            Literal["light", "dark"] | None, Field(description="appearance: light or dark.")
        ] = None,
    ) -> Annotated[CallToolResult, SystemControlModel]:
        if action == "volume" and level is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "action=volume needs level (0-100).")
        if action == "appearance" and mode is None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, 'action=appearance needs mode ("light" or "dark").'
            )
        if level is not None and action != "volume":
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"level only goes with action=volume, not {action}."
            )
        if mode is not None and action != "appearance":
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"mode only goes with action=appearance, not {action}."
            )
        timeout = CONTROL_TIMEOUT_S + 5
        if action == "appearance":
            assert mode is not None
            await ctx.run_engine(set_appearance, ctx.runner, mode == "dark", timeout_s=timeout)
            model = SystemControlModel(action=action, appearance=mode, message=f"appearance set to {mode}")
        else:
            change = (
                functools.partial(set_volume, ctx.runner, level=level)
                if action == "volume"
                else functools.partial(set_volume, ctx.runner, mute=action == "mute")
            )
            settings = await ctx.run_engine(change, timeout_s=timeout)
            done = {"volume": f"volume set to {level}", "mute": "sound muted", "unmute": "sound unmuted"}[
                action
            ]
            model = SystemControlModel(action=action, volume=settings, message=done)
        return ok_result(model.message, model)

    return system_control


# =========================================================================== processes
PROCESSES_DESCRIPTION = """\
List running processes (busiest first), or stop one.
Params: action list|kill; name (list: filter by executable name or path), limit; pid and force (kill:
SIGTERM, or SIGKILL with force=true). To quit an app, prefer the app tool (it lets the app save).
Returns pid, parent pid, CPU %, memory % and executable for each process. kill asks the user first and
refuses system processes (pid ≤ 1), AXPilot itself and protected apps.
Errors: INVALID_ARGUMENT (no such pid), BLOCKED_BY_POLICY, USER_DENIED, COMMAND_FAILED."""


class ProcessModel(BaseModel):
    """One running process."""

    model_config = ConfigDict(extra="forbid")

    pid: int
    ppid: int
    cpu: float
    mem: float
    command: str
    name: str


class ProcessesModel(OutputModel):
    """Output of ``processes``."""

    action: ProcessAction
    processes: list[ProcessModel] = Field(default_factory=list)
    count: int = 0
    pid: int | None = Field(default=None, description="kill: the process signalled.")
    signal: str | None = Field(default=None, description="kill: SIGTERM or SIGKILL.")
    message: str | None = None


def _process_model(info: ProcessInfo) -> ProcessModel:
    return ProcessModel(**info.to_dict())


def _process_line(process: ProcessModel) -> str:
    return f"{process.pid:>6} {process.cpu:5.1f}% cpu {process.mem:5.1f}% mem  {process.name}"


async def _kill(ctx: ServerContext, pid: int, force: bool) -> CallToolResult:
    """Refuse what must not be stopped, then ask, then signal: nobody is asked about a kill
    that cannot happen."""
    check_kill_allowed(pid)
    apps = await ctx.run_engine(ctx.engine.list_apps)
    owner = next((app for app in apps if app.pid == pid), None)
    if owner is not None:
        ctx.policy.check_app(owner, kind="manage")
    if not process_exists(pid):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"No process with pid {pid}.",
            hint="It may have exited already. Call processes to list current processes.",
        )
    what = f"{owner.name} (pid {pid})" if owner is not None else f"process {pid}"
    verb = "Force-kill" if force else "Stop"
    await ctx.confirm_action(f"{verb} {what}? Unsaved work in it may be lost.", destructive=True)
    signal_name = kill_process(pid, force=force)
    model = ProcessesModel(
        action="kill", pid=pid, signal=signal_name, message=f"sent {signal_name} to {what}"
    )
    return ok_result(model.message or "", model)


@tool(
    "processes",
    title="List or stop processes",
    description=PROCESSES_DESCRIPTION,
    destructive=True,
    windows_mcp_name="Process",
    confirm_when=lambda args: False,  # kill asks itself, after its safety checks
)
def processes_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``processes`` tool."""

    async def processes(
        *,
        action: Annotated[ProcessAction, Field(description="list | kill.")] = "list",
        name: Annotated[
            str | None, Field(description="list: keep processes whose path contains this.", max_length=256)
        ] = None,
        limit: Annotated[
            int, Field(description="list: most processes to return.", ge=1, le=PROCESS_LIMIT_MAX)
        ] = 30,
        pid: Annotated[int | None, Field(description="kill: the process id.")] = None,
        force: Annotated[bool, Field(description="kill: SIGKILL instead of SIGTERM.")] = False,
    ) -> Annotated[CallToolResult, ProcessesModel]:
        if action == "kill":
            if pid is None:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, "kill needs pid.", hint="List processes first."
                )
            return await _kill(ctx, pid, force)
        if pid is not None or force:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "pid and force only go with action=kill.")
        found = await ctx.run_engine(functools.partial(list_processes, ctx.runner, name=name, limit=limit))
        models = [_process_model(info) for info in found]
        model = ProcessesModel(action="list", processes=models, count=len(models))
        head = f"{len(models)} process{'es' if len(models) != 1 else ''}" + (
            f" matching {name!r}" if name else ""
        )
        return ok_result("\n".join([head + (":" if models else "."), *map(_process_line, models)]), model)

    return processes
