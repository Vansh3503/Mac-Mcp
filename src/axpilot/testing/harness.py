"""Test harness: run AXPilot tools in-process against a simulated desktop.

Everything here is hermetic: the simulated desktop (:class:`~axpilot.engine.fake.FakeEngine`),
a :class:`~axpilot.system.runner.FakeRunner` instead of real commands, no config file or
``AXPILOT_*`` variables from your machine, and an audit log in a temporary directory::

    import pytest
    from axpilot.testing import call, connected_client, make_test_context

    @pytest.mark.anyio
    async def test_my_flow():
        ctx = make_test_context({"tools.profile": "full"})
        result, error = await call(ctx, "snapshot", {"app": "Notes"})
        assert error is None
        async with connected_client(ctx) as client:          # several calls, one session
            result = await client.call_tool("list_apps", {})

``call`` returns ``(result, error_code)``: the MCP :class:`CallToolResult` and the AXPilot
error code (``"STALE_REF"``…) or ``None`` on success.
"""

from __future__ import annotations

import tempfile
import weakref
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.server import MCPServer
from mcp_types import CallToolResult

from axpilot.config import load_config
from axpilot.engine.base import Engine
from axpilot.engine.fake import FakeEngine
from axpilot.policy.confirm import AutoConfirmer, Confirmer
from axpilot.server.app import build_server
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.system.runner import CommandRunner, FakeRunner

__all__ = ["call", "connected_client", "error_code_of", "make_test_context", "structured", "text_of"]


def make_test_context(
    config_overrides: Mapping[str, Any] | None = None,
    engine: Engine | None = None,
    confirm: bool | Confirmer = True,
    *,
    audit_path: Path | None = None,
    runner: CommandRunner | None = None,
) -> ServerContext:
    """A :class:`ServerContext` for tests.

    ``config_overrides`` uses dotted keys (``{"tools.profile": "full", "safety.confirm":
    "always"}``); defaults apply otherwise, never your real config. ``engine`` defaults to
    ``FakeEngine.demo()``. ``confirm`` answers confirmations (``True`` approves, ``False``
    declines) or is a :class:`Confirmer` of your own. ``audit_path`` defaults to a file in a
    temporary directory removed with the context; ``runner`` to a :class:`FakeRunner`.
    """
    scratch = tempfile.TemporaryDirectory(prefix="axpilot-test-")
    config = load_config(Path(scratch.name) / "config.toml", env={}, overrides=dict(config_overrides or {}))
    confirmer = confirm if not isinstance(confirm, bool) else AutoConfirmer(allow=confirm)
    ctx = ServerContext.create(
        config,
        engine if engine is not None else FakeEngine.demo(),
        confirmer=confirmer,
        runner=runner if runner is not None else FakeRunner(),
        audit_path=audit_path if audit_path is not None else Path(scratch.name) / "audit.jsonl",
    )
    weakref.finalize(ctx, scratch.cleanup)
    return ctx


@asynccontextmanager
async def connected_client(
    ctx: ServerContext,
    profile: str | None = None,
    naming: str | None = None,
    *,
    client_name: str | None = None,
) -> AsyncIterator[Client]:
    """An MCP :class:`Client` connected in-process to ``build_server(ctx, ...)``."""
    server = build_server(ctx, profile=profile, naming=naming, client_name=client_name)
    async with Client(server) as client:
        yield client


async def call(
    target: ServerContext | MCPServer | Client,
    tool: str,
    args: Mapping[str, Any] | None = None,
    *,
    profile: str | None = None,
    naming: str | None = None,
) -> tuple[CallToolResult, str | None]:
    """Call one tool and return ``(result, error_code)``.

    ``target`` is a context (a server is built for this call with ``profile``/``naming``), a
    server, or an already connected client. ``error_code`` is ``None`` on success.
    """
    arguments = dict(args or {})
    if isinstance(target, Client):
        result = await target.call_tool(tool, arguments)
        return result, error_code_of(result)
    server = target if isinstance(target, MCPServer) else build_server(target, profile=profile, naming=naming)
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    return result, error_code_of(result)


def structured(result: CallToolResult) -> dict[str, Any]:
    """The ``structuredContent`` of a successful result. Raises ``AssertionError`` (with the
    error text) for a failed one, so tests fail with a readable message."""
    if result.is_error or result.structured_content is None:
        raise AssertionError(f"expected a successful result, got: {text_of(result)}")
    return dict(result.structured_content)
