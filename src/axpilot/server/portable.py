"""Portable input schemas: what every MCP client and model provider accepts.

The MCP SDK derives each tool's ``inputSchema`` from its Python signature, which is precise but uses
constructs some function-calling APIs reject or handle badly: ``anyOf: [T, {"type": "null"}]`` for
optional parameters, ``$ref``/``$defs`` for nested models, and ``title`` on every property.
:func:`portable_schema` rewrites the advertised schema into the common subset (OpenAI function
calling, Gemini function declarations, Anthropic tool use):

* an optional parameter advertises its type only: it is optional because it is not ``required``
  (the server still accepts an explicit ``null``);
* ``$ref`` is replaced by the definition, and ``$defs`` is dropped;
* ``title`` keys below the top level and ``default: null`` are dropped (descriptions say what
  matters);
* objects declare ``additionalProperties: false``.

Validation is unchanged: the SDK validates arguments against the function's own model, so the
rewrite only affects what clients are told.
"""

from __future__ import annotations

import copy
from typing import Any

__all__ = ["portable_schema"]

_NULL = {"type": "null"}


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = copy.deepcopy(defs[ref.removeprefix("#/$defs/")])
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return _inline(merged, defs)
    return {key: _inline(value, defs) for key, value in node.items() if key != "$defs"}


def _simplify(node: Any, *, root: bool = False) -> Any:
    if isinstance(node, list):
        return [_simplify(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: value for key, value in node.items()}
    for key in ("anyOf", "oneOf"):
        options = out.get(key)
        if isinstance(options, list) and _NULL in options:
            rest = [option for option in options if option != _NULL]
            if len(rest) == 1:
                del out[key]
                out = {**rest[0], **out}
            else:
                out[key] = rest
    if out.get("default", ...) is None:
        del out["default"]
    if not root:
        out.pop("title", None)
    if "properties" in out:
        out["properties"] = {name: _simplify(value) for name, value in out["properties"].items()}
        out.setdefault("additionalProperties", False)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        if key in out:
            out[key] = _simplify(out[key])
    return out


def portable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The client-facing form of an input ``schema`` (see the module docstring)."""
    defs = schema.get("$defs", {})
    return _simplify(_inline(schema, defs), root=True)
