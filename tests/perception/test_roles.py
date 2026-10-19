from __future__ import annotations

import pytest

from axpilot.models import RawNode
from axpilot.perception.roles import INTERACTIVE_ROLES, is_interactive, short_role


@pytest.mark.parametrize(
    ("ax_role", "subrole", "expected"),
    [
        ("AXButton", None, "button"),
        ("AXButton", "AXCloseButton", "button"),
        ("AXTextField", None, "field"),
        ("AXTextField", "AXSearchField", "search"),
        ("AXTextField", "AXSecureTextField", "secure"),
        ("AXTextArea", None, "textarea"),
        ("AXStaticText", None, "text"),
        ("AXCheckBox", None, "checkbox"),
        ("AXCheckBox", "AXToggle", "toggle"),
        ("AXCheckBox", "AXSwitch", "switch"),
        ("AXRadioButton", None, "radio"),
        ("AXRadioButton", "AXTabButton", "tab"),
        ("AXPopUpButton", None, "popup"),
        ("AXMenuButton", None, "menubutton"),
        ("AXComboBox", None, "combobox"),
        ("AXSlider", None, "slider"),
        ("AXIncrementor", None, "stepper"),
        ("AXLink", None, "link"),
        ("AXRow", None, "row"),
        ("AXCell", None, "cell"),
        ("AXTable", None, "table"),
        ("AXOutline", None, "outline"),
        ("AXList", None, "list"),
        ("AXTabGroup", None, "tabs"),
        ("AXImage", None, "image"),
        ("AXWebArea", None, "webarea"),
        ("AXScrollArea", None, "scrollarea"),
        ("AXGroup", None, "group"),
        ("AXWindow", None, "window"),
        ("AXWindow", "AXDialog", "dialog"),
        ("AXWindow", "AXSystemDialog", "dialog"),
        ("AXSheet", None, "sheet"),
        ("AXToolbar", None, "toolbar"),
        ("AXMenuBar", None, "menubar"),
        ("AXMenuBarItem", None, "menu"),
        ("AXMenu", None, "menu"),
        ("AXMenuItem", None, "menuitem"),
        ("AXDisclosureTriangle", None, "disclosure"),
        ("AXColorWell", None, "colorwell"),
        ("AXSplitter", None, "splitter"),
        ("AXHeading", None, "heading"),
        ("AXProgressIndicator", None, "progress"),
        ("AXBusyIndicator", None, "busy"),
        ("AXValueIndicator", None, "indicator"),
        ("AXDateField", None, "date"),
        ("AXApplication", None, "app"),
    ],
)
def test_short_role_table(ax_role: str, subrole: str | None, expected: str) -> None:
    assert short_role(ax_role, subrole) == expected


@pytest.mark.parametrize(
    ("ax_role", "expected"),
    [
        ("AXSplitGroup", "splitgroup"),
        ("AXRadioGroup", "radiogroup"),
        ("AXLayoutArea", "layoutarea"),
        ("Foo", "foo"),
    ],
)
def test_short_role_falls_back_to_lowercased_name_without_prefix(ax_role: str, expected: str) -> None:
    assert short_role(ax_role) == expected


def test_subrole_only_refines_matching_role() -> None:
    # AXSearchField is a subrole of text fields; on a button it means nothing.
    assert short_role("AXButton", "AXSearchField") == "button"


@pytest.mark.parametrize("role", sorted(INTERACTIVE_ROLES))
def test_interactive_roles_are_interactive_without_actions(role: str) -> None:
    assert is_interactive(RawNode(role=role))


def test_meaningful_action_makes_any_node_interactive() -> None:
    assert is_interactive(RawNode(role="AXGroup", actions=("AXPress",)))
    assert is_interactive(RawNode(role="AXImage", actions=("AXShowMenu", "AXIncrement")))


def test_passive_actions_alone_do_not_make_a_node_interactive() -> None:
    assert not is_interactive(RawNode(role="AXGroup", actions=("AXShowMenu", "AXScrollToVisible")))
    assert not is_interactive(RawNode(role="AXStaticText"))


def test_focused_text_roles_count_as_interactive() -> None:
    assert is_interactive(RawNode(role="AXWebArea", focused=True))
    assert is_interactive(RawNode(role="AXStaticText", focused=True))
    assert not is_interactive(RawNode(role="AXWebArea"))
    assert not is_interactive(RawNode(role="AXGroup", focused=True))
