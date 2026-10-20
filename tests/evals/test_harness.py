"""The eval harness: task files, checks, the runner, reports and ``python -m axpilot.evals``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError
from axpilot.evals import (
    AgentReport,
    EvalSession,
    ScriptedAgent,
    Task,
    load_suite,
    load_task,
    run_suite,
    run_task,
)
from axpilot.evals.__main__ import main
from axpilot.evals.checks import evaluate, resolve
from axpilot.evals.model import task_from_dict
from axpilot.evals.report import to_json, to_markdown

TASK = {
    "id": "t",
    "title": "T",
    "goal": "Turn on bold.",
    "check": [
        {
            "tool": "find",
            "args": {"app": "TextEdit", "text": "Bold"},
            "path": "matches.0.value",
            "op": "equals",
            "value": "1",
        }
    ],
    "solution": [
        {"tool": "find", "args": {"app": "TextEdit", "text": "Bold"}, "save": {"bold": "matches.0.ref"}},
        {"tool": "set_value", "args": {"ref": "$bold", "value": "on"}},
    ],
}


def task(**changes: Any) -> Task:
    return task_from_dict({**TASK, **changes})


# --------------------------------------------------------------------------- checks
DATA = {
    "apps": [
        {"name": "Notes", "hidden": False, "bundle_id": "com.apple.Notes", "frame": [0, 25, 800, 600]},
        {"name": "Mail", "hidden": True, "bundle_id": "com.apple.mail", "frame": [10, 20, 30, 40]},
    ],
    "count": 2,
    "text": "Hello World",
}


@pytest.mark.parametrize(
    ("path", "values"),
    [
        ("", [DATA]),
        ("count", [2]),
        ("apps.*.name", ["Notes", "Mail"]),
        ("apps.1.name", ["Mail"]),
        ("apps.-1.name", ["Mail"]),
        ("apps.9.name", []),
        ("apps[name=Mail].hidden", [True]),
        ("apps[hidden=true].name", ["Mail"]),
        ("apps[bundle_id=com.apple.Notes].frame.2", [800]),
        ("apps[name=Nope].name", []),
        ("missing.deeper", []),
        ("text.0", []),
        ("count.*", []),
    ],
)
def test_resolve(path: str, values: list[Any]) -> None:
    assert resolve(DATA, path) == values


def test_resolve_wildcards_over_mappings_and_bare_filters() -> None:
    assert resolve({"a": {"x": 1, "y": 2}}, "a.*") == [1, 2]
    assert resolve([{"k": 1}, {"k": 2}, "loose"], "[k=2].k") == [2]


@pytest.mark.parametrize(
    ("op", "values", "expected", "outcome"),
    [
        ("equals", ["a", "b"], "b", True),
        ("equals", [100.0], 100, True),
        ("equals", [True], "True", True),
        ("equals", [], "a", False),
        ("not_equals", ["a", "b"], "c", True),
        ("not_equals", ["a", "b"], "a", False),
        ("not_equals", [], "a", True),
        ("contains", ["Hello World"], "world", True),
        ("contains", [["checked", "focused"]], "checked", True),
        ("contains", [3], "3", False),
        ("not_contains", ["rm -rf ~"], "rm -rf", False),
        ("not_contains", ["ls"], "rm -rf", True),
        ("matches", ["New Note — 10:45"], "^New Note — ", True),
        ("matches", [5], "5", False),
        ("exists", [None], None, True),
        ("exists", [], None, False),
        ("absent", [], None, True),
        ("gte", ["12"], 10, True),
        ("lte", [12], 10, False),
        ("gte", ["many"], 10, False),
    ],
)
def test_evaluate(op: str, values: list[Any], expected: Any, outcome: bool) -> None:
    assert evaluate(op, values, expected) is outcome


def test_an_unknown_operator_is_a_bug() -> None:
    with pytest.raises(ValueError, match="unknown operator"):
        evaluate("between", [1], 2)


# --------------------------------------------------------------------------- task files
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"colour": "red"}, "unknown key"),
        ({"goal": " "}, "non-empty `goal`"),
        ({"desktop": "windows"}, "desktop must be"),
        ({"check": []}, "at least one"),
        ({"check": ["x"]}, "must be a table"),
        ({"check": [{"tool": "find", "op": "near", "value": 1}]}, "op must be"),
        ({"check": [{"tool": "find", "op": "equals"}]}, "needs `value`"),
        ({"check": [{"op": "equals", "value": 1}]}, "needs `tool`"),
        ({"check": [{"kind": "guess", "op": "exists"}]}, 'kind must be "tool" or "answer"'),
        ({"check": [{"tool": "find", "op": "exists", "args": "x"}]}, "must be a table"),
        ({"check": [{"tool": "find", "op": "exists", "extra": 1}]}, "unknown key"),
        ({"solution": [{"args": {}}]}, "needs `tool`"),
        ({"solution": ["x"]}, "must be a table"),
        ({"solution": [{"tool": "x", "save": {"a": 1}}]}, "result paths"),
        ({"solution": [{"tool": "x", "bogus": 1}]}, "unknown key"),
        ({"max_steps": 0}, "max_steps"),
    ],
)
def test_invalid_tasks_are_refused_with_the_field(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(AXPilotError, match=message):
        task(**changes)


def test_task_files_and_suites(tmp_path: Path) -> None:
    (tmp_path / "a.toml").write_text(
        'id = "a"\ntitle = "A"\ngoal = "g"\ntags = ["x"]\n[[check]]\nkind = "answer"\nop = "exists"\n',
        encoding="utf-8",
    )
    (tmp_path / "b.toml").write_text(
        'id = "b"\ntitle = "B"\ngoal = "g"\n[[check]]\nkind = "answer"\nop = "exists"\n', encoding="utf-8"
    )
    loaded = load_task(tmp_path / "a.toml")
    assert loaded.path == tmp_path / "a.toml"
    assert [t.id for t in load_suite(tmp_path)] == ["a", "b"]
    assert [t.id for t in load_suite(tmp_path, tags=["x"])] == ["a"]
    (tmp_path / "c.toml").write_text(
        'id = "a"\ntitle = "C"\ngoal = "g"\n[[check]]\nkind = "answer"\nop = "exists"\n', encoding="utf-8"
    )
    with pytest.raises(AXPilotError, match="duplicate task id"):
        load_suite(tmp_path)
    (tmp_path / "bad.toml").write_text("id = ", encoding="utf-8")
    with pytest.raises(AXPilotError, match="cannot read the task"):
        load_task(tmp_path / "bad.toml")


# --------------------------------------------------------------------------- the runner
class Crashing:
    name = "crashing"

    async def run(self, task: Task, session: EvalSession) -> AgentReport:
        raise RuntimeError("model API unreachable")


class Busy:
    name = "busy"

    async def run(self, task: Task, session: EvalSession) -> AgentReport:
        while True:
            await session.call("status")


class Talker:
    name = "talker"

    async def run(self, task: Task, session: EvalSession) -> AgentReport:
        assert {tool.name for tool in session.tools} >= {"snapshot", "click"}
        return AgentReport(final_text="There are 14 notes.", input_tokens=100, output_tokens=7)


@pytest.mark.anyio
async def test_a_crashing_agent_fails_its_task_only() -> None:
    result = await run_task(task(), Crashing())
    assert not result.success
    assert result.error == "RuntimeError: model API unreachable"


@pytest.mark.anyio
async def test_the_step_limit_stops_an_agent() -> None:
    result = await run_task(task(max_steps=3), Busy())
    assert not result.success
    assert result.steps == 3
    assert "all 3 steps" in (result.error or "")


@pytest.mark.anyio
async def test_answer_checks_and_tokens() -> None:
    answer_task = task(check=[{"kind": "answer", "op": "contains", "value": "14"}], solution=[])
    result = await run_task(answer_task, Talker())
    assert result.success
    assert result.report.input_tokens == 100


@pytest.mark.anyio
async def test_checks_must_read_and_failing_check_tools_fail() -> None:
    writes = task(check=[{"tool": "click", "args": {"x": 1, "y": 1}, "op": "exists"}])
    result = await run_task(writes, ScriptedAgent())
    assert "not a read-only tool" in result.checks[0].detail
    broken = task(check=[{"tool": "find", "args": {"app": "NoSuchApp", "text": "x"}, "op": "exists"}])
    result = await run_task(broken, ScriptedAgent())
    assert not result.success
    assert "find failed" in result.checks[0].detail


@pytest.mark.anyio
async def test_a_reference_solution_that_goes_wrong_is_reported() -> None:
    wrong_error = task(solution=[{"tool": "status", "expect_error": "BLOCKED_BY_POLICY"}])
    result = await run_task(wrong_error, ScriptedAgent())
    assert "ended with success, not BLOCKED_BY_POLICY" in (result.error or "")
    nothing_saved = task(solution=[{"tool": "status", "save": {"x": "no.such.path"}}])
    result = await run_task(nothing_saved, ScriptedAgent())
    assert "nothing at 'no.such.path'" in (result.error or "")


@pytest.mark.anyio
async def test_real_desktop_tasks_need_permission() -> None:
    mac_task = task(desktop="macos")
    skipped = await run_task(mac_task, ScriptedAgent())
    assert skipped.skipped is not None
    assert not skipped.checks
    allowed = await run_task(
        mac_task, ScriptedAgent(), allow_real_desktop=True, engine_factory=lambda t: FakeEngine.demo()
    )
    assert allowed.success  # with a stand-in engine; the real one needs a Mac


@pytest.mark.anyio
async def test_suites_and_reports() -> None:
    seen: list[str] = []
    suite = await run_suite(
        [
            task(),
            task(id="fails", max_steps=1, tags=["controls"]),
            task(id="mac", desktop="macos", tags=["controls"]),
        ],
        ScriptedAgent(),
        progress=lambda result: seen.append(result.task.id),
    )
    assert seen == ["t", "fails", "mac"]
    assert suite.passed == 1
    assert len(suite.ran) == 2
    assert suite.success_rate == 0.5
    data = to_json(suite)
    assert data["passed"] == 1
    assert data["results"][2]["skipped"]
    assert "args" not in data["results"][0]["calls"][0]  # what the agent typed stays out
    text = to_markdown(suite)
    assert "**1 passed (50%)**" in text
    assert "| controls | 0/1 |" in text
    assert "| `mac` | skipped |" in text


@pytest.mark.anyio
async def test_an_empty_suite_has_no_rate() -> None:
    suite = await run_suite([], ScriptedAgent())
    assert suite.success_rate == 0.0
    assert "0 of 0 tasks" in to_markdown(suite)


# --------------------------------------------------------------------------- the command line
def test_cli_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--tag", "safety"]) == 0
    out = capsys.readouterr().out
    assert "safety-terminal-command" in out
    assert "notes-search" not in out


def test_cli_run_writes_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["run", "--tag", "windows", "--json", str(tmp_path / "r.json"), "--markdown", str(tmp_path / "r.md")]
    )
    assert code == 0
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["passed"] >= 3
    assert "AXPilot eval: scripted" in (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "pass" in capsys.readouterr().err


def test_cli_with_an_agent_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "my_agent.py").write_text(
        "from axpilot.evals import AgentReport\n"
        "class Lazy:\n"
        "    name = 'lazy'\n"
        "    async def run(self, task, session):\n"
        "        return AgentReport()\n"
        "def make():\n"
        "    return Lazy()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert main(["run", "--tag", "windows", "--agent", "my_agent:make"]) == 1  # does nothing: fails
    sys.modules.pop("my_agent", None)
    with pytest.raises(SystemExit, match="module:factory"):
        main(["run", "--agent", "nocolon"])


def test_cli_reports_bad_task_folders(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "x.toml").write_text("id = 1", encoding="utf-8")
    assert main(["list", "--tasks", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_the_model_agent_example_is_valid_python() -> None:
    import ast

    path = Path(__file__).resolve().parents[2] / "examples" / "evals" / "openai_compatible_agent.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    assert ast.get_docstring(tree)
    assert "<your-model>" in text
    assert any(isinstance(node, ast.FunctionDef) and node.name == "make" for node in tree.body)
