"""Running eval tasks: a fresh desktop per task, the agent, then the checks.

For each task :func:`run_task` builds a server on a fresh desktop (the simulated ``demo``
desktop, or the real Mac for ``desktop = "macos"`` tasks when the caller allows it), connects an
in-process MCP client, and hands the agent an :class:`EvalSession`: the task's tools and a
:meth:`~EvalSession.call` that counts steps and stops the agent at ``max_steps``. Then every
check runs through the same server with read-only tools, so it sees exactly what the agent left
behind. Nothing here touches the network; a model-backed agent brings its own client.

:class:`ScriptedAgent` replays each task's reference solution. CI runs it over the whole suite,
which proves that every task is solvable and every check is reachable; it is not a model
baseline.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp import Client
from mcp_types import CallToolResult, Tool

from axpilot.config import load_config
from axpilot.engine.base import Engine
from axpilot.engine.fake import FakeEngine
from axpilot.evals.checks import evaluate, resolve
from axpilot.evals.model import Check, Task
from axpilot.policy.confirm import AutoConfirmer, DenyConfirmer
from axpilot.server.app import build_server
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.system.runner import FakeRunner

__all__ = [
    "Agent",
    "AgentReport",
    "CheckResult",
    "EvalSession",
    "ScriptedAgent",
    "SolutionError",
    "StepLimitReached",
    "SuiteResult",
    "TaskResult",
    "ToolCall",
    "run_suite",
    "run_task",
]

_log = logging.getLogger(__name__)


class StepLimitReached(Exception):
    """The agent used more tool calls than the task's ``max_steps``."""


class SolutionError(Exception):
    """A reference solution did not go as written (a wrong error code, a missing value)."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call the agent made."""

    tool: str
    args: Mapping[str, Any]
    error_code: str | None
    duration_ms: float


@dataclass(slots=True)
class AgentReport:
    """What an agent returns: its final reply and, if it knows them, the tokens it used."""

    final_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class EvalSession:
    """The agent's view of a task: its tools, and :meth:`call`."""

    def __init__(self, client: Client, task: Task, tools: Sequence[Tool]) -> None:
        self.client = client
        self.task = task
        self.tools = list(tools)
        self.calls: list[ToolCall] = []

    async def call(self, tool: str, args: Mapping[str, Any] | None = None) -> CallToolResult:
        """Call ``tool``; raises :class:`StepLimitReached` once ``max_steps`` calls were made."""
        if len(self.calls) >= self.task.max_steps:
            raise StepLimitReached(f"the agent used all {self.task.max_steps} steps")
        started = time.perf_counter()
        result = await self.client.call_tool(tool, dict(args or {}))
        self.calls.append(
            ToolCall(tool, dict(args or {}), error_code_of(result), (time.perf_counter() - started) * 1000)
        )
        return result


class Agent(Protocol):
    """Anything that can attempt a task through an :class:`EvalSession`."""

    name: str

    async def run(self, task: Task, session: EvalSession) -> AgentReport: ...


def _substitute(value: Any, saved: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$") and value[1:] in saved:
        return saved[value[1:]]
    if isinstance(value, Mapping):
        return {k: _substitute(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, saved) for v in value]
    return value


class ScriptedAgent:
    """Replays the task's reference solution (see the module docstring)."""

    name = "scripted"

    async def run(self, task: Task, session: EvalSession) -> AgentReport:
        saved: dict[str, Any] = {}
        for number, step in enumerate(task.solution, start=1):
            result = await session.call(step.tool, _substitute(dict(step.args), saved))
            code = error_code_of(result)
            if code != step.expect_error:
                expected = step.expect_error or "success"
                got = code or "success"
                raise SolutionError(
                    f"step {number} ({step.tool}) ended with {got}, not {expected}: {text_of(result)[:200]}"
                )
            for name, path in step.save.items():
                values = resolve(result.structured_content or {}, path)
                if not values:
                    raise SolutionError(
                        f"step {number} ({step.tool}) has nothing at {path!r} to save as {name!r}"
                    )
                saved[name] = values[0]
        return AgentReport(final_text=task.answer)


@dataclass(slots=True)
class CheckResult:
    """The outcome of one check."""

    check: Check
    passed: bool
    detail: str


@dataclass(slots=True)
class TaskResult:
    """The outcome of one task."""

    task: Task
    agent: str
    success: bool
    checks: list[CheckResult]
    calls: list[ToolCall]
    duration_s: float
    report: AgentReport = field(default_factory=AgentReport)
    error: str | None = None
    skipped: str | None = None

    @property
    def steps(self) -> int:
        return len(self.calls)

    @property
    def failed_calls(self) -> int:
        return sum(1 for call in self.calls if call.error_code is not None)


@dataclass(slots=True)
class SuiteResult:
    """Every task's result, in the order run."""

    agent: str
    results: list[TaskResult]

    @property
    def ran(self) -> list[TaskResult]:
        return [r for r in self.results if r.skipped is None]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.ran if r.success)

    @property
    def success_rate(self) -> float:
        return self.passed / len(self.ran) if self.ran else 0.0


