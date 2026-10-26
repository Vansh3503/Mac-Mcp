"""The ``axpilot`` command line in-process: every command, the exit codes (0/1/2/3/4/130), JSON
shapes, and errors as one line (tracebacks only with ``--debug``)."""

from __future__ import annotations

import io
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from axpilot import __version__, cli
from axpilot.cli import exit_code_for, main
from axpilot.errors import AXPilotError, ErrorCode


class Ran:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err

    def json(self) -> Any:
        return json.loads(self.out)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every path AXPilot touches points into ``tmp_path``; no editor; HOME is private."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXPILOT_HOME", str(tmp_path / "support"))
    monkeypatch.setenv("AXPILOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AXPILOT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AXPILOT_SOCKET", str(tmp_path / "axpilot.sock"))
    monkeypatch.setenv("AXPILOT_APP_PATH", str(tmp_path / "Applications" / "AXPilot.app"))
    for var in ("EDITOR", "VISUAL", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("AXPILOT_") and name not in {
            "AXPILOT_HOME",
            "AXPILOT_CONFIG_DIR",
            "AXPILOT_LOG_DIR",
            "AXPILOT_SOCKET",
            "AXPILOT_APP_PATH",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("AXPILOT_SERVER_TOKEN_STORE", "file")  # never the real macOS keychain
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def invoke(*argv: str) -> Ran:
        capsys.readouterr()
        with pytest.raises(SystemExit) as exited:
            main(list(argv))
        captured = capsys.readouterr()
        code = exited.value.code
        return Ran(code if isinstance(code, int) else 1, captured.out, captured.err)

    return invoke


# =========================================================================== basics
def test_version(run: Any) -> None:
    ran = run("version")
    assert ran.code == 0
    assert ran.out.startswith(f"axpilot {__version__} ")
    data = run("version", "--json").json()
    assert data["axpilot"] == __version__
    assert data["platform"] == sys.platform


def test_help_and_usage_errors(run: Any) -> None:
    assert run("--help").code == 0
    assert "stdio" in run("--help").out
    assert run().code == 2  # no command: help, as a usage error
    unknown = run("nosuchcommand")
    assert unknown.code == 2
    assert "No such command" in unknown.err
    assert unknown.out == ""
    assert run("call").code == 2  # missing argument
    assert run("http", "--port", "99999").code == 2  # out of range
    assert run("config").code == 2


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        (ErrorCode.PERMISSION_ACCESSIBILITY, 3),
        (ErrorCode.PERMISSION_SCREEN_RECORDING, 3),
        (ErrorCode.PERMISSION_AUTOMATION, 3),
        (ErrorCode.UNSUPPORTED_OS, 4),
        (ErrorCode.INVALID_ARGUMENT, 2),
        (ErrorCode.APP_NOT_FOUND, 1),
        (ErrorCode.HOST_UNAVAILABLE, 1),
        (ErrorCode.INTERNAL, 1),
    ],
)
def test_exit_codes(code: ErrorCode, exit_code: int) -> None:
    assert exit_code_for(AXPilotError(code, "x")) == exit_code


# =========================================================================== errors
def boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("something broke")


def test_unexpected_errors_are_one_line_without_debug(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_tool_rows", boom)
    ran = run("tools")
    assert ran.code == 1
    assert ran.err.startswith("error: INTERNAL: RuntimeError: something broke")
    assert "Traceback" not in ran.err
    assert "--debug" in ran.err


def test_debug_shows_the_traceback(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_tool_rows", boom)
    ran = run("--debug", "tools")
    assert ran.code == 1
    assert "Traceback" in ran.err


def test_interrupts_exit_130(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_tool_rows", interrupted)
    assert run("tools").code == 130  # Typer turns Ctrl-C into a quiet exit 130


def test_errors_inside_task_groups_are_unwrapped(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def grouped(*args: Any, **kwargs: Any) -> Any:
        inner = AXPilotError(ErrorCode.PERMISSION_ACCESSIBILITY, "No Accessibility permission.")
        raise ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("x"), inner])])

    monkeypatch.setattr(cli, "_tool_rows", grouped)
    ran = run("tools")
    assert ran.code == 3
    assert "PERMISSION_ACCESSIBILITY" in ran.err


