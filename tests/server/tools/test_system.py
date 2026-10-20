"""System tools: clipboard, notify, spotlight_search, system_info, system_control and
processes; FakeRunner failures, confirmations, the error matrix and fault injection."""

from __future__ import annotations

import base64
import io
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp_types import ImageContent
from PIL import Image

from axpilot.engine.fake import FakeEngine
from axpilot.errors import ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.system.runner import FakeRunner
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio

PS_OUTPUT = (
    "    1     0   0.0  0.1 /sbin/launchd\n"
    "  812     1   3.5  1.2 /System/Applications/Notes.app/Contents/MacOS/Notes\n"
    "  990     1   1.0  2.0 /Applications/1Password.app/Contents/MacOS/1Password\n"
    "garbage line\n"
    " 4321   812  12,5  0,3 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
)


class Recorder:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self.answer


def make_ctx(*, engine: Any = None, confirm: Any = True, **overrides: Any) -> ServerContext:
    ctx = make_test_context({"tools.profile": "full", **overrides}, engine=engine, confirm=confirm)

    async def no_sleep(_: float) -> None:
        await anyio.lowlevel.checkpoint()

    ctx.sleep = no_sleep
    return ctx


@pytest.fixture
def ctx() -> ServerContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def runner_of(ctx: ServerContext) -> FakeRunner:
    assert isinstance(ctx.runner, FakeRunner)
    return ctx.runner


def engine_of(ctx: ServerContext) -> FakeEngine:
    engine = getattr(ctx.engine, "inner", ctx.engine)
    assert isinstance(engine, FakeEngine)
    return engine


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args or {})
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


def png(width: int = 4, height: int = 3) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


# =========================================================================== clipboard
async def test_clipboard_text_round_trip(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "clipboard", {"action": "set", "text": "Milk, eggs"})
    assert data["text_length"] == 10
    got = await ok(client, "clipboard", {})
    assert got["text"] == "Milk, eggs"
    assert got["truncated"] is False
    assert engine_of(ctx).clipboard_get().text == "Milk, eggs"


async def test_long_clipboard_text_is_cut(client: Client) -> None:
    await ok(client, "clipboard", {"action": "set", "text": "x" * 25_000})
    result, _ = await call(client, "clipboard", {})
    data = structured(result)
    assert len(data["text"]) == 10_000
    assert data["text_length"] == 25_000
    assert data["truncated"] is True
    assert "first 10000 of 25000" in text_of(result)


async def test_clipboard_image_round_trip(client: Client) -> None:
    encoded = base64.b64encode(png(4, 3)).decode("ascii")
    data = await ok(client, "clipboard", {"action": "set", "image_png_base64": encoded})
    assert data["image_size"] == [4, 3]
    result, error = await call(client, "clipboard", {"include_image": True})
    assert error is None
    got = structured(result)
    assert got["has_image"] is True
    assert got["image_size"] == [4, 3]
    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    without, _ = await call(client, "clipboard", {})
    assert not [block for block in without.content if isinstance(block, ImageContent)]


async def test_clipboard_files(client: Client, tmp_path: Path) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b c.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    await ok(client, "clipboard", {"action": "set", "files": [str(first), str(second)]})
    got = await ok(client, "clipboard", {})
    assert got["files"] == [str(first), str(second)]


async def test_an_empty_clipboard(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).clipboard_set(text="")
    result, error = await call(client, "clipboard", {})
    assert error is None, text_of(result)
    assert text_of(result) == "the clipboard is empty"


@pytest.mark.parametrize(
    "args",
    [
        {"action": "set"},
        {"action": "set", "text": "a", "files": ["/tmp"]},
        {"action": "set", "image_png_base64": "not base64!!"},
        {"action": "set", "image_png_base64": base64.b64encode(b"GIF89a....").decode()},
        {"action": "set", "image_png_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nbroken").decode()},
        {"action": "set", "files": ["relative.txt"]},
        {"action": "set", "files": ["/definitely/not/here.txt"]},
        {"action": "set", "files": []},
        {"action": "get", "text": "x"},
        {"action": "paste"},
    ],
)
async def test_clipboard_error_matrix(client: Client, args: dict[str, Any]) -> None:
    result, error = await call(client, "clipboard", args)
    assert error == "INVALID_ARGUMENT", text_of(result)


