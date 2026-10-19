"""The guard: the one wrapper every MCP tool runs inside.

For each call, in this order:

1. **enabled**: the tool must be enabled for this server (``TOOL_DISABLED``);
2. **rate limit**: input tools count against ``[safety] max_actions_per_minute``
   (``RATE_LIMITED``);
3. **confirmation**: when :meth:`Policy.requires_confirmation` says so, the user is asked
   (bounded wait; a "no" or no answer is ``USER_DENIED``). A destructive tool with harmless
   actions too counts as destructive only for the calls its ``confirm_when`` picks; a tool that
   ``confirms_itself`` asks in its body instead, once it can show exactly what will run;
4. **input turn**: input tools hold the single input lock
   (:meth:`ControlState.input_turn`: ``PAUSED_BY_USER``, ``USER_ACTIVE``, ``BUSY``);
5. **the tool body**, which applies the app/window policy and calls the engine through
   :meth:`ServerContext.run_engine` (deadline → ``TIMEOUT``);
6. **audit**: one record per call in ``finally`` (redacted arguments, duration, outcome),
   also for failures and cancellations.

Errors never escape: an :class:`AXPilotError` becomes an error result, anything else becomes
``INTERNAL`` with an incident id (``err_<8 hex>``) whose traceback is logged (stderr and the
log file), never returned. Cancellation by the client propagates (and releases the input
lock); the audit records it as ``CANCELLED``.
"""

from __future__ import annotations

import inspect
import json
import time
import typing
from collections.abc import Mapping
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import Context
from mcp_types import CallToolResult
from pydantic import BaseModel, ValidationError

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.audit import AuditEvent
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import CallState, ProgressFn, ServerContext, call_scope
from axpilot.server.registry import ToolFn, ToolSpec
from axpilot.server.results import error_code_of, error_result

__all__ = [
    "CANCELLED",
    "CONTEXT_PARAM",
    "guard_tool",
    "internal_error",
    "output_model_of",
    "record_audit",
]

CONTEXT_PARAM = "mcp_context"
"""Name of the keyword parameter through which the MCP SDK injects its request context."""
CANCELLED = "CANCELLED"
"""Audit ``error_code`` of a call the client cancelled."""
_DETAIL_MAX_CHARS = 2000


# --------------------------------------------------------------------------- helpers
def output_model_of(fn: ToolFn) -> type[BaseModel] | None:
    """The pydantic model in ``Annotated[CallToolResult, Model]`` (the tool's output schema)."""
    try:
        hint = typing.get_type_hints(fn, include_extras=True).get("return")
    except (NameError, TypeError):
        return None
    if typing.get_origin(hint) is Annotated:
        args = typing.get_args(hint)
        if args and args[0] is CallToolResult:
            model = next((a for a in args[1:] if isinstance(a, type) and issubclass(a, BaseModel)), None)
            return model
    return None


def internal_error(ctx: ServerContext, tool: str, exc: BaseException) -> AXPilotError:
    """Log ``exc`` with its traceback under a new incident id and return the ``INTERNAL`` error
    that may be shown to the client (it contains no traceback and no exception text)."""
    incident = ctx.new_incident_id()
    ctx.logger.error(
        "Tool %s failed unexpectedly (incident %s): %s", tool, incident, type(exc).__name__, exc_info=exc
    )
    return AXPilotError(
        ErrorCode.INTERNAL,
        f"{tool} failed because of an internal error (incident {incident}).",
        hint=f"This is an AXPilot bug; the details are in AXPilot's log under {incident}. "
        "Retrying may work; otherwise report it with the incident id.",
        details={"incident_id": incident},
    )


