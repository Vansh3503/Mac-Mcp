"""Streamable HTTP: the bearer token, Host/Origin checks, loopback-only binding and a working MCP
session behind them."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.host.http import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    MCP_PATH,
    AccessGuard,
    build_http_app,
    check_bind,
    get_token,
    load_or_create_token,
    token_file,
)
from axpilot.system.keychain import Keychain
from axpilot.testing import make_test_context

TOKEN = "t" * 43
PORT = 8765
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
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = build_http_app(
        make_test_context(), token=TOKEN, port=PORT, allowed_origins=["http://localhost:3000"]
    )
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as test_client:
        yield test_client


def post(client: TestClient, payload: Any, **headers: str) -> Any:
    merged = {**MCP_HEADERS, "authorization": f"Bearer {TOKEN}", **headers}
    return client.post(MCP_PATH, content=json.dumps(payload), headers=merged)


def messages(response: Any) -> list[dict[str, Any]]:
    """JSON-RPC messages of a JSON or event-stream response."""
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
        return body if isinstance(body, list) else [body]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


# =========================================================================== the guard
def test_a_valid_request_reaches_the_mcp_server(client: TestClient) -> None:
    response = post(client, INITIALIZE)
    assert response.status_code == 200, response.text
    (result,) = messages(response)
    assert result["result"]["serverInfo"]["name"] == "axpilot"
    assert "access-control-allow-origin" not in response.headers


def test_tools_can_be_listed_over_http(client: TestClient) -> None:
    response = post(client, INITIALIZE)
    session = response.headers.get("mcp-session-id")
    extra = {"mcp-session-id": session} if session else {}
    post(
        client,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        **extra,
        **{"mcp-protocol-version": "2025-06-18"},
    )
    listed = post(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        **extra,
        **{"mcp-protocol-version": "2025-06-18"},
    )
    assert listed.status_code == 200, listed.text
    (result,) = messages(listed)
    names = {tool["name"] for tool in result["result"]["tools"]}
    assert {"snapshot", "status"} <= names


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x", f"bearer {TOKEN[:-1]}"],
)
def test_requests_without_the_token_get_401(client: TestClient, authorization: str | None) -> None:
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers["authorization"] = authorization
    response = client.post(MCP_PATH, content=json.dumps(INITIALIZE), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"] == "unauthorized"


def test_the_token_scheme_is_case_insensitive(client: TestClient) -> None:
    assert post(client, INITIALIZE, authorization=f"bearer {TOKEN}").status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8765", "127.0.0.1:9999", "192.168.1.5:8765", ""])
def test_wrong_host_headers_get_403(client: TestClient, host: str) -> None:
    response = post(client, INITIALIZE, host=host)
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_loopback_host_headers_pass(client: TestClient, host: str) -> None:
    assert post(client, INITIALIZE, host=host).status_code == 200


def test_browser_origins_need_to_be_allowed(client: TestClient) -> None:
    assert post(client, INITIALIZE, origin="https://evil.example").status_code == 403
    assert post(client, INITIALIZE, origin="null").status_code == 403
    assert post(client, INITIALIZE, origin="http://localhost:3000").status_code == 200
    assert post(client, INITIALIZE, origin="http://localhost:3000/").status_code == 200


def test_bad_origin_beats_a_good_token(client: TestClient) -> None:
    response = client.post(
        MCP_PATH, content=json.dumps(INITIALIZE), headers={**MCP_HEADERS, "origin": "https://x.example"}
    )
    assert response.status_code == 403


def test_a_short_token_is_refused() -> None:
    with pytest.raises(AXPilotError):
        AccessGuard(lambda *a: None, token="short", allowed_hosts=["127.0.0.1"])  # type: ignore[arg-type]


# =========================================================================== token
def test_a_token_is_created_privately_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "support" / "http-token"
    first = load_or_create_token(path)
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(path) == first


def test_a_readable_token_file_is_tightened(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "http-token"
    path.write_text("x" * 40, encoding="utf-8")
    os.chmod(path, 0o644)
    assert load_or_create_token(path) == "x" * 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "readable by others" in caplog.text


def test_a_short_token_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "http-token"
    path.write_text("weak", encoding="utf-8")
    assert load_or_create_token(path) != "weak"


def test_the_token_file_lives_in_application_support(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AXPILOT_HOME", str(tmp_path))
    assert token_file() == tmp_path / "http-token"
    assert len(load_or_create_token()) >= 32


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_token_folder_is_reported(tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir()
    os.chmod(folder, 0o500)
    try:
        with pytest.raises(AXPilotError) as caught:
            load_or_create_token(folder / "http-token")
        assert caught.value.code is ErrorCode.COMMAND_FAILED
    finally:
        os.chmod(folder, 0o700)


# =========================================================================== keychain
class MemoryKeychain(Keychain):
    """The keychain as a dict; ``error`` makes every call fail like a locked keychain."""

    def __init__(self, items: dict[tuple[str, str], str] | None = None, error: bool = False) -> None:
        super().__init__()
        self.items = dict(items or {})
        self.error = error
        self.writes = 0

    def _fail(self) -> None:
        if self.error:
            raise AXPilotError(ErrorCode.COMMAND_FAILED, "Reading the keychain failed (exit 51).")

    def read(self, service: str, account: str) -> str | None:
        self._fail()
        return self.items.get((service, account))

    def write(self, service: str, account: str, secret: str, *, label: str = "AXPilot") -> None:
        self._fail()
        self.writes += 1
        self.items[(service, account)] = secret


KEY = (KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)


def test_file_store_and_auto_off_macos_use_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain = MemoryKeychain()
    path = tmp_path / "http-token"
    stored = get_token("file", path=path, keychain=chain)
    assert stored.location == str(path)
    assert path.read_text(encoding="utf-8").strip() == stored.value
    monkeypatch.setattr("sys.platform", "linux")
    assert get_token("auto", path=path, keychain=chain).value == stored.value
    assert chain.items == {}


def test_a_new_token_goes_into_the_keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    chain = MemoryKeychain()
    path = tmp_path / "http-token"
    stored = get_token("auto", path=path, keychain=chain)
    assert len(stored.value) >= 32
    assert chain.items == {KEY: stored.value}
    assert "keychain" in stored.location
    assert KEYCHAIN_SERVICE in stored.location
    assert not path.exists()
    again = get_token("auto", path=path, keychain=chain)
    assert again.value == stored.value
    assert chain.writes == 1


def test_a_file_token_moves_into_the_keychain(tmp_path: Path) -> None:
    path = tmp_path / "http-token"
    path.write_text("A" * 43 + "\n", encoding="utf-8")
    chain = MemoryKeychain()
    stored = get_token("keychain", path=path, keychain=chain)
    assert stored.value == "A" * 43  # configured clients keep working
    assert chain.items == {KEY: "A" * 43}
    assert not path.exists()


def test_a_leftover_file_with_another_token_is_kept(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "http-token"
    path.write_text("B" * 43, encoding="utf-8")
    chain = MemoryKeychain({KEY: "K" * 43})
    assert get_token("keychain", path=path, keychain=chain).value == "K" * 43
    assert path.exists()
    assert "no longer used" in caplog.text
    path.write_text("K" * 43, encoding="utf-8")  # the same token: a migration leftover
    get_token("keychain", path=path, keychain=chain)
    assert not path.exists()


def test_a_short_keychain_token_is_replaced(tmp_path: Path) -> None:
    chain = MemoryKeychain({KEY: "weak"})
    stored = get_token("keychain", path=tmp_path / "http-token", keychain=chain)
    assert stored.value != "weak"
    assert chain.items[KEY] == stored.value


def test_a_locked_keychain_falls_back_to_the_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "http-token"
    stored = get_token("keychain", path=path, keychain=MemoryKeychain(error=True))
    assert stored.location == str(path)
    assert path.read_text(encoding="utf-8").strip() == stored.value
    assert "cannot hold the HTTP token" in caplog.text
    assert stored.value not in caplog.text


def test_a_file_token_the_keychain_cannot_take_stays_in_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "http-token"
    odd = "odd token with spaces and quotes '\" " + "x" * 30
    path.write_text(odd, encoding="utf-8")
    chain = MemoryKeychain()
    stored = get_token("keychain", path=path, keychain=chain)
    assert stored.value == odd.strip()
    assert stored.location == str(path)
    assert chain.items == {}
    assert "cannot move to the keychain" in caplog.text


# =========================================================================== binding
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binds_are_fine(host: str) -> None:
    check_bind(host)


def test_remote_binds_need_permission_and_tls(tmp_path: Path) -> None:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("c", encoding="utf-8")
    key.write_text("k", encoding="utf-8")
    for kwargs in ({}, {"allow_remote": True}):
        with pytest.raises(AXPilotError) as caught:
            check_bind("0.0.0.0", **kwargs)  # type: ignore[arg-type]
        assert caught.value.code is ErrorCode.BLOCKED_BY_POLICY
    check_bind("0.0.0.0", allow_remote=True, tls_cert=cert, tls_key=key)
    with pytest.raises(AXPilotError) as caught:
        check_bind("127.0.0.1", tls_cert=cert)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError) as caught:
        check_bind("127.0.0.1", tls_cert=tmp_path / "missing.pem", tls_key=key)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
