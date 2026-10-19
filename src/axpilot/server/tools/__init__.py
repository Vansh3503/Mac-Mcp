"""MCP tool modules, one per toolset.

Each module declares tool factories with :func:`axpilot.server.registry.tool`; importing it
only registers them. :func:`axpilot.server.app.build_server` imports every module, builds the
enabled tools for a :class:`~axpilot.server.context.ServerContext` and wraps them in the guard.

Writing a tool (see :mod:`axpilot.server.tools.observe` for complete examples):

* the factory takes the context and returns ``async def <name>(...) -> Annotated[CallToolResult,
  <OutputModel>]``; parameters are ``Annotated[type, Field(description=..., ge=..., ...)]``;
* do not use ``from __future__ import annotations`` in tool modules (the MCP SDK evaluates the
  annotations when the tool is registered);
* validate arguments first and raise ``AXPilotError(ErrorCode.INVALID_ARGUMENT, ...)``;
* apply the policy (``ctx.policy.check_app`` / ``ctx.check_target``) before touching an app;
* call the engine only through ``await ctx.run_engine(...)`` (worker thread + deadline);
* return :func:`axpilot.server.results.ok_result`; raise :class:`AXPilotError` for failures (the
  guard turns them into error results, and anything else into ``INTERNAL``).
"""
