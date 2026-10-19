"""Toolsets, profiles and the set of enabled tools.

A *toolset* groups related tools (``observe``, ``input``…). A *profile* is a named
starting set of tools (``minimal``, ``standard``, ``full``, ``computer``). The config's
``[tools] enable``/``disable`` lists then add or remove tools or whole toolsets.

The order of :data:`TOOL_ORDER` is the order of ``tools/list``: it is deterministic
(toolset order, then tool order within the toolset) so clients can cache it.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence

from axpilot.errors import AXPilotError, ErrorCode

__all__ = [
    "ALL_TOOLS",
    "DEFAULT_PROFILE",
    "PROFILES",
    "TOOLSETS",
    "TOOL_ORDER",
    "enabled_tools",
    "expand_names",
    "toolset_of",
]

TOOLSETS: dict[str, tuple[str, ...]] = {
    "observe": (
        "snapshot",
        "screenshot",
        "find",
        "explore",
        "read",
        "inspect",
        "ocr",
        "list_apps",
        "list_windows",
        "list_displays",
        "wait_for",
        "status",
    ),
    "input": (
        "click",
        "type_text",
        "press_keys",
        "scroll",
        "drag",
        "move_mouse",
        "set_value",
        "select_option",
        "perform_action",
        "select_menu",
        "fill_form",
        "handle_dialog",
    ),
    "apps": ("app", "window", "open"),
    "system": (
        "clipboard",
        "notify",
        "spotlight_search",
        "system_info",
        "system_control",
        "processes",
    ),
    "scripting": ("shell", "applescript", "shortcuts", "defaults"),
    "browser": ("browser_tabs", "browser_read", "browser_eval_js"),
    "computer": ("computer",),
}
"""Toolset name → its tools, both in canonical order."""

TOOL_ORDER: tuple[str, ...] = tuple(tool for tools in TOOLSETS.values() for tool in tools)
"""Every tool in canonical order (toolset order, then tool order)."""

ALL_TOOLS: frozenset[str] = frozenset(TOOL_ORDER)

_TOOLSET_OF: dict[str, str] = {tool: name for name, tools in TOOLSETS.items() for tool in tools}


def _toolsets(*names: str) -> frozenset[str]:
    return frozenset(tool for name in names for tool in TOOLSETS[name])


PROFILES: dict[str, frozenset[str]] = {
    "minimal": frozenset(
        {
            "snapshot",
            "screenshot",
            "click",
            "type_text",
            "press_keys",
            "scroll",
            "app",
            "wait_for",
            "status",
            "explore",
        }
    ),
    "standard": (_toolsets("observe", "input", "apps") - {"list_displays"}) | {"clipboard"},
    "full": _toolsets("observe", "input", "apps", "system", "scripting", "browser"),
    "computer": frozenset({"computer", "status"}),
}
"""Profile name → the tools it enables before ``enable``/``disable`` are applied."""

DEFAULT_PROFILE = "standard"


def _unknown_name_error(kind: str, name: str, choices: Iterable[str], hint: str) -> AXPilotError:
    close = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.7)
    suggestion = f" Did you mean {close[0]!r}?" if close else ""
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown {kind} {name!r}.{suggestion}", hint=hint)


def _normalize(name: str) -> str:
    return name.strip().casefold()


def expand_names(names: Iterable[str]) -> set[str]:
    """Expand tool and toolset names into a set of tool names.

    A name that is both a toolset and a tool (``computer``) means the same thing either
    way. Raises ``INVALID_ARGUMENT`` for unknown names.
    """
    tools: set[str] = set()
    for raw in names:
        name = _normalize(raw)
        if name in TOOLSETS:
            tools.update(TOOLSETS[name])
        elif name in ALL_TOOLS:
            tools.add(name)
        else:
            raise _unknown_name_error(
                "tool or toolset",
                raw,
                [*TOOLSETS, *TOOL_ORDER],
                hint=f"Toolsets: {', '.join(TOOLSETS)}. Tools: {', '.join(TOOL_ORDER)}.",
            )
    return tools


def enabled_tools(profile: str, enable: Sequence[str] = (), disable: Sequence[str] = ()) -> list[str]:
    """The tools a server exposes, in :data:`TOOL_ORDER`.

    Starts from ``profile``, adds ``enable`` and then removes ``disable`` (so a name in both
    ends up disabled). Both lists accept tool or toolset names. Raises ``INVALID_ARGUMENT``
    for an unknown profile, tool or toolset.
    """
    key = _normalize(profile)
    if key not in PROFILES:
        raise _unknown_name_error(
            "profile",
            profile,
            PROFILES,
            hint=f"Profiles: {', '.join(PROFILES)}.",
        )
    tools = set(PROFILES[key]) | expand_names(enable)
    tools -= expand_names(disable)
    return [tool for tool in TOOL_ORDER if tool in tools]


def toolset_of(tool: str) -> str:
    """The toolset a tool belongs to. Raises ``INVALID_ARGUMENT`` for unknown tools."""
    try:
        return _TOOLSET_OF[tool]
    except KeyError:
        raise _unknown_name_error("tool", tool, TOOL_ORDER, hint=f"Tools: {', '.join(TOOL_ORDER)}.") from None
