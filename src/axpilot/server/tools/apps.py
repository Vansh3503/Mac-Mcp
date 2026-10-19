"""App and window tools: ``app`` (launch, activate, hide, quit…), ``window`` (focus, move,
resize, minimize, close…) and ``open`` (URLs, files and folders).

These tools manage apps rather than type into them, so they apply the ``manage`` policy: deny
and allow lists, protected windows and AXPilot's own app, but not the Terminal rule (bringing a
terminal to the front types nothing into it). ``force_quit`` loses unsaved work, so it asks the
user once the target app is known and allowed.

``open`` only follows URL schemes that open content (:data:`OPEN_SCHEMES`). Other schemes are
app-defined commands (``shortcuts://run-shortcut``, ``zoommtg://``, an app's own automation
URLs) that can act inside apps without any UI an agent or the user would see, so they need the
``scripting`` toolset, like other ways of running app commands. For the same reason it does not
open URL files (``.webloc``…) without it, and without the ``shell`` tool it does not run code:
files the Mac would execute (``.command``, executables, :data:`RUNNABLE_SUFFIXES`) and anything
opened with a terminal app are blocked, like typing into a terminal.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import math
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlparse

from mcp_types import CallToolResult
from pydantic import Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppAction, AppInfo, Point, Rect, WindowAction, WindowInfo
from axpilot.perception.format import format_rect, quote
from axpilot.policy.profiles import TOOLSETS
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import AppModel, OutputModel, SnapshotIdArg, WindowModel
from axpilot.system.bundles import bundle_id_of

__all__ = ["OPEN_SCHEMES", "RUNNABLE_SUFFIXES", "URL_FILE_SUFFIXES", "app_from_query"]

OPEN_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "file", "x-apple.systempreferences"})
RUNNABLE_SUFFIXES: frozenset[str] = frozenset({".command", ".tool", ".terminal", ".jar"})
"""Files whose default app runs them (Terminal runs ``.command``/``.tool`` files and the command
saved in a ``.terminal`` file; Jar Launcher runs ``.jar``)."""
URL_FILE_SUFFIXES: frozenset[str] = frozenset({".webloc", ".inetloc", ".fileloc", ".url"})
"""Files that open the URL they contain, whatever its scheme."""
"""URL schemes ``open`` follows without the scripting toolset."""
WINDOW_MAX_SIZE = 20_000.0
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")

AppActionArg = Literal["launch", "activate", "hide", "unhide", "quit", "force_quit"]
WindowActionArg = Literal["focus", "move", "resize", "minimize", "restore", "zoom", "fullscreen", "close"]


# =========================================================================== shared
def app_from_query(query: str) -> AppInfo:
    """A best guess of the app a launch query names (name, bundle id or ``.app`` path), for the
    policy check before an app that is not running is resolved. For a path, the bundle id comes
    from the bundle's ``Info.plist`` when it can be read."""
    text = query.strip()
    if "/" in text:
        path = Path(os.path.expanduser(text.rstrip("/")))
        return AppInfo(
            name=path.name.removesuffix(".app"), pid=0, bundle_id=bundle_id_of(path), path=str(path)
        )
    if _BUNDLE_ID.match(text) and not text.casefold().endswith(".app"):
        return AppInfo(name=text, pid=0, bundle_id=text)
    return AppInfo(name=text.removesuffix(".app"), pid=0)


async def _installed(ctx: ServerContext, query: str) -> AppInfo:
    """The installed (or running) app ``query`` names, or a guess from the query itself."""
    try:
        apps = await ctx.run_engine(ctx.engine.list_apps, True)
    except AXPilotError:
        return app_from_query(query)
    folded = query.strip().casefold()
    for app in apps:
        if app.matches(query) or (app.path and app.path.rstrip("/").casefold() == folded.rstrip("/")):
            return app
    return app_from_query(query)


def _app_line(app: AppModel) -> str:
    states = [state for state, on in (("frontmost", app.frontmost), ("hidden", app.hidden)) if on]
    bundle = f", {app.bundle_id}" if app.bundle_id else ""
    return f"{app.name} (pid {app.pid}{bundle})" + (f" · {', '.join(states)}" if states else "")


# =========================================================================== app
APP_DESCRIPTION = """\
Launch, activate (bring to the front), hide, unhide, quit or force-quit an app.
Use it to switch apps instead of clicking the Dock. Params: action; name (app name such as "Notes",
bundle id such as "com.apple.Notes", or for launch an .app path).
Returns the app afterwards (pid, frontmost, hidden). quit asks the app to quit (it may ask to save
first); force_quit kills it without saving and asks the user to confirm.
Errors: APP_NOT_FOUND, BLOCKED_BY_POLICY (protected apps), USER_DENIED, INVALID_ARGUMENT."""


