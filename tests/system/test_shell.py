from __future__ import annotations

from pathlib import Path

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import FakeRunner
from axpilot.system.shell import MAX_SHELL_TIMEOUT_S, run_shell, shell_argv


def test_runs_a_login_shell_with_the_command_as_one_argument(runner: FakeRunner) -> None:
    runner.register(["/bin/zsh", "-lc"], stdout="hi\n")
    result = run_shell(runner, "echo hi | tr a-z A-Z; ls ~")
    call = runner.last_call
    assert call.argv == ["/bin/zsh", "-lc", "echo hi | tr a-z A-Z; ls ~"]
    assert call.timeout_s == 30
    assert call.input_text is None
    assert result.stdout == "hi\n"


def test_custom_shell_timeout_and_output_limit(runner: FakeRunner) -> None:
    runner.register(["/bin/bash"], stdout="")
    run_shell(runner, "true", shell="/bin/bash", timeout_s=120, max_output=500)
    call = runner.last_call
    assert call.argv[:2] == ["/bin/bash", "-lc"]
    assert call.timeout_s == 120
    assert call.max_output == 500


def test_cwd_is_expanded_and_validated(runner: FakeRunner, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "project").mkdir()
    runner.register(["/bin/zsh"], stdout="")
    run_shell(runner, "pwd", cwd="~/project")
    assert runner.last_call.cwd == str(tmp_path / "project")
    with pytest.raises(AXPilotError) as info:
        run_shell(runner, "pwd", cwd="~/missing")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "does not exist" in info.value.message


def test_non_zero_exit_is_returned_not_raised(runner: FakeRunner) -> None:
    runner.register(["/bin/zsh"], stdout="", stderr="zsh: command not found: nope\n", exit_code=127)
    result = run_shell(runner, "nope")
    assert result.exit_code == 127
    assert "command not found" in result.stderr


def test_timeout_is_returned_with_partial_output(runner: FakeRunner) -> None:
    runner.register(["/bin/zsh"], stdout="partial\n", timed_out=True)
    result = run_shell(runner, "sleep 100")
    assert result.timed_out
    assert result.stdout == "partial\n"


@pytest.mark.parametrize(
    ("command", "kwargs", "fragment"),
    [
        ("", {}, "empty"),
        ("   ", {}, "empty"),
        ("echo a\x00b", {}, "NUL"),
        ("x" * 100_001, {}, "too long"),
        ("ls", {"shell": "zsh"}, "absolute path"),
        ("ls", {"timeout_s": 0}, "between 0 and"),
        ("ls", {"timeout_s": MAX_SHELL_TIMEOUT_S + 1}, "between 0 and"),
        ("ls", {"timeout_s": True}, "number"),
        ("ls", {"cwd": ""}, "directory"),
    ],
)
def test_invalid_arguments(runner: FakeRunner, command: str, kwargs: dict, fragment: str) -> None:
    with pytest.raises(AXPilotError) as info:
        run_shell(runner, command, **kwargs)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in info.value.message
    assert runner.calls == []


def test_shell_argv() -> None:
    assert shell_argv("ls -la") == ["/bin/zsh", "-lc", "ls -la"]
    assert shell_argv("ls", shell="/bin/sh") == ["/bin/sh", "-lc", "ls"]
