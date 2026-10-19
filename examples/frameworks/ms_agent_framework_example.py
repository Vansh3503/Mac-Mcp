"""Microsoft Agent Framework + AXPilot over stdio.

pip install agent-framework
python ms_agent_framework_example.py
"""

import asyncio

from agent_framework import ChatAgent, MCPStdioTool
from agent_framework.openai import OpenAIChatClient


async def main() -> None:
    async with MCPStdioTool(name="axpilot", command="axpilot", args=["stdio"]) as axpilot:
        agent = ChatAgent(
            chat_client=OpenAIChatClient(model_id="<your-model>"),
            name="MacOperator",
            instructions="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
            tools=axpilot,
        )
        result = await agent.run("Which windows are open in Safari, and what are their titles?")
        print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
