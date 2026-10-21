"""The line protocol between the stdio bridge and the host (CONTRACTS §7).

Both directions carry newline-delimited UTF-8 JSON, one message per line, at most
:data:`MAX_LINE_BYTES` per line. The bridge's first line is a *hello*::

    {"axpilot_bridge": 1,
     "client": {"parent_app": "Claude", "parent_bundle_id": "com.anthropic.claudefordesktop", "pid": 4242},
     "profile": null, "naming": null}

The host answers with one *reply* line, ``{"ok": true, "server_version": "0.1.0"}`` or
``{"ok": false, "error": {"code": ..., "message": ..., "hint": ...}}``; after an ``ok`` reply both
directions carry raw MCP JSON-RPC lines.

Version negotiation: the hello names the highest protocol version the bridge speaks. The host
speaks :data:`BRIDGE_PROTOCOL`; a newer bridge gets ``NOT_SUPPORTED`` (update the host), an
invalid version ``INVALID_ARGUMENT``.

Parsing never trusts the peer: wrong types, oversized strings, unknown versions and bad JSON
raise :class:`~axpilot.errors.AXPilotError` with ``INVALID_ARGUMENT``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from anyio import DelimiterNotFound, EndOfStream, IncompleteRead
from anyio.abc import ByteReceiveStream
from anyio.streams.buffered import BufferedByteReceiveStream

from axpilot import __version__
from axpilot.clientnames import client_display_name
from axpilot.errors import AXPilotError, ErrorCode

__all__ = [
    "BRIDGE_PROTOCOL",
    "HELLO_KEY",
    "MAX_LINE_BYTES",
    "ClientIdentity",
    "Hello",
    "LineReader",
    "Reply",
    "parse_hello",
    "parse_reply",
]

BRIDGE_PROTOCOL = 1
"""The bridge protocol version this AXPilot speaks."""
HELLO_KEY = "axpilot_bridge"
MAX_LINE_BYTES = 4 * 1024 * 1024
"""Longest accepted line (hello, reply or JSON-RPC message), in bytes, without the newline."""
_MAX_TEXT = 512
_MAX_SETTING = 64


def _invalid(message: str) -> AXPilotError:
    return AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid bridge message: {message}",
        hint="Make sure the bridge (`axpilot stdio`) and the host are the same AXPilot version.",
    )


def _text(data: dict[str, Any], key: str, *, limit: int = _MAX_TEXT) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"{key} must be a string or null.")
    if len(value) > limit:
        raise _invalid(f"{key} is longer than {limit} characters.")
    return value


def _load(line: bytes | str, what: str) -> dict[str, Any]:
    raw = line.encode("utf-8") if isinstance(line, str) else line
    if len(raw) > MAX_LINE_BYTES:
        raise _invalid(f"the {what} is larger than {MAX_LINE_BYTES} bytes.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"the {what} is not a JSON line ({exc}).") from exc
    if not isinstance(data, dict):
        raise _invalid(f"the {what} must be a JSON object.")
    return data


def _line(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


# --------------------------------------------------------------------------- hello
@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """Who is behind a bridge: the app that started it (best effort) and the bridge's pid."""

    parent_app: str | None = None
    """Display name or path of the app that started the bridge (``"Claude"``)."""
    parent_bundle_id: str | None = None
    pid: int = 0

    @property
    def key(self) -> str:
        """Stable identity for pairing decisions: the bundle id, else the app, else ``"unknown"``."""
        return self.parent_bundle_id or self.parent_app or "unknown"

    @property
    def display(self) -> str:
        """Name to show the user: a known client's readable name (``claude`` is "Claude Code"), else
        the app, else the bundle id (:func:`axpilot.clientnames.client_display_name`)."""
        return client_display_name(self.parent_app) or self.parent_bundle_id or "an unknown app"

    def to_dict(self) -> dict[str, Any]:
        return {"parent_app": self.parent_app, "parent_bundle_id": self.parent_bundle_id, "pid": self.pid}


@dataclass(frozen=True, slots=True)
class Hello:
    """The bridge's first line."""

    client: ClientIdentity
    profile: str | None = None
    naming: str | None = None
    version: int = BRIDGE_PROTOCOL

    def to_line(self) -> bytes:
        return _line(
            {
                HELLO_KEY: self.version,
                "client": self.client.to_dict(),
                "profile": self.profile,
                "naming": self.naming,
            }
        )


