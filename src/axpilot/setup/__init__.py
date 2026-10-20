"""``axpilot setup``: prepare a Mac for launcher mode (PLAN.md §8.2).

:func:`run_setup` runs the steps in order and reports each one instead of stopping at the first
problem, so the user sees everything that needs attention at once:

1. ``platform``: macOS 14 Sonoma or later (elsewhere setup reports that it is macOS-only).
2. ``runtime``: the dedicated virtual environment with a pinned ``axpilot``
   (:mod:`axpilot.setup.runtime`).
3. ``manifest``: ``runtime.json``, which tells the launcher what to start.
4. ``launcher``: the byte-identical copy of ``AXPilot.app`` (:mod:`axpilot.setup.launcher`), plus
   a ``quarantine`` step if Gatekeeper would assess it.
5. ``login item`` (optional) and ``launch`` (optional).

All external commands go through the given :class:`~axpilot.system.runner.CommandRunner`.
"""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import axpilot
from axpilot import paths
from axpilot.clients.fileops import display_path
from axpilot.errors import AXPilotError
from axpilot.setup import launcher
from axpilot.setup.launcher import (
    QUARANTINE_XATTR,
    install_launcher,
    install_login_item,
    launch,
    quarantined_paths,
)
from axpilot.setup.runtime import (
    RuntimeInstall,
    detect_dev_source,
    ensure_runtime,
    is_dev_version,
    write_manifest,
)
from axpilot.system.runner import CommandRunner

__all__ = [
    "MIN_MACOS",
    "PlatformInfo",
    "SetupReport",
    "SetupStep",
    "check_platform",
    "parse_version",
    "platform_info",
    "run_setup",
]

MIN_MACOS: tuple[int, int] = (14, 0)
"""The oldest supported macOS (14 Sonoma; ADR-3)."""


class SetupStep(NamedTuple):
    """One step of ``axpilot setup``: its name, whether it succeeded, and what happened."""

    name: str
    ok: bool
    detail: str


@dataclass(slots=True)
class SetupReport:
    """Everything :func:`run_setup` did, step by step."""

    steps: list[SetupStep] = field(default_factory=list)
    supported: bool = True
    """False if this system cannot run AXPilot's launcher mode at all."""

    @property
    def ok(self) -> bool:
        """True if the platform is supported and every step succeeded."""
        return self.supported and bool(self.steps) and all(step.ok for step in self.steps)

    def add(self, name: str, ok: bool, detail: str) -> SetupStep:
        step = SetupStep(name, ok, detail)
        self.steps.append(step)
        return step

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (for ``axpilot setup --json``)."""
        return {
            "ok": self.ok,
            "supported": self.supported,
            "steps": [step._asdict() for step in self.steps],
        }

    def format(self) -> str:
        """Plain text, one line per step (``✓`` done, ``✗`` failed)."""
        width = max((len(step.name) for step in self.steps), default=0)
        lines = [f"{'✓' if step.ok else '✗'} {step.name.ljust(width)}  {step.detail}" for step in self.steps]
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Platform


def parse_version(text: str) -> tuple[int, ...]:
    """``"14.6.1"`` → ``(14, 6, 1)``; parsing stops at the first non-numeric part."""
    parts: list[int] = []
    for piece in text.strip().split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
        if match.end() != len(piece):
            break
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """The operating system as far as setup and doctor care."""

    system: str
    """``sys.platform`` (``"darwin"`` on macOS)."""
    os_version: str | None
    """The macOS product version (``"15.4"``), or ``None`` elsewhere/unknown."""
    machine: str

    @property
    def is_macos(self) -> bool:
        return self.system == "darwin"


def platform_info(
    *, system: str | None = None, os_version: str | None = None, machine: str | None = None
) -> PlatformInfo:
    """Describe this machine; each field can be overridden (tests simulate macOS on Linux)."""
    current = system or sys.platform
    if os_version is None and current == "darwin" and sys.platform == "darwin":
        os_version = platform.mac_ver()[0] or None
    return PlatformInfo(current, os_version, machine or platform.machine() or "unknown")


def check_platform(info: PlatformInfo) -> tuple[bool, str]:
    """Whether AXPilot's macOS features can run here, with a one-line explanation."""
    if not info.is_macos:
        return False, (
            f"AXPilot controls macOS, and this system is {info.system}; setup and launcher mode are "
            "macOS-only. Use `axpilot stdio --engine fake` to try the server with a simulated desktop."
        )
    version = parse_version(info.os_version or "")
    if not version:
        return False, "Could not determine the macOS version; AXPilot needs macOS 14 Sonoma or later."
    if version < MIN_MACOS:
        return False, f"macOS {info.os_version} is too old; AXPilot needs macOS 14 Sonoma or later."
    return True, f"macOS {info.os_version} ({info.machine})"


# --------------------------------------------------------------------------------------------
# Steps