class AppActionModel(OutputModel):
    """Output of ``app``."""

    action: AppActionArg
    app: AppModel | None = Field(default=None, description="The app afterwards (None once it quit).")
    message: str


def _never(_: Mapping[str, Any]) -> bool:
    """The guard never asks: the tool asks itself once its target is known and allowed."""
    return False


@tool(
    "app",
    title="Manage an app",
    description=APP_DESCRIPTION,
    destructive=True,
    open_world=False,
    windows_mcp_name="App",
    confirm_when=_never,
)
def app_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``app`` tool."""

    async def app(
        *,
        action: Annotated[
            AppActionArg, Field(description="launch | activate | hide | unhide | quit | force_quit.")
        ],
        name: Annotated[
            str,
            Field(
                description='App name ("Notes"), bundle id ("com.apple.Notes") or, for launch, an .app path.',
                min_length=1,
                max_length=1024,
            ),
        ],
    ) -> Annotated[CallToolResult, AppActionModel]:
        query = name.strip()
        if not query:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "name is empty.", hint="Pass an app name or bundle id."
            )
        if action == "launch":
            target = await _installed(ctx, query)
        else:
            target = await ctx.run_engine(ctx.engine.find_app, query)
        ctx.policy.check_app(target, kind="manage")
        if action == "force_quit":
            await ctx.confirm_action(
                f"Force quit {target.name}? Unsaved changes in it will be lost.",
                destructive=True,
            )
        engine_action: AppAction = action
        after = await ctx.run_engine(ctx.engine.app_control, engine_action, query)
        if after is not None:
            ctx.policy.check_app(after, kind="manage")  # what actually launched (an alias, a path)
        model = AppActionModel(
            action=action,
            app=AppModel.from_info(after) if after is not None else None,
            message=_app_message(action, target, after),
        )
        lines = [model.message]
        if model.app is not None:
            lines.append(_app_line(model.app))
        return ok_result("\n".join(lines), model)

    return app


def _app_message(action: str, target: AppInfo, after: AppInfo | None) -> str:
    name = after.name if after is not None else target.name
    return {
        "launch": f"launched {name}",
        "activate": f"activated {name}",
        "hide": f"hid {name}",
        "unhide": f"unhid {name}",
        "quit": f"asked {name} to quit",
        "force_quit": f"force-quit {name}",
    }[action]


# =========================================================================== window
WINDOW_DESCRIPTION = """\
Focus, move, resize, minimize, restore, zoom, toggle full screen or close a window.
Params: action; window_id (from list_windows) or ref (any element of a snapshot: its window);
x + y (move: the window's new top-left corner, screen points); width + height (resize).
Returns the window afterwards (frame and state); close returns closed=true (the app may ask to save
first). Errors: WINDOW_NOT_FOUND, STALE_REF, BLOCKED_BY_POLICY, INVALID_ARGUMENT."""


class WindowActionModel(OutputModel):
    """Output of ``window``."""

    action: WindowActionArg
    window: WindowModel | None = Field(default=None, description="The window afterwards (None once closed).")
    closed: bool = False
    message: str


def _geometry(
    action: str, x: float | None, y: float | None, width: float | None, height: float | None
) -> Rect | None:
    """The rect ``window_control`` expects for ``action``; rejects arguments the action ignores."""
    values = [v for v in (x, y, width, height) if v is not None]
    if any(not math.isfinite(v) for v in values):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "x, y, width and height must be finite numbers.")
    if action == "move":
        if x is None or y is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "move needs x and y (the new top-left corner).")
        if width is not None or height is not None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "move takes x and y only; use resize for the size."
            )
        return Rect(x, y, 0.0, 0.0)
    if action == "resize":
        if width is None or height is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "resize needs width and height.")
        if x is not None or y is not None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "resize takes width and height only; use move for x/y."
            )
        if not (0 < width <= WINDOW_MAX_SIZE and 0 < height <= WINDOW_MAX_SIZE):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"width and height must be between 1 and {WINDOW_MAX_SIZE:g} points.",
            )
        return Rect(0.0, 0.0, width, height)
    if values:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{action} takes no x, y, width or height.",
            hint="Use action=move or action=resize to change the frame.",
        )
    return None


async def _window_of(
    ctx: ServerContext, window_id: int | None, ref: str | None, snapshot_id: str | None
) -> int:
    if (window_id is None) == (ref is None):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            "Give exactly one of window_id and ref.",
            hint="window_id comes from list_windows; ref is any element of a snapshot of that window.",
        )
    if window_id is not None:
        return window_id
    assert ref is not None
    snap, _ = ctx.snapshots.resolve(ref, snapshot_id)
    if snap.window is None:
        raise AXPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"Snapshot {snap.snapshot_id} of {snap.app.name} has no window.",
            hint="Use list_windows and pass window_id.",
        )
    return snap.window.window_id


async def _on_a_display(ctx: ServerContext, point: Point) -> None:
    displays = await ctx.run_engine(ctx.engine.list_displays)
    if displays and not any(d.frame.contains(point) for d in displays):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"({point.x:g}, {point.y:g}) is outside every display; the window would be unreachable.",
            hint="Use list_displays for the screen areas (global points).",
        )


def _window_line(window: WindowModel) -> str:
    states = [s for s, on in (("focused", window.focused), ("minimized", window.minimized),
                              ("fullscreen", window.fullscreen)) if on]  # fmt: skip
    title = quote(window.title, 60) if window.title else '""'
    frame = format_rect(Rect(*window.frame))
    return f"window {window.window_id} {window.app} {title} {frame}" + (
        f" · {', '.join(states)}" if states else ""
    )


@tool(
    "window",
    title="Manage a window",
    description=WINDOW_DESCRIPTION,
    windows_mcp_name="Window",
)
def window_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``window`` tool."""

    async def window(
        *,
        action: Annotated[
            WindowActionArg,
            Field(description="focus | move | resize | minimize | restore | zoom | fullscreen | close."),
        ],
        window_id: Annotated[int | None, Field(description="Window id from list_windows.", ge=0)] = None,
        ref: Annotated[
            str | None,
            Field(description="Any element ref of a snapshot of the window.", min_length=2, max_length=32),
        ] = None,
        snapshot_id: SnapshotIdArg = None,
        x: Annotated[float | None, Field(description="move: new left edge (screen points).")] = None,
        y: Annotated[float | None, Field(description="move: new top edge (screen points).")] = None,
        width: Annotated[float | None, Field(description="resize: new width in points.")] = None,
        height: Annotated[float | None, Field(description="resize: new height in points.")] = None,
    ) -> Annotated[CallToolResult, WindowActionModel]:
        rect = _geometry(action, x, y, width, height)
        target_id = await _window_of(ctx, window_id, ref, snapshot_id)
        _, info = await ctx.check_target(app=None, window_id=target_id, kind="manage")
        assert info is not None
        if action == "move" and rect is not None:
            await _on_a_display(ctx, Point(rect.x + 1, rect.y + 1))
        engine_action: WindowAction = action
        after: WindowInfo | None = await ctx.run_engine(
            ctx.engine.window_control, target_id, engine_action, rect
        )
        model = WindowActionModel(
            action=action,
            window=WindowModel.from_info(after) if after is not None else None,
            closed=action == "close",
            message=f"{action} window {target_id} ({info.app} {quote(info.title, 60)})",
        )
        lines = [model.message]
        if model.window is not None:
            lines.append(_window_line(model.window))
        return ok_result("\n".join(lines), model)

    return window


# =========================================================================== open
OPEN_DESCRIPTION = """\
Open a URL, file or folder with its default app (or with_app), or reveal a file in the Finder.
Params: target (https://…, mailto:…, x-apple.systempreferences:…, an absolute path or ~/path, or a
file:// URL), with_app (app name or bundle id), reveal (select it in a Finder window).
Only http, https, mailto, file and x-apple.systempreferences URLs are opened; other schemes are app
commands and need the scripting toolset. Without the shell tool, files that would run (.command,
executables) and anything opened with a terminal app are blocked. Returns what was opened.
Errors: INVALID_ARGUMENT (empty target, missing file, relative path), BLOCKED_BY_POLICY (URL scheme,
protected app), APP_NOT_FOUND (with_app)."""


class OpenModel(OutputModel):
    """Output of ``open``."""

    target: str
    kind: Literal["url", "file", "folder", "app"]
    with_app: str | None = None
    revealed: bool = False
    message: str


def _scripting_enabled(ctx: ServerContext) -> bool:
    return any(name in ctx.enabled for name in TOOLSETS["scripting"])


def _local_path(target: str) -> Path:
    path = Path(os.path.expanduser(target))
    if not path.is_absolute():
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{target!r} is a relative path.",
            hint="Use an absolute path or ~/path (spotlight_search finds files by name).",
        )
    if not path.exists():
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{str(path)!r} does not exist.",
            hint="Check the path, or find the file with spotlight_search.",
        )
    return path


