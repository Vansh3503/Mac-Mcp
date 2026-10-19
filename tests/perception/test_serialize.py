from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import tree_from_dict, tree_to_dict
from axpilot.perception.serialize import node_from_dict, node_to_dict

NODE_FIELDS = [f.name for f in dataclasses.fields(RawNode) if f.name not in ("children", "handle")]


def every_field_node() -> RawNode:
    child = RawNode(role="AXStaticText", value="Label", frame=Rect(1.5, 2.25, 30, 12))
    return RawNode(
        role="AXCheckBox",
        subrole="AXSwitch",
        title="Wi-Fi",
        description="Toggle Wi-Fi",
        value=1,
        help="Turns Wi-Fi on or off",
        identifier="wifi-switch",
        placeholder="(none)",
        label="Wi-Fi:",
        url="x-apple.systempreferences:wifi",
        enabled=False,
        focused=True,
        selected=True,
        expanded=False,
        secure=True,
        visible=False,
        frame=Rect(-120.5, 40, 51, 31),
        actions=("AXPress", "AXShowMenu"),
        child_count=7,
        children=[child],
        handle=object(),
    )


def assert_same_node(a: RawNode, b: RawNode) -> None:
    for name in NODE_FIELDS:
        assert getattr(a, name) == getattr(b, name), name
    assert len(a.children) == len(b.children)
    for left, right in zip(a.children, b.children, strict=True):
        assert_same_node(left, right)


def sample_tree() -> RawTree:
    app = AppInfo(name="Notes", pid=812, bundle_id="com.apple.Notes", path="/System/Applications/Notes.app")
    window = WindowInfo(
        101, 812, "Notes", "Notes", Rect(0, 25, 1440, 875), bundle_id="com.apple.Notes", focused=True
    )
    root = RawNode(
        role="AXWindow", title="Notes", frame=Rect(0, 25, 1440, 875), children=[every_field_node()]
    )
    menu_bar = RawNode(role="AXMenuBar", children=[RawNode(role="AXMenuBarItem", title="File")])
    return RawTree(app, window, root, menu_bar=menu_bar, node_count=4, truncated=True, elapsed_ms=12.5)


def test_every_node_field_round_trips_except_the_handle() -> None:
    original = every_field_node()
    restored = node_from_dict(json.loads(json.dumps(node_to_dict(original))))
    assert_same_node(original, restored)
    assert restored.handle is None


def test_defaults_are_omitted() -> None:
    assert node_to_dict(RawNode(role="AXButton")) == {"role": "AXButton"}
    assert node_to_dict(RawNode(role="AXButton", frame=Rect(1, 2, 3, 4.5), actions=("AXPress",))) == {
        "role": "AXButton",
        "frame": [1, 2, 3, 4.5],
        "actions": ["AXPress"],
    }


def test_tree_round_trip_through_json() -> None:
    tree = sample_tree()
    data = json.loads(json.dumps(tree_to_dict(tree)))
    restored = tree_from_dict(data)
    assert restored.app == tree.app
    assert restored.window == tree.window
    assert_same_node(tree.root, restored.root)
    assert restored.menu_bar is not None
    assert tree.menu_bar is not None
    assert_same_node(tree.menu_bar, restored.menu_bar)
    assert (restored.node_count, restored.truncated, restored.elapsed_ms) == (4, True, 12.5)
    assert tree_to_dict(restored) == data


def test_window_inherits_app_fields_when_omitted() -> None:
    data = {
        "app": {"name": "Notes", "pid": 812, "bundle_id": "com.apple.Notes"},
        "window": {"window_id": 101, "title": "Notes", "frame": [0, 25, 1440, 875]},
        "root": {"role": "AXWindow", "title": "Notes", "frame": [0, 25, 1440, 875]},
    }
    tree = tree_from_dict(data)
    assert tree.window == WindowInfo(
        101, 812, "Notes", "Notes", Rect(0, 25, 1440, 875), bundle_id="com.apple.Notes"
    )
    assert tree.menu_bar is None
    assert tree_to_dict(tree) == data


def test_handles_are_assigned_in_pre_order_across_root_and_menu_bar() -> None:
    tree = tree_from_dict(tree_to_dict(sample_tree()))
    handles = [node.handle for node in tree.root.walk()]
    assert handles == ["n0", "n1", "n2"]
    assert tree.menu_bar is not None
    assert [node.handle for node in tree.menu_bar.walk()] == ["n3", "n4"]