async def test_clipboard_text_is_redacted_in_the_audit_log(ctx: ServerContext, client: Client) -> None:
    await ok(client, "clipboard", {"action": "set", "text": "hunter2-secret"})
    record = ctx.audit.tail(1)[0]
    assert "hunter2-secret" not in str(record)


# =========================================================================== notify
async def test_notify_posts_a_notification(ctx: ServerContext, client: Client) -> None:
    await ok(client, "notify", {"title": "Export done", "message": "3 files", "subtitle": "AXPilot"})
    event = engine_of(ctx).events_of("notify")[-1]
    assert event["title"] == "Export done"


@pytest.mark.parametrize(
    "args", [{"title": " ", "message": "x"}, {"title": "x"}, {"title": "x" * 300, "message": "y"}]
)
async def test_notify_error_matrix(client: Client, args: dict[str, Any]) -> None:
    _, error = await call(client, "notify", args)
    assert error == "INVALID_ARGUMENT"


async def test_engine_faults_in_notify() -> None:
    faulty = FaultyEngine(FakeEngine.demo(), {"notify": Fault.crash("notification center crashed")})
    async with connected_client(make_ctx(engine=faulty)) as client:
        result, error = await call(client, "notify", {"title": "a", "message": "b"})
        assert error == "INTERNAL"
        assert "notification center crashed" not in text_of(result)
        faulty.clear_faults()
        await ok(client, "notify", {"title": "a", "message": "b"})


# =========================================================================== spotlight
async def test_spotlight_search_returns_paths(ctx: ServerContext, client: Client, tmp_path: Path) -> None:
    runner_of(ctx).register(["mdfind"], stdout="/Users/alex/Invoice 1.pdf\x00/Users/alex/Invoice 2.pdf\x00")
    args = {"query": "invoice", "kind": "pdf", "scope": str(tmp_path)}
    result, error = await call(client, "spotlight_search", args)
    assert error is None, text_of(result)
    data = structured(result)
    assert data["paths"] == ["/Users/alex/Invoice 1.pdf", "/Users/alex/Invoice 2.pdf"]
    assert data["count"] == 2
    argv = runner_of(ctx).calls[-1].argv
    assert argv[0] == "mdfind"
    assert "-onlyin" in argv
    assert any("com.adobe.pdf" in part for part in argv)


