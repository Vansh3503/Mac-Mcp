"""The bridge line protocol: hello and reply round trips, strict validation, line reading."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from anyio.abc import ByteReceiveStream
from hypothesis import given
from hypothesis import strategies as st

from axpilot import __version__
from axpilot.bridge.protocol import (
    BRIDGE_PROTOCOL,
    HELLO_KEY,
    MAX_LINE_BYTES,
    ClientIdentity,
    Hello,
    LineReader,
    Reply,
    parse_hello,
    parse_reply,
)
from axpilot.errors import AXPilotError, ErrorCode


def hello_dict(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        HELLO_KEY: 1,
        "client": {"parent_app": "Claude", "parent_bundle_id": "com.anthropic.claudefordesktop", "pid": 42},
        "profile": "full",
        "naming": None,
    }
    data.update(changes)
    return data


def line(data: Any) -> bytes:
    return json.dumps(data).encode() + b"\n"


def test_hello_round_trip() -> None:
    hello = Hello(client=ClientIdentity("Cursor", "com.todesktop.230313mzl4w4u92", 7), profile="minimal")
    raw = hello.to_line()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert parse_hello(raw.rstrip(b"\n")) == hello


def test_identity_key_and_display() -> None:
    assert ClientIdentity("Claude", "com.anthropic.claudefordesktop").key == "com.anthropic.claudefordesktop"
    assert ClientIdentity("node", None).key == "node"
    assert ClientIdentity().key == "unknown"
    assert ClientIdentity().display == "an unknown app"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"not json", "not a JSON line"),
        (b"[1, 2]", "JSON object"),
        (b"\xff\xfe", "not a JSON line"),
        (line(hello_dict(**{HELLO_KEY: True})), "protocol version"),
        (line(hello_dict(**{HELLO_KEY: 0})), "protocol version"),
        (line(hello_dict(**{HELLO_KEY: "1"})), "protocol version"),
        (line({"client": {}}), "protocol version"),
        (line(hello_dict(client="Claude")), "client must be an object"),
        (line(hello_dict(client={"pid": -1})), "pid"),
        (line(hello_dict(client={"pid": True})), "pid"),
        (line(hello_dict(client={"parent_app": 5})), "parent_app"),
        (line(hello_dict(client={"parent_app": "x" * 513})), "longer than"),
        (line(hello_dict(profile="p" * 65)), "longer than"),
        (b"{" + b" " * MAX_LINE_BYTES + b"}", "larger than"),
    ],
)
def test_invalid_hellos_are_refused(raw: bytes, fragment: str) -> None:
    with pytest.raises(AXPilotError) as caught:
        parse_hello(raw)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in caught.value.message


def test_a_newer_bridge_is_told_to_update() -> None:
    with pytest.raises(AXPilotError) as caught:
        parse_hello(line(hello_dict(**{HELLO_KEY: BRIDGE_PROTOCOL + 1})))
    assert caught.value.code is ErrorCode.NOT_SUPPORTED
    assert "axpilot update" in (caught.value.hint or "")


def test_replies_round_trip() -> None:
    accepted = parse_reply(Reply.accept().to_line())
    assert accepted.ok
    assert accepted.server_version == __version__
    refused = parse_reply(Reply.refuse(AXPilotError(ErrorCode.BUSY, "full", hint="later")).to_line())
    assert not refused.ok
    err = refused.to_error()
    assert (err.code, err.message, err.hint) == (ErrorCode.BUSY, "full", "later")


def test_unknown_refusals_become_host_unavailable() -> None:
    reply = parse_reply(line({"ok": False, "error": {"code": "SOMETHING_NEW"}}))
    err = reply.to_error()
    assert err.code is ErrorCode.HOST_UNAVAILABLE
    assert err.message
    assert parse_reply(line({"ok": False, "error": "nope"})).to_error().code is ErrorCode.HOST_UNAVAILABLE


@pytest.mark.parametrize("raw", [line({"ok": "yes"}), line({}), b"garbage"])
def test_invalid_replies(raw: bytes) -> None:
    with pytest.raises(AXPilotError) as caught:
        parse_reply(raw)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


@given(
    app=st.one_of(st.none(), st.text(max_size=512)),
    bundle=st.one_of(st.none(), st.text(max_size=512)),
    pid=st.integers(min_value=0, max_value=2**31),
    profile=st.one_of(st.none(), st.text(max_size=64)),
)
def test_any_valid_hello_survives_the_wire(
    app: str | None, bundle: str | None, pid: int, profile: str | None
) -> None:
    hello = Hello(client=ClientIdentity(app, bundle, pid), profile=profile)
    assert parse_hello(hello.to_line()) == hello


# --------------------------------------------------------------------------- LineReader
class Chunks(ByteReceiveStream):
    """A byte stream that delivers ``chunks`` one by one, then ends."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def receive(self, max_bytes: int = 65536) -> bytes:
        if not self._chunks:
            raise anyio.EndOfStream
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self._chunks.clear()


async def read_all(chunks: list[bytes], *, max_bytes: int = MAX_LINE_BYTES) -> list[bytes | None]:
    reader = LineReader(Chunks(chunks), max_bytes=max_bytes)
    lines: list[bytes | None] = []
    while True:
        got = await reader.readline()
        lines.append(got)
        if got is None:
            break
    lines.append(await reader.readline())  # and it stays at the end
    return lines


@pytest.mark.anyio
async def test_line_reader_joins_chunks_and_keeps_a_last_unterminated_line() -> None:
    lines = await read_all([b'{"a"', b":1}\n{", b'"b":2}\n', b"\n", b"tail"])
    assert lines == [b'{"a":1}', b'{"b":2}', b"", b"tail", None, None]


@pytest.mark.anyio
async def test_line_reader_ends_cleanly() -> None:
    assert await read_all([]) == [None, None]
    assert await read_all([b"one\n"]) == [b"one", None, None]


@pytest.mark.anyio
async def test_line_reader_refuses_oversized_lines() -> None:
    with pytest.raises(AXPilotError) as caught:
        await LineReader(Chunks([b"x" * 20]), max_bytes=10).readline()
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
