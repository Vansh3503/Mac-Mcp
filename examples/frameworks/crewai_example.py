"""CrewAI + AXPilot over stdio (via crewai-tools' MCP adapter).

pip install crewai "crewai-tools[mcp]"
python crewai_example.py
"""

from crewai import Agent, Crew, Task
from crewai_tools import MCPServerAdapter
from mcp import StdioServerParameters

server = StdioServerParameters(command="axpilot", args=["stdio"])

with MCPServerAdapter(server) as axpilot_tools:
    operator = Agent(
        role="Mac operator",
        goal="Complete tasks in the user's Mac apps reliably",
        backstory="You drive macOS apps through their accessibility tree and verify every step.",
        tools=axpilot_tools,
        llm="<your-model>",
    )
    task = Task(
        description="Create a note in Notes titled 'Standup' with three bullet points for today.",
        expected_output="Confirmation that the note exists, with its title.",
        agent=operator,
    )
    print(Crew(agents=[operator], tasks=[task]).kickoff())
