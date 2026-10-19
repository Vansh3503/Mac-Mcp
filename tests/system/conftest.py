"""Shared fixtures for system-integration tests: a FakeRunner and tiny source lexers.

The lexers let tests *prove* that untrusted text embedded in generated AppleScript or Spotlight
queries stays inside its string literal: they split source into code and literals exactly the
way the real parsers delimit strings (a double quote opens and closes a literal; a backslash
escapes the next character).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from axpilot.system.runner import FakeRunner

_APPLESCRIPT_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}


@dataclass(frozen=True)
class Lexed:
    """Source split into code (each literal replaced by ``<S>``) and the decoded literals."""

    code: str
    literals: list[str]
    unescaped_wildcards: list[int]
    """Per literal, how many ``*`` / ``?`` were *not* escaped (Spotlight wildcards)."""


def _lex(source: str, escapes: dict[str, str]) -> Lexed:
    code: list[str] = []
    literals: list[str] = []
    wildcards: list[int] = []
    i = 0
    while i < len(source):
        ch = source[i]
        if ch != '"':
            code.append(ch)
            i += 1
            continue
        i += 1
        buf: list[str] = []
        unescaped = 0
        while True:
            if i >= len(source):
                raise ValueError("unterminated string literal")
            c = source[i]
            if c == "\\":
                if i + 1 >= len(source):
                    raise ValueError("dangling backslash at end of source")
                buf.append(escapes.get(source[i + 1], source[i + 1]))
                i += 2
                continue
            i += 1
            if c == '"':
                break
            if c in "*?":
                unescaped += 1
            buf.append(c)
        literals.append("".join(buf))
        wildcards.append(unescaped)
        code.append("<S>")
    return Lexed("".join(code), literals, wildcards)


def lex_applescript(source: str) -> Lexed:
    """Lex AppleScript source (``\\n``, ``\\r``, ``\\t`` escapes are decoded)."""
    return _lex(source, _APPLESCRIPT_ESCAPES)


def lex_mdquery(source: str) -> Lexed:
    """Lex a Spotlight raw query expression (a backslash makes the next character literal)."""
    return _lex(source, {})


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


# Session scope: the lexers are pure functions, and hypothesis rejects function-scoped fixtures.
@pytest.fixture(scope="session")
def as_lexer() -> Callable[[str], Lexed]:
    return lex_applescript


@pytest.fixture(scope="session")
def md_lexer() -> Callable[[str], Lexed]:
    return lex_mdquery
