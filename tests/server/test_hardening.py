"""Edge cases for any client or framework: a tool-name prefix, older protocol versions, text that
stands on its own (for clients that ignore structuredContent and images), and bounded results."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client
from mcp_types import TextContent
from starlette.testclient import TestClient

from axpilot.errors import AXPilotError
from axpilot.host.http import MCP_PATH, build_http_app
from axpilot.server.app import build_server
from axpilot.server.instructions import instructions
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import connected_client, make_test_context, structured

TEXT_BUDGET = 60_000
"""Most characters of text any default call may return (clients and models have limits)."""


# =========================================================================== prefix
@pytest.mark.anyio
async def test_a_tool_name_prefix() -> None:
    ctx = make_test_context({"tools.prefix": "mac_"})
    async with connected_client(ctx) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        assert names
        assert all(name.startswith("mac_") for name in names)
        assert "mac_snapshot" in names
        assert structured(await client.call_tool("mac_status", {}))["engine"] == "fake"
        assert error_code_of(await client.call_tool("status", {})) == "INVALID_ARGUMENT"
        assert error_code_of(await client.call_tool("mac_shell", {"command": "ls"})) == "TOOL_DISABLED"
        assert "`mac_snapshot`" in (client.instructions or "")
        assert "`snapshot`" not in (client.instructions or "")
    assert [record["tool"] for record in ctx.audit.tail(5)][-1] == "shell"  # audits keep canonical names


@pytest.mark.anyio
async def test_a_prefix_with_the_windows_mcp_naming() -> None:
    ctx = make_test_context({"tools.prefix": "mac-"})
    async with connected_client(ctx, naming="windows-mcp") as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"mac-Click", "mac-Snapshot"} <= names
        assert "`mac-Snapshot`" in (client.instructions or "")
    assert "`win_Click`" in instructions("windows-mcp", "win_")


@pytest.mark.parametrize("prefix", ["bad prefix", "a" * 25, "émoji"])
def test_invalid_prefixes_are_refused(prefix: str) -> None:
    with pytest.raises(AXPilotError):
        make_test_context({"tools.prefix": prefix})


# =========================================================================== protocol versions
@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_older_and_newer_clients_can_initialize(version: str) -> None:
    token = "t" * 40
    app = build_http_app(make_test_context(), token=token, port=7788)
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "old", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://127.0.0.1:7788") as client:
        response = client.post(MCP_PATH, content=json.dumps(initialize), headers=headers)
    assert response.status_code == 200, response.text
    body = response.text
    payload = json.loads(body) if body.lstrip().startswith("{") else json.loads(body.split("data:", 1)[1])
    negotiated = payload["result"]["protocolVersion"]
    assert negotiated == version or negotiated >= "2025-06-18"


# =========================================================================== self-sufficient text
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("status", {}),
    ("list_apps", {}),
    ("list_windows", {}),
    ("list_displays", {}),
    ("snapshot", {"app": "Notes"}),
    ("snapshot", {"app": "Mail", "mode": "full"}),
    ("explore", {"app": "Notes"}),
    ("find", {"text": "New Note", "app": "Notes"}),
    ("read", {"app": "Mail"}),
    ("inspect", {"ref": "e1"}),
    ("screenshot", {}),
    ("ocr", {"app": "Notes"}),
    ("wait_for", {"condition": "window", "query": "Notes", "timeout_ms": 1000}),
    ("select_menu", {"app": "Notes"}),
    ("clipboard", {}),
    ("handle_dialog", {"app": "Login Demo"}),
]


@pytest.mark.anyio
async def test_text_stands_on_its_own_and_stays_bounded() -> None:
    ctx = make_test_context({"tools.profile": "full"})
    async with Client(build_server(ctx)) as client:
        await client.call_tool("app", {"action": "launch", "name": "Mail"})
        for tool, args in CALLS:
            result = await client.call_tool(tool, args)
            assert error_code_of(result) is None, (tool, text_of(result))
            texts = [block.text for block in result.content if isinstance(block, TextContent)]
            text = "\n".join(texts)
            assert text.strip(), tool
            assert len(text) <= TEXT_BUDGET, (tool, len(text))
            assert len(json.dumps(result.structured_content)) <= 4 * TEXT_BUDGET, tool
            data = structured(result)
            for key in ("snapshot_id", "screenshot_id"):  # ids an agent needs next are in the text too
                if isinstance(data.get(key), str) and tool not in ("find", "read", "handle_dialog"):
                    assert data[key] in text, (tool, key)


@pytest.mark.anyio
async def test_errors_are_self_sufficient_text() -> None:
    async with connected_client(make_test_context()) as client:
        result = await client.call_tool("snapshot", {"app": "NoSuchApp"})
    text = text_of(result)
    assert text.startswith("APP_NOT_FOUND: ")
    assert "Hint:" in text
