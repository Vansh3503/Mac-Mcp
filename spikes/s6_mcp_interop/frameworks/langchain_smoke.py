#!/usr/bin/env python3
"""Spike S6: LangChain's MCP adapters against AXPilot, without a model or an API key.

It loads the tools with ``langchain_mcp_adapters.client.MultiServerMCPClient``, prints how each
input schema became a LangChain tool's arguments, and invokes ``status``. Run it in its own
environment so the adapters can pick their own ``mcp`` version:

    uv run --no-project --with langchain-mcp-adapters \\
        python spikes/s6_mcp_interop/frameworks/langchain_smoke.py
    ... langchain_smoke.py --server "axpilot stdio --engine fake"   # if axpilot is on PATH

Verify the adapter API against its current docs (PLAN.md §9).
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


async def smoke(server_command: str) -> dict[str, Any]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    argv = shlex.split(server_command)
    client = MultiServerMCPClient({"axpilot": {"command": argv[0], "args": argv[1:], "transport": "stdio"}})
    tools = await client.get_tools()
    report: dict[str, Any] = {
        "server": server_command,
        "tools": {tool.name: sorted(getattr(tool, "args", {}) or {}) for tool in tools},
    }
    status = next((tool for tool in tools if tool.name == "status"), None)
    if status is not None:
        result = await status.ainvoke({})
        report["status_result_type"] = type(result).__name__
        report["status_excerpt"] = str(result)[:300]
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
        sys.exit(
            f"{exc}. Run with: uv run --no-project --with langchain-mcp-adapters python {Path(__file__).name}"
        )
    print(json.dumps(report, indent=2))
    return 0 if "status_result_type" in report else 1


if __name__ == "__main__":
    sys.exit(main())
