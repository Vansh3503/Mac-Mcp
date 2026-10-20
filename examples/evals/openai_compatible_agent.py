"""A model-backed agent for AXPilot's eval suite, over any OpenAI-compatible chat API.

pip install openai
export OPENAI_API_KEY=...            # and OPENAI_BASE_URL for another provider
export AXPILOT_EVAL_MODEL="<your-model>"
cd examples/evals && python -m axpilot.evals run --agent openai_compatible_agent:make \
    --json baseline.json --markdown baseline.md

The agent turns the task's MCP tools into function definitions, lets the model call them through
the eval session (which counts steps and stops at the task's limit), and finishes when the model
replies without calling a tool. The eval suite itself never touches the network; this file does,
because it talks to your model.
"""

import json
import os
from typing import Any

from openai import AsyncOpenAI

from axpilot.evals import AgentReport, EvalSession, Task
from axpilot.server.results import text_of

SYSTEM = (
    "You operate a Mac with the AXPilot tools. Observe before you act, act on element refs, and "
    "verify each step. When the task is done, or cannot be done safely, reply with a short final "
    "answer and no tool call."
)


class OpenAICompatibleAgent:
    def __init__(self, model: str) -> None:
        self.name = f"openai-compatible:{model}"
        self.model = model
        self.client = AsyncOpenAI()

    async def run(self, task: Task, session: EvalSession) -> AgentReport:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.input_schema,
                },
            }
            for t in session.tools
        ]
        messages: list[Any] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task.goal}]
        report = AgentReport(input_tokens=0, output_tokens=0)
        while True:
            response = await self.client.chat.completions.create(
                model=self.model, messages=messages, tools=tools
            )
            if response.usage is not None:
                report.input_tokens = (report.input_tokens or 0) + response.usage.prompt_tokens
                report.output_tokens = (report.output_tokens or 0) + response.usage.completion_tokens
            message = response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                report.final_text = message.content
                return report
            for call in message.tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                result = await session.call(call.function.name, arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text_of(result)})


def make() -> OpenAICompatibleAgent:
    return OpenAICompatibleAgent(os.environ.get("AXPILOT_EVAL_MODEL", "<your-model>"))
