"""The bundled eval suite: every task is solvable on the simulated desktop and every check means
something (it fails before the reference solution, except for safety tasks)."""

from __future__ import annotations

import pytest

from axpilot.evals import ScriptedAgent, load_suite, run_task
from axpilot.evals.model import Task

pytestmark = pytest.mark.anyio

SUITE = load_suite()


def test_the_suite_has_about_thirty_varied_tasks() -> None:
    assert len(SUITE) >= 30
    tags = {tag for task in SUITE for tag in task.tags}
    assert {"safety", "answer", "controls", "windows", "navigation", "vision", "menus", "input"} <= tags
    assert sum(task.safety for task in SUITE) >= 3
    assert all(task.desktop == "demo" for task in SUITE)  # the bundled suite runs anywhere


@pytest.mark.parametrize("task", SUITE, ids=lambda task: task.id)
async def test_the_reference_solution_succeeds(task: Task) -> None:
    result = await run_task(task, ScriptedAgent())
    failed = [check.detail for check in result.checks if not check.passed]
    assert result.success, result.error or failed
    assert result.steps <= task.max_steps


@pytest.mark.parametrize("task", SUITE, ids=lambda task: task.id)
async def test_checks_are_not_satisfied_before_acting(task: Task) -> None:
    result = await run_task(task, ScriptedAgent(), agent_enabled=False)
    if task.safety:
        assert result.success, "a safety task's checks hold when nothing happens"
    else:
        assert not result.success, "the task is already done before the agent acts"
