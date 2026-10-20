"""Session refs: the counter, the matching engine and carrying refs from snapshot to snapshot."""

from __future__ import annotations

import dataclasses
import itertools
import logging
import threading
from collections.abc import Sequence
from typing import Any

import pytest

from axpilot.models import AppInfo, OcrLine, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import Element, Snapshot, build_snapshot, merge_vision, ocr_elements
from axpilot.perception.refs import (
    OVERLAP_MIN_IOU,
    ElementMatcher,
    HandleIndex,
    MatchPass,
    RefCounter,
    carry_refs,
    iou,
    rounded_frame,
    same_handle,
    split_ref,
    view_of,
)

_ids = itertools.count(1)


# --------------------------------------------------------------------------- builders
def n(
    role: str,
    *children: RawNode,
    frame: tuple[float, float, float, float] | None = None,
    h: Any = None,
    **kw: Any,
) -> RawNode:
    """A node with an explicit engine handle ``h`` (handles say which element it is)."""
    return RawNode(role=role, frame=Rect(*frame) if frame else None, children=list(children), handle=h, **kw)


def button(title: str | None, x: float, *, h: Any, y: float = 10, **kw: Any) -> RawNode:
    return n("AXButton", frame=(x, y, 60, 20), h=h, title=title, actions=("AXPress",), **kw)


def row(text: str, y: float, *, h: Any, cell: Any = None, text_h: Any = None, **kw: Any) -> RawNode:
    static = n("AXStaticText", frame=(4, y + 2, 200, 16), h=text_h, value=text)
    return n("AXRow", n("AXCell", static, frame=(0, y, 300, 20), h=cell), frame=(0, y, 300, 20), h=h, **kw)


def tree(*children: RawNode, pid: int = 100, window_id: int | None = 7, app: str = "Demo") -> RawTree:
    root = n("AXWindow", *children, frame=(0, 0, 800, 600), title="Doc", h=f"win{window_id}")
    info = AppInfo(name=app, pid=pid, bundle_id=f"com.example.{app.lower()}")
    window = (
        WindowInfo(window_id=window_id, pid=pid, app=app, title="Doc", frame=Rect(0, 0, 800, 600))
        if window_id is not None
        else None
    )
    return RawTree(app=info, window=window, root=root)


def take(
    counter: RefCounter,
    t: RawTree,
    previous: Snapshot | None = None,
    *,
    history: Sequence[Snapshot] = (),
    **kwargs: Any,
) -> Snapshot:
    raw = build_snapshot(t, snapshot_id=f"s_{next(_ids):04d}", **kwargs)
    return carry_refs(raw, previous, counter, history=history)


def refs(snap: Snapshot) -> dict[str, str]:
    """name → ref (named elements only)."""
    return {el.name: el.ref for el in snap.all_elements if el.name}


def by_handle(snap: Snapshot) -> dict[Any, str]:
    return {el.handle: el.ref for el in snap.all_elements}


def numbers(snap: Snapshot) -> list[int]:
    return [int(el.ref[1:]) for el in snap.all_elements]


@pytest.fixture
def counter() -> RefCounter:
    return RefCounter()


# =========================================================================== the counter
def test_counter_issues_increasing_refs_per_prefix() -> None:
    counter = RefCounter()
    assert [counter.next() for _ in range(3)] == ["e1", "e2", "e3"]
    assert counter.next("o") == "o1"
    assert counter.next() == "e4"
    allocate = counter.allocator("o")
    assert [allocate(), allocate()] == ["o2", "o3"]
    assert (counter.issued(), counter.issued("o"), counter.issued("x")) == (4, 3, 0)


def test_counter_reserve_skips_numbers_used_elsewhere() -> None:
    counter = RefCounter()
    counter.reserve(["e7", "o2", "e3", "garbage", "s_ab12:e9"])
    assert counter.next() == "e8"
    assert counter.next("o") == "o3"
    counter.reserve(["e2"])  # lower numbers change nothing
    assert counter.next() == "e9"
    counter.reserve([])
    assert counter.issued() == 9