def test_non_json_values_are_stringified() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque"

    data = node_to_dict(RawNode(role="AXValueIndicator", value={"pos": (1, 2.0), "obj": Opaque()}))
    assert data["value"] == {"pos": [1, 2.0], "obj": "opaque"}
    json.dumps(data)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"role": "AXButton", "colour": "red"}, "unknown key(s): colour"),
        ({"role": ""}, "role must be a non-empty string"),
        ({"title": "x"}, "role must be a non-empty string"),
        ({"role": "AXButton", "frame": [0, 0, 10]}, "frame must be [x, y, w, h]"),
        ({"role": "AXButton", "frame": [0, 0, "w", 4]}, "frame must be [x, y, w, h]"),
        ({"role": "AXButton", "enabled": "yes"}, "enabled must be true or false"),
        ({"role": "AXButton", "title": 3}, "title must be a string"),
        ({"role": "AXButton", "actions": "AXPress"}, "actions must be a list of strings"),
        ({"role": "AXButton", "children": {}}, "children must be a list"),
        (
            {"role": "AXGroup", "children": [{"role": "AXButton", "expanded": "no"}]},
            "root.children[0].expanded",
        ),
        ({"role": "AXList", "child_count": 1.5}, "child_count must be an integer"),
    ],
)
def test_malformed_nodes_are_rejected_with_their_location(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(AXPilotError) as info:
        node_from_dict(data)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in info.value.message


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"app": {"name": "X", "pid": 1}},
        {"app": {"name": "X"}, "root": {"role": "AXWindow"}},
        {"app": {"name": "X", "pid": 1}, "root": {"role": "AXWindow"}, "extra": 1},
        {
            "app": {"name": "X", "pid": 1},
            "window": {"title": "no id", "frame": [0, 0, 1, 1]},
            "root": {"role": "AXWindow"},
        },
        {"app": {"name": "X", "pid": 1}, "root": {"role": "AXWindow"}, "node_count": "many"},
    ],
)
def test_malformed_trees_are_rejected(data: Any) -> None:
    with pytest.raises(AXPilotError) as info:
        tree_from_dict(data)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_extra_keys_and_node_hook() -> None:
    seen: list[tuple[str, Any]] = []
    node = node_from_dict(
        {"role": "AXGroup", "id": "g", "children": [{"role": "AXButton", "id": "b", "on_press": {"x": 1}}]},
        extra_keys={"id", "on_press"},
        on_node=lambda n, d: seen.append((n.role, d.get("id"))),
    )
    assert [child.role for child in node.children] == ["AXButton"]
    assert seen == [("AXButton", "b"), ("AXGroup", "g")]


@pytest.mark.parametrize("window_id", [201, 301, 401, 501, 601, 701, 801])
def test_demo_window_trees_round_trip(window_id: int) -> None:
    tree = FakeEngine.demo().capture_tree(window_id=window_id, include_menus=True)
    data = tree_to_dict(tree)
    restored = tree_from_dict(json.loads(json.dumps(data)))
    assert_same_node(tree.root, restored.root)
    assert tree_to_dict(restored) == data


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"app": "Notes", "root": {"role": "AXWindow"}}, "app must be an object"),
        (
            {"app": {"name": "X", "pid": 1, "colour": 2}, "root": {"role": "AXWindow"}},
            "app has unknown key(s)",
        ),
        (
            {"app": {"name": "X", "pid": 1}, "window": 5, "root": {"role": "AXWindow"}},
            "window must be an object",
        ),
        (
            {
                "app": {"name": "X", "pid": 1},
                "window": {"window_id": 1, "frame": [0, 0, 1, 1], "z": 1},
                "root": {"role": "AXWindow"},
            },
            "window has unknown key(s)",
        ),
        ({"app": {"name": "X", "pid": 1}, "root": "AXWindow"}, "root must be an object"),
    ],
)
def test_malformed_apps_and_windows_name_the_problem(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(AXPilotError) as info:
        tree_from_dict(data)
    assert fragment in info.value.message


def test_window_from_dict_without_an_app_needs_pid_and_app() -> None:
    from axpilot.perception.serialize import window_from_dict, window_to_dict

    with pytest.raises(AXPilotError):
        window_from_dict({"window_id": 1, "frame": [0, 0, 1, 1]})
    window = WindowInfo(3, 9, "Other", "T", Rect(0, 0, 5, 5), minimized=True)
    data = window_to_dict(window, AppInfo("Mine", 1))
    assert data["pid"] == 9
    assert data["app"] == "Other"
    assert data["minimized"] is True
    assert window_from_dict(data) == window
