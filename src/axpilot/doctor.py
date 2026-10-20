"""``axpilot doctor``: check an installation and say how to fix what is wrong.

:func:`run_checks` never raises: every problem, including an unexpected one inside a check,
becomes a :class:`Check` with status ``fail``. Each check has a status:

* ``ok`` (✓): fine.
* ``warn`` (!): works, but something is degraded or will need attention.
* ``fail`` (✗): broken; the hint says what to do.
* ``skip`` (–): not applicable here, or nothing to check.

The report covers the platform, Python and AXPilot versions, the config file, the launcher
(byte-for-byte match with the bundled copy, quarantine flag, cdhash), the runtime manifest, the
host socket, macOS permissions (when an engine is given) and the MCP clients found on this Mac.
"""

from __future__ import annotations

import errno
import logging
import os
import platform
import socket
import stat
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import axpilot
from axpilot import paths
from axpilot.clients import detect_installed, inspect_entry
from axpilot.clients.fileops import display_path
from axpilot.config import load_config
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.setup import PlatformInfo, check_platform, platform_info
from axpilot.setup.launcher import (
    QUARANTINE_XATTR,
    LauncherStatus,
    installed_cdhash,
    launcher_status,
)
from axpilot.setup.runtime import is_dev_version, manifest_problems, read_manifest
from axpilot.system.runner import CommandRunner

if TYPE_CHECKING:
    from axpilot.engine.base import Engine

__all__ = ["Check", "CheckStatus", "checks_payload", "format_report", "run_checks", "worst_status"]

logger = logging.getLogger(__name__)

CheckStatus = Literal["ok", "warn", "fail", "skip"]

MARKERS: dict[CheckStatus, str] = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "–"}
_SEVERITY: dict[CheckStatus, int] = {"skip": 0, "ok": 1, "warn": 2, "fail": 3}
_MIN_PYTHON = (3, 11)


@dataclass(frozen=True, slots=True)
class Check:
    """The result of one diagnostic."""

    name: str
    status: CheckStatus
    detail: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


def _default_hint(code: ErrorCode) -> str | None:
    return AXPilotError(code, "").hint


def _arch(machine: str) -> str:
    lowered = machine.lower()
    return "arm64" if lowered in ("arm64", "aarch64") else lowered


# --------------------------------------------------------------------------------------------
# Individual checks


def _check_platform(info: PlatformInfo) -> Check:
    if not info.is_macos:
        return Check(
            "Platform",
            "fail",
            f"This system is {info.system}; AXPilot controls macOS 14 Sonoma or later.",
            "Use `axpilot stdio --engine fake` to try the server with a simulated desktop.",
        )
    supported, detail = check_platform(info)
    if supported:
        return Check("Platform", "ok", detail)
    return Check("Platform", "fail", detail, "Upgrade to macOS 14 Sonoma or later.")


def _check_python() -> Check:
    version = platform.python_version()
    detail = f"Python {version} ({sys.executable})"
    if sys.version_info[:2] < _MIN_PYTHON:
        return Check("Python", "fail", detail, "AXPilot needs Python 3.11 or later; reinstall it with uv.")
    return Check("Python", "ok", detail)


def _check_axpilot() -> Check:
    version = axpilot.__version__
    suffix = " (development build)" if is_dev_version(version) else ""
    return Check("AXPilot", "ok", f"axpilot {version}{suffix}")


def _check_config() -> Check:
    path = paths.config_file()
    try:
        load_config(path)
    except AXPilotError as exc:
        return Check("Config", "fail", exc.message, exc.hint)
    if path.exists():
        return Check("Config", "ok", f"{display_path(path)} is valid.")
    return Check("Config", "ok", f"No config file at {display_path(path)}; using the defaults.")


_REGRANT_HINT = (
    "Run `axpilot setup` to install the launcher that ships with this AXPilot, then grant "
    "Accessibility to AXPilot again: macOS ties the grant to the exact launcher build."
)


