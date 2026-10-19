# ADR-0004: MCP protocol surface

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-4](../PLAN.md#4-key-decisions-adrs) and [§6](../PLAN.md#6-tool-surface-v10)

## Context and problem statement

The MCP specification 2026-07-28 is stateless: there is no `initialize` handshake or session, and
state that spans calls has to travel as server-minted handles in tool arguments. HTTP+SSE, MCP
logging, sampling and roots are deprecated; Tasks became an extension. Clients differ widely in what
they support (structured output, elicitation, images), and agents range from large vision models to
small local text models.

Which parts of MCP does AXPilot use, and how does it carry state?

## Decision drivers

- Work with every mainstream client, including ones on older protocol revisions.
- Several clients at once, with no hidden per-session state.
- Machine-checkable results for frameworks, compact text for models.
- A tool API stable enough to promise under SemVer.

## Decision

- **Tools** declare an `inputSchema` and an `outputSchema`. Results carry `structuredContent` plus a
  compact text `content` (and an image where relevant), and annotations (`readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`).
- **Few, orthogonal tools**, with `action` enums instead of tool sprawl, grouped in toolsets and
  selected by profiles (PLAN §6.1).
- **Deterministic `tools/list` order** (toolset order, then tool order), so clients can cache it.
- **Stateless handles:** `snapshot_id`, `screenshot_id` and element refs are passed as arguments and
  resolved through bounded LRU stores. No per-session state; multiple clients are safe.
- **Transports:** stdio (default) and Streamable HTTP. **No SSE.**
- A server **`instructions`** operating guide; optional resources (`axpilot://snapshot/{id}`);
  progress notifications for `wait_for`; the Tasks extension later.
- **Errors** are tool results with `isError: true`, a stable code, a message and a hint, also
  provided as `_meta["axpilot/error"]`. Unexpected exceptions become `INTERNAL` with an incident id
  and never leak tracebacks.
- **Confirmations:** a native dialog in the host works with every client; MCP elicitation is used
  when the client supports it (direct mode).
- **Logs** go to stderr and `~/Library/Logs/AXPilot/` (optionally OpenTelemetry), not to the
  deprecated MCP logging feature.

## Consequences

Positive:

- Works across protocol revisions (the SDK negotiates older ones) and with stateless load patterns.
- Frameworks get typed results; models get short text; both see the same data.
- Profiles let small models see fewer tools without forking the server.

Negative:

- Handles expire (LRU eviction), so agents must handle `SNAPSHOT_NOT_FOUND` and `STALE_REF`.
- Tool names, parameters and schemas are public API: changes need golden-file updates and SemVer
  discipline.
- Clients that only speak HTTP+SSE are not supported.

## Alternatives considered

- **Many small tools**, one per action: easy to read, but bloats context and confuses smaller models.
- **Session state** (an implicit "current snapshot" per connection): simpler arguments, but
  incompatible with the stateless specification and unsafe with several clients.
- **HTTP+SSE**: still common in older clients, but deprecated.
- **MCP logging** for diagnostics: deprecated, and logs belong on disk, not in the model's context.

## Validation

- Contract tests run every tool against `FakeEngine` through an in-process MCP client, including
  each error code.
- Golden files of `tools/list` for each profile and naming scheme fail CI on accidental API changes.
- Spike S6 checks interoperability with Claude Desktop, Claude Code, Cursor, VS Code, the OpenAI
  Agents SDK and LangChain's adapters, over stdio, Streamable HTTP and the Unix-socket bridge.
