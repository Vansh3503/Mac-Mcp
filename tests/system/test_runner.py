from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system import runner as runner_module
from axpilot.system.runner import (
    EXIT_NOT_EXECUTABLE,
    EXIT_NOT_FOUND,
    EXIT_TIMED_OUT,
    CommandResult,
    CommandRunner,
    FakeCall,
    FakeRunner,
    ensure_success,
    excerpt,
    output_records,
    truncate_output,
)

PY = sys.executable
MARKER_RE = re.compile(r"\n\[output truncated after (\d+) characters; (\d+) bytes in total\]\Z")


def python(code: str) -> list[str]:
    return [PY, "-c", code]


def process_gone(pid: int) -> bool:
    """True once ``pid`` no longer exists (or is only a zombie awaiting its reaper)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return state.stdout.strip() == "" or state.stdout.strip().startswith("Z")


def wait_until(predicate, timeout_s: float = 3.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------------------------
# CommandRunner: real subprocesses (harmless commands only)


@pytest.fixture
def real() -> CommandRunner:
    return CommandRunner()


def test_runs_argv_and_captures_stdout(real: CommandRunner) -> None:
    result = real.run(["echo", "hello", "world"])
    assert result == CommandResult(["echo", "hello", "world"], 0, "hello world\n", "")
    assert result.ok
    assert result.program == "echo"


def test_arguments_are_not_interpreted_by_a_shell(real: CommandRunner) -> None:
    result = real.run(["echo", "$HOME; rm -rf / `id` $(id)"])
    assert result.stdout == "$HOME; rm -rf / `id` $(id)\n"


def test_captures_stderr_and_exit_code(real: CommandRunner) -> None:
    result = real.run(["sh", "-c", "echo out; echo oops >&2; exit 3"])
    assert result.exit_code == 3
    assert result.stdout == "out\n"
    assert result.stderr == "oops\n"
    assert not result.ok
    assert not result.timed_out


def test_input_text_is_written_to_stdin_as_utf8(real: CommandRunner) -> None:
    result = real.run(["cat"], input_text="héllo wörld ✓\nline 2")
    assert result.stdout == "héllo wörld ✓\nline 2"


def test_stdin_is_empty_without_input_text(real: CommandRunner) -> None:
    started = time.monotonic()
    result = real.run(["cat"], timeout_s=5)
    assert result.ok
    assert result.stdout == ""
    assert time.monotonic() - started < 2


def test_large_input_to_a_command_that_ignores_it(real: CommandRunner) -> None:
    result = real.run(["true"], input_text="x" * 2_000_000, timeout_s=5)
    assert result.ok
    assert not result.timed_out


def test_timeout_returns_partial_output(real: CommandRunner) -> None:
    started = time.monotonic()
    result = real.run(["sh", "-c", "echo started; exec sleep 5"], timeout_s=0.3)
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert result.exit_code == EXIT_TIMED_OUT
    assert result.stdout == "started\n"
    assert not result.ok
    assert elapsed < 3


def test_timeout_kills_the_whole_process_group(real: CommandRunner) -> None:
    result = real.run(["sh", "-c", "sleep 30 & echo $!; wait"], timeout_s=0.3)
    assert result.timed_out
    grandchild = int(result.stdout.split()[0])
    assert wait_until(lambda: process_gone(grandchild)), "background child survived the timeout"


def test_background_child_holding_the_pipe_counts_as_timeout(real: CommandRunner) -> None:
    started = time.monotonic()
    result = real.run(["sh", "-c", "echo parent done; sleep 30 &"], timeout_s=0.4)
    assert result.timed_out
    assert result.exit_code == EXIT_TIMED_OUT
    assert result.stdout == "parent done\n"
    assert time.monotonic() - started < 4


def test_output_is_truncated_with_a_marker(real: CommandRunner) -> None:
    result = real.run(python("print('x' * 50_000)"), max_output=1000)
    assert result.truncated
    assert result.stdout.startswith("x" * 1000)
    marker = MARKER_RE.search(result.stdout)
    assert marker is not None
    assert marker.group(1) == "1000"
    assert marker.group(2) == "50001"
    assert result.stdout[: marker.start()] == "x" * 1000


def test_memory_stays_bounded_for_huge_output(real: CommandRunner) -> None:
    code = "import sys\nfor _ in range(80): sys.stdout.write('y' * 65536)"
    result = real.run(python(code), max_output=100, timeout_s=20)
    assert result.ok
    assert result.truncated
    marker = MARKER_RE.search(result.stdout)
    assert marker is not None
    assert marker.group(2) == str(80 * 65536)
    assert len(result.stdout) < 200


def test_stderr_is_truncated_independently(real: CommandRunner) -> None:
    result = real.run(python("import sys; sys.stderr.write('e' * 5000); print('ok')"), max_output=100)
    assert result.truncated
    assert result.stdout == "ok\n"
    assert result.stderr.startswith("e" * 100)
    assert MARKER_RE.search(result.stderr)


def test_invalid_utf8_is_replaced(real: CommandRunner) -> None:
    result = real.run(python("import sys; sys.stdout.buffer.write(b'a\\xffb')"))
    assert result.stdout == "a�b"


def test_env_is_merged_over_the_current_environment(real: CommandRunner) -> None:
    result = real.run(["sh", "-c", 'printf "%s|%s" "$AXP_TEST_VAR" "$PATH"'], env={"AXP_TEST_VAR": "42"})
    value, path = result.stdout.split("|", 1)
    assert value == "42"
    assert path == os.environ["PATH"]


def test_cwd_is_used_and_expands_home(real: CommandRunner, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    probe = python("import os; print(os.getcwd())")
    assert real.run(probe, cwd=str(tmp_path)).stdout.strip() == os.path.realpath(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert real.run(probe, cwd="~").stdout.strip() == os.path.realpath(tmp_path)


def test_missing_program_reports_127(real: CommandRunner) -> None:
    result = real.run(["axpilot-definitely-not-a-command-xyz", "--help"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "command not found" in result.stderr
    assert result.stdout == ""


def test_non_executable_program_reports_126(real: CommandRunner, tmp_path: Path) -> None:
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)
    result = real.run([str(script)])
    assert result.exit_code == EXIT_NOT_EXECUTABLE


def test_signal_death_reports_128_plus_signal(real: CommandRunner) -> None:
    result = real.run(["sh", "-c", "kill -9 $$"])
    assert result.exit_code == 128 + 9
    assert not result.timed_out


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"argv": []}, "must not be empty"),
        ({"argv": "echo hi"}, "not a single string"),
        ({"argv": [""]}, "argv[0]"),
        ({"argv": ["echo", "a\x00b"]}, "NUL"),
        ({"argv": ["echo", 3]}, "must be a string"),
        ({"argv": ["echo"], "timeout_s": 0}, "positive"),
        ({"argv": ["echo"], "timeout_s": -1}, "positive"),
        ({"argv": ["echo"], "timeout_s": float("nan")}, "positive"),
        ({"argv": ["echo"], "timeout_s": True}, "number"),
        ({"argv": ["echo"], "max_output": -1}, "max_output"),
        ({"argv": ["echo"], "env": {"A=B": "x"}}, "environment variable name"),
        ({"argv": ["echo"], "env": {"A": "x\x00"}}, "NUL"),
        ({"argv": ["echo"], "cwd": "/definitely/not/a/dir"}, "does not exist"),
    ],
)
def test_invalid_arguments_raise_before_running(real: CommandRunner, kwargs: dict, fragment: str) -> None:
    argv = kwargs.pop("argv")
    with pytest.raises(AXPilotError) as info:
        real.run(argv, **kwargs)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in info.value.message


# ---------------------------------------------------------------------------------------------
# FakeRunner


def test_fake_unregistered_command_returns_127_without_raising() -> None:
    fake = FakeRunner()
    result = fake.run(["mdfind", "-0", "invoice"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert "no response registered" in result.stderr
    assert "mdfind -0 invoice" in result.stderr
    assert len(fake.calls) == 1


def test_fake_most_specific_prefix_wins() -> None:
    fake = FakeRunner()
    fake.register([], stdout="catch-all")
    fake.register(["defaults"], stdout="defaults")
    fake.register(["defaults", "read"], stdout="read")
    fake.register(["defaults", "read", "com.apple.dock"], stdout="dock")
    assert fake.run(["defaults", "read", "com.apple.dock", "tilesize"]).stdout == "dock"
    assert fake.run(["defaults", "read", "com.apple.finder"]).stdout == "read"
    assert fake.run(["defaults", "write", "x", "y", "-int", "1"]).stdout == "defaults"
    assert fake.run(["ps"]).stdout == "catch-all"


def test_fake_prefix_must_match_whole_elements() -> None:
    fake = FakeRunner()
    fake.register(["defaults", "read"], stdout="read")
    assert fake.run(["defaults", "reader"]).exit_code == EXIT_NOT_FOUND
    assert fake.run(["defaults"]).exit_code == EXIT_NOT_FOUND


def test_fake_registering_again_replaces_the_response() -> None:
    fake = FakeRunner()
    fake.register(["sw_vers"], stdout="old")
    fake.register(["sw_vers"], stdout="new", exit_code=2)
    result = fake.run(["sw_vers"])
    assert (result.stdout, result.exit_code) == ("new", 2)


def test_fake_records_every_keyword(tmp_path: Path) -> None:
    fake = FakeRunner()
    fake.register(["cat"], stdout="x")
    fake.run(["cat"], input_text="in", timeout_s=7, cwd=str(tmp_path), env={"A": "1"}, max_output=99)
    assert fake.last_call == FakeCall(["cat"], "in", 7, str(tmp_path), {"A": "1"}, 99)
    assert fake.calls_for(["cat"]) == [fake.last_call]
    assert fake.calls_for(["dog"]) == []


def test_fake_handler_computes_the_result() -> None:
    fake = FakeRunner()

    def echo(call: FakeCall) -> CommandResult:
        return CommandResult(call.argv, 0, (call.input_text or "").upper(), "")

    fake.register(["osascript"], handler=echo)
    assert fake.run(["osascript", "-l", "AppleScript"], input_text="abc").stdout == "ABC"


def test_fake_timed_out_registration() -> None:
    fake = FakeRunner()
    fake.register(["sleep"], stdout="partial", timed_out=True, exit_code=0)
    result = fake.run(["sleep", "10"])
    assert result.timed_out
    assert result.exit_code == EXIT_TIMED_OUT
    assert result.stdout == "partial"


def test_fake_applies_output_limits() -> None:
    fake = FakeRunner()
    fake.register(["ps"], stdout="z" * 500)
    result = fake.run(["ps"], max_output=10)
    assert result.truncated
    assert result.stdout.startswith("z" * 10)
    assert MARKER_RE.search(result.stdout)


def test_fake_validates_like_the_real_runner() -> None:
    fake = FakeRunner()
    with pytest.raises(AXPilotError) as info:
        fake.run([])
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError):
        fake.register("defaults read")  # type: ignore[arg-type]
    assert fake.calls == []


def test_fake_last_call_without_calls() -> None:
    with pytest.raises(LookupError):
        _ = FakeRunner().last_call


# ---------------------------------------------------------------------------------------------
# Output helpers


def test_truncate_output() -> None:
    assert truncate_output("short", 10) == ("short", False)
    assert truncate_output("exactly10!", 10) == ("exactly10!", False)
    text, cut = truncate_output("é" * 20, 5)
    assert cut
    assert text.startswith("ééééé\n[output truncated after 5 characters; 40 bytes in total]")
    assert truncate_output("abc", 0) == ("\n[output truncated after 0 characters; 3 bytes in total]", True)
    assert truncate_output("", 0) == ("", False)


def test_output_records() -> None:
    assert output_records("") == []
    assert output_records("a\nb\n") == ["a", "b"]
    assert output_records("a\nb") == ["a", "b"]
    assert output_records("a\n\nb\n") == ["a", "", "b"]
    assert output_records("a\x00b\x00", "\x00") == ["a", "b"]


def test_output_records_drops_partial_record_after_truncation() -> None:
    text, cut = truncate_output("first\nsecond\nthird line", 15)
    assert cut
    assert output_records(text) == ["first", "second"]
    exact, _ = truncate_output("first\nsecond\nthird", 13)
    assert output_records(exact) == ["first", "second"]
    with pytest.raises(AXPilotError):
        output_records("x", "")


def test_excerpt() -> None:
    assert excerpt("  hello  ") == "hello"
    assert excerpt("x" * 600) == "x" * 499 + "…"
    assert len(excerpt("y" * 50, 10)) == 10


# ---------------------------------------------------------------------------------------------
# ensure_success


def test_ensure_success_returns_successful_results() -> None:
    ok = CommandResult(["/usr/bin/sw_vers"], 0, "x", "")
    assert ensure_success(ok, what="Reading the OS version") is ok


def test_ensure_success_timeout() -> None:
    result = CommandResult(["mdfind", "secret query"], EXIT_TIMED_OUT, "", "", timed_out=True)
    with pytest.raises(AXPilotError) as info:
        ensure_success(result, what="Spotlight search", timeout_s=15)
    assert info.value.code is ErrorCode.TIMEOUT
    assert "15 s" in info.value.message
    assert "secret query" not in info.value.to_text()


def test_ensure_success_missing_program() -> None:
    result = CommandResult(["/usr/bin/shortcuts", "list"], EXIT_NOT_FOUND, "", "shortcuts: command not found")
    with pytest.raises(AXPilotError) as info:
        ensure_success(result, what="Listing shortcuts", missing_hint="Needs macOS 12.")
    assert info.value.code is ErrorCode.NOT_SUPPORTED
    assert "`shortcuts`" in info.value.message
    assert info.value.hint == "Needs macOS 12."


def test_ensure_success_failure_summarizes_stderr_without_arguments() -> None:
    stderr = "first problem\n" + "z" * 2000
    result = CommandResult(["defaults", "write", "dom", "token", "-string", "hunter2"], 4, "", stderr)
    with pytest.raises(AXPilotError) as info:
        ensure_success(result, what="Writing a preference", hint="Check the domain.")
    err = info.value
    assert err.code is ErrorCode.COMMAND_FAILED
    assert "exit 4" in err.message
    assert "first problem" in err.message
    assert err.hint == "Check the domain."
    assert err.details["exit_code"] == 4
    assert err.details["command"] == "defaults"
    assert len(err.details["stderr"]) <= 500
    assert "hunter2" not in str(err.to_dict())


# ---------------------------------------------------------------------------------------------
# Failure and cleanup paths of the real runner


def test_exec_format_error_reports_126(real: CommandRunner, tmp_path: Path) -> None:
    garbage = tmp_path / "garbage-binary"
    garbage.write_bytes(b"\x00\x01\x02\x03 not a program")
    garbage.chmod(0o755)
    assert real.run([str(garbage)]).exit_code == EXIT_NOT_EXECUTABLE


def test_unexpected_spawn_errors_become_command_failed(
    real: CommandRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(runner_module.subprocess, "Popen", refuse)
    with pytest.raises(AXPilotError) as info:
        real.run(["echo", "hi"])
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "Argument list too long" in info.value.message
    assert isinstance(info.value.__cause__, OSError)


def test_interruption_kills_the_child_and_propagates(
    real: CommandRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_wait = subprocess.Popen.wait
    seen: list[subprocess.Popen[bytes]] = []

    def interrupted_wait(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if not seen:
            seen.append(self)
            raise KeyboardInterrupt
        return original_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt):
        real.run(["sleep", "30"], timeout_s=10)
    proc = seen[0]
    assert original_wait(proc, 3) == -9, "the child must be killed when the caller is interrupted"


def test_falls_back_to_killing_the_child_when_killpg_is_refused(
    real: CommandRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(runner_module.os, "killpg", refuse)
    started = time.monotonic()
    result = real.run(["sleep", "30"], timeout_s=0.2)
    assert result.timed_out
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("cwd", ["a\x00b", __file__])
def test_cwd_must_be_a_real_directory(real: CommandRunner, cwd: str) -> None:
    with pytest.raises(AXPilotError) as info:
        real.run(["true"], cwd=cwd)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_fake_rejects_non_string_prefix_elements() -> None:
    with pytest.raises(AXPilotError):
        FakeRunner().register(["ps", 1])  # type: ignore[list-item]


def test_ensure_success_uses_the_first_non_blank_stderr_line() -> None:
    result = CommandResult(["mdfind"], 1, "", "\n   \nreal problem\nmore detail\n")
    with pytest.raises(AXPilotError) as info:
        ensure_success(result, what="Spotlight search")
    assert info.value.message == "Spotlight search failed (exit 1): real problem"
