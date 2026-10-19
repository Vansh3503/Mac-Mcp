"""The stdio bridge: hello and reply, byte forwarding in both directions, and every way the host can
fail to answer."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream

from axpilot.bridge.protocol import ClientIdentity, Hello, LineReader, Reply
from axpilot.bridge.stdio_pipe import fd_reader, fd_writer, run_bridge
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import AutoConfirmer
from axpilot.testing import make_test_context
from axpilot.testing.host import running_host

pytestmark = pytest.mark.anyio

HELLO = Hello(client=ClientIdentity("Claude", "com.anthropic.claudefordesktop", 4242))
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def line(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode("utf-8") + b"\n"


class Pipe:
    """Stands in for stdin and stdout: ``feed`` input, ``close`` it, read ``lines`` of output."""

    def __init__(self) -> None:
        self._send, self._receive = anyio.create_memory_object_stream[bytes](100)
        self.output = bytearray()
        self.new_output = anyio.Event()

    async def feed(self, data: bytes) -> None:
        await self._send.send(data)

    def close(self) -> None:
        self._send.close()

    async def read_input(self) -> bytes:
        try:
            return await self._receive.receive()
        except anyio.EndOfStream:
            return b""

    async def write_output(self, data: bytes) -> None:
        self.output.extend(data)
        self.new_output.set()
        self.new_output = anyio.Event()

    def lines(self) -> list[dict[str, Any]]:
        """The complete lines written so far (a long message arrives in several chunks)."""
        complete = bytes(self.output).rpartition(b"\n")[0]
        return [json.loads(raw) for raw in complete.splitlines() if raw.strip()]

    async def wait_for(self, count: int) -> list[dict[str, Any]]:
        with anyio.fail_after(10):
            while len(self.lines()) < count:
                await self.new_output.wait()
        return self.lines()


@asynccontextmanager
async def fake_host(path: Path, handler: Callable[[SocketStream], Awaitable[None]]) -> AsyncIterator[None]:
    """A Unix socket server whose every connection is handled by ``handler``."""
    listener = await anyio.create_unix_listener(path)

    async def handle(stream: SocketStream) -> None:
        async with stream:
            await handler(stream)

    async with listener, anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, handle)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()


async def bridge(path: Path, pipe: Pipe, **options: Any) -> int:
    return await run_bridge(
        path, HELLO, read_input=pipe.read_input, write_output=pipe.write_output, **options
    )


# =========================================================================== with the real host
async def test_messages_flow_both_ways_and_end_with_stdin(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    pipe = Pipe()
    results: list[int] = []
    initialized: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    async with running_host(make_test_context(), path, pairing_confirmer=AutoConfirmer()):
        async with anyio.create_task_group() as tg:

            async def run() -> None:
                results.append(await bridge(path, pipe))

            tg.start_soon(run)
            await pipe.feed(line(INITIALIZE))
            (initialized,) = await pipe.wait_for(1)
            # Two messages in one chunk, one message split across chunks: framing is the host's job.
            listing = line(LIST_TOOLS)
            await pipe.feed(line(INITIALIZED) + listing[:10])
            await pipe.feed(listing[10:])
            messages = await pipe.wait_for(2)
            pipe.close()  # stdin ends; the host ends the session; the bridge returns
        assert results == [0]
    assert initialized["result"]["serverInfo"]["name"] == "axpilot"
    names = {tool["name"] for tool in messages[1]["result"]["tools"]}
    assert {"snapshot", "click"} <= names
    assert bytes(pipe.output).endswith(b"\n")


async def test_a_denied_client_gets_the_hosts_error(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with running_host(make_test_context(), path, pairing_confirmer=AutoConfirmer(allow=False)):
        pipe = Pipe()
        with pytest.raises(AXPilotError) as caught:
            await bridge(path, pipe)
    assert caught.value.code is ErrorCode.USER_DENIED
    assert pipe.output == b""  # nothing on stdout: errors go to stderr via the CLI


# =========================================================================== host failures
async def test_no_host(sock_dir: Path) -> None:
    with pytest.raises(AXPilotError) as caught:
        await bridge(sock_dir / "missing.sock", Pipe())
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "axpilot doctor" in (caught.value.hint or "")


async def test_a_host_that_never_answers(sock_dir: Path) -> None:
    path = sock_dir / "silent.sock"

    async def silent(stream: SocketStream) -> None:
        await anyio.sleep(30)

    async with fake_host(path, silent):
        with pytest.raises(AXPilotError) as caught:
            await bridge(path, Pipe(), reply_timeout_s=0.1)
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "did not answer in time" in caught.value.message


async def test_a_host_that_hangs_up(sock_dir: Path) -> None:
    path = sock_dir / "rude.sock"

    async def hang_up(stream: SocketStream) -> None:
        await LineReader(stream).readline()

    async with fake_host(path, hang_up):
        with pytest.raises(AXPilotError) as caught:
            await bridge(path, Pipe())
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "closed the connection" in caught.value.message


async def test_a_malformed_reply(sock_dir: Path) -> None:
    path = sock_dir / "odd.sock"

    async def nonsense(stream: SocketStream) -> None:
        await LineReader(stream).readline()
        await stream.send(b'{"hello": "world"}\n')

    async with fake_host(path, nonsense):
        with pytest.raises(AXPilotError) as caught:
            await bridge(path, Pipe())
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


async def test_the_hello_is_sent_first(sock_dir: Path) -> None:
    path = sock_dir / "echo.sock"
    seen: list[bytes] = []

    async def record(stream: SocketStream) -> None:
        seen.append(await LineReader(stream).readline() or b"")
        await stream.send(Reply.accept().to_line())

    pipe = Pipe()
    pipe.close()
    async with fake_host(path, record):
        assert await bridge(path, pipe) == 0
    hello = json.loads(seen[0])
    assert hello["client"]["parent_bundle_id"] == "com.anthropic.claudefordesktop"


async def test_bytes_after_the_reply_are_forwarded(sock_dir: Path) -> None:
    """The host may send its first message right behind the reply, in the same packet."""
    path = sock_dir / "eager.sock"

    async def eager(stream: SocketStream) -> None:
        await LineReader(stream).readline()
        await stream.send(Reply.accept().to_line() + b'{"early": true}\n')

    pipe = Pipe()
    async with fake_host(path, eager):
        assert await bridge(path, pipe) == 0  # the host closes; stdin was never closed
    assert pipe.lines() == [{"early": True}]


async def test_the_bridge_ends_when_the_host_goes_away_mid_session(sock_dir: Path) -> None:
    path = sock_dir / "brief.sock"

    async def brief(stream: SocketStream) -> None:
        reader = LineReader(stream)
        await reader.readline()
        await stream.send(Reply.accept().to_line())
        request = await reader.readline()
        await stream.send((request or b"") + b"\n")  # echo one line, then leave

    pipe = Pipe()
    async with fake_host(path, brief):
        await pipe.feed(b'{"ping": 1}\n')
        with anyio.fail_after(10):
            assert await bridge(path, pipe) == 0
    assert pipe.lines() == [{"ping": 1}]


# =========================================================================== file descriptors
async def test_fd_reader_and_writer() -> None:
    read_fd, write_fd = os.pipe()
    received = bytearray()

    async def write() -> None:
        try:
            await fd_writer(write_fd)(b"x" * 300_000)  # more than a pipe buffer holds at once
        finally:
            os.close(write_fd)

    read = fd_reader(read_fd)
    try:
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                tg.start_soon(write)
                while chunk := await read():
                    received.extend(chunk)
    finally:
        os.close(read_fd)
    assert received == b"x" * 300_000
