"""Stable, session-unique refs through the MCP server: carried across snapshots, never reused,
resolved through older snapshots (still policy-checked and re-validated), and robust to engine faults."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Point, Rect
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.input import resolve_target
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio


async def no_sleep(_: float) -> None:
    await anyio.lowlevel.checkpoint()


def fast(ctx: ServerContext) -> ServerContext:
    ctx.sleep = no_sleep
    return ctx


@pytest.fixture
def ctx() -> ServerContext:
    return fast(make_test_context({"tools.profile": "full"}))


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def engine_of(ctx: ServerContext) -> FakeEngine:
    engine = getattr(ctx.engine, "inner", ctx.engine)
    assert isinstance(engine, FakeEngine)
    return engine


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args or {})
    return result, error_code_of(result)


async def snapshot(client: Client, **args: Any) -> dict[str, Any]:
    result, error = await call(client, "snapshot", args)
    assert error is None, text_of(result)
    return structured(result)


def named(data: dict[str, Any]) -> dict[str, str]:
    """name → ref of a snapshot payload (named elements)."""
    return {el["name"]: el["ref"] for el in data["elements"] if el["name"]}


def numbers(data: dict[str, Any], prefix: str = "e") -> set[int]:
    return {int(el["ref"][1:]) for el in data["elements"] if el["ref"].startswith(prefix)}


def note_body(ctx: ServerContext) -> Any:
    engine = engine_of(ctx)
    return engine.element_attributes(engine.resolve_ref("notes.body"))["AXValue"]


# =========================================================================== the reported bug
async def test_old_note_body_ref_still_types_into_the_body_after_new_note(
    ctx: ServerContext, client: Client
) -> None:
    refs = named(await snapshot(client, app="Notes"))
    body = refs["Note body"]
    result, error = await call(client, "click", {"ref": refs["New Note"]})
    assert error is None, text_of(result)
    result, error = await call(client, "type_text", {"ref": body, "text": "Groceries"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["target"]["ref"] == body
    assert data["target"]["role"] == "textarea"
    assert note_body(ctx) == "Groceries"
    assert "row" not in text_of(result).splitlines()[0]
    assert not any("not a text field" in w for w in data.get("warnings", []))
    inserted = [p["handle"] for name, p in engine_of(ctx).events if name in ("insert_text", "set_value")]
    assert inserted[-1] == engine_of(ctx).resolve_ref("notes.body")


async def test_the_diff_after_an_action_keeps_refs_and_never_removes_and_adds_one_element(
    client: Client,
) -> None:
    before = await snapshot(client, app="Notes")
    refs = named(before)
    result, error = await call(client, "click", {"ref": refs["New Note"]})
    assert error is None
    diff = structured(result)["diff"]
    added = {el["ref"] for el in diff["added"]}
    removed = {el["ref"] for el in diff["removed"]}
    assert added.isdisjoint(removed)
    assert removed == set()
    changed = {item["ref"]: item for item in diff["changed"]}
    assert refs["Note body"] in changed  # its value was cleared: same ref, reported as changed
    assert refs["Shopping list — 10:42"] in changed  # unselected, same ref although rows shifted
    new_row = next(el for el in diff["added"] if el["name"].startswith("New Note"))
    assert int(new_row["ref"][1:]) > max(numbers(before))  # a never-used number
    after = await snapshot(client, app="Notes")
    assert {name: ref for name, ref in named(after).items() if name in refs} == refs


async def test_report_snapshot_returns_carried_refs(client: Client) -> None:
    refs = named(await snapshot(client, app="Notes"))
    result, error = await call(client, "click", {"ref": refs["New Note"], "report": "snapshot"})
    assert error is None
    returned = named(structured(result)["snapshot"])
    assert all(returned[name] == ref for name, ref in refs.items() if name in returned)
    assert returned["Note body"] == refs["Note body"]


# =========================================================================== across apps and windows
async def test_a_notes_ref_still_means_notes_after_a_mail_snapshot(
    ctx: ServerContext, client: Client
) -> None:
    engine_of(ctx).app_control("launch", "com.apple.mail")
    notes = await snapshot(client, app="Notes")
    mail = await snapshot(client, app="Mail")
    assert numbers(notes).isdisjoint(numbers(mail))  # numbering is unique across apps
    share = named(notes)["Share"]
    result, error = await call(client, "inspect", {"ref": share})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["snapshot_id"] == notes["snapshot_id"]
    assert data["element"]["name"] == "Share"
    result, error = await call(client, "click", {"ref": named(notes)["New Note"], "report": "none"})
    assert error is None, text_of(result)
    pressed = [p for name, p in engine_of(ctx).events if name in ("press", "perform_action")]
    assert pressed[-1]["handle"] == engine_of(ctx).resolve_ref("notes.new-note")


async def test_snapshots_of_the_same_window_keep_refs_and_others_never_reuse_numbers(
    ctx: ServerContext, client: Client
) -> None:
    first = await snapshot(client, app="Notes")
    textedit = await snapshot(client, app="TextEdit")
    again = await snapshot(client, app="Notes", mode="full")
    interactive = await snapshot(client, app="Notes")
    assert named(interactive) == named(first)
    assert numbers(first).isdisjoint(numbers(textedit))
    full_only = numbers(again) - numbers(first)
    assert full_only
    assert min(full_only) > max(numbers(textedit))  # new elements get never-used numbers
    assert interactive["focused_ref"] == first["focused_ref"]


async def test_elements_of_a_closed_window_give_stale_ref(ctx: ServerContext, client: Client) -> None:
    textedit = await snapshot(client, app="TextEdit")
    bold = next(el["ref"] for el in textedit["elements"] if el["name"] == "Bold")
    engine_of(ctx).window_control(textedit["window"]["window_id"], "close")
    await snapshot(client, app="Notes")
    result, error = await call(client, "click", {"ref": bold})
    assert error == "STALE_REF", text_of(result)
    assert "no longer open" in text_of(result)
    assert not ctx.control.busy
    _, error = await call(client, "click", {"ref": named(await snapshot(client, app="Notes"))["New Note"]})
    assert error is None


async def test_virtualized_rows_scrolled_away_get_new_refs_and_old_ones_go_stale(
    ctx: ServerContext, client: Client
) -> None:
    engine = engine_of(ctx)
    engine.app_control("launch", "com.apple.mail")
    before = await snapshot(client, app="Mail", max_elements=400)
    rows = {el["name"]: el["ref"] for el in before["elements"] if el["role"] == "row" and "—" in el["name"]}
    first_row = next(name for name in rows if name.startswith("Alice Chen"))
    engine.scroll(Point(440, 400), 0, 10)  # 100 points: the first rows scroll out, the rest move up
    after = await snapshot(client, app="Mail", max_elements=400)
    after_rows = {
        el["name"]: el["ref"] for el in after["elements"] if el["role"] == "row" and "—" in el["name"]
    }
    kept = set(rows) & set(after_rows)
    assert kept
    assert all(after_rows[name] == rows[name] for name in kept)  # rows still on screen keep their refs
    new = set(after_rows) - set(rows)
    assert new
    assert min(int(after_rows[name][1:]) for name in new) > max(numbers(before))
    result, error = await call(client, "click", {"ref": rows[first_row]})
    assert error == "STALE_REF", text_of(result)


async def test_ocr_refs_are_session_unique_and_expire_with_their_window(
    ctx: ServerContext, client: Client
) -> None:
    engine_of(ctx).app_control("launch", "Canvas Paint")
    first = await snapshot(client, app="Canvas Paint", mode="vision")
    ocr_first = sorted(numbers(first, "o"))
    assert ocr_first
    await snapshot(client, app="Notes")  # another window: the canvas o-refs stay valid
    o_ref = f"o{ocr_first[0]}"
    result, error = await call(client, "inspect", {"ref": o_ref})
    assert error is None, text_of(result)
    assert structured(result)["snapshot_id"] == first["snapshot_id"]
    second = await snapshot(client, app="Canvas Paint", mode="vision")
    assert min(numbers(second, "o")) > max(ocr_first)  # o numbers are never reused either
    result, error = await call(client, "inspect", {"ref": o_ref})
    assert error == "STALE_REF", text_of(result)
    assert "OCR" in text_of(result)


async def test_wait_for_returns_a_ref_that_later_snapshots_keep(client: Client) -> None:
    result, error = await call(
        client, "wait_for", {"condition": "element", "query": "New Note", "app": "Notes", "timeout_ms": 500}
    )
    assert error is None, text_of(result)
    ref = structured(result)["element"]["ref"]
    assert named(await snapshot(client, app="Notes"))["New Note"] == ref


# =========================================================================== where an old ref is now
async def test_a_ref_only_an_older_snapshot_lists_is_aimed_at_where_the_element_is_now(
    ctx: ServerContext,
) -> None:
    first = await ctx.take_snapshot(app="Notes")
    button = next(el for el in first.all_elements if el.name == "New Note")
    await ctx.take_snapshot(app="Notes", mode="text")  # newer, and it does not list the button
    engine_of(ctx).move_element("notes.new-note", Rect(500, 40, 20, 20))
    target = await resolve_target(ctx, ref=button.ref, snapshot_id=None, x=None, y=None)
    assert target.el is button
    assert (target.point.x, target.point.y) == (510, 50)  # the live frame, not the old snapshot's


async def test_an_explicit_snapshot_id_uses_the_newest_listing_of_the_same_ref(ctx: ServerContext) -> None:
    first = await ctx.take_snapshot(app="Notes")
    button = next(el for el in first.all_elements if el.name == "New Note")
    engine_of(ctx).move_element("notes.new-note", Rect(500, 40, 20, 20))
    second = await ctx.take_snapshot(app="Notes")
    assert second.element(button.ref) is not None  # carried: the same element
    target = await resolve_target(ctx, ref=button.ref, snapshot_id=first.snapshot_id, x=None, y=None)
    assert target.snap is first
    assert (target.point.x, target.point.y) == (510, 50)


async def test_a_row_that_newer_snapshots_no_longer_list_is_stale(ctx: ServerContext, client: Client) -> None:
    first = await ctx.take_snapshot(app="Notes", mode="full")
    inside = next(el for el in first.all_elements if el.role == "cell")
    row = first.element(inside.parent_ref or "")
    assert row is not None
    assert row.role == "row"
    newer = await ctx.take_snapshot(app="Notes", mode="text")
    assert newer.element(row.ref) is None
    for ref in (row.ref, inside.ref):
        with pytest.raises(AXPilotError) as caught:
            await resolve_target(ctx, ref=ref, snapshot_id=None, x=None, y=None)
        assert caught.value.code is ErrorCode.STALE_REF
        assert newer.snapshot_id in caught.value.message
    result, error = await call(client, "click", {"ref": row.ref})
    assert error == "STALE_REF", text_of(result)
    assert "reuse rows" in text_of(result)
    assert not ctx.control.busy


# =========================================================================== errors
@pytest.mark.parametrize(
    ("args", "code", "fragment"),
    [
        ({"ref": "e9999"}, ErrorCode.STALE_REF, "never issued"),
        ({"ref": "o77"}, ErrorCode.STALE_REF, "never issued"),
        ({"ref": "s_zzzz:e1"}, ErrorCode.SNAPSHOT_NOT_FOUND, "s_zzzz"),
        ({"ref": "e1;rm"}, ErrorCode.INVALID_ARGUMENT, "not an element ref"),
    ],
)
async def test_unknown_refs(client: Client, args: dict[str, Any], code: ErrorCode, fragment: str) -> None:
    await snapshot(client, app="Notes")
    for tool in ("inspect", "click"):
        result, error = await call(client, tool, args)
        assert error == code.value, text_of(result)
        assert fragment in text_of(result)


async def test_a_ref_missing_from_an_explicit_snapshot_lists_what_it_has(client: Client) -> None:
    notes = await snapshot(client, app="Notes")
    textedit = await snapshot(client, app="TextEdit")
    ref = named(notes)["New Note"]
    result, error = await call(client, "inspect", {"ref": ref, "snapshot_id": textedit["snapshot_id"]})
    assert error == "STALE_REF"
    low, high = min(numbers(textedit)), max(numbers(textedit))
    assert f"e{low}–e{high}" in text_of(result)
    result, error = await call(client, "inspect", {"ref": ref})
    assert error is None


# =========================================================================== policy & faults
async def test_an_old_ref_is_still_policy_checked(ctx: ServerContext, client: Client) -> None:
    notes = await snapshot(client, app="Notes")
    await snapshot(client, app="TextEdit")
    ctx.config.safety.deny_apps.append("Notes")  # the settings changed; a new connection applies them
    async with connected_client(ctx) as later:
        for tool, args in (
            ("click", {"ref": named(notes)["New Note"]}),
            ("type_text", {"ref": named(notes)["Note body"], "text": "x"}),
            ("inspect", {"ref": named(notes)["Share"]}),
        ):
            result, error = await call(later, tool, args)
            assert error == "BLOCKED_BY_POLICY", (tool, text_of(result))
    acted = {"press", "insert_text", "set_value", "perform_action", "focus"}
    assert not [name for name, _ in engine_of(ctx).events if name in acted]


async def test_an_old_terminal_ref_cannot_be_typed_into() -> None:
    standard = fast(make_test_context({"tools.profile": "standard"}))
    async with connected_client(standard) as other:
        terminal = await snapshot(other, app="Terminal")
        await snapshot(other, app="Notes")
        area = next(el["ref"] for el in terminal["elements"] if el["role"] in ("textarea", "text"))
        result, error = await call(other, "type_text", {"ref": area, "text": "rm -rf ~"})
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert "rm -rf" not in text_of(result)


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (Fault.crash("boom /Users/me/secret"), "INTERNAL"),
        (Fault.hang(10), "TIMEOUT"),
        (Fault.error(ErrorCode.STALE_REF, "element went away"), "STALE_REF"),
    ],
)
async def test_engine_faults_while_revalidating_an_old_ref(fault: Fault, code: str) -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    ctx.engine_timeout_s = 0.1
    ctx.capture_timeout_s = 0.1  # re-validating a ref may re-capture the window: both deadlines apply
    try:
        async with connected_client(ctx) as client:
            notes = await snapshot(client, app="Notes")
            await snapshot(client, app="TextEdit")  # the Notes ref now resolves via an older snapshot
            faulty.set_fault("element_valid", fault)
            result, error = await call(client, "click", {"ref": named(notes)["New Note"]})
            assert error == code, text_of(result)
            assert "/Users/me" not in text_of(result)
            assert "Traceback" not in text_of(result)
            assert not ctx.control.busy
            faulty.clear_faults()
            result, error = await call(client, "click", {"ref": named(notes)["New Note"]})
            assert error is None, text_of(result)
    finally:
        faulty.release()


async def test_a_failure_while_observing_the_result_only_warns() -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    async with connected_client(ctx) as client:
        refs = named(await snapshot(client, app="Notes"))
        faulty.set_fault("capture_tree", Fault.error(ErrorCode.TIMEOUT, "slow app"))
        result, error = await call(client, "click", {"ref": refs["New Note"]})
        assert error is None, text_of(result)
        assert "Could not observe" in text_of(result)
        faulty.clear_faults()
        assert named(await snapshot(client, app="Notes"))["Note body"] == refs["Note body"]
