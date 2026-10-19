"""LlamaIndex agent workflow + AXPilot over stdio.

pip install llama-index llama-index-tools-mcp llama-index-llms-openai
python llamaindex_example.py
"""

import asyncio

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.llms.openai import OpenAI
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec


async def main() -> None:
    client = BasicMCPClient("axpilot", args=["stdio"])
    tools = await McpToolSpec(client=client).to_tool_list_async()
    agent = FunctionAgent(
        tools=tools,
        llm=OpenAI(model="<your-model>"),
        system_prompt="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
    )
    response = await agent.run("Read the whole Mail inbox and summarize who wrote to me today.")
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
