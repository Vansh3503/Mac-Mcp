"""Read JSON with comments ("JSONC"), the dialect of VS Code, Zed and Gemini CLI settings files.

JSONC is JSON plus ``// line`` and ``/* block */`` comments and trailing commas before ``}`` or
``]``. The scanner here is string-aware: ``"https://example.com"`` and ``"a /* b */ c"`` are string
contents, not comments. Comments are replaced by spaces (newlines are kept), so line and column
numbers in error messages still point into the original text.

AXPilot only *reads* JSONC. Writing a file back with :mod:`json` would drop its comments, so callers
use :func:`has_comments` to decide whether a rewrite is safe.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Literal

__all__ = ["JSONCError", "has_comments", "loads", "strip_comments", "strip_trailing_commas"]

_SegmentKind = Literal["code", "string", "comment"]
_BOM = "﻿"


class JSONCError(ValueError):
    """The text is not valid JSON(C). ``lineno`` and ``colno`` are 1-based."""

    def __init__(self, message: str, text: str, pos: int) -> None:
        self.msg = message
        self.pos = pos
        self.lineno = text.count("\n", 0, pos) + 1
        self.colno = pos - (text.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{message} (line {self.lineno}, column {self.colno})")


def _string_end(text: str, start: int) -> int:
    """Index just past the string literal that opens at ``start`` (a double quote)."""
    i = start + 1
    end = len(text)
    while i < end:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        if ch in "\r\n":
            break
        i += 1
    raise JSONCError("Unterminated string", text, start)


def _segments(text: str) -> Iterator[tuple[_SegmentKind, int, int]]:
    """Split ``text`` into ``(kind, start, end)`` spans of code, string literals and comments."""
    i = 0
    code_start = 0
    end = len(text)
    while i < end:
        ch = text[i]
        if ch == '"':
            stop = _string_end(text, i)
            kind: _SegmentKind = "string"
        elif ch == "/" and text.startswith("//", i):
            newline = min((pos for pos in (text.find("\n", i), text.find("\r", i)) if pos != -1), default=end)
            stop = newline
            kind = "comment"
        elif ch == "/" and text.startswith("/*", i):
            close = text.find("*/", i + 2)
            if close == -1:
                raise JSONCError("Unterminated block comment", text, i)
            stop = close + 2
            kind = "comment"
        else:
            i += 1
            continue
        if code_start < i:
            yield "code", code_start, i
        yield kind, i, stop
        i = code_start = stop
    if code_start < end:
        yield "code", code_start, end


def has_comments(text: str) -> bool:
    """True if ``text`` contains at least one ``//`` or ``/* */`` comment outside strings.

    Raises :class:`JSONCError` for an unterminated string or block comment.
    """
    return any(kind == "comment" for kind, _, _ in _segments(text))


def strip_comments(text: str) -> str:
    """Replace every comment with spaces, keeping line breaks (so positions stay valid)."""
    parts: list[str] = []
    for kind, start, stop in _segments(text):
        chunk = text[start:stop]
        if kind == "comment":
            chunk = "".join(ch if ch in "\r\n" else " " for ch in chunk)
        parts.append(chunk)
    return "".join(parts)


def strip_trailing_commas(text: str) -> str:
    """Replace commas that directly precede ``}`` or ``]`` (ignoring whitespace) with a space.

    ``text`` must already be free of comments (see :func:`strip_comments`).
    """
    chars = list(text)
    for kind, start, stop in _segments(text):
        if kind != "code":
            continue
        for i in range(start, stop):
            if chars[i] != ",":
                continue
            j = i + 1
            while j < len(chars) and chars[j] in " \t\r\n":
                j += 1
            if j < len(chars) and chars[j] in "}]":
                chars[i] = " "
    return "".join(chars)


def loads(text: str) -> Any:
    """Parse JSONC text (a leading byte-order mark is ignored).

    Raises :class:`JSONCError` with the 1-based line and column of the problem.
    """
    if text.startswith(_BOM):
        text = " " + text[1:]
    cleaned = strip_trailing_commas(strip_comments(text))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JSONCError(exc.msg, text, exc.pos) from exc
