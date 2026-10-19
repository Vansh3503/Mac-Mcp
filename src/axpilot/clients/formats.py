"""Rendering and text-level editing for the config formats MCP clients use.

* JSON: :func:`dump_json` keeps the file's indentation style (:func:`detect_indent`).
* TOML: :func:`append_toml_table` / :func:`remove_toml_table` edit the text of a file so that
  comments and formatting elsewhere survive. Callers must check the result by parsing it (see
  :func:`toml_text_matches`), and fall back to a full rewrite if it does not match.
* YAML: :func:`dump_yaml` is a small emitter for the plain data AXPilot writes (mappings, lists,
  strings, numbers, booleans). Every string is double-quoted, so no value can be misread as
  another type. AXPilot never needs to *parse* YAML.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Mapping, Sequence
from typing import Any

import tomli_w

__all__ = [
    "append_toml_table",
    "detect_indent",
    "dump_json",
    "dump_toml",
    "dump_yaml",
    "nest",
    "remove_toml_table",
    "toml_has_comments",
    "toml_text_matches",
]

# --------------------------------------------------------------------------------------------
# Shared


def nest(keys: Sequence[str], leaf: Any) -> dict[str, Any]:
    """``nest(("a", "b"), 1) == {"a": {"b": 1}}``. ``keys`` must not be empty."""
    if not keys:
        raise ValueError("nest() needs at least one key")
    value: Any = leaf
    for key in reversed(keys):
        value = {key: value}
    return value


# --------------------------------------------------------------------------------------------
# JSON

_INDENT_RE = re.compile(r"^([ \t]+)\S", re.MULTILINE)


def detect_indent(text: str | None, default: int = 2) -> int | str:
    """The indentation unit of a JSON document: a number of spaces, or ``"\\t"``."""
    if text:
        match = _INDENT_RE.search(text)
        if match:
            unit = match.group(1)
            return "\t" if "\t" in unit else len(unit)
    return default


def dump_json(data: Any, indent: int | str = 2) -> str:
    """Pretty JSON with a trailing newline. Non-ASCII text is written as is."""
    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------------------------
# TOML


def dump_toml(data: Mapping[str, Any]) -> str:
    """Serialize ``data`` as TOML."""
    return tomli_w.dumps(data)


def toml_text_matches(text: str, expected: Mapping[str, Any]) -> bool:
    """True if ``text`` is valid TOML that parses to exactly ``expected``."""
    try:
        return tomllib.loads(text) == expected
    except tomllib.TOMLDecodeError:
        return False


def _toml_string_end(text: str, start: int) -> int:
    """Index just past the TOML string starting at ``start`` (any of the four string kinds)."""
    quote = text[start]
    if text.startswith(quote * 3, start):
        close = start + 3
        while True:
            close = text.find(quote * 3, close)
            if close == -1:
                return len(text)
            if quote == '"' and _escaped(text, close):
                close += 1
                continue
            end = close + 3
            # Up to two quotes may directly precede the closing delimiter ("""a"""" is valid).
            while end < len(text) and text[end] == quote and end - close < 5:
                end += 1
            return end
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote == '"':
            i += 2
            continue
        if ch in (quote, "\n"):
            return i + 1
        i += 1
    return len(text)


def _escaped(text: str, pos: int) -> bool:
    """True if the character at ``pos`` is preceded by an odd number of backslashes."""
    count = 0
    i = pos - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def toml_has_comments(text: str) -> bool:
    """True if ``text`` contains a ``#`` comment outside string literals."""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _toml_string_end(text, i)
        elif ch == "#":
            return True
        else:
            i += 1
    return False


_HEADER_RE = re.compile(r"^[ \t]*\[(?P<inner>.*)\][ \t]*(?:#.*)?$")


