"""Navigation helpers: containers, page signatures, row collection and page merging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.engine.fake import FakeEngine
from axpilot.perception import build_snapshot
from axpilot.perception.model import Snapshot
from axpilot.perception.navigate import (
    CELL_SEPARATOR,
    ancestors,
    collect_items,
    collect_rows,
    descendants,
    element_text,
    merge_pages,
    overlap_length,
    page_signature,
    scroll_container_for,
    viewport_of,
)

FIXTURE = Path(__file__).parents[2] / "src/axpilot/engine/fixtures/demo_desktop.json"


def mail_rows() -> list[str]:
    """Every message row of the fake Mail app, as collect_rows renders them."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def find(node: dict) -> dict | None:
        if "content_rows" in node:
            return node
        for child in node.get("children", []):
            found = find(child)
            if found is not None:
                return found
        return None

    for window in data["windows"]:
        node = find(window["tree"])
        if node is not None:
            rows = node["content_rows"]["rows"]
            return [CELL_SEPARATOR.join(row) if isinstance(row, list) else row for row in rows]
    raise AssertionError("the demo fixture has no content_rows list")


@pytest.fixture
def mail() -> FakeEngine:
    engine = FakeEngine.demo()
    engine.app_control("launch", "Mail")
    return engine


def snap_of(engine: FakeEngine, app: str = "Mail", sid: str = "s_test", mode: str = "full") -> Snapshot:
    """``full`` mode keeps table cells, which ``read`` needs; interactive mode prunes them."""
    return build_snapshot(engine.capture_tree(app=app), snapshot_id=sid, mode=mode, max_elements=2000)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- overlap / merge
@pytest.mark.parametrize(
    ("tail", "head", "expected"),
    [
        (["a", "b", "c"], ["b", "c", "d"], 2),
        (["a", "b", "c"], ["c"], 1),
        (["a", "b"], ["x", "y"], 0),
        (["a", "b"], ["a", "b"], 2),
        ([], ["a"], 0),
        (["a"], [], 0),
        (["  a  b ", "c"], ["a b", "c", "d"], 2),  # whitespace-tolerant
    ],
)
def test_overlap_length(tail: list[str], head: list[str], expected: int) -> None:
    assert overlap_length(tail, head) == expected


def test_merge_pages_drops_the_overlap_between_pages() -> None:
    pages = [["a", "b", "c"], ["b", "c", "d", "e"], ["e", "f"]]
    assert merge_pages(pages) == ["a", "b", "c", "d", "e", "f"]


def test_merge_pages_handles_empty_and_identical_pages() -> None:
    assert merge_pages([]) == []
    assert merge_pages([[], ["a"], []]) == ["a"]
    assert merge_pages([["a", "b"], ["a", "b"]]) == ["a", "b"]  # did not move: nothing new


@given(
    lines=st.lists(st.text(min_size=1, max_size=6).filter(str.strip), min_size=1, max_size=60, unique=True),
    window=st.integers(min_value=1, max_value=12),
    step=st.integers(min_value=1, max_value=12),
)
def test_merging_overlapping_windows_reconstructs_the_list(lines: list[str], window: int, step: int) -> None:
    normalized = list(dict.fromkeys(" ".join(line.split()) for line in lines))
    step = min(step, window)  # consecutive pages overlap or touch, like scrolling by less than a page
    pages = [normalized[start : start + window] for start in range(0, len(normalized), step)]
    merged = [" ".join(line.split()) for line in merge_pages(pages)]
    assert merged == normalized


# --------------------------------------------------------------------------- containers
def test_the_largest_scroll_container_is_the_message_list(mail: FakeEngine) -> None:
    snap = snap_of(mail)
    container = scroll_container_for(snap, None)
    assert container is not None
    assert container.role in {"scrollarea", "table", "list", "outline"}
    rows = collect_rows(snap, container)
    assert rows
    assert rows[0] == mail_rows()[0]


def test_an_element_inside_a_row_resolves_to_its_scroll_container(mail: FakeEngine) -> None:
    snap = snap_of(mail)
    container = scroll_container_for(snap, None)
    assert container is not None
    inside = [el for el in descendants(snap, container) if el.role == "row"]
    assert inside
    chain = ancestors(snap, inside[0])
    assert container in chain or scroll_container_for(snap, inside[0]) is not None
    assert scroll_container_for(snap, inside[0]) is not None


def test_descendants_of_none_is_every_element(mail: FakeEngine) -> None:
    snap = snap_of(mail)
    assert descendants(snap, None) == list(snap.all_elements)


def test_element_text_prefers_values_of_inputs(mail: FakeEngine) -> None:
    snap = snap_of(mail)
    texts = [element_text(el) for el in snap.all_elements]
    assert any(texts)
    for el in snap.all_elements:
        if el.role in {"field", "search", "textarea"} and el.value:
            assert element_text(el) == el.value


