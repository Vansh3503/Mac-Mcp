"""Evaluating checks: dotted paths into tool results and the comparison operators.

``resolve(data, "apps.*.name")`` returns every value the path reaches: ``*`` expands a list,
digits index one, and ``key[field=value]`` keeps the items of the list ``key`` whose ``field``
equals ``value`` (``windows[title=Notes].frame.2``; dots inside the brackets are fine). Missing
keys simply contribute nothing. :func:`evaluate` applies an operator:
positive operators (``equals``, ``contains``, ``matches``, ``exists``, ``gte``, ``lte``) pass if at
least one value satisfies them, negative ones (``not_equals``, ``not_contains``, ``absent``) only if
every value does. Comparisons never raise: a value of the wrong type simply does not match.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from axpilot.evals.model import NEGATIVE_OPERATORS

__all__ = ["evaluate", "resolve"]


_FILTER = re.compile(r"^(?P<key>[^\[\]]*)\[(?P<field>[^=\]]+)=(?P<value>[^\]]*)\]$")


def _segments(path: str) -> list[str]:
    """Split ``path`` on dots outside brackets."""
    parts, current, depth = [], "", 0
    for char in path:
        if char == "." and depth == 0:
            parts.append(current)
            current = ""
            continue
        depth += {"[": 1, "]": -1}.get(char, 0)
        current += char
    parts.append(current)
    return [part for part in parts if part]


def _matches_filter(item: Any, field: str, wanted: str) -> bool:
    if not isinstance(item, Mapping) or field not in item:
        return False
    value = item[field]
    return value == wanted or (not isinstance(value, str) and str(value).lower() == wanted.lower())


def resolve(data: Any, path: str) -> list[Any]:
    """Every value at ``path`` in ``data`` (an empty path is ``data`` itself)."""
    values = [data]
    for part in _segments(path):
        found: list[Any] = []
        selector = _FILTER.match(part)
        if selector is not None:
            key, field, wanted = selector["key"], selector["field"], selector["value"]
            for value in values:
                items = value.get(key) if key and isinstance(value, Mapping) else (value if not key else None)
                if isinstance(items, Sequence) and not isinstance(items, str):
                    found.extend(item for item in items if _matches_filter(item, field, wanted))
            values = found
            continue
        for value in values:
            if part == "*":
                if isinstance(value, Sequence) and not isinstance(value, str):
                    found.extend(value)
                elif isinstance(value, Mapping):
                    found.extend(value.values())
            elif isinstance(value, Mapping):
                if part in value:
                    found.append(value[part])
            elif isinstance(value, Sequence) and not isinstance(value, str) and part.lstrip("-").isdigit():
                index = int(part)
                if -len(value) <= index < len(value):
                    found.append(value[index])
        values = found
    return values


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _one(op: str, actual: Any, expected: Any) -> bool:
    if op in {"equals", "not_equals"}:
        same = actual == expected or (
            isinstance(expected, str) and not isinstance(actual, str) and _text(actual) == expected
        )
        return same if op == "equals" else not same
    if op in {"contains", "not_contains"}:
        if isinstance(actual, str):
            inside = _text(expected).casefold() in actual.casefold()
        elif isinstance(actual, Sequence):
            inside = expected in actual
        else:
            inside = False
        return inside if op == "contains" else not inside
    if op == "matches":
        return isinstance(actual, str) and re.search(_text(expected), actual) is not None
    if op in {"gte", "lte"}:
        try:
            number, bound = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        return number >= bound if op == "gte" else number <= bound
    raise ValueError(f"unknown operator {op!r}")


def evaluate(op: str, values: list[Any], expected: Any = None) -> bool:
    """Whether ``values`` (from :func:`resolve`) satisfy ``op`` against ``expected``."""
    if op == "exists":
        return bool(values)
    if op == "absent":
        return not values
    if op in NEGATIVE_OPERATORS:
        return all(_one(op, value, expected) for value in values)
    return any(_one(op, value, expected) for value in values)
