"""The guard: every error path of the wrapper that all tools run inside (QUALITY.md A1-A7, B2)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import anyio
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.guard import CONTEXT_PARAM, guard_tool
from axpilot.server.registry import ToolSpec
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import make_test_context

pytestmark = pytest.mark.anyio

INCIDENT = re.compile(r"\berr_[0-9a-f]{8}\b")


class EchoModel(BaseModel):
    """Output of the test tool."""

    echoed: str


def ok(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content={"echoed": text})


def make_spec(name: str, *, input_action: bool = False, destructive: bool = False, **extra: Any) -> ToolSpec:
    toolsets = {"click": "input", "shell": "scripting", "status": "observe", "app": "apps"}
    return ToolSpec(
        name=name,
        toolset=toolsets[name],
        factory=lambda ctx: None,  # type: ignore[arg-type,return-value]
        title=f"Test {name}",
        description=f"Test tool {name}.",
        read_only=not (input_action or destructive),
        destructive=destructive,
        input_action=input_action,
        **extra,
    )


Body = Callable[[str], Awaitable[CallToolResult]]


@asynccontextmanager
async def serve(ctx: ServerContext, spec: ToolSpec, body: Body) -> AsyncIterator[Client]:
    """A one-tool MCP server whose tool runs ``body`` inside the guard."""

    async def tool_fn(
        text: Annotated[str, Field(description="Text to echo.")] = "hi",
    ) -> Annotated[CallToolResult, EchoModel]:
        return await body(text)

    server = MCPServer(name="guard-test")
    server.add_tool(guard_tool(spec, ctx, tool_fn), name=spec.name, annotations=spec.annotations())
    async with Client(server) as client:
        yield client


async def echo(text: str) -> CallToolResult:
    return ok(text)


def audit_lines(ctx: ServerContext) -> list[dict[str, Any]]:
    return ctx.audit.tail(100)


# --------------------------------------------------------------------------- success and errors
async def test_success_returns_the_result_and_audits_it() -> None:
    ctx = make_test_context()
    async with serve(ctx, make_spec("status"), echo) as client:
        result = await client.call_tool("status", {"text": "hello"})
    assert not result.is_error
    assert result.structured_content == {"echoed": "hello"}
    (event,) = audit_lines(ctx)
    assert event["tool"] == "status"
    assert event["ok"] is True
    assert event["error_code"] is None


async def test_axpilot_errors_become_error_results_with_code_hint_and_meta() -> None:
    ctx = make_test_context()

    async def fail(_: str) -> CallToolResult:
        raise AXPilotError(ErrorCode.APP_NOT_FOUND, "No running app matches 'Nopes'.", hint="Use list_apps.")

    async with serve(ctx, make_spec("status"), fail) as client:
        result = await client.call_tool("status", {})
    assert result.is_error
    assert error_code_of(result) == "APP_NOT_FOUND"
    text = text_of(result)
    assert text.startswith("APP_NOT_FOUND: No running app matches 'Nopes'.")
    assert "Hint: Use list_apps." in text
    assert result.meta is not None
    assert result.meta["axpilot/error"]["code"] == "APP_NOT_FOUND"
    assert audit_lines(ctx)[-1]["error_code"] == "APP_NOT_FOUND"


async def test_unexpected_exceptions_become_internal_with_an_incident_id_and_no_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_test_context()
    secret = "token=hunter2 /Users/someone/private/path.txt"

    async def crash(_: str) -> CallToolResult:
        raise RuntimeError(secret)

    with caplog.at_level(logging.ERROR, logger="axpilot"):
        async with serve(ctx, make_spec("status"), crash) as client:
            result = await client.call_tool("status", {})
            follow_up = await client.call_tool("status", {})  # the server survives
    text = text_of(result)
    assert error_code_of(result) == "INTERNAL"
    incident = INCIDENT.search(text)
    assert incident is not None
    assert secret not in text
    assert "Traceback" not in text
    assert "RuntimeError" not in text
    assert result.meta is not None
    assert result.meta["axpilot/error"]["details"]["incident_id"] == incident.group(0)
    assert any(incident.group(0) in record.getMessage() and record.exc_info for record in caplog.records)
    assert error_code_of(follow_up) == "INTERNAL"
    assert audit_lines(ctx)[0]["summary"] == f"incident {incident.group(0)}"


async def test_an_exception_group_with_one_axpilot_error_keeps_its_code() -> None:
    ctx = make_test_context()

    async def grouped(_: str) -> CallToolResult:
        raise ExceptionGroup("task group", [AXPilotError(ErrorCode.STALE_REF, "e3 is gone.")])

    async with serve(ctx, make_spec("status"), grouped) as client:
        result = await client.call_tool("status", {})
    assert error_code_of(result) == "STALE_REF"


@pytest.mark.parametrize(
    ("returned", "why"),
    [
        ("not a result", "wrong type"),
        (
            CallToolResult(content=[TextContent(type="text", text="x")], structured_content={"wrong": 1}),
            "schema",
        ),
    ],
)
async def test_invalid_tool_results_become_internal_errors(returned: Any, why: str) -> None:
    ctx = make_test_context()

    async def bad(_: str) -> Any:
        return returned

    async with serve(ctx, make_spec("status"), bad) as client:
        result = await client.call_tool("status", {})
    assert error_code_of(result) == "INTERNAL", why


async def test_error_results_returned_by_a_tool_skip_output_validation() -> None:
    ctx = make_test_context()

    async def soft_fail(_: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text="nope")], is_error=True)

    async with serve(ctx, make_spec("status"), soft_fail) as client:
        result = await client.call_tool("status", {})
    assert result.is_error
    assert text_of(result) == "nope"


# --------------------------------------------------------------------------- enabled / rate limit
async def test_disabled_tools_are_refused() -> None:
    ctx = make_test_context({"tools.profile": "minimal"})
    called = False

    async def body(text: str) -> CallToolResult:
        nonlocal called
        called = True
        return ok(text)

    async with serve(ctx, make_spec("shell", destructive=True), body) as client:
        result = await client.call_tool("shell", {})
    assert error_code_of(result) == "TOOL_DISABLED"
    assert not called


async def test_input_tools_are_rate_limited_but_reads_are_not() -> None:
    ctx = make_test_context({"safety.max_actions_per_minute": 2})
    async with serve(ctx, make_spec("click", input_action=True), echo) as client:
        codes = [error_code_of(await client.call_tool("click", {})) for _ in range(3)]
    assert codes == [None, None, "RATE_LIMITED"]
    async with serve(ctx, make_spec("status"), echo) as client:
        reads = [error_code_of(await client.call_tool("status", {})) for _ in range(5)]
    assert reads == [None] * 5


# --------------------------------------------------------------------------- confirmations
async def test_destructive_tools_need_approval_and_a_no_stops_them() -> None:
    ctx = make_test_context({"tools.profile": "full"}, confirm=False)
    called = False

    async def body(text: str) -> CallToolResult:
        nonlocal called
        called = True
        return ok(text)

    async with serve(ctx, make_spec("shell", destructive=True), body) as client:
        result = await client.call_tool("shell", {"text": "rm -rf ~/x"})
    assert error_code_of(result) == "USER_DENIED"
    assert not called
    assert audit_lines(ctx)[-1]["error_code"] == "USER_DENIED"


async def test_approved_destructive_calls_run_and_the_request_shows_the_arguments() -> None:
    seen: list[ConfirmRequest] = []

    class Recorder:
        async def confirm(self, request: ConfirmRequest) -> bool:
            seen.append(request)
            return True

    ctx = make_test_context({"tools.profile": "full"}, confirm=Recorder())
    async with serve(ctx, make_spec("shell", destructive=True), echo) as client:
        result = await client.call_tool("shell", {"text": "ls -la"})
    assert not result.is_error
    (request,) = seen
    assert request.tool == "shell"
    assert request.detail is not None
    assert json.loads(request.detail) == {"text": "ls -la"}


async def test_custom_confirmation_text_is_used() -> None:
    seen: list[ConfirmRequest] = []

    class Recorder:
        async def confirm(self, request: ConfirmRequest) -> bool:
            seen.append(request)
            return True

    ctx = make_test_context({"tools.profile": "full"}, confirm=Recorder())
    spec = make_spec("shell", destructive=True, confirm_text=lambda args: (f"Run: {args['text']}", None))
    async with serve(ctx, spec, echo) as client:
        await client.call_tool("shell", {"text": "echo hi"})
    assert seen[0].summary == "Run: echo hi"


async def test_confirm_never_skips_the_question() -> None:
    ctx = make_test_context({"tools.profile": "full", "safety.confirm": "never"}, confirm=False)
    async with serve(ctx, make_spec("shell", destructive=True), echo) as client:
        result = await client.call_tool("shell", {})
    assert not result.is_error


async def test_an_unanswered_confirmation_times_out_as_user_denied() -> None:
    class Silent:
        async def confirm(self, request: ConfirmRequest) -> bool:
            await anyio.sleep(30)
            return True

    ctx = make_test_context({"tools.profile": "full"}, confirm=Silent())
    ctx.confirm_timeout_s = 0.05
    async with serve(ctx, make_spec("shell", destructive=True), echo) as client:
        result = await client.call_tool("shell", {})
    assert error_code_of(result) == "USER_DENIED"
    assert result.meta is not None
    assert result.meta["axpilot/error"]["details"]["timed_out"] is True


# --------------------------------------------------------------------------- input turn
async def test_paused_by_the_kill_switch() -> None:
    ctx = make_test_context()
    ctx.control.pause("kill switch")
    async with serve(ctx, make_spec("click", input_action=True), echo) as client:
        result = await client.call_tool("click", {})
    assert error_code_of(result) == "PAUSED_BY_USER"


async def test_input_yields_while_the_user_is_using_the_mouse_or_keyboard() -> None:
    ctx = make_test_context()
    ctx.engine.hid_idle = 0.1  # type: ignore[attr-defined]
    async with serve(ctx, make_spec("click", input_action=True), echo) as client:
        result = await client.call_tool("click", {})
    assert error_code_of(result) == "USER_ACTIVE"


async def test_reads_are_not_blocked_by_an_active_user() -> None:
    ctx = make_test_context()
    ctx.engine.hid_idle = 0.1  # type: ignore[attr-defined]
    async with serve(ctx, make_spec("status"), echo) as client:
        result = await client.call_tool("status", {})
    assert not result.is_error


async def test_a_second_input_action_gets_busy_while_the_first_holds_the_lock() -> None:
    ctx = make_test_context()
    ctx.input_lock_timeout_s = 0.05
    started, release = anyio.Event(), anyio.Event()

    async def slow(text: str) -> CallToolResult:
        if text == "first":
            started.set()
            await release.wait()
        return ok(text)

    async with serve(ctx, make_spec("click", input_action=True), slow) as client:
        results: dict[str, CallToolResult] = {}

        async def first() -> None:
            results["first"] = await client.call_tool("click", {"text": "first"})

        async with anyio.create_task_group() as tg:
            tg.start_soon(first)
            await started.wait()
            results["second"] = await client.call_tool("click", {"text": "second"})
            release.set()
    assert error_code_of(results["second"]) == "BUSY"
    assert not results["first"].is_error


async def test_cancelling_a_call_releases_the_input_lock_and_is_audited() -> None:
    ctx = make_test_context()
    spec = make_spec("click", input_action=True)
    started = anyio.Event()

    async def forever(text: str) -> CallToolResult:
        if text == "again":  # the follow-up call after the cancellation returns at once
            return ok(text)
        started.set()
        await anyio.sleep(30)
        return ok(text)

    async def tool_fn(text: str = "x") -> Annotated[CallToolResult, EchoModel]:
        return await forever(text)

    guarded = guard_tool(spec, ctx, tool_fn)
    async with anyio.create_task_group() as tg:
        tg.start_soon(guarded)
        await started.wait()
        assert ctx.control.busy
        tg.cancel_scope.cancel()
    assert not ctx.control.busy
    assert audit_lines(ctx)[-1]["error_code"] == "CANCELLED"
    result = await guarded(text="again")
    assert not result.is_error


# --------------------------------------------------------------------------- engine deadlines
async def test_engine_calls_past_their_deadline_time_out() -> None:
    ctx = make_test_context()
    gate = anyio.Event()

    def hang() -> None:
        anyio.from_thread.run(gate.wait)

    with pytest.raises(AXPilotError) as caught:
        await ctx.run_engine(hang, timeout_s=0.05)
    gate.set()
    assert caught.value.code is ErrorCode.TIMEOUT
    assert caught.value.details["timeout_s"] == 0.05


async def test_engine_results_and_keyword_arguments_pass_through() -> None:
    ctx = make_test_context()

    def add(a: int, b: int = 0) -> int:
        return a + b

    assert await ctx.run_engine(add, 2, b=3) == 5


# --------------------------------------------------------------------------- wrapper shape
def test_tools_may_not_declare_the_reserved_context_parameter() -> None:
    ctx = make_test_context()

    async def tool_fn(mcp_context: str = "") -> CallToolResult:
        return ok(mcp_context)

    with pytest.raises(ValueError, match=CONTEXT_PARAM):
        guard_tool(make_spec("status"), ctx, tool_fn)


async def test_the_wrapper_keeps_the_input_and_output_schema() -> None:
    ctx = make_test_context()
    async with serve(ctx, make_spec("status"), echo) as client:
        (listed,) = (await client.list_tools()).tools
    assert set(listed.input_schema["properties"]) == {"text"}
    assert listed.input_schema["properties"]["text"]["description"] == "Text to echo."
    assert listed.output_schema is not None
    assert set(listed.output_schema["properties"]) == {"echoed"}
    assert listed.annotations is not None
    assert listed.annotations.read_only_hint is True