EngineFactory = Callable[[Task], Engine]


def _context(task: Task, engine: Engine, audit_dir: Path) -> ServerContext:
    overrides = {"tools.profile": task.profile, **dict(task.config)}
    config = load_config(audit_dir / "config.toml", env={}, overrides=overrides)
    fake = isinstance(engine, FakeEngine)
    return ServerContext.create(
        config,
        engine,
        # Evals run unattended: the simulated desktop approves, a real Mac never does.
        confirmer=AutoConfirmer(allow=True) if fake else DenyConfirmer(),
        runner=FakeRunner() if fake else None,
        audit_path=audit_dir / "audit.jsonl",
        elicitation=False,
    )


async def _run_check(client: Client, server: Any, check: Check, report: AgentReport) -> CheckResult:
    if check.kind == "answer":
        text = report.final_text or ""
        passed = evaluate(check.op, [text], check.value)
        return CheckResult(check, passed, f"answer {check.op} {check.value!r}")
    assert check.tool is not None
    spec = server.tool_specs.get(check.tool)
    reads = spec is not None and (spec.read_only or _reading(check.tool, check.args))
    if not reads:
        return CheckResult(check, False, f"{check.tool} is not a read-only tool of this server")
    result = await client.call_tool(check.tool, dict(check.args))
    if result.is_error:
        return CheckResult(check, False, f"{check.tool} failed: {text_of(result)[:200]}")
    values = resolve(result.structured_content or {}, check.path)
    passed = evaluate(check.op, values, check.value)
    shown = values[:3]
    return CheckResult(check, passed, f"{check.path or '.'} = {shown!r} {check.op} {check.value!r}")


_READ_ACTIONS: dict[str, tuple[str, frozenset[str]]] = {"clipboard": ("action", frozenset({"get"}))}
"""Tools that also change things, and the argument values that only read (usable in checks)."""


def _reading(tool: str, args: Mapping[str, Any]) -> bool:
    if tool not in _READ_ACTIONS:
        return False
    key, values = _READ_ACTIONS[tool]
    return args.get(key) in values


def _default_engine(task: Task) -> Engine:
    if task.desktop == "demo":
        return FakeEngine.demo()
    from axpilot.engine import create_engine  # noqa: PLC0415 - the real Mac, only when allowed

    return create_engine("macos")


async def run_task(
    task: Task,
    agent: Agent,
    *,
    engine_factory: EngineFactory | None = None,
    allow_real_desktop: bool = False,
    agent_enabled: bool = True,
) -> TaskResult:
    """Run ``task`` with ``agent`` on a fresh desktop and check the outcome.

    ``desktop = "macos"`` tasks are skipped unless ``allow_real_desktop`` (they drive this Mac).
    ``agent_enabled=False`` runs only the checks (to see which already hold before any action)."""
    if task.desktop == "macos" and not allow_real_desktop:
        return TaskResult(
            task, agent.name, False, [], [], 0.0, skipped="drives the real Mac (pass --real-desktop)"
        )
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="axpilot-eval-") as scratch:
        engine = (engine_factory or _default_engine)(task)
        ctx = _context(task, engine, Path(scratch))
        server = build_server(ctx)
        report, error = AgentReport(), None
        try:
            async with Client(server) as client:
                session = EvalSession(client, task, (await client.list_tools()).tools)
                if agent_enabled:
                    try:
                        report = await agent.run(task, session)
                    except StepLimitReached as exc:
                        error = str(exc)
                    except Exception as exc:  # an agent crash fails the task, never the suite (A8: kept)
                        _log.warning("Agent %s failed on %s", agent.name, task.id, exc_info=True)
                        error = f"{type(exc).__name__}: {exc}"
                checks = [await _run_check(client, server, check, report) for check in task.checks]
        finally:
            ctx.close()
        success = error is None and all(c.passed for c in checks)
        return TaskResult(
            task, agent.name, success, checks, session.calls, time.perf_counter() - started, report, error
        )


async def run_suite(
    tasks: Sequence[Task],
    agent: Agent,
    *,
    engine_factory: EngineFactory | None = None,
    allow_real_desktop: bool = False,
    progress: Callable[[TaskResult], None] | None = None,
) -> SuiteResult:
    """Run every task in order; ``progress`` is called after each one."""
    results: list[TaskResult] = []
    for task in tasks:
        result = await run_task(
            task, agent, engine_factory=engine_factory, allow_real_desktop=allow_real_desktop
        )
        results.append(result)
        if progress is not None:
            progress(result)
    return SuiteResult(agent.name, results)
