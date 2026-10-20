"""Strands Agents + AXPilot over stdio.

pip install strands-agents mcp   # and install AXPilot: uv tool install axpilot
python strands_example.py
"""

from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.tools.mcp import MCPClient

axpilot = MCPClient(lambda: stdio_client(StdioServerParameters(command="axpilot", args=["stdio"])))

with axpilot:
    agent = Agent(
        model="<your-model>",
        tools=axpilot.list_tools_sync(),
        system_prompt="You operate the user's Mac with the AXPilot tools. Observe, act, verify.",
    )
    agent("Which apps are running, and which one is in front? Then switch to Finder.")
