"""Pure helpers that find what an open dropdown shows (:mod:`axpilot.perception.dropdown`)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import RawNode, Rect
from axpilot.perception import dropdown as dd


def node(role: str, handle: str, *children: RawNode, **fields: object) -> RawNode:
    fields.setdefault("frame", Rect(0, 0, 100, 20))
    return RawNode(role=role, handle=handle, children=list(children), **fields)  # type: ignore[arg-type]


def item(handle: str, title: str, **fields: object) -> RawNode:
    return node("AXMenuItem", handle, title=title, **fields)


# =========================================================================== matching
@pytest.mark.parametrize(
    ("option", "index"),
    [("Heading", 1), ("heading", 1), ("head", 1), ("Sub", 2), ("Save As...", 3), ("  save   as… ", 3)],
)
def test_match_option(option: str, index: int) -> None:
    assert dd.match_option(["Title", "Heading", "Subheading", "Save As…"], option) == index


def test_a_word_inside_a_title_is_not_a_match() -> None:
    """TextEdit (macOS 26): "Bold" on the typeface pop-up chose the font "Arial Rounded MT Bold"."""
    with pytest.raises(AXPilotError) as caught:
        dd.match_option(["Helvetica", "Arial Rounded MT Bold", "Times"], "Bold")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.details["options"] == ["Arial Rounded MT Bold"]
    assert '"Arial Rounded MT Bold"' in caught.value.message


def test_a_title_listed_twice_is_the_first() -> None:
    """TextEdit lists recently used fonts again above the full list."""
    assert dd.match_option(["Recently Used", "Helvetica", "Times", "Arial", "Helvetica"], "helvetica") == 1
    with pytest.raises(AXPilotError, match="several"):
        dd.match_option(["Bold", "BOLD"], "bold")  # different titles that fold alike stay ambiguous


def test_leading_marks_are_skipped() -> None:
    assert dd.match_option(["🇫🇷 France", "🇯🇵 Japan", "• Kenya"], "Japan") == 1
    assert dd.match_option(["✓ Show Sidebar", "Hide Toolbar"], "show") == 0


def test_match_option_errors_list_the_options() -> None:
    with pytest.raises(AXPilotError) as caught:
        dd.match_option(["Align Left", "Center", "Align Right"], "Align")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.details["options"] == ["Align Left", "Align Right"]
    with pytest.raises(AXPilotError) as caught:
        dd.match_option(["A", "B"], "zebra")
    assert 'Options: "A", "B"' in caught.value.message
    with pytest.raises(AXPilotError):
        dd.match_option([], "x")


@given(st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=8, unique_by=dd.fold), st.data())
def test_an_exact_title_always_matches_itself(names: list[str], data: st.DataObject) -> None:
    names = [name for name in names if dd.fold(name)] or ["x"]
    index = data.draw(st.integers(0, len(names) - 1))
    assert dd.match_option(names, names[index]) == index


def test_fold() -> None:
    assert dd.fold("  Save   As...  ") == dd.fold("save as…") == "save as"
    assert dd.fold(None) == ""
    assert dd.fold("Dr.") == "dr"


# =========================================================================== menus
def test_menu_options_skip_separators_and_hidden_items() -> None:
    menu = node(
        "AXMenu",
        "m",
        item("a", "Title"),
        item("sep", "", enabled=False),
        item("b", "Body", selected=True),
        item("c", "Hidden", visible=False),
        item("d", "Zero", frame=Rect(0, 0, 0, 0)),
        node("AXStaticText", "t", value="not an item"),
    )
    options = dd.menu_options(menu)
    assert [o.title for o in options] == ["Title", "Body"]
    assert options[1].selected
    assert options[0].enabled


def test_open_menu_of_a_control() -> None:
    closed = node("AXPopUpButton", "p", node("AXMenu", "m", item("a", "A"), frame=Rect(0, 0, 0, 0)))
    assert dd.open_menu_of(closed) is None
    opened = node("AXPopUpButton", "p", node("AXMenu", "m", item("a", "A")))
    assert dd.open_menu_of(opened) is opened.children[0]
    wrapped = node("AXMenuButton", "b", node("AXGroup", "g", node("AXMenu", "m", item("a", "A"))))
    assert dd.open_menu_of(wrapped) is wrapped.children[0].children[0]
    empty = node("AXPopUpButton", "p", node("AXMenu", "m"))
    assert dd.open_menu_of(empty) is None


def test_new_menus_ignore_menus_seen_before_submenus_and_the_menu_bar() -> None:
    old = node("AXMenu", "old", item("o", "Old"))
    submenu = node("AXMenu", "sub", item("s", "Deeper"))
    fresh = node(
        "AXMenu",
        "new",
        item(
            "n",
            "New",
        ),
        node("AXMenuItem", "parent", submenu, title="More"),
    )
    bar = node(
        "AXMenuBar", "bar", node("AXMenuBarItem", "file", node("AXMenu", "filemenu", item("f", "Open")))
    )
    before = node("AXWindow", "w", old, bar)
    after = node("AXWindow", "w", old, bar, fresh)
    found = dd.new_menus(after, dd.visible_handles(before))
    assert [menu.handle for menu, _ in found] == ["new"]
    assert dd.new_menus(before, dd.visible_handles(before)) == []


def test_a_menu_that_was_hidden_before_counts_as_new() -> None:
    menu = node("AXMenu", "m", item("a", "A"), visible=False)
    before = node("AXWindow", "w", menu)
    baseline = dd.visible_handles(before)
    menu.visible = True
    assert [m.handle for m, _ in dd.new_menus(before, baseline)] == ["m"]


def test_combo_list_reads_text_field_rows() -> None:
    """TextEdit's font-size box (macOS 26): its open list is an ``AXList`` of ``AXTextField`` rows in a
    scroll area, which used to count as no options at all."""
    actions = ("AXShowMenu", "AXConfirm")
    rows = [node("AXTextField", f"r{size}", value=size, actions=actions) for size in ("9", "24")]
    box = node(
        "AXComboBox",
        "box",
        node("AXButton", "arrow", actions=("AXPress",)),
        node("AXScrollArea", "scroll", node("AXList", "list", *rows)),
        value="12",
    )
    found = dd.combo_list(box)
    assert found is not None
    assert found.titles() == ["9", "24"]
    assert found.container.handle == "list"
    assert dd.combo_arrow(box) is box.children[0]


# =========================================================================== web lists
def web(*children: RawNode) -> RawNode:
    return node(
        "AXWindow", "w", node("AXGroup", "wrap", node("AXWebArea", "page", *children), frame=Rect(0, 0, 0, 0))
    )


def test_page_options_find_the_list_that_appeared() -> None:
    control = node("AXComboBox", "combo", description="Shipping")
    listing = node(
        "AXList",
        "list",
        node("AXStaticText", "o1", value="Standard"),
        node("AXStaticText", "o2", value="Express"),
        node("AXRow", "o3", node("AXCell", "c", node("AXStaticText", "t", value="Overnight"))),
        visible=False,
    )
    tree = web(control, listing)
    baseline = dd.visible_handles(tree)
    listing.visible = True
    found = dd.page_options(tree, baseline, exclude="combo")
    assert found is not None
    assert found.where == "page"
    assert found.in_web
    assert found.container is listing
    assert found.titles() == ["Standard", "Express", "Overnight"]  # the row, not its cell's text


def test_page_options_prefer_a_real_list_and_ignore_stray_new_text() -> None:
    stray = node("AXStaticText", "hint", value="3 results")
    listing = node("AXList", "list", node("AXStaticText", "a", value="Lisbon"), visible=False)
    tree = web(node("AXGroup", "g", stray, visible=False), listing)
    baseline = dd.visible_handles(tree)
    tree.children[0].children[0].children[0].visible = True
    listing.visible = True
    found = dd.page_options(tree, baseline)
    assert found is not None
    assert found.container is listing
    assert found.titles() == ["Lisbon"]


def test_page_options_need_two_options_outside_a_list_and_skip_the_control() -> None:
    lonely = node("AXGroup", "g", node("AXStaticText", "a", value="Only one"), visible=False)
    tree = web(lonely)
    baseline = dd.visible_handles(tree)
    lonely.visible = True
    assert dd.page_options(tree, baseline) is None
    pair = node(
        "AXGroup",
        "g2",
        node("AXButton", "b1", title="Red"),
        node("AXButton", "b2", title="Blue"),
        visible=False,
    )
    tree = web(pair)
    baseline = dd.visible_handles(tree)
    pair.visible = True
    found = dd.page_options(tree, baseline)
    assert found is not None
    assert found.titles() == ["Red", "Blue"]
    assert dd.page_options(tree, baseline, exclude="b1") is None
    assert dd.page_options(node("AXWindow", "w"), baseline) is None


def test_an_aria_menu_in_a_page_is_an_app_menu() -> None:
    menu = node("AXMenu", "m", item("a", "Newest"), item("b", "Oldest"), visible=False)
    tree = web(menu)
    baseline = dd.visible_handles(tree)
    menu.visible = True
    found = dd.page_options(tree, baseline)
    assert found is not None
    assert found.where == "app_menu"


def test_options_in_a_popup_window() -> None:
    window = node(
        "AXWindow",
        "popup",
        node("AXList", "short", node("AXStaticText", "x", value="Only")),
        node(
            "AXList",
            "long",
            node("AXStaticText", "a", value="Main"),
            node("AXStaticText", "b", value="Tests"),
        ),
    )
    found = dd.options_in(window)
    assert found is not None
    assert found.where == "window"
    assert found.titles() == ["Main", "Tests"]
    menu_window = node("AXWindow", "w", node("AXMenu", "m", item("a", "Copy")))
    found = dd.options_in(menu_window)
    assert found is not None
    assert found.container.role == "AXMenu"
    assert dd.options_in(node("AXWindow", "empty", node("AXList", "l"))) is None


# =========================================================================== verification and lookup
def test_shows_choice() -> None:
    assert dd.shows_choice(RawNode(role="AXPopUpButton", value="Heading"), "heading")
    assert dd.shows_choice(RawNode(role="AXButton", title="Sort: Newest"), "Newest")
    assert dd.shows_choice(RawNode(role="AXComboBox", description="Express"), "Express")
    assert not dd.shows_choice(RawNode(role="AXPopUpButton", value="Body"), "Heading")
    assert not dd.shows_choice(RawNode(role="AXPopUpButton", value="Body"), "  ")


def test_text_near_the_control() -> None:
    control = node("AXComboBox", "combo")
    group = node("AXGroup", "g", node("AXStaticText", "v", value="Express (2 days)"), control)
    listing = node("AXList", "l", node("AXStaticText", "o", value="Overnight"))
    tree = web(node("AXGroup", "form", group, listing))
    assert dd.text_near(tree, "combo", "Express (2 days)")
    assert not dd.text_near(tree, "combo", "Overnight")  # text inside a list is an option, not the value
    assert not dd.text_near(tree, "missing", "Express (2 days)")
    assert not dd.text_near(control, "combo", "Express")  # no ancestors


def test_find_node_and_web_content() -> None:
    hidden = node("AXStaticText", "h", visible=False)
    tree = web(hidden)
    found = dd.find_node(tree, "h")
    assert found is not None
    assert found[0] is hidden
    assert dd.in_web_content(found[1])
    assert not dd.in_web_content(())
    assert dd.find_node(tree, "nope") is None
    assert dd.option_title(RawNode(role="AXStaticText", value=12)) == "12"