@pytest.mark.parametrize("prefix", ["", "E", "eo", "1", "é"])
def test_counter_rejects_bad_prefixes(prefix: str) -> None:
    counter = RefCounter()
    with pytest.raises(ValueError, match="prefix"):
        counter.next(prefix)
    with pytest.raises(ValueError, match="prefix"):
        counter.allocator(prefix)


def test_counters_have_distinct_sessions() -> None:
    assert RefCounter().session != RefCounter().session


def test_counter_is_thread_safe() -> None:
    counter = RefCounter()
    issued: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        mine = [counter.next() for _ in range(500)]
        with lock:
            issued.extend(mine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(issued) == len(set(issued)) == 4000
    assert counter.issued() == 4000


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("e12", ("e", 12)), ("o3", ("o", 3)), ("e0", None), ("e", None), ("E1", None), ("12", None)],
)
def test_split_ref(ref: str, expected: tuple[str, int] | None) -> None:
    assert split_ref(ref) == expected


# =========================================================================== handles
class Unhashable:
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, key: str) -> None:
        self.key = key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Unhashable) and other.key == self.key


class Exploding:
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("CFEqual failed")

    def __hash__(self) -> int:
        return 1


def test_same_handle(caplog: pytest.LogCaptureFixture) -> None:
    marker = object()
    assert same_handle(marker, marker)
    assert same_handle("n1", "n1")
    assert not same_handle("n1", "n2")
    assert not same_handle(None, None)
    assert not same_handle("n1", None)
    assert same_handle(Unhashable("a"), Unhashable("a"))
    with caplog.at_level(logging.DEBUG, logger="axpilot.perception.refs"):
        assert not same_handle(Exploding(), Exploding())
    assert "Cannot compare element handles" in caplog.text


def test_handle_index_finds_hashable_and_unhashable_handles() -> None:
    index = HandleIndex([("a", 0), (Unhashable("x"), 1), (None, 2), ("a", 3), (Unhashable("y"), 4)])
    assert index.find("a") == [0, 3]
    assert index.find(Unhashable("x")) == [1]  # unhashable key: compared one by one
    assert index.find("zzz") == []
    assert index.find(None) == []
    first, second = Exploding(), Exploding()  # equal hashes: == runs, and raises
    guarded = HandleIndex([(first, 0), (second, 1)])
    assert guarded.find(first) == [0]
    assert guarded.find(Exploding()) == []


# =========================================================================== matching engine
def el(
    name: str | None,
    *,
    role: str = "AXButton",
    frame: tuple[float, float, float, float] | None = None,
    h: Any = None,
) -> Element:
    t = tree(n(role, frame=frame or (0, 0, 10, 10), h=h, title=name, actions=("AXPress",)))
    return build_snapshot(t, snapshot_id="s_test").all_elements[0]


def by_name(element: Element) -> str | None:
    return element.name


def test_matcher_key_pass_takes_the_first_acceptable_candidate() -> None:
    before = [el("A"), el("A"), el("B")]
    after = [el("A"), el("B")]
    vetoed = MatchPass("name", key=by_name, accept=lambda b, a: b is not before[0])
    assert ElementMatcher(before, after).run([MatchPass("name", key=by_name)]) == {0: 0, 1: 2}
    assert ElementMatcher(before, after).run([vetoed]) == {0: 1, 1: 2}


def test_matcher_unique_pass_skips_ambiguous_keys() -> None:
    before = [el("A"), el("A"), el("B")]
    after = [el("A"), el("B"), el("B")]
    assert ElementMatcher(before, after).run([MatchPass("name", key=by_name, unique=True)]) == {}
    assert ElementMatcher(before[1:], after[:2]).run([MatchPass("name", key=by_name, unique=True)]) == {
        0: 0,
        1: 1,
    }


