# Agent frameworks

AXPilot is a standard MCP server, so any framework with an MCP client can use it: over **stdio**
(`axpilot stdio`, the framework starts AXPilot as a subprocess) or over **Streamable HTTP**
(`axpilot http`: `http://127.0.0.1:7788/mcp` with a bearer token).

> **Status:** alpha (0.1.0a1 on PyPI). With a checkout instead, replace
> `"command": "axpilot", "args": ["stdio"]` with the from-source command, for example
> `"command": "uv", "args": ["--directory", "/path/to/axpilot", "run", "axpilot", "stdio", "--engine", "fake"]`
> (simulated desktop) or `… "stdio", "--direct"` (your Mac). A runnable example for each framework is
> in [`examples/frameworks/`](https://github.com/vansh3503/Mac-Mcp/tree/main/examples/frameworks)
> (CI checks that they parse; the frameworks are not installed there). Framework APIs change
> quickly; the snippets follow each project's documentation at the time of writing.

## Supported frameworks

| Framework | Language | MCP integration |
|---|---|---|
| OpenAI Agents SDK | Python, TypeScript | `MCPServerStdio` / `MCPServerStreamableHttp` |
| LangChain / LangGraph | Python, TypeScript | `langchain-mcp-adapters` (`MultiServerMCPClient`) |
| Pydantic AI | Python | `MCPServerStdio` |
| Claude Agent SDK | Python, TypeScript | the `mcp_servers` option |
| CrewAI | Python | `MCPServerAdapter` (crewai-tools) |
| Google ADK | Python | `McpToolset` |
| Microsoft Agent Framework | Python, .NET | `MCPStdioTool` |
| LlamaIndex | Python | `BasicMCPClient` + `McpToolSpec` |
| smolagents | Python | `MCPClient` / `ToolCollection.from_mcp` |
| Agno, Strands | Python | `MCPTools` / `MCPClient` |
| Vercel AI SDK, Mastra | TypeScript | AI SDK MCP client / `@mastra/mcp` |
| Local models (Ollama, LM Studio) | n/a | Any framework above, or LM Studio's built-in MCP support; use `--profile minimal` |

## OpenAI Agents SDK

```python
import asyncio

from agents import Agent, Runner
from agents.mcp import MCPServerStdio


async def main() -> None:
    async with MCPServerStdio(
        params={"command": "axpilot", "args": ["stdio"]},
        cache_tools_list=True,
    ) as mac:
        agent = Agent(
            name="Mac operator",
            instructions="Call snapshot before acting, act on element refs, and verify each result.",
            mcp_servers=[mac],
        )
        result = await Runner.run(agent, "Open Notes and create a note titled 'Groceries'")
        print(result.final_output)


asyncio.run(main())
```

## LangChain / LangGraph

```python
import asyncio

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools


async def main() -> None:
    client = MultiServerMCPClient(
        {"axpilot": {"command": "axpilot", "args": ["stdio"], "transport": "stdio"}}
    )
    async with client.session("axpilot") as session:
        agent = create_agent("<your-model>", await load_mcp_tools(session))
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "What app is in front, and what is its window title?"}]}
        )
        print(result["messages"][-1].content)


asyncio.run(main())
```

Load the tools from one `client.session(...)`. `client.get_tools()` on its own starts a new AXPilot
for every tool call, so a `click(ref=…)` never finds the snapshot it came from (`SNAPSHOT_NOT_FOUND`).

## Pydantic AI

```python
import asyncio

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

mac = MCPServerStdio("axpilot", args=["stdio"])
agent = Agent("<your-model>", toolsets=[mac])


async def main() -> None:
    async with agent:
        result = await agent.run("List my open windows.")
        print(result.output)


asyncio.run(main())
```

## Claude Agent SDK

```python
import asyncio

from claude_agent_sdk import ClaudeAgentOptions, query

options = ClaudeAgentOptions(
    mcp_servers={"axpilot": {"command": "axpilot", "args": ["stdio"]}},
    allowed_tools=["mcp__axpilot__snapshot", "mcp__axpilot__find", "mcp__axpilot__status"],
)


async def main() -> None:
    async for message in query(prompt="Describe the frontmost window.", options=options):
        print(message)


asyncio.run(main())
```

## Agno

Agno 3 builds its MCP connections with fastmcp: `pip install agno "fastmcp>=4,<5"`.

```python
import asyncio

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters


async def main() -> None:
    server = StdioServerParameters(command="axpilot", args=["stdio"])
    async with MCPTools(server_params=server, timeout_seconds=60) as mac:
        agent = Agent(model=OpenAIChat(id="<your-model>"), tools=[mac])
        await agent.aprint_response("Open TextEdit and write a two-line shopping list.")


asyncio.run(main())
```

## Strands Agents

```python
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.tools.mcp import MCPClient

mac = MCPClient(lambda: stdio_client(StdioServerParameters(command="axpilot", args=["stdio"])))

with mac:
    agent = Agent(model="<your-model>", tools=mac.list_tools_sync())
    agent("Which apps are running, and which one is in front?")
```

## Tips for agent builders

- **Give the agent the operating guide.** The server sends MCP `instructions` describing the
  observe → act → verify loop. If your framework drops them, load the
  [Agent Skill](https://github.com/vansh3503/Mac-Mcp/blob/main/skills/axpilot/SKILL.md) into your
  system prompt instead (`axpilot skill install` copies it where your tool expects it).
- **Keep one MCP session for the whole run.** Snapshots and their refs live in the session; a
  framework that opens a new session per tool call (LangChain's `get_tools()` without a session)
  loses them between `snapshot` and `click`.
- **Pick a profile.** `minimal` keeps the tool list short for small or local models; `standard` is
  the default; `computer` exposes a single screenshot-and-coordinates tool for vision-first loops.
- **Filter tools further** with `[tools] disable = [...]` in the config, or your framework's own
  tool filter, rather than wrapping AXPilot's tools.
- **Handle error codes.** Results with `isError: true` carry a stable code in `_meta["axpilot/error"]`
  and in the text (`STALE_REF: …`). Retrying `STALE_REF` after a new `snapshot` is safe; retrying
  input actions blindly is not.
- **Expect confirmations.** Destructive tools ask the human first (a native dialog in launcher
  mode). Unattended runs should use a profile without destructive tools, or set
  `safety.confirm = "never"` only in a disposable environment such as a VM.
- **Treat screen content as untrusted.** Web pages, emails and documents can contain instructions
  aimed at your agent. Keep `scripting` and `browser` off unless the task needs them.

## Scripts without an agent

The same tools are a Python automation library. Scripts get the same policy, confirmations, audit
log and error codes as agents:

```python
from axpilot import Desktop

with Desktop.connect() as mac:  # through AXPilot.app; Desktop.fake() for tests
    mac.app("Notes").activate()
    mac.menu("File", "New Note")
    mac.type_text("Groceries\nMilk, eggs")
    for row in mac.read("Notes", mode="rows").rows:
        print(row)
    mac.find("Share", app="Notes").click()
```

`AsyncDesktop` is the async twin. More in
[`examples/scripts/`](https://github.com/vansh3503/Mac-Mcp/tree/main/examples/scripts).

To address one specific element in a script that runs again tomorrow, ask your agent for a
[locator](locators.md) and use `mac.find(locator=LOCATOR).click()`: unlike a text search, it matches
exactly and refuses to guess when several elements match.
