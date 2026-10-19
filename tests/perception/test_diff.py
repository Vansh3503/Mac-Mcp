from __future__ import annotations

import json
from collections.abc import Callable
from itertools import count

import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import Snapshot, build_snapshot, diff_payload, diff_snapshots, format_diff
from axpilot.perception.diff import element_changes
from axpilot.perception.serialize import assign_handles

_ids = count(1)


def n(
    role: str, *children: RawNode, frame: tuple[float, float, float, float] | None = None, **kw: object
) -> RawNode:
    return RawNode(role=role, frame=Rect(*frame) if frame else None, children=list(children), **kw)  # type: ignore[arg-type]


def snapshot(
    *children: RawNode, window_id: int = 7, title: str = "Doc", pid: int = 100, app: str = "Demo"
) -> Snapshot:
    root = n("AXWindow", *children, frame=(0, 0, 800, 600), title=title)
    assign_handles(root)
    tree = RawTree(
        app=AppInfo(app, pid, f"com.example.{app.lower()}"),
        window=WindowInfo(window_id, pid, app, title, Rect(0, 0, 800, 600)),
        root=root,
    )
    return build_snapshot(tree, snapshot_id=f"s_{next(_ids):04d}")


def row(title: str, y: float, **kw: object) -> RawNode:
    return n("AXRow", n("AXCell", n("AXStaticText", value=title)), frame=(0, y, 300, 20), **kw)


def field(value: str, *, focused: bool = False, name: str | None = None) -> RawNode:
    return n("AXTextField", value=value, focused=focused, description=name, frame=(400, 10, 200, 24))


def test_identical_snapshots_have_an_empty_diff() -> None:
    before = snapshot(n("AXButton", title="OK", frame=(0, 0, 50, 20)), field("a"))
    after = snapshot(n("AXButton", title="OK", frame=(0, 0, 50, 20)), field("a"))
    diff = diff_snapshots(before, after)
    assert diff.is_empty()
    assert format_diff(diff) == f"No changes since {before.snapshot_id} (now {after.snapshot_id})."
    assert diff_payload(diff)["empty"] is True


def test_value_change_is_reported_with_before_and_after() -> None:
    before, after = snapshot(field("a")), snapshot(field("ab"))
    diff = diff_snapshots(before, after)
    assert [(b.value, a.value) for b, a in diff.changed] == [("a", "ab")]
    assert element_changes(*diff.changed[0]) == {"value": ("a", "ab")}
    assert format_diff(diff).splitlines()[1] == '~ e1 field value: "a" → "ab"'


def test_added_and_removed_elements() -> None:
    before = snapshot(n("AXButton", title="Old", frame=(0, 0, 50, 20)), field("x", name="Name"))
    after = snapshot(field("x", name="Name"), n("AXButton", title="Save", frame=(0, 40, 50, 20)))
    diff = diff_snapshots(before, after)
    assert [el.name for el in diff.added] == ["Save"]
    assert [el.name for el in diff.removed] == ["Old"]
    assert diff.changed == []
    assert format_diff(diff).splitlines() == [
        f"Changes since {before.snapshot_id} (now {after.snapshot_id}): 1 added, 1 removed, 0 changed",
        '+ e2 button "Save"  (25,50)',
        '- button "Old"',
    ]


def test_focus_change_gets_its_own_line_and_hides_focus_only_changes() -> None:
    before = snapshot(
        field("", name="Search"),
        n("AXTextArea", description="Body", value="hi", focused=True, frame=(0, 50, 400, 400)),
    )
    after = snapshot(
        field("", name="Search", focused=True),
        n("AXTextArea", description="Body", value="hi", frame=(0, 50, 400, 400)),
    )
    diff = diff_snapshots(before, after)
    assert (diff.focus_before, diff.focus_after, diff.focus_after_ref) == ("Body", "Search", "e1")
    assert len(diff.changed) == 2  # both elements changed their focused state
    lines = format_diff(diff).splitlines()
    assert lines[1] == 'focus: "Body" → e1 "Search"'
    assert len(lines) == 2
    assert lines[0].endswith("0 added, 0 removed, 0 changed")


