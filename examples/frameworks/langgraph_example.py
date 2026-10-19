"""LangChain / LangGraph agent + AXPilot over stdio (via langchain-mcp-adapters).

pip install langchain langgraph langchain-mcp-adapters
python langgraph_example.py
"""

import asyncio

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

TASK = "In Finder, list the five newest files in Downloads."


async def main() -> None:
    client = MultiServerMCPClient(
        {"axpilot": {"command": "axpilot", "args": ["stdio"], "transport": "stdio"}}
    )
    tools = await client.get_tools()
    agent = create_agent(
        model="<your-model>",
        tools=tools,
        system_prompt="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": TASK}]})
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
