"""build_server, the registry and SDK-level failures (unknown tools, invalid arguments)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from mcp import Client
from mcp_types import CallToolResult

from axpilot import __version__
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.profiles import PROFILES, TOOL_ORDER, enabled_tools
from axpilot.server import registry
from axpilot.server.app import NAMINGS, build_server, load_tool_modules
from axpilot.server.instructions import instructions
from axpilot.server.registry import REGISTRY, RegistryError, ToolSpec, register, specs_for, tool
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import connected_client, make_test_context

anyio_mark = pytest.mark.anyio


@pytest.fixture
def clean_registry() -> Iterator[None]:
    """Let a test register specs without leaking them into other tests."""
    load_tool_modules()
    saved = dict(REGISTRY)
    try:
        yield
    finally:
        REGISTRY.clear()
        REGISTRY.update(saved)


def _factory(ctx: object) -> object:  # pragma: no cover - never called
    raise AssertionError


def _other_factory(ctx: object) -> object:  # pragma: no cover - never called
    raise AssertionError


# --------------------------------------------------------------------------- registry
def test_every_registered_tool_is_in_tool_order_and_its_toolset() -> None:
    load_tool_modules()
    assert REGISTRY
    for name, spec in REGISTRY.items():
        assert name == spec.name
        assert name in TOOL_ORDER
        assert spec.description.strip()
        assert spec.title.strip()


def test_read_only_tools_are_not_input_actions_or_destructive() -> None:
    load_tool_modules()
    for spec in REGISTRY.values():
        if spec.read_only:
            assert not spec.destructive, spec.name
            assert not spec.input_action, spec.name


def test_specs_for_follows_tool_order_and_skips_unknown_names() -> None:
    load_tool_modules()
    names = [*list(REGISTRY)[::-1], "no_such_tool"]
    ordered = [spec.name for spec in specs_for(names)]
    assert ordered == sorted(ordered, key=TOOL_ORDER.index)
    assert "no_such_tool" not in ordered


@pytest.mark.usefixtures("clean_registry")
def test_registering_a_name_twice_from_another_factory_fails() -> None:
    existing = REGISTRY["status"]
    with pytest.raises(RegistryError, match="already registered"):
        register(
            ToolSpec(name="status", toolset="observe", factory=_other_factory, title="X", description="Y")  # type: ignore[arg-type]
        )
    register(existing)  # re-registering the same factory (module reload) is fine


@pytest.mark.usefixtures("clean_registry")
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "not_a_tool", "toolset": "observe"}, "not in any toolset"),
        ({"name": "click", "toolset": "observe"}, "belongs to toolset"),
        ({"name": "click", "toolset": "input", "read_only": True, "input_action": True}, "read-only"),
        ({"name": "click", "toolset": "input", "title": " "}, "title and a description"),
    ],
)
def test_invalid_specs_are_rejected(kwargs: dict[str, object], message: str) -> None:
    fields = {"factory": _factory, "title": "T", "description": "D", **kwargs}
    with pytest.raises(RegistryError, match=message):
        register(ToolSpec(**fields))  # type: ignore[arg-type]


@pytest.mark.usefixtures("clean_registry")
def test_the_decorator_fills_the_toolset_from_tool_order() -> None:
    REGISTRY.pop("list_displays", None)

    @tool("list_displays", title="Displays", description="  List displays.  ", read_only=True)
    def factory(ctx: object) -> object:  # pragma: no cover - never called
        raise AssertionError

    spec = REGISTRY["list_displays"]
    assert spec.toolset == "observe"
    assert spec.description == "List displays."
    assert registry.get_spec("list_displays") is spec


# --------------------------------------------------------------------------- build_server
@anyio_mark
@pytest.mark.parametrize("profile", sorted(PROFILES))
async def test_tools_list_is_the_enabled_registered_tools_in_tool_order(profile: str) -> None:
    load_tool_modules()
    ctx = make_test_context({"tools.profile": profile})
    async with connected_client(ctx) as client:
        listed = [t.name for t in (await client.list_tools()).tools]
    expected = [name for name in enabled_tools(profile) if name in REGISTRY]
    assert listed == expected


@anyio_mark
async def test_every_tool_has_annotations_an_output_schema_and_described_parameters() -> None:
    ctx = make_test_context({"tools.profile": "full"})
    async with connected_client(ctx) as client:
        tools = (await client.list_tools()).tools
    assert tools
    for listed in tools:
        spec = REGISTRY[listed.name]
        assert listed.annotations is not None
        assert listed.annotations.read_only_hint is spec.read_only
        assert listed.annotations.destructive_hint is spec.destructive
        assert listed.output_schema is not None, listed.name
        assert listed.description
        assert len(listed.description) > 40
        for param, schema in listed.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{listed.name}.{param} has no description"
        assert "mcp_context" not in listed.input_schema.get("properties", {})


@anyio_mark
async def test_server_identity_and_instructions() -> None:
    ctx = make_test_context()
    server = build_server(ctx)
    assert server.name == "axpilot"
    async with connected_client(ctx) as client:
        assert client.server_info is not None
        assert client.server_info.version == __version__
        assert client.instructions == instructions()
    words = len(instructions().split())
    assert 100 <= words <= 350
    text = instructions().lower()
    for concept in ("snapshot", "ref", "select_menu", "stale_ref", "untrusted"):
        assert concept in text


def test_profile_and_naming_overrides_are_validated() -> None:
    ctx = make_test_context()
    with pytest.raises(AXPilotError) as caught:
        build_server(ctx, profile="nonsense")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError) as caught:
        build_server(ctx, naming="nonsense")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert set(NAMINGS) == {"default", "windows-mcp"}


@anyio_mark
async def test_a_profile_override_does_not_change_the_shared_context() -> None:
    ctx = make_test_context()
    async with connected_client(ctx, profile="minimal") as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "inspect" not in names
    assert ctx.profile == "standard"
    assert "inspect" in ctx.enabled


# --------------------------------------------------------------------------- SDK-level failures
@anyio_mark
async def test_invalid_arguments_become_invalid_argument_with_the_bad_fields() -> None:
    ctx = make_test_context()
    async with connected_client(ctx) as client:
        result = await client.call_tool("wait_for", {"condition": "teleport", "timeout_ms": "soon"})
    assert error_code_of(result) == "INVALID_ARGUMENT"
    assert result.meta is not None
    assert {"condition", "timeout_ms"} <= set(result.meta["axpilot/error"]["details"]["fields"])
    assert ctx.audit.tail(1)[0]["error_code"] == "INVALID_ARGUMENT"


@anyio_mark
async def test_missing_required_arguments_are_reported() -> None:
    ctx = make_test_context()
    async with connected_client(ctx) as client:
        result = await client.call_tool("inspect", {})
    assert error_code_of(result) == "INVALID_ARGUMENT"
    assert "ref" in text_of(result)


@anyio_mark
async def test_unknown_and_disabled_tools_get_clear_errors() -> None:
    ctx = make_test_context({"tools.profile": "minimal"})
    async with connected_client(ctx) as client:
        unknown = await client.call_tool("teleport", {})
        disabled = await client.call_tool("inspect", {"ref": "e1"})
    assert error_code_of(unknown) == "INVALID_ARGUMENT"
    assert "teleport" in text_of(unknown)
    assert error_code_of(disabled) == "TOOL_DISABLED"


@anyio_mark
async def test_a_tool_that_raises_outside_the_guard_is_still_internal() -> None:
    """Even if an exception escaped the guard, the server answers with INTERNAL, not a crash."""

    async def broken() -> CallToolResult:
        raise RuntimeError("boom")

    ctx = make_test_context()
    server = build_server(ctx)
    server.add_tool(broken, name="raw_broken")
    async with Client(server) as client:
        result = await client.call_tool("raw_broken", {})
    assert error_code_of(result) == "INTERNAL"
    assert "boom" not in text_of(result)
