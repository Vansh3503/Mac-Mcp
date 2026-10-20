"""LangChain / LangGraph agent + AXPilot over stdio (via langchain-mcp-adapters).

pip install langchain langgraph langchain-mcp-adapters
python langgraph_example.py
"""

import asyncio

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

TASK = "In Finder, list the five newest files in Downloads."


async def main() -> None:
    client = MultiServerMCPClient(
        {"axpilot": {"command": "axpilot", "args": ["stdio"], "transport": "stdio"}}
    )
    # One session for the whole run: snapshot refs live in it. `client.get_tools()` alone starts a
    # new AXPilot for every tool call, so `click(ref=…)` would never find the snapshot.
    async with client.session("axpilot") as session:
        agent = create_agent(
            model="<your-model>",
            tools=await load_mcp_tools(session),
            system_prompt="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
        )
        result = await agent.ainvoke({"messages": [{"role": "user", "content": TASK}]})
        print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
