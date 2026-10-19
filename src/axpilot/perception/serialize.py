"""JSON round-trip for raw accessibility trees (fixtures and ``axpilot debug dump-tree``).

Format::

    {"app": {"name": "Notes", "pid": 812, "bundle_id": "com.apple.Notes"},
     "window": {"window_id": 101, "title": "Notes", "frame": [0, 25, 1440, 875]},
     "root": {"role": "AXWindow", "title": "Notes", "frame": [0, 25, 1440, 875], "children": [...]},
     "menu_bar": {...}}

Node keys mirror :class:`~axpilot.models.RawNode` field names, ``frame`` is ``[x, y, w, h]``
and omitted keys take the dataclass defaults. Every field round-trips except ``handle``;
:func:`tree_from_dict` assigns fresh string handles ``"n0"``, ``"n1"``, … in pre-order
(root first, then the menu bar).
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from collections.abc import Callable, Collection, Mapping
from typing import Any

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo

NodeHook = Callable[[RawNode, Mapping[str, Any]], None]

_NODE_FIELDS: dict[str, dataclasses.Field[Any]] = {
    f.name: f for f in dataclasses.fields(RawNode) if f.name != "handle"
}
_BOOL_FIELDS = frozenset(
    name for name in ("enabled", "focused", "selected", "secure", "visible") if name in _NODE_FIELDS
)
_STR_FIELDS = frozenset(
    ("subrole", "title", "description", "help", "identifier", "placeholder", "label", "url")
)
_APP_KEYS = frozenset(f.name for f in dataclasses.fields(AppInfo))
_WINDOW_KEYS = frozenset(f.name for f in dataclasses.fields(WindowInfo))
_TREE_KEYS = frozenset({"app", "window", "root", "menu_bar", "node_count", "truncated", "elapsed_ms"})


def _invalid(message: str) -> AXPilotError:
    return AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        message,
        hint="Tree JSON uses RawNode field names; see axpilot.perception.serialize.",
    )


def _default(field: dataclasses.Field[Any]) -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        return field.default_factory()
    return dataclasses.MISSING


def _number(value: float) -> int | float:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def rect_to_list(rect: Rect) -> list[int | float]:
    """``[x, y, w, h]`` without rounding (integral floats are written as ints)."""
    return [_number(rect.x), _number(rect.y), _number(rect.w), _number(rect.h)]


def rect_from_list(value: Any, where: str = "frame") -> Rect:
    """Parse ``[x, y, w, h]``; raises ``INVALID_ARGUMENT`` if malformed."""
    if (
        not isinstance(value, list | tuple)
        or len(value) != 4
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
        or not all(math.isfinite(v) for v in value)
    ):
        raise _invalid(f"{where} must be [x, y, w, h] numbers, got {value!r}.")
    x, y, w, h = value
    return Rect(x, y, w, h)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


# --------------------------------------------------------------------------- nodes
def node_to_dict(node: RawNode) -> dict[str, Any]:
    """Serialize a node and its subtree, omitting fields that equal their defaults."""
    out: dict[str, Any] = {"role": node.role}
    for name, field in _NODE_FIELDS.items():
        if name in ("role", "children"):
            continue
        value = getattr(node, name)
        if value == _default(field):
            continue
        if name == "frame":
            out[name] = rect_to_list(value)
        elif name == "actions":
            out[name] = list(value)
        elif name == "value":
            out[name] = _json_value(value)
        else:
            out[name] = value
    if node.children:
        out["children"] = [node_to_dict(child) for child in node.children]
    return out


def _check_type(name: str, value: Any, where: str) -> None:
    if name in _BOOL_FIELDS and not isinstance(value, bool):
        raise _invalid(f"{where}.{name} must be true or false, got {value!r}.")
    if name in _STR_FIELDS and value is not None and not isinstance(value, str):
        raise _invalid(f"{where}.{name} must be a string, got {value!r}.")
    if name == "expanded" and value is not None and not isinstance(value, bool):
        raise _invalid(f"{where}.expanded must be true, false or null, got {value!r}.")
    if (
        name == "child_count"
        and value is not None
        and (not isinstance(value, int) or isinstance(value, bool))
    ):
        raise _invalid(f"{where}.child_count must be an integer, got {value!r}.")


def node_from_dict(
    data: Mapping[str, Any],
    *,
    extra_keys: Collection[str] = (),
    on_node: NodeHook | None = None,
    where: str = "root",
) -> RawNode:
    """Parse a node dict (recursively). Unknown keys raise ``INVALID_ARGUMENT`` unless listed in
    ``extra_keys``; ``on_node(node, data)`` is called for every node, children first."""
    if not isinstance(data, Mapping):
        raise _invalid(f"{where} must be an object, got {type(data).__name__}.")
    role = data.get("role")
    if not isinstance(role, str) or not role:
        raise _invalid(f"{where}.role must be a non-empty string.")
    unknown = [key for key in data if key not in _NODE_FIELDS and key not in extra_keys]
    if unknown:
        raise _invalid(f"{where} has unknown key(s): {', '.join(sorted(map(str, unknown)))}.")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        if name not in _NODE_FIELDS or name in ("role", "children"):
            continue
        _check_type(name, value, where)
        if name == "frame":
            kwargs[name] = None if value is None else rect_from_list(value, f"{where}.frame")
        elif name == "actions":
            if not isinstance(value, list | tuple) or not all(isinstance(a, str) for a in value):
                raise _invalid(f"{where}.actions must be a list of strings.")
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = value
    children_data = data.get("children", [])
    if not isinstance(children_data, list):
        raise _invalid(f"{where}.children must be a list.")
    children = [
        node_from_dict(child, extra_keys=extra_keys, on_node=on_node, where=f"{where}.children[{index}]")
        for index, child in enumerate(children_data)
    ]
    node = RawNode(role=role, children=children, **kwargs)
    if on_node is not None:
        on_node(node, data)
    return node


# --------------------------------------------------------------------------- apps & windows
def app_to_dict(app: AppInfo) -> dict[str, Any]:
    """Serialize an app, omitting defaults (``name`` and ``pid`` are always present)."""
    out: dict[str, Any] = {"name": app.name, "pid": app.pid}
    for field in dataclasses.fields(AppInfo):
        value = getattr(app, field.name)
        if field.name not in out and value != _default(field):
            out[field.name] = value
    return out


def app_from_dict(data: Any, where: str = "app") -> AppInfo:
    """Parse an app dict; raises ``INVALID_ARGUMENT`` if malformed."""
    if not isinstance(data, Mapping):
        raise _invalid(f"{where} must be an object.")
    unknown = sorted(str(key) for key in data if key not in _APP_KEYS)
    if unknown:
        raise _invalid(f"{where} has unknown key(s): {', '.join(unknown)}.")
    if not isinstance(data.get("name"), str) or not isinstance(data.get("pid"), int):
        raise _invalid(f"{where} needs a string 'name' and an integer 'pid'.")
    return AppInfo(**dict(data))


def window_to_dict(window: WindowInfo, app: AppInfo | None = None) -> dict[str, Any]:
    """Serialize a window; ``pid``/``app``/``bundle_id`` are omitted when they match ``app``."""
    out: dict[str, Any] = {
        "window_id": window.window_id,
        "title": window.title,
        "frame": rect_to_list(window.frame),
    }
    inherited = {"pid": app.pid, "app": app.name, "bundle_id": app.bundle_id} if app else {}
    for field in dataclasses.fields(WindowInfo):
        if field.name in out:
            continue
        value = getattr(window, field.name)
        if field.name in inherited:
            if value != inherited[field.name]:
                out[field.name] = value
        elif value != _default(field):
            out[field.name] = value
    return out


def window_from_dict(data: Any, app: AppInfo | None = None, where: str = "window") -> WindowInfo:
    """Parse a window dict, filling ``pid``/``app``/``bundle_id`` from ``app`` when omitted."""
    if not isinstance(data, Mapping):
        raise _invalid(f"{where} must be an object.")
    unknown = sorted(str(key) for key in data if key not in _WINDOW_KEYS)
    if unknown:
        raise _invalid(f"{where} has unknown key(s): {', '.join(unknown)}.")
    if not isinstance(data.get("window_id"), int):
        raise _invalid(f"{where}.window_id must be an integer.")
    kwargs = dict(data)
    kwargs["frame"] = rect_from_list(data.get("frame"), f"{where}.frame")
    kwargs.setdefault("title", "")
    if app is not None:
        kwargs.setdefault("pid", app.pid)
        kwargs.setdefault("app", app.name)
        kwargs.setdefault("bundle_id", app.bundle_id)
    if "pid" not in kwargs or "app" not in kwargs:
        raise _invalid(f"{where} needs 'pid' and 'app' when no app is given.")
    return WindowInfo(**kwargs)


# --------------------------------------------------------------------------- trees
def tree_to_dict(tree: RawTree) -> dict[str, Any]:
    """Serialize a tree (everything except engine handles)."""
    out: dict[str, Any] = {
        "app": app_to_dict(tree.app),
        "window": window_to_dict(tree.window, tree.app) if tree.window is not None else None,
        "root": node_to_dict(tree.root),
    }
    if tree.menu_bar is not None:
        out["menu_bar"] = node_to_dict(tree.menu_bar)
    if tree.node_count:
        out["node_count"] = tree.node_count
    if tree.truncated:
        out["truncated"] = True
    if tree.elapsed_ms:
        out["elapsed_ms"] = tree.elapsed_ms
    return out


def assign_handles(*roots: RawNode | None, prefix: str = "n") -> None:
    """Give every node a string handle ``f"{prefix}{index}"`` in pre-order across ``roots``."""
    counter = itertools.count()
    for root in roots:
        if root is not None:
            for node in root.walk():
                node.handle = f"{prefix}{next(counter)}"


def tree_from_dict(data: dict[str, Any]) -> RawTree:
    """Parse a tree dict produced by :func:`tree_to_dict` (or written by hand).

    Raises ``INVALID_ARGUMENT`` with the offending location for malformed input."""
    if not isinstance(data, Mapping):
        raise _invalid("A tree must be a JSON object.")
    unknown = sorted(str(key) for key in data if key not in _TREE_KEYS)
    if unknown:
        raise _invalid(f"Tree has unknown key(s): {', '.join(unknown)}.")
    if "root" not in data:
        raise _invalid("Tree needs a 'root' node.")
    app = app_from_dict(data.get("app"))
    window_data = data.get("window")
    window = window_from_dict(window_data, app) if window_data is not None else None
    root = node_from_dict(data["root"], where="root")
    menu_data = data.get("menu_bar")
    menu_bar = node_from_dict(menu_data, where="menu_bar") if menu_data is not None else None
    assign_handles(root, menu_bar)
    node_count = data.get("node_count", 0)
    elapsed_ms = data.get("elapsed_ms", 0.0)
    truncated = data.get("truncated", False)
    if (
        not isinstance(node_count, int)
        or not isinstance(elapsed_ms, int | float)
        or not isinstance(truncated, bool)
    ):
        raise _invalid("node_count must be an integer, elapsed_ms a number and truncated a boolean.")
    return RawTree(
        app=app,
        window=window,
        root=root,
        menu_bar=menu_bar,
        node_count=node_count,
        truncated=truncated,
        elapsed_ms=float(elapsed_ms),
    )
