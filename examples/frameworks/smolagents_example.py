"""Hugging Face smolagents + AXPilot over stdio.

pip install "smolagents[mcp]"
python smolagents_example.py
"""

from mcp import StdioServerParameters
from smolagents import InferenceClientModel, MCPClient, ToolCallingAgent

server = StdioServerParameters(command="axpilot", args=["stdio"])

with MCPClient(server) as tools:
    agent = ToolCallingAgent(tools=tools, model=InferenceClientModel(model_id="<your-model>"), max_steps=20)
    print(agent.run("Open Calculator and compute 12.5% of 840 using its buttons."))