def _header_keys(line: str) -> tuple[str, ...] | None:
    """For a table header line, its key path; ``()`` for an array-of-tables header or one we
    cannot parse; ``None`` if the line is not a header."""
    match = _HEADER_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    inner = match.group("inner")
    if inner.startswith("[") and inner.endswith("]"):
        return ()
    try:
        parsed: Any = tomllib.loads(f"[{inner}]\n")
    except tomllib.TOMLDecodeError:
        return ()
    keys: list[str] = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key = next(iter(parsed))
        keys.append(key)
        parsed = parsed[key]
    return tuple(keys)


_TRAILING_BLANK_RE = re.compile(r"(?:[ \t]*\r?\n)+\Z")


def remove_toml_table(text: str, path: Sequence[str]) -> str:
    """Remove the ``[path]`` table and its sub-tables from ``text``, line by line.

    Lines outside those tables (including comments) are kept. Inline definitions such as
    ``axpilot = { ... }`` are not touched, so verify the result with :func:`toml_text_matches`.
    """
    target = tuple(path)
    out: list[str] = []
    skipping = False
    removed = False
    for line in text.splitlines(keepends=True):
        keys = _header_keys(line)
        if keys is not None:
            skipping = len(keys) >= len(target) and keys[: len(target)] == target
        if skipping:
            removed = True
        else:
            out.append(line)
    result = "".join(out)
    if removed:
        result = _TRAILING_BLANK_RE.sub("\n", result)
        if not result.strip():
            return ""
    return result


def append_toml_table(text: str, keys: Sequence[str], name: str, entry: Mapping[str, Any]) -> str:
    """Append ``[keys.name]`` (with ``entry``'s values) to the end of ``text``."""
    block = dump_toml(nest(keys, {name: dict(entry)}))
    if not text.strip():
        return block
    return text.rstrip() + "\n\n" + block


# --------------------------------------------------------------------------------------------
# YAML

_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_YAML_RESERVED = frozenset({"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~"})


def _yaml_printable(ch: str) -> bool:
    """YAML 1.2's c-printable set (characters allowed unescaped in a double-quoted scalar)."""
    code = ord(ch)
    return (
        code in (0x09, 0x0A, 0x0D, 0x85)
        or 0x20 <= code <= 0x7E
        or 0xA0 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or code >= 0x10000
    ) and code not in (0x2028, 0x2029, 0xFEFF)


def _yaml_string(value: str) -> str:
    quoted = json.dumps(value, ensure_ascii=False)  # JSON strings are YAML double-quoted scalars
    out: list[str] = []
    for ch in quoted:
        if _yaml_printable(ch):
            out.append(ch)
        elif ord(ch) <= 0xFFFF:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(f"\\U{ord(ch):08x}")
    return "".join(out)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("YAML output does not support NaN or infinity")
        return repr(value)
    if isinstance(value, str):
        return _yaml_string(value)
    if isinstance(value, Mapping) and not value:
        return "{}"
    if isinstance(value, list | tuple) and not value:
        return "[]"
    raise TypeError(f"Cannot write {type(value).__name__} values as YAML")


def _yaml_key(key: Any) -> str:
    if not isinstance(key, str):
        raise TypeError("YAML mapping keys must be strings")
    if _PLAIN_KEY_RE.fullmatch(key) and key.lower() not in _YAML_RESERVED:
        return key
    return _yaml_string(key)


def _is_block(value: Any) -> bool:
    return isinstance(value, Mapping | list | tuple) and len(value) > 0


def _yaml_lines(value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, Mapping) and value:
        lines: list[str] = []
        for key, item in value.items():
            if _is_block(item):
                lines.append(f"{pad}{_yaml_key(key)}:")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{pad}{_yaml_key(key)}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list | tuple) and value:
        lines = []
        for item in value:
            if _is_block(item):
                nested = _yaml_lines(item, indent + 2)
                lines.append(f"{pad}- {nested[0][indent + 2 :]}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return lines
    return [pad + _yaml_scalar(value)]


def dump_yaml(data: Any, *, header: str | None = None) -> str:
    """Serialize plain data as block-style YAML, optionally preceded by ``# header`` lines."""
    lines = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    lines.extend(_yaml_lines(data, 0))
    return "\n".join(lines) + "\n"
