"""Tool-argument fuzzing (PLAN §7): every tool, under both namings, is called with arguments
generated from the input schema it advertises, both valid ones and deliberately broken ones
(unknown keys, missing required keys, wrong types, values out of range). Whatever arrives, the
call ends in a successful result or an error with an AXPilot error code: never ``INTERNAL``,
never a traceback, and the input lock is free afterwards.

Nothing leaves the sandbox: the simulated desktop, a ``FakeRunner`` instead of real commands, a
temporary audit log and a virtual clock (waits pass at once, so the full ranges of ``timeout_ms``
and ``duration`` are fuzzed). Every example starts from a fresh desktop and fresh server state.
Confirmations are approved, so destructive paths run too, except stopping a process:
``processes`` is the one tool that signals a real pid, so it is declined, and ``kill_process`` is
patched to fail loudly as a second guard.

Each tool gets ``AXPILOT_FUZZ_EXAMPLES`` examples (default 10, with a fixed seed so CI is
repeatable). Setting the variable also picks a random seed, for deeper runs::

    AXPILOT_FUZZ_EXAMPLES=200 uv run pytest tests/server/test_fuzz.py -q
"""

from __future__ import annotations

import functools
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import anyio
import anyio.lowlevel
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp import Client
from mcp_types import CallToolResult, TextContent

import axpilot.server.tools.system as system_tools
from axpilot.engine.fake import FakeEngine
from axpilot.errors import ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.app import NAMINGS, build_server
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import Fault, FaultyEngine, make_test_context

_DEEP = "AXPILOT_FUZZ_EXAMPLES" in os.environ
EXAMPLES = int(os.environ.get("AXPILOT_FUZZ_EXAMPLES", "10"))
CONFIG = {"tools.profile": "full", "tools.enable": ["computer"]}
STATUS_TOOL = {"default": "status", "windows-mcp": "ControlStatus"}

STATE_FIELDS = (
    "engine",
    "snapshots",
    "captures",
    "limiter",
    "audit",
    "confirmer",
    "control",
    "runner",
    "questions",
    "clock",
    "sleep",
)
"""The :class:`ServerContext` fields that hold state; every example gets fresh ones."""

ERROR_CODES = {code.value for code in ErrorCode}
TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "/')

STRINGS = (
    "",
    " ",
    "e1",
    "e2",
    "e99999",
    "o1",
    "Notes",
    "Finder",
    "Mail",
    "TextEdit",
    "1Password",
    "com.apple.finder",
    "~/Documents",
    "/",
    "../..",
    "/etc/passwd",
    "file:///etc/passwd",
    "https://example.com",
    "javascript:alert(1)",
    "cmd+q",
    "cmd+shift+4",
    "File > Quit",
    "a\x00b",
    "\n",
    "'\"\\",
    "$(id)",
    "`id`",
    "🙂",
    "שלום",
    "x" * 300,
)
"""Strings worth trying wherever they fit: refs, apps and menus of the simulated desktop, paths,
URL schemes, key combos and characters that break naive quoting."""

ANY_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
WRONG_TYPE = st.one_of(
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(alphabet="xyz", min_size=1, max_size=5),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=2),
)
INT_LIMIT = 2**64


# =========================================================================== strategies
def from_schema(schema: Mapping[str, Any]) -> st.SearchStrategy[Any]:
    """Values that conform to ``schema`` (the JSON Schema subset AXPilot's portable schemas use)."""
    if "anyOf" in schema:
        return st.one_of([from_schema(option) for option in schema["anyOf"]])
    if "enum" in schema:
        return st.sampled_from(list(schema["enum"]))
    kind = schema.get("type")
    if isinstance(kind, list):
        return st.one_of([from_schema({**schema, "type": one}) for one in kind])
    if kind == "string":
        return _strings(schema)
    if kind == "integer":
        return _integers(schema)
    if kind == "number":
        return _numbers(schema)
    if kind == "boolean":
        return st.booleans()
    if kind == "array":
        low = schema.get("minItems", 0)
        return st.lists(
            from_schema(schema.get("items", {})),
            min_size=low,
            max_size=max(low, min(schema.get("maxItems", 5), 5)),
        )
    if kind == "object":
        return objects(schema)
    return ANY_SCALAR


