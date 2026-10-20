"""``python -m axpilot.evals``: list the tasks, or run them with an agent.

    python -m axpilot.evals list
    python -m axpilot.evals run                          # the scripted reference agent
    python -m axpilot.evals run --agent mypkg.agent:make --json out.json --markdown out.md
    python -m axpilot.evals run --tasks my-tasks/ --tag safety

``--agent module:factory`` imports ``module`` and calls ``factory()`` for an object with a
``name`` and an async ``run(task, session)`` (see :class:`axpilot.evals.runner.Agent`). Exit
status: 0 when every task that ran passed, 1 otherwise, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio

from axpilot.errors import AXPilotError
from axpilot.evals.model import load_suite
from axpilot.evals.report import to_json, to_markdown
from axpilot.evals.runner import ScriptedAgent, TaskResult, run_suite


def _agent(spec: str) -> Any:
    if spec == "scripted":
        return ScriptedAgent()
    module_name, _, factory = spec.partition(":")
    if not module_name or not factory:
        raise SystemExit("--agent must be 'scripted' or 'module:factory'")
    return getattr(importlib.import_module(module_name), factory)()


def _progress(result: TaskResult) -> None:
    mark = "skip" if result.skipped else ("pass" if result.success else "FAIL")
    print(
        f"{mark:4}  {result.task.id:32} {result.steps:3} steps  {result.duration_s:5.1f} s", file=sys.stderr
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m axpilot.evals", description="AXPilot's eval suite.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "run"):
        command = commands.add_parser(name)
        command.add_argument("--tasks", type=Path, help="Folder of task files (default: the bundled suite).")
        command.add_argument(
            "--tag", action="append", default=[], help="Only tasks with this tag (repeatable)."
        )
    run = commands.choices["run"]
    run.add_argument("--agent", default="scripted", help="'scripted' (default) or module:factory.")
    run.add_argument("--json", type=Path, help="Write the full results as JSON here.")
    run.add_argument("--markdown", type=Path, help="Write a Markdown report here.")
    run.add_argument(
        "--real-desktop", action="store_true", help='Also run desktop = "macos" tasks (drives this Mac).'
    )
    args = parser.parse_args(argv)
    try:
        tasks = load_suite(args.tasks, tags=args.tag)
    except AXPilotError as err:
        print(f"error: {err.message}", file=sys.stderr)
        return 2
    if args.command == "list":
        for task in tasks:
            print(f"{task.id:32} {task.desktop:6} {','.join(task.tags):24} {task.title}")
        return 0
    agent = _agent(args.agent)
    suite = anyio.run(
        lambda: run_suite(tasks, agent, allow_real_desktop=args.real_desktop, progress=_progress)
    )
    if args.json:
        args.json.write_text(json.dumps(to_json(suite), indent=2) + "\n", encoding="utf-8")
    report = to_markdown(suite)
    if args.markdown:
        args.markdown.write_text(report, encoding="utf-8")
    print(report)
    return 0 if suite.passed == len(suite.ran) else 1


if __name__ == "__main__":
    raise SystemExit(main())
