"""Tool naming schemes: ``default`` (AXPilot's names) and ``windows-mcp``
(:mod:`axpilot.server.naming.windows_mcp`)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.context import ServerContext
from axpilot.server.naming.windows_mcp import Adapter, apply_windows_mcp, canonical_name
from axpilot.server.registry import ToolFn, ToolSpec

__all__ = ["NAMINGS", "Adapter", "NamingEntry", "apply_naming", "canonical_for"]

NAMINGS: tuple[str, ...] = ("default", "windows-mcp")

NamingEntry = tuple[str, ToolSpec, Callable[[ToolFn, ServerContext], ToolFn] | None]
"""``(public name, spec, adapter)``: an adapter turns a tool function into one with the naming
scheme's parameter shape (it gets the server context too, for adapters that combine tools)."""


def apply_naming(specs: Sequence[ToolSpec], naming: str) -> list[NamingEntry]:
    """The public tools for ``specs`` under ``naming``. Raises ``INVALID_ARGUMENT`` for an unknown
    naming."""
    if naming == "default":
        return [(spec.name, spec, None) for spec in specs]
    if naming == "windows-mcp":
        return apply_windows_mcp(specs)
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown tool naming {naming!r}.",
        hint=f"Use one of: {', '.join(NAMINGS)}.",
    )


def canonical_for(public: str, naming: str) -> str | None:
    """The canonical tool behind a public name under ``naming`` (``None`` if there is none)."""
    if naming == "windows-mcp":
        return canonical_name(public)
    return public
