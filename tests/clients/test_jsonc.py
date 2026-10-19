from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.clients import jsonc


def test_plain_json_parses_unchanged() -> None:
    text = '{"a": [1, 2, {"b": null}], "c": "d"}'
    assert jsonc.loads(text) == json.loads(text)
    assert not jsonc.has_comments(text)


def test_line_and_block_comments_are_ignored() -> None:
    text = """// header
    {
      /* block
         comment */ "a": 1, // trailing
      "b": /* inline */ 2
    }
    """
    assert jsonc.loads(text) == {"a": 1, "b": 2}
    assert jsonc.has_comments(text)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/path",
        "a /* not a comment */ b",
        "// not a comment either",
        'escaped \\" quote // still a string',
        "ends with a backslash \\\\",
    ],
)
def test_comment_markers_inside_strings_are_content(value: str) -> None:
    raw = json.dumps({"url": value})
    assert jsonc.loads(raw) == {"url": value}
    assert not jsonc.has_comments(raw)


def test_trailing_commas_are_removed() -> None:
    text = '{"a": [1, 2,], "b": {"c": 3,},\n}'
    assert jsonc.loads(text) == {"a": [1, 2], "b": {"c": 3}}


def test_trailing_comma_before_comment_and_brace() -> None:
    text = '{"a": 1, // last one\n}'
    assert jsonc.loads(text) == {"a": 1}


def test_comma_inside_string_before_brace_is_kept() -> None:
    assert jsonc.loads('{"a": ",}"}') == {"a": ",}"}


def test_strip_comments_keeps_positions() -> None:
    text = '{"a": 1, /* x */\n"b": 2}'
    stripped = jsonc.strip_comments(text)
    assert len(stripped) == len(text)
    assert stripped.index('"b"') == text.index('"b"')
    assert stripped.count("\n") == text.count("\n")


def test_byte_order_mark_is_ignored() -> None:
    assert jsonc.loads('﻿{"a": 1}') == {"a": 1}


def test_crlf_line_comments() -> None:
    assert jsonc.loads('{\r\n"a": 1 // c\r\n}') == {"a": 1}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"a": "unterminated}', "Unterminated string"),
        ('{"a": 1 /* open', "Unterminated block comment"),
        ('{"a": "line\nbreak"}', "Unterminated string"),
    ],
)
def test_scanner_errors(text: str, message: str) -> None:
    with pytest.raises(jsonc.JSONCError, match=message):
        jsonc.loads(text)


def test_syntax_errors_report_line_and_column() -> None:
    with pytest.raises(jsonc.JSONCError) as info:
        jsonc.loads('{\n  // comment\n  "a": 1\n  "b": 2\n}')
    assert info.value.lineno == 4
    assert info.value.colno == 3
    assert "line 4" in str(info.value)


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(),
        lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4),
        max_leaves=20,
    )
)
def test_any_json_document_round_trips(value: object) -> None:
    for indent in (None, 2):
        text = json.dumps(value, indent=indent)
        assert jsonc.loads(text) == value
        assert not jsonc.has_comments(text)
