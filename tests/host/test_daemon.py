"""The host end to end on Linux: a real Unix socket, the bridge protocol, pairing, limits and
MCP sessions served from the fake engine."""

from __future__ import annotations

import itertools
import json
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from axpilot.bridge.client import host_transport
from axpilot.bridge.protocol import HELLO_KEY, ClientIdentity, Hello, LineReader, parse_reply
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.host import daemon
from axpilot.host.daemon import Host, peer_uid, prepare_socket, serve, socket_is_live
from axpilot.host.pairing import PairingStore
from axpilot.host.session import run_lowlevel
from axpilot.policy.confirm import AutoConfirmer, ConfirmRequest
from axpilot.server.app import build_server
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of
from axpilot.testing import make_test_context, structured
from axpilot.testing.host import running_host

pytestmark = pytest.mark.anyio

CLAUDE = Hello(client=ClientIdentity("Claude", "com.anthropic.claudefordesktop", 4242))


def make_ctx(**overrides: Any) -> ServerContext:
    return make_test_context({"tools.profile": "standard", **overrides})


async def raw_exchange(path: Path, payload: bytes) -> dict[str, Any] | None:
    """Send ``payload`` on a fresh connection and return the host's first reply line (parsed)."""
    async with await anyio.connect_unix(path) as conn:
        await conn.send(payload)
        reader = LineReader(conn)
        with anyio.fail_after(5):
            line = await reader.readline()
    return json.loads(line) if line else None


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def code_of(reply: dict[str, Any] | None) -> str | None:
    return reply["error"]["code"] if reply is not None else None


async def until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    with anyio.fail_after(timeout_s):
        for _ in itertools.count():
            if predicate():
                return
            await anyio.sleep(0.01)


# =========================================================================== sessions
async def test_a_bridged_client_gets_a_full_mcp_session(sock_dir: Path) -> None:
    ctx = make_ctx()
    pairing = AutoConfirmer(allow=True)
    path = sock_dir / "axpilot.sock"
    async with running_host(ctx, path, pairing_confirmer=pairing) as host:
        assert mode_of(path) == 0o600
        assert mode_of(sock_dir) == 0o700
        async with Client(host_transport(path, CLAUDE)) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            assert "snapshot" in names
            assert "click" in names
            status = structured(await client.call_tool("status", {}))
            assert status["mode"] == "host"
            assert status["pairing"] == "always"
            assert status["client"] == "Claude"
            assert [c["app"] for c in status["clients"]] == ["Claude"]
            snap = structured(await client.call_tool("snapshot", {"app": "Notes"}))
            new_note = next(el["ref"] for el in snap["elements"] if el["name"] == "New Note")
            clicked = await client.call_tool("click", {"ref": new_note})
            assert error_code_of(clicked) is None
            assert len(host.sessions) == 1
        await until(lambda: not host.sessions)
    assert [r.tool for r in pairing.requests] == ["pairing"]
    assert "Claude" in pairing.requests[0].summary
    assert not path.exists()  # removed when the host stops


