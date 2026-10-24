from __future__ import annotations

import json
import os
import plistlib
import socket
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import axpilot
from axpilot import doctor, paths
from axpilot.clients import base as clients_base
from axpilot.doctor import Check, checks_payload, format_report, run_checks, worst_status
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import PermissionStatus
from axpilot.setup import launcher
from axpilot.setup.launcher import executable_path, install_launcher
from axpilot.setup.runtime import write_manifest
from axpilot.system.runner import CommandResult, CommandRunner
from axpilot.testing.host import short_socket_dir

ARM = "c" * 40

# --------------------------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with short_socket_dir() as sockets:  # tmp_path is too long for AF_UNIX on macOS
        yield _isolate(tmp_path, monkeypatch, sockets / "host.sock")


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, socket_path: Path) -> Path:
    for name in list(os.environ):
        if name.startswith("AXPILOT_"):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "Applications").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("AXPILOT_HOME", str(tmp_path / "support"))
    monkeypatch.setenv("AXPILOT_APP_PATH", str(tmp_path / "apps" / "AXPilot.app"))
    monkeypatch.setenv("AXPILOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AXPILOT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AXPILOT_SOCKET", str(socket_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(clients_base, "SYSTEM_APPLICATIONS_DIR", tmp_path / "Applications")
    monkeypatch.setattr(launcher, "has_xattr", lambda path, name: False)
    return home


@dataclass
class Runner(CommandRunner):
    codesign_stderr: str = ""
    codesign_exit: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_s: float = 30,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        max_output: int = 20_000,
    ) -> CommandResult:
        self.calls.append(list(argv))
        return CommandResult(list(argv), self.codesign_exit, "", self.codesign_stderr)


@pytest.fixture
def runner() -> Runner:
    return Runner()


@dataclass
class StubEngine:
    status: PermissionStatus | None = None
    error: Exception | None = None

    def permissions(self) -> PermissionStatus:
        if self.error is not None:
            raise self.error
        assert self.status is not None
        return self.status


@pytest.fixture
def make_app(tmp_path: Path) -> Callable[..., Path]:
    def make(folder: str = "bundle", *, binary: bytes = b"launcher", cdhash: str | None = None) -> Path:
        app = tmp_path / folder / "AXPilot.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        exe = executable_path(app)
        exe.write_bytes(binary)
        exe.chmod(0o755)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.0.0"}))
        if cdhash is not None:
            (app.parent / "CDHASH").write_text(cdhash)
        return app

    return make


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def mac_checks(runner: Runner, engine: Any = None, source: Path | None = None) -> dict[str, Check]:
    return by_name(
        run_checks(
            engine, runner=runner, launcher_source=source, system="darwin", os_version="15.4", machine="arm64"
        )
    )


def linux_checks(runner: Runner, engine: Any = None, source: Path | None = None) -> dict[str, Check]:
    return by_name(
        run_checks(engine, runner=runner, launcher_source=source, system="linux", machine="x86_64")
    )


# --------------------------------------------------------------------------------------------
# Overall


def test_linux_without_anything_installed(env: Path, runner: Runner) -> None:
    checks = run_checks(None, runner=runner, system="linux", machine="x86_64")
    assert [(c.name, c.status) for c in checks] == [
        ("Platform", "fail"),
        ("Python", "ok"),
        ("AXPilot", "ok"),
        ("Config", "ok"),
        ("Launcher", "skip"),
        ("Runtime", "skip"),
        ("Host", "skip"),
        ("Permissions", "skip"),
        ("Clients", "skip"),
    ]
    assert "--engine fake" in (checks[0].hint or "")
    assert runner.calls == []


def test_macos_without_setup(env: Path, runner: Runner) -> None:
    checks = mac_checks(runner)
    assert checks["Platform"].status == "ok"
    assert checks["Platform"].detail == "macOS 15.4 (arm64)"
    assert checks["Launcher"].status == "warn"
    assert checks["Launcher"].hint == "Run `axpilot setup`."
    assert checks["Runtime"].status == "warn"


def test_old_macos_fails(env: Path, runner: Runner) -> None:
    checks = by_name(run_checks(None, runner=runner, system="darwin", os_version="13.6", machine="arm64"))
    assert checks["Platform"].status == "fail"
    assert "Upgrade" in (checks["Platform"].hint or "")


def test_version_check_mentions_dev_builds(env: Path, runner: Runner) -> None:
    detail = linux_checks(runner)["AXPilot"].detail
    assert axpilot.__version__ in detail


def test_a_crashing_check_becomes_a_failure(
    env: Path, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> list[Any]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(doctor, "detect_installed", boom)
    clients = linux_checks(runner)["Clients"]
    assert clients.status == "fail"
    assert "RuntimeError: disk on fire" in clients.detail


# --------------------------------------------------------------------------------------------
# Config


def test_valid_config_file(env: Path, runner: Runner) -> None:
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text('[tools]\nprofile = "minimal"\n')
    assert linux_checks(runner)["Config"] == Check("Config", "ok", f"{paths.config_file()} is valid.")


@pytest.mark.parametrize("content", ["[tools\n", '[tools]\nprofile = "huge"\n', "[unknown]\nx = 1\n"])
def test_invalid_config_file(env: Path, runner: Runner, content: str) -> None:
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(content)
    check = linux_checks(runner)["Config"]
    assert check.status == "fail"
    assert check.hint


def test_invalid_config_environment_variable(
    env: Path, runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AXPILOT_TOOLS_PROFILE", "enormous")
    assert linux_checks(runner)["Config"].status == "fail"


# --------------------------------------------------------------------------------------------
# Launcher


def test_launcher_matches_bundle_and_cdhash(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app(cdhash=f"arm64 {ARM}\n")
    install_launcher(source=source)
    runner.codesign_stderr = f"Identifier=x\nCDHash={ARM}\n"
    checks = mac_checks(runner, source=source)
    assert checks["Launcher"].status == "ok"
    assert "matches the bundled launcher" in checks["Launcher"].detail
    assert checks["Quarantine"].status == "ok"
    assert checks["Code signature"].status == "ok"
    assert runner.calls == [["codesign", "-dvvv", "-a", "arm64", str(paths.launcher_app_path())]]


def test_launcher_cdhash_mismatch(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app(cdhash=f"arm64 {ARM}\n")
    install_launcher(source=source)
    runner.codesign_stderr = f"CDHash={'d' * 40}\n"
    check = mac_checks(runner, source=source)["Code signature"]
    assert check.status == "fail"
    assert "grant Accessibility to AXPilot again" in (check.hint or "")


def test_launcher_cdhash_unreadable(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app(cdhash=f"arm64 {ARM}\n")
    install_launcher(source=source)
    runner.codesign_exit = 1
    assert mac_checks(runner, source=source)["Code signature"].status == "warn"


def test_launcher_without_published_cdhash(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    runner.codesign_stderr = f"CDHash={ARM}\n"
    check = mac_checks(runner, source=source)["Code signature"]
    assert check.status == "skip"
    assert ARM in check.detail


def test_wildcard_cdhash_applies_to_every_arch(
    env: Path, runner: Runner, make_app: Callable[..., Path]
) -> None:
    source = make_app(cdhash=f"{ARM}\n")
    install_launcher(source=source)
    runner.codesign_stderr = f"CDHash={ARM}\n"
    assert mac_checks(runner, source=source)["Code signature"].status == "ok"


def test_launcher_on_linux_skips_signature(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    checks = linux_checks(runner, source=source)
    assert checks["Launcher"].status == "ok"
    assert checks["Code signature"].status == "skip"
    assert runner.calls == []


def test_changed_launcher_executable(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    executable_path(paths.launcher_app_path()).write_bytes(b"different build")
    check = mac_checks(runner, source=source)["Launcher"]
    assert check.status == "fail"
    assert "differs from the launcher" in check.detail
    assert "grant" in (check.hint or "")


def test_changed_launcher_resources(env: Path, runner: Runner, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    (paths.launcher_app_path() / "Contents" / "Info.plist").write_bytes(b"edited")
    check = mac_checks(runner, source=source)["Launcher"]
    assert check.status == "fail"
    assert "other files" in check.detail


def test_launcher_without_bundled_copy(
    env: Path, runner: Runner, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_launcher(source=make_app())
    monkeypatch.setattr(launcher, "bundled_app", lambda: None)
    assert mac_checks(runner)["Launcher"].status == "warn"


def test_quarantined_launcher(
    env: Path, runner: Runner, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_app()
    install_launcher(source=source)
    monkeypatch.setattr(launcher, "has_xattr", lambda path, name: True)
    check = mac_checks(runner, source=source)["Quarantine"]
    assert check.status == "fail"
    assert "xattr -dr com.apple.quarantine" in (check.hint or "")


def test_quarantine_unknown(
    env: Path, runner: Runner, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_app()
    install_launcher(source=source)
    monkeypatch.setattr(launcher, "has_xattr", lambda path, name: None)
    assert linux_checks(runner, source=source)["Quarantine"].status == "skip"


# --------------------------------------------------------------------------------------------
# Runtime


@pytest.fixture
def python(env: Path) -> Path:
    exe = paths.runtime_dir() / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_runtime_ok(env: Path, runner: Runner, python: Path) -> None:
    write_manifest(python)
    check = linux_checks(runner)["Runtime"]
    assert check.status == "ok"
    assert axpilot.__version__ in check.detail


def test_runtime_with_missing_interpreter(env: Path, runner: Runner) -> None:
    write_manifest(paths.runtime_dir() / "bin" / "python")
    check = linux_checks(runner)["Runtime"]
    assert check.status == "fail"
    assert "does not exist" in check.detail


def test_runtime_version_mismatch(env: Path, runner: Runner, python: Path) -> None:
    write_manifest(python, axpilot_version="0.0.1")
    check = linux_checks(runner)["Runtime"]
    assert check.status == "warn"
    assert "axpilot update" in (check.hint or "")


def test_runtime_manifest_writable_by_others(env: Path, runner: Runner, python: Path) -> None:
    write_manifest(python)
    paths.runtime_manifest().chmod(0o666)
    check = linux_checks(runner)["Runtime"]
    assert check.status == "warn"
    assert "chmod 600" in (check.hint or "")


def test_runtime_manifest_invalid(env: Path, runner: Runner) -> None:
    paths.runtime_manifest().parent.mkdir(parents=True)
    paths.runtime_manifest().write_text(json.dumps({"version": 9}))
    check = linux_checks(runner)["Runtime"]
    assert check.status == "fail"
    assert "`version` must be 1" in check.detail


# --------------------------------------------------------------------------------------------
# Host socket


@pytest.fixture
def listening_socket(env: Path) -> Iterator[Path]:
    path = paths.socket_path()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        yield path
    finally:
        server.close()


def test_host_running(env: Path, runner: Runner, listening_socket: Path) -> None:
    check = linux_checks(runner)["Host"]
    assert check.status == "ok"
    assert "accepting connections" in check.detail


def test_host_older_than_the_runtime(env: Path, runner: Runner, listening_socket: Path) -> None:
    """macOS 26 (#56): after `axpilot update`, the host kept running the previous version."""
    write_manifest(Path("/x/bin/python"))
    os.utime(listening_socket, (1_000_000, 1_000_000))
    check = linux_checks(runner)["Host"]
    assert check.status == "warn"
    assert "previous version" in check.detail
    assert check.hint is not None
    assert "axpilot update" in check.hint


def test_host_stale_socket(env: Path, runner: Runner) -> None:
    path = paths.socket_path()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.close()  # the file stays, nobody listens
    check = linux_checks(runner)["Host"]
    assert check.status == "fail"
    assert "stale socket" in check.detail


def test_host_path_is_not_a_socket(env: Path, runner: Runner) -> None:
    paths.socket_path().write_text("")
    assert linux_checks(runner)["Host"].status == "fail"


def test_host_not_running(env: Path, runner: Runner) -> None:
    check = linux_checks(runner)["Host"]
    assert check.status == "skip"
    assert "Not running" in check.detail


# --------------------------------------------------------------------------------------------
# Permissions


def test_permissions_granted(env: Path, runner: Runner) -> None:
    engine = StubEngine(PermissionStatus(True, True, {"Safari": True}, responsible_app="AXPilot"))
    checks = linux_checks(runner, engine)
    assert checks["Accessibility"].status == "ok"
    assert "AXPilot" in checks["Accessibility"].detail
    assert checks["Screen Recording"].status == "ok"
    assert checks["Automation"] == Check("Automation", "ok", "Allowed for Safari.")
    assert "Permissions" not in checks


def test_permissions_missing(env: Path, runner: Runner) -> None:
    engine = StubEngine(
        PermissionStatus(False, False, {"Mail": False, "Safari": True}, notes=["Restart needed."])
    )
    checks = linux_checks(runner, engine)
    accessibility = checks["Accessibility"]
    assert accessibility.status == "fail"
    assert "Restart needed." in accessibility.detail
    assert "Privacy & Security" in (accessibility.hint or "")
    assert checks["Screen Recording"].status == "warn"
    assert checks["Automation"].status == "warn"
    assert "Mail" in checks["Automation"].detail


def test_automation_not_asked_yet_is_not_a_denial(env: Path, runner: Runner) -> None:
    """macOS 26: Finder gave -1744 (not asked yet), and doctor said "Denied" with a Settings path
    that listed nothing."""
    unasked = PermissionStatus(
        True, True, {"com.apple.finder": False}, automation_unasked=["com.apple.finder"]
    )
    check = linux_checks(runner, StubEngine(unasked))["Automation"]
    assert check.status == "skip"
    assert check.detail == "Not asked yet for com.apple.finder; macOS asks the first time a tool needs it."
    both = PermissionStatus(
        True,
        True,
        {"com.apple.finder": False, "com.apple.mail": False},
        automation_unasked=["com.apple.finder"],
    )
    check = linux_checks(runner, StubEngine(both))["Automation"]
    assert check.status == "warn"
    assert check.detail == "Denied for com.apple.mail. Not asked yet for com.apple.finder."


def test_launcher_mode_does_not_judge_the_terminals_permissions(
    env: Path, runner: Runner, make_app: Callable[..., Path]
) -> None:
    """macOS 26: doctor said Accessibility was granted "to Claude" (the app running the terminal);
    a new user's Terminal without it would have shown a failure although AXPilot.app worked."""
    terminal = StubEngine(PermissionStatus(False, False, {"Mail": False}, responsible_app="Terminal"))
    assert mac_checks(runner, terminal)["Accessibility"].status == "fail"  # direct mode: it counts
    make_app("apps")  # AXPilot.app installed: launcher mode
    checks = mac_checks(runner, terminal)
    for name in ("Accessibility", "Screen Recording"):
        assert checks[name].status == "skip", name
        assert "Not checked for AXPilot.app" in checks[name].detail
        assert "For direct mode: not granted (macOS attributes it to Terminal)." in checks[name].detail
    assert "Automation" not in checks


def test_permissions_without_automation_entries(env: Path, runner: Runner) -> None:
    checks = linux_checks(runner, StubEngine(PermissionStatus(True, False)))
    assert "Automation" not in checks


def test_permission_errors(env: Path, runner: Runner) -> None:
    engine = StubEngine(error=AXPilotError(ErrorCode.ENGINE_UNAVAILABLE, "PyObjC missing", hint="Reinstall."))
    assert linux_checks(runner, engine)["Permissions"] == Check(
        "Permissions", "fail", "PyObjC missing", "Reinstall."
    )
    crashing = StubEngine(error=ValueError("bad"))
    assert linux_checks(runner, crashing)["Permissions"].status == "fail"


# --------------------------------------------------------------------------------------------
# Clients


def write_cursor(home: Path, command: str) -> None:
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"axpilot": {"command": command, "args": ["stdio"]}}}))


def test_connected_client(env: Path, runner: Runner, tmp_path: Path) -> None:
    exe = tmp_path / "bin" / "axpilot"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    write_cursor(env, str(exe))
    check = linux_checks(runner)["Client: Cursor"]
    assert check.status == "ok"
    assert "Connected" in check.detail


def test_client_with_a_stale_command(env: Path, runner: Runner) -> None:
    write_cursor(env, "/gone/axpilot")
    check = linux_checks(runner)["Client: Cursor"]
    assert check.status == "warn"
    assert check.hint == "Run `axpilot connect cursor`."


def test_client_with_a_bare_command_is_ok(env: Path, runner: Runner) -> None:
    write_cursor(env, "uvx")
    assert linux_checks(runner)["Client: Cursor"].status == "ok"


def test_installed_but_not_connected_client(env: Path, runner: Runner) -> None:
    (env / ".codex").mkdir()
    check = linux_checks(runner)["Client: OpenAI Codex CLI"]
    assert check.status == "skip"
    assert check.hint == "Run `axpilot connect codex`."


def test_client_with_unreadable_config(env: Path, runner: Runner) -> None:
    path = env / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    check = linux_checks(runner)["Client: Gemini CLI"]
    assert check.status == "warn"
    assert "not valid JSON" in check.detail


# --------------------------------------------------------------------------------------------
# Output


SAMPLE = [
    Check("Platform", "ok", "macOS 15.4 (arm64)"),
    Check("Launcher", "warn", "Not installed.", "Run `axpilot setup`."),
    Check("Accessibility", "fail", "Not granted.", "Grant it."),
    Check("Permissions", "skip", "No engine."),
]


def test_format_report() -> None:
    assert format_report(SAMPLE) == (
        "✓ Platform       macOS 15.4 (arm64)\n"
        "! Launcher       Not installed.\n"
        "                 → Run `axpilot setup`.\n"
        "✗ Accessibility  Not granted.\n"
        "                 → Grant it.\n"
        "– Permissions    No engine.\n"
        "\n"
        "1 ok, 1 warning(s), 1 problem(s), 1 skipped"
    )


def test_format_report_of_nothing() -> None:
    assert format_report([]) == "\n0 ok, 0 warning(s), 0 problem(s), 0 skipped"


def test_checks_payload_is_json_ready() -> None:
    payload = checks_payload(SAMPLE)
    assert payload[1] == {
        "name": "Launcher",
        "status": "warn",
        "detail": "Not installed.",
        "hint": "Run `axpilot setup`.",
    }
    assert payload[0]["hint"] is None
    json.dumps(payload)


@pytest.mark.parametrize(
    ("statuses", "worst"),
    [
        ([], "skip"),
        (["skip", "ok"], "ok"),
        (["ok", "warn", "skip"], "warn"),
        (["warn", "fail", "ok"], "fail"),
    ],
)
def test_worst_status(statuses: list[Any], worst: str) -> None:
    assert worst_status(Check("x", status, "") for status in statuses) == worst


def test_real_checks_render(env: Path, runner: Runner) -> None:
    report = format_report(run_checks(None, runner=runner, system="linux"))
    assert report.splitlines()[0].startswith("✗ Platform")
    assert "skipped" in report.splitlines()[-1]
