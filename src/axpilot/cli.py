"""The ``axpilot`` command line.

Serving MCP:

* ``axpilot stdio``: what MCP clients run. By default a bridge to the host behind
  ``AXPilot.app`` (started on demand); ``--direct`` runs the server in this process, and
  ``--engine fake`` runs it against a simulated desktop (anywhere, no permissions needed).
* ``axpilot http``: Streamable HTTP on 127.0.0.1 with a bearer token.
* ``axpilot host``: the host itself (what the launcher runs).

Setting up and checking: ``setup``, ``update``, ``uninstall``, ``doctor``, ``connect`` /
``disconnect`` (MCP client configs), ``config``, ``skill install``.

Scripting and debugging: ``tools`` (the tool list and schemas), ``call`` (run one tool, print its
structured result as JSON), ``debug dump-tree``, ``version``.

Exit codes (QUALITY.md A9): ``0`` ok, ``1`` operational error, ``2`` usage error, ``3`` missing
permission, ``4`` unsupported OS, ``130`` interrupted. Errors print one line and a hint on stderr
(``--json`` prints ``{"error": {...}}`` on stdout instead); tracebacks only with ``--debug``. In
``stdio`` mode nothing but JSON-RPC is ever written to stdout.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, NoReturn

import anyio
import typer

from axpilot import __version__, paths
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.runloop import Pump, main_thread_pump, run_with_pump

__all__ = ["app", "exit_code_for", "main"]

_log = logging.getLogger("axpilot.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PERMISSION = 3
EXIT_UNSUPPORTED = 4
EXIT_INTERRUPTED = 130

_PERMISSION_CODES = frozenset(
    {
        ErrorCode.PERMISSION_ACCESSIBILITY,
        ErrorCode.PERMISSION_SCREEN_RECORDING,
        ErrorCode.PERMISSION_AUTOMATION,
    }
)


def exit_code_for(err: AXPilotError) -> int:
    """The process exit code for an error (see the module docstring)."""
    if err.code in _PERMISSION_CODES or err.code.value == "LAUNCHER_CHANGED":
        return EXIT_PERMISSION
    if err.code is ErrorCode.UNSUPPORTED_OS:
        return EXIT_UNSUPPORTED
    if err.code is ErrorCode.INVALID_ARGUMENT:
        return EXIT_USAGE
    return EXIT_ERROR


@dataclass
class _State:
    debug: bool = False
    json: bool = False


_state = _State()


class Engine(StrEnum):
    auto = "auto"
    macos = "macos"
    fake = "fake"


class SkillTarget(StrEnum):
    claude = "claude"
    claude_project = "claude-project"
    codex = "codex"


class Scope(StrEnum):
    user = "user"
    project = "project"


app = typer.Typer(
    name="axpilot",
    help="AXPilot: an MCP server that lets AI agents see and operate macOS, safely.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
config_app = typer.Typer(help="Read and change AXPilot's configuration.", no_args_is_help=True)
skill_app = typer.Typer(help="Install the AXPilot Agent Skill for coding agents.", no_args_is_help=True)
debug_app = typer.Typer(help="Debugging helpers.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(skill_app, name="skill")
app.add_typer(debug_app, name="debug")

JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON.")]
EngineOption = Annotated[
    Engine, typer.Option("--engine", help="auto (macOS), macos, or fake (a simulated desktop).")
]
FixtureOption = Annotated[
    Path | None, typer.Option("--fixture", help="Desktop fixture for --engine fake.", dir_okay=False)
]
ProfileOption = Annotated[str | None, typer.Option("--profile", help="minimal | standard | full | computer.")]
NamingOption = Annotated[str | None, typer.Option("--naming", help="default | windows-mcp.")]
YesOption = Annotated[
    bool, typer.Option("--yes", "-y", help="Approve AXPilot's confirmations without asking.")
]


# =========================================================================== plumbing
def _out(text: str) -> None:
    sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text + ("\n" if not text.endswith("\n") else ""))
    sys.stderr.flush()


def _print_json(data: Any) -> None:
    _out(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _fail(err: AXPilotError) -> NoReturn:
    raise err


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Logs to whatever ``sys.stderr`` is at the time (never stdout: it may carry JSON-RPC)."""

    @property  # type: ignore[override]
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