def _launcher_identity(status: LauncherStatus) -> Check:
    label = display_path(status.path)
    version = f" (launcher {status.version})" if status.version else ""
    regrant = _REGRANT_HINT
    if status.bundled_path is None:
        return Check(
            "Launcher",
            "warn",
            f"{label}{version} is installed, but this build has no bundled launcher to compare it with.",
            "Development builds cannot verify the launcher; release builds can.",
        )
    if status.identical:
        sha = (status.sha256 or "")[:12]
        return Check("Launcher", "ok", f"{label}{version} matches the bundled launcher (SHA-256 {sha}…).")
    if status.hash_matches:
        return Check(
            "Launcher",
            "fail",
            f"{label}{version} has the right executable, but other files in the bundle were changed.",
            regrant,
        )
    bundled = f" {status.bundled_version}" if status.bundled_version else ""
    return Check(
        "Launcher",
        "fail",
        f"{label}{version} differs from the launcher{bundled} bundled with axpilot {axpilot.__version__}.",
        regrant,
    )


def _quarantine(status: LauncherStatus) -> Check:
    if status.quarantined is None:
        return Check("Quarantine", "skip", "Extended attributes cannot be checked on this system.")
    if status.quarantined:
        shown = ", ".join(status.quarantined[:3]) + ("…" if len(status.quarantined) > 3 else "")
        return Check(
            "Quarantine",
            "fail",
            f"{len(status.quarantined)} item(s) of the launcher carry {QUARANTINE_XATTR} ({shown}), "
            "so Gatekeeper will block or question it.",
            "Open AXPilot.app once from Finder (Control-click → Open), or run "
            f"`xattr -dr {QUARANTINE_XATTR} '{status.path}'` if you trust this copy.",
        )
    return Check("Quarantine", "ok", "No quarantine flag; Gatekeeper will not assess the launcher.")


def _cdhash(status: LauncherStatus, info: PlatformInfo, runner: CommandRunner) -> Check:
    if not info.is_macos:
        return Check("Code signature", "skip", "Code signatures can only be read on macOS.")
    arch = _arch(info.machine)
    actual = installed_cdhash(runner, status.path, arch)
    if actual is None:
        return Check(
            "Code signature",
            "warn",
            f"Could not read the launcher's code signature for {arch}.",
            f"Run `codesign -dvvv -a {arch} '{status.path}'` to see why.",
        )
    expected = status.expected_cdhash.get(arch) or status.expected_cdhash.get("*")
    if expected is None:
        return Check(
            "Code signature", "skip", f"cdhash {actual} ({arch}); no published value to compare with."
        )
    if actual == expected:
        return Check("Code signature", "ok", f"cdhash {actual} ({arch}) matches the published launcher.")
    return Check(
        "Code signature",
        "fail",
        f"cdhash {actual} ({arch}) differs from the published {expected}: this is not the "
        "launcher AXPilot released.",
        _REGRANT_HINT,
    )


def _check_launcher(info: PlatformInfo, runner: CommandRunner, source: Path | None) -> list[Check]:
    status = launcher_status(source=source)
    if not status.installed:
        detail = f"Not installed at {display_path(status.path)}"
        if not info.is_macos:
            return [Check("Launcher", "skip", f"{detail}; launcher mode is macOS-only.")]
        return [
            Check("Launcher", "warn", f"{detail}; only direct mode is available.", "Run `axpilot setup`.")
        ]
    return [_launcher_identity(status), _quarantine(status), _cdhash(status, info, runner)]


