"""Navigation tools: explore, find (with scrolling), scroll and read; policy, limits, partial
results, the error matrix and fault injection."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp import Client

from axpilot.engine.fake import DEMO_FIXTURE, FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Point
from axpilot.perception.navigate import scroll_container_for
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.navigate import MAX_SCROLLS, _Scroller, merge_items
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio


# =========================================================================== helpers
async def no_sleep(_: float) -> None:
    await anyio.lowlevel.checkpoint()


def fast(ctx: ServerContext) -> ServerContext:
    ctx.sleep = no_sleep
    return ctx


def mail_rows() -> list[str]:
    """The Mail fixture's 150 message rows as read returns them (cells joined by " | ")."""
    data = json.loads(Path(DEMO_FIXTURE).read_text(encoding="utf-8"))
    window = next(w for w in data["windows"] if w["title"].startswith("Inbox"))
    stack = [window["tree"]]
    while stack:
        node = stack.pop()
        if "content_rows" in node:
            return [" | ".join(row) for row in node["content_rows"]["rows"]]
        stack.extend(node.get("children", []))
    raise AssertionError("no content_rows in the Mail fixture")


class HookedEngine(FakeEngine):
    """The demo desktop with a hook that runs before every scroll (to fail or pause mid-loop)."""

    hook: Callable[[int], None] | None = None
    scroll_calls = 0

    def scroll(self, point: Point | None, dx: float, dy: float) -> None:
        self.scroll_calls += 1
        if self.hook is not None:
            self.hook(self.scroll_calls)
        super().scroll(point, dx, dy)


def demo_ctx(engine: FakeEngine | None = None, **overrides: Any) -> ServerContext:
    engine = engine if engine is not None else HookedEngine.demo()
    return fast(make_test_context({"tools.profile": "full", **overrides}, engine=engine))


def mail_engine(offset_lines: float = 0) -> HookedEngine:
    engine = HookedEngine.demo()
    assert isinstance(engine, HookedEngine)
    engine.app_control("launch", "com.apple.mail")
    if offset_lines:
        engine.scroll(Point(440, 400), 0, offset_lines)
    engine.scroll_calls = 0
    return engine


@pytest.fixture
def ctx() -> ServerContext:
    return demo_ctx(mail_engine())


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def engine_of(ctx: ServerContext) -> HookedEngine:
    engine = getattr(ctx.engine, "inner", ctx.engine)
    assert isinstance(engine, HookedEngine)
    return engine


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args or {})
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


def position(ctx: ServerContext) -> dict[str, Any]:
    return engine_of(ctx).content_scroll("mail.messages.scroll")


def events(ctx: ServerContext, kind: str) -> list[dict[str, Any]]:
    return engine_of(ctx).events_of(kind)


def reader_desktop() -> dict[str, Any]:
    """A small desktop: one window whose scroll area holds a long web page (48 paragraphs and two
    buttons below the visible part, one without AXScrollToVisible)."""
    paragraphs = [
        {"role": "AXStaticText", "value": f"Paragraph {i}", "frame": [20, 60 + i * 40, 500, 20]}
        for i in range(48)
    ]
    more = {
        "id": "reader.more",
        "role": "AXButton",
        "title": "Load more",
        "frame": [650, 580, 100, 24],
        "actions": ["AXPress", "AXScrollToVisible"],
    }
    following = {
        "id": "reader.next",
        "role": "AXButton",
        "title": "Next",
        "frame": [650, 600, 100, 24],
        "actions": ["AXPress"],
    }
    page = {
        "id": "reader.page",
        "role": "AXWebArea",
        "description": "Article",
        "frame": [0, 50, 800, 1980],
        "children": [*paragraphs, more, following],
    }
    area = {"id": "reader.scroll", "role": "AXScrollArea", "frame": [0, 50, 800, 400], "children": [page]}
    display = {
        "display_id": 1,
        "frame": [0, 0, 1440, 900],
        "work_area": [0, 25, 1440, 875],
        "scale": 2.0,
        "main": True,
        "name": "Display",
    }
    app = {
        "name": "Reader",
        "bundle_id": "com.example.Reader",
        "pid": 4242,
        "path": "/Applications/Reader.app",
        "frontmost": True,
    }
    window = {
        "window_id": 9001,
        "app": "com.example.Reader",
        "title": "Article",
        "frame": [0, 25, 800, 600],
        "tree": {"role": "AXWindow", "title": "Article", "frame": [0, 25, 800, 600], "children": [area]},
    }
    return {
        "os_version": "15.4",
        "displays": [display],
        "apps": [app],
        "windows": [window],
        "permissions": {"accessibility": True, "screen_recording": True},
    }


