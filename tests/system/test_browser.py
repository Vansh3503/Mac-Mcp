from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.browser import (
    BROWSERS,
    RS,
    US,
    TabInfo,
    activate_tab,
    build_activate_tab_script,
    build_close_tab_script,
    build_eval_js_script,
    build_frontmost_script,
    build_list_tabs_script,
    build_open_url_script,
    close_tab,
    eval_js,
    frontmost_browser,
    list_tabs,
    normalize_url,
    open_url,
    parse_tabs,
    resolve_browser,
)
from axpilot.system.runner import FakeRunner

Lexer = Callable[[str], Any]
SAFARI = BROWSERS["safari"]
CHROME = BROWSERS["chrome"]
ARC = BROWSERS["arc"]


def record(*fields: object) -> str:
    return US.join(str(f) for f in fields)


def tab_output(*records: str) -> str:
    return RS.join(records) + "\n"


def script(runner: FakeRunner) -> str:
    return runner.last_call.input_text or ""


# ---------------------------------------------------------------------------------------------
# The browser table


def test_browser_table() -> None:
    assert SAFARI.bundle_id == "com.apple.Safari"
    assert SAFARI.family == "safari"
    chromium = {"chrome", "arc", "edge", "brave", "chromium", "vivaldi"}
    assert chromium <= set(BROWSERS)
    for key in chromium:
        assert BROWSERS[key].family == "chromium"
    assert len({spec.bundle_id for spec in BROWSERS.values()}) == len(BROWSERS)
    assert all(spec.key == key for key, spec in BROWSERS.items())


@pytest.mark.parametrize(
    ("query", "key"),
    [
        ("safari", "safari"),
        ("Safari", "safari"),
        ("Google Chrome", "chrome"),
        ("Google Chrome.app", "chrome"),
        ("com.google.Chrome", "chrome"),
        ("company.thebrowser.Browser", "arc"),
        ("Microsoft Edge", "edge"),
        ("brave", "brave"),
        ("Brave Browser", "brave"),
        ("  VIVALDI ", "vivaldi"),
    ],
)
def test_resolve_browser(query: str, key: str) -> None:
    assert resolve_browser(query) is BROWSERS[key]
    assert resolve_browser(BROWSERS[key]) is BROWSERS[key]


@pytest.mark.parametrize("query", ["firefox", "", "com.example.browser"])
def test_unsupported_browser(query: str) -> None:
    with pytest.raises(AXPilotError) as info:
        resolve_browser(query)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "safari" in (info.value.hint or "")


# ---------------------------------------------------------------------------------------------
# Script dialects


def test_safari_list_script_dialect() -> None:
    source = build_list_tabs_script(SAFARI)
    assert 'tell application id "com.apple.Safari"' in source
    assert "current tab of axpWindow" in source
    assert "name of axpTab" in source
    assert 'if not (application id "com.apple.Safari" is running) then error' in source
    assert "number -600" in source
    assert "character id 31" in source
    assert "character id 30" in source
    assert "active tab" not in source


def test_chromium_list_script_dialect() -> None:
    source = build_list_tabs_script(CHROME)
    assert 'tell application id "com.google.Chrome"' in source
    assert "active tab of axpWindow" in source
    assert "title of axpTab" in source
    assert "current tab" not in source


def test_eval_js_dialects() -> None:
    safari = build_eval_js_script(SAFARI, "document.title")
    assert 'do JavaScript "document.title" in current tab of window 1' in safari
    chrome = build_eval_js_script(CHROME, "document.title", (2, 3))
    assert 'execute tab 3 of window 2 javascript "document.title"' in chrome
    assert "do JavaScript" not in chrome
    for source in (safari, chrome):
        assert "-2763" in source  # "no result" from undefined is not an error
        assert 'if axpResult is missing value then return ""' in source


