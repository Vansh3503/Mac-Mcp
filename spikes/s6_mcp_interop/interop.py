#!/usr/bin/env python3
"""Spike S6: does the AXPilot MCP server interoperate with the official MCP Python SDK client?

    uv run python spikes/s6_mcp_interop/interop.py
    uv run python spikes/s6_mcp_interop/interop.py --command "axpilot stdio --engine fake --profile minimal"
    uv run python spikes/s6_mcp_interop/interop.py --http http://127.0.0.1:7788/mcp --token "$AXPILOT_TOKEN"

It starts the server (default ``axpilot stdio --engine fake``) once per protocol mode
(``auto``: the 2026-07-28 discovery with fallback; ``legacy``: the initialize handshake that older
clients use), lists the tools twice (the order must be deterministic), checks every tool's
schema, description and annotations, and calls one tool (default ``status``). The report goes to
stdout and ``results/<timestamp>-<label>.json``; the exit status is 1 if any mode failed.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spikelib

SPIKE = "s6_mcp_interop"
SPIKE_DIR = Path(__file__).resolve().parent
DEFAULT_COMMAND = "axpilot stdio --engine fake"
DEFAULT_MODES = ("auto", "legacy")


# --- pure helpers (unit-tested) ------------------------------------------------------------------


def tool_report(tools: Sequence[Any]) -> dict[str, Any]:
    """Facts and problems about a ``tools/list`` result (mcp_types.Tool objects)."""
    names = [tool.name for tool in tools]
    problems: list[str] = []
    for tool in tools:
        schema = tool.input_schema or {}
        if schema.get("type") != "object":
            problems.append(f"{tool.name}: inputSchema root type is {schema.get('type')!r}, not 'object'")
        if not (tool.description or "").strip():
            problems.append(f"{tool.name}: no description")
        if tool.annotations is None:
            problems.append(f"{tool.name}: no annotations")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    problems += [f"{name}: listed more than once" for name in duplicates]

    def hinted(flag: str) -> list[str]:
        return [t.name for t in tools if t.annotations is not None and getattr(t.annotations, flag)]

    return {
        "count": len(tools),
        "names": names,
        "with_output_schema": [t.name for t in tools if t.output_schema],
        "with_annotations": [t.name for t in tools if t.annotations is not None],
        "read_only": hinted("read_only_hint"),
        "destructive": hinted("destructive_hint"),
        "problems": problems,
    }


def call_report(result: Any) -> dict[str, Any]:
    """The shape of a ``tools/call`` result (mcp_types.CallToolResult)."""
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    structured = result.structured_content
    return {
        "is_error": result.is_error,
        "content_types": [getattr(block, "type", type(block).__name__) for block in result.content],
        "structured_type": type(structured).__name__ if structured is not None else None,
        "structured_keys": sorted(structured) if isinstance(structured, dict) else None,
        "text_excerpt": (texts[0][:300] if texts else None),
    }


def parse_arguments_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("--call-args must be a JSON object")
    return value


# --- the client side -----------------------------------------------------------------------------


ServerOpener = Callable[[AsyncExitStack], Awaitable[Any]]


def server_factory(args: argparse.Namespace) -> ServerOpener:
    """An opener that returns a fresh server spec for ``mcp.Client`` (one per connection);
    resources it needs, such as an HTTP client, are closed with the given exit stack."""
    from mcp import StdioServerParameters

    if args.http:

        async def open_http(stack: AsyncExitStack) -> Any:
            from mcp.client.streamable_http import streamable_http_client

            try:
                import httpx2 as httpx  # what mcp 2.x depends on
            except ImportError:  # pragma: no cover - depends on the installed SDK
                import httpx  # type: ignore[no-redef]
            headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
            http = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=30))
            return streamable_http_client(args.http, http_client=http)

        return open_http
    argv = shlex.split(args.command)
    if not argv:
        raise SystemExit("--command is empty")

    async def open_stdio(_stack: AsyncExitStack) -> Any:
        return StdioServerParameters(command=argv[0], args=argv[1:])

    return open_stdio


async def list_all_tools(client: Any, **kwargs: Any) -> list[Any]:
    tools: list[Any] = []
    cursor = None
    for _ in range(100):  # bounded: a server that never ends pagination must not hang us
        page = await client.list_tools(cursor=cursor, **kwargs)
        tools += page.tools
        cursor = page.next_cursor
        if cursor is None:
            break
    return tools


async def check_mode(
    open_server: ServerOpener, mode: str, call_name: str | None, call_args: dict[str, Any], limit_s: float
) -> dict[str, Any]:
    """Connect in one protocol mode and exercise list and call, within ``limit_s`` seconds."""
    from mcp import Client

    report: dict[str, Any] = {"mode": mode}
    started = time.perf_counter()
    try:
        with anyio.fail_after(limit_s):
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    Client(await open_server(stack), mode=mode, read_timeout_seconds=limit_s)
                )
                report["connect_ms"] = round((time.perf_counter() - started) * 1000, 1)
                info = client.server_info
                report["server"] = {"name": info.name, "version": info.version} if info else None
                report["protocol_version"] = client.protocol_version
                report["instructions_chars"] = len(client.instructions or "")
                report["capabilities"] = client.server_capabilities.model_dump(exclude_none=True)
                listed = time.perf_counter()
                tools = await list_all_tools(client)
                report["list_ms"] = round((time.perf_counter() - listed) * 1000, 1)
                again = await list_all_tools(client, cache_mode="refresh")
                report["tools"] = tool_report(tools)
                report["deterministic_order"] = [t.name for t in tools] == [t.name for t in again]
                if call_name is None:
                    report["call"] = {"skipped": "no tool to call"}
                elif call_name not in report["tools"]["names"]:
                    report["call"] = {"skipped": f"server has no tool named {call_name!r}"}
                else:
                    called = time.perf_counter()
                    result = await client.call_tool(call_name, call_args)
                    report["call"] = {"tool": call_name, **call_report(result)}
                    report["call"]["ms"] = round((time.perf_counter() - called) * 1000, 1)
        report["ok"] = not report["call"].get("is_error", False) and report["deterministic_order"]
    except Exception as exc:  # a diagnostic tool: record every failure instead of crashing
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


async def run(args: argparse.Namespace) -> dict[str, Any]:
    make_server = server_factory(args)
    call_args = parse_arguments_json(args.call_args)
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    report = spikelib.result_header(SPIKE, args.label)
    report["server_command"] = None if args.http else args.command
    report["http"] = args.http
    report["modes"] = [
        await check_mode(make_server, mode, args.call or None, call_args, args.timeout) for mode in modes
    ]
    report["ok"] = all(item["ok"] for item in report["modes"])
    return report


def print_summary(report: dict[str, Any]) -> None:
    for item in report["modes"]:
        if not item["ok"] and "error" in item:
            print(f"[{item['mode']}] FAILED: {item['error']}")
            continue
        tools = item["tools"]
        print(f"[{item['mode']}] protocol {item['protocol_version']}, server {item['server']}")
        ordered = item["deterministic_order"]
        print(f"    {tools['count']} tools in {item['list_ms']} ms; deterministic order: {ordered}")
        schemas, annotated = len(tools["with_output_schema"]), len(tools["with_annotations"])
        print(f"    with output schema: {schemas}; with annotations: {annotated}")
        for problem in tools["problems"]:
            print(f"    problem: {problem}")
        print(f"    call: {item['call']}")
    print(f"overall: {'ok' if report['ok'] else 'FAILED'}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--command", default=DEFAULT_COMMAND, help=f"stdio server command (default: {DEFAULT_COMMAND!r})"
    )
    parser.add_argument("--http", metavar="URL", help="connect to a Streamable HTTP endpoint instead")
    parser.add_argument("--token", help="bearer token for --http")
    parser.add_argument(
        "--modes", default=",".join(DEFAULT_MODES), help="comma-separated: auto, legacy, 2026-07-28"
    )
    parser.add_argument("--call", default="status", help="tool to call ('' to skip)")
    parser.add_argument("--call-args", help="JSON object of arguments for --call")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per mode")
    parser.add_argument("--label")
    parser.add_argument("--no-save", action="store_true", help="do not write results/")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = anyio.run(run, args)
    print_summary(report)
    if not args.no_save:
        for path in spikelib.write_results(SPIKE_DIR, report, label=args.label or "interop"):
            print(f"wrote {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
