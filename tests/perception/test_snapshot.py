from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawNode, RawTree
from axpilot.perception import Snapshot, build_snapshot
from axpilot.perception.snapshot import NAME_MAX_CHARS, node_name, stringify_value, truncate

NodeFactory = Callable[..., RawNode]
TreeFactory = Callable[..., RawTree]


def build(tree: RawTree, **kwargs: object) -> Snapshot:
    return build_snapshot(tree, snapshot_id="s_test", **kwargs)  # type: ignore[arg-type]


def names(snap: Snapshot) -> list[str | None]:
    return [el.name for el in snap.all_elements]


# --------------------------------------------------------------------------- refs
def test_refs_are_dense_and_assigned_in_pre_order(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXButton", title="One", frame=(10, 10, 50, 20)),
        node(
            "AXGroup",
            node("AXButton", title="Two", frame=(10, 40, 50, 20)),
            node("AXButton", title="Three", frame=(10, 70, 50, 20)),
        ),
        node("AXButton", title="Four", frame=(10, 100, 50, 20)),
    )
    snap = build(tree)
    assert [el.ref for el in snap.all_elements] == ["e1", "e2", "e3", "e4"]
    assert names(snap) == ["One", "Two", "Three", "Four"]


def test_same_tree_yields_same_refs(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(*[node("AXButton", title=f"B{i}", frame=(0, i * 20, 40, 20)) for i in range(10)])
    first, second = build(tree), build(tree)
    assert [(e.ref, e.name) for e in first.all_elements] == [(e.ref, e.name) for e in second.all_elements]


def test_element_lookup_by_ref(node: NodeFactory, make_tree: TreeFactory) -> None:
    snap = build(make_tree(*[node("AXButton", title=f"B{i}", frame=(0, i * 20, 40, 20)) for i in range(3)]))
    el = snap.element("e2")
    assert el is not None
    assert el.name == "B1"
    assert snap.element("e9") is None
    assert snap.element("x") is None
    assert snap.ref_session is None  # local refs; the server renumbers them (perception.refs)


def test_element_lookup_follows_changes_to_the_element_list(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    snap = build(make_tree(*[node("AXButton", title=f"B{i}", frame=(0, i * 20, 40, 20)) for i in range(3)]))
    assert snap.element("e3") is not None  # builds the index
    renamed = [dataclasses.replace(el, ref=f"e{10 + i}") for i, el in enumerate(snap.all_elements)]
    snap.all_elements = renamed  # a new list: the index is rebuilt
    assert snap.element("e3") is None
    assert snap.element("e11") is renamed[1]
    extra = dataclasses.replace(renamed[0], ref="e99")
    snap.all_elements.append(extra)  # the same list, longer: rebuilt too
    assert snap.element("e99") is extra
    copy = dataclasses.replace(snap, elements=snap.all_elements[:1])
    assert copy.element("e99") is extra


# --------------------------------------------------------------------------- pruning
def test_invisible_nodes_are_skipped_with_their_subtree(node: NodeFactory, make_tree: TreeFactory) -> None:
    hidden = node("AXGroup", node("AXButton", title="Inside", frame=(0, 0, 10, 10)), visible=False)
    snap = build(make_tree(hidden, node("AXButton", title="Shown", frame=(0, 20, 10, 10))))
    assert names(snap) == ["Shown"]


def test_zero_size_nodes_are_skipped_but_their_children_are_examined(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    wrapper = node("AXGroup", node("AXLink", title="Visible link", frame=(5, 5, 60, 18)), frame=(0, 0, 0, 0))
    zero_button = node("AXButton", title="Collapsed", frame=(10, 10, 0, 20))
    snap = build(make_tree(wrapper, zero_button))
    assert names(snap) == ["Visible link"]


def test_nodes_outside_the_window_are_skipped(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXButton", title="Inside", frame=(10, 10, 50, 20)),
        node("AXButton", title="Partly", frame=(780, 590, 50, 20)),
        node("AXButton", title="Below", frame=(10, 700, 50, 20)),
        node("AXButton", title="Left", frame=(-200, 10, 50, 20)),
    )
    assert names(build(tree)) == ["Inside", "Partly"]


def test_open_menus_are_exempt_from_the_window_bounds(node: NodeFactory, make_tree: TreeFactory) -> None:
    popup = node(
        "AXPopUpButton",
        node(
            "AXMenu",
            node("AXMenuItem", title="Far option", frame=(10, 900, 120, 20)),
            frame=(10, 600, 120, 400),
        ),
        title="Choice",
        frame=(10, 580, 120, 20),
    )
    assert names(build(make_tree(popup))) == ["Choice", "Far option"]


def test_unnamed_groups_are_collapsed_and_children_promoted(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    tree = make_tree(node("AXGroup", node("AXGroup", node("AXButton", title="Deep", frame=(0, 0, 10, 10)))))
    (el,) = build(tree).all_elements
    assert el.depth == 0
    assert el.parent_ref is None


def test_named_groups_are_kept_as_context(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node(
            "AXGroup",
            node("AXButton", title="Bold", frame=(0, 0, 10, 10)),
            description="Formatting",
            frame=(0, 0, 100, 20),
        )
    )
    group, button = build(tree).all_elements
    assert (group.role, group.name, group.interactive) == ("group", "Formatting", False)
    assert button.parent_ref == group.ref
    assert button.depth == 1


def test_context_roles_need_a_name(node: NodeFactory, make_tree: TreeFactory) -> None:
    unnamed = node("AXList", node("AXRow", title="Row", frame=(0, 0, 100, 20)), frame=(0, 0, 100, 100))
    named = node(
        "AXList",
        node("AXRow", title="Row 2", frame=(0, 100, 100, 20)),
        description="Items",
        frame=(0, 100, 100, 100),
    )
    snap = build(make_tree(unnamed, named))
    assert [(el.role, el.name, el.depth) for el in snap.all_elements] == [
        ("row", "Row", 0),
        ("list", "Items", 0),
        ("row", "Row 2", 1),
    ]


def test_lists_report_rows_and_inherit_scrollable_and_viewport_from_collapsed_scroll_area(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    rows = [node("AXRow", title=f"Row {i}", frame=(0, i * 20, 200, 20)) for i in range(3)]
    hidden_row = node("AXRow", title="Hidden", frame=(0, 80, 200, 20), visible=False)
    area = node(
        "AXScrollArea",
        node("AXList", *rows, hidden_row, description="Things", frame=(0, 0, 200, 300)),
        node("AXScrollBar", frame=(200, 0, 15, 300)),
        frame=(0, 0, 215, 300),
    )
    lst = build(make_tree(area)).all_elements[0]
    # The scroll area is collapsed, so its on-screen frame travels with the list.
    assert lst.extra == {"rows": 3, "scrollable": True, "viewport": [0, 0, 215, 300]}


def test_named_scroll_area_is_itself_marked_scrollable(node: NodeFactory, make_tree: TreeFactory) -> None:
    content = node("AXButton", title="Far", frame=(0, 0, 100, 900))
    area = node("AXScrollArea", content, description="Canvas", frame=(0, 0, 300, 300))
    area_el, button = build(make_tree(area)).all_elements
    assert area_el.extra.get("scrollable") is True
    assert "scrollable" not in button.extra


def test_scroll_area_without_overflow_or_scrollbar_is_not_scrollable(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    area = node(
        "AXScrollArea", node("AXTextArea", value="hi", frame=(0, 0, 100, 100)), frame=(0, 0, 100, 100)
    )
    (text_area,) = build(make_tree(area)).all_elements
    assert "scrollable" not in text_area.extra


def test_row_count_includes_children_cut_by_capture_budget(node: NodeFactory, make_tree: TreeFactory) -> None:
    table = node(
        "AXTable",
        node("AXRow", title="A", frame=(0, 0, 100, 20)),
        node("AXRow", title="B", frame=(0, 20, 100, 20)),
        description="Big",
        frame=(0, 0, 100, 400),
        child_count=482,
    )
    assert build(make_tree(table)).all_elements[0].extra["rows"] == 482


def test_static_text_is_dropped_in_interactive_mode_but_kept_in_dialogs(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    sheet = node(
        "AXSheet",
        node("AXStaticText", value="Save changes?", frame=(110, 110, 200, 20)),
        node("AXButton", title="Save", frame=(300, 200, 60, 24)),
        frame=(100, 100, 400, 200),
    )
    tree = make_tree(node("AXStaticText", value="Body text", frame=(10, 10, 100, 20)), sheet)
    assert [(el.role, el.name) for el in build(tree).all_elements] == [
        ("sheet", None),
        ("text", "Save changes?"),
        ("button", "Save"),
    ]


def test_window_root_and_traffic_light_buttons_are_not_listed(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    tree = make_tree(
        node("AXButton", subrole="AXCloseButton", frame=(8, 6, 14, 16), actions=("AXPress",)),
        node("AXButton", subrole="AXMinimizeButton", frame=(28, 6, 14, 16), actions=("AXPress",)),
        node("AXButton", title="OK", frame=(100, 100, 40, 20)),
    )
    assert names(build(tree)) == ["OK"]
    full = build(tree, mode="full")
    assert names(full) == ["Doc", "Close", "Minimize", "OK"]


def test_menu_bar_nodes_inside_the_tree_are_left_to_the_menus_line(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    bar = node("AXMenuBar", node("AXMenuBarItem", title="File", frame=(40, 0, 40, 25)), frame=(0, 0, 800, 25))
    tree = make_tree(bar, node("AXButton", title="OK", frame=(100, 100, 40, 20)))
    assert names(build(tree)) == ["OK"]


# --------------------------------------------------------------------------- names & values
def test_secure_fields_never_expose_their_value(node: NodeFactory, make_tree: TreeFactory) -> None:
    by_flag = node("AXTextField", description="PIN", value="1234", secure=True, frame=(0, 0, 100, 20))
    by_subrole = node(
        "AXTextField",
        subrole="AXSecureTextField",
        description="Password",
        value="hunter2",
        frame=(0, 30, 100, 20),
    )
    snap = build(make_tree(by_flag, by_subrole))
    for el in snap.all_elements:
        assert el.value is None
        assert el.secure
        assert "secure" in el.states
    assert snap.all_elements[1].role == "secure"


def test_values_are_truncated_with_an_ellipsis(node: NodeFactory, make_tree: TreeFactory) -> None:
    field = node("AXTextArea", value="abcdefghijklmnopqrstuvwxyz", frame=(0, 0, 100, 100))
    (el,) = build(make_tree(field), value_max_chars=10).all_elements
    assert el.value == "abcdefghi…"
    assert len(el.value) == 10


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"title": "T", "label": "L", "description": "D", "placeholder": "P"}, "T"),
        ({"label": "L", "description": "D", "placeholder": "P"}, "L"),
        ({"description": "D", "placeholder": "P"}, "D"),
        ({"placeholder": "P"}, "P"),
        ({}, None),
    ],
)
def test_name_priority(kwargs: dict[str, str], expected: str | None) -> None:
    node = RawNode(
        role="AXTextField",
        title=kwargs.get("title"),
        label=kwargs.get("label"),
        description=kwargs.get("description"),
        placeholder=kwargs.get("placeholder"),
    )
    assert node_name(node) == expected


def test_names_collapse_whitespace_and_are_capped() -> None:
    assert node_name(RawNode(role="AXButton", title="  Save\n  As…  ")) == "Save As…"
    long_name = node_name(RawNode(role="AXButton", title="x" * 500))
    assert long_name is not None
    assert len(long_name) == NAME_MAX_CHARS
    assert long_name.endswith("…")


def test_unnamed_rows_and_links_are_labelled_by_their_text(node: NodeFactory) -> None:
    row = node(
        "AXRow",
        node("AXCell", node("AXStaticText", value="Shopping list"), node("AXStaticText", value="10:42")),
        node("AXCell", node("AXStaticText", value="Milk, eggs")),
    )
    link = node("AXLink", node("AXStaticText", value="GitHub"), actions=("AXPress",))
    hidden_text = node(
        "AXButton", node("AXStaticText", value="secret", visible=False), node("AXStaticText", value="Go")
    )
    assert node_name(row) == "Shopping list — 10:42"
    assert node_name(link) == "GitHub"
    assert node_name(hidden_text) == "Go"


def test_rows_are_labelled_by_their_text_fields_too(node: NodeFactory) -> None:
    """Finder's list view (macOS 26): the Name column is an editable text field, the other
    columns static text; every row of a folder made at once has the same date and size."""
    finder_row = node(
        "AXRow",
        node("AXCell", node("AXTextField", value="file-001.txt")),
        node("AXCell", node("AXStaticText", value="Today at 2:57 PM")),
        node("AXCell", node("AXStaticText", value="Zero bytes")),
    )
    secure_row = node(
        "AXRow",
        node("AXCell", node("AXTextField", value="hunter2", secure=True)),
        node("AXCell", node("AXStaticText", value="Account")),
    )
    button = node("AXButton", node("AXTextField", value="typed"), node("AXStaticText", value="Go"))
    assert node_name(finder_row) == "file-001.txt — Today at 2:57 PM"
    assert node_name(secure_row) == "Account"
    assert node_name(button) == "Go"  # only rows take field values


def test_text_inputs_are_not_labelled_by_their_content(node: NodeFactory) -> None:
    field = node("AXTextArea", node("AXStaticText", value="typed text"))
    assert node_name(field) is None


def test_static_text_uses_its_value_as_name(node: NodeFactory, make_tree: TreeFactory) -> None:
    sheet = node(
        "AXSheet", node("AXStaticText", value="Saved.", frame=(10, 10, 100, 20)), frame=(0, 0, 400, 300)
    )
    text = build(make_tree(sheet)).all_elements[1]
    assert (text.name, text.value) == ("Saved.", None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, "true"),
        (False, "false"),
        (3, "3"),
        (0.5, "0.5"),
        (12.0, "12"),
        (["a", "b"], "a, b"),
        ({"k": 1}, '{"k": 1}'),
        ("text", "text"),
    ],
)
def test_value_stringification(value: object, expected: str | None) -> None:
    assert stringify_value(value, 200) == expected


def test_toggle_values_become_checked_state(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXCheckBox", title="On", value=1, frame=(0, 0, 50, 20)),
        node("AXCheckBox", title="Off", value=0, frame=(0, 30, 50, 20)),
        node("AXCheckBox", title="Mixed", value=2, frame=(0, 60, 50, 20)),
    )
    on, off, mixed = build(tree).all_elements
    assert (on.checked, off.checked, mixed.checked) == (True, False, True)
    assert "checked" in on.states
    assert "checked" not in off.states
    assert "mixed" in mixed.states


def test_extra_carries_identifier_description_and_url(node: NodeFactory, make_tree: TreeFactory) -> None:
    link = node(
        "AXLink",
        title="Docs",
        description="Open the docs",
        identifier="docs-link",
        url="https://example.com/docs",
        frame=(0, 0, 50, 20),
        actions=("AXPress",),
    )
    (el,) = build(make_tree(link)).all_elements
    assert el.extra == {
        "identifier": "docs-link",
        "description": "Open the docs",
        "url": "https://example.com/docs",
    }
    assert el.identifier == "docs-link"


def test_states_in_display_order(node: NodeFactory, make_tree: TreeFactory) -> None:
    row = node(
        "AXRow", title="R", enabled=False, selected=True, focused=True, expanded=False, frame=(0, 0, 10, 10)
    )
    (el,) = build(make_tree(row)).all_elements
    assert el.states == ("disabled", "selected", "focused", "collapsed")


# --------------------------------------------------------------------------- paging & metadata
def test_paging_slices_elements_but_refs_resolve_against_all(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    tree = make_tree(*[node("AXButton", title=f"B{i}", frame=(0, i * 10, 40, 10)) for i in range(25)])
    page = build(tree, max_elements=10, offset=10)
    assert [el.ref for el in page.elements] == [f"e{i}" for i in range(11, 21)]
    assert page.total == 25
    assert page.next_offset == 20
    assert page.element("e1") is not None
    last = build(tree, max_elements=10, offset=20)
    assert len(last.elements) == 5
    assert last.next_offset is None


def test_offset_past_the_end_gives_an_empty_page(node: NodeFactory, make_tree: TreeFactory) -> None:
    snap = build(make_tree(node("AXButton", title="Only", frame=(0, 0, 10, 10))), offset=50)
    assert snap.elements == []
    assert snap.total == 1
    assert snap.next_offset is None


@pytest.mark.parametrize(
    "kwargs",
    [{"mode": "everything"}, {"max_elements": 0}, {"offset": -1}, {"value_max_chars": 0}],
)
def test_invalid_arguments_are_rejected(make_tree: TreeFactory, kwargs: dict[str, object]) -> None:
    with pytest.raises(AXPilotError) as info:
        build(make_tree(), **kwargs)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_focused_ref_points_at_the_focused_element(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXButton", title="A", frame=(0, 0, 10, 10)),
        node("AXTextField", description="Name", focused=True, frame=(0, 20, 100, 20)),
    )
    snap = build(tree)
    assert snap.focused_ref == "e2"
    assert snap.focused_element is snap.all_elements[1]


def test_focused_ref_is_none_when_the_focused_node_was_pruned(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    tree = make_tree(
        node("AXGroup", focused=True, frame=(0, 0, 10, 10)),
        node("AXButton", title="A", frame=(0, 20, 10, 10)),
    )
    assert build(tree).focused_ref is None


def test_menus_list_top_level_titles_without_the_apple_menu(
    node: NodeFactory, make_tree: TreeFactory
) -> None:
    bar = node(
        "AXMenuBar",
        *[node("AXMenuBarItem", title=title) for title in ("Apple", "Notes", "File", "Edit", "")],
    )
    assert build(make_tree(menu_bar=bar)).menus == ["Notes", "File", "Edit"]
    assert build(make_tree()).menus == []


def test_total_nodes_and_truncation_come_from_the_tree(node: NodeFactory, make_tree: TreeFactory) -> None:
    counted = make_tree(node("AXButton", title="A", frame=(0, 0, 10, 10)))
    assert build(counted).total_nodes == 2
    reported = make_tree(node_count=4000, truncated=True)
    snap = build(reported)
    assert (snap.total_nodes, snap.truncated) == (4000, True)


def test_locators_record_role_paths_from_the_root(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXButton", title="First", frame=(0, 0, 10, 10)),
        node("AXGroup", node("AXButton", title="Nested", frame=(0, 20, 10, 10), identifier="nested")),
        node("AXButton", title="Second", frame=(0, 40, 10, 10)),
    )
    first, nested, second = build(tree).all_elements
    assert first.locator.path == (("AXButton", 0),)
    assert nested.locator.path == (("AXGroup", 0), ("AXButton", 0))
    assert second.locator.path == (("AXButton", 1),)
    assert (nested.locator.pid, nested.locator.window_id, nested.locator.identifier) == (100, 7, "nested")
    assert nested.handle is not None


# --------------------------------------------------------------------------- modes
def test_text_mode_is_a_reading_view(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXHeading", node("AXStaticText", value="Title", frame=(0, 0, 100, 20)), frame=(0, 0, 100, 20)),
        node("AXStaticText", value="First paragraph.", frame=(0, 30, 300, 20)),
        node("AXButton", title="Continue", frame=(0, 60, 80, 20)),
        node("AXStaticText", value="   ", frame=(0, 90, 80, 20)),
    )
    snap = build(tree, mode="text")
    assert [(el.role, el.name) for el in snap.all_elements] == [
        ("heading", "Title"),
        ("text", "First paragraph."),
    ]


def test_full_mode_keeps_every_visible_node(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node("AXGroup", node("AXStaticText", value="x", frame=(0, 0, 10, 10)), frame=(0, 0, 50, 50)),
        node("AXImage", frame=(0, 60, 10, 10), visible=False),
    )
    snap = build(tree, mode="full")
    assert [el.role for el in snap.all_elements] == ["window", "group", "text"]
    assert [el.depth for el in snap.all_elements] == [0, 1, 2]


# --------------------------------------------------------------------------- demo desktop
def test_demo_notes_snapshot(snapshot_of: Callable[..., Snapshot]) -> None:
    snap = snapshot_of("Notes")
    by_name = {el.name: el for el in snap.all_elements}
    assert by_name["New Note"].role == "button"
    notes = by_name["Notes"]
    assert notes.role == "list"
    assert notes.extra == {"rows": 14, "scrollable": True, "viewport": [201, 77, 300, 823]}
    rows = [el for el in snap.all_elements if el.parent_ref == notes.ref]
    assert len(rows) == 12  # the last two rows are scrolled below the window
    assert rows[0].name == "Shopping list — 10:42"
    assert rows[0].selected
    focused = snap.focused_element
    assert focused is not None
    assert focused.name == "Note body"
    assert snap.menus == ["Notes", "File", "Edit", "Format", "View", "Window", "Help"]
    assert "New Note — 10:45" not in by_name  # hidden until "New Note" is pressed


def test_lists_without_row_children_count_their_items(node: NodeFactory, make_tree: TreeFactory) -> None:
    items = [
        node("AXGroup", node("AXStaticText", value=f"Item {i}"), frame=(0, i * 20, 100, 20)) for i in range(4)
    ]
    lst = node(
        "AXList",
        *items,
        node("AXScrollBar", frame=(100, 0, 15, 80)),
        description="Photos",
        frame=(0, 0, 115, 80),
    )
    assert build(make_tree(lst)).all_elements[0].extra["rows"] == 4


def test_an_application_root_has_no_window_bounds(node: NodeFactory) -> None:
    root = node("AXApplication", node("AXButton", title="Far away", frame=(5000, 5000, 40, 20)), title="Demo")
    snap = build(RawTree(app=AppInfo("Demo", 1), window=None, root=root))
    assert names(snap) == ["Far away"]
    assert snap.window is None


def test_truncate_edge_cases() -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("abcd", 1) == "…"
    assert truncate("ab  cd", 4) == "ab…"