async def test_spotlight_query_is_one_argument(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(["mdfind"], stdout="")
    query = 'a"; rm -rf ~ #'
    data = await ok(client, "spotlight_search", {"query": query})
    assert data["paths"] == []
    assert runner_of(ctx).calls[-1].argv[-1] == query


@pytest.mark.parametrize(
    ("registration", "args", "code"),
    [
        (None, {"query": "x"}, ErrorCode.NOT_SUPPORTED),  # mdfind missing (exit 127)
        ({"exit_code": 1, "stderr": "mds is unhappy"}, {"query": "x"}, ErrorCode.COMMAND_FAILED),
        ({"timed_out": True}, {"query": "x"}, ErrorCode.TIMEOUT),
        ({}, {"query": ""}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"query": "x", "kind": "spaceship"}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"query": "x", "limit": 0}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_spotlight_error_matrix(
    ctx: ServerContext,
    client: Client,
    registration: dict[str, Any] | None,
    args: dict[str, Any],
    code: ErrorCode,
) -> None:
    if registration is not None:
        runner_of(ctx).register(["mdfind"], **registration)
    result, error = await call(client, "spotlight_search", args)
    assert error == code.value, text_of(result)


# =========================================================================== system_info
def register_info(runner: FakeRunner) -> None:
    runner.register(["sw_vers"], stdout="ProductName:\tmacOS\nProductVersion:\t15.4\nBuildVersion:\t24E248\n")
    runner.register(
        ["sysctl"],
        stdout="hw.model: Mac15,3\nhw.memsize: 17179869184\nmachdep.cpu.brand_string: Apple M3\n"
        "hw.ncpu: 8\nkern.boottime: { sec = 1700000000, usec = 0 } Tue Nov 14 22:13:20 2023\n"
        "vm.loadavg: { 1.10 1.20 1.30 }\n",
    )
    runner.register(
        ["pmset"],
        stdout="Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=1)\t80%; discharging; 4:10 remaining present: true\n",
    )
    runner.register(["defaults"], stdout="Dark\n")
    runner.register(
        ["osascript"], stdout="output volume:40, input volume:50, alert volume:100, output muted:false\n"
    )


async def test_system_info(ctx: ServerContext, client: Client) -> None:
    register_info(runner_of(ctx))
    result, error = await call(client, "system_info")
    assert error is None, text_of(result)
    info = structured(result)["info"]
    assert info["os_version"] == "15.4"
    assert info["model"] == "Mac15,3"
    assert info["appearance"] == "dark"
    text = text_of(result)
    assert "macOS 15.4 (24E248)" in text
    assert "battery 80%" in text


async def test_system_info_is_best_effort(client: Client) -> None:
    result, error = await call(client, "system_info")  # every command is missing
    assert error is None
    assert set(structured(result)["info"]["unavailable"]) >= {"os", "hardware"}
    assert "unavailable" in text_of(result)


# =========================================================================== system_control
async def test_volume_mute_and_appearance(ctx: ServerContext, client: Client) -> None:
    runner = runner_of(ctx)
    runner.register(
        ["osascript"], stdout="output volume:40, input volume:50, alert volume:100, output muted:false\n"
    )
    data = await ok(client, "system_control", {"action": "volume", "level": 40})
    assert data["volume"]["output"] == 40
    assert "set volume output volume 40" in (runner.calls[-1].input_text or "")
    await ok(client, "system_control", {"action": "mute"})
    assert "set volume output muted true" in (runner.calls[-1].input_text or "")
    await ok(client, "system_control", {"action": "unmute"})
    assert "output muted false" in (runner.calls[-1].input_text or "")
    data = await ok(client, "system_control", {"action": "appearance", "mode": "dark"})
    assert data["appearance"] == "dark"
    assert "set dark mode to true" in (runner.calls[-1].input_text or "")


@pytest.mark.parametrize(
    ("registration", "args", "code"),
    [
        ({}, {"action": "volume"}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"action": "volume", "level": 101}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"action": "appearance"}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"action": "mute", "level": 3}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"action": "volume", "level": 3, "mode": "dark"}, ErrorCode.INVALID_ARGUMENT),
        (None, {"action": "mute"}, ErrorCode.NOT_SUPPORTED),
        (
            {
                "exit_code": 1,
                "stderr": "execution error: Not authorized to send Apple events to System Events. (-1743)",
            },
            {"action": "appearance", "mode": "light"},
            ErrorCode.PERMISSION_AUTOMATION,
        ),
        ({"timed_out": True}, {"action": "mute"}, ErrorCode.TIMEOUT),
    ],
)
async def test_system_control_error_matrix(
    ctx: ServerContext,
    client: Client,
    registration: dict[str, Any] | None,
    args: dict[str, Any],
    code: ErrorCode,
) -> None:
    if registration is not None:
        runner_of(ctx).register(["osascript"], **registration)
    result, error = await call(client, "system_control", args)
    assert error == code.value, text_of(result)


