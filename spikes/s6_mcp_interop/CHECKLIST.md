# S6 client and framework checklist

For each client, connect AXPilot with the fake engine first (no permissions involved), then
repeat the starred (★) rows with the real engine on a Mac. Record ✅ / ❌ / n.a. and notes per row
in `docs/spikes/S6-mcp-interop.md` (format: `docs/spikes/RESULTS_TEMPLATE.md`), with the client's
version.

Server command used everywhere below (adjust the path):

```sh
uv run --project /ABS/PATH/TO/Mac-Mcp axpilot stdio --engine fake
```

## Checks per client

| # | Check | How |
|---|---|---|
| 1 | Server starts and stays connected | the client shows AXPilot as connected; no error toast |
| 2 | All tools listed, in our order | compare with `interop.py` output; same names, same order after a reconnect |
| 3 | Descriptions shown and useful | open the tool list in the client's UI |
| 4 | Annotations respected | read-only tools run without an extra confirmation where the client supports that; destructive ones are flagged |
| 5 | `structuredContent` handled | call `status`; the client shows the text content, and does not error on the structured part |
| 6 | Images | ★ call `screenshot`: the image is shown to the model |
| 7 | Tool errors | call `click` with `ref: "e999"`: the client shows an error result with our code and hint, and the session continues |
| 8 | `instructions` | the client passes the server instructions to the model (ask the model what AXPilot's instructions say) |
| 9 | Protocol version | note what the client negotiates (handshake era vs 2026-07-28); `axpilot` logs it on stderr |
| 10 | stderr | server logs on stderr do not break the session |
| 11 | Long call | ★ `wait_for` with `timeout_ms: 20000`: no client-side timeout; progress shown if supported |
| 12 | Cancellation | cancel a long `wait_for` from the client UI: the call stops, the server keeps running |
| 13 | Restart | quit and reopen the client: it reconnects without leftovers (`pgrep -fl axpilot`) |
| 14 | Agent loop | ★ ask: "Open TextEdit, create a document and type 'hello from AXPilot'" |

## Clients

| Client | Config | Notes to record |
|---|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json`: `{"mcpServers": {"axpilot": {"command": "uv", "args": ["run", "--project", "/ABS/PATH/TO/Mac-Mcp", "axpilot", "stdio", "--engine", "fake"]}}}`; restart Claude | Claude Desktop starts servers with a minimal PATH: use absolute paths (`which uv`). Logs: `~/Library/Logs/Claude/mcp-server-axpilot.log` |
| Claude Code | `claude mcp add axpilot -- uv run --project /ABS/PATH/TO/Mac-Mcp axpilot stdio --engine fake`; check with `claude mcp list` and `/mcp` | also try `--scope project` (`.mcp.json`) |
| Cursor | `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project), `mcpServers` key as for Claude Desktop | Settings → MCP shows status and tools |
| VS Code (Copilot agent mode) | `.vscode/mcp.json`: `{"servers": {"axpilot": {"type": "stdio", "command": "uv", "args": [...]}}}` (note `servers`, not `mcpServers`) | Command Palette → "MCP: List Servers"; tool picker in agent mode |

## Frameworks (no model needed)

| Framework | Command | Pass |
|---|---|---|
| MCP Python SDK | `uv run python spikes/s6_mcp_interop/interop.py` | `overall: ok` in `auto` and `legacy` modes |
| OpenAI Agents SDK | `uv run --no-project --with openai-agents python spikes/s6_mcp_interop/frameworks/agents_sdk_smoke.py` | tools listed, no `strict_schema_failures`, `status_is_error` false |
| LangChain MCP adapters | `uv run --no-project --with langchain-mcp-adapters python spikes/s6_mcp_interop/frameworks/langchain_smoke.py` | every tool has its arguments, `status` returns a result |
| MCP Inspector (CLI) | `npx @modelcontextprotocol/inspector --cli uv run --project /ABS/PATH axpilot stdio --engine fake --method tools/list` | the tool list prints; repeat with `--method tools/call --tool-name status` |

Record each framework's version (`uv pip list` inside the `--with` environment shows it in the
command output, or add `--with <pkg>==<version>` to pin it) and which `mcp` version it resolved:
a framework that pins `mcp<2` is an interop data point in itself.

## Streamable HTTP (once `axpilot http` exists)

```sh
uv run axpilot http --engine fake --port 7788 &        # prints or stores the bearer token
uv run python spikes/s6_mcp_interop/interop.py --http http://127.0.0.1:7788/mcp --token "$TOKEN"
```

Also check that a request without the token, with a foreign `Origin`, or with a foreign `Host`
header is refused (PLAN.md §7.1 threat 5).