def _error_detail(exc: AXPilotError) -> str:
    return f"{exc.message} {exc.hint}" if exc.hint else exc.message


def _step_runtime(
    report: SetupReport, runner: CommandRunner, dev_source: Path | None, use_uv: bool
) -> RuntimeInstall | None:
    source = dev_source
    if source is None and is_dev_version(axpilot.__version__):
        source = detect_dev_source()
    try:
        install = ensure_runtime(runner, source=source, use_uv=use_uv)
    except AXPilotError as exc:
        report.add("runtime", False, _error_detail(exc))
        return None
    verb = "Created" if install.created else "Updated"
    report.add(
        "runtime",
        True,
        f"{verb} {display_path(install.runtime_dir)} with axpilot {install.axpilot_version} "
        f"(using {install.installer}).",
    )
    return install


def _step_manifest(report: SetupReport, install: RuntimeInstall | None) -> bool:
    if install is None:
        report.add("manifest", False, "Skipped because the runtime is not ready.")
        return False
    try:
        write_manifest(install.python, axpilot_version=install.axpilot_version)
    except AXPilotError as exc:
        report.add("manifest", False, _error_detail(exc))
        return False
    report.add("manifest", True, f"Wrote {display_path(paths.runtime_manifest())}.")
    return True


def _checkout_launcher(dev_source: Path | None) -> Path | None:
    """The source checkout's own build (``launcher/build.sh``), for a package that bundles none."""
    checkout = dev_source if dev_source is not None else detect_dev_source()
    if checkout is None or launcher.bundled_app() is not None:
        return None
    app = Path(checkout).expanduser() / "launcher" / "build" / launcher.APP_BUNDLE_NAME
    return app if app.is_dir() else None


def _step_launcher(report: SetupReport, source: Path | None, dev_source: Path | None) -> bool:
    try:
        result = install_launcher(source=source or _checkout_launcher(dev_source))
    except AXPilotError as exc:
        report.add("launcher", False, _error_detail(exc))
        return False
    version = f" (launcher {result.version})" if result.version else ""
    if result.changed:
        report.add("launcher", True, f"Installed {display_path(result.path)}{version}.")
    else:
        report.add("launcher", True, f"{display_path(result.path)}{version} is already up to date.")
    flagged = quarantined_paths(result.path)
    if flagged:
        report.add(
            "quarantine",
            False,
            f"macOS marked {display_path(result.path)} as downloaded ({QUARANTINE_XATTR}), so Gatekeeper "
            "will ask before it first runs. Open it once from Finder (Control-click → Open), or run "
            f"`xattr -dr {QUARANTINE_XATTR} '{result.path}'` if you trust this copy.",
        )
    return True


def _step_login_item(report: SetupReport) -> None:
    try:
        path, changed = install_login_item()
    except AXPilotError as exc:
        report.add("login item", False, _error_detail(exc))
        return
    state = "Added" if changed else "Kept"
    report.add("login item", True, f"{state} {display_path(path)}; AXPilot starts when you log in.")


def _step_launch(report: SetupReport, runner: CommandRunner, info: PlatformInfo, ready: bool) -> None:
    if not ready:
        report.add("launch", False, "Skipped because the launcher or runtime is not ready.")
        return
    try:
        launch(runner, system=info.system)
    except AXPilotError as exc:
        report.add("launch", False, _error_detail(exc))
        return
    report.add(
        "launch",
        True,
        "Started AXPilot.app. Grant it Accessibility in System Settings → Privacy & Security when asked.",
    )


def run_setup(
    *,
    runner: CommandRunner,
    login_item: bool = False,
    dev_source: Path | None = None,
    launch_app: bool = False,
    launcher_source: Path | None = None,
    use_uv: bool = True,
    system: str | None = None,
    os_version: str | None = None,
    machine: str | None = None,
) -> SetupReport:
    """Prepare launcher mode and report every step.

    ``dev_source`` installs the runtime from a source checkout or wheel (default for development
    builds: the checkout this package was imported from). ``launcher_source`` overrides the
    bundled ``AXPilot.app``; a package without one uses the checkout's ``launcher/build``.
    ``launch_app`` starts the app at the end. ``system``, ``os_version`` and ``machine`` override
    platform detection (for tests).

    Never raises for a failing step; on a system other than macOS 14+ the report contains only the
    ``platform`` step and ``supported`` is False.
    """
    report = SetupReport()
    info = platform_info(system=system, os_version=os_version, machine=machine)
    supported, detail = check_platform(info)
    report.add("platform", supported, detail)
    if not supported:
        report.supported = False
        return report
    install = _step_runtime(report, runner, dev_source, use_uv)
    manifest_ok = _step_manifest(report, install)
    launcher_ok = _step_launcher(report, launcher_source, dev_source)
    if login_item:
        _step_login_item(report)
    if launch_app:
        _step_launch(report, runner, info, ready=manifest_ok and launcher_ok)
    return report