def tall_lines_desktop() -> dict[str, Any]:
    """The demo desktop with Mail's scroll "line" 40 points tall instead of 10."""
    data = json.loads(Path(DEMO_FIXTURE).read_text(encoding="utf-8"))
    stack = [w["tree"] for w in data["windows"]]
    while stack:
        node = stack.pop()
        if "content_rows" in node:
            node["content_rows"]["points_per_line"] = 40
        stack.extend(node.get("children", []))
    return data


@pytest.fixture
async def reader() -> AsyncIterator[tuple[ServerContext, Client]]:
    ctx = demo_ctx(HookedEngine.from_dict(reader_desktop()))
    async with connected_client(ctx) as client:
        yield ctx, client


def named(data: dict[str, Any]) -> dict[str, str]:
    return {el["name"]: el["ref"] for el in data["elements"] if el["name"]}


# =========================================================================== explore
async def test_explore_maps_mail(ctx: ServerContext, client: Client) -> None:
    result, error = await call(client, "explore", {"app": "Mail"})
    assert error is None, text_of(result)
    data = structured(result)
    text = text_of(result)
    assert data["app"]["name"] == "Mail"
    assert data["app_kind"] == "native"
    assert data["ax_quality"] == "good"
    assert [w["title"] for w in data["windows"]] == ["Inbox – 150 messages"]
    menus = {m["title"]: m["items"] for m in data["menus"]}
    assert "File" in menus
    assert "Apple" not in menus
    assert all(count >= 1 for count in menus.values())
    assert {"Get Mail", "New Message", "Reply"} <= {el["name"] for el in data["toolbar"]}
    assert {"Inbox", "Sent"} <= {el["name"] for el in data["sections"]}
    table = next(area for area in data["scroll_areas"] if area["name"] == "Messages")
    assert table["rows"] == 150
    assert table["scrollable"] is True
    assert data["elements"] > 10
    assert "menus: " in text
    assert "(use select_menu)" in text
    assert f'area {table["ref"]} table "Messages" 150 rows, scrollable' in text
    # the refs belong to a stored snapshot: other tools can use them right away
    read = await ok(client, "read", {"target": table["ref"], "max_pages": 1})
    assert read["container"]["ref"] == table["ref"]


@pytest.mark.parametrize(
    ("app", "kind", "quality"),
    [("Slack", "electron", None), ("Canvas Paint", None, "poor")],
)
async def test_explore_reports_the_compatibility_profile(
    ctx: ServerContext, client: Client, app: str, kind: str | None, quality: str | None
) -> None:
    engine_of(ctx).app_control("launch", app)
    data = await ok(client, "explore", {"app": app})
    if kind is not None:
        assert data["app_kind"] == kind
    if quality is not None:
        assert data["ax_quality"] == quality
    assert data["notes"]
    assert data["strategies"]


async def test_explore_survives_an_unreadable_menu_bar() -> None:
    faulty = FaultyEngine(mail_engine(), {"menu_bar": Fault.error(ErrorCode.TIMEOUT, "menu bar busy")})
    ctx = demo_ctx(faulty)  # type: ignore[arg-type]
    async with connected_client(ctx) as client:
        result, error = await call(client, "explore", {"app": "Mail"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["menus"] == []
    assert any("menu bar" in w for w in data["warnings"])


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"app": "No Such App"}, ErrorCode.APP_NOT_FOUND),
        ({"app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
        ({"window_id": 999999}, ErrorCode.WINDOW_NOT_FOUND),
        ({"window_id": 801}, ErrorCode.BLOCKED_BY_POLICY),  # System Settings › Privacy & Security
    ],
)
async def test_explore_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "explore", args)
    assert error == code.value, text_of(result)


