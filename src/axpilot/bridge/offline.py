"""The stand-in server ``axpilot stdio`` runs when it cannot reach the AXPilot host.

An MCP client starts ``axpilot stdio`` and expects the MCP handshake. When AXPilot.app is not
installed, its host does not come up, or the host refuses this client, exiting before the
handshake shows up in the client as "server failed to start", with the reason buried in a log.
Instead the bridge serves this stand-in on stdin/stdout: it completes the handshake, lists the
tools the host would offer (same names, descriptions and schemas, for the requested profile and
naming), tells the agent in its instructions what is wrong, and answers every tool call with that
error and its hint ("Run `axpilot setup` once"), so the agent can tell the user what to do.

Nothing here touches the desktop: no engine call is ever made, nothing is audited, and the tool
functions are never run.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp_types import CallToolResult, InputRequiredResult

from axpilot.config import Config
from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import DenyConfirmer
from axpilot.server.app import AXPilotServer, build_server
from axpilot.server.context import ServerContext
from axpilot.server.results import error_result

__all__ = ["STAND_IN_ERRORS", "UnavailableServer", "build_unavailable_server"]

STAND_IN_ERRORS: frozenset[ErrorCode] = frozenset(
    {ErrorCode.HOST_UNAVAILABLE, ErrorCode.USER_DENIED, ErrorCode.BUSY, ErrorCode.NOT_SUPPORTED}
)
"""Errors of reaching the host (or of the host refusing this client) that the stand-in reports
through the MCP session instead of exiting. Configuration errors (an unknown profile) still exit."""

_NO_DESKTOP: dict[str, Any] = {"apps": [], "windows": []}
"""The stand-in's engine is never called; an empty simulated desktop satisfies the context."""


class UnavailableServer(AXPilotServer):
    """An AXPilot server whose every tool call returns :attr:`unavailable`."""

    def __init__(self, ctx: ServerContext, *, unavailable: AXPilotError, **kwargs: Any) -> None:
        note = (
            f"AXPilot cannot reach its host on this Mac, so every tool returns {unavailable.code.value}: "
            f"{unavailable.message}"
        )
        if unavailable.hint:
            note += f" {unavailable.hint}"
        note += " Tell the user, then try again after they fixed it (the MCP client may need a restart)."
        kwargs["instructions"] = f"{note}\n\n{kwargs.get('instructions') or ''}".rstrip()
        super().__init__(ctx, **kwargs)
        self.unavailable = unavailable

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult | InputRequiredResult:
        """The unavailability error for every tool this server lists (unknown names get the usual
        "no such tool" answer)."""
        if name not in self.tool_specs:
            return await super().call_tool(name, arguments, context)
        return error_result(self.unavailable)


def build_unavailable_server(
    unavailable: AXPilotError,
    config: Config,
    *,
    profile: str | None = None,
    naming: str | None = None,
) -> UnavailableServer:
    """The stand-in for ``unavailable``: the tools of ``profile``/``naming`` (defaults from
    ``config``), each answering with the error. Raises ``INVALID_ARGUMENT`` for an unknown profile
    or naming, like the host would."""
    ctx = ServerContext.create(
        config, FakeEngine.from_dict(_NO_DESKTOP), confirmer=DenyConfirmer(), elicitation=False
    )
    ctx.audit.enabled = False
    server = build_server(
        ctx,
        profile=profile,
        naming=naming,
        server_factory=functools.partial(UnavailableServer, unavailable=unavailable),
    )
    assert isinstance(server, UnavailableServer)
    return server
