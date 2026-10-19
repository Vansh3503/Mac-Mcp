"""The tool surface as a contract: TOOL_ORDER and the registry agree, annotations follow the PLAN,
every tool has an output schema, and ``tools/list`` matches the golden files for every profile ×
naming (regenerate them with ``AXPILOT_UPDATE_GOLDEN=1``)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from axpilot.policy.profiles import PROFILES, TOOL_ORDER, TOOLSETS
from axpilot.server.app import NAMINGS, build_server, load_tool_modules
from axpilot.server.registry import REGISTRY
from axpilot.testing import make_test_context

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("AXPILOT_UPDATE_GOLDEN") == "1"

DESTRUCTIVE = {
    "app",
    "processes",
    "shell",
    "applescript",
    "shortcuts",
    "defaults",
    "browser_eval_js",
    "handle_dialog",
}
"""Tools that can destroy work or run code (PLAN §7.2); each asks the user (all or some calls)."""
CONFIRMS_ITSELF = {"shell", "applescript", "shortcuts", "defaults", "browser_eval_js"}


def test_tool_order_and_the_registry_agree() -> None:
    load_tool_modules()
    assert set(REGISTRY) == set(TOOL_ORDER)
    for name, spec in REGISTRY.items():
        assert spec.toolset in TOOLSETS, name
        assert name in TOOLSETS[spec.toolset], name


def test_annotations_follow_the_plan() -> None:
    load_tool_modules()
    for name in TOOLSETS["observe"]:
        spec = REGISTRY[name]
        assert spec.read_only, name
        assert not spec.destructive, name
        assert not spec.input_action, name
    assert {name for name, spec in REGISTRY.items() if spec.destructive} == DESTRUCTIVE
    assert {name for name, spec in REGISTRY.items() if spec.confirms_itself} == CONFIRMS_ITSELF
    for name in TOOLSETS["input"]:
        assert not REGISTRY[name].read_only, name
    assert REGISTRY["browser_read"].read_only
    for name, spec in REGISTRY.items():
        assert not (spec.read_only and spec.destructive), name
        assert spec.title, name
        assert spec.description.strip(), name
        assert "Errors:" in spec.description or spec.read_only, f"{name} should list its errors"


async def listing(profile: str, naming: str, *, enable: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    ctx = make_test_context({"tools.profile": profile, "tools.enable": list(enable)})
    async with Client(build_server(ctx, naming=naming)) as client:
        tools = (await client.list_tools()).tools
    return [
        {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "annotations": tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            if tool.annotations
            else None,
        }
        for tool in tools
    ]


def schema_hash(schema: Any) -> str:
    """Output schemas are large and shared between profiles: the per-profile files keep a hash, and
    ``output_schemas_<naming>.json`` keeps each schema once."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()[:16]


def dumped(data: Any) -> str:
    return json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def check_golden(path: Path, data: Any) -> None:
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumped(data), encoding="utf-8")
        return
    assert path.is_file(), f"{path.name} is missing: run with AXPILOT_UPDATE_GOLDEN=1"
    expected = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        assert [item["name"] for item in data] == [item["name"] for item in expected], (
            f"{path.name}: tools changed"
        )
        pairs = zip(data, expected, strict=True)
    else:
        assert sorted(data) == sorted(expected), f"{path.name}: tools changed"
        pairs = ((data[key], expected[key]) for key in sorted(data))
    for actual, wanted in pairs:
        assert actual == wanted, (
            f"{path.name} differs. If the change is intended, regenerate with AXPILOT_UPDATE_GOLDEN=1 and "
            "review the diff."
        )


@pytest.mark.anyio
@pytest.mark.parametrize("naming", NAMINGS)
@pytest.mark.parametrize("profile", sorted(PROFILES))
async def test_tools_list_matches_the_golden_file(profile: str, naming: str) -> None:
    tools = await listing(profile, naming)
    for tool in tools:
        assert tool["output_schema"], tool["name"]
        assert tool["input_schema"]["type"] == "object", tool["name"]
        tool["output_schema"] = schema_hash(tool["output_schema"])
    check_golden(GOLDEN / f"tools_{profile}_{naming}.json", tools)


@pytest.mark.anyio
@pytest.mark.parametrize("naming", NAMINGS)
async def test_output_schemas_match_the_golden_file(naming: str) -> None:
    tools = await listing("full", naming, enable=("computer",))
    schemas = {tool["name"]: tool["output_schema"] for tool in tools}
    check_golden(GOLDEN / f"output_schemas_{naming}.json", schemas)
    for profile in PROFILES:  # every profile's hashes point at a stored schema
        path = GOLDEN / f"tools_{profile}_{naming}.json"
        if path.is_file():
            hashes = {schema_hash(schema) for schema in schemas.values()}
            for tool in json.loads(path.read_text(encoding="utf-8")):
                assert tool["output_schema"] in hashes, (profile, tool["name"])
