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
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult, InputRequiredResult
from pydantic import ValidationError

from axpilot import SERVER_NAME, __version__
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.profiles import ALL_TOOLS, enabled_tools
from axpilot.server.context import ServerContext
from axpilot.server.guard import guard_tool, internal_error, record_audit
from axpilot.server.instructions import instructions
from axpilot.server.registry import ToolFn, ToolSpec, specs_for
from axpilot.server.results import error_result

__all__ = [
    "NAMINGS",
    "OPTIONAL_TOOL_MODULES",
    "REQUIRED_TOOL_MODULES",
    "AXPilotServer",
    "build_server",
    "load_tool_modules",
]

_log = logging.getLogger(__name__)

NAMINGS: tuple[str, ...] = ("default", "windows-mcp")

REQUIRED_TOOL_MODULES: tuple[str, ...] = ("observe",)
"""Tool modules that must import (a failure is a bug and raises)."""

OPTIONAL_TOOL_MODULES: tuple[str, ...] = (
    "input",
    "apps",
    "system",
    "scripting",
    "browser",
    "computer",
    "navigate",
)
"""Tool modules written in parallel with this one. INTEGRATION NOTE: once every module exists,
move them all to :data:`REQUIRED_TOOL_MODULES` so a missing module fails loudly."""

_TOOLS_PACKAGE = "axpilot.server.tools"
_MAX_PROBLEMS = 5

NamingEntry = tuple[str, ToolSpec, Callable[[ToolFn], ToolFn] | None]
"""``(public name, spec, adapter)``; an adapter turns our tool function into one with the naming
scheme's parameter shape."""


# --------------------------------------------------------------------------- tool modules
def load_tool_modules() -> list[str]:
    """Import every tool module (registering its tools); return the optional ones that are
    missing. Only a missing *module* is tolerated: an import error inside an existing module
    propagates."""
    for module in REQUIRED_TOOL_MODULES:
        importlib.import_module(f"{_TOOLS_PACKAGE}.{module}")
    missing: list[str] = []
    for module in OPTIONAL_TOOL_MODULES:
        qualified = f"{_TOOLS_PACKAGE}.{module}"
        try:
            importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:
                raise
            missing.append(module)
    if missing:
        _log.info("Tool modules not available yet: %s", ", ".join(missing))
    return missing


# --------------------------------------------------------------------------- naming
def _default_naming(specs: Sequence[ToolSpec]) -> list[NamingEntry]:
    return [(spec.name, spec, None) for spec in specs]


def _naming_function() -> Callable[[Sequence[ToolSpec], str], Sequence[NamingEntry]] | None:
    for module in ("axpilot.server.naming", "axpilot.server.naming.windows_mcp"):
        try:
            loaded = importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name is None or not module.startswith(exc.name):
                raise
            continue
        apply = getattr(loaded, "apply_naming", None)
        if callable(apply):
            return cast("Callable[[Sequence[ToolSpec], str], Sequence[NamingEntry]]", apply)
    return None


def _apply_naming(specs: Sequence[ToolSpec], naming: str) -> list[NamingEntry]:
    if naming not in NAMINGS:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unknown tool naming {naming!r}.",
            hint=f"Use one of: {', '.join(NAMINGS)}.",
        )
    if naming == "default":
        return _default_naming(specs)
    apply = _naming_function()
    if apply is None:
        _log.warning("The %r naming is not available in this build; using default tool names.", naming)
        return _default_naming(specs)
    return list(apply(specs, naming))


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
        spec = self.tool_specs.get(name)
        record_audit(
            self.axpilot_context,
            spec.name if spec is not None else name,
            arguments if isinstance(arguments, Mapping) else {},
            ok=False,
            error_code=err.code.value,
            started=started,
        )
        return error_result(err)

    def _sdk_error(self, name: str, exc: ToolError) -> AXPilotError | None:
        ctx = self.axpilot_context
        if isinstance(exc, UnexpectedToolError):
            return internal_error(ctx, name, exc.__cause__ or exc)
        if isinstance(exc.__cause__, ValidationError):
            return _validation_error(name, exc.__cause__)
        if self._tool_manager.get_tool(name) is not None:
            return None
        if name in ALL_TOOLS and name not in ctx.enabled:
            try:
                ctx.policy.check_tool(name)
            except AXPilotError as err:
                return err
        if name in ALL_TOOLS:
            return AXPilotError(
                ErrorCode.NOT_SUPPORTED,
                f"The {name!r} tool is not available in this AXPilot build.",
                hint="Use `status` to see the tools this server provides.",
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
    entries = _apply_naming(specs_for(enabled), naming)
    server = AXPilotServer(
        scoped,
        name=SERVER_NAME,
        instructions=instructions(),
        version=__version__,
        log_level="WARNING",
    )
    for public_name, spec, adapter in entries:
        fn = spec.factory(scoped)
        if adapter is not None:
            fn = adapter(fn)
        server.add_tool(
            guard_tool(spec, scoped, fn, public_name=public_name),
            name=public_name,
            title=spec.title,
            description=spec.description,
            annotations=spec.annotations(),
        )
        server.tool_specs[public_name] = spec
    unimplemented = [name for name in enabled if name not in {s.name for _, s, _ in entries}]
    if unimplemented:
        _log.info("Enabled tools without an implementation yet: %s", ", ".join(unimplemented))
    return server
