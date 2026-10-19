#!/usr/bin/env python3
"""Spike S6: the OpenAI Agents SDK against AXPilot, without a model or an API key.

It connects with ``agents.mcp.MCPServerStdio``, lists the tools, checks that each input schema
survives the SDK's strict-schema conversion (OpenAI's strict mode rejects some JSON Schema
features), and calls ``status``. Run it in its own environment so the SDK can pick its own
``mcp`` version:

    uv run --no-project --with openai-agents python spikes/s6_mcp_interop/frameworks/agents_sdk_smoke.py
    ... agents_sdk_smoke.py --server "axpilot stdio --engine fake"   # if axpilot is on PATH

The default server command runs this checkout's CLI through ``uv run --project``. Verify the
SDK API against its current docs (PLAN.md §9); this script targets openai-agents 0.x.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
DEFAULT_SERVER = f"uv run --project {shlex.quote(str(REPO))} axpilot stdio --engine fake"


def input_schema(tool: Any) -> dict[str, Any]:
    """mcp 1.x tools have ``inputSchema``; mcp 2.x tools have ``input_schema``."""
    return dict(getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {})


async def smoke(server_command: str) -> dict[str, Any]:
    from agents.mcp import MCPServerStdio

    argv = shlex.split(server_command)
    report: dict[str, Any] = {"server": server_command}
    params = {"command": argv[0], "args": argv[1:]}
    async with MCPServerStdio(params=params, cache_tools_list=True, name="axpilot") as server:
        tools = await server.list_tools()
        report["tools"] = [tool.name for tool in tools]
        try:
            from agents.strict_schema import ensure_strict_json_schema
        except ImportError:
            report["strict_schema"] = "agents.strict_schema not found in this SDK version"
        else:
            failures = {}
            for tool in tools:
                try:
                    ensure_strict_json_schema(input_schema(tool))
                except Exception as exc:  # report every incompatible schema
                    failures[tool.name] = f"{type(exc).__name__}: {exc}"
            report["strict_schema_failures"] = failures
        if "status" in report["tools"]:
            result = await server.call_tool("status", {})
            report["status_is_error"] = bool(getattr(result, "isError", getattr(result, "is_error", False)))
            report["status_content_types"] = [getattr(block, "type", "?") for block in result.content]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="stdio server command")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(smoke(args.server))
    except ImportError as exc:
        sys.exit(f"{exc}. Run with: uv run --no-project --with openai-agents python {Path(__file__).name}")
    print(json.dumps(report, indent=2))
    failed = report.get("status_is_error") or report.get("strict_schema_failures")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
