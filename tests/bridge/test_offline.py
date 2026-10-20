"""The stand-in server ``axpilot stdio`` runs when the host is unavailable
(:mod:`axpilot.bridge.offline`): the handshake completes, the usual tools are listed, and every
tool call returns the error with its hint."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from axpilot.bridge.offline import STAND_IN_ERRORS, UnavailableServer, build_unavailable_server
from axpilot.config import Config, load_config
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.app import build_server
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import make_test_context

pytestmark = pytest.mark.anyio

SETUP = AXPilotError(
    ErrorCode.HOST_UNAVAILABLE,
    "AXPilot.app is not installed at /Applications/AXPilot.app.",
    hint="Run `axpilot setup` once (or use `axpilot stdio --direct`).",
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return load_config(tmp_path / "config.toml", env={}, overrides={})


@pytest.mark.parametrize(("profile", "naming"), [(None, None), ("minimal", None), ("full", "windows-mcp")])
async def test_the_stand_in_lists_what_the_host_would(
    config: Config, profile: str | None, naming: str | None
) -> None:
    stand_in = build_unavailable_server(SETUP, config, profile=profile, naming=naming)
    real = build_server(make_test_context(), profile=profile or "standard", naming=naming)
    listed = {tool.name: tool for tool in await stand_in.list_tools()}
    expected = {tool.name: tool for tool in await real.list_tools()}
    assert listed.keys() == expected.keys()
    for name, tool in listed.items():
        assert tool.input_schema == expected[name].input_schema, name
        assert tool.description == expected[name].description, name


async def test_every_tool_call_returns_the_error_and_its_hint(config: Config) -> None:
    stand_in = build_unavailable_server(SETUP, config)
    assert isinstance(stand_in, UnavailableServer)
    async with Client(stand_in) as client:
        assert client.instructions is not None
        assert "HOST_UNAVAILABLE" in client.instructions
        assert "axpilot setup" in client.instructions
        tools = (await client.list_tools()).tools
        assert len(tools) > 20
        for tool in tools:
            result = await client.call_tool(tool.name, {})
            assert result.is_error, tool.name
            assert error_code_of(result) == "HOST_UNAVAILABLE", tool.name
            assert "Run `axpilot setup` once" in text_of(result)
        unknown = await client.call_tool("no_such_tool", {})
        assert error_code_of(unknown) == "INVALID_ARGUMENT"
        disabled = await client.call_tool("shell", {"command": "ls"})  # not in the standard profile
        assert error_code_of(disabled) == "TOOL_DISABLED"
    assert stand_in.axpilot_context.audit.tail() == []  # nothing happened, nothing is audited
    engine = stand_in.axpilot_context.engine
    assert getattr(engine, "events", None) == []  # the engine is never called


def test_an_unknown_profile_is_still_an_error(config: Config) -> None:
    with pytest.raises(AXPilotError) as caught:
        build_unavailable_server(SETUP, config, profile="everything")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_which_errors_get_a_stand_in() -> None:
    assert ErrorCode.HOST_UNAVAILABLE in STAND_IN_ERRORS
    assert ErrorCode.USER_DENIED in STAND_IN_ERRORS  # the user declined pairing this client
    assert ErrorCode.INVALID_ARGUMENT not in STAND_IN_ERRORS  # a bad --profile still exits (code 2)