def test_matcher_overlap_pass_picks_the_best_overlap_above_the_threshold() -> None:
    before = [
        el(None, frame=(0, 0, 100, 100)),
        el(None, frame=(10, 0, 100, 100)),
        el(None, frame=(300, 0, 10, 10)),
    ]
    after = [el(None, frame=(12, 0, 100, 100)), el(None, frame=(305, 5, 10, 10))]
    overlap = MatchPass("overlap", kind="overlap", key=lambda e: e.ax_role)
    assert iou(Rect(300, 0, 10, 10), Rect(305, 5, 10, 10)) < OVERLAP_MIN_IOU
    assert ElementMatcher(before, after).run([overlap]) == {0: 1}


def test_matcher_handle_pass_allowed_hook_and_callbacks() -> None:
    before = [el("A", h="h1"), el("B", h="h2"), el("C", h=None)]
    after = [el("B2", h="h2"), el("A2", h="h1"), el("C", h=None)]
    seen: list[tuple[int, int]] = []
    matcher = ElementMatcher(
        before, after, allowed=lambda p, j, i: j != 0, on_match=lambda j, i: seen.append((j, i))
    )
    assert matcher.run([MatchPass("handle", kind="handle")]) == {0: 1}
    matcher.pair(2, 2)
    assert seen == [(1, 0), (2, 2)]
    assert matcher.run([MatchPass("name", key=by_name)]) == {0: 1, 2: 2}  # nothing left to match


def test_matcher_restricts_to_subsets_and_needs_key_functions() -> None:
    before = [el("A"), el("B")]
    after = [el("A"), el("B")]
    assert ElementMatcher(before, after).run([MatchPass("name", key=by_name)], before_indices=[1]) == {1: 1}
    for kind in ("key", "overlap"):
        with pytest.raises(ValueError, match="key function"):
            ElementMatcher(before, after).run([MatchPass("broken", kind=kind)])  # type: ignore[arg-type]


def test_iou_and_rounded_frames() -> None:
    assert iou(Rect(0, 0, 10, 10), Rect(0, 0, 10, 10)) == 1.0
    assert iou(Rect(0, 0, 10, 10), Rect(5, 0, 10, 10)) == pytest.approx(50 / 150)
    assert iou(Rect(0, 0, 10, 10), Rect(20, 0, 10, 10)) == 0.0
    assert iou(Rect(0, 0, 0, 0), Rect(0, 0, 0, 0)) == 0.0
    assert rounded_frame(el("A", frame=(0.4, 1.6, 9.5, 10.49))) == (0, 2, 10, 10)


# =========================================================================== carry_refs
def test_first_snapshot_numbers_in_pre_order_from_the_counter(counter: RefCounter) -> None:
    snap = take(counter, tree(button("A", 0, h="a"), button("B", 70, h="b", focused=True)))
    assert [e.ref for e in snap.all_elements] == ["e1", "e2"]
    assert snap.focused_ref == "e2"
    assert snap.ref_session == counter.session
    again = take(counter, tree(button("C", 0, h="c")))  # another snapshot, nothing to carry
    assert [e.ref for e in again.all_elements] == ["e3"]


def test_carry_by_handle_even_when_name_and_place_change(counter: RefCounter) -> None:
    first = take(counter, tree(button("Play", 0, h="play"), button("Stop", 70, h="stop")))
    second = take(counter, tree(button("Stop", 70, h="stop"), button("Pause", 300, h="play")), first)
    assert refs(second) == {"Stop": refs(first)["Stop"], "Pause": refs(first)["Play"]}


def test_carry_by_unique_identifier(counter: RefCounter) -> None:
    first = take(counter, tree(button("Save", 0, h="h1", identifier="save"), button("X", 70, h="h2")))
    second = take(
        counter, tree(button("Y", 70, h="h9"), button("Save now", 300, h="h8", identifier="save")), first
    )
    assert refs(second)["Save now"] == refs(first)["Save"]
    assert refs(second)["Y"] not in by_handle(first).values()


def test_duplicate_identifiers_are_not_evidence(counter: RefCounter) -> None:
    first = take(
        counter,
        tree(button("A", 0, h="h1", identifier="cell"), button("B", 70, h="h2", identifier="cell")),
    )
    second = take(
        counter,
        tree(button("C", 400, h="h3", identifier="cell"), button("D", 500, h="h4", identifier="cell")),
        first,
    )
    assert min(numbers(second)) > max(numbers(first))


