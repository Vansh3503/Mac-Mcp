from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import FakeRunner, truncate_output
from axpilot.system.spotlight import (
    MAX_LIMIT,
    SPOTLIGHT_KINDS,
    build_spotlight_query,
    normalize_kind,
    spotlight_argv,
    spotlight_search,
)

Lexer = Callable[[str], Any]


def nul_joined(*paths: str) -> str:
    return "".join(f"{p}\x00" for p in paths)


def test_plain_query_is_one_argv_element(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stdout=nul_joined("/Users/me/a.txt", "/Users/me/b.txt"))
    paths = spotlight_search(runner, "  quarterly report; rm -rf ~ $(id)  ")
    assert runner.last_call.argv == ["mdfind", "-0", "quarterly report; rm -rf ~ $(id)"]
    assert paths == ["/Users/me/a.txt", "/Users/me/b.txt"]


def test_scope_uses_onlyin_with_an_expanded_absolute_path(
    runner: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    runner.register(["mdfind"], stdout="")
    spotlight_search(runner, "invoice", scope="~/Documents")
    assert runner.last_call.argv == ["mdfind", "-0", "-onlyin", str(tmp_path / "Documents"), "invoice"]


@pytest.mark.parametrize("scope", ["relative/dir", "-rf", "", "/definitely/not/here"])
def test_bad_scopes_are_rejected(runner: FakeRunner, scope: str) -> None:
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "x", scope=scope)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert runner.calls == []


def test_kind_builds_a_content_type_query(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stdout=nul_joined("/Users/me/Taxes.pdf"))
    paths = spotlight_search(runner, "taxes", kind="pdf")
    expression = runner.last_call.argv[-1]
    assert expression == (
        '(kMDItemContentTypeTree == "com.adobe.pdf") && '
        '(kMDItemDisplayName == "*taxes*"cd || kMDItemTextContent == "taxes"cdw)'
    )
    assert paths == ["/Users/me/Taxes.pdf"]


def test_kind_without_query_lists_that_kind() -> None:
    assert (
        build_spotlight_query("", kind="app") == '(kMDItemContentTypeTree == "com.apple.application-bundle")'
    )


def test_multi_type_kinds_are_or_combined() -> None:
    expression = build_spotlight_query("", kind="document")
    for uti in SPOTLIGHT_KINDS["document"]:
        assert f'kMDItemContentTypeTree == "{uti}"' in expression
    assert expression.count(" || ") == len(SPOTLIGHT_KINDS["document"]) - 1


@pytest.mark.parametrize(
    ("alias", "kind"),
    [("application", "app"), ("Images", "image"), ("video", "movie"), ("music", "audio"), (" PDF ", "pdf")],
)
def test_kind_aliases(alias: str, kind: str) -> None:
    assert normalize_kind(alias) == kind


def test_unknown_kind_lists_valid_kinds(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "x", kind="spaceship")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "pdf" in (info.value.hint or "")


def test_query_text_is_escaped_inside_kind_expressions(md_lexer: Lexer) -> None:
    payload = 'x" || kMDItemFSName == "*'
    expression = build_spotlight_query(payload, kind="image")
    lexed = md_lexer(expression)
    assert lexed.code == (
        "(kMDItemContentTypeTree == <S>) && (kMDItemDisplayName == <S>cd || kMDItemTextContent == <S>cdw)"
    )
    assert lexed.literals == ["public.image", f"*{payload}*", payload]


@given(text=st.text(alphabet=st.characters(exclude_characters="\x00"), max_size=200))
def test_no_query_can_change_the_expression_structure(md_lexer: Lexer, text: str) -> None:
    if not text.strip():
        return
    lexed = md_lexer(build_spotlight_query(text, kind="pdf"))
    assert lexed.code == (
        "(kMDItemContentTypeTree == <S>) && (kMDItemDisplayName == <S>cd || kMDItemTextContent == <S>cdw)"
    )
    stripped = text.strip()
    assert lexed.literals == ["com.adobe.pdf", f"*{stripped}*", stripped]
    # Only the two wildcards AXPilot adds around the name are live; the user's are escaped.
    assert lexed.unescaped_wildcards == [0, 2, 0]


@pytest.mark.parametrize(
    ("query", "kind", "fragment"),
    [
        ("", None, "empty"),
        ("   ", None, "empty"),
        ("-onlyin /", None, "start with '-'"),
        ("a\x00b", None, "NUL"),
        ("x" * 1001, None, "too long"),
    ],
)
def test_invalid_queries(runner: FakeRunner, query: str, kind: str | None, fragment: str) -> None:
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, query, kind=kind)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in info.value.message
    assert runner.calls == []


def test_leading_dash_is_fine_inside_a_kind_query() -> None:
    assert build_spotlight_query("-draft", kind="text").startswith("(kMDItemContentTypeTree")


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1, True])
def test_invalid_limits(runner: FakeRunner, limit: int) -> None:
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "x", limit=limit)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_limit_caps_results_and_output_budget(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stdout=nul_joined(*(f"/f/{i}" for i in range(50))))
    assert spotlight_search(runner, "f", limit=3) == ["/f/0", "/f/1", "/f/2"]
    assert runner.last_call.max_output >= 3 * 1024


def test_parsing_keeps_paths_with_newlines_and_spaces(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stdout=nul_joined("/Users/me/My File.txt", "/Users/me/odd\nname") + "\n")
    assert spotlight_search(runner, "file") == ["/Users/me/My File.txt", "/Users/me/odd\nname"]


def test_truncated_output_drops_the_partial_path(runner: FakeRunner) -> None:
    text, _ = truncate_output(nul_joined("/a/one", "/a/two", "/a/three-is-long"), 18)
    runner.register(["mdfind"], stdout=text)
    assert spotlight_search(runner, "x") == ["/a/one", "/a/two"]


def test_timeout_with_partial_results_returns_them(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stdout=nul_joined("/a/one"), timed_out=True)
    assert spotlight_search(runner, "x") == ["/a/one"]


def test_timeout_without_results_raises(runner: FakeRunner) -> None:
    runner.register(["mdfind"], timed_out=True)
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "x", timeout_s=3)
    assert info.value.code is ErrorCode.TIMEOUT


def test_unparseable_query_is_invalid_argument(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stderr="Failed to create query for 'kMDItemFSName == '.\n", exit_code=0)
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "kMDItemFSName ==")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_command_failure(runner: FakeRunner) -> None:
    runner.register(["mdfind"], stderr="mds is not running\n", exit_code=1)
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "x")
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "mds is not running" in info.value.message


def test_missing_mdfind_is_not_supported(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, "x")
    assert info.value.code is ErrorCode.NOT_SUPPORTED


def test_spotlight_argv() -> None:
    assert spotlight_argv("q") == ["mdfind", "-0", "q"]


def test_query_must_be_text(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError) as info:
        spotlight_search(runner, None)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