def objects(schema: Mapping[str, Any]) -> st.SearchStrategy[dict[str, Any]]:
    """Objects with every required property and any subset of the optional ones."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    return st.fixed_dictionaries(
        {name: from_schema(prop) for name, prop in properties.items() if name in required},
        optional={name: from_schema(prop) for name, prop in properties.items() if name not in required},
    )


def _strings(schema: Mapping[str, Any]) -> st.SearchStrategy[str]:
    low, high = schema.get("minLength", 0), schema.get("maxLength")
    fitting = [text for text in STRINGS if len(text) >= low and (high is None or len(text) <= high)]
    generated = st.text(min_size=low, max_size=max(low, min(high if high is not None else 40, 40)))
    return st.one_of(st.sampled_from(fitting), generated) if fitting else generated


def _integers(schema: Mapping[str, Any]) -> st.SearchStrategy[int]:
    low = schema.get("minimum", -INT_LIMIT)
    if "exclusiveMinimum" in schema:
        low = max(low, math.floor(schema["exclusiveMinimum"]) + 1)
    return st.integers(min_value=low, max_value=schema.get("maximum", INT_LIMIT))


def _numbers(schema: Mapping[str, Any]) -> st.SearchStrategy[float]:
    exclusive = "exclusiveMinimum" in schema
    return st.floats(
        min_value=schema["exclusiveMinimum"] if exclusive else schema.get("minimum"),
        max_value=schema.get("maximum"),
        exclude_min=exclusive,
        allow_nan=False,
        allow_infinity=False,
    )


def _out_of_range(prop: Mapping[str, Any]) -> list[Any]:
    """Values just outside ``prop``'s bounds (none for unions, where another option may fit)."""
    if "anyOf" in prop:
        return []
    values: list[Any] = []
    if "enum" in prop:
        values.append("not-an-option")
    if "minimum" in prop:
        values.append(prop["minimum"] - 1)
    if "exclusiveMinimum" in prop:
        values.append(prop["exclusiveMinimum"])
    if "maximum" in prop:
        values.append(prop["maximum"] + 1)
    if prop.get("maxLength", math.inf) < 100_000:
        values.append("x" * (prop["maxLength"] + 1))
    if prop.get("type") == "array" and "maxItems" in prop:
        values.append([None] * (prop["maxItems"] + 1))
    return values


def broken(schema: Mapping[str, Any]) -> st.SearchStrategy[dict[str, Any]]:
    """Valid arguments with one thing broken: an unknown key, a missing required key, a value of
    the wrong type or a value out of range."""
    base = objects(schema)
    properties = schema.get("properties", {})
    required = list(schema.get("required", []))
    mutations = [base.map(lambda args: {**args, "unexpected_argument": 1})]
    if required:
        mutations.append(
            st.tuples(base, st.sampled_from(required)).map(
                lambda t: {k: v for k, v in t[0].items() if k != t[1]}
            )
        )
    if properties:
        names = st.sampled_from(sorted(properties))
        mutations.append(st.tuples(base, names, WRONG_TYPE).map(lambda t: {**t[0], t[1]: t[2]}))
        outside = [(name, value) for name, prop in properties.items() for value in _out_of_range(prop)]
        if outside:
            mutations.append(
                st.tuples(base, st.sampled_from(outside)).map(lambda t: {**t[0], t[1][0]: t[1][1]})
            )
    return st.one_of(mutations)


def conforms(value: Any, schema: Mapping[str, Any]) -> bool:
    """Whether ``value`` satisfies ``schema`` (the same subset :func:`from_schema` handles)."""
    if "anyOf" in schema:
        return any(conforms(value, option) for option in schema["anyOf"])
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind is None:
        return True
    if kind == "string":
        low, high = schema.get("minLength", 0), schema.get("maxLength", math.inf)
        return isinstance(value, str) and low <= len(value) <= high
    if kind == "boolean":
        return isinstance(value, bool)
    if kind in ("integer", "number"):
        types = int if kind == "integer" else (int, float)
        if isinstance(value, bool) or not isinstance(value, types):
            return False
        low, high = schema.get("minimum", -math.inf), schema.get("maximum", math.inf)
        return low <= value <= high and value > schema.get("exclusiveMinimum", -math.inf)
    if kind == "array":
        return (
            isinstance(value, list)
            and schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf)
            and all(conforms(item, schema.get("items", {})) for item in value)
        )
    if kind == "object":
        properties = schema.get("properties", {})
        return (
            isinstance(value, dict)
            and set(schema.get("required", [])) <= value.keys()
            and (schema.get("additionalProperties", True) is not False or value.keys() <= properties.keys())
            and all(conforms(item, properties.get(key, {})) for key, item in value.items())
        )
    return False