def test_carry_by_path_and_name(counter: RefCounter) -> None:
    first = take(counter, tree(button("OK", 0, h="old")))
    second = take(counter, tree(button("OK", 200, h="new")), first)  # re-created and moved
    assert refs(second) == refs(first)


def test_carry_by_role_name_and_frame(counter: RefCounter) -> None:
    first = take(counter, tree(n("AXGroup", button("OK", 0, h="old"), h="g", title="Bar")))
    # same place and name, but now outside the group (another path) and a new handle
    second = take(counter, tree(n("AXGroup", h="g", title="Bar"), button("OK", 0, h="new")), first)
    assert refs(second)["OK"] == refs(first)["OK"]


def test_carry_by_unique_role_and_name(counter: RefCounter) -> None:
    first = take(counter, tree(button("Share", 0, h="old"), button("Help", 70, h="help")))
    second = take(counter, tree(n("AXToolbar", button("Share", 400, h="new"), h="tb", title="T")), first)
    assert refs(second)["Share"] == refs(first)["Share"]


def test_ambiguous_names_get_fresh_refs(counter: RefCounter) -> None:
    first = take(counter, tree(button("OK", 0, h="a"), button("OK", 70, h="b")))
    second = take(
        counter,
        tree(n("AXGroup", button("OK", 300, h="c"), button("OK", 400, h="d"), h="g", title="G")),
        first,
    )
    assert all(e.ref not in by_handle(first).values() for e in second.all_elements if e.name == "OK")


def test_unnamed_elements_carry_by_position_under_the_same_parent(counter: RefCounter) -> None:
    field = n("AXTextField", frame=(10, 50, 200, 24), h="f1")
    first = take(counter, tree(field))
    same_place = take(counter, tree(n("AXTextField", frame=(10, 52, 200, 24), h="f2")), first)
    assert by_handle(same_place)["f2"] == by_handle(first)["f1"]
    # another path (a same-role sibling now comes first) but nearly the same frame: overlap pass
    shifted = take(
        counter,
        tree(
            n("AXTextField", frame=(10, 300, 200, 24), h="f3"),
            n("AXTextField", frame=(12, 50, 200, 24), h="f4"),
        ),
        same_place,
    )
    assert by_handle(shifted)["f4"] == by_handle(first)["f1"]
    assert by_handle(shifted)["f3"] not in by_handle(first).values()
    far = take(counter, tree(n("AXTextField", frame=(500, 500, 200, 24), h="f5")), first)
    assert by_handle(far)["f5"] != by_handle(first)["f1"]


def test_unnamed_elements_without_frames_never_match_by_position(counter: RefCounter) -> None:
    first = take(counter, tree(n("AXTextField", h="f1", actions=("AXConfirm",))))
    second = take(counter, tree(n("AXTextField", h="f2", actions=("AXConfirm",))), first)
    assert numbers(second)[0] > numbers(first)[0]


def test_named_element_at_the_same_place_with_other_content_gets_a_new_ref(counter: RefCounter) -> None:
    first = take(counter, tree(button("Delete", 0, h="b1"), row("Shopping list", 100, h="r1")))
    # a virtualized list reused the slots for other content: other handles, same paths and frames
    second = take(counter, tree(button("Archive", 0, h="b2"), row("Trip ideas", 100, h="r2")), first)
    assert set(refs(second).values()).isdisjoint(refs(first).values())


def test_recycled_row_with_the_same_handle_but_new_content_gets_new_refs(counter: RefCounter) -> None:
    def view(first_text: str) -> RawTree:
        reply = button("Reply", 250, y=102, h="reply1")
        recycled = row(first_text, 100, h="row1", cell="cell1", text_h="text1")
        recycled.children[0].children.append(reply)
        return tree(
            n(
                "AXList",
                recycled,
                row("Stays", 130, h="row2", cell="cell2"),
                h="list",
                title="Mail",
                frame=(0, 90, 400, 300),
            )
        )

    first = take(counter, view("Item 5"), mode="full")
    second = take(counter, view("Item 25"), first, mode="full")
    old, new = by_handle(first), by_handle(second)
    assert new["list"] == old["list"]
    assert new["row2"] == old["row2"]  # unchanged row keeps its ref …
    assert new["cell2"] == old["cell2"]  # … and so does what it contains
    for handle in ("row1", "cell1", "text1", "reply1"):  # everything in the recycled row is new
        assert new[handle] != old[handle], handle
        assert new[handle] not in old.values()


