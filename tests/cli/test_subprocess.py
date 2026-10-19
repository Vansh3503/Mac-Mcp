"""``axpilot`` as real processes: an MCP client talking to ``axpilot stdio --engine fake``, nothing but
JSON-RPC on its stdout, and the full chain client → ``axpilot stdio`` bridge → ``axpilot host``."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters, stdio_client

from axpilot.testing import structured

pytestmark = pytest.mark.anyio

AXPILOT = [sys.executable, "-m", "axpilot"]
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "subprocess-test", "version": "1"},
    },
}


@pytest.fixture
def env(tmp_path: Path, sock_dir: Path) -> dict[str, str]:
    """The child's environment: every AXPilot path in temporary folders."""
    values = {key: value for key, value in os.environ.items() if not key.startswith("AXPILOT_")}
    values.update(
        AXPILOT_HOME=str(tmp_path / "support"),
        AXPILOT_CONFIG_DIR=str(tmp_path / "config"),
        AXPILOT_LOG_DIR=str(tmp_path / "logs"),
        AXPILOT_SOCKET=str(sock_dir / "axpilot.sock"),
        AXPILOT_APP_PATH=str(tmp_path / "AXPilot.app"),
        PYTHONUNBUFFERED="1",
    )
    return values


def jsonrpc(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode("utf-8") + b"\n"


async def test_stdout_carries_only_json_rpc(env: dict[str, str]) -> None:
    requests = [
        INITIALIZE,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "snapshot", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    ]
    output, stderr = bytearray(), bytearray()
    returncode: int | None = None
    with anyio.fail_after(60):
        async with (
            await anyio.open_process(
                [*AXPILOT, "--debug", "stdio", "--engine", "fake"],  # --debug: logs, but on stderr
                env=env,
            ) as process,
            anyio.create_task_group() as tg,
        ):
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            async def drain_stderr() -> None:
                async for chunk in process.stderr:  # type: ignore[union-attr]
                    stderr.extend(chunk)

            tg.start_soon(drain_stderr)
            for request in requests:
                await process.stdin.send(jsonrpc(request))
                # wait for each answer: closing stdin cancels requests still in flight
                while "id" in request and output.count(b"\n") < request["id"]:
                    output.extend(await process.stdout.receive())
            await process.stdin.aclose()
            async for chunk in process.stdout:
                output.extend(chunk)
            returncode = await process.wait()
    assert returncode == 0, stderr.decode()
    replies = [json.loads(line) for line in output.decode("utf-8").splitlines()]  # every line is JSON
    assert all(reply.get("jsonrpc") == "2.0" for reply in replies)
    by_id = {reply["id"]: reply for reply in replies if "id" in reply}
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id[1]["result"]["serverInfo"]["name"] == "axpilot"
    assert by_id[3]["result"]["structuredContent"]["app"]["name"]
    assert by_id[4]["result"]["isError"] is True
    assert b"Traceback" not in stderr


async def test_an_mcp_client_drives_the_fake_desktop(env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "axpilot", "stdio", "--engine", "fake"], env=env
    )
    with anyio.fail_after(60):
        async with Client(stdio_client(params)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {"snapshot", "click", "type_text"} <= names
            snap = structured(await client.call_tool("snapshot", {"app": "Notes"}))
            new_note = next(el["ref"] for el in snap["elements"] if el["name"] == "New Note")
            clicked = await client.call_tool("click", {"ref": new_note})
            assert not clicked.is_error, clicked.content


@pytest.fixture
def host(env: dict[str, str], sock_dir: Path) -> Iterator[subprocess.Popen[bytes]]:
    """``axpilot host --engine fake --yes`` (approves pairing) on the test socket."""
    path = sock_dir / "axpilot.sock"
    process = subprocess.Popen(
        [*AXPILOT, "host", "--engine", "fake", "--yes", "--socket", str(path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = 60.0
        while deadline > 0 and not _accepts(path):
            if process.poll() is not None:
                pytest.fail(f"the host exited: {process.stderr.read().decode() if process.stderr else ''}")
            time.sleep(0.1)
            deadline -= 0.1
        assert _accepts(path), "the host did not start"
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _accepts(path: Path) -> bool:
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


async def test_client_bridge_host_chain(
    env: dict[str, str], host: subprocess.Popen[bytes], sock_dir: Path
) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "axpilot", "stdio"], env=env)
    with anyio.fail_after(60):
        async with Client(stdio_client(params)) as client:
            status = structured(await client.call_tool("status", {}))
            assert status["mode"] == "host"
            assert status["pairing"] == "always"
            snap = structured(await client.call_tool("snapshot", {"app": "Notes"}))
            assert snap["app"]["name"] == "Notes"
        minimal = StdioServerParameters(
            command=sys.executable, args=["-m", "axpilot", "stdio", "--profile", "minimal"], env=env
        )
        async with Client(stdio_client(minimal)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "select_menu" not in names
    host.send_signal(signal.SIGTERM)
    assert host.wait(timeout=20) == 0
    assert not (sock_dir / "axpilot.sock").exists()
    stderr = host.stderr.read().decode() if host.stderr else ""
    assert "Traceback" not in stderr
    assert host.stdout is not None
    assert host.stdout.read() == b""  # the host never writes to stdout


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _post(url: str, payload: dict[str, Any], token: str | None = None) -> tuple[int, str]:
    """POST JSON to ``url`` directly (no proxy); returns the status and the body."""
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_http_serves_mcp_with_the_token(env: dict[str, str], tmp_path: Path) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [*AXPILOT, "http", "--engine", "fake", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    token = ""
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                status, _ = _post(url, INITIALIZE)
                break
            except urllib.error.URLError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("axpilot http did not start")
                time.sleep(0.1)
        assert status == 401
        token = (tmp_path / "support" / "http-token").read_text(encoding="utf-8").strip()
        status, body = _post(url, INITIALIZE, token)
        assert status == 200, body
        assert '"name":"axpilot"' in body.replace(" ", "")
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
    assert code == 0
    stderr = process.stderr.read().decode() if process.stderr else ""
    assert f"http://127.0.0.1:{port}/mcp" in stderr
    assert token not in stderr  # shown only with --show-token
    assert "Traceback" not in stderr