def test_activate_tab_dialects() -> None:
    assert "set current tab of window 2 to tab 3 of window 2" in build_activate_tab_script(SAFARI, (2, 3))
    assert "set active tab index of window 2 to 3" in build_activate_tab_script(CHROME, (2, 3))
    arc = build_activate_tab_script(ARC, (2, 3))
    assert "tell tab 3 of window 2 to select" in arc
    assert "active tab index" not in arc
    for source in (build_activate_tab_script(SAFARI, (1, 1)), arc):
        assert "set index of window" in source
        assert "\tactivate" in source


def test_close_tab_dialects() -> None:
    assert "\tclose current tab of window 1" in build_close_tab_script(SAFARI)
    assert "\tclose active tab of window 1" in build_close_tab_script(CHROME)
    assert "\tclose tab 4 of window 2" in build_close_tab_script(CHROME, (2, 4))


def test_open_url_dialects() -> None:
    safari = build_open_url_script(SAFARI, "https://example.com")
    assert 'make new tab at end of tabs with properties {URL:"https://example.com"}' in safari
    assert 'make new document with properties {URL:"https://example.com"}' in safari
    assert "if (count of windows) = 0 then" in safari
    chrome = build_open_url_script(CHROME, "https://example.com", new_window=True)
    assert "if true then" in chrome
    assert "make new window" in chrome
    assert 'set URL of active tab of axpWindow to "https://example.com"' in chrome
    assert "is running" not in chrome, "opening a URL may launch the browser"


def test_frontmost_script_lists_every_bundle_id_without_system_events() -> None:
    source = build_frontmost_script()
    for spec in BROWSERS.values():
        assert f'"{spec.bundle_id}"' in source
    assert "path to frontmost application" in source
    assert "System Events" not in source


# ---------------------------------------------------------------------------------------------
# Injection safety: user strings stay inside string literals


JS_PAYLOAD = '"; do shell script "id"; "\\\n\r\t'


def test_javascript_is_embedded_as_one_literal(as_lexer: Lexer) -> None:
    for spec in (SAFARI, CHROME):
        lexed = as_lexer(build_eval_js_script(spec, JS_PAYLOAD))
        assert JS_PAYLOAD in lexed.literals
        assert "do shell script" not in lexed.code


@given(js=st.text(alphabet=st.characters(exclude_characters="\x00"), min_size=1, max_size=300))
def test_no_javascript_can_break_out(as_lexer: Lexer, js: str) -> None:
    if not js.strip():
        return
    clean = as_lexer(build_eval_js_script(CHROME, "x"))
    lexed = as_lexer(build_eval_js_script(CHROME, js))
    assert lexed.code == clean.code
    assert js in lexed.literals


@given(path=st.text(alphabet=st.characters(min_codepoint=0x20, exclude_characters="\x7f"), max_size=200))
def test_no_url_can_break_out(as_lexer: Lexer, path: str) -> None:
    url = "https://example.com/" + path
    clean = as_lexer(build_open_url_script(SAFARI, "https://example.com/"))
    lexed = as_lexer(build_open_url_script(SAFARI, url))
    assert lexed.code == clean.code
    assert normalize_url(url) in lexed.literals


# ---------------------------------------------------------------------------------------------
# Parsing


def test_parse_tabs() -> None:
    output = tab_output(
        record(1, 1, "false", "https://a.example/", "A"),
        record(1, 2, "true", "https://b.example/?q=1&x", "B — with ünicode"),
        record(2, 1, "true", "", ""),
    )
    assert parse_tabs(output) == [
        TabInfo(1, 1, "A", "https://a.example/", False),
        TabInfo(1, 2, "B — with ünicode", "https://b.example/?q=1&x", True),
        TabInfo(2, 1, "", "", True),
    ]


def test_parse_tabs_skips_malformed_records() -> None:
    output = tab_output(
        record(1, 1, "true", "https://ok/", "OK"),
        record("x", 1, "true", "https://bad-index/", "Bad"),
        record(1, 2, "true", "only four fields"),
        "garbage",
        "",
    )
    assert parse_tabs(output) == [TabInfo(1, 1, "OK", "https://ok/", True)]


