"""Serve one MCP session over a pair of byte streams (the host's accepted Unix socket).

This does for a socket what the SDK's ``stdio_server`` does for stdin/stdout: newline-delimited
JSON-RPC in both directions, parsed into ``SessionMessage`` objects on memory streams that the
low-level MCP server runs on. A line that is not valid JSON-RPC is answered with a JSON-RPC parse
error (``-32700``, ``id`` null) and the session goes on (the SDK itself would drop it silently); a
line longer than :data:`~axpilot.bridge.protocol.MAX_LINE_BYTES` ends the session, since the
stream cannot be resynchronised.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
import mcp_types as types
from anyio.abc import ByteSendStream
from mcp.server import MCPServer
from mcp.shared.message import SessionMessage

from axpilot.bridge.protocol import LineReader
from axpilot.errors import AXPilotError

__all__ = ["run_lowlevel", "serve_session"]

_log = logging.getLogger(__name__)


async def run_lowlevel(server: MCPServer, read_stream: Any, write_stream: Any) -> None:
    """Run ``server`` on already-parsed message streams until the read side closes.

    The only place AXPilot touches the SDK's private ``_lowlevel_server``: ``MCPServer`` offers
    no public way to serve a transport of one's own (its ``run_stdio_async`` does exactly this
    over stdio). A test pins this behaviour, so an SDK change shows up as a failing test."""
    lowlevel = server._lowlevel_server  # private: see the docstring
    await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())


def _parse_error() -> types.JSONRPCError:
    return types.JSONRPCError(
        jsonrpc="2.0", id=None, error=types.ErrorData(code=types.PARSE_ERROR, message="Parse error")
    )


async def serve_session(server: MCPServer, reader: LineReader, send: ByteSendStream) -> None:
    """Serve ``server`` until the peer closes its side (or sends an oversized line).

    ``reader`` yields the peer's lines (it may already hold data read after the hello); replies go
    to ``send``. Returns normally at the end of the session; transport errors end it quietly."""
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async def inbound() -> None:
        async with read_writer:
            while True:
                try:
                    line = await reader.readline()
                except AXPilotError as err:
                    _log.warning("Ending an MCP session: %s", err.message)
                    return
                except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                    return
                if line is None:
                    return
                if not line.strip():
                    continue
                try:
                    message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                except ValueError as exc:  # pydantic's ValidationError is a ValueError
                    _log.debug("Unparsable JSON-RPC line: %s", exc)
                    try:
                        await write_stream.send(SessionMessage(_parse_error()))
                    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                        return
                    continue
                await read_writer.send(SessionMessage(message))

    async def outbound() -> None:
        async with write_reader:
            async for session_message in write_reader:
                data = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
                try:
                    await send.send(data.encode("utf-8") + b"\n")
                except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                    return

    reading = anyio.CancelScope()

    async def read_until_cancelled() -> None:
        with reading:
            await inbound()

    async with anyio.create_task_group() as tg:
        tg.start_soon(read_until_cancelled)
        tg.start_soon(outbound)
        try:
            await run_lowlevel(server, read_stream, write_stream)
        finally:
            # Let the writer flush every reply, then stop reading (the peer may still be open).
            await write_stream.aclose()
            reading.cancel()