def test_identifier_matches_an_element_that_moved_in_the_tree() -> None:
    before = snapshot(n("AXButton", title="Go", identifier="go", frame=(0, 0, 50, 20)))
    moved = n("AXGroup", n("AXButton", title="Go!", identifier="go", frame=(100, 100, 50, 20)))
    diff = diff_snapshots(before, snapshot(moved))
    assert diff.added == []
    assert diff.removed == []
    assert element_changes(*diff.changed[0]) == {"name": ("Go", "Go!")}


def test_row_inserted_at_the_top_does_not_shift_every_match() -> None:
    before = snapshot(
        n(
            "AXList",
            row("Alpha", 0, selected=True),
            row("Beta", 20),
            description="Items",
            frame=(0, 0, 300, 200),
        )
    )
    after = snapshot(
        n(
            "AXList",
            row("New", 0, selected=True),
            row("Alpha", 20),
            row("Beta", 40),
            description="Items",
            frame=(0, 0, 300, 200),
        )
    )
    diff = diff_snapshots(before, after)
    assert [el.name for el in diff.added] == ["New"]
    assert diff.removed == []
    changed = {a.name: element_changes(b, a) for b, a in diff.changed}
    assert changed == {"Alpha": {"selected": (True, False)}}


def test_static_text_whose_content_changed_is_matched_by_path() -> None:
    def sheet(text: str) -> RawNode:
        return n("AXSheet", n("AXStaticText", value=text, frame=(10, 10, 200, 20)), frame=(0, 0, 400, 300))

    diff = diff_snapshots(snapshot(sheet("3 items")), snapshot(sheet("4 items")))
    assert diff.added == []
    assert diff.removed == []
    (pair,) = diff.changed
    assert element_changes(*pair) == {"name": ("3 items", "4 items")}
    assert format_diff(diff).splitlines()[1] == '~ e2 text "4 items" name: "3 items" → "4 items"'


def test_state_changes_are_described_in_words() -> None:
    before = snapshot(
        n("AXCheckBox", title="Wrap", value=0, frame=(0, 0, 20, 20), actions=("AXPress",)),
        n("AXButton", title="Save", enabled=False, frame=(0, 30, 50, 20)),
        n("AXDisclosureTriangle", title="More", expanded=False, frame=(0, 60, 20, 20)),
    )
    after = snapshot(
        n("AXCheckBox", title="Wrap", value=1, frame=(0, 0, 20, 20), actions=("AXPress",)),
        n("AXButton", title="Save", frame=(0, 30, 50, 20)),
        n("AXDisclosureTriangle", title="More", expanded=True, frame=(0, 60, 20, 20)),
    )
    assert format_diff(diff_snapshots(before, after)).splitlines()[1:] == [
        '~ e1 checkbox "Wrap" checked',
        '~ e2 button "Save" enabled',
        '~ e3 disclosure "More" expanded',
    ]


def test_window_change_matches_nothing_and_summarises_the_old_window() -> None:
    before = snapshot(n("AXButton", title="OK", frame=(0, 0, 50, 20)), field("a"), title="One", window_id=1)
    after = snapshot(n("AXButton", title="OK", frame=(0, 0, 50, 20)), title="Two", window_id=2)
    diff = diff_snapshots(before, after)
    assert diff.window_changed
    assert not diff.app_changed
    assert [el.name for el in diff.added] == ["OK"]
    assert len(diff.removed) == 2
    lines = format_diff(diff).splitlines()
    assert lines[1] == 'window: "One" → "Two"'
    assert lines[-1] == "- 2 elements of the previous window"


def test_app_change_is_reported() -> None:
    before = snapshot(n("AXButton", title="A", frame=(0, 0, 50, 20)), app="Notes", pid=1)
    after = snapshot(n("AXButton", title="A", frame=(0, 0, 50, 20)), app="Finder", pid=2)
    diff = diff_snapshots(before, after)
    assert diff.app_changed
    assert diff.window_changed is False
    assert format_diff(diff).splitlines()[1] == "app: Notes → Finder"


