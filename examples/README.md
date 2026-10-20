# AXPilot examples

## Automation scripts (`scripts/`)

Plain Python scripts built on the automation SDK (`from axpilot import Desktop`). Each one runs
against the simulated desktop with `--fake`, so you can try it anywhere, and against your Mac
through AXPilot.app without it (run `axpilot setup` once; the host asks you to allow the script
the first time).

| Script | What it does |
|---|---|
| `export_notes.py` | Exports the Notes list to Markdown with `read`, which scrolls through long lists for you |
| `new_note.py` | Creates a note through the menu bar and checks that the list grew |
| `collect_inbox.py` | Saves every line of the Mail inbox to a CSV file and says whether it got all of them |
| `fill_form.py` | Fills an email field and a checkbox in one call (password fields stay yours) |
| `annotated_screenshot.py` | Saves a window screenshot with the element refs drawn on it |

```sh
uv run python examples/scripts/export_notes.py --fake
uv run python examples/scripts/collect_inbox.py --fake --out inbox.csv
```

Scripts get the same tools, safety policy, confirmations, audit log and error codes as AI agents.
`Desktop.fake()` makes them testable in CI; `Desktop.connect()` uses AXPilot.app's permissions,
and `Desktop.direct()` runs in your own process (which then needs the macOS permissions itself).

## Agent frameworks (`frameworks/`)

Minimal agents that start AXPilot with `axpilot stdio` and give a model its tools. Replace
`<your-model>` with a model your framework supports and set its API key the way the framework
expects.

| File | Framework |
|---|---|
| `openai_agents_example.py` | OpenAI Agents SDK |
| `langgraph_example.py` | LangChain / LangGraph (`langchain-mcp-adapters`) |
| `crewai_example.py` | CrewAI (`crewai-tools` MCP adapter) |
| `google_adk_example.py` | Google Agent Development Kit |
| `pydantic_ai_example.py` | Pydantic AI |
| `llamaindex_example.py` | LlamaIndex (`llama-index-tools-mcp`) |
| `smolagents_example.py` | Hugging Face smolagents |
| `ms_agent_framework_example.py` | Microsoft Agent Framework |
| `claude_agent_sdk_example.py` | Claude Agent SDK |
| `agno_example.py` | Agno (`MCPTools`) |
| `strands_example.py` | Strands Agents (`MCPClient`) |
| `ts/ai_sdk_example.ts` | Vercel AI SDK (`@ai-sdk/mcp`) |
| `ts/mastra_example.ts` | Mastra (`@mastra/mcp`) |

The files end in `_example` so that running them does not shadow the framework's own package.
These frameworks move quickly: the examples follow their documented MCP integrations at the time
of writing, and CI only checks that they parse. If an import has moved, the framework's MCP guide
has the current name.

Every action an agent takes goes through AXPilot's policy: protected apps stay off limits,
destructive actions ask you first, and the audit log records each call.
