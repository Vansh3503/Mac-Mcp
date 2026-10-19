"""The tool registry: every MCP tool is declared once, with its metadata, via :func:`tool`.

A tool module declares *factories*: functions that take the
:class:`~axpilot.server.context.ServerContext` and return the async tool function. The
tool function's parameters (``Annotated[type, Field(description=...)]``) become the input
schema; its return annotation ``Annotated[CallToolResult, <OutputModel>]`` becomes the output
schema::

    @tool(
        "list_displays",
        title="List displays",
        description="List the displays …",
        read_only=True,
        idempotent=True,
    )
    def list_displays_tool(ctx: ServerContext) -> ToolFn:
        async def list_displays() -> Annotated[CallToolResult, DisplayListModel]:
            ...
        return list_displays

Importing a tool module only registers specs in :data:`REGISTRY` (no other side effects);
:func:`axpilot.server.app.build_server` instantiates the enabled ones and wraps each in the
guard (:mod:`axpilot.server.guard`). Registering a name twice raises :class:`RegistryError`
at import time, as does a name missing from :data:`axpilot.policy.profiles.TOOL_ORDER`.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp_types import CallToolResult, ToolAnnotations

from axpilot.policy.profiles import TOOL_ORDER, TOOLSETS

if TYPE_CHECKING:
    from axpilot.server.context import ServerContext

__all__ = [
    "REGISTRY",
    "ConfirmText",
    "ConfirmWhen",
    "RegistryError",
    "ToolFactory",
    "ToolFn",
    "ToolSpec",
    "get_spec",
    "register",
    "specs_for",
    "tool",
]

ToolFn = Callable[..., Awaitable[CallToolResult]]
"""An async tool function: keyword parameters in, :class:`CallToolResult` out."""

ToolFactory = Callable[["ServerContext"], ToolFn]
"""Builds a tool function bound to one :class:`ServerContext`."""

ConfirmText = Callable[[Mapping[str, Any]], tuple[str, str | None]]
"""Builds ``(summary, detail)`` for the confirmation dialog from a call's arguments."""

ConfirmWhen = Callable[[Mapping[str, Any]], bool]
"""For a destructive tool: whether *this* call (its arguments) is destructive."""

_TOOLSET_OF: dict[str, str] = {name: toolset for toolset, names in TOOLSETS.items() for name in names}
_ORDER: dict[str, int] = {name: index for index, name in enumerate(TOOL_ORDER)}


class RegistryError(ValueError):
    """A tool declaration is invalid (duplicate or unknown name, contradictory hints)."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything AXPilot knows about one tool besides its implementation."""

    name: str
    """Canonical name (``snapshot``); must appear in ``TOOL_ORDER``."""
    toolset: str
    factory: ToolFactory
    title: str
    description: str
    """Written for LLMs: purpose, when to use it, key parameters, what comes back, common errors."""
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    input_action: bool = False
    """Takes the single input lock and counts against the rate limit (clicks, typing…)."""
    windows_mcp_name: str | None = None
    """Name under the Windows-MCP naming profile (``Click``), if the tool has one."""
    confirm_text: ConfirmText | None = None
    """Custom confirmation text; the default names the tool and lists the arguments."""
    confirm_when: ConfirmWhen | None = None
    """For a destructive tool with harmless actions too (``app``: launch is harmless, force_quit
    is not): the guard treats a call as destructive only when this returns True for its
    arguments. ``None``: every call of a destructive tool is."""

    def annotations(self) -> ToolAnnotations:
        """MCP tool annotations (hints for clients)."""
        return ToolAnnotations(
            title=self.title,
            read_only_hint=self.read_only,
            destructive_hint=self.destructive,
            idempotent_hint=self.idempotent,
            open_world_hint=self.open_world,
        )

    @property
    def order(self) -> int:
        """Position in ``TOOL_ORDER`` (the order of ``tools/list``)."""
        return _ORDER[self.name]


REGISTRY: dict[str, ToolSpec] = {}
"""Canonical tool name → spec, filled as tool modules are imported."""

_lock = threading.Lock()


def _origin(factory: Callable[..., Any]) -> tuple[str, str]:
    return getattr(factory, "__module__", ""), getattr(factory, "__qualname__", repr(factory))


def _validate(spec: ToolSpec) -> None:
    if spec.name not in _ORDER:
        raise RegistryError(
            f"Tool {spec.name!r} is not in any toolset; add it to axpilot.policy.profiles.TOOLSETS first."
        )
    expected = _TOOLSET_OF[spec.name]
    if spec.toolset != expected:
        raise RegistryError(f"Tool {spec.name!r} belongs to toolset {expected!r}, not {spec.toolset!r}.")
    if spec.read_only and (spec.destructive or spec.input_action):
        raise RegistryError(f"Tool {spec.name!r} cannot be read-only and destructive/an input action.")
    if not spec.title.strip() or not spec.description.strip():
        raise RegistryError(f"Tool {spec.name!r} needs a title and a description.")
    if spec.confirm_when is not None and not spec.destructive:
        raise RegistryError(f"Tool {spec.name!r} has confirm_when but is not destructive.")


def register(spec: ToolSpec) -> ToolSpec:
    """Add ``spec`` to :data:`REGISTRY`. Raises :class:`RegistryError` for an invalid spec or
    a duplicate name (re-importing the *same* factory, e.g. after a module reload, is allowed)."""
    _validate(spec)
    with _lock:
        existing = REGISTRY.get(spec.name)
        if existing is not None and _origin(existing.factory) != _origin(spec.factory):
            module, qualname = _origin(existing.factory)
            raise RegistryError(f"Tool {spec.name!r} is already registered by {module}.{qualname}.")
        REGISTRY[spec.name] = spec
    return spec


def tool(
    name: str,
    *,
    title: str,
    description: str,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
    input_action: bool = False,
    windows_mcp_name: str | None = None,
    confirm_text: ConfirmText | None = None,
    confirm_when: ConfirmWhen | None = None,
    toolset: str | None = None,
) -> Callable[[ToolFactory], ToolFactory]:
    """Decorator that registers a tool factory (see the module docstring). ``toolset``
    defaults to the toolset that lists ``name`` in ``TOOLSETS``."""

    def decorator(factory: ToolFactory) -> ToolFactory:
        register(
            ToolSpec(
                name=name,
                toolset=toolset or _TOOLSET_OF.get(name, ""),
                factory=factory,
                title=title,
                description=description.strip(),
                read_only=read_only,
                destructive=destructive,
                idempotent=idempotent,
                open_world=open_world,
                input_action=input_action,
                windows_mcp_name=windows_mcp_name,
                confirm_text=confirm_text,
                confirm_when=confirm_when,
            )
        )
        return factory

    return decorator


def get_spec(name: str) -> ToolSpec:
    """The spec of a registered tool. Raises ``KeyError`` for unknown names."""
    return REGISTRY[name]


def specs_for(names: Iterable[str]) -> list[ToolSpec]:
    """Registered specs for ``names``, in ``TOOL_ORDER`` (names without a spec are skipped)."""
    wanted = set(names)
    return sorted((spec for name, spec in REGISTRY.items() if name in wanted), key=lambda s: s.order)