def test_format_diff_caps_the_number_of_lines() -> None:
    before = snapshot()
    after = snapshot(*[n("AXButton", title=f"B{i}", frame=(0, i * 10, 40, 10)) for i in range(40)])
    lines = format_diff(diff_snapshots(before, after), max_items=5).splitlines()
    assert len(lines) == 1 + 5 + 1
    assert lines[-1] == "… 35 more changes (call snapshot for the full view)."


def test_diff_payload_is_json_safe_and_capped() -> None:
    before = snapshot(
        field("a"), *[n("AXButton", title=f"Old{i}", frame=(0, 100 + i * 10, 40, 10)) for i in range(3)]
    )
    after = snapshot(
        field("ab"), *[n("AXButton", title=f"New{i}", frame=(200, 100 + i * 10, 40, 10)) for i in range(5)]
    )
    payload = diff_payload(diff_snapshots(before, after), max_items=2)
    assert payload["counts"] == {"added": 5, "removed": 3, "changed": 1}
    assert payload["truncated"] is True
    assert len(payload["added"]) == 2
    assert len(payload["removed"]) == 2
    assert payload["changed"] == [
        {"ref": "e1", "role": "field", "name": None, "changes": {"value": ["a", "ab"]}}
    ]
    assert payload["added"][0] == {
        "ref": "e2",
        "role": "button",
        "name": "New0",
        "value": None,
        "center": [220.0, 105.0],
        "states": [],
    }
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("app_changed", True), ("window_changed", True), ("focus_after", "x"), ("added", ["el"])],
)
def test_is_empty_considers_every_kind_of_change(field_name: str, value: object) -> None:
    diff = diff_snapshots(snapshot(), snapshot())
    assert diff.is_empty()
    setattr(diff, field_name, value)
    assert not diff.is_empty()


def test_diff_after_pressing_new_note_in_the_demo(
    engine: FakeEngine, snapshot_of: Callable[..., Snapshot]
) -> None:
    engine.app_control("activate", "Notes")  # typing goes to the frontmost app
    before = snapshot_of("Notes")
    new_note = next(el for el in before.all_elements if el.name == "New Note")
    engine.perform_action(new_note.handle, "AXPress")
    engine.type_text("Groceries")
    diff = diff_snapshots(before, snapshot_of("Notes"))
    assert [el.name for el in diff.added] == ["New Note — 10:45"]
    changed = {after.name: element_changes(b, after) for b, after in diff.changed}
    assert changed["Shopping list — 10:42"] == {"selected": (True, False)}
    assert changed["Note body"]["value"][1] == "Groceries"
    assert set(changed) == {"Shopping list — 10:42", "Note body"}


def test_selection_and_focus_words_when_focus_moves_between_look_alikes() -> None:
    def fields(focus_first: bool) -> list[RawNode]:
        return [
            n("AXTextField", description="Code", focused=focus_first, frame=(0, 0, 50, 20)),
            n("AXTextField", description="Code", focused=not focus_first, frame=(0, 30, 50, 20)),
            n("AXRow", title="R", selected=focus_first, frame=(0, 60, 50, 20)),
        ]

    diff = diff_snapshots(snapshot(*fields(True)), snapshot(*fields(False)))
    assert diff.focus_before == diff.focus_after == "Code"
    assert format_diff(diff).splitlines()[1:] == [
        '~ e1 field "Code" unfocused',
        '~ e2 field "Code" focused',
        '~ e3 row "R" unselected',
    ]


def test_elements_without_frames_still_match() -> None:
    before = snapshot(
        n("AXButton", title="Floating", actions=("AXPress",)), n("AXButton", title="A", frame=(0, 0, 9, 9))
    )
    after = snapshot(
        n("AXButton", title="Sinking", actions=("AXPress",)), n("AXButton", title="A", frame=(0, 0, 9, 9))
    )
    diff = diff_snapshots(before, after)
    assert diff.added == []
    assert [element_changes(b, a) for b, a in diff.changed] == [{"name": ("Floating", "Sinking")}]