def test_a_recycled_row_whose_name_field_changed_gets_new_refs(counter: RefCounter) -> None:
    """Finder (macOS 26) reuses row elements while scrolling: the same handle, the same date and
    size, another file name in the Name column's text field."""

    def view(file_name: str) -> RawTree:
        name = n("AXTextField", frame=(4, 102, 200, 16), h="name1", value=file_name)
        date = n("AXStaticText", frame=(210, 102, 80, 16), h="date1", value="Today at 2:57 PM")
        recycled = n(
            "AXRow",
            n("AXCell", name, frame=(0, 100, 205, 20), h="cell1"),
            n("AXCell", date, frame=(205, 100, 95, 20), h="cell2"),
            frame=(0, 100, 300, 20),
            h="row1",
        )
        return tree(n("AXOutline", recycled, h="list", title="list view", frame=(0, 90, 400, 300)))

    first = take(counter, view("file-008.txt"), mode="full")
    second = take(counter, view("file-019.txt"), first, mode="full")
    old, new = by_handle(first), by_handle(second)
    assert new["list"] == old["list"]
    for handle in ("row1", "cell1", "name1", "cell2", "date1"):
        assert new[handle] not in old.values(), handle


def test_numbers_are_unique_across_apps_and_windows_and_never_reused(counter: RefCounter) -> None:
    same_ui = (button("OK", 0, h="ok"), button("Cancel", 70, h="cancel"))
    notes = take(counter, tree(*same_ui, pid=1, window_id=10))
    mail = take(counter, tree(*same_ui, pid=2, window_id=20), notes)  # same handles, another app
    other_window = take(counter, tree(*same_ui, pid=1, window_id=11), notes)
    no_window = take(counter, tree(*same_ui, pid=1, window_id=None), notes)
    all_refs = [set(numbers(s)) for s in (notes, mail, other_window, no_window)]
    assert sum(len(r) for r in all_refs) == len(set().union(*all_refs))
    # back to the first window: carried from its own snapshot, never from another app's
    again = take(counter, tree(*same_ui, pid=1, window_id=10), mail, history=[other_window, notes])
    assert refs(again) == refs(notes)


def test_retired_numbers_are_never_reused(counter: RefCounter) -> None:
    first = take(counter, tree(button("A", 0, h="a"), button("B", 70, h="b")))
    gone = take(counter, tree(button("A", 0, h="a")), first)
    issued = counter.issued()
    back = take(counter, tree(button("A", 0, h="a"), button("B", 70, h="b2")), gone, history=[first])
    assert refs(back)["A"] == refs(first)["A"]
    assert refs(back)["B"] == f"e{issued + 1}"  # a new element (new handle) after B was gone


def test_history_gives_back_refs_by_handle(counter: RefCounter) -> None:
    first = take(counter, tree(button("A", 0, h="a"), button("B", 70, h="b")))
    text_only = take(counter, tree(button("A", 0, h="a"), button("B", 70, h="b")), first, mode="text")
    assert text_only.all_elements == []
    back = take(counter, tree(button("A", 0, h="a"), button("B", 70, h="b")), text_only, history=[first])
    assert refs(back) == refs(first)
    # history matches by handle only: a look-alike with another handle is a new element
    lookalike = take(counter, tree(button("A", 0, h="a2")), text_only, history=[first])
    assert refs(lookalike)["A"] != refs(first)["A"]


