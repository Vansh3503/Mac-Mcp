"""Building the MCP server: :func:`build_server`.

``build_server(ctx)`` imports the tool modules (which fill the registry), picks the tools
enabled by the profile and ``[tools] enable``/``disable``, applies the naming scheme, wraps
each tool in the guard and registers it, in ``TOOL_ORDER``, on an :class:`AXPilotServer`.

:class:`AXPilotServer` is an :class:`MCPServer` that also reports the SDK's own failures in
AXPilot's error format: arguments rejected by the input schema become ``INVALID_ARGUMENT``
(naming the fields, never echoing their values), calls to tools this server does not expose
become ``TOOL_DISABLED`` / ``NOT_SUPPORTED`` / ``INVALID_ARGUMENT``, and both are audited.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Mapping
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult, InputRequiredResult
from mcp_types import Tool as MCPTool
from pydantic import ValidationError

from axpilot import SERVER_NAME, __version__
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.profiles import ALL_TOOLS, enabled_tools
from axpilot.server.context import ServerContext
from axpilot.server.guard import guard_tool, internal_error, record_audit
from axpilot.server.instructions import instructions
from axpilot.server.naming import NAMINGS, apply_naming, canonical_for
from axpilot.server.portable import portable_schema
from axpilot.server.registry import ToolSpec, specs_for
from axpilot.server.results import error_result

__all__ = ["NAMINGS", "TOOL_MODULES", "AXPilotServer", "build_server", "load_tool_modules"]

_log = logging.getLogger(__name__)

TOOL_MODULES: tuple[str, ...] = (
    "observe",
    "input",
    "controls",
    "apps",
    "system",
    "scripting",
    "browser",
    "computer",
    "navigate",
)
"""Every module of :mod:`axpilot.server.tools`; importing one registers its tools."""

_TOOLS_PACKAGE = "axpilot.server.tools"
_MAX_PROBLEMS = 5


# --------------------------------------------------------------------------- tool modules
def load_tool_modules() -> None:
    """Import every tool module, registering its tools (an import error is a bug and raises)."""
    for module in TOOL_MODULES:
        importlib.import_module(f"{_TOOLS_PACKAGE}.{module}")


# --------------------------------------------------------------------------- the server
def _validation_error(tool: str, exc: ValidationError) -> AXPilotError:
    problems: list[str] = []
    fields: list[str] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        fields.append(location)
        problems.append(f"{location}: {error.get('msg', 'invalid')}")
    shown = "; ".join(problems[:_MAX_PROBLEMS])
    if len(problems) > _MAX_PROBLEMS:
        shown += f" (and {len(problems) - _MAX_PROBLEMS} more)"
    return AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid arguments for {tool}: {shown}.",
        hint="Check the tool's input schema (tools/list) and call it again with valid arguments.",
        details={"fields": sorted(set(fields))},
    )


class AXPilotServer(MCPServer):
    """An :class:`MCPServer` bound to a :class:`ServerContext` (see the module docstring)."""

    def __init__(self, ctx: ServerContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.axpilot_context = ctx
        self.tool_specs: dict[str, ToolSpec] = {}
        """Public tool name → spec, for every tool this server exposes."""

    async def list_tools(self) -> list[MCPTool]:
        """The tools, with input schemas in their portable form (:mod:`axpilot.server.portable`)."""
        return [
            tool.model_copy(update={"input_schema": portable_schema(tool.input_schema)})
            for tool in await super().list_tools()
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult | InputRequiredResult:
        """Call a tool; SDK-level failures are returned as AXPilot error results."""
        started = time.perf_counter()
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            err = self._sdk_error(name, exc)
            if err is None:
                raise
        record_audit(
            self.axpilot_context,
            self._audit_name(name),
            arguments if isinstance(arguments, Mapping) else {},
            ok=False,
            error_code=err.code.value,
            started=started,
        )
        return error_result(err)

    def _audit_name(self, name: str) -> str:
        """The canonical tool name for audit records (the public name if there is none)."""
        spec = self.tool_specs.get(name)
        if spec is not None:
            return spec.name
        ctx = self.axpilot_context
        prefix = ctx.config.tools.prefix
        canonical = canonical_for(name.removeprefix(prefix) if prefix else name, ctx.naming)
        return canonical if canonical in ALL_TOOLS else name

    def _sdk_error(self, name: str, exc: ToolError) -> AXPilotError | None:
        ctx = self.axpilot_context
        if isinstance(exc, UnexpectedToolError):
            return internal_error(ctx, name, exc.__cause__ or exc)
        if isinstance(exc.__cause__, ValidationError):
            return _validation_error(name, exc.__cause__)
        if self._tool_manager.get_tool(name) is not None:
            return None
        prefix = ctx.config.tools.prefix
        if prefix and not name.startswith(prefix) and self._tool_manager.get_tool(prefix + name) is not None:
            return AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"There is no tool named {name!r}: this server's tool names start with {prefix!r}.",
                hint=f"Call {prefix + name!r}.",
            )
        canonical = canonical_for(name.removeprefix(prefix) if prefix else name, ctx.naming)
        if canonical in ALL_TOOLS and canonical not in ctx.enabled:
            try:
                ctx.policy.check_tool(canonical)
            except AXPilotError as err:
                return err
        if canonical in ALL_TOOLS:
            return AXPilotError(
                ErrorCode.NOT_SUPPORTED,
                f"This server does not expose {name!r} under its {ctx.naming!r} tool naming.",
                hint="Call tools/list to see the tools this server provides.",
            )
        return AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"There is no tool named {name!r}.",
            hint="Call tools/list to see the available tools.",
        )


def build_server(
    ctx: ServerContext,
    *,
    profile: str | None = None,
    naming: str | None = None,
    client_name: str | None = None,
) -> AXPilotServer:
    """Build the MCP server for ``ctx``.

    ``profile`` (default: the context's) selects the tools, together with the config's
    ``[tools] enable``/``disable``; ``naming`` (``default`` | ``windows-mcp``) the public tool
    names; ``client_name`` labels audit records and confirmations. Raises ``INVALID_ARGUMENT``
    for an unknown profile, tool name or naming.
    """
    load_tool_modules()
    if profile is None:
        enabled, profile = ctx.enabled, ctx.profile
    else:
        tools = ctx.config.tools
        enabled = tuple(enabled_tools(profile, tools.enable, tools.disable))
    naming = naming or ctx.naming
    scoped = ctx.scoped(profile=profile, enabled=enabled, naming=naming, client_name=client_name)
    entries = apply_naming(specs_for(enabled), naming)
    server = AXPilotServer(
        scoped,
        name=SERVER_NAME,
        instructions=instructions(naming, scoped.config.tools.prefix),
        version=__version__,
        log_level="WARNING",
    )
    prefix = scoped.config.tools.prefix
    for name, spec, adapter in entries:
        public_name = prefix + name
        fn = spec.factory(scoped)
        if adapter is not None:
            fn = adapter(fn, scoped)
        server.add_tool(
            guard_tool(spec, scoped, fn, public_name=public_name),
            name=public_name,
            title=spec.title,
            description=spec.description,
            annotations=spec.annotations(),
        )
        server.tool_specs[public_name] = spec
    return server