# --------------------------------------------------------------------------- signatures
def test_the_signature_changes_when_the_list_scrolls_and_stops_at_the_end(mail: FakeEngine) -> None:
    snap = snap_of(mail, sid="s_0")
    container = scroll_container_for(snap, None)
    assert container is not None
    viewport = viewport_of(snap, container)
    assert viewport is not None
    center = viewport.center
    first = page_signature(snap, container)
    assert first == page_signature(snap_of(mail, sid="s_again"), scroll_container_for(snap, None))

    mail.scroll(center, 0, 20)
    moved = snap_of(mail, sid="s_1")
    assert page_signature(moved, scroll_container_for(moved, None)) != first

    signatures = []
    for index in range(200):
        mail.scroll(center, 0, 50)
        page = snap_of(mail, sid=f"s_{index + 2}")
        signatures.append(page_signature(page, scroll_container_for(page, None)))
        if len(signatures) >= 2 and signatures[-1] == signatures[-2]:
            break
    assert signatures[-1] == signatures[-2], "the signature must settle once the end is reached"


def test_reading_every_page_collects_each_row_exactly_once(mail: FakeEngine) -> None:
    expected = mail_rows()
    pages: list[list[str]] = []
    previous = None
    for index in range(100):
        snap = snap_of(mail, sid=f"s_{index}")
        container = scroll_container_for(snap, None)
        signature = page_signature(snap, container)
        if signature == previous:
            break
        previous = signature
        pages.append(collect_rows(snap, container))
        viewport = viewport_of(snap, container)
        assert viewport is not None
        mail.scroll(viewport.center, 0, 40)
    merged = merge_pages(pages)
    assert merged == expected
    assert len(merged) == 150


# --------------------------------------------------------------------------- interactive snapshots
def test_a_collapsed_scroll_area_leaves_its_viewport_on_the_list(mail: FakeEngine) -> None:
    snap = snap_of(mail, mode="interactive")
    container = scroll_container_for(snap, None)
    assert container is not None
    assert container.role == "table"
    assert container.frame is not None
    assert container.frame.h > 3000  # the table's frame spans all 150 rows
    viewport = viewport_of(snap, container)
    assert viewport is not None
    assert viewport.h <= 823  # but only the scroll area's part is on screen
    assert container.extra["viewport"] == [200.0, 77.0, 480.0, 823.0]


def test_scrolling_an_interactive_snapshot_at_the_viewport_center_moves_the_list(mail: FakeEngine) -> None:
    snap = snap_of(mail, mode="interactive")
    container = scroll_container_for(snap, None)
    viewport = viewport_of(snap, container)
    assert viewport is not None
    before = page_signature(snap, container)
    mail.scroll(viewport.center, 0, 20)
    after = snap_of(mail, sid="s_after", mode="interactive")
    assert page_signature(after, scroll_container_for(after, None)) != before
    assert mail.content_scroll("mail.messages.scroll")["first"] > 0


def test_rows_of_a_scroll_area_come_from_its_table_one_line_each(mail: FakeEngine) -> None:
    snap = snap_of(mail)
    scroll_areas = [el for el in snap.all_elements if el.role == "scrollarea"]
    assert len(scroll_areas) >= 2  # the mailbox sidebar and the message list
    (messages,) = [sa for sa in scroll_areas if any(d.role == "table" for d in descendants(snap, sa))]
    sidebar = scroll_areas[0]
    assert "Inbox" in collect_rows(snap, sidebar)
    rows = collect_rows(snap, messages)
    assert rows == mail_rows()[: len(rows)]
    assert all(row.count(CELL_SEPARATOR) == 2 for row in rows)


def test_collect_items_pairs_each_line_with_its_element(mail: FakeEngine) -> None:
    snap = snap_of(mail)
    container = scroll_container_for(snap, None)
    items = collect_items(snap, container)
    assert [text for _, text in items] == collect_rows(snap, container)
    for ref, text in items:
        assert ref is not None
        row = snap.element(ref)
        assert row is not None
        assert row.role == "row"
        assert text.startswith(row.name.split(" — ")[0] if row.name else "")


def test_collect_items_of_a_text_area_have_no_refs() -> None:
    engine = FakeEngine.demo()
    engine.set_value(engine.resolve_ref("textedit.body"), "one\ntwo")
    snap = build_snapshot(engine.capture_tree(app="TextEdit"), snapshot_id="s_text", mode="full")
    area = next(el for el in snap.all_elements if el.role == "textarea")
    assert collect_items(snap, area) == [(None, "one"), (None, "two")]