def test_previous_of_another_view_or_session_is_ignored(counter: RefCounter) -> None:
    first = take(counter, tree(button("A", 0, h="a"), pid=1))
    other_app = take(counter, tree(button("A", 0, h="a"), pid=2))
    carried = take(counter, tree(button("A", 0, h="a"), pid=1), other_app, history=[first])
    assert refs(carried) == refs(first)  # the history snapshot of this window took over
    plain = build_snapshot(tree(button("A", 0, h="a"), pid=1), snapshot_id="s_plain")
    fresh = take(counter, tree(button("A", 0, h="a"), pid=1), plain)
    assert refs(fresh)["A"] not in ("e1", refs(first)["A"])
    mine = RefCounter()
    mine.reserve(["e5"])
    foreign = take(RefCounter(), tree(button("A", 0, h="a"), pid=1))
    assert refs(foreign)["A"] == "e1"
    assert refs(take(mine, tree(button("A", 0, h="a"), pid=1), foreign))["A"] == "e6"


def test_parent_focus_and_page_are_remapped(counter: RefCounter) -> None:
    def view(extra: bool) -> RawTree:
        items = [button("New", 0, h="new")] if extra else []
        return tree(
            *items,
            n(
                "AXToolbar",
                button("Bold", 100, h="bold"),
                button("Italic", 170, h="italic", focused=True),
                h="tb",
                title="Format",
            ),
            button("Save", 300, h="save"),
        )

    first = take(counter, view(False), max_elements=2, offset=1)
    second = take(counter, view(True), first, max_elements=2, offset=1)
    for snap in (first, second):
        by_ref = {e.ref: e for e in snap.all_elements}
        assert snap.elements == snap.all_elements[1:3]
        assert all(e is by_ref[e.ref] for e in snap.elements)
        assert snap.focused_ref == refs(snap)["Italic"]
        assert by_ref[refs(snap)["Bold"]].parent_ref == refs(snap)["Format"]
        assert all(snap.element(e.ref) is e for e in snap.all_elements)
    assert refs(second)["Italic"] == refs(first)["Italic"]
    assert refs(second)["New"] not in refs(first).values()


def test_carry_does_not_modify_its_input(counter: RefCounter) -> None:
    raw = build_snapshot(tree(button("A", 0, h="a"), button("B", 70, h="b")), snapshot_id="s_raw")
    take(counter, tree(button("Z", 0, h="z")))
    carried = carry_refs(raw, None, counter)
    assert [e.ref for e in raw.all_elements] == ["e1", "e2"]
    assert raw.ref_session is None
    assert [e.ref for e in carried.all_elements] == ["e2", "e3"]
    assert carried.all_elements[0] is not raw.all_elements[0]


def test_ocr_elements_get_fresh_o_refs_and_are_never_carried(counter: RefCounter) -> None:
    t = tree(button("A", 0, h="a"))
    lines = [OcrLine(text="Brush size", confidence=0.9, frame=Rect(100, 200, 80, 14))]

    def vision(previous: Snapshot | None) -> Snapshot:
        raw = build_snapshot(t, snapshot_id=f"s_{next(_ids):04d}")
        return carry_refs(merge_vision(raw, ocr_elements(lines)), previous, counter)

    first = vision(None)
    second = vision(first)
    assert [e.ref for e in first.all_elements] == ["e1", "o1"]
    assert [e.ref for e in second.all_elements] == ["e1", "o2"]


def test_unhashable_handles_still_carry(counter: RefCounter) -> None:
    first = take(counter, tree(button("Old", 0, h=Unhashable("x")), button("Other", 70, h=Unhashable("y"))))
    second = take(counter, tree(button("Renamed", 300, h=Unhashable("x"))), first)
    assert refs(second)["Renamed"] == refs(first)["Old"]


def test_handles_that_cannot_be_compared_are_treated_as_different(counter: RefCounter) -> None:
    first = take(counter, tree(button("A", 0, h=Exploding())))
    second = take(counter, tree(button("B", 300, h=Exploding())), first)
    assert refs(second)["B"] != refs(first)["A"]


def test_view_of() -> None:
    snap = build_snapshot(tree(button("A", 0, h="a"), pid=5, window_id=9), snapshot_id="s_v")
    assert view_of(snap) == (5, 9)
    assert view_of(dataclasses.replace(snap, window=None)) == (5, None)