# =========================================================================== processes
async def test_processes_list_and_filter(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(["ps"], stdout=PS_OUTPUT)
    data = await ok(client, "processes")
    assert [p["pid"] for p in data["processes"]] == [4321, 812, 990, 1]  # busiest first
    chrome = data["processes"][0]
    assert chrome["name"] == "Google Chrome"
    assert chrome["cpu"] == 12.5
    filtered = await ok(client, "processes", {"name": "notes"})
    assert [p["pid"] for p in filtered["processes"]] == [812]
    limited = await ok(client, "processes", {"limit": 1})
    assert limited["count"] == 1


async def test_processes_kill_a_real_child_process_after_asking() -> None:
    recorder = Recorder()
    ctx = make_ctx(confirm=recorder)
    async with await anyio.open_process([sys.executable, "-c", "import time; time.sleep(30)"]) as child:
        try:
            async with connected_client(ctx) as client:
                data = await ok(client, "processes", {"action": "kill", "pid": child.pid})
            assert data["signal"] == "SIGTERM"
            with anyio.fail_after(5):
                assert await child.wait() != 0
            (request,) = recorder.requests
            assert f"process {child.pid}" in request.summary
        finally:
            if child.returncode is None:
                child.kill()


async def test_a_declined_kill_leaves_the_process_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[int] = []
    monkeypatch.setattr("axpilot.system.processes.os.kill", lambda pid, sig: signals.append(sig))
    ctx = make_ctx(confirm=Recorder(answer=False))
    async with connected_client(ctx) as client:
        _, error = await call(client, "processes", {"action": "kill", "pid": 54321})
    assert error == "USER_DENIED"
    assert signals == [0]  # only the existence check


@pytest.mark.parametrize(
    ("pid", "code"),
    [
        (1, "BLOCKED_BY_POLICY"),
        (0, "BLOCKED_BY_POLICY"),
        (-5, "BLOCKED_BY_POLICY"),
        (990, "BLOCKED_BY_POLICY"),  # 1Password's pid in the demo desktop
        (os.getpid(), "BLOCKED_BY_POLICY"),
        (os.getppid(), "BLOCKED_BY_POLICY"),
        (54321, "INVALID_ARGUMENT"),  # no such process
    ],
)
async def test_kill_refusals_ask_nobody(monkeypatch: pytest.MonkeyPatch, pid: int, code: str) -> None:
    signals: list[int] = []

    def fake_kill(target: int, sig: int) -> None:
        signals.append(sig)
        raise ProcessLookupError(target)

    monkeypatch.setattr("axpilot.system.processes.os.kill", fake_kill)
    recorder = Recorder()
    ctx = make_ctx(confirm=recorder)
    async with connected_client(ctx) as client:
        result, error = await call(client, "processes", {"action": "kill", "pid": pid})
    assert error == code, text_of(result)
    assert recorder.requests == []  # refused before anyone is asked
    assert set(signals) <= {0}  # at most an existence check, never a real signal


async def test_a_pid_beyond_pid_t_is_an_invalid_argument() -> None:
    # Found by tests/server/test_fuzz.py: os.kill raised OverflowError, which became INTERNAL.
    async with connected_client(make_ctx()) as client:
        result, error = await call(client, "processes", {"action": "kill", "pid": 2**31})
    assert error == "INVALID_ARGUMENT", text_of(result)


@pytest.mark.parametrize(
    ("registration", "args", "code"),
    [
        (None, {}, ErrorCode.NOT_SUPPORTED),
        ({"exit_code": 1, "stderr": "ps: illegal option"}, {}, ErrorCode.COMMAND_FAILED),
        ({"timed_out": True}, {}, ErrorCode.TIMEOUT),
        ({}, {"action": "kill"}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"action": "list", "pid": 5}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"limit": 0}, ErrorCode.INVALID_ARGUMENT),
        ({}, {"action": "terminate", "pid": 5}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_processes_error_matrix(
    ctx: ServerContext,
    client: Client,
    registration: dict[str, Any] | None,
    args: dict[str, Any],
    code: ErrorCode,
) -> None:
    if registration is not None:
        runner_of(ctx).register(["ps"], **registration)
    result, error = await call(client, "processes", args)
    assert error == code.value, text_of(result)


async def test_listing_processes_asks_nobody() -> None:
    recorder = Recorder()
    ctx = make_ctx(confirm=recorder)
    runner_of(ctx).register(["ps"], stdout=PS_OUTPUT)
    async with connected_client(ctx) as client:
        await ok(client, "processes")
    assert recorder.requests == []


async def test_a_slow_command_does_not_block_other_calls(ctx: ServerContext, client: Client) -> None:
    """Command-line tools run in worker threads: the event loop stays free meanwhile."""

    def slow(call: Any) -> Any:
        time.sleep(0.15)
        from axpilot.system.runner import CommandResult

        return CommandResult(argv=list(call.argv), exit_code=0, stdout="", stderr="")

    runner_of(ctx).register(["mdfind"], handler=slow)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(5):
            await anyio.sleep(0.01)
            ticks += 1

    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        await ok(client, "spotlight_search", {"query": "x"})
    assert ticks == 5