# =========================================================================== read
async def test_read_collects_every_row_exactly_once(ctx: ServerContext, client: Client) -> None:
    result, error = await call(client, "read", {"app": "Mail"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["lines"] == mail_rows()
    assert data["count"] == 150
    assert data["content"] == "\n".join(mail_rows())
    assert data["complete"] is True
    assert data["at_end"] is True
    assert data["mode"] == "rows"
    assert data["container"]["name"] == "Messages"
    assert data["pages"] > 1
    assert data["scrolls"] <= MAX_SCROLLS
    assert data["warnings"] == []
    assert position(ctx)["at_end"]
    head = text_of(result).splitlines()[0]
    assert "150 rows" in head
    assert "complete" in head
    # the final snapshot is stored: its refs work
    ctx.snapshots.get(data["snapshot_id"])


async def test_read_from_the_middle_goes_to_the_top_first() -> None:
    ctx = demo_ctx(mail_engine(offset_lines=120))
    async with connected_client(ctx) as client:
        data = await ok(client, "read", {"app": "Mail"})
    assert data["lines"] == mail_rows()
    assert data["complete"] is True


async def test_read_without_from_top_starts_where_the_list_is() -> None:
    ctx = demo_ctx(mail_engine(offset_lines=120))
    async with connected_client(ctx) as client:
        data = await ok(client, "read", {"app": "Mail", "from_top": False})
    rows = mail_rows()
    first = rows.index(data["lines"][0])
    assert first > 0
    assert data["lines"] == rows[first:]
    assert data["at_end"] is True
    assert data["complete"] is False
    assert any("from_top=false" in w for w in data["warnings"])


async def test_read_stops_at_max_pages_with_a_partial_result(client: Client) -> None:
    data = await ok(client, "read", {"app": "Mail", "max_pages": 2})
    rows = mail_rows()
    assert data["pages"] == 2
    assert data["complete"] is False
    assert data["at_end"] is False
    assert data["lines"] == rows[: len(data["lines"])]
    assert 30 < len(data["lines"]) < 150
    assert any("max_pages" in w for w in data["warnings"])


async def test_read_restore_puts_the_list_back(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).scroll(Point(440, 400), 0, 130)
    before = position(ctx)["offset"]
    data = await ok(client, "read", {"app": "Mail", "restore": True})
    assert data["complete"] is True
    assert data["restored"] is True
    assert abs(position(ctx)["offset"] - before) <= 10  # within one line


async def test_restoring_ends_when_half_a_line_is_left() -> None:
    """``round()`` rounds 0.5 to 0: restoring 1.5 lines (2, then -0.5 left) must not spin."""
    engine = mail_engine()
    ctx = demo_ctx(engine)
    snap = await ctx.take_snapshot(app="Mail", mode="full")
    container = scroll_container_for(snap, None)
    scroller = _Scroller(ctx=ctx, snap=snap, container=container, label="read", deadline=ctx.clock() + 3)
    scroller.line_points = 10.0
    scroller.moved["y"] = -15.0
    with anyio.fail_after(2):
        await scroller.restore(snap, container)
    assert engine.scroll_calls == 1


async def test_read_measures_the_line_height_before_paging() -> None:
    """A tall scroll "line" (40 points here) would skip rows with the default estimate."""
    engine = HookedEngine.from_dict(tall_lines_desktop())
    engine.app_control("launch", "com.apple.mail")
    ctx = demo_ctx(engine)
    async with connected_client(ctx) as client:
        result = await ok(client, "read", {"app": "Mail", "from_top": False})
        assert result["lines"] == mail_rows()
        assert result["complete"] is True  # it started at the top
        # a later call reuses the measurement: a page is a page
        scrolled = await ok(client, "scroll", {"app": "Mail", "to": "top"})
        assert scrolled["moved"]
        await ok(client, "scroll", {"app": "Mail", "direction": "down"})
    assert 600 <= engine.content_scroll("mail.messages.scroll")["offset"] <= 700


async def test_read_by_ref_of_the_list_or_of_a_row(ctx: ServerContext, client: Client) -> None:
    snap = await ok(client, "snapshot", {"app": "Mail"})
    table = next(el for el in snap["elements"] if el["name"] == "Messages")
    row = next(el for el in snap["elements"] if el["role"] == "row" and "Alice" in (el["name"] or ""))
    by_row = await ok(client, "read", {"target": row["ref"], "snapshot_id": snap["snapshot_id"]})
    assert by_row["container"]["ref"] == table["ref"]
    # the read left the list at its end: the first row scrolled out, so its ref is stale now
    _, error = await call(client, "read", {"target": row["ref"]})
    assert error == "STALE_REF"
    by_table = await ok(client, "read", {"target": table["ref"]})
    assert by_table["lines"] == by_row["lines"] == mail_rows()


async def test_read_a_text_area_returns_its_whole_value_without_scrolling(
    ctx: ServerContext, client: Client
) -> None:
    engine = engine_of(ctx)
    long_text = "\n".join(f"line {i}: " + "x" * 50 for i in range(300))
    engine.set_value(engine.resolve_ref("textedit.body"), long_text)
    snap = await ok(client, "snapshot", {"app": "TextEdit"})
    body = next(el for el in snap["elements"] if el["role"] == "textarea")
    assert len(body["value"]) < len(long_text)  # the snapshot truncates long values
    data = await ok(client, "read", {"target": body["ref"]})
    assert data["lines"] == long_text.splitlines()
    assert data["complete"] is True
    assert data["scrolls"] == 0
    assert data["mode"] == "text"
    assert not events(ctx, "scroll")


async def test_read_a_terminal_text_area_needs_no_input_permission() -> None:
    ctx = fast(make_test_context({"tools.profile": "standard"}, engine=HookedEngine.demo()))
    async with connected_client(ctx) as client:
        snap = await ok(client, "snapshot", {"app": "Terminal"})
        shell = next(el for el in snap["elements"] if el["role"] == "textarea")
        data = await ok(client, "read", {"target": shell["ref"]})
    assert data["complete"] is True
    assert data["scrolls"] == 0


async def test_read_a_web_page_in_text_mode(reader: tuple[ServerContext, Client]) -> None:
    _, client = reader
    data = await ok(client, "read", {})
    paragraphs = [line for line in data["lines"] if line.startswith("Paragraph")]
    assert paragraphs == [f"Paragraph {i}" for i in range(48)]
    assert data["mode"] == "text"
    assert data["container"]["role"] == "webarea"
    assert data["complete"] is True
    assert len(data["lines"]) == len(set(data["lines"]))


async def test_read_cuts_huge_content_and_says_so(ctx: ServerContext, client: Client) -> None:
    engine = engine_of(ctx)
    engine.set_value(engine.resolve_ref("textedit.body"), "\n".join("y" * 900 for _ in range(100)))
    snap = await ok(client, "snapshot", {"app": "TextEdit"})
    body = next(el for el in snap["elements"] if el["role"] == "textarea")
    data = await ok(client, "read", {"target": body["ref"]})
    assert data["truncated"] is True
    assert data["complete"] is False
    assert len(data["content"]) <= 60_000
    assert any("cut" in w for w in data["warnings"])


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"app": "No Such App"}, ErrorCode.APP_NOT_FOUND),
        ({"app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
        ({"target": "e99999"}, ErrorCode.STALE_REF),
        ({"target": "e1", "app": "Mail"}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "s_zzzz:e1"}, ErrorCode.SNAPSHOT_NOT_FOUND),
        ({"target": "auto", "max_pages": 31}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "auto", "timeout_ms": 10}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "not a ref"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_read_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await ok(client, "snapshot", {"app": "Mail"})
    result, error = await call(client, "read", args)
    assert error == code.value, text_of(result)


async def test_read_refuses_ocr_refs(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).app_control("launch", "Canvas Paint")
    snap = await ok(client, "snapshot", {"app": "Canvas Paint", "mode": "vision"})
    o_ref = next(el["ref"] for el in snap["elements"] if el["ref"].startswith("o"))
    result, error = await call(client, "read", {"target": o_ref})
    assert error == "INVALID_ARGUMENT", text_of(result)


# =========================================================================== partial results
async def test_read_stops_at_its_deadline_with_what_it_has() -> None:
    ctx = demo_ctx(mail_engine())
    ticks = itertools.count()
    ctx.clock = lambda: next(ticks) * 3.0  # every clock read is 3 s later
    async with connected_client(ctx) as client:
        data = await ok(client, "read", {"app": "Mail", "timeout_ms": 10_000})
    assert data["complete"] is False
    assert 0 < data["count"] < 150
    assert data["lines"] == mail_rows()[: data["count"]]
    assert any("time limit" in w for w in data["warnings"])


async def test_an_engine_timeout_mid_read_returns_the_pages_so_far() -> None:
    engine = mail_engine()

    def fail_third(n: int) -> None:
        if n == 3:
            raise AXPilotError(ErrorCode.TIMEOUT, "Mail stopped responding")

    engine.hook = fail_third
    ctx = demo_ctx(engine)
    async with connected_client(ctx) as client:
        data = await ok(client, "read", {"app": "Mail"})
        assert data["complete"] is False
        assert data["lines"] == mail_rows()[: data["count"]]
        assert any("TIMEOUT" in w for w in data["warnings"])
        assert not ctx.control.busy
        engine.hook = None
        assert (await ok(client, "read", {"app": "Mail"}))["complete"] is True


async def test_the_user_taking_over_mid_read_ends_it_early() -> None:
    engine = mail_engine()

    def user_moves(n: int) -> None:
        if n == 2:
            engine.hid_idle = 0.05

    engine.hook = user_moves
    ctx = demo_ctx(engine)
    async with connected_client(ctx) as client:
        data = await ok(client, "read", {"app": "Mail"})
    assert data["complete"] is False
    assert any("USER_ACTIVE" in w for w in data["warnings"])


async def test_pausing_mid_read_is_an_error_not_a_partial_result() -> None:
    engine = mail_engine()
    ctx = demo_ctx(engine)
    engine.hook = lambda n: ctx.control.pause("hotkey") if n == 2 else None
    async with connected_client(ctx) as client:
        result, error = await call(client, "read", {"app": "Mail"})
    assert error == "PAUSED_BY_USER", text_of(result)
    assert not ctx.control.busy


@pytest.mark.parametrize(("idle", "code"), [(0.05, "USER_ACTIVE"), (999, "PAUSED_BY_USER")])
async def test_nothing_is_scrolled_when_the_first_scroll_is_refused(idle: float, code: str) -> None:
    engine = mail_engine()
    engine.hid_idle = idle
    ctx = demo_ctx(engine)
    if code == "PAUSED_BY_USER":
        ctx.control.pause("hotkey")
    async with connected_client(ctx) as client:
        for tool, args in (
            ("read", {"app": "Mail"}),
            ("find", {"app": "Mail", "text": "zzz", "scroll": True}),
        ):
            _, error = await call(client, tool, args)
            assert error == code
    assert engine.scroll_calls == 0


async def test_a_busy_input_lock_is_reported(ctx: ServerContext, client: Client) -> None:
    ctx.input_lock_timeout_s = 0.05
    holding, release = anyio.Event(), anyio.Event()
    error: str | None = None

    async def hold() -> None:
        async with ctx.control.input_turn(label="someone else"):
            holding.set()
            await release.wait()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await holding.wait()
        _, error = await call(client, "read", {"app": "Mail"})
        release.set()
    assert error == "BUSY"
    assert engine_of(ctx).scroll_calls == 0


async def test_read_and_find_count_once_against_the_rate_limit() -> None:
    ctx = demo_ctx(mail_engine(), **{"safety.max_actions_per_minute": 2})
    async with connected_client(ctx) as client:
        first = await ok(client, "read", {"app": "Mail"})
        assert first["scrolls"] > 2  # many scrolls, one action
        await ok(client, "find", {"app": "Mail", "text": "zzz", "scroll": True, "max_scrolls": 3})
        _, error = await call(client, "read", {"app": "Mail"})
    assert error == "RATE_LIMITED"


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (Fault.crash("boom in /Users/someone/secret"), "INTERNAL"),
        (Fault.error(ErrorCode.PERMISSION_ACCESSIBILITY, "no AX"), "PERMISSION_ACCESSIBILITY"),
        (Fault.hang(10), "TIMEOUT"),
    ],
)
async def test_engine_faults_while_reading(fault: Fault, code: str) -> None:
    faulty = FaultyEngine(mail_engine())
    ctx = demo_ctx(faulty)  # type: ignore[arg-type]
    ctx.engine_timeout_s = 0.1
    ctx.capture_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            faulty.set_fault("capture_tree", fault)
            result, error = await call(client, "read", {"app": "Mail"})
            assert error == code, text_of(result)
            assert "/Users/someone" not in text_of(result)
            assert "Traceback" not in text_of(result)
            assert not ctx.control.busy
            faulty.clear_faults()
            ctx.engine_timeout_s = ctx.capture_timeout_s = 5.0
            assert (await ok(client, "read", {"app": "Mail"}))["complete"] is True
    finally:
        faulty.release()


