"""The stdio bridge: what an MCP client actually runs (``axpilot stdio``).

It connects to the host's Unix socket, sends the hello, waits for the reply (the host may be
asking the user to pair this client, so up to :data:`REPLY_TIMEOUT_S`), then copies bytes in
both directions: the client's stdin to the socket and the socket to stdout. When stdin ends,
the bridge half-closes the socket and keeps forwarding until the host closes it; when the host
closes, the bridge ends. Nothing but the host's bytes is ever written to stdout (errors and
logs go to stderr), so the client sees a clean JSON-RPC stream.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio

from axpilot.bridge.protocol import Hello, LineReader, parse_reply
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.context import CONFIRM_TIMEOUT_MAX_S

__all__ = ["CHUNK_BYTES", "REPLY_TIMEOUT_S", "fd_reader", "fd_writer", "run_bridge"]

_log = logging.getLogger(__name__)

CHUNK_BYTES = 65536
REPLY_TIMEOUT_S = CONFIRM_TIMEOUT_MAX_S + 15
"""How long the bridge waits for the host's reply (pairing may ask the user first)."""

Read = Callable[[], Awaitable[bytes]]
"""Returns the next chunk of input; ``b""`` at the end."""
Write = Callable[[bytes], Awaitable[None]]


def fd_reader(fd: int = 0) -> Read:
    """Reads ``fd`` in a worker thread that is abandoned on cancellation (a read blocked on an
    open stdin must not keep the bridge alive after the host went away)."""

    async def read() -> bytes:
        return await anyio.to_thread.run_sync(os.read, fd, CHUNK_BYTES, abandon_on_cancel=True)

    return read


def fd_writer(fd: int = 1) -> Write:
    """Writes everything to ``fd`` (in a worker thread)."""

    def write_all(data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]

    async def write(data: bytes) -> None:
        await anyio.to_thread.run_sync(write_all, data)

    return write


async def run_bridge(
    socket_path: Path,
    hello: Hello,
    *,
    read_input: Read | None = None,
    write_output: Write | None = None,
    reply_timeout_s: float = REPLY_TIMEOUT_S,
) -> int:
    """Bridge stdin/stdout (or ``read_input``/``write_output``) to the host. Returns ``0`` when
    the host closes the session.

    Raises ``HOST_UNAVAILABLE`` when the host cannot be reached or does not answer, the host's
    own error when it refuses the client (``USER_DENIED``, ``BUSY``, ``NOT_SUPPORTED``…), and
    ``INVALID_ARGUMENT`` for a malformed reply."""
    read_input = read_input or fd_reader(0)
    write_output = write_output or fd_writer(1)
    try:
        conn = await anyio.connect_unix(socket_path)
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"Cannot connect to the AXPilot host at {socket_path}: {exc.strerror or exc}.",
            hint="Start AXPilot (open AXPilot.app), or run `axpilot doctor`.",
        ) from exc
    async with conn:
        await conn.send(hello.to_line())
        reader = LineReader(conn)
        line: bytes | None = None
        with anyio.move_on_after(reply_timeout_s) as waiting:
            line = await reader.readline()
        if line is None:
            why = "did not answer in time" if waiting.cancelled_caught else "closed the connection"
            raise AXPilotError(
                ErrorCode.HOST_UNAVAILABLE,
                f"The AXPilot host {why}.",
                hint="Run `axpilot doctor`, or restart AXPilot.",
            )
        reply = parse_reply(line)
        if not reply.ok:
            raise reply.to_error()

        async def upstream() -> None:
            try:
                while chunk := await read_input():
                    await conn.send(chunk)
                await conn.send_eof()
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError) as exc:
                _log.debug("The host stopped reading: %r", exc)  # downstream sees the end and finishes

        async with anyio.create_task_group() as tg:
            tg.start_soon(upstream)
            buffered = reader.buffered
            while True:
                try:
                    chunk = await buffered.receive(CHUNK_BYTES)
                except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                    break
                await write_output(chunk)
            tg.cancel_scope.cancel()
    return 0
