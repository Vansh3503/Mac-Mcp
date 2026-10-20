"""Eval results as JSON (for tooling and the published baseline) and Markdown (for people)."""

from __future__ import annotations

import platform
from collections import defaultdict
from typing import Any

from axpilot import __version__
from axpilot.evals.runner import SuiteResult, TaskResult

__all__ = ["to_json", "to_markdown"]


def _task_json(result: TaskResult) -> dict[str, Any]:
    return {
        "id": result.task.id,
        "title": result.task.title,
        "tags": list(result.task.tags),
        "success": result.success,
        "skipped": result.skipped,
        "error": result.error,
        "steps": result.steps,
        "failed_calls": result.failed_calls,
        "duration_s": round(result.duration_s, 3),
        "input_tokens": result.report.input_tokens,
        "output_tokens": result.report.output_tokens,
        "checks": [{"passed": c.passed, "detail": c.detail, "note": c.check.note} for c in result.checks],
        "calls": [
            {"tool": c.tool, "error_code": c.error_code, "duration_ms": round(c.duration_ms, 1)}
            for c in result.calls
        ],
    }


def to_json(suite: SuiteResult) -> dict[str, Any]:
    """The whole run: environment, totals and one entry per task (tool arguments are left out:
    they may contain what the agent typed)."""
    ran = suite.ran
    tokens = [r.report.input_tokens or 0 for r in ran], [r.report.output_tokens or 0 for r in ran]
    return {
        "axpilot": __version__,
        "agent": suite.agent,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tasks": len(suite.results),
        "ran": len(ran),
        "passed": suite.passed,
        "success_rate": round(suite.success_rate, 4),
        "steps": sum(r.steps for r in ran),
        "input_tokens": sum(tokens[0]),
        "output_tokens": sum(tokens[1]),
        "results": [_task_json(r) for r in suite.results],
    }


def to_markdown(suite: SuiteResult) -> str:
    """A summary table, the pass rate per tag, and one row per task."""
    ran = suite.ran
    lines = [
        f"# AXPilot eval: {suite.agent}",
        "",
        f"AXPilot {__version__} · {len(ran)} of {len(suite.results)} tasks run · "
        f"**{suite.passed} passed ({suite.success_rate:.0%})** · {sum(r.steps for r in ran)} tool calls",
        "",
    ]
    by_tag: dict[str, list[bool]] = defaultdict(list)
    for result in ran:
        for tag in result.task.tags:
            by_tag[tag].append(result.success)
    if by_tag:
        lines += ["| Tag | Passed |", "|---|---|"]
        lines += [f"| {tag} | {sum(v)}/{len(v)} |" for tag, v in sorted(by_tag.items())]
        lines.append("")
    lines += ["| Task | Result | Steps | Failed calls | Time | Note |", "|---|---|---|---|---|---|"]
    for result in suite.results:
        if result.skipped is not None:
            outcome, note = "skipped", result.skipped
        else:
            outcome = "pass" if result.success else "fail"
            failed = [c.detail for c in result.checks if not c.passed]
            note = result.error or "; ".join(failed)
        note = note.replace("|", "\\|")[:160]
        lines.append(
            f"| `{result.task.id}` | {outcome} | {result.steps} | {result.failed_calls} | "
            f"{result.duration_s:.1f} s | {note} |"
        )
    return "\n".join(lines) + "\n"