# =========================================================================== the sandbox
class VirtualClock:
    """A clock whose ``sleep`` returns at once and moves time forward instead."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)
        await anyio.lowlevel.checkpoint()


class ApproveAllButKill:
    """Approves every confirmation except stopping a process."""

    async def confirm(self, request: ConfirmRequest) -> bool:
        return request.tool != "processes"


def check_result(tool: str, args: Mapping[str, Any], result: CallToolResult) -> None:
    """Fail with a readable message unless ``result`` is a success or a coded AXPilot error,
    without a traceback."""
    text = text_of(result)
    where = f"{tool}({json.dumps(args, default=repr)[:500]})"
    for marker in TRACEBACK_MARKERS:
        assert marker not in text, f"{where} leaked a traceback:\n{text[:2000]}"
    if not result.is_error:
        return
    code = error_code_of(result)
    assert code is not None, f"{where} failed without an AXPilot error code:\n{text[:2000]}"
    assert code in ERROR_CODES, f"{where} failed with an unknown error code {code!r}"
    assert code != ErrorCode.INTERNAL, f"{where} raised an unexpected exception:\n{text[:2000]}"


class Sandbox:
    """One server per naming; every call runs on a fresh desktop and fresh server state."""

    def __init__(self, naming: str) -> None:
        self.naming = naming
        self.kills: list[tuple[Any, ...]] = []
        self._fresh = self._context()
        self.server = build_server(self._fresh, naming=naming)
        self.ctx: ServerContext = self.server.axpilot_context
        self.schemas: dict[str, dict[str, Any]] = anyio.run(self._list_schemas)

    @staticmethod
    def _context(engine: FakeEngine | FaultyEngine | None = None) -> ServerContext:
        ctx = make_test_context(CONFIG, engine=engine, confirm=ApproveAllButKill())
        clock = VirtualClock()
        ctx.clock, ctx.sleep = clock.clock, clock.sleep
        return ctx

    async def _list_schemas(self) -> dict[str, dict[str, Any]]:
        async with Client(self.server) as client:
            return {tool.name: dict(tool.input_schema) for tool in (await client.list_tools()).tools}

    def reset(self, engine: FakeEngine | FaultyEngine | None = None) -> None:
        """Fresh state for the next call. The fresh context is kept alive so its temporary
        directory (the audit log's) stays until the next reset."""
        self._fresh = self._context(engine)
        for name in STATE_FIELDS:
            setattr(self.ctx, name, getattr(self._fresh, name))

    def call(self, tool: str, args: Mapping[str, Any]) -> CallToolResult:
        async def run() -> CallToolResult:
            async with Client(self.server) as client:
                return await client.call_tool(tool, dict(args))

        return anyio.run(run)

    def check(self, tool: str, args: Mapping[str, Any]) -> CallToolResult:
        """Call ``tool`` on a fresh desktop and check the result and the state left behind."""
        self.reset()
        result = self.call(tool, args)
        check_result(tool, args, result)
        assert not self.ctx.control.busy, f"{tool} left the input lock held"
        assert not self.ctx.control.waiting_for_user, f"{tool} left a question open"
        assert not self.kills, f"{tool} tried to stop a real process: {self.kills}"
        return result

    @contextmanager
    def no_real_kills(self) -> Iterator[None]:
        """Patch ``kill_process`` to record and refuse (the confirmer already declines)."""

        def refuse(*args: Any, **kwargs: Any) -> str:
            self.kills.append(args)
            raise AssertionError("the fuzzer must never signal a real process")

        original = system_tools.kill_process
        system_tools.kill_process = refuse
        try:
            yield
        finally:
            system_tools.kill_process = original


@functools.cache
def sandbox(naming: str) -> Sandbox:
    return Sandbox(naming)


CASES = [(naming, name) for naming in NAMINGS for name in sandbox(naming).schemas]


# =========================================================================== the fuzz tests
@pytest.mark.parametrize(("naming", "tool"), CASES, ids=[f"{naming}-{tool}" for naming, tool in CASES])
def test_any_arguments_end_in_a_result_or_a_coded_error(naming: str, tool: str) -> None:
    box = sandbox(naming)
    schema = box.schemas[tool]

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        derandomize=not _DEEP,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(args=st.one_of(objects(schema), broken(schema)))
    def fuzz(args: dict[str, Any]) -> None:
        box.check(tool, args)

    with box.no_real_kills():
        fuzz()
        status = box.check(STATUS_TOOL[naming], {})
    assert not status.is_error, text_of(status)


# =========================================================================== the harness itself
def test_generated_arguments_conform_to_every_schema() -> None:
    schemas = [schema for naming in NAMINGS for schema in sandbox(naming).schemas.values()]
    schemas.append(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode"],
            "properties": {
                "mode": {"type": "string", "enum": ["a", "b"]},
                "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                "anything": {"type": "array", "items": {}, "minItems": 1, "maxItems": 2},
                "either": {"anyOf": [{"type": "string", "maxLength": 3}, {"type": "integer", "minimum": 5}]},
                "positive": {"type": "number", "exclusiveMinimum": 0},
                "nested": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"flag": {"type": "boolean"}},
                },
            },
        }
    )

    @settings(max_examples=20, deadline=None, derandomize=True, database=None)
    @given(data=st.data())
    def draw(data: st.DataObject) -> None:
        for schema in schemas:
            value = data.draw(objects(schema))
            assert conforms(value, schema), (value, schema)

    draw()


