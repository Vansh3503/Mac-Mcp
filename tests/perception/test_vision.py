"""Vision fallback: OCR text as pseudo-elements, merged into snapshots."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import OcrLine, Rect
from axpilot.perception import build_snapshot
from axpilot.perception.model import Snapshot
from axpilot.perception.vision import (
    group_ocr_lines,
    is_vision_element,
    merge_vision,
    normalize_text,
    ocr_elements,
    ocr_ref_index,
)


def line(text: str, x: float, y: float, w: float = 40.0, h: float = 14.0, *, conf: float = 0.9) -> OcrLine:
    return OcrLine(text=text, confidence=conf, frame=Rect(x, y, w, h))


# --------------------------------------------------------------------------- small helpers
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Save  As… ", "save as"),  # ellipses unified, then stripped at the edges
        ("Name:", "name"),
        ("Done.", "done"),
        (None, ""),
        ("", ""),
        ("ÉLAN  Café", "élan café"),
    ],
)
def test_normalize_text(raw: str | None, expected: str) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    ("ref", "expected"), [("o1", 1), ("o42", 42), ("o0", None), ("o01", None), ("o", None), ("e1", None)]
)
def test_ocr_ref_index(ref: str, expected: int | None) -> None:
    assert ocr_ref_index(ref) == expected


# --------------------------------------------------------------------------- grouping
def test_low_confidence_empty_and_zero_size_boxes_are_dropped() -> None:
    lines = [
        line("kept", 10, 10),
        line("faint", 10, 40, conf=0.1),
        line("   ", 10, 70),
        OcrLine(text="flat", confidence=0.9, frame=Rect(10, 100, 0, 10)),
    ]
    assert [g.text for g in group_ocr_lines(lines)] == ["kept"]


def test_words_on_one_baseline_merge_but_columns_stay_apart() -> None:
    lines = [
        line("File", 10, 10, w=30),
        line("name", 44, 10, w=36),  # gap 4 pt < 1 line height → same phrase
        line("Size", 400, 10, w=30),  # far right → its own column
    ]
    assert [g.text for g in group_ocr_lines(lines)] == ["File name", "Size"]


def test_reading_order_is_top_to_bottom_then_left_to_right() -> None:
    lines = [line("third", 10, 80), line("second", 300, 10), line("first", 10, 10)]
    assert [g.text for g in group_ocr_lines(lines)] == ["first", "second", "third"]


@given(st.permutations(list(range(6))))
def test_grouping_does_not_depend_on_input_order(order: list[int]) -> None:
    base = [
        line("Alpha", 10, 10),
        line("Beta", 300, 10),
        line("Gamma", 10, 60),
        line("Delta", 300, 60),
        line("Eps", 10, 110),
        line("Zeta", 300, 110),
    ]
    shuffled = [base[i] for i in order]
    assert [g.text for g in group_ocr_lines(shuffled)] == [g.text for g in group_ocr_lines(base)]


# --------------------------------------------------------------------------- elements
def test_ocr_elements_are_clickable_pseudo_elements() -> None:
    elements = ocr_elements([line("Brush", 10, 10), line("Eraser", 10, 60)], pid=7, window_id=3)
    assert [el.ref for el in elements] == ["o1", "o2"]
    first = elements[0]
    assert first.role == "ocrtext"
    assert first.interactive is True
    assert first.handle is None
    assert first.locator.pid == 7
    assert first.locator.window_id == 3
    assert is_vision_element(first)
    assert first.extra["source"] == "ocr"


def test_start_index_and_excluded_names() -> None:
    elements = ocr_elements(
        [line("Save", 10, 10), line("Cancel", 10, 60)], start_index=5, exclude_names=["save"]
    )
    assert [(el.ref, el.name) for el in elements] == [("o5", "Cancel")]


@pytest.mark.parametrize("kwargs", [{"start_index": 0}, {"min_confidence": 1.5}, {"min_confidence": -0.1}])
def test_invalid_arguments(kwargs: dict[str, float]) -> None:
    with pytest.raises(AXPilotError) as caught:
        ocr_elements([line("x", 0, 0)], **kwargs)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


# --------------------------------------------------------------------------- merging
@pytest.fixture
def login() -> Snapshot:
    engine = FakeEngine.demo()
    return build_snapshot(engine.capture_tree(app="Login Demo"), snapshot_id="s_login")


@pytest.fixture
def notes() -> Snapshot:
    engine = FakeEngine.demo()
    return build_snapshot(engine.capture_tree(app="Notes"), snapshot_id="s_notes")


def test_ocr_elements_follow_the_ax_elements(notes: Snapshot) -> None:
    frame = notes.window.frame if notes.window else Rect(0, 0, 100, 100)
    extra = ocr_elements([line("Unlabelled canvas text", frame.x + 600, frame.y + 600, w=160)])
    merged = merge_vision(notes, extra)
    refs = [el.ref for el in merged.all_elements]
    assert refs[: len(notes.all_elements)] == [el.ref for el in notes.all_elements]
    assert refs[-1] == "o1"


def test_text_the_ax_tree_already_shows_is_not_repeated(notes: Snapshot) -> None:
    button = next(el for el in notes.all_elements if el.name == "New Note")
    assert button.frame is not None
    copy = line("New Note", button.frame.x, button.frame.y, w=button.frame.w, h=button.frame.h)
    merged = merge_vision(notes, ocr_elements([copy]))
    assert not any(is_vision_element(el) for el in merged.all_elements)


def test_text_inside_a_secure_field_is_never_surfaced(login: Snapshot) -> None:
    secure = next(el for el in login.all_elements if el.secure)
    assert secure.frame is not None
    leaked = line("hunter2", secure.frame.x + 2, secure.frame.y + 2, w=50, h=secure.frame.h - 4)
    merged = merge_vision(login, ocr_elements([leaked]))
    assert all("hunter2" not in (el.name or "") for el in merged.all_elements)


def test_text_outside_the_window_is_dropped(notes: Snapshot) -> None:
    assert notes.window is not None
    outside = line("Another app", notes.window.frame.x + notes.window.frame.w + 50, 10)
    merged = merge_vision(notes, ocr_elements([outside]))
    assert not any(is_vision_element(el) for el in merged.all_elements)


def test_merging_twice_adds_nothing_new_and_numbering_continues(notes: Snapshot) -> None:
    assert notes.window is not None
    frame = notes.window.frame
    first = merge_vision(notes, ocr_elements([line("Canvas one", frame.x + 600, frame.y + 600, w=120)]))
    again = merge_vision(first, ocr_elements([line("Canvas one", frame.x + 600, frame.y + 600, w=120)]))
    assert [el.ref for el in again.all_elements] == [el.ref for el in first.all_elements]
    more = merge_vision(first, ocr_elements([line("Canvas two", frame.x + 600, frame.y + 700, w=120)]))
    assert [el.ref for el in more.all_elements if is_vision_element(el)] == ["o1", "o2"]


def test_allocate_gives_session_unique_o_refs(notes: Snapshot) -> None:
    assert notes.window is not None
    frame = notes.window.frame
    issued = iter(["o41", "o42", "o43"])
    first = merge_vision(
        notes,
        ocr_elements([line("Canvas one", frame.x + 600, frame.y + 600, w=120)]),
        allocate=lambda: next(issued),
    )
    more = merge_vision(
        first,
        ocr_elements([line("Canvas two", frame.x + 600, frame.y + 700, w=120)]),
        allocate=lambda: next(issued),
    )
    assert [el.ref for el in more.all_elements if is_vision_element(el)] == ["o41", "o42"]
    added = more.element("o42")
    assert added is not None
    assert added.name == "Canvas two"


def test_paging_with_max_elements(notes: Snapshot) -> None:
    assert notes.window is not None
    frame = notes.window.frame
    texts = [line(f"Item {i}", frame.x + 600, frame.y + 300 + 30 * i, w=80) for i in range(5)]
    merged = merge_vision(notes, ocr_elements(texts), max_elements=10)
    assert len(merged.elements) == 10
    assert len(merged.all_elements) == len(notes.all_elements) + 5
    with pytest.raises(AXPilotError):
        merge_vision(notes, [], max_elements=0)