async def test_a_crash_while_scrolling_is_internal_and_releases_the_lock() -> None:
    engine = mail_engine()
    engine.hook = lambda n: (_ for _ in ()).throw(RuntimeError("scroll bug")) if n == 2 else None
    ctx = demo_ctx(engine)
    async with connected_client(ctx) as client:
        _, error = await call(client, "read", {"app": "Mail"})
        assert error == "INTERNAL"
        assert not ctx.control.busy
        engine.hook = None
        _, again = await call(client, "read", {"app": "Mail"})
        assert again is None


# =========================================================================== merging pages
def test_merge_by_ref_keeps_repeated_lines() -> None:
    pages = [
        [("e1", "Re: hi"), ("e2", "Re: hi"), ("e3", "Lunch")],
        [("e2", "Re: hi"), ("e3", "Lunch"), ("e4", "Re: hi"), ("e5", "Re: hi")],
    ]
    assert merge_items(pages) == (["Re: hi", "Re: hi", "Lunch", "Re: hi", "Re: hi"], 0)


def test_merge_falls_back_to_text_overlap_when_refs_change() -> None:
    pages = [[("e1", "a"), ("e2", "b"), ("e3", "c")], [("e7", "b"), ("e8", "c"), ("e9", "d")]]
    assert merge_items(pages) == (["a", "b", "c", "d"], 0)


