"""OpenAI Agents SDK + AXPilot over stdio.

pip install openai-agents   # and install AXPilot: uv tool install axpilot
python openai_agents_example.py
"""

import asyncio

from agents import Agent, Runner
from agents.mcp import MCPServerStdio

TASK = "Open Notes and tell me the titles of my three most recent notes."


async def main() -> None:
    async with MCPServerStdio(
        name="axpilot",
        params={"command": "axpilot", "args": ["stdio"]},
        client_session_timeout_seconds=60,  # actions may wait for the user to confirm
    ) as axpilot:
        agent = Agent(
            name="Mac operator",
            instructions="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
            model="<your-model>",
            mcp_servers=[axpilot],
        )
        result = await Runner.run(agent, TASK, max_turns=30)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
