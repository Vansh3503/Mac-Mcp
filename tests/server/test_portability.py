"""Portable tool schemas: every tool, under every naming, advertises an input schema that function
calling APIs accept (OpenAI, Gemini, Anthropic): valid names, flat objects without ``$ref``/``$defs``,
no ``null`` unions, ``additionalProperties: false`` and a description on every parameter. The
server still accepts explicit ``null`` for optional parameters."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from mcp import Client

from axpilot.server.app import NAMINGS, build_server
from axpilot.server.portable import portable_schema
from axpilot.server.results import error_code_of
from axpilot.testing import make_test_context

pytestmark = pytest.mark.anyio

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def walk(node: Any, path: str = "") -> Any:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}/{index}")


async def all_tools(naming: str) -> list[Any]:
    ctx = make_test_context({"tools.profile": "full", "tools.enable": ["computer"]})
    async with Client(build_server(ctx, naming=naming)) as client:
        return list((await client.list_tools()).tools)


@pytest.mark.parametrize("naming", NAMINGS)
async def test_every_input_schema_is_portable(naming: str) -> None:
    for tool in await all_tools(naming):
        schema = tool.input_schema
        assert NAME_RE.match(tool.name), tool.name
        assert schema["type"] == "object", tool.name
        assert schema.get("additionalProperties") is False, tool.name
        text = json.dumps(schema)
        assert "$ref" not in text, tool.name
        assert "$defs" not in text, tool.name
        assert '"null"' not in text, tool.name
        for name, prop in schema.get("properties", {}).items():
            assert NAME_RE.match(name), (tool.name, name)
            assert prop.get("description"), (tool.name, name)
            assert "title" not in prop, (tool.name, name)
            if "anyOf" in prop:  # a real union (e.g. a list or its JSON text): every option is typed
                assert all("type" in option for option in prop["anyOf"]), (tool.name, name)
            else:
                assert "type" in prop, (tool.name, name)
        for path, node in walk(schema):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, (tool.name, path)
            if node.get("type") == "array":
                assert "items" in node, (tool.name, path)
        for required in schema.get("required", []):
            assert required in schema["properties"], (tool.name, required)


async def test_explicit_nulls_are_still_accepted() -> None:
    ctx = make_test_context()
    async with Client(build_server(ctx)) as client:
        result = await client.call_tool("snapshot", {"app": None, "window_id": None, "max_elements": None})
    assert error_code_of(result) is None


def test_portable_schema() -> None:
    item = {"type": "object", "title": "Item", "properties": {"a": {"type": "string", "title": "A"}}}
    name = {"anyOf": [{"type": "string", "maxLength": 5}, {"type": "null"}], "default": None}
    schema = {
        "type": "object",
        "title": "fooArguments",
        "$defs": {"Item": item},
        "properties": {
            "name": {**name, "title": "Name", "description": "A name."},
            "items": {
                "type": "array",
                "items": {"$ref": "#/$defs/Item"},
                "description": "Items.",
                "title": "I",
            },
            "item": {"$ref": "#/$defs/Item", "description": "One item."},
            "either": {
                "anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}],
                "description": "E.",
            },
        },
        "required": ["items"],
    }
    out = portable_schema(schema)
    assert out["additionalProperties"] is False
    assert out["properties"]["name"] == {"type": "string", "maxLength": 5, "description": "A name."}
    assert out["properties"]["items"]["items"] == {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }
    assert out["properties"]["item"]["description"] == "One item."
    assert out["properties"]["either"]["anyOf"] == [{"type": "integer"}, {"type": "string"}]
    assert "$defs" not in out
    assert out["title"] == "fooArguments"
    assert schema["properties"]["name"]["anyOf"]  # the input is not modified
