from __future__ import annotations

import json
import tomllib

import pytest

from axpilot.clients.formats import (
    append_toml_table,
    detect_indent,
    dump_json,
    dump_yaml,
    nest,
    remove_toml_table,
    toml_has_comments,
    toml_text_matches,
)

# --------------------------------------------------------------------------------------------
# JSON


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, 2),
        ("{}", 2),
        ('{\n    "a": 1\n}', 4),
        ('{\n  "a": 1\n}', 2),
        ('{\n\t"a": 1\n}', "\t"),
    ],
)
def test_detect_indent(text: str | None, expected: int | str) -> None:
    assert detect_indent(text) == expected


def test_dump_json_keeps_unicode_and_ends_with_newline() -> None:
    text = dump_json({"path": "/Users/José/axpilot"}, 2)
    assert text.endswith("}\n")
    assert "José" in text
    assert json.loads(text) == {"path": "/Users/José/axpilot"}


def test_nest() -> None:
    assert nest(("a", "b"), 1) == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        nest((), 1)


# --------------------------------------------------------------------------------------------
# TOML


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a = 1\n# comment\n", True),
        ('a = "# not a comment"\n', False),
        ("a = '# literal'\n", False),
        ('a = """\n# inside a multi-line string\n"""\n', False),
        ("a = '''\n# literal block\n'''\nb = 2 # real\n", True),
        ('a = "quote \\" # still string"\n', False),
        ("[table] # header comment\n", True),
    ],
)
def test_toml_has_comments(text: str, expected: bool) -> None:
    assert toml_has_comments(text) is expected


def test_append_toml_table_keeps_comments_and_parses() -> None:
    text = '# my settings\nmodel = "o4"\n\n[mcp_servers.other]\ncommand = "other" # keep\n'
    entry = {"command": "/bin/axpilot", "args": ["stdio"], "env": {"A": "1"}}
    result = append_toml_table(text, ("mcp_servers",), "axpilot", entry)
    assert result.startswith(text.rstrip())
    assert "# my settings" in result
    assert "# keep" in result
    data = tomllib.loads(result)
    assert data["mcp_servers"]["axpilot"] == entry
    assert data["mcp_servers"]["other"] == {"command": "other"}


def test_append_toml_table_to_empty_text() -> None:
    result = append_toml_table("", ("mcp_servers",), "axpilot", {"command": "x", "args": []})
    assert tomllib.loads(result) == {"mcp_servers": {"axpilot": {"command": "x", "args": []}}}


def test_remove_toml_table_removes_table_and_subtables_only() -> None:
    text = (
        "# top\n"
        "[mcp_servers.other]\n"
        'command = "other"\n'
        "\n"
        "[mcp_servers.axpilot]\n"
        'command = "x"\n'
        "\n"
        "[mcp_servers.axpilot.env]\n"
        'A = "1"\n'
        "\n"
        "[profiles.fast] # comment\n"
        'model = "m"\n'
    )
    result = remove_toml_table(text, ("mcp_servers", "axpilot"))
    data = tomllib.loads(result)
    assert data == {"mcp_servers": {"other": {"command": "other"}}, "profiles": {"fast": {"model": "m"}}}
    assert "# top" in result
    assert "# comment" in result


def test_remove_toml_table_with_quoted_header_keys() -> None:
    text = '[mcp_servers."axpilot"]\ncommand = "x"\n'
    assert remove_toml_table(text, ("mcp_servers", "axpilot")) == ""


def test_remove_toml_table_at_end_trims_blank_lines() -> None:
    text = 'a = 1\n\n[mcp_servers.axpilot]\ncommand = "x"\n'
    assert remove_toml_table(text, ("mcp_servers", "axpilot")) == "a = 1\n"


def test_remove_toml_table_without_match_is_identity() -> None:
    text = "a = 1   \n\n\n"
    assert remove_toml_table(text, ("mcp_servers", "axpilot")) == text


def test_toml_text_matches() -> None:
    assert toml_text_matches("a = 1\n", {"a": 1})
    assert not toml_text_matches("a = 2\n", {"a": 1})
    assert not toml_text_matches("a = = 1\n", {"a": 1})


# --------------------------------------------------------------------------------------------
# YAML


def test_dump_yaml_block_structure() -> None:
    data = {
        "name": "AXPilot",
        "schema": "v1",
        "mcpServers": [{"name": "axpilot", "args": ["stdio", "--profile", "minimal"], "env": {"A": "1"}}],
        "enabled": True,
        "timeout": 300,
        "envs": {},
        "list": [],
        "nothing": None,
    }
    assert dump_yaml(data) == (
        'name: "AXPilot"\n'
        'schema: "v1"\n'
        "mcpServers:\n"
        '  - name: "axpilot"\n'
        "    args:\n"
        '      - "stdio"\n'
        '      - "--profile"\n'
        '      - "minimal"\n'
        "    env:\n"
        '      A: "1"\n'
        "enabled: true\n"
        "timeout: 300\n"
        "envs: {}\n"
        "list: []\n"
        "nothing: null\n"
    )


def test_dump_yaml_header_comment() -> None:
    assert dump_yaml({"a": 1}, header="line one\nline two") == "# line one\n# line two\na: 1\n"


@pytest.mark.parametrize(
    ("key", "rendered"),
    [("plain_key-1", "plain_key-1"), ("yes", '"yes"'), ("with space", '"with space"'), ("1abc", '"1abc"')],
)
def test_dump_yaml_quotes_keys_that_need_it(key: str, rendered: str) -> None:
    assert dump_yaml({key: 1}) == f"{rendered}: 1\n"


def test_dump_yaml_escapes_strings() -> None:
    text = dump_yaml({"v": 'quote " backslash \\ tab \t newline \n nel \x85 ls \u2028 del \x7f'})
    assert text == 'v: "quote \\" backslash \\\\ tab \\t newline \\n nel \x85 ls \\u2028 del \\u007f"\n'


def test_dump_yaml_rejects_unsupported_values() -> None:
    with pytest.raises(TypeError):
        dump_yaml({"v": object()})
    with pytest.raises(ValueError):
        dump_yaml({"v": float("nan")})
    with pytest.raises(TypeError):
        dump_yaml({1: "x"})