def test_merge_counts_pages_without_any_overlap_as_possible_gaps() -> None:
    pages = [[("e1", "a"), ("e2", "b")], [("e7", "x"), ("e8", "y")]]
    assert merge_items(pages) == (["a", "b", "x", "y"], 1)


def test_a_header_shared_by_every_page_does_not_hide_a_gap() -> None:
    """Finder's list view lists its column header as a row on every page (macOS 26)."""
    header = ("e1", "Name Date Modified Size Kind")
    overlapping = [
        [header, ("e2", "file-001"), ("e3", "file-002")],
        [header, ("e3", "file-002"), ("e4", "file-003")],
    ]
    skipped = [
        [header, ("e2", "file-001"), ("e3", "file-002")],
        [header, ("e9", "file-008"), ("e10", "file-009")],
    ]
    assert merge_items(overlapping) == ([header[1], "file-001", "file-002", "file-003"], 0)
    assert merge_items(skipped) == ([header[1], "file-001", "file-002", "file-008", "file-009"], 1)


@settings(max_examples=150, deadline=None)
@given(
    total=st.integers(min_value=1, max_value=120),
    window=st.integers(min_value=2, max_value=30),
    step=st.integers(min_value=1, max_value=29),
    texts=st.lists(st.sampled_from(["Re: hi", "Lunch", "Invoice", ""]), min_size=120, max_size=120),
)
def test_merging_overlapping_windows_reconstructs_the_list(
    total: int, window: int, step: int, texts: list[str]
) -> None:
    """Pages are overlapping windows of one list (stable refs, repeated texts), as read scrolls
    less than a screen at a time: the merge has every item exactly once, in order."""
    step = min(step, window - 1)
    items = [(f"e{i + 1}", texts[i] or f"line {i}") for i in range(total)]
    pages = [items[start : start + window] for start in range(0, total, step)]
    lines, gaps = merge_items(pages)
    assert lines == [text for _, text in items]
    assert gaps == 0