def _setup_logging(debug: bool) -> None:
    logger = logging.getLogger("axpilot")
    handler = next((h for h in logger.handlers if isinstance(h, _StderrHandler)), None)
    if handler is None:
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("axpilot: %(levelname)s %(message)s"))
        logger.addHandler(handler)
    level = logging.DEBUG if debug else logging.WARNING
    handler.setLevel(level)
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    # The MCP SDK puts a handler on the root logger; AXPilot's records are printed once, here.
    logger.propagate = False


def _run(coroutine: Callable[[], Coroutine[Any, Any, Any]], pump: Pump | None = None) -> Any:
    # On macOS the main thread's Cocoa run loop must keep turning (keyboard layout, app list);
    # ``pump`` (the UI's, which also dispatches AppKit events) replaces the plain one.
    return anyio.run(run_with_pump, coroutine, pump or main_thread_pump())


def _start_ui(ctx: Any) -> Any:
    """The kill switch and the on-screen indicator (``axpilot.host.ui``) for a server that drives
    the real Mac; ``None`` elsewhere, for the simulated desktop, or when both are off or AppKit
    fails (the server then runs without them)."""
    from axpilot.engine.fake import FakeEngine  # noqa: PLC0415

    if sys.platform != "darwin" or isinstance(ctx.engine, FakeEngine):
        return None
    from axpilot.host import ui  # noqa: PLC0415 - imports PyObjC lazily, macOS only

    safety = ctx.config.safety
    return ui.start(ctx.control, kill_switch=safety.kill_switch, indicator=safety.indicator)


@app.callback()
def _root(
    debug: Annotated[bool, typer.Option("--debug", help="Verbose logs and tracebacks on stderr.")] = False,
) -> None:
    _state.debug = debug
    _setup_logging(debug)


def _config(**overrides: Any) -> Any:
    from axpilot.config import load_config  # noqa: PLC0415 - keep `axpilot --help` fast

    return load_config(overrides={key: value for key, value in overrides.items() if value is not None})


def _confirmer(yes: bool, engine_kind: str) -> Any:
    """``--yes`` approves; on a Mac a dialog asks; elsewhere (and for the fake engine) nobody can
    be asked, so confirmations are declined with a hint to pass ``--yes``."""
    from axpilot.policy.confirm import AutoConfirmer, DenyConfirmer  # noqa: PLC0415

    if yes:
        return AutoConfirmer(allow=True)
    if sys.platform == "darwin" and engine_kind != "fake":
        from axpilot.host.dialogs import DialogConfirmer  # noqa: PLC0415

        return DialogConfirmer()
    return DenyConfirmer()


def _context(
    engine: Engine,
    fixture: Path | None,
    *,
    yes: bool,
    log: bool = True,
    overrides: dict[str, Any] | None = None,
) -> Any:
    from axpilot.engine import create_engine  # noqa: PLC0415
    from axpilot.server.context import ServerContext, default_log_file  # noqa: PLC0415

    config = _config(**(overrides or {}))
    kind = engine.value if fixture is None else "fake"
    instance = create_engine(kind, fixture=fixture)
    return ServerContext.create(
        config,
        instance,
        confirmer=_confirmer(yes, kind),
        log_path=default_log_file() if log else None,
        elicitation=False if yes else None,  # --yes approves without asking anyone
    )