def test_the_checker_flags_internal_errors() -> None:
    box = Sandbox("default")
    box.reset(FaultyEngine(FakeEngine.demo(), {"list_apps": Fault.crash("simulated bug")}))
    result = box.call("list_apps", {})
    assert error_code_of(result) == ErrorCode.INTERNAL
    with pytest.raises(AssertionError, match="unexpected exception"):
        check_result("list_apps", {}, result)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            CallToolResult(content=[TextContent(type="text", text="boom")], is_error=True),
            "without an AXPilot",
        ),
        (
            CallToolResult(content=[TextContent(type="text", text="Traceback (most recent call last):\n")]),
            "leaked a traceback",
        ),
    ],
)
def test_the_checker_flags_uncoded_errors_and_tracebacks(result: CallToolResult, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        check_result("tool", {}, result)


def test_waits_pass_on_the_virtual_clock() -> None:
    box = Sandbox("default")
    started = time.monotonic()
    result = box.check("wait_for", {"condition": "duration", "timeout_ms": 60_000})
    assert not result.is_error, text_of(result)
    assert time.monotonic() - started < 10
    assert box.ctx.clock() >= 1_060.0


def test_stopping_a_process_is_declined_and_guarded() -> None:
    box = Sandbox("default")
    original = system_tools.kill_process
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with box.no_real_kills():
            result = box.check("processes", {"action": "kill", "pid": child.pid})
            assert error_code_of(result) == ErrorCode.USER_DENIED, text_of(result)
            with pytest.raises(AssertionError, match="real process"):
                system_tools.kill_process(child.pid)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)
    assert box.kills == [(child.pid,)]
    assert system_tools.kill_process is original


def test_every_call_starts_from_a_fresh_desktop() -> None:
    def running(box: Sandbox) -> list[str]:
        result = box.call("list_apps", {})
        assert result.structured_content is not None, text_of(result)
        return [app["name"] for app in result.structured_content["apps"]]

    box = Sandbox("default")
    quit_notes = box.check("app", {"action": "quit", "name": "Notes"})
    assert not quit_notes.is_error, text_of(quit_notes)
    assert "Notes" not in running(box)
    box.reset()
    assert "Notes" in running(box)
