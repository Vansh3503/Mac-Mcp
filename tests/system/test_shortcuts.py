from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandResult, FakeCall, FakeRunner
from axpilot.system.shortcuts import MAX_TIMEOUT_S, list_shortcuts, run_shortcut, shortcut_run_argv


def test_list_shortcuts_parses_lines(runner: FakeRunner) -> None:
    runner.register(["shortcuts", "list"], stdout="Morning Routine\n  Resize Image \n\nLog Water\n")
    assert list_shortcuts(runner) == ["Morning Routine", "Resize Image", "Log Water"]
    assert runner.last_call.argv == ["shortcuts", "list"]


def test_list_shortcuts_empty(runner: FakeRunner) -> None:
    runner.register(["shortcuts", "list"], stdout="")
    assert list_shortcuts(runner) == []


def test_list_shortcuts_without_the_cli(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError) as info:
        list_shortcuts(runner)
    assert info.value.code is ErrorCode.NOT_SUPPORTED
    assert "macOS 12" in (info.value.hint or "")


def test_run_shortcut_without_input(runner: FakeRunner) -> None:
    runner.register(["shortcuts", "run"], stdout="Sunny, 21°C\n")
    result = run_shortcut(runner, "Weather; rm -rf ~")
    call = runner.last_call
    assert call.argv == ["shortcuts", "run", "Weather; rm -rf ~"]
    assert call.timeout_s == 60
    assert result.stdout == "Sunny, 21°C\n"


def test_run_shortcut_passes_input_through_a_private_temp_file(runner: FakeRunner) -> None:
    seen: dict[str, object] = {}

    def handler(call: FakeCall) -> CommandResult:
        assert call.argv[:4] == ["shortcuts", "run", "Translate", "--input-path"]
        path = Path(call.argv[4])
        seen["path"] = path
        seen["content"] = path.read_text(encoding="utf-8")
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        return CommandResult(call.argv, 0, "Bonjour\n", "")

    runner.register(["shortcuts", "run"], handler=handler)
    result = run_shortcut(runner, "Translate", input_text="Hello, wörld\nline 2", timeout_s=5)
    assert result.stdout == "Bonjour\n"
    assert seen["content"] == "Hello, wörld\nline 2"
    assert seen["mode"] == 0o600
    assert not Path(str(seen["path"])).exists(), "temp input file must be deleted"
    assert runner.last_call.input_text is None, "input goes through the file, not stdin"


def test_temp_file_is_deleted_even_when_the_shortcut_fails(runner: FakeRunner) -> None:
    paths: list[str] = []

    def handler(call: FakeCall) -> CommandResult:
        paths.append(call.argv[4])
        return CommandResult(call.argv, 1, "", "Error: The action could not run.\n")

    runner.register(["shortcuts", "run"], handler=handler)
    with pytest.raises(AXPilotError) as info:
        run_shortcut(runner, "Broken", input_text="x")
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "The action could not run." in info.value.message
    assert paths
    assert not Path(paths[0]).exists()


@pytest.mark.parametrize(
    "stderr",
    [
        'Error: Couldn’t find shortcut "Nope"\n',
        "Error: The operation couldn't be completed. No shortcut named Nope.\n",
    ],
)
def test_unknown_shortcut_is_invalid_argument(runner: FakeRunner, stderr: str) -> None:
    runner.register(["shortcuts", "run"], stderr=stderr, exit_code=1)
    with pytest.raises(AXPilotError) as info:
        run_shortcut(runner, "Nope")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "Nope" in info.value.message
    assert "list" in (info.value.hint or "")


def test_shortcut_timeout(runner: FakeRunner) -> None:
    runner.register(["shortcuts", "run"], timed_out=True)
    with pytest.raises(AXPilotError) as info:
        run_shortcut(runner, "Ask Me", timeout_s=2)
    assert info.value.code is ErrorCode.TIMEOUT
    assert "Ask for Input" in (info.value.hint or "")
    assert runner.last_call.timeout_s == 2


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("", {}),
        ("   ", {}),
        ("-i", {}),
        ("--output-path=/tmp/x", {}),
        ("a\x00b", {}),
        ("two\nlines", {}),
        ("x" * 256, {}),
        ("ok", {"timeout_s": 0}),
        ("ok", {"timeout_s": MAX_TIMEOUT_S + 1}),
        ("ok", {"input_text": 5}),
    ],
)
def test_invalid_arguments(runner: FakeRunner, name: str, kwargs: dict) -> None:
    with pytest.raises(AXPilotError) as info:
        run_shortcut(runner, name, **kwargs)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert runner.calls == []


def test_shortcut_run_argv() -> None:
    assert shortcut_run_argv("A") == ["shortcuts", "run", "A"]
    assert shortcut_run_argv("A", "/tmp/in.txt") == ["shortcuts", "run", "A", "--input-path", "/tmp/in.txt"]


def test_timeout_must_be_a_number(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError):
        list_shortcuts(runner, timeout_s=True)  # type: ignore[arg-type]


def test_input_file_already_removed_by_the_shortcut(runner: FakeRunner) -> None:
    def handler(call: FakeCall) -> CommandResult:
        os.unlink(call.argv[4])
        return CommandResult(call.argv, 0, "done\n", "")

    runner.register(["shortcuts", "run"], handler=handler)
    assert run_shortcut(runner, "Consume", input_text="x").stdout == "done\n"


def test_input_file_cleanup_failure_does_not_fail_the_run(
    runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axpilot.system import shortcuts

    paths: list[str] = []

    def handler(call: FakeCall) -> CommandResult:
        paths.append(call.argv[4])
        return CommandResult(call.argv, 0, "ok\n", "")

    def refuse(path: str) -> None:
        raise PermissionError(13, "Permission denied", path)

    runner.register(["shortcuts", "run"], handler=handler)
    monkeypatch.setattr(shortcuts.os, "unlink", refuse)
    try:
        assert run_shortcut(runner, "Any", input_text="x").stdout == "ok\n"
    finally:
        monkeypatch.undo()
        os.unlink(paths[0])