async def test_each_client_gets_its_own_profile_and_naming(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()):
        minimal = Hello(client=CLAUDE.client, profile="minimal")
        async with Client(host_transport(path, minimal)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
        async with Client(host_transport(path, CLAUDE)) as client:
            standard = {tool.name for tool in (await client.list_tools()).tools}
    assert "select_menu" not in names
    assert "select_menu" in standard
    assert names < standard


async def test_concurrent_clients(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    results: list[str] = []
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()) as host:

        async def use(name: str) -> None:
            hello = Hello(client=ClientIdentity(name, f"com.example.{name}", 1))
            async with Client(host_transport(path, hello)) as client:
                data = structured(await client.call_tool("status", {}))
                results.append(data["client"])

        async with anyio.create_task_group() as tg:
            for name in ("one", "two", "three"):
                tg.start_soon(use, name)
        assert sorted(results) == ["one", "three", "two"]
        assert set(host.pairing.entries()[0].to_dict()) >= {"client", "decision"}


async def test_too_many_clients_get_busy(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer(), max_clients=1) as host:
        async with Client(host_transport(path, CLAUDE)):
            with pytest.raises(AXPilotError) as caught:
                async with host_transport(path, CLAUDE):
                    pass
            assert caught.value.code is ErrorCode.BUSY
        await until(lambda: not host.sessions)  # the host frees the slot once it sees the disconnect
        async with Client(host_transport(path, CLAUDE)) as client:
            await client.list_tools()


# =========================================================================== pairing
class Recorder:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        if self.answer is None:
            await anyio.sleep(30)  # nobody answers
        return bool(self.answer)


async def test_an_approved_client_is_remembered(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    recorder = Recorder(True)
    async with running_host(make_ctx(), path, pairing_confirmer=recorder) as host:
        for _ in range(2):
            async with Client(host_transport(path, CLAUDE)) as client:
                await client.list_tools()
    assert len(recorder.requests) == 1
    assert host.pairing.decision("com.anthropic.claudefordesktop") == "always"


async def test_a_denied_client_stays_denied(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    recorder = Recorder(False)
    errors: list[AXPilotError] = []
    async with running_host(make_ctx(), path, pairing_confirmer=recorder) as host:
        for _ in range(2):
            with pytest.raises(AXPilotError) as caught:
                async with host_transport(path, CLAUDE):
                    pass
            errors.append(caught.value)
    assert [e.code for e in errors] == [ErrorCode.USER_DENIED] * 2
    assert len(recorder.requests) == 1  # the second time it is refused without asking
    assert host.pairing.decision("com.anthropic.claudefordesktop") == "denied"
    assert "declined it earlier" in errors[1].message
    assert str(host.pairing.path) in (errors[1].hint or "")


async def test_an_unanswered_pairing_is_not_remembered(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    ctx = make_ctx()
    ctx.confirm_timeout_s = 0.1
    async with running_host(ctx, path, pairing_confirmer=Recorder(None)) as host:
        with pytest.raises(AXPilotError) as caught:
            async with host_transport(path, CLAUDE):
                pass
    assert caught.value.code is ErrorCode.USER_DENIED
    assert "Nobody answered" in caught.value.message
    assert host.pairing.decision("com.anthropic.claudefordesktop") is None


async def test_no_tool_acts_while_a_pairing_question_is_open(sock_dir: Path) -> None:
    """Another, already paired client must not answer the pairing dialog for the user."""
    path = sock_dir / "axpilot.sock"
    ctx = make_ctx()
    seen: list[bool] = []

    class Watching:
        async def confirm(self, request: ConfirmRequest) -> bool:
            seen.append(ctx.control.waiting_for_user)
            return True

    async with running_host(ctx, path, pairing_confirmer=Watching()), host_transport(path, CLAUDE):
        pass
    assert seen == [True]
    assert not ctx.control.waiting_for_user


async def test_pairing_can_be_turned_off(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    recorder = Recorder(False)
    ctx = make_ctx(**{"safety.require_pairing": False})
    async with (
        running_host(ctx, path, pairing_confirmer=recorder),
        Client(host_transport(path, CLAUDE)) as client,
    ):
        status = structured(await client.call_tool("status", {}))
    assert recorder.requests == []
    assert status["pairing"] == "not required"


# =========================================================================== refusals
async def test_bad_hellos_are_answered_with_an_error(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()):
        garbage = await raw_exchange(path, b"hello there\n")
        newer = await raw_exchange(path, json.dumps({HELLO_KEY: 99, "client": {}}).encode() + b"\n")
        profile = await raw_exchange(path, Hello(client=CLAUDE.client, profile="godmode").to_line())
        naming = await raw_exchange(path, Hello(client=CLAUDE.client, naming="klingon").to_line())
    assert code_of(garbage) == "INVALID_ARGUMENT"
    assert code_of(newer) == "NOT_SUPPORTED"
    assert code_of(profile) == "INVALID_ARGUMENT"
    assert code_of(naming) == "INVALID_ARGUMENT"


async def test_a_silent_connection_is_closed(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with (
        running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer(), hello_timeout_s=0.1),
        await anyio.connect_unix(path) as conn,
    ):
        with anyio.fail_after(5):
            assert await LineReader(conn).readline() is None


async def test_other_users_are_refused(sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon, "peer_uid", lambda sock: os.getuid() + 1)
    path = sock_dir / "axpilot.sock"
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()):
        reply = await raw_exchange(path, CLAUDE.to_line())
    assert reply is not None
    assert reply["error"]["code"] == "BLOCKED_BY_POLICY"


async def test_one_broken_connection_does_not_take_the_host_down(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def flaky_build(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return build_server(*args, **kwargs)

    monkeypatch.setattr(daemon, "build_server", flaky_build)
    path = sock_dir / "axpilot.sock"
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()):
        assert await raw_exchange(path, CLAUDE.to_line()) is None  # closed without a reply
        async with Client(host_transport(path, CLAUDE)) as client:
            await client.list_tools()


async def test_invalid_json_inside_a_session_gets_a_json_rpc_error(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with (
        running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()),
        await anyio.connect_unix(path) as conn,
    ):
        await conn.send(CLAUDE.to_line())
        reader = LineReader(conn)
        with anyio.fail_after(5):
            assert parse_reply(await reader.readline() or b"").ok
            await conn.send(b"{this is not json\n")
            answer = json.loads(await reader.readline() or b"{}")
            ping = {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}
            await conn.send(json.dumps(ping).encode() + b"\n")
            pong = json.loads(await reader.readline() or b"{}")
    assert answer == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert pong.get("id") == 7  # the session goes on


# =========================================================================== the socket
def test_peer_uid_is_the_current_user() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid(left) in (os.getuid(), None)
    finally:
        left.close()
        right.close()


def test_peer_uid_survives_a_failing_socket() -> None:
    closed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    closed.close()
    assert peer_uid(closed) is None


def test_a_stale_socket_is_removed(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # the file stays, nobody listens
    assert path.exists()
    assert not socket_is_live(path)
    prepare_socket(path)
    assert not path.exists()


async def test_a_live_host_cannot_be_replaced(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    async with running_host(make_ctx(), path, pairing_confirmer=AutoConfirmer()):
        assert socket_is_live(path)
        with pytest.raises(AXPilotError) as caught:
            await serve(make_ctx(), path)
        assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
        assert "already serving" in caught.value.message


def test_a_file_in_the_way_is_not_deleted(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    path.write_text("precious", encoding="utf-8")
    with pytest.raises(AXPilotError) as caught:
        prepare_socket(path)
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert path.read_text(encoding="utf-8") == "precious"


def test_the_socket_folder_is_created_private_and_tightened(sock_dir: Path) -> None:
    fresh = sock_dir / "new" / "nested"
    prepare_socket(fresh / "axpilot.sock")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700
    loose = sock_dir / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    prepare_socket(loose / "axpilot.sock")
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


async def test_serve_uses_the_pairing_store(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    store = PairingStore(sock_dir / "pairs.json")
    store.remember("com.anthropic.claudefordesktop", "always")
    ctx = make_ctx()
    async with anyio.create_task_group() as tg:
        await tg.start(lambda task_status: serve(ctx, path, pairing=store, task_status=task_status))
        async with Client(host_transport(path, CLAUDE)) as client:
            await client.list_tools()
        tg.cancel_scope.cancel()


async def test_unreachable_host_and_refusals_from_the_transport(sock_dir: Path) -> None:
    with pytest.raises(AXPilotError) as caught:
        async with host_transport(sock_dir / "missing.sock", CLAUDE):
            pass
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE


def test_the_private_sdk_entry_point_still_exists() -> None:
    """run_lowlevel relies on MCPServer._lowlevel_server; this fails if the SDK changes it."""
    server = build_server(make_ctx())
    lowlevel = server._lowlevel_server
    assert callable(lowlevel.run)
    assert lowlevel.create_initialization_options() is not None
    assert callable(run_lowlevel)


async def test_host_class_defaults(sock_dir: Path) -> None:
    host = Host(ctx=make_ctx(), socket_path=sock_dir / "x.sock", pairing=PairingStore(sock_dir / "p.json"))
    assert host.max_clients == daemon.DEFAULT_MAX_CLIENTS
    info = host._info_for(CLAUDE.client)
    assert info == {"pairing": "unpaired", "clients": []}