def parse_hello(line: bytes | str) -> Hello:
    """Validate a hello line. Raises ``INVALID_ARGUMENT``, or ``NOT_SUPPORTED`` for a bridge that
    speaks a newer protocol than this host."""
    data = _load(line, "hello")
    version = data.get(HELLO_KEY)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _invalid(f"{HELLO_KEY} must be a protocol version (an integer ≥ 1).")
    if version > BRIDGE_PROTOCOL:
        raise AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            f"The bridge speaks protocol {version}, but this AXPilot host ({__version__}) only speaks "
            f"{BRIDGE_PROTOCOL}.",
            hint="Update AXPilot (`axpilot update`) so the bridge and the host match, then restart AXPilot.",
        )
    client = data.get("client")
    if not isinstance(client, dict):
        raise _invalid("client must be an object.")
    pid = client.get("pid", 0)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
        raise _invalid("client.pid must be a non-negative integer.")
    identity = ClientIdentity(
        parent_app=_text(client, "parent_app"), parent_bundle_id=_text(client, "parent_bundle_id"), pid=pid
    )
    return Hello(
        client=identity,
        profile=_text(data, "profile", limit=_MAX_SETTING),
        naming=_text(data, "naming", limit=_MAX_SETTING),
        version=version,
    )


# --------------------------------------------------------------------------- reply
@dataclass(frozen=True, slots=True)
class Reply:
    """The host's answer to a hello."""

    ok: bool
    server_version: str | None = None
    error: dict[str, Any] | None = None
    """``AXPilotError.to_dict()`` when ``ok`` is false."""

    @classmethod
    def accept(cls) -> Reply:
        return cls(ok=True, server_version=__version__)

    @classmethod
    def refuse(cls, err: AXPilotError) -> Reply:
        return cls(ok=False, error=err.to_dict())

    def to_line(self) -> bytes:
        if self.ok:
            return _line({"ok": True, "server_version": self.server_version})
        return _line({"ok": False, "error": self.error or {}})

    def to_error(self) -> AXPilotError:
        """The refusal as an :class:`AXPilotError` (``HOST_UNAVAILABLE`` if the host sent no code)."""
        error = self.error or {}
        try:
            code = ErrorCode(str(error.get("code")))
        except ValueError:
            code = ErrorCode.HOST_UNAVAILABLE
        message = error.get("message")
        hint = error.get("hint")
        return AXPilotError(
            code,
            message if isinstance(message, str) and message else "The AXPilot host refused the connection.",
            hint=hint if isinstance(hint, str) else None,
        )


def parse_reply(line: bytes | str) -> Reply:
    """Validate a reply line. Raises ``INVALID_ARGUMENT``."""
    data = _load(line, "reply")
    ok = data.get("ok")
    if not isinstance(ok, bool):
        raise _invalid("ok must be true or false.")
    if ok:
        return Reply(ok=True, server_version=_text(data, "server_version", limit=_MAX_SETTING))
    error = data.get("error")
    return Reply(ok=False, error=error if isinstance(error, dict) else {})


# --------------------------------------------------------------------------- reading lines
class LineReader:
    """Reads newline-terminated lines of at most :data:`MAX_LINE_BYTES` from a byte stream."""

    def __init__(self, stream: ByteReceiveStream, *, max_bytes: int = MAX_LINE_BYTES) -> None:
        self._buffered = BufferedByteReceiveStream(stream)
        self._max = max_bytes
        self._ended = False

    async def readline(self) -> bytes | None:
        """The next line without its newline; ``None`` at the end of the stream (a last line
        without a newline is returned first). Raises ``INVALID_ARGUMENT`` for a line that is too
        long (the stream cannot be resynchronised after that)."""
        if self._ended:
            return None
        try:
            return await self._buffered.receive_until(b"\n", self._max + 1)
        except DelimiterNotFound as exc:
            raise _invalid(f"a line is longer than {self._max} bytes.") from exc
        except (IncompleteRead, EndOfStream):
            self._ended = True
            rest = self._buffered.buffer
            if rest:
                await self._buffered.receive(len(rest))  # take it out of the buffer
            return rest or None

    @property
    def buffered(self) -> BufferedByteReceiveStream:
        """The underlying buffered stream (data read ahead stays in its buffer)."""
        return self._buffered