def test_parse_tabs_keeps_unit_separators_in_the_title() -> None:
    assert parse_tabs(record(1, 1, "false", "u", "a") + US + "b")[0].title == f"a{US}b"


def test_parse_tabs_empty() -> None:
    assert parse_tabs("") == []
    assert parse_tabs("\n") == []


def test_tab_info_ref_and_dict() -> None:
    tab = TabInfo(2, 5, "T", "https://t/", False)
    assert tab.ref == (2, 5)
    assert tab.to_dict() == {
        "window_index": 2,
        "tab_index": 5,
        "title": "T",
        "url": "https://t/",
        "active": False,
    }


# ---------------------------------------------------------------------------------------------
# Running against the fake runner


def test_list_tabs(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout=tab_output(record(1, 1, "true", "https://x/", "X")))
    assert list_tabs(runner, "safari") == [TabInfo(1, 1, "X", "https://x/", True)]
    assert runner.last_call.argv == ["osascript", "-l", "AppleScript"]
    assert script(runner) == build_list_tabs_script(SAFARI)


def test_list_tabs_when_not_running(runner: FakeRunner) -> None:
    runner.register(
        ["osascript"], stderr="0:120: execution error: Google Chrome is not running. (-600)\n", exit_code=1
    )
    with pytest.raises(AXPilotError) as info:
        list_tabs(runner, "chrome")
    err = info.value
    assert err.code is ErrorCode.APP_NOT_FOUND
    assert err.message == "Google Chrome is not running."
    assert err.details["browser"] == "chrome"


def test_browser_not_installed(runner: FakeRunner) -> None:
    runner.register(
        ["osascript"],
        stderr='0:31: syntax error: Can’t get application id "com.vivaldi.Vivaldi". (-2700)\n',
        exit_code=1,
    )
    with pytest.raises(AXPilotError) as info:
        list_tabs(runner, "vivaldi")
    assert info.value.code is ErrorCode.APP_NOT_FOUND
    assert "not installed" in info.value.message


def test_activate_tab(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="")
    activate_tab(runner, "chrome", (1, 3))
    assert "set active tab index of window 1 to 3" in script(runner)


def test_missing_tab_is_invalid_argument(runner: FakeRunner) -> None:
    runner.register(
        ["osascript"],
        stderr="0:99: execution error: Safari got an error: Can’t get tab 9 of window 1. (-1728)\n",
        exit_code=1,
    )
    with pytest.raises(AXPilotError) as info:
        activate_tab(runner, "safari", (1, 9))
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "browser_tabs" in (info.value.hint or "")


@pytest.mark.parametrize(
    "tab", [(0, 1), (1, 0), (-1, 2), (1,), (1, 2, 3), (True, 1), ("1", "2"), (1, 10_001)]
)
def test_invalid_tab_refs(runner: FakeRunner, tab: Any) -> None:
    with pytest.raises(AXPilotError) as info:
        activate_tab(runner, "safari", tab)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError):
        close_tab(runner, "chrome", tab)
    with pytest.raises(AXPilotError):
        eval_js(runner, "chrome", "1", tab)
    assert runner.calls == []


def test_open_url(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="")
    open_url(runner, "edge", "example.com/path?q=a b")
    source = script(runner)
    assert 'tell application id "com.microsoft.edgemac"' in source
    assert '{URL:"https://example.com/path?q=a b"}' in source


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com", "https://example.com"),
        ("  http://x.test/a  ", "http://x.test/a"),
        ("example.com", "https://example.com"),
        ("localhost:8080/app", "https://localhost:8080/app"),
        ("example.com:8443", "https://example.com:8443"),
        ("127.0.0.1:3000/?q=1", "https://127.0.0.1:3000/?q=1"),
        ("tel:123", "tel:123"),
        ("mailto:me@example.com", "mailto:me@example.com"),
        ("file:///Users/me/index.html", "file:///Users/me/index.html"),
        ("about:blank", "about:blank"),
    ],
)
def test_normalize_url(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "JavaScript:void(0)", " data:text/html,<b>x</b>", "blob:abc"]
)
def test_code_urls_are_blocked(runner: FakeRunner, url: str) -> None:
    with pytest.raises(AXPilotError) as info:
        open_url(runner, "safari", url)
    assert info.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert "browser_eval_js" in (info.value.hint or "")
    assert runner.calls == []