def _as_axpilot_error(exc: BaseException) -> AXPilotError | None:
    """The :class:`AXPilotError` behind ``exc`` (also inside a one-error exception group)."""
    if isinstance(exc, AXPilotError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        leaves = exc.exceptions
        if len(leaves) == 1:
            return _as_axpilot_error(leaves[0])
    return None


def record_audit(
    ctx: ServerContext,
    tool: str,
    args: Mapping[str, Any],
    *,
    ok: bool,
    error_code: str | None,
    started: float,
    summary: str | None = None,
    client: str | None = None,
) -> None:
    """Append one audit event. Best effort: a failing audit log never fails the tool."""
    try:
        ctx.audit.record(
            AuditEvent(
                tool=tool,
                client=client if client is not None else ctx.client_name,
                args=dict(args),
                ok=ok,
                error_code=error_code,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                summary=summary,
            )
        )
    except Exception as exc:  # never fail a tool because of the audit side channel (A7)
        ctx.logger.warning("Could not write the audit record for %s: %s", tool, exc)


def _default_confirm_text(spec: ToolSpec, args: Mapping[str, Any], client: str | None) -> tuple[str, str]:
    who = client or "The AI agent"
    summary = f"{who} wants to use {spec.title} ({spec.name})."
    shown = {key: value for key, value in args.items() if value is not None}
    detail = json.dumps(shown, ensure_ascii=False, default=str, indent=2)
    if len(detail) > _DETAIL_MAX_CHARS:
        detail = detail[: _DETAIL_MAX_CHARS - 1] + "…"
    return summary, detail


def _client_from(mcp_context: Any) -> str | None:
    """Best-effort client name from the MCP session (``clientInfo.name``)."""
    try:
        params = mcp_context.session.client_params
        info = getattr(params, "client_info", None)
        name = getattr(info, "name", None)
    except (AttributeError, ValueError, RuntimeError):
        return None
    return name if isinstance(name, str) and name else None


def _progress_from(mcp_context: Any) -> ProgressFn | None:
    report = getattr(mcp_context, "report_progress", None) if mcp_context is not None else None
    if report is None:
        return None

    async def progress(value: float, total: float | None, message: str | None) -> None:
        await report(value, total, message)

    return progress


# --------------------------------------------------------------------------- the guard
class _Call:
    """One guarded invocation of a tool."""

    def __init__(self, spec: ToolSpec, ctx: ServerContext, fn: ToolFn, model: type[BaseModel] | None) -> None:
        self.spec = spec
        self.ctx = ctx
        self.fn = fn
        self.model = model

    async def run(self, state: CallState, args: dict[str, Any]) -> CallToolResult:
        spec, ctx = self.spec, self.ctx
        ctx.policy.check_tool(spec.name)
        if not spec.read_only:  # the kill switch stops every tool that changes something
            ctx.control.ensure_running()
        if spec.input_action:
            ctx.limiter.check()
        await self._confirm(state, args)
        if spec.input_action:
            async with ctx.control.input_turn(timeout_s=ctx.input_lock_timeout_s, label=spec.name):
                result = await self.fn(**args)
        else:
            result = await self.fn(**args)
        return self._checked(result)

    async def _confirm(self, state: CallState, args: Mapping[str, Any]) -> None:
        spec, ctx = self.spec, self.ctx
        if spec.confirms_itself:
            return
        destructive = spec.destructive and (spec.confirm_when is None or spec.confirm_when(args))
        if not ctx.policy.requires_confirmation(spec.name, destructive=destructive):
            return
        if spec.confirm_text is not None:
            summary, detail = spec.confirm_text(args)
        else:
            summary, detail = _default_confirm_text(spec, args, state.client)
        await ctx.ask_user(
            ConfirmRequest(tool=spec.name, summary=summary, detail=detail, client=state.client)
        )
        state.confirmed = True

    def _checked(self, result: Any) -> CallToolResult:
        """Make sure the tool returned a valid result, so the SDK never has to reject one."""
        if not isinstance(result, CallToolResult):
            raise TypeError(f"{self.spec.name} returned {type(result).__name__}, not CallToolResult")
        if self.model is not None and not result.is_error:
            try:
                self.model.model_validate(result.structured_content)
            except ValidationError as exc:
                raise TypeError(f"{self.spec.name} returned invalid structured content: {exc}") from exc
        return result


def guard_tool(spec: ToolSpec, ctx: ServerContext, fn: ToolFn, *, public_name: str | None = None) -> ToolFn:
    """Wrap ``fn`` (built by ``spec.factory(ctx)``) in the guard.

    The wrapper keeps ``fn``'s parameters and return annotation (so the MCP SDK derives the
    same input/output schemas) and adds the keyword-only :data:`CONTEXT_PARAM` through which
    the SDK injects its request context (used for progress and the client name).
    """
    name = public_name or spec.name
    signature = inspect.signature(fn, eval_str=True)
    if CONTEXT_PARAM in signature.parameters:
        raise ValueError(f"Tool {spec.name!r} must not declare a parameter named {CONTEXT_PARAM!r}.")
    hints = typing.get_type_hints(fn, include_extras=True)
    call = _Call(spec, ctx, fn, output_model_of(fn))

    async def guarded(**kwargs: Any) -> CallToolResult:
        mcp_context = kwargs.pop(CONTEXT_PARAM, None)
        state = CallState(
            tool=spec.name,
            public_name=name,
            client=ctx.client_name or _client_from(mcp_context),
            progress=_progress_from(mcp_context),
        )
        started = time.perf_counter()
        ok, code, summary = False, CANCELLED, None
        try:
            with call_scope(state):
                result = await call.run(state, kwargs)
            ok, code = not result.is_error, error_code_of(result)
            return result
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:
            err = _as_axpilot_error(exc) or internal_error(ctx, spec.name, exc)
            code = err.code.value
            if err.code is ErrorCode.INTERNAL:
                summary = f"incident {err.details.get('incident_id', '?')}"
            return error_result(err)
        finally:
            record_audit(
                ctx, spec.name, kwargs, ok=ok, error_code=code, started=started, summary=summary,
                client=state.client,
            )  # fmt: skip

    parameters = [
        *signature.parameters.values(),
        inspect.Parameter(
            CONTEXT_PARAM, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None
        ),
    ]
    guarded.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    annotations = {key: value for key, value in hints.items() if key != "return"}
    annotations[CONTEXT_PARAM] = Context | None
    annotations["return"] = hints.get("return", CallToolResult)
    guarded.__annotations__ = annotations
    guarded.__name__ = name
    guarded.__qualname__ = name
    guarded.__doc__ = spec.description
    guarded.__module__ = getattr(fn, "__module__", __name__)
    guarded.axpilot_spec = spec  # type: ignore[attr-defined]
    guarded.axpilot_tool = fn  # type: ignore[attr-defined]
    return guarded
