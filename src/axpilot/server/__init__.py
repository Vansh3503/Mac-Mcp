"""The MCP layer: tool registry, guard, shared context and server construction.

Typical use (what ``axpilot stdio --direct`` does)::

    from axpilot.config import load_config
    from axpilot.engine import create_engine
    from axpilot.server import ServerContext, build_server

    ctx = ServerContext.create(load_config(), create_engine("auto"))
    server = build_server(ctx)
    server.run("stdio")
"""

from __future__ import annotations

from axpilot.server.app import AXPilotServer, build_server, load_tool_modules
from axpilot.server.context import ServerContext, current_call
from axpilot.server.registry import REGISTRY, ToolSpec, tool
from axpilot.server.results import error_result, ok_result

__all__ = [
    "REGISTRY",
    "AXPilotServer",
    "ServerContext",
    "ToolSpec",
    "build_server",
    "current_call",
    "error_result",
    "load_tool_modules",
    "ok_result",
    "tool",
]
