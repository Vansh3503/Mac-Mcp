"""Google Agent Development Kit (ADK) + AXPilot over stdio.

pip install google-adk
python google_adk_example.py      # or put root_agent in an ADK app folder and use `adk web`
"""

import asyncio

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

root_agent = LlmAgent(
    model="<your-model>",
    name="mac_operator",
    instruction="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
    tools=[
        McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(command="axpilot", args=["stdio"]),
                timeout=60,
            )
        )
    ],
)


async def main() -> None:
    runner = InMemoryRunner(agent=root_agent)
    session = await runner.session_service.create_session(app_name=runner.app_name, user_id="me")
    message = types.Content(role="user", parts=[types.Part(text="What apps are running right now?")])
    async for event in runner.run_async(user_id="me", session_id=session.id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            print(event.content.parts[0].text)


if __name__ == "__main__":
    asyncio.run(main())