# =========================================================================== find
async def test_find_without_scrolling(ctx: ServerContext, client: Client) -> None:
    result, error = await call(client, "find", {"app": "Notes", "text": "New Note"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["found"] is True
    best = data["matches"][0]
    assert best["name"] == "New Note"
    assert best["score"] > 0.9
    assert data["scrolls"] == 0
    assert text_of(result).startswith('found "New Note"')
    clicked = await ok(client, "click", {"ref": best["ref"]})  # the ref works
    assert clicked["target"]["name"] == "New Note"


async def test_find_scrolls_until_the_element_appears(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "find", {"app": "Mail", "text": "Invoice (94)", "scroll": True})
    assert data["found"] is True
    assert data["scrolls"] > 0
    best = data["matches"][0]
    assert best["name"] == "Invoice (94)"
    assert best["score"] > 0.9
    inspected = await ok(client, "inspect", {"ref": best["ref"]})
    assert inspected["element"]["name"] == "Invoice (94)"
    assert data["container"]["name"] == "Messages"
    first, last = position(ctx)["first"], position(ctx)["last"]
    assert first <= 93 <= last  # row 94 is on screen


async def test_find_reports_not_found_after_searching_everything(ctx: ServerContext, client: Client) -> None:
    result, error = await call(
        client, "find", {"app": "Mail", "text": "Quantum entanglement", "scroll": True}
    )
    assert error is None
    data = structured(result)
    assert data["found"] is False
    assert data["at_end"] is True
    assert data["searched_all"] is True
    assert all(match["score"] < 0.45 for match in data["matches"])
    assert "searched everything" in text_of(result) or "no good match" in text_of(result)


async def test_find_from_the_middle_also_searches_above() -> None:
    ctx = demo_ctx(mail_engine(offset_lines=200))
    async with connected_client(ctx) as client:
        data = await ok(client, "find", {"app": "Mail", "text": "Design review (2)", "scroll": True})
    assert data["found"] is True
    assert data["matches"][0]["name"] == "Design review (2)"


async def test_find_gives_up_after_max_scrolls(client: Client) -> None:
    data = await ok(
        client, "find", {"app": "Mail", "text": "Team photo (150)", "scroll": True, "max_scrolls": 2}
    )
    assert data["found"] is False
    assert data["searched_all"] is False
    assert data["scrolls"] == 2
    assert any("max_scrolls" in w for w in data["warnings"])


async def test_find_by_role_and_inside_a_container(client: Client) -> None:
    snap = await ok(client, "snapshot", {"app": "Mail"})
    outline = next(el for el in snap["elements"] if el["name"] == "Mailboxes")
    data = await ok(client, "find", {"role": "row", "container": outline["ref"], "limit": 50})
    names = {match["name"] for match in data["matches"]}
    assert {"Inbox", "Drafts", "Sent", "Junk", "Archive"} <= names
    assert not any("Alice" in (name or "") for name in names)  # message rows are outside
    buttons = await ok(client, "find", {"app": "Mail", "role": "button"})
    assert {"Get Mail", "Reply"} <= {match["name"] for match in buttons["matches"]}


async def test_find_needs_no_scroll_area_to_search(reader: tuple[ServerContext, Client]) -> None:
    _, client = reader
    data = await ok(client, "find", {"text": "Paragraph 40", "scroll": True})
    assert data["found"] is True
    assert data["scrolls"] > 0


async def test_find_scrolling_in_a_terminal_is_blocked() -> None:
    ctx = fast(make_test_context({"tools.profile": "standard"}, engine=HookedEngine.demo()))
    async with connected_client(ctx) as client:
        result, error = await call(
            client, "find", {"app": "Terminal", "text": "zzz-not-there", "scroll": True}
        )
        assert error == "BLOCKED_BY_POLICY", text_of(result)
        _, error = await call(client, "find", {"app": "Terminal", "text": "zzz-not-there"})
        assert error is None


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"app": "Mail"}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "Mail", "text": "   "}, ErrorCode.INVALID_ARGUMENT),
        ({"text": "x", "container": "e1", "app": "Mail"}, ErrorCode.INVALID_ARGUMENT),
        ({"text": "x", "container": "e99999"}, ErrorCode.STALE_REF),
        ({"text": "x", "app": "No Such App"}, ErrorCode.APP_NOT_FOUND),
        ({"text": "x", "app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
        ({"text": "x", "max_scrolls": 51}, ErrorCode.INVALID_ARGUMENT),
        ({"text": "x", "limit": 0}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_find_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await ok(client, "snapshot", {"app": "Mail"})
    result, error = await call(client, "find", args)
    assert error == code.value, text_of(result)


# =========================================================================== scroll
async def test_scroll_a_list_by_a_page(ctx: ServerContext, client: Client) -> None:
    snap = await ok(client, "snapshot", {"app": "Mail"})
    table = next(el for el in snap["elements"] if el["name"] == "Messages")
    result, error = await call(client, "scroll", {"ref": table["ref"]})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["moved"] is True
    assert data["at_end"] is False
    assert data["scrolls"] == 1
    assert data["container"]["ref"] == table["ref"]
    offset = position(ctx)["offset"]
    assert 600 <= offset <= 700  # 80% of the 823-point viewport
    added = {el["name"] for el in data["diff"]["added"] if el["role"] == "row"}
    assert any("(40)" in name for name in added)
    assert data["diff"]["counts"]["removed"] > 0
    # rows that scrolled away are removed, not "renamed"; only the scroll bar's value changed
    assert [item["role"] for item in data["diff"]["changed"]] == ["scrollbar"]
    scroll_event = events(ctx, "scroll")[-1]
    assert 200 <= scroll_event["point"][0] <= 680


async def test_scroll_to_the_bottom_and_back(ctx: ServerContext, client: Client) -> None:
    down = await ok(client, "scroll", {"app": "Mail", "to": "bottom", "report": "none"})
    assert down["moved"] is True
    assert down["at_end"] is True
    assert position(ctx)["at_end"]
    again = await ok(client, "scroll", {"app": "Mail", "to": "bottom", "report": "none"})
    assert again["moved"] is False
    up = await ok(client, "scroll", {"app": "Mail", "to": "top", "report": "none"})
    assert up["moved"] is True
    assert position(ctx)["at_top"]
    assert up["scrolls"] <= 6  # growing jumps, not a page at a time


async def test_scroll_lines_and_the_top_edge(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "scroll", {"app": "Mail", "amount": 3, "unit": "lines", "report": "none"})
    assert position(ctx)["offset"] == 30
    assert data["moved"] is True
    await ok(client, "scroll", {"app": "Mail", "direction": "up", "amount": 3, "unit": "lines"})
    result, error = await call(client, "scroll", {"app": "Mail", "direction": "up"})
    assert error is None
    stuck = structured(result)
    assert stuck["moved"] is False
    assert stuck["at_end"] is True
    assert "did not move up" in text_of(result)


async def test_scroll_at_a_point(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "scroll", {"x": 440, "y": 400, "report": "none"})
    assert data["moved"] is True
    assert data["point"] == [440, 400]
    assert events(ctx, "scroll")[-1]["point"] == [440, 400]


async def test_scroll_report_snapshot(client: Client) -> None:
    data = await ok(client, "scroll", {"app": "Mail", "report": "snapshot"})
    assert data["snapshot"]["snapshot_id"] == data["snapshot_id"]
    assert data["diff"] is None


async def test_scroll_into_view_with_accessibility(reader: tuple[ServerContext, Client]) -> None:
    ctx, client = reader
    refs = named(await ok(client, "snapshot", {}))
    data = await ok(client, "scroll", {"scroll_into_view": refs["Load more"]})
    assert data["visible"] is True
    assert data["strategy"] == "ax"
    assert data["scrolls"] == 0
    assert engine_of(ctx).events_of("scroll_to_visible")


async def test_scroll_into_view_by_scrolling_when_ax_cannot(reader: tuple[ServerContext, Client]) -> None:
    _, client = reader
    refs = named(await ok(client, "snapshot", {}))
    data = await ok(client, "scroll", {"scroll_into_view": refs["Next"]})
    assert data["visible"] is True
    assert data["strategy"] == "scroll"
    assert data["scrolls"] >= 1
    after = await ok(client, "snapshot", {})
    button = next(el for el in after["elements"] if el["name"] == "Next")
    assert button["ref"] == refs["Next"]
    assert button["frame"][1] + button["frame"][3] <= 450 + 1  # inside the scroll area


async def test_scroll_into_view_of_a_visible_element_does_nothing(client: Client) -> None:
    snap = await ok(client, "snapshot", {"app": "Mail"})
    row = next(el for el in snap["elements"] if el["role"] == "row" and "Alice" in (el["name"] or ""))
    data = await ok(client, "scroll", {"scroll_into_view": row["ref"]})
    assert data["visible"] is True
    assert data["moved"] is False


async def test_scrolling_a_terminal_needs_the_shell_tool() -> None:
    ctx = fast(make_test_context({"tools.profile": "standard"}, engine=HookedEngine.demo()))
    async with connected_client(ctx) as client:
        result, error = await call(client, "scroll", {"app": "Terminal"})
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert "Terminal" in text_of(result)


async def test_scrolling_onto_a_deny_listed_app_is_blocked(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).app_control("activate", "1Password")
    result, error = await call(client, "scroll", {"x": 1950, "y": 840})
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert not events(ctx, "scroll")


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"ref": "e1", "x": 1, "y": 2}, ErrorCode.INVALID_ARGUMENT),
        ({"x": 400}, ErrorCode.INVALID_ARGUMENT),
        ({"x": -9000, "y": 5}, ErrorCode.INVALID_ARGUMENT),
        ({"scroll_into_view": "e1", "ref": "e2"}, ErrorCode.INVALID_ARGUMENT),
        ({"scroll_into_view": "e1", "app": "Mail"}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e1", "app": "Mail"}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "Mail", "amount": 0}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "Mail", "direction": "sideways"}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e99999"}, ErrorCode.STALE_REF),
        ({"scroll_into_view": "e99999"}, ErrorCode.STALE_REF),
        ({"app": "No Such App"}, ErrorCode.APP_NOT_FOUND),
        ({"app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
    ],
)
async def test_scroll_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await ok(client, "snapshot", {"app": "Mail"})
    result, error = await call(client, "scroll", args)
    assert error == code.value, text_of(result)