def test_a_group_of_interrupts_exits_130(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def grouped(*args: Any, **kwargs: Any) -> Any:
        raise BaseExceptionGroup("x", [KeyboardInterrupt()])

    monkeypatch.setattr(cli, "_tool_rows", grouped)
    assert run("tools").code == 130


def test_a_group_without_known_errors_is_internal(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def grouped(*args: Any, **kwargs: Any) -> Any:
        raise ExceptionGroup("x", [ValueError("odd")])

    monkeypatch.setattr(cli, "_tool_rows", grouped)
    ran = run("tools", "--json")
    assert ran.code == 1
    assert ran.json()["error"]["code"] == "INTERNAL"
    assert "ValueError: odd" in ran.json()["error"]["message"]


def test_json_errors_go_to_stdout(run: Any) -> None:
    ran = run("config", "get", "nope.nothing", "--json")
    assert ran.code == 2
    error = ran.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert "hint" in error
    assert ran.err == ""


# =========================================================================== tools & call
def test_tools(run: Any) -> None:
    ran = run("tools")
    assert ran.code == 0
    lines = ran.out.splitlines()
    assert any(line.startswith("snapshot ") for line in lines)
    data = run("tools", "--json").json()
    assert data["count"] == len(data["tools"]) == len(lines)
    snapshot = next(tool for tool in data["tools"] if tool["name"] == "snapshot")
    assert snapshot["input_schema"]["type"] == "object"
    assert snapshot["annotations"]["readOnlyHint"] is True
    minimal = run("tools", "--profile", "minimal", "--json").json()
    assert minimal["count"] < data["count"]


def test_tools_with_windows_mcp_naming_or_a_bad_profile(run: Any) -> None:
    bad = run("tools", "--profile", "godmode")
    assert bad.code == 2
    assert "Unknown profile" in bad.err


def test_call_prints_structured_json(run: Any) -> None:
    ran = run("call", "status", "--engine", "fake")
    assert ran.code == 0, ran.err
    data = ran.json()
    assert data["engine"] == "fake"
    snap = run("call", "snapshot", "--engine", "fake", "--arg", "app=Notes", "--arg", "max_elements=5").json()
    assert snap["app"]["name"] == "Notes"
    assert len(snap["elements"]) <= 5
    same = run("call", "snapshot", "--engine", "fake", "--json", '{"app": "Notes", "max_elements": 5}').json()
    assert same["app"] == snap["app"]


def test_call_tool_errors_print_the_error_object(run: Any) -> None:
    ran = run("call", "snapshot", "--engine", "fake", "--arg", "app=NoSuchApp")
    assert ran.code == 1
    assert ran.json()["error"]["code"] == "APP_NOT_FOUND"
    unknown = run("call", "no_such_tool", "--engine", "fake")
    assert unknown.code == 2
    assert unknown.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_commands_turn_the_cocoa_run_loop_without_changing_results(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: on macOS every command keeps the main run loop turning (keyboard layout, app list);
    errors and exit codes must come through exactly as without it."""
    turns: list[float] = []
    monkeypatch.setattr(cli, "main_thread_pump", lambda: turns.append)
    ok = run("call", "status", "--engine", "fake")
    assert ok.code == 0, ok.err
    assert turns, "the run loop was not turned"
    failed = run("call", "snapshot", "--engine", "fake", "--arg", "app=NoSuchApp")
    assert failed.code == 1
    assert failed.json()["error"]["code"] == "APP_NOT_FOUND"
    unknown = run("call", "no_such_tool", "--engine", "fake")
    assert unknown.code == 2


def test_call_confirmations_need_yes_outside_a_mac(run: Any) -> None:
    denied = run("call", "app", "--engine", "fake", "--arg", "action=force_quit", "--arg", "name=Notes")
    assert denied.code == 1
    assert denied.json()["error"]["code"] == "USER_DENIED"
    approved = run(
        "call", "app", "--engine", "fake", "--yes", "--arg", "action=force_quit", "--arg", "name=Notes"
    )
    assert approved.code == 0, approved.out


@pytest.mark.parametrize(
    "argv",
    [
        ["--arg", "no-equals"],
        ["--arg", "=value"],
        ["--json", "{not json"],
        ["--json", "[1, 2]"],
    ],
)
def test_call_rejects_bad_arguments(run: Any, argv: list[str]) -> None:
    ran = run("call", "status", "--engine", "fake", *argv)
    assert ran.code == 2
    assert ran.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_call_with_a_fixture(run: Any, tmp_path: Path) -> None:
    demo = resources.files("axpilot.engine").joinpath("fixtures", "demo_desktop.json")
    desktop = json.loads(demo.read_text(encoding="utf-8"))
    desktop["os_version"] = "15.9"
    fixture = tmp_path / "desk.json"
    fixture.write_text(json.dumps(desktop), encoding="utf-8")
    ran = run("call", "status", "--fixture", str(fixture))  # a fixture implies the fake engine
    assert ran.code == 0, ran.out
    assert ran.json()["os_version"] == "15.9"
    missing = run("call", "status", "--fixture", str(tmp_path / "missing.json"))
    assert missing.code != 0


def test_dump_tree(run: Any) -> None:
    ran = run("debug", "dump-tree", "--engine", "fake", "--app", "Notes")
    assert ran.code == 0, ran.err
    tree = ran.json()
    assert tree["app"]["name"] == "Notes"
    missing = run("debug", "dump-tree", "--engine", "fake", "--app", "NoSuchApp")
    assert missing.code == 1
    assert missing.json()["error"]["code"] == "APP_NOT_FOUND"


# =========================================================================== serving
@pytest.mark.skipif(sys.platform == "darwin", reason="on a Mac the bridge would start AXPilot.app")
def test_the_stdio_bridge_without_a_host_reports_it_and_still_serves(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))
    ran = run("stdio")  # stdin is empty: the stand-in answers nothing and ends with it
    assert ran.code == 0
    assert "HOST_UNAVAILABLE" in ran.err
    assert "--engine fake" in ran.err
    assert "every tool call returns this error" in ran.err
    assert ran.out == ""  # stdout stays clean for JSON-RPC


def test_http_refuses_remote_binds(run: Any, tmp_path: Path) -> None:
    remote = run("http", "--host", "0.0.0.0")
    assert remote.code == 1
    assert "BLOCKED_BY_POLICY" in remote.err
    cert = tmp_path / "cert.pem"
    cert.write_text("c", encoding="utf-8")
    half = run("http", "--tls-cert", str(cert))
    assert half.code == 2
    assert not (tmp_path / "support" / "http-token").exists()  # refused before creating a token


def test_http_reads_the_server_settings(run: Any) -> None:
    assert run("config", "set", "server.http_host", "0.0.0.0").code == 0
    ran = run("http")
    assert ran.code == 1
    assert "Refusing to listen on 0.0.0.0" in ran.err


# =========================================================================== config
def test_config_path_show_get_set(run: Any, tmp_path: Path) -> None:
    assert run("config", "path").out.strip() == str(tmp_path / "config" / "config.toml")
    shown = run("config", "show", "--json").json()
    assert shown["tools"]["profile"] == "standard"
    assert "[tools]" in run("config", "show").out or "tools" in run("config", "show").out
    assert run("config", "get", "tools.profile").out.strip() == "standard"
    assert run("config", "get", "tools.profile", "--json").out.strip() == '"standard"'
    assert run("config", "set", "tools.profile", "minimal").code == 0
    assert run("config", "get", "tools.profile").out.strip() == "minimal"
    assert (tmp_path / "config" / "config.toml").is_file()
    section = json.loads(run("config", "get", "tools").out)
    assert section["profile"] == "minimal"


def test_config_set_refuses_unknown_keys_and_bad_values(run: Any, tmp_path: Path) -> None:
    unknown = run("config", "set", "tools.nope", "1")
    assert unknown.code == 2
    assert "Unknown configuration key" in unknown.err
    invalid = run("config", "set", "tools.profile", "godmode")
    assert invalid.code == 2
    assert not (tmp_path / "config" / "config.toml").exists()


def test_config_edit_uses_the_editor(run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "editor.sh"
    script.write_text('#!/bin/sh\necho "[tools]\nprofile = \\"full\\"" > "$1"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    assert run("config", "edit").code == 0
    assert run("config", "get", "tools.profile").out.strip() == "full"


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS falls back to TextEdit")
def test_config_edit_without_an_editor(run: Any, tmp_path: Path) -> None:
    ran = run("config", "edit")
    assert ran.code == 1
    assert "NOT_SUPPORTED" in ran.err
    assert (tmp_path / "config" / "config.toml").is_file()  # created for editing by hand


# =========================================================================== skill
def test_skill_install(run: Any, tmp_path: Path) -> None:
    dest = tmp_path / "skills" / "axpilot"
    first = run("skill", "install", "--dest", str(dest), "--json")
    assert first.code == 0
    assert first.json() == {"path": str(dest / "SKILL.md"), "changed": True}
    assert "name: axpilot" in (dest / "SKILL.md").read_text(encoding="utf-8")
    again = run("skill", "install", "--dest", str(dest))
    assert again.out.startswith("Already up to date:")
    (dest / "SKILL.md").write_text("my notes", encoding="utf-8")
    refused = run("skill", "install", "--dest", str(dest))
    assert refused.code == 2
    assert "--force" in refused.err
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "my notes"
    assert run("skill", "install", "--dest", str(dest), "--force").code == 0
    assert "name: axpilot" in (dest / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("target", "relative"),
    [
        ("claude", "home/.claude/skills/axpilot"),
        ("claude-project", ".claude/skills/axpilot"),
        ("codex", "home/.codex/skills/axpilot"),
    ],
)
def test_skill_targets(run: Any, tmp_path: Path, target: str, relative: str) -> None:
    assert run("skill", "install", "--target", target).code == 0
    assert (tmp_path / relative / "SKILL.md").is_file()


# =========================================================================== clients
def test_connect_lists_clients(run: Any) -> None:
    listed = run("connect", "--list")
    assert listed.code == 0
    assert "claude-desktop" in listed.out
    rows = run("connect", "--json").json()["clients"]
    assert {"name", "display_name", "installed"} <= set(rows[0])


def test_connect_and_disconnect_a_client(run: Any, tmp_path: Path) -> None:
    preview = run("connect", "cursor", "--dry-run", "--json")
    assert preview.code == 0, preview.err
    assert preview.json()["dry_run"] is True
    config = tmp_path / "home" / ".cursor" / "mcp.json"
    assert not config.exists()
    added = run("connect", "cursor", "--json").json()
    assert added["changed"] is True
    assert "axpilot" in json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    removed = run("disconnect", "cursor")
    assert removed.code == 0
    assert "axpilot" not in json.loads(config.read_text(encoding="utf-8")).get("mcpServers", {})


def test_connect_an_unknown_client(run: Any) -> None:
    ran = run("connect", "notepad")
    assert ran.code in (1, 2)
    assert "notepad" in ran.err


# =========================================================================== setup & doctor
@pytest.mark.skipif(sys.platform == "darwin", reason="checks the non-macOS answers")
def test_setup_doctor_and_update_outside_macos(run: Any) -> None:
    setup = run("setup", "--json")
    assert setup.code == 4
    assert setup.json()["supported"] is False
    assert run("update").code == 4
    doctor = run("doctor", "--json")
    assert doctor.code == 4
    payload = doctor.json()
    assert payload["status"] == "fail"
    assert any(check["name"] == "Platform" for check in payload["checks"])
    assert "Platform" in run("doctor").out


def test_uninstall(run: Any, tmp_path: Path) -> None:
    runtime = tmp_path / "support" / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (tmp_path / "support" / "runtime.json").write_text("{}", encoding="utf-8")
    config = tmp_path / "config" / "config.toml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    ran = run("uninstall", "--yes", "--json")
    assert ran.code == 0
    assert set(ran.json()["removed"]) == {str(runtime), str(tmp_path / "support" / "runtime.json")}
    assert ran.json()["quit"] is False  # no host answers on (this test's) socket: nothing to quit
    assert config.exists()  # kept
    assert run("uninstall", "--yes").out.startswith("Removed: nothing")


def test_uninstall_quits_a_running_axpilot_first(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "support" / "runtime"
    runtime.mkdir(parents=True)
    stops: list[tuple[str, bool]] = []

    def stop_host(runner: Any, *, retry: str) -> None:
        stops.append((retry, runtime.exists()))

    monkeypatch.setattr("axpilot.host.daemon.socket_is_live", lambda path: True)
    monkeypatch.setattr("axpilot.setup.stop_host", stop_host)
    ran = run("uninstall", "--yes", "--json")
    assert ran.code == 0
    assert ran.json()["quit"] is True
    assert stops == [("axpilot uninstall", True)]  # quit while the runtime still existed
    assert not runtime.exists()
    assert run("uninstall", "--yes").out.startswith("Quit AXPilot. Removed: nothing")


def test_uninstall_keeps_the_runtime_when_axpilot_does_not_quit(
    run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "support" / "runtime"
    runtime.mkdir(parents=True)

    def stop_host(runner: Any, *, retry: str) -> None:
        raise AXPilotError(
            ErrorCode.TIMEOUT, "AXPilot did not quit within 15 s.", hint=f"Then run `{retry}` again."
        )

    monkeypatch.setattr("axpilot.host.daemon.socket_is_live", lambda path: True)
    monkeypatch.setattr("axpilot.setup.stop_host", stop_host)
    ran = run("uninstall", "--yes")
    assert ran.code == 1
    assert "TIMEOUT" in ran.err
    assert "axpilot uninstall" in ran.err
    assert runtime.exists()  # never deleted under a host that still runs


def test_uninstall_asks_first(run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "support" / "runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("n\n"))
    ran = run("uninstall")
    assert ran.code == 1
    assert runtime.exists()
