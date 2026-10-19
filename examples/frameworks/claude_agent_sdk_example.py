"""Claude Agent SDK + AXPilot over stdio.

pip install claude-agent-sdk
python claude_agent_sdk_example.py
"""

import asyncio

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

options = ClaudeAgentOptions(
    model="<your-model>",
    mcp_servers={"axpilot": {"type": "stdio", "command": "axpilot", "args": ["stdio"]}},
    allowed_tools=["mcp__axpilot__*"],
    system_prompt="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
)


async def main() -> None:
    async for message in query(prompt="Rename the newest note in Notes to 'Archive'.", options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)


if __name__ == "__main__":
    asyncio.run(main())
