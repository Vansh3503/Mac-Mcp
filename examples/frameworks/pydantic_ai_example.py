"""Pydantic AI + AXPilot over stdio.

pip install pydantic-ai
python pydantic_ai_example.py
"""

import asyncio

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio

axpilot = MCPServerStdio("axpilot", args=["stdio"], timeout=60)
agent = Agent(
    "<your-model>",
    toolsets=[axpilot],
    instructions="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
)


async def main() -> None:
    async with agent:
        result = await agent.run("Switch System Settings to dark mode and confirm it worked.")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