@pytest.mark.parametrize(
    "url", ["", "   ", "https://x/\nhttps://y/", "https://x/\x00", "https://" + "a" * 9000]
)
def test_bad_urls_are_rejected(url: str) -> None:
    with pytest.raises(AXPilotError) as info:
        normalize_url(url)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_close_tab(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="")
    close_tab(runner, "safari", (2, 1))
    assert "\tclose tab 1 of window 2" in script(runner)


def test_eval_js_returns_the_result(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout='{"title":"Example"}\n')
    assert eval_js(runner, "chrome", "JSON.stringify({title: document.title})") == '{"title":"Example"}'
    assert runner.last_call.timeout_s == 30


@pytest.mark.parametrize(
    ("browser", "stderr", "hint_fragment"),
    [
        (
            "safari",
            "0:120: execution error: Safari got an error: You must enable the "
            "'Allow JavaScript from Apple Events' option in Safari's Develop menu "
            "to use 'do JavaScript'. (8)\n",
            "Develop",
        ),
        (
            "chrome",
            "0:95: execution error: Google Chrome got an error: Executing JavaScript through "
            "AppleScript is turned off. To turn it on, from the menu bar, go to View > Developer > "
            "Allow JavaScript from Apple Events. (12)\n",
            "View → Developer",
        ),
    ],
)
def test_javascript_disabled_has_a_helpful_hint(
    runner: FakeRunner, browser: str, stderr: str, hint_fragment: str
) -> None:
    runner.register(["osascript"], stderr=stderr, exit_code=1)
    with pytest.raises(AXPilotError) as info:
        eval_js(runner, browser, "1 + 1")
    err = info.value
    assert err.code is ErrorCode.NOT_SUPPORTED
    assert "Allow JavaScript from Apple Events" in err.message
    assert hint_fragment in (err.hint or "")


@pytest.mark.parametrize("js", ["", "   ", "x" * 100_001])
def test_eval_js_validation(runner: FakeRunner, js: str) -> None:
    with pytest.raises(AXPilotError) as info:
        eval_js(runner, "safari", js)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_automation_permission_is_preserved(runner: FakeRunner) -> None:
    runner.register(
        ["osascript"],
        stderr="0:50: execution error: Not authorized to send Apple events to Safari. (-1743)\n",
        exit_code=1,
    )
    with pytest.raises(AXPilotError) as info:
        list_tabs(runner, "safari")
    assert info.value.code is ErrorCode.PERMISSION_AUTOMATION
    assert info.value.details["browser"] == "safari"
    assert info.value.details["app"] == "Safari"


def test_other_script_errors_pass_through(runner: FakeRunner) -> None:
    runner.register(["osascript"], stderr="0:1: execution error: Something odd. (-10000)\n", exit_code=1)
    with pytest.raises(AXPilotError) as info:
        close_tab(runner, "safari")
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert info.value.details["browser"] == "safari"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("com.apple.Safari\n", "safari"),
        ("company.thebrowser.Browser\n", "arc"),
        ("\n", None),
        ("com.apple.finder\n", None),
    ],
)
def test_frontmost_browser(runner: FakeRunner, stdout: str, expected: str | None) -> None:
    runner.register(["osascript"], stdout=stdout)
    assert frontmost_browser(runner) == expected
    assert script(runner) == build_frontmost_script()
