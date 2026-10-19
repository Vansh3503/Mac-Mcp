from __future__ import annotations

import os
import signal

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system import processes
from axpilot.system.processes import (
    MAX_LIMIT,
    PS_ARGV,
    ProcessInfo,
    kill_process,
    list_processes,
    parse_ps_line,
)
from axpilot.system.runner import FakeRunner, truncate_output

PS_OUTPUT = """\
    1     0   0.0  0.1 /sbin/launchd
  812     1  12.5  3.2 /System/Applications/Notes.app/Contents/MacOS/Notes
  901     1  45.0  8.9 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
  902   901   3,5  1,0 /Applications/Google Chrome.app/Contents/Frameworks/Chrome Helper (Renderer)
  950     1   0.0  0.0 (kernel_task)
garbage line
  abc     1   0.0  0.0 /bin/bad-pid
 1200     1   0.0
"""


def test_parse_line_keeps_spaces_in_the_command() -> None:
    info = parse_ps_line(
        "  901     1  45.0  8.9 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    assert info == ProcessInfo(
        901, 1, 45.0, 8.9, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    assert info is not None
    assert info.name == "Google Chrome"


def test_parse_line_accepts_decimal_commas() -> None:
    info = parse_ps_line("902 901 3,5 1,0 /bin/helper")
    assert info is not None
    assert (info.cpu, info.mem) == (3.5, 1.0)


@pytest.mark.parametrize(
    "line", ["", "garbage line", "abc 1 0.0 0.0 /bin/x", "1 2 x.y 0.0 /bin/x", "1200 1 0.0"]
)
def test_parse_line_rejects_malformed_lines(line: str) -> None:
    assert parse_ps_line(line) is None


def test_list_processes_runs_ps_and_sorts_by_cpu(runner: FakeRunner) -> None:
    runner.register(["ps"], stdout=PS_OUTPUT)
    result = list_processes(runner)
    assert runner.last_call.argv == list(PS_ARGV) == ["ps", "-axo", "pid=,ppid=,%cpu=,%mem=,comm="]
    assert [p.pid for p in result] == [901, 812, 902, 1, 950]
    assert result[0].command.endswith("Google Chrome")


def test_list_processes_filters_by_name_case_insensitively(runner: FakeRunner) -> None:
    runner.register(["ps"], stdout=PS_OUTPUT)
    assert [p.pid for p in list_processes(runner, name="chrome")] == [901, 902]
    assert [p.pid for p in list_processes(runner, name="NOTES")] == [812]
    assert list_processes(runner, name="nothing-matches") == []
    assert len(list_processes(runner, name="  ")) == 5


def test_list_processes_limit(runner: FakeRunner) -> None:
    runner.register(["ps"], stdout=PS_OUTPUT)
    assert [p.pid for p in list_processes(runner, limit=2)] == [901, 812]


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1, True, 2.5])
def test_list_processes_invalid_limit(runner: FakeRunner, limit: object) -> None:
    with pytest.raises(AXPilotError) as info:
        list_processes(runner, limit=limit)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_list_processes_drops_a_truncated_last_line(runner: FakeRunner) -> None:
    text, _ = truncate_output("10 1 0.0 0.0 /bin/one\n11 1 0.0 0.0 /bin/two-is-cut", 30)
    runner.register(["ps"], stdout=text)
    assert [p.pid for p in list_processes(runner)] == [10]


def test_list_processes_failure(runner: FakeRunner) -> None:
    runner.register(["ps"], stderr="ps: illegal option\n", exit_code=1)
    with pytest.raises(AXPilotError) as info:
        list_processes(runner)
    assert info.value.code is ErrorCode.COMMAND_FAILED


def test_process_info_to_dict() -> None:
    info = ProcessInfo(5, 1, 0.5, 0.25, "/usr/bin/top")
    assert info.to_dict() == {
        "pid": 5,
        "ppid": 1,
        "cpu": 0.5,
        "mem": 0.25,
        "command": "/usr/bin/top",
        "name": "top",
    }


# ---------------------------------------------------------------------------------------------
# kill_process


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture os.kill calls instead of signalling anything."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


def test_kill_sends_sigterm_by_default(sent: list[tuple[int, int]]) -> None:
    assert kill_process(4242) == "SIGTERM"
    assert sent == [(4242, signal.SIGTERM)]


def test_kill_force_sends_sigkill(sent: list[tuple[int, int]]) -> None:
    assert kill_process(4242, force=True) == "SIGKILL"
    assert sent == [(4242, signal.SIGKILL)]


@pytest.mark.parametrize("pid", [1, 0, -1, -4242])
def test_kill_refuses_launchd_and_process_groups(sent: list[tuple[int, int]], pid: int) -> None:
    with pytest.raises(AXPilotError) as info:
        kill_process(pid)
    assert info.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert sent == []


def test_kill_refuses_our_own_process(sent: list[tuple[int, int]]) -> None:
    with pytest.raises(AXPilotError) as info:
        kill_process(os.getpid(), force=True)
    assert info.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert "AXPilot itself" in info.value.message
    assert sent == []


def test_kill_refuses_our_parent(sent: list[tuple[int, int]]) -> None:
    with pytest.raises(AXPilotError) as info:
        kill_process(os.getppid())
    assert info.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert "parent" in info.value.message
    assert sent == []


@pytest.mark.parametrize("pid", ["123", 12.0, True, None])
def test_kill_rejects_non_integer_pids(sent: list[tuple[int, int]], pid: object) -> None:
    with pytest.raises(AXPilotError) as info:
        kill_process(pid)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert sent == []


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ProcessLookupError(3, "No such process"), ErrorCode.INVALID_ARGUMENT),
        (PermissionError(1, "Operation not permitted"), ErrorCode.COMMAND_FAILED),
        (OSError(22, "Invalid argument"), ErrorCode.COMMAND_FAILED),
    ],
)
def test_kill_maps_os_errors(monkeypatch: pytest.MonkeyPatch, exc: OSError, code: ErrorCode) -> None:
    def fail(pid: int, sig: int) -> None:
        raise exc

    monkeypatch.setattr(processes.os, "kill", fail)
    with pytest.raises(AXPilotError) as info:
        kill_process(4242)
    assert info.value.code is code
    assert "4242" in info.value.message
    assert info.value.__cause__ is exc


def test_list_processes_skips_blank_lines_and_rejects_bad_names(runner: FakeRunner) -> None:
    runner.register(["ps"], stdout="\n  10 1 0.0 0.0 /bin/one\n   \n")
    assert [p.pid for p in list_processes(runner)] == [10]
    with pytest.raises(AXPilotError) as info:
        list_processes(runner, name=5)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
