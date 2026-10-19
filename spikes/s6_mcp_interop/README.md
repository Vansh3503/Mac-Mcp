# Spike S6: MCP interoperability

**Question.** Does AXPilot's server, built on the MCP Python SDK v2 (`MCPServer`), work with the
clients and frameworks people will use: Claude Desktop, Claude Code, Cursor, VS Code, the OpenAI
Agents SDK and LangChain's MCP adapters? In particular: tool listing in a deterministic order,
`outputSchema` + `structuredContent`, annotations, error results, images, and both protocol eras
(the 2026-07-28 discovery and the older initialize handshake).

Unlike S1–S5 this needs no Mac for most of it: the fake engine (`--engine fake`) serves recorded
desktop fixtures on any OS.

## Files

| File | Purpose |
|---|---|
| `interop.py` | Connects with `mcp.Client` in `auto` and `legacy` modes, lists tools twice, checks schemas/descriptions/annotations, calls `status`; also `--http URL --token T` |
| `frameworks/agents_sdk_smoke.py` | OpenAI Agents SDK: list, strict-schema conversion, call |
| `frameworks/langchain_smoke.py` | LangChain MCP adapters: tools and their argument schemas, call |
| `CHECKLIST.md` | The manual checks per client, with config snippets |

## Run

```sh
uv sync
uv run python spikes/s6_mcp_interop/interop.py                                  # axpilot stdio --engine fake
uv run python spikes/s6_mcp_interop/interop.py --command "axpilot stdio --engine fake --profile minimal"
uv run python spikes/s6_mcp_interop/interop.py --command "axpilot stdio --engine fake --naming windows-mcp"
uv run --no-project --with openai-agents python spikes/s6_mcp_interop/frameworks/agents_sdk_smoke.py
uv run --no-project --with langchain-mcp-adapters python spikes/s6_mcp_interop/frameworks/langchain_smoke.py
```

`interop.py` exits 1 if any mode fails; `--modes` selects modes (`auto`, `legacy`, or a pinned
version such as `2026-07-28`), `--call`/`--call-args` choose the tool call, `--no-save` skips the
results file. Then work through [`CHECKLIST.md`](CHECKLIST.md) for each client.

The bridge path (client → `axpilot stdio` → Unix socket → host) is exercised by the same commands
once the host exists: start the host with the fake engine and drop `--engine fake` from the stdio
command (see `axpilot --help`), then compare with direct mode (`axpilot stdio --direct --engine fake`).

## What to record

Per client or framework, with its version: the checklist rows, the negotiated protocol version,
anything the client ignores or mangles (structured content, images, annotations, long
descriptions), and schema features that a framework rejects. Decisions this feeds: whether the
default tool descriptions and schemas need to change (for example for OpenAI strict mode), what
`axpilot connect` must write per client, and the examples in `examples/`.