def _classify(ctx: ServerContext, target: str) -> tuple[str, Path | None, list[str]]:
    """``(kind, local path or None, warnings)`` for an ``open`` target; refuses what is not allowed."""
    match = _SCHEME.match(target)
    if match is None or target.startswith(("/", "~")):
        path = _local_path(target)
        return ("app" if path.suffix == ".app" else "folder" if path.is_dir() else "file"), path, []
    scheme = match.group(1).lower()
    if scheme == "file":
        parsed = urlparse(target)
        if parsed.netloc not in ("", "localhost"):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "file URLs must point to this Mac (file:///path).")
        path = _local_path(unquote(parsed.path))
        return ("app" if path.suffix == ".app" else "folder" if path.is_dir() else "file"), path, []
    if scheme in OPEN_SCHEMES:
        return "url", None, []
    if _scripting_enabled(ctx):
        return (
            "url",
            None,
            [f"Opened a {scheme}: URL, an app command (allowed because scripting is enabled)."],
        )
    raise AXPilotError(
        ErrorCode.BLOCKED_BY_POLICY,
        f"Opening {scheme}: URLs is blocked: they are commands handled by apps, not content.",
        hint="Use http(s), mailto or file targets, or drive the app with its UI. To allow app URL "
        "schemes, the user can enable the scripting toolset.",
        details={"rule": "open_schemes", "scheme": scheme},
    )


