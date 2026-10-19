"""Browser tools: browser_tabs, browser_read and browser_eval_js against the fake desktop's Safari
and a FakeRunner that plays the browser's AppleScript side. Includes injection tests: URLs and
JavaScript with quotes, backslashes and line breaks stay inside their AppleScript string literal."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from axpilot.engine.fake import DEMO_FIXTURE, FakeEngine
from axpilot.errors import ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.browser import page_lines, web_areas
from axpilot.system.browser import BROWSERS, RS, US, build_list_tabs_script
from axpilot.system.runner import CommandResult, FakeCall, FakeRunner
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio

SAFARI = BROWSERS["safari"]
TABS = [
    (1, 1, False, "https://example.com/", "Example Domain"),
    (1, 2, True, "https://news.ycombinator.com/", "Hacker News"),
    (2, 1, True, "https://bank.example/", "My Bank – Accounts"),
]


class Recorder:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self.answer


class FakeBrowser:
    """Answers osascript like Safari: the tab list for the listing script, ``js_result`` for
    JavaScript, nothing for the rest. Every script is kept in ``scripts``."""

    def __init__(self, tabs: list[tuple[int, int, bool, str, str]] = TABS, js_result: str = "42") -> None:
        self.tabs = tabs
        self.js_result = js_result
        self.scripts: list[str] = []
        self.failure: tuple[int, str] | None = None

    def __call__(self, call: FakeCall) -> CommandResult:
        script = call.input_text or ""
        self.scripts.append(script)
        if self.failure is not None:
            code, stderr = self.failure
            return CommandResult(call.argv, code, "", stderr)
        if script == build_list_tabs_script(SAFARI):
            records = [
                US.join([str(w), str(t), "true" if a else "false", u, title])
                for w, t, a, u, title in self.tabs
            ]
            return CommandResult(call.argv, 0, RS.join(records) + "\n", "")
        if "JavaScript" in script:
            return CommandResult(call.argv, 0, self.js_result + "\n", "")
        return CommandResult(call.argv, 0, "", "")


def make_ctx(*, confirm: Any = True, engine: Any = None, **overrides: Any) -> ServerContext:
    return make_test_context({"tools.profile": "full", **overrides}, engine=engine, confirm=confirm)


def runner_of(ctx: ServerContext) -> FakeRunner:
    assert isinstance(ctx.runner, FakeRunner)
    return ctx.runner


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture
def ctx(recorder: Recorder, browser: FakeBrowser) -> ServerContext:
    context = make_ctx(confirm=recorder)
    runner_of(context).register(["osascript"], handler=browser)
    return context


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        await ok(connected, "app", {"action": "launch", "name": "Safari"})
        yield connected


async def call(client: Client, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args)
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


def applescript_literals(source: str) -> list[str]:
    """The decoded string literals of AppleScript ``source`` (a small tokenizer for the tests)."""
    escapes = {"n": "\n", "r": "\r", "t": "\t"}
    literals: list[str] = []
    i = 0
    while i < len(source):
        if source[i] != '"':
            i += 1
            continue
        j, chars = i + 1, []
        while source[j] != '"':
            if source[j] == "\\":
                chars.append(escapes.get(source[j + 1], source[j + 1]))
                j += 2
            else:
                chars.append(source[j])
                j += 1
        literals.append("".join(chars))
        i = j + 1
    return literals


def without_literals(source: str) -> str:
    out, i = [], 0
    while i < len(source):
        if source[i] == '"':
            j = i + 1
            while source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            out.append('""')
            i = j + 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def desktop_with_page(tmp_path: Path, children: list[dict[str, Any]] | None) -> FakeEngine:
    """The demo desktop with Safari running and its page replaced (``None``: no web area)."""
    data = json.loads(Path(DEMO_FIXTURE).read_text(encoding="utf-8"))
    for app in data["apps"]:
        if app["name"] == "Safari":
            app.pop("running", None)
            app["pid"] = 4000
    window = next(w for w in data["windows"] if w["app"] == "com.apple.Safari")

    def edit(node: dict[str, Any]) -> bool:
        for index, child in enumerate(node.get("children", [])):
            if child.get("role") == "AXWebArea":
                if children is None:
                    del node["children"][index]
                else:
                    child["children"] = children
                return True
            if edit(child):
                return True
        return False

    assert edit(window["tree"])
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return FakeEngine.from_fixture(path)


# =========================================================================== browser_tabs
async def test_list_tabs(client: Client, browser: FakeBrowser, recorder: Recorder) -> None:
    data = await ok(client, "browser_tabs", {})
    assert data["browser"] == "Safari"
    assert [(t["window"], t["tab"], t["active"], t["title"]) for t in data["tabs"]] == [
        (1, 1, False, "Example Domain"),
        (1, 2, True, "Hacker News"),
        (2, 1, True, "My Bank – Accounts"),
    ]
    assert browser.scripts[-1] == build_list_tabs_script(SAFARI)
    assert recorder.requests == []


async def test_tabs_matching_deny_windows_are_hidden(browser: FakeBrowser) -> None:
    ctx = make_ctx(**{"safety.deny_windows": ["com.apple.Safari::bank"]})
    runner_of(ctx).register(["osascript"], handler=browser)
    async with connected_client(ctx) as client:
        await ok(client, "app", {"action": "launch", "name": "Safari"})
        data = await ok(client, "browser_tabs", {})
        assert data["hidden"] == 1
        assert [t["title"] for t in data["tabs"]] == ["Example Domain", "Hacker News"]
        _, error = await call(client, "browser_tabs", {"action": "activate", "window": 2, "tab": 1})
        assert error == "BLOCKED_BY_POLICY"
        _, error = await call(client, "browser_eval_js", {"js": "1", "window": 2, "tab": 1})
        assert error == "BLOCKED_BY_POLICY"


async def test_activate_and_close(client: Client, browser: FakeBrowser) -> None:
    data = await ok(client, "browser_tabs", {"action": "activate", "window": 1, "tab": 1})
    assert "Example Domain" in data["message"]
    assert "current tab" in browser.scripts[-1]
    closed = await ok(client, "browser_tabs", {"action": "close"})
    assert "Hacker News" in closed["message"]  # the front window's active tab
    closed = await ok(client, "browser_tabs", {"action": "close", "window": 1, "tab": 1})
    assert "Example Domain" in closed["message"]
    for args in (
        {"action": "activate", "window": 9, "tab": 1},
        {"action": "activate"},
        {"action": "close", "window": 1},
        {"action": "list", "window": 1},
        {"action": "list", "url": "https://x.example"},
        {"action": "open"},
        {"action": "open", "url": "https://x.example", "tab": 1},
    ):
        _, error = await call(client, "browser_tabs", args)
        assert error == "INVALID_ARGUMENT", args


@pytest.mark.parametrize(
    "url",
    [
        'https://example.com/?q=" & (do shell script "say pwned") & "',
        "https://example.com/a\\b\\\\c",
        'https://example.com/"quoted"',
    ],
)
async def test_open_urls_cannot_break_out_of_their_literal(
    client: Client, browser: FakeBrowser, url: str
) -> None:
    data = await ok(client, "browser_tabs", {"action": "open", "url": url})
    assert data["url"] == url
    script = browser.scripts[-1]
    assert url in applescript_literals(script)
    assert "do shell script" not in without_literals(script)
    assert "pwned" not in without_literals(script)


async def test_open_refuses_script_urls_and_line_breaks(client: Client) -> None:
    _, error = await call(client, "browser_tabs", {"action": "open", "url": "javascript:alert(1)"})
    assert error == "BLOCKED_BY_POLICY"
    _, error = await call(client, "browser_tabs", {"action": "open", "url": "https://x.example/\nfoo"})
    assert error == "INVALID_ARGUMENT"
    data = await ok(client, "browser_tabs", {"action": "open", "url": "example.com", "new_window": True})
    assert data["url"] == "https://example.com"


async def test_open_starts_a_browser_that_is_not_running() -> None:
    ctx = make_ctx()
    browser = FakeBrowser()
    runner_of(ctx).register(["osascript"], handler=browser)
    async with connected_client(ctx) as client:
        data = await ok(
            client, "browser_tabs", {"action": "open", "url": "https://x.example", "browser": "chrome"}
        )
        assert data["browser"] == "Google Chrome"
        assert "com.google.Chrome" in browser.scripts[-1]
        _, error = await call(client, "browser_tabs", {"browser": "chrome"})
        assert error == "APP_NOT_FOUND"
        _, error = await call(client, "browser_tabs", {"browser": "netscape"})
        assert error == "INVALID_ARGUMENT"


async def test_no_browser_running() -> None:
    ctx = make_ctx()
    async with connected_client(ctx) as client:
        _, error = await call(client, "browser_tabs", {})
        assert error == "APP_NOT_FOUND"
        _, error = await call(client, "browser_read", {})
        assert error == "APP_NOT_FOUND"


async def test_a_protected_browser_is_refused(browser: FakeBrowser) -> None:
    ctx = make_ctx(**{"safety.deny_apps": ["com.apple.Safari"]})
    runner_of(ctx).register(["osascript"], handler=browser)
    async with connected_client(ctx) as client:
        await call(client, "app", {"action": "launch", "name": "Safari"})
        for tool, args in (
            ("browser_tabs", {"browser": "Safari"}),
            ("browser_tabs", {"action": "open", "url": "https://x.example", "browser": "Safari"}),
            ("browser_read", {"browser": "Safari"}),
            ("browser_eval_js", {"js": "1", "browser": "Safari"}),
        ):
            _, error = await call(client, tool, args)
            assert error == "BLOCKED_BY_POLICY", tool
    assert browser.scripts == []


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("execution error: Not authorized to send Apple events to Safari. (-1743)", "PERMISSION_AUTOMATION"),
        ("execution error: Safari got an error: Can’t get window 9. (-1719)", "INVALID_ARGUMENT"),
        ("execution error: Safari got an error: Application isn’t running. (-600)", "APP_NOT_FOUND"),
    ],
)
async def test_tab_script_errors(client: Client, browser: FakeBrowser, stderr: str, code: str) -> None:
    browser.failure = (1, stderr)
    _, error = await call(client, "browser_tabs", {})
    assert error == code


# =========================================================================== browser_read
async def test_read_the_page_through_accessibility(client: Client, browser: FakeBrowser) -> None:
    data = await ok(client, "browser_read", {})
    assert data["source"] == "accessibility"
    assert data["title"] == "Start Page"
    assert data["text"].splitlines() == [
        "# Favorites",
        "Apple GitHub Hacker News",
        "# Privacy Report",
        "In the last seven days, Safari has prevented 112 trackers from profiling you.",
    ]
    assert data["next_offset"] is None
    assert browser.scripts == []  # no AppleScript, no JavaScript
    linked = await ok(client, "browser_read", {"include_links": True})
    assert "[GitHub](https://github.com)" in linked["text"]


async def test_reading_order_and_inline_runs(tmp_path: Path) -> None:
    page = [
        {"role": "AXHeading", "children": [{"role": "AXStaticText", "value": "Install"}]},
        {
            "role": "AXGroup",
            "children": [
                {"role": "AXStaticText", "value": "Read the "},
                {
                    "role": "AXLink",
                    "url": "https://docs.example",
                    "children": [{"role": "AXStaticText", "value": "docs"}],
                },
                {"role": "AXStaticText", "value": ", then run it."},
            ],
        },
        {"role": "AXList", "children": [
            {"role": "AXGroup", "children": [{"role": "AXStaticText", "value": "first"}]},
            {"role": "AXGroup", "children": [{"role": "AXStaticText", "value": "second"}]},
        ]},
        {"role": "AXTextField", "title": "Email", "value": "a@b.example"},
        {"role": "AXTextField", "title": "Password", "secure": True},
        {"role": "AXButton", "title": "Sign up"},
        {"role": "AXImage", "description": "Logo"},
    ]  # fmt: skip
    ctx = make_ctx(engine=desktop_with_page(tmp_path, page))
    async with connected_client(ctx) as client:
        data = await ok(client, "browser_read", {"browser": "Safari", "include_links": True})
    assert data["text"].splitlines() == [
        "# Install",
        "Read the [docs](https://docs.example), then run it.",
        "first",
        "second",
        "[Email: a@b.example]",
        "[Sign up]",
        "[image: Logo]",
    ]


async def test_long_pages_are_read_in_pieces(tmp_path: Path) -> None:
    paragraphs = [{"role": "AXStaticText", "value": f"Paragraph {n}: " + "word " * 40} for n in range(30)]
    ctx = make_ctx(engine=desktop_with_page(tmp_path, paragraphs))
    async with connected_client(ctx) as client:
        first = await ok(client, "browser_read", {"max_chars": 1000})
        assert len(first["text"]) == 1000
        assert first["next_offset"] == 1000
        collected = first["text"]
        offset = first["next_offset"]
        while offset is not None:
            page = await ok(client, "browser_read", {"max_chars": 1000, "offset": offset})
            collected += page["text"]
            offset = page["next_offset"]
        assert collected.count("Paragraph") == 30
        assert len(collected) == first["total_chars"]
        _, error = await call(client, "browser_read", {"offset": first["total_chars"] + 1})
        assert error == "INVALID_ARGUMENT"


async def test_without_a_web_area_javascript_is_the_fallback(tmp_path: Path) -> None:
    browser = FakeBrowser(js_result="Plain text\nfrom innerText")
    ctx = make_ctx(engine=desktop_with_page(tmp_path, None))
    runner_of(ctx).register(["osascript"], handler=browser)
    async with connected_client(ctx) as client:
        data = await ok(client, "browser_read", {"browser": "Safari"})
    assert data["source"] == "javascript"
    assert data["text"] == "Plain text\nfrom innerText"
    assert "innerText" in browser.scripts[-1]
    assert data["warnings"]


async def test_without_a_web_area_or_javascript_reading_is_not_supported(tmp_path: Path) -> None:
    ctx = make_ctx(engine=desktop_with_page(tmp_path, None), **{"tools.disable": ["browser_eval_js"]})
    async with connected_client(ctx) as client:
        _, error = await call(client, "browser_read", {"browser": "Safari"})
    assert error == "NOT_SUPPORTED"
    assert runner_of(ctx).calls == []


async def test_read_errors_from_the_engine(tmp_path: Path) -> None:
    engine = FaultyEngine(desktop_with_page(tmp_path, []), {"capture_tree": Fault.error(ErrorCode.TIMEOUT)})
    ctx = make_ctx(engine=engine)
    async with connected_client(ctx) as client:
        _, error = await call(client, "browser_read", {"browser": "Safari"})
    assert error == "TIMEOUT"


def test_page_helpers() -> None:
    from axpilot.models import RawNode

    inner = RawNode(role="AXWebArea", children=[RawNode(role="AXStaticText", value="frame text")])
    outer = RawNode(
        role="AXWebArea",
        children=[RawNode(role="AXStaticText", value="main"), RawNode(role="AXGroup", children=[inner])],
    )
    root = RawNode(role="AXWindow", children=[outer])
    assert web_areas(root) == [outer]  # the iframe is read as part of its page
    assert page_lines(outer) == ["main", "frame text"]


# =========================================================================== browser_eval_js
async def test_eval_js_asks_with_the_page_and_the_code(
    client: Client, browser: FakeBrowser, recorder: Recorder
) -> None:
    data = await ok(client, "browser_eval_js", {"js": "document.title"})
    assert data["result"] == "42"
    assert (data["window"], data["tab"], data["url"]) == (1, 2, "https://news.ycombinator.com/")
    (request,) = recorder.requests
    assert request.tool == "browser_eval_js"
    assert request.summary == 'Run JavaScript in "Hacker News" (Safari)?'
    assert request.detail == "Page: https://news.ycombinator.com/\n\ndocument.title"


@pytest.mark.parametrize(
    "js",
    [
        'alert("hi") " & (do shell script "say pwned") & "',
        "const s = 'a\\\\b'; s",
        "line1\nline2\r\n\tindented",
        '\\" & do shell script \\"say pwned',
    ],
)
async def test_javascript_cannot_break_out_of_its_literal(
    client: Client, browser: FakeBrowser, js: str
) -> None:
    await ok(client, "browser_eval_js", {"js": js, "window": 1, "tab": 1})
    script = browser.scripts[-1]
    assert js in applescript_literals(script)
    assert "do shell script" not in without_literals(script)
    assert "pwned" not in without_literals(script)


async def test_eval_js_errors(client: Client, browser: FakeBrowser, recorder: Recorder) -> None:
    for args in ({"js": "  "}, {"js": "1", "window": 1}, {"js": "1", "window": 5, "tab": 1}):
        _, error = await call(client, "browser_eval_js", args)
        assert error == "INVALID_ARGUMENT", args
    assert recorder.requests == []
    browser.failure = (
        1,
        "execution error: Safari got an error: You must enable 'Allow JavaScript from Apple Events'",
    )
    _, error = await call(client, "browser_eval_js", {"js": "1"})
    assert error == "NOT_SUPPORTED"


async def test_a_declined_script_does_not_run(browser: FakeBrowser) -> None:
    ctx = make_ctx(confirm=False)
    runner_of(ctx).register(["osascript"], handler=browser)
    async with connected_client(ctx) as client:
        await ok(client, "app", {"action": "launch", "name": "Safari"})
        _, error = await call(client, "browser_eval_js", {"js": "document.cookie"})
    assert error == "USER_DENIED"
    assert not any("JavaScript" in script for script in browser.scripts)


async def test_browser_tools_are_off_by_default() -> None:
    async with connected_client(make_test_context()) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not names & {"browser_tabs", "browser_read", "browser_eval_js"}