async def test_scroll_needs_something_scrollable(ctx: ServerContext, client: Client) -> None:
    refs = named(await ok(client, "snapshot", {"app": "Notes"}))
    result, error = await call(client, "scroll", {"ref": refs["New Note"]})
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)
    engine_of(ctx).app_control("launch", "com.apple.calculator")
    result, error = await call(client, "scroll", {"app": "Calculator"})
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)


async def test_scroll_is_an_input_action(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).hid_idle = 0.05
    _, active = await call(client, "scroll", {"app": "Mail"})
    engine_of(ctx).hid_idle = 999
    ctx.control.pause("hotkey")
    _, paused = await call(client, "scroll", {"app": "Mail"})
    assert (active, paused) == ("USER_ACTIVE", "PAUSED_BY_USER")
    assert not events(ctx, "scroll")


async def test_a_scroll_that_runs_out_of_time_reports_how_far_it_got() -> None:
    ctx = demo_ctx(mail_engine())
    ticks = itertools.count()
    ctx.clock = lambda: next(ticks) * 25.0
    async with connected_client(ctx) as client:
        result, error = await call(client, "scroll", {"app": "Mail", "to": "bottom", "report": "none"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["at_end"] is False
    assert "stopped after" in data["message"]


@pytest.mark.parametrize(
    ("fault", "code"),
    [(Fault.crash("scroll bug"), "INTERNAL"), (Fault.hang(10), "TIMEOUT")],
)
async def test_engine_faults_while_scrolling(fault: Fault, code: str) -> None:
    faulty = FaultyEngine(mail_engine())
    ctx = demo_ctx(faulty)  # type: ignore[arg-type]
    ctx.engine_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            faulty.set_fault("scroll", fault)
            result, error = await call(client, "scroll", {"app": "Mail"})
            assert error == code, text_of(result)
            assert not ctx.control.busy
            faulty.clear_faults()
            assert (await ok(client, "scroll", {"app": "Mail"}))["moved"] is True
    finally:
        faulty.release()