def _runs_code(what: str) -> AXPilotError:
    return AXPilotError(
        ErrorCode.BLOCKED_BY_POLICY,
        f"{what}, which amounts to running shell commands, and the `shell` tool is disabled.",
        hint="To look at the file, open it with_app a viewer or editor (TextEdit, Xcode…). The user "
        'can allow running code with [tools] enable = ["shell"].',
        details={"rule": "terminal"},
    )


def _check_file_runs_nothing(ctx: ServerContext, path: Path, *, default_app: bool) -> None:
    """Refuse URL files, and files the default app would run (an explicit ``with_app`` that is not a
    terminal only shows them)."""
    suffix = path.suffix.casefold()
    if suffix in URL_FILE_SUFFIXES and not _scripting_enabled(ctx):
        raise AXPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Opening the URL file {quote(path.name, 80)} is blocked: it can hold any URL scheme, "
            "including app commands.",
            hint="Open the URL itself (http, https, mailto…) instead.",
            details={"rule": "open_schemes"},
        )
    if ctx.policy.shell_enabled or not default_app:
        return
    if suffix in RUNNABLE_SUFFIXES:
        raise _runs_code(f"Opening {quote(path.name, 80)} runs it")
    if os.access(path, os.X_OK):
        raise _runs_code(f"{quote(path.name, 80)} is executable, so opening it runs it in Terminal")


@tool(
    "open",
    title="Open a URL or file",
    description=OPEN_DESCRIPTION,
    open_world=True,
    windows_mcp_name="Open",
)
def open_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``open`` tool."""

    async def open(
        *,
        target: Annotated[
            str,
            Field(description="URL, absolute path, ~/path or file:// URL.", min_length=1, max_length=4096),
        ],
        with_app: Annotated[
            str | None,
            Field(description="Open with this app (name or bundle id).", min_length=1, max_length=256),
        ] = None,
        reveal: Annotated[
            bool, Field(description="Select the file in a Finder window instead of opening it.")
        ] = False,
    ) -> Annotated[CallToolResult, OpenModel]:
        text = target.strip()
        if not text:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Nothing to open: target is empty.")
        kind, path, warnings = _classify(ctx, text)
        if reveal and path is None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "reveal works for files and folders, not URLs.")
        if reveal and with_app is not None:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "reveal always uses the Finder; omit with_app.")
        if kind == "app" and path is not None and not reveal:
            ctx.policy.check_app(await _installed(ctx, str(path)), kind="manage")
        if kind == "file" and path is not None and not reveal:
            _check_file_runs_nothing(ctx, path, default_app=with_app is None)
        app_name: str | None = None
        if with_app is not None:
            app_info = await _installed(ctx, with_app)
            ctx.policy.check_app(app_info, kind="manage")
            if ctx.policy.is_terminal(app_info) and not ctx.policy.shell_enabled:
                raise _runs_code(f"Opening {quote(text, 80)} with {app_info.name} runs it in a terminal")
            app_name = app_info.name
        opened = str(path) if path is not None else text
        await ctx.run_engine(ctx.engine.open_target, opened, with_app, reveal)
        verb = "revealed" if reveal else "opened"
        how = f" with {app_name}" if app_name else ""
        model = OpenModel(
            target=opened,
            kind=kind,  # type: ignore[arg-type]
            with_app=app_name,
            revealed=reveal,
            message=f"{verb} {kind} {quote(opened, 120)}{how}",
        )
        return ok_result(model.message, model, warnings=warnings)

    return open
