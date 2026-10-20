"""AXPilot's eval suite: tasks an agent must complete on a desktop, checked by state.

Run it with ``python -m axpilot.evals run`` (see :mod:`axpilot.evals.__main__`). The bundled
tasks (``axpilot/evals/tasks/*.toml``) use the simulated ``demo`` desktop, so they run anywhere;
a task with ``desktop = "macos"`` drives the real Mac and only runs with ``--real-desktop``.

A task file::

    id = "textedit-bold"
    title = "Turn on bold in TextEdit"
    goal = "In TextEdit, turn on Bold in the format bar."
    tags = ["textedit", "controls"]
    max_steps = 10                     # default 30
    profile = "standard"               # the tool profile the agent gets
    [config]                           # optional config overrides (dotted keys)

    [[check]]                          # every check must hold at the end
    tool = "find"                      # a read-only tool...
    args = { app = "TextEdit", text = "Bold", role = "checkbox" }
    path = "matches.0.value"           # ...and a dotted path into its result (`*`: every item)
    op = "equals"                      # equals, not_equals, contains, not_contains, matches,
    value = "1"                        # exists, absent, gte, lte
    # kind = "answer" tests the agent's final reply instead (no tool, no path)

    [[solution]]                       # the reference solution (the scripted agent replays it)
    tool = "find"
    args = { app = "TextEdit", text = "Bold", role = "checkbox" }
    save = { bold = "matches.0.ref" }  # "$bold" is usable in later steps
    [[solution]]
    tool = "set_value"
    args = { ref = "$bold", value = "on" }
    # expect_error = "BLOCKED_BY_POLICY" for a step that must be refused (safety tasks)

Tasks tagged ``safety`` succeed when nothing harmful happened, so their checks hold from the
start; every other task's checks must fail before its solution runs (CI checks both). The
scripted agent proves the tasks are solvable; a published baseline needs model-backed agents
(module:factory) run by the maintainer with their own API keys.
"""

from __future__ import annotations

from axpilot.evals.model import Check, Step, Task, load_suite, load_task
from axpilot.evals.runner import (
    Agent,
    AgentReport,
    EvalSession,
    ScriptedAgent,
    SuiteResult,
    TaskResult,
    run_suite,
    run_task,
)

__all__ = [
    "Agent",
    "AgentReport",
    "Check",
    "EvalSession",
    "ScriptedAgent",
    "Step",
    "SuiteResult",
    "Task",
    "TaskResult",
    "load_suite",
    "load_task",
    "run_suite",
    "run_task",
]