async def _until_signalled(serve: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    """Run ``serve`` until it returns or SIGTERM/SIGINT arrives (then cancel it cleanly)."""
    async with anyio.create_task_group() as tg:

        async def watch() -> None:
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for received in signals:
                    _log.info("Stopping on %s", signal.Signals(received).name)
                    tg.cancel_scope.cancel()
                    return

        tg.start_soon(watch)
        await serve()
        tg.cancel_scope.cancel()


# =========================================================================== version
@app.command()
def version(as_json: JsonOption = False) -> None:
    """Print AXPilot's version."""
    info = {"axpilot": __version__, "python": sys.version.split()[0], "platform": sys.platform}
    if as_json:
        _print_json(info)
    else:
        _out(f"axpilot {__version__} (Python {info['python']}, {sys.platform})")


# =========================================================================== serving
@app.command()
def stdio(
    *,
    direct: Annotated[
        bool, typer.Option("--direct", help="Run the server in this process instead of via AXPilot.app.")
    ] = False,
    engine: EngineOption = Engine.auto,
    fixture: FixtureOption = None,
    profile: ProfileOption = None,
    naming: NamingOption = None,
    yes: YesOption = False,
) -> None:
    """Serve MCP over stdin/stdout (what MCP clients run).

    Without --direct or the fake engine this bridges to the AXPilot host, starting AXPilot.app if
    needed. When the host cannot be reached (AXPilot.app not installed, the host does not come
    up, or it refuses this client), the error goes to stderr and a stand-in still answers the MCP
    handshake, listing the usual tools and returning the error from every tool call, so the client
    shows the reason instead of "server failed to start"."""
    in_process = direct or engine is Engine.fake or fixture is not None
    if in_process:
        from axpilot.server.app import build_server  # noqa: PLC0415

        ctx = _context(engine, fixture, yes=yes, overrides={"tools.profile": profile, "tools.naming": naming})
        ui = _start_ui(ctx)
        try:
            server = build_server(ctx)
            _run(server.run_stdio_async, ui.pump if ui is not None else None)
        finally:
            if ui is not None:
                ui.close()
            ctx.close()
        return
    from axpilot.bridge.launch import ensure_host_running, identify_parent  # noqa: PLC0415
    from axpilot.bridge.offline import STAND_IN_ERRORS  # noqa: PLC0415
    from axpilot.bridge.protocol import Hello  # noqa: PLC0415
    from axpilot.bridge.stdio_pipe import run_bridge  # noqa: PLC0415

    try:
        socket_path = ensure_host_running()
        hello = Hello(client=identify_parent(), profile=profile, naming=naming)
        code = _run(lambda: run_bridge(socket_path, hello))  # raises only before the session starts
    except AXPilotError as err:
        if err.code not in STAND_IN_ERRORS:
            raise
        _serve_stand_in(err, profile=profile, naming=naming)
        return
    raise typer.Exit(code)


def _serve_stand_in(err: AXPilotError, *, profile: str | None, naming: str | None) -> None:
    """Report ``err`` on stderr, then answer the MCP session with it (see :mod:`axpilot.bridge.offline`)."""
    from axpilot.bridge.offline import build_unavailable_server  # noqa: PLC0415

    _report_error(err)
    _err("axpilot: answering the MCP client anyway; every tool call returns this error.")
    server = build_unavailable_server(err, _config(), profile=profile, naming=naming)
    _run(server.run_stdio_async)


@app.command()
def host(
    *,
    engine: EngineOption = Engine.auto,
    fixture: FixtureOption = None,
    socket: Annotated[Path | None, typer.Option("--socket", help="Socket path (default: AXPilot's).")] = None,
    max_clients: Annotated[int, typer.Option("--max-clients", min=1, max=64)] = 8,
    yes: YesOption = False,
) -> None:
    """Run the AXPilot host on its Unix socket (what AXPilot.app starts)."""
    from axpilot.host.daemon import launcher_pid, serve  # noqa: PLC0415

    ctx = _context(engine, fixture, yes=yes)
    ctx.mode = "host"
    parent = launcher_pid()  # read at once: the launcher may die while the host starts
    ui = _start_ui(ctx)
    try:
        _run(
            lambda: _until_signalled(lambda: serve(ctx, socket, max_clients=max_clients, parent_pid=parent)),
            ui.pump if ui is not None else None,
        )
    finally:
        if ui is not None:
            ui.close()
        ctx.close()


@app.command()
def http(
    *,
    bind: Annotated[
        str | None, typer.Option("--host", help="Address to listen on (default: [server] http_host).")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", min=1, max=65535, help="Port (default: [server] http_port).")
    ] = None,
    engine: EngineOption = Engine.auto,
    fixture: FixtureOption = None,
    profile: ProfileOption = None,
    naming: NamingOption = None,
    yes: YesOption = False,
    allow_remote: Annotated[
        bool, typer.Option("--allow-remote", help="Allow a non-loopback address (needs TLS).")
    ] = False,
    tls_cert: Annotated[Path | None, typer.Option("--tls-cert", dir_okay=False)] = None,
    tls_key: Annotated[Path | None, typer.Option("--tls-key", dir_okay=False)] = None,
    allow_origin: Annotated[
        list[str] | None, typer.Option("--allow-origin", help="A browser Origin to accept (repeatable).")
    ] = None,
    show_token: Annotated[
        bool, typer.Option("--show-token", help="Print the bearer token on stderr.")
    ] = False,
) -> None:
    """Serve MCP over Streamable HTTP (loopback only, bearer token required).

    Options left out come from the [server] section of the configuration."""
    from axpilot.host.http import MCP_PATH, check_bind, get_token, run_http  # noqa: PLC0415

    settings = _config().server
    address = bind or settings.http_host
    listen_port = port or settings.http_port
    remote = allow_remote or settings.allow_remote
    cert = tls_cert or (Path(settings.tls_cert).expanduser() if settings.tls_cert else None)
    key = tls_key or (Path(settings.tls_key).expanduser() if settings.tls_key else None)
    check_bind(address, allow_remote=remote, tls_cert=cert, tls_key=key)
    stored = get_token(settings.token_store)
    token = stored.value
    ctx = _context(engine, fixture, yes=yes)
    scheme = "https" if cert else "http"
    shown = f"[{address}]" if ":" in address else address
    _err(f"AXPilot MCP over HTTP at {scheme}://{shown}:{listen_port}{MCP_PATH}")
    _err(f"Bearer token: {token if show_token else f'in {stored.location}'}")
    try:
        _run(
            lambda: _until_signalled(
                lambda: run_http(
                    ctx,
                    token=token,
                    host=address,
                    port=listen_port,
                    allow_remote=remote,
                    tls_cert=cert,
                    tls_key=key,
                    allowed_origins=allow_origin or [],
                    profile=profile,
                    naming=naming,
                )
            )
        )
    finally:
        ctx.close()


# =========================================================================== setup
@app.command()
def setup(
    *,
    login_item: Annotated[bool, typer.Option("--login-item", help="Start AXPilot at login.")] = False,
    launch: Annotated[bool, typer.Option("--launch", help="Open AXPilot.app when done.")] = False,
    dev_source: Annotated[
        Path | None, typer.Option("--dev-source", help="Install the runtime from this checkout or wheel.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Prepare launcher mode: runtime, AXPilot.app, and optionally a login item."""
    _setup_command(login_item=login_item, launch=launch, dev_source=dev_source, as_json=as_json)


@app.command()
def update(as_json: JsonOption = False) -> None:
    """Update the runtime AXPilot.app runs to this AXPilot version (re-runs setup), then offer to
    restart AXPilot if its host still runs the previous version."""
    from axpilot.setup import offer_host_restart, restart_host  # noqa: PLC0415

    def confirm(question: str) -> bool:
        return typer.confirm(question, default=False)

    def after(report: Any, runner: Any) -> None:
        interactive = not as_json and sys.stdin.isatty()
        offer_host_restart(
            report, confirm=confirm if interactive else None, restart=lambda: restart_host(runner)
        )

    _setup_command(login_item=False, launch=False, dev_source=None, as_json=as_json, after=after)


def _setup_command(
    *,
    login_item: bool,
    launch: bool,
    dev_source: Path | None,
    as_json: bool,
    after: Callable[[Any, Any], None] | None = None,
) -> None:
    from axpilot.setup import run_setup  # noqa: PLC0415
    from axpilot.system.runner import CommandRunner  # noqa: PLC0415

    _state.json = as_json
    runner = CommandRunner()
    report = run_setup(runner=runner, login_item=login_item, dev_source=dev_source, launch_app=launch)
    if after is not None:
        after(report, runner)
    if as_json:
        _print_json(report.to_dict())
    else:
        _out(report.format())
    if not report.supported:
        raise typer.Exit(EXIT_UNSUPPORTED)
    if not report.ok:
        raise typer.Exit(EXIT_ERROR)


@app.command()
def uninstall(
    *,
    remove_launcher: Annotated[
        bool,
        typer.Option(
            "--remove-launcher", help="Also delete AXPilot.app (permissions must be granted again)."
        ),
    ] = False,
    yes: YesOption = False,
    as_json: JsonOption = False,
) -> None:
    """Remove the login item, the runtime and its manifest (keeps your config and logs)."""
    from axpilot.setup.launcher import remove_login_item  # noqa: PLC0415

    _state.json = as_json
    targets = [paths.runtime_dir(), paths.runtime_manifest()] + (
        [paths.launcher_app_path()] if remove_launcher else []
    )
    if not yes and not typer.confirm(
        "Remove " + ", ".join(str(t) for t in targets) + " and the login item?", default=False
    ):
        raise typer.Exit(EXIT_ERROR)
    removed: list[str] = []
    if remove_login_item():
        removed.append("login item")
    for target in targets:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
            removed.append(str(target))
        elif target.exists() or target.is_symlink():
            target.unlink()
            removed.append(str(target))
    if as_json:
        _print_json({"removed": removed})
    else:
        _out("Removed: " + (", ".join(removed) if removed else "nothing (AXPilot was not set up)."))


@app.command()
def doctor(as_json: JsonOption = False) -> None:
    """Check the installation, permissions, host and client configuration."""
    from axpilot.doctor import checks_payload, format_report, run_checks, worst_status  # noqa: PLC0415
    from axpilot.engine import create_engine  # noqa: PLC0415
    from axpilot.system.runner import CommandRunner  # noqa: PLC0415

    _state.json = as_json
    engine = None
    if sys.platform == "darwin":
        try:
            engine = create_engine("macos")
        except AXPilotError as err:
            _log.warning("Permission checks skipped: %s", err.message)
    try:
        checks = run_checks(engine, runner=CommandRunner())
    finally:
        if engine is not None:
            engine.close()
    worst = worst_status(checks)
    if as_json:
        _print_json({"status": worst, "checks": checks_payload(checks)})
    else:
        _out(format_report(checks))
    if any(check.name == "Platform" and check.status == "fail" for check in checks):
        raise typer.Exit(EXIT_UNSUPPORTED)
    if worst == "fail":
        raise typer.Exit(EXIT_ERROR)


# =========================================================================== clients
@app.command()
def connect(
    *,
    client: Annotated[str | None, typer.Argument(help="The MCP client, e.g. claude-desktop, cursor.")] = None,
    list_clients: Annotated[bool, typer.Option("--list", help="List the supported clients.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change.")] = False,
    scope: Annotated[Scope, typer.Option("--scope")] = Scope.user,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", file_okay=False)] = None,
    force: Annotated[bool, typer.Option("--force", help="Rewrite a commented JSONC file.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Add AXPilot to an MCP client's configuration."""
    from axpilot.clients.connect import connect as do_connect  # noqa: PLC0415
    from axpilot.clients.connect import resolve_command  # noqa: PLC0415

    _state.json = as_json
    if list_clients or client is None:
        _list_clients(as_json)
        return
    command, args = resolve_command()
    result = do_connect(
        client,
        command=command,
        args=args,
        scope=scope.value,
        project_dir=project_dir,
        dry_run=dry_run,
        force=force,
    )
    _report_connect(result, as_json)


@app.command()
def disconnect(
    *,
    client: Annotated[str, typer.Argument(help="The MCP client to remove AXPilot from.")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    scope: Annotated[Scope, typer.Option("--scope")] = Scope.user,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", file_okay=False)] = None,
    as_json: JsonOption = False,
) -> None:
    """Remove AXPilot from an MCP client's configuration."""
    from axpilot.clients.connect import disconnect as do_disconnect  # noqa: PLC0415

    _state.json = as_json
    result = do_disconnect(client, scope=scope.value, project_dir=project_dir, dry_run=dry_run)
    _report_connect(result, as_json)


def _list_clients(as_json: bool) -> None:
    from axpilot.clients.targets import TARGETS, detect_installed  # noqa: PLC0415

    installed = {target.name for target in detect_installed()}
    rows = [
        {"name": t.name, "display_name": t.display_name, "installed": t.name in installed} for t in TARGETS
    ]
    if as_json:
        _print_json({"clients": rows})
        return
    width = max(len(row["name"]) for row in rows)
    for row in rows:
        mark = "installed" if row["installed"] else ""
        _out(f"{row['name'].ljust(width)}  {row['display_name']}  {mark}".rstrip())
    _out("\nConnect one with: axpilot connect <name>")


def _report_connect(result: Any, as_json: bool) -> None:
    if as_json:
        _print_json(
            {
                "target": result.target,
                "path": str(result.path),
                "changed": result.changed,
                "dry_run": result.dry_run,
                "manual": result.manual,
                "backup": str(result.backup_path) if result.backup_path else None,
                "message": result.message,
                "snippet": result.snippet if result.manual else None,
                "warnings": list(result.warnings),
                "preview": result.preview,
            }
        )
        return
    _out(result.message)
    if result.manual:
        _out(result.snippet)
    if result.dry_run and result.preview:
        _out(result.preview)
    for warning in result.warnings:
        _err(f"warning: {warning}")


# =========================================================================== config
def _parse_value(text: str) -> Any:
    """``true``/``false``, numbers, JSON lists and objects; anything else stays a string."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _dig(data: Any, dotted: str) -> Any:
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            _fail(
                AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Unknown configuration key {dotted!r}.",
                    hint="`axpilot config show` lists every key.",
                )
            )
        current = current[part]
    return current


@config_app.command("path")
def config_path() -> None:
    """Print the config file's path."""
    _out(str(paths.config_file()))


@config_app.command("show")
def config_show(as_json: JsonOption = False) -> None:
    """Print the effective configuration (file, environment and defaults merged)."""
    from axpilot.config import dump_config  # noqa: PLC0415

    _state.json = as_json
    config = _config()
    if as_json:
        _print_json(config.model_dump(mode="json"))
    else:
        _out(dump_config(config))


@config_app.command("get")
def config_get(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. tools.profile.")], as_json: JsonOption = False
) -> None:
    """Print one effective setting."""
    _state.json = as_json
    value = _dig(_config().model_dump(mode="json"), key)
    _out(json.dumps(value, ensure_ascii=False) if as_json or not isinstance(value, str) else value)


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. safety.confirm.")],
    value: Annotated[str, typer.Argument(help="New value (true/false, numbers and JSON lists are parsed).")],
) -> None:
    """Change one setting in the config file."""
    from axpilot.config import set_config_value  # noqa: PLC0415

    _dig(_config().model_dump(mode="json"), key)  # unknown keys fail before writing
    set_config_value(paths.config_file(), key, _parse_value(value))
    _out(f"{key} = {value}  ({paths.config_file()})")


@config_app.command("edit")
def config_edit() -> None:
    """Open the config file in your editor ($EDITOR)."""
    target = paths.config_file()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# AXPilot configuration: see `axpilot config show` for every key.\n", encoding="utf-8"
        )
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([*shlex.split(editor), str(target)], check=False)
    elif sys.platform == "darwin":
        subprocess.run(["open", "-W", "-t", str(target)], check=False)
    else:
        _fail(
            AXPilotError(
                ErrorCode.NOT_SUPPORTED,
                "No editor is configured.",
                hint=f"Set $EDITOR, or edit {target} with any text editor.",
            )
        )
    _config()  # validate what was written


# =========================================================================== skill
def _skill_source() -> str:
    """The Agent Skill shipped with this AXPilot (package data, else the source checkout)."""
    packaged = resources.files("axpilot").joinpath("_data", "SKILL.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    checkout = Path(__file__).resolve().parents[2] / "skills" / "axpilot" / "SKILL.md"
    if checkout.is_file():
        return checkout.read_text(encoding="utf-8")
    _fail(AXPilotError(ErrorCode.NOT_SUPPORTED, "This AXPilot build does not include the Agent Skill."))


def _skill_dest(target: SkillTarget) -> Path:
    home = Path.home()
    return {
        SkillTarget.claude: home / ".claude" / "skills" / "axpilot",
        SkillTarget.claude_project: Path.cwd() / ".claude" / "skills" / "axpilot",
        SkillTarget.codex: home / ".codex" / "skills" / "axpilot",
    }[target]


@skill_app.command("install")
def skill_install(
    *,
    target: Annotated[
        SkillTarget, typer.Option("--target", help="Where to install it.")
    ] = SkillTarget.claude,
    dest: Annotated[
        Path | None, typer.Option("--dest", help="Install into this folder instead.", file_okay=False)
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace a modified copy.")] = False,
    as_json: JsonOption = False,
) -> None:
    """Install the AXPilot Agent Skill (SKILL.md) for a coding agent."""
    _state.json = as_json
    text = _skill_source()
    folder = dest or _skill_dest(target)
    file = folder / "SKILL.md"
    if file.exists() and file.read_text(encoding="utf-8") != text and not force:
        _fail(
            AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{file} exists and differs from this version's skill.",
                hint="Pass --force to replace it.",
            )
        )
    changed = not file.exists() or file.read_text(encoding="utf-8") != text
    if changed:
        folder.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
    if as_json:
        _print_json({"path": str(file), "changed": changed})
    else:
        _out(f"{'Installed' if changed else 'Already up to date:'} {file}")


# =========================================================================== tools & call
def _tool_rows(profile: str | None, naming: str | None) -> list[dict[str, Any]]:
    from mcp import Client  # noqa: PLC0415

    from axpilot.server.app import build_server  # noqa: PLC0415

    ctx = _context(Engine.fake, None, yes=False, log=False)
    server = build_server(ctx, profile=profile, naming=naming)

    async def listing() -> list[dict[str, Any]]:
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
        return [
            {
                "name": t.name,
                "title": t.title,
                "description": t.description,
                "input_schema": t.input_schema,
                "output_schema": t.output_schema,
                "annotations": t.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
                if t.annotations
                else {},
            }
            for t in tools
        ]

    return _run(listing)


@app.command()
def tools(profile: ProfileOption = None, naming: NamingOption = None, as_json: JsonOption = False) -> None:
    """List the MCP tools (and their input schemas with --json)."""
    _state.json = as_json
    rows = _tool_rows(profile, naming)
    if as_json:
        _print_json({"tools": rows, "count": len(rows)})
        return
    width = max((len(row["name"]) for row in rows), default=0)
    for row in rows:
        summary = (row["description"] or "").splitlines()[0] if row["description"] else ""
        _out(f"{row['name'].ljust(width)}  {summary}")


def _call_arguments(pairs: list[str], raw: str | None) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if raw is not None:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            _fail(AXPilotError(ErrorCode.INVALID_ARGUMENT, f"--json is not valid JSON: {exc}"))
        if not isinstance(parsed, dict):
            _fail(AXPilotError(ErrorCode.INVALID_ARGUMENT, "--json must be a JSON object of arguments."))
        arguments.update(parsed)
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            _fail(
                AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"--arg {pair!r} is not key=value.",
                    hint="Example: --arg app=Notes --arg max_elements=50 --arg modifiers='[\"cmd\"]'",
                )
            )
        arguments[key.strip()] = _parse_value(value)
    return arguments


@app.command()
def call(
    *,
    tool: Annotated[str, typer.Argument(help="Tool name, e.g. snapshot.")],
    arg: Annotated[
        list[str] | None, typer.Option("--arg", help="An argument as key=value (repeatable).")
    ] = None,
    json_arguments: Annotated[
        str | None, typer.Option("--json", help="All arguments as a JSON object.")
    ] = None,
    engine: EngineOption = Engine.auto,
    fixture: FixtureOption = None,
    profile: ProfileOption = None,
    naming: NamingOption = None,
    direct: Annotated[
        bool, typer.Option("--direct", help="Run in this process (the default for call).")
    ] = False,
    yes: YesOption = False,
) -> None:
    """Run one tool in this process and print its structured result as JSON."""
    from mcp import Client  # noqa: PLC0415

    from axpilot.server.app import build_server  # noqa: PLC0415
    from axpilot.server.results import ERROR_META_KEY, text_of  # noqa: PLC0415

    del direct  # accepted for symmetry with `stdio --direct`; call always runs in-process
    _state.json = True
    arguments = _call_arguments(arg or [], json_arguments)
    ctx = _context(engine, fixture, yes=yes, log=False)
    try:
        server = build_server(ctx, profile=profile, naming=naming)

        async def run() -> Any:
            async with Client(server) as client:
                return await client.call_tool(tool, arguments)

        result = _run(run)
    finally:
        ctx.close()
    if result.is_error:
        error = (result.meta or {}).get(ERROR_META_KEY) or {"code": "INTERNAL", "message": text_of(result)}
        _print_json({"error": error})
        try:
            code = ErrorCode(error.get("code"))
        except ValueError:
            code = ErrorCode.INTERNAL
        raise typer.Exit(exit_code_for(AXPilotError(code, str(error.get("message", "")))))
    _print_json(
        result.structured_content if result.structured_content is not None else {"text": text_of(result)}
    )


# =========================================================================== debug
@debug_app.command("dump-tree")
def dump_tree(
    *,
    app_name: Annotated[
        str | None, typer.Option("--app", help="App name, bundle id or pid (default: frontmost).")
    ] = None,
    window_id: Annotated[int | None, typer.Option("--window-id", min=0)] = None,
    menus: Annotated[bool, typer.Option("--menus", help="Include the menu bar.")] = False,
    engine: EngineOption = Engine.auto,
    fixture: FixtureOption = None,
) -> None:
    """Print an app's raw accessibility tree as JSON (for bug reports and fixtures)."""
    from axpilot.perception.serialize import tree_to_dict  # noqa: PLC0415

    _state.json = True
    ctx = _context(engine, fixture, yes=False, log=False)
    try:
        tree = _run(lambda: ctx.capture_tree(app=app_name, window_id=window_id, include_menus=menus))
    finally:
        ctx.close()
    _print_json(tree_to_dict(tree))


# =========================================================================== entry point
def _report_error(err: AXPilotError) -> int:
    if _state.json:
        _print_json({"error": err.to_dict()})
    else:
        _err(f"error: {err.code.value}: {err.message}")
        if err.hint:
            _err(f"hint: {err.hint}")
    return exit_code_for(err)


def main(argv: list[str] | None = None) -> NoReturn:
    """Console-script entry point: run the CLI and exit with the documented code."""
    _state.debug = "--debug" in (argv if argv is not None else sys.argv[1:])
    _state.json = False
    command = typer.main.get_command(app)
    try:
        returned = command.main(args=argv, prog_name="axpilot", standalone_mode=False)
        # without standalone mode, click returns the code of typer.Exit instead of raising it
        code = returned if isinstance(returned, int) and not isinstance(returned, bool) else EXIT_OK
    except typer.Exit as exit_:
        code = exit_.exit_code
    except typer.Abort:
        _err("aborted")
        code = EXIT_INTERRUPTED
    except typer.TyperException as exc:  # usage errors: unknown command, bad option, missing argument
        show = getattr(exc, "show", None)
        if callable(show):
            show(file=sys.stderr)
        else:
            _err(f"error: {exc}")
        code = EXIT_USAGE
    except AXPilotError as err:
        if _state.debug:
            _log.exception("command failed")
        code = _report_error(err)
    except KeyboardInterrupt:
        _err("interrupted")
        code = EXIT_INTERRUPTED
    except BaseExceptionGroup as group:
        code = _report_group(group)
    except Exception as exc:  # the last line of defence: one line, no traceback (A9)
        if _state.debug:
            _log.exception("unexpected error")
        code = _report_error(
            AXPilotError(
                ErrorCode.INTERNAL,
                f"{type(exc).__name__}: {exc}",
                hint="Run again with --debug for details, and report it at https://github.com/vansh3503/Mac-Mcp/issues.",
            )
        )
    sys.stdout.flush()
    raise SystemExit(code)


def _report_group(group: BaseExceptionGroup) -> int:
    """Errors raised inside a task group arrive wrapped; report the first one."""
    leaves = list(_leaves(group))
    for exc in leaves:
        if isinstance(exc, KeyboardInterrupt):
            _err("interrupted")
            return EXIT_INTERRUPTED
    for exc in leaves:
        if isinstance(exc, AXPilotError):
            return _report_error(exc)
    if _state.debug:
        _log.error("unexpected errors", exc_info=group)
    first = leaves[0] if leaves else group
    return _report_error(AXPilotError(ErrorCode.INTERNAL, f"{type(first).__name__}: {first}"))


def _leaves(group: BaseExceptionGroup) -> Any:
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            yield from _leaves(exc)
        else:
            yield exc


if __name__ == "__main__":  # pragma: no cover
    main()
