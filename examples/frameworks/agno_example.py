"""Agno + AXPilot over stdio.

pip install agno "fastmcp>=4,<5" openai   # Agno 3 connects through fastmcp; and install AXPilot
python agno_example.py
"""

import asyncio

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters

TASK = "Open TextEdit, write a two-line shopping list, then read the document back to me."


async def main() -> None:
    server = StdioServerParameters(command="axpilot", args=["stdio"])
    # Actions may wait for the user to confirm, so allow more than the default timeout.
    async with MCPTools(server_params=server, timeout_seconds=60) as axpilot:
        agent = Agent(
            model=OpenAIChat(id="<your-model>"),
            tools=[axpilot],
            instructions="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
        )
        await agent.aprint_response(TASK)


if __name__ == "__main__":
    asyncio.run(main())
