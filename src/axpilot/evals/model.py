"""Eval tasks: what the agent is asked, where, and how success is checked.

A task is a TOML file (see :mod:`axpilot.evals` for the format). :func:`load_task` validates it
completely before anything runs: unknown keys, a check with an unknown operator, or a solution
step without a tool are errors that name the file and the field.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["OPERATORS", "Check", "Step", "Task", "load_suite", "load_task"]

Operator = Literal[
    "equals", "not_equals", "contains", "not_contains", "matches", "exists", "absent", "gte", "lte"
]
OPERATORS: frozenset[str] = frozenset(Operator.__args__)  # type: ignore[attr-defined]
NEGATIVE_OPERATORS = frozenset({"not_equals", "not_contains", "absent"})
"""Operators that must hold for every value a path finds (the others: for at least one)."""
DESKTOPS = frozenset({"demo", "macos"})
_TASK_KEYS = frozenset(
    {
        "id",
        "title",
        "goal",
        "desktop",
        "profile",
        "tags",
        "max_steps",
        "config",
        "check",
        "solution",
        "answer",
    }
)
_CHECK_KEYS = frozenset({"tool", "args", "path", "op", "value", "kind", "note"})
_STEP_KEYS = frozenset({"tool", "args", "save", "expect_error"})


@dataclass(frozen=True, slots=True)
class Check:
    """One success condition.

    ``kind = "tool"`` (the default) calls the read-only ``tool`` with ``args`` and tests the value
    at ``path`` in its structured result; ``kind = "answer"`` tests the agent's final reply
    (``path`` is ignored). ``path`` is dotted (``matches.0.value``); ``*`` stands for every item of
    a list (``apps.*.name``)."""

    op: str
    value: Any = None
    tool: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    path: str = ""
    kind: Literal["tool", "answer"] = "tool"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Step:
    """One tool call of a reference solution. ``save`` keeps values of its result for later
    steps (``{"note": "matches.0.ref"}`` makes ``"$note"`` usable in later arguments);
    ``expect_error`` is the error code the call must end with (safety tasks)."""

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    save: Mapping[str, str] = field(default_factory=dict)
    expect_error: str | None = None


@dataclass(frozen=True, slots=True)
class Task:
    """One eval task (see the module docstring of :mod:`axpilot.evals`)."""

    id: str
    title: str
    goal: str
    checks: tuple[Check, ...]
    solution: tuple[Step, ...] = ()
    answer: str | None = None
    """The reference solution's final reply (for ``answer`` checks)."""
    desktop: str = "demo"
    profile: str = "standard"
    tags: tuple[str, ...] = ()
    max_steps: int = 30
    config: Mapping[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def safety(self) -> bool:
        """A safety task succeeds when nothing harmful happened (its checks hold from the start)."""
        return "safety" in self.tags


def _bad(where: str, message: str) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, f"{where}: {message}")


def _keys(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _bad(where, f"unknown key(s) {', '.join(unknown)}")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _bad(where, "must be a table")
    return value


def _check(data: Any, where: str) -> Check:
    if not isinstance(data, Mapping):
        raise _bad(where, "must be a table")
    _keys(data, _CHECK_KEYS, where)
    op, kind = data.get("op"), data.get("kind", "tool")
    if op not in OPERATORS:
        raise _bad(where, f"op must be one of {', '.join(sorted(OPERATORS))}")
    if kind not in {"tool", "answer"}:
        raise _bad(where, 'kind must be "tool" or "answer"')
    if kind == "tool" and not isinstance(data.get("tool"), str):
        raise _bad(where, "a tool check needs `tool`")
    if op not in {"exists", "absent"} and "value" not in data:
        raise _bad(where, f"op {op!r} needs `value`")
    return Check(
        op=op,
        value=data.get("value"),
        tool=data.get("tool"),
        args=_mapping(data.get("args"), f"{where}.args"),
        path=str(data.get("path", "")),
        kind=kind,
        note=data.get("note"),
    )


def _step(data: Any, where: str) -> Step:
    if not isinstance(data, Mapping):
        raise _bad(where, "must be a table")
    _keys(data, _STEP_KEYS, where)
    if not isinstance(data.get("tool"), str):
        raise _bad(where, "needs `tool`")
    save = _mapping(data.get("save"), f"{where}.save")
    if not all(isinstance(v, str) for v in save.values()):
        raise _bad(f"{where}.save", "values are result paths (strings)")
    return Step(
        tool=data["tool"],
        args=_mapping(data.get("args"), f"{where}.args"),
        save=save,
        expect_error=data.get("expect_error"),
    )


def task_from_dict(data: Mapping[str, Any], where: str = "task", path: Path | None = None) -> Task:
    """Build and validate a :class:`Task` from parsed TOML."""
    _keys(data, _TASK_KEYS, where)
    for key in ("id", "title", "goal"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise _bad(where, f"needs a non-empty `{key}`")
    desktop = data.get("desktop", "demo")
    if desktop not in DESKTOPS:
        raise _bad(where, f"desktop must be one of {', '.join(sorted(DESKTOPS))}")
    checks = data.get("check")
    if not isinstance(checks, Sequence) or not checks:
        raise _bad(where, "needs at least one [[check]]")
    steps = data.get("solution", [])
    max_steps = data.get("max_steps", 30)
    if not isinstance(max_steps, int) or not 1 <= max_steps <= 200:
        raise _bad(where, "max_steps must be an integer from 1 to 200")
    return Task(
        id=data["id"],
        title=data["title"],
        goal=data["goal"].strip(),
        checks=tuple(_check(c, f"{where}.check[{n}]") for n, c in enumerate(checks)),
        solution=tuple(_step(s, f"{where}.solution[{n}]") for n, s in enumerate(steps)),
        answer=data.get("answer"),
        desktop=desktop,
        profile=str(data.get("profile", "standard")),
        tags=tuple(str(t) for t in data.get("tags", ())),
        max_steps=max_steps,
        config=_mapping(data.get("config"), f"{where}.config"),
        path=path,
    )


def load_task(path: Path) -> Task:
    """Load one task file. Raises ``INVALID_ARGUMENT`` naming the file and field."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _bad(str(path), f"cannot read the task: {exc}") from exc
    return task_from_dict(data, where=path.name, path=path)


def load_suite(folder: Path | None = None, *, tags: Sequence[str] = ()) -> list[Task]:
    """Every ``*.toml`` task in ``folder`` (default: the bundled suite), sorted by id, optionally
    only those with one of ``tags``. Task ids must be unique."""
    root = folder or Path(__file__).resolve().parent / "tasks"
    tasks = sorted((load_task(p) for p in root.glob("*.toml")), key=lambda t: t.id)
    ids = [t.id for t in tasks]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise _bad(str(root), f"duplicate task id(s) {', '.join(duplicates)}")
    if tags:
        tasks = [t for t in tasks if set(t.tags) & set(tags)]
    return tasks
