"""An MCP client transport to the host, in this process (no ``axpilot stdio`` subprocess).

:func:`host_transport` connects to the host's socket, sends the hello, checks the reply and
then carries JSON-RPC messages, so ``mcp.Client(host_transport())`` talks to the host exactly
as a bridged MCP client does: same pairing, policy, audit and errors. The automation SDK's
``Desktop.connect()`` and the tests use it.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.shared.message import SessionMessage

from axpilot import paths
from axpilot.bridge.protocol import ClientIdentity, Hello, LineReader, parse_reply
from axpilot.bridge.stdio_pipe import REPLY_TIMEOUT_S
from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["host_transport"]


@contextlib.asynccontextmanager
async def host_transport(
    socket_path: Path | None = None,
    hello: Hello | None = None,
    *,
    reply_timeout_s: float = REPLY_TIMEOUT_S,
) -> AsyncIterator[tuple[Any, Any]]:
    """Yield ``(read_stream, write_stream)`` of MCP messages to the host at ``socket_path``
    (default: AXPilot's socket). ``hello`` defaults to this process as the client.

    Raises ``HOST_UNAVAILABLE`` if the host cannot be reached or does not answer, and the host's
    own error if it refuses the client."""
    path = socket_path or paths.socket_path()
    hello = hello or Hello(client=ClientIdentity(parent_app="AXPilot SDK", pid=os.getpid()))
    try:
        conn = await anyio.connect_unix(path)
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"Cannot connect to the AXPilot host at {path}: {exc.strerror or exc}.",
            hint="Start AXPilot (open AXPilot.app), or use Desktop.direct().",
        ) from exc
    async with conn:
        await conn.send(hello.to_line())
        reader = LineReader(conn)
        line: bytes | None = None
        with anyio.move_on_after(reply_timeout_s):
            line = await reader.readline()
        if line is None:
            raise AXPilotError(ErrorCode.HOST_UNAVAILABLE, "The AXPilot host did not accept the connection.")
        reply = parse_reply(line)
        if not reply.ok:
            raise reply.to_error()

        read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
        write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

        async def inbound() -> None:
            async with read_writer:
                while True:
                    try:
                        received = await reader.readline()
                    except (AXPilotError, anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                        return
                    if received is None:
                        return
                    if not received.strip():
                        continue
                    try:
                        message = types.jsonrpc_message_adapter.validate_json(received, by_name=False)
                    except Exception as exc:  # handed to the client session, like the stdio transport
                        await read_writer.send(exc)
                        continue
                    await read_writer.send(SessionMessage(message))

        async def outbound() -> None:
            async with write_reader:
                async for session_message in write_reader:
                    data = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    try:
                        await conn.send(data.encode("utf-8") + b"\n")
                    except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                        return

        async with anyio.create_task_group() as tg:
            tg.start_soon(inbound)
            tg.start_soon(outbound)
            try:
                yield read_stream, write_stream
            finally:
                tg.cancel_scope.cancel()