def _check_runtime(info: PlatformInfo) -> Check:
    path = paths.runtime_manifest()
    try:
        manifest = read_manifest(path)
    except AXPilotError as exc:
        if exc.code == ErrorCode.HOST_UNAVAILABLE:
            status: CheckStatus = "warn" if info.is_macos else "skip"
            return Check("Runtime", status, f"Not set up (no {display_path(path)}).", "Run `axpilot setup`.")
        return Check("Runtime", "fail", exc.message, exc.hint)
    problems = manifest_problems(manifest)
    if problems:
        return Check(
            "Runtime",
            "fail",
            "runtime.json is unusable: " + "; ".join(problems) + ".",
            "Run `axpilot setup` to recreate the runtime.",
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o022:
        return Check(
            "Runtime",
            "warn",
            f"{display_path(path)} can be modified by other users, who could make AXPilot run their code.",
            f"Run `chmod 600 '{path}'` (or `axpilot setup`).",
        )
    installed = manifest.axpilot_version
    if installed and installed != axpilot.__version__:
        return Check(
            "Runtime",
            "warn",
            f"The runtime has axpilot {installed}, but this command is axpilot {axpilot.__version__}.",
            "Run `axpilot update` (or `axpilot setup`) to bring the runtime up to date.",
        )
    version = f" (axpilot {installed})" if installed else ""
    return Check("Runtime", "ok", f"{display_path(Path(manifest.python))}{version}")


def _probe_socket(path: Path) -> int:
    """``connect_ex`` result of a non-blocking connection attempt to a Unix socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        return sock.connect_ex(str(path))
    finally:
        sock.close()


def _check_host() -> Check:
    path = paths.socket_path()
    label = display_path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return Check(
            "Host", "skip", f"Not running (no socket at {label}); clients start it when they connect."
        )
    if not stat.S_ISSOCK(info.st_mode):
        return Check(
            "Host", "fail", f"{label} exists but is not a socket.", "Delete it; the host recreates it."
        )
    if info.st_uid != os.getuid():
        return Check(
            "Host",
            "fail",
            f"{label} belongs to another user.",
            "Delete it (or set AXPILOT_SOCKET to a path of your own) and restart AXPilot.",
        )
    try:
        result = _probe_socket(path)
    except OSError as exc:  # e.g. a path longer than AF_UNIX allows
        return Check("Host", "fail", f"Cannot connect to {label}: {exc.strerror or exc}.", "Restart AXPilot.")
    if result == 0 or result in (errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK):
        return Check("Host", "ok", f"Running and accepting connections at {label}.")
    if result == errno.ECONNREFUSED:
        return Check(
            "Host",
            "fail",
            f"Nothing is listening on {label} (a stale socket from a host that stopped).",
            "Quit AXPilot from its menu-bar icon (if shown) and start it again with `open -g -a AXPilot`.",
        )
    return Check("Host", "fail", f"Cannot connect to {label}: {os.strerror(result)}.", "Restart AXPilot.")


def _check_permissions(engine: Engine | None) -> list[Check]:
    if engine is None:
        return [
            Check(
                "Permissions", "skip", "Not checked (no engine). Run doctor on the Mac you want to control."
            )
        ]
    try:
        perms = engine.permissions()
    except AXPilotError as exc:
        return [Check("Permissions", "fail", exc.message, exc.hint)]
    responsible = f" (macOS attributes it to {perms.responsible_app})" if perms.responsible_app else ""
    notes = "".join(f" {note}" for note in perms.notes)
    checks = [
        Check("Accessibility", "ok", f"Granted{responsible}.{notes}")
        if perms.accessibility
        else Check(
            "Accessibility",
            "fail",
            f"Not granted{responsible}; AXPilot cannot read or control apps.{notes}",
            _default_hint(ErrorCode.PERMISSION_ACCESSIBILITY),
        ),
        Check("Screen Recording", "ok", "Granted.")
        if perms.screen_recording
        else Check(
            "Screen Recording",
            "warn",
            "Not granted; screenshots and OCR are unavailable (snapshots still work).",
            _default_hint(ErrorCode.PERMISSION_SCREEN_RECORDING),
        ),
    ]
    if perms.automation:
        unasked = sorted(set(perms.automation_unasked))
        denied = sorted(app for app, ok in perms.automation.items() if not ok and app not in unasked)
        allowed = sorted(app for app, ok in perms.automation.items() if ok)
        later = f" Not asked yet for {', '.join(unasked)}." if unasked else ""
        if denied:
            checks.append(
                Check(
                    "Automation",
                    "warn",
                    "Denied for " + ", ".join(denied) + "." + later,
                    "Allow AXPilot (or the app that runs it) in System Settings → Privacy & Security → "
                    "Automation.",
                )
            )
        elif unasked:
            checks.append(
                Check(
                    "Automation",
                    "skip",
                    f"Not asked yet for {', '.join(unasked)}; macOS asks the first time a tool needs it.",
                )
            )
        else:
            checks.append(Check("Automation", "ok", "Allowed for " + ", ".join(allowed) + "."))
    return checks


def _command_missing(command: str) -> bool:
    path = Path(command)
    return path.is_absolute() and not (path.is_file() and os.access(path, os.X_OK))


def _check_clients() -> list[Check]:
    targets = detect_installed()
    if not targets:
        return [Check("Clients", "skip", "No supported MCP client found.", "See `axpilot connect --list`.")]
    checks: list[Check] = []
    for target in targets:
        name = f"Client: {target.display_name}"
        connect_hint = f"Run `axpilot connect {target.name}`."
        status = inspect_entry(target)
        label = display_path(status.path)
        if status.error:
            checks.append(Check(name, "warn", f"Cannot read {label}: {status.error}", connect_hint))
        elif status.configured is None:
            checks.append(
                Check(name, "skip", f"Cannot tell whether AXPilot is configured in {label}.", connect_hint)
            )
        elif not status.configured:
            checks.append(Check(name, "skip", "Installed, not connected.", connect_hint))
        elif status.command and _command_missing(status.command):
            checks.append(
                Check(
                    name, "warn", f"Connected, but its command {status.command} does not exist.", connect_hint
                )
            )
        else:
            checks.append(Check(name, "ok", f"Connected ({label})."))
    return checks


# --------------------------------------------------------------------------------------------
# Public API


def _guarded(name: str, check: Callable[[], Check | list[Check]]) -> list[Check]:
    """Run one check; an unexpected exception becomes a ``fail`` instead of aborting doctor."""
    try:
        result = check()
    except AXPilotError as exc:
        return [Check(name, "fail", exc.message, exc.hint)]
    except Exception as exc:
        logger.exception("doctor check %r crashed", name)
        return [
            Check(
                name,
                "fail",
                f"The check itself failed: {type(exc).__name__}: {exc}",
                "Please report this at https://github.com/vansh3503/Mac-Mcp/issues with this output.",
            )
        ]
    return result if isinstance(result, list) else [result]


def run_checks(
    engine: Engine | None,
    *,
    runner: CommandRunner,
    launcher_source: Path | None = None,
    system: str | None = None,
    os_version: str | None = None,
    machine: str | None = None,
) -> list[Check]:
    """Run every diagnostic, in a fixed order. ``engine`` enables the permission checks;
    ``runner`` runs ``codesign`` (macOS). ``launcher_source`` overrides the bundled launcher and
    ``system``/``os_version``/``machine`` the platform detection (for tests)."""
    info = platform_info(system=system, os_version=os_version, machine=machine)
    steps: list[tuple[str, Callable[[], Check | list[Check]]]] = [
        ("Platform", lambda: _check_platform(info)),
        ("Python", _check_python),
        ("AXPilot", _check_axpilot),
        ("Config", _check_config),
        ("Launcher", lambda: _check_launcher(info, runner, launcher_source)),
        ("Runtime", lambda: _check_runtime(info)),
        ("Host", _check_host),
        ("Permissions", lambda: _check_permissions(engine)),
        ("Clients", _check_clients),
    ]
    checks: list[Check] = []
    for name, check in steps:
        checks.extend(_guarded(name, check))
    return checks


def worst_status(checks: Iterable[Check]) -> CheckStatus:
    """The most severe status among ``checks`` (``fail`` > ``warn`` > ``ok`` > ``skip``)."""
    worst: CheckStatus = "skip"
    for check in checks:
        if _SEVERITY[check.status] > _SEVERITY[worst]:
            worst = check.status
    return worst


def format_report(checks: Iterable[Check]) -> str:
    """Plain-text report: one line per check with a ✓ ! ✗ – marker, hints indented below, and a
    summary line."""
    items = list(checks)
    width = max((len(check.name) for check in items), default=0)
    lines: list[str] = []
    for check in items:
        lines.append(f"{MARKERS[check.status]} {check.name.ljust(width)}  {check.detail}")
        if check.hint:
            lines.append(f"{' ' * (width + 4)}→ {check.hint}")
    counts = {status: sum(1 for check in items if check.status == status) for status in MARKERS}
    lines.append("")
    lines.append(
        f"{counts['ok']} ok, {counts['warn']} warning(s), "
        f"{counts['fail']} problem(s), {counts['skip']} skipped"
    )
    return "\n".join(lines)


def checks_payload(checks: Iterable[Check]) -> list[dict[str, Any]]:
    """JSON-friendly list of ``{"name", "status", "detail", "hint"}`` objects."""
    return [check.to_dict() for check in checks]
