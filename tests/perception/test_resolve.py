from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import Locator, Snapshot, build_snapshot, live_handle, match_locator
from axpilot.perception.serialize import assign_handles


class StubEngine:
    """Just enough of the Engine interface for live_handle: validity + re-capture."""

    def __init__(
        self, tree: RawTree | None, *, valid: set[Any] | None = None, error: AXPilotError | None = None
    ):
        self.tree = tree
        self.valid = valid or set()
        self.error = error
        self.captures: list[dict[str, Any]] = []

    def element_valid(self, handle: Any) -> bool:
        return handle in self.valid

    def capture_tree(self, **kwargs: Any) -> RawTree:
        self.captures.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.tree is not None
        return self.tree


def tree_of(*children: RawNode, prefix: str = "n") -> RawTree:
    root = RawNode(role="AXWindow", frame=Rect(0, 0, 500, 500), children=list(children))
    assign_handles(root, prefix=prefix)
    app = AppInfo("Demo", 42, "com.example.demo")
    return RawTree(app, WindowInfo(9, 42, "Demo", "Doc", Rect(0, 0, 500, 500)), root)


def button(title: str, y: float = 0, **kw: Any) -> RawNode:
    return RawNode(role="AXButton", title=title, frame=Rect(0, y, 50, 20), **kw)


def first(snap: Snapshot, name: str):  # type: ignore[no-untyped-def]
    return next(el for el in snap.all_elements if el.name == name)


def test_a_valid_handle_is_returned_without_recapturing() -> None:
    snap = build_snapshot(tree_of(button("OK")), snapshot_id="s_aaaa")
    el = snap.all_elements[0]
    engine = StubEngine(None, valid={el.handle})
    assert live_handle(engine, snap, el) == el.handle  # type: ignore[arg-type]
    assert engine.captures == []


def test_stale_handle_is_re_found_by_identifier() -> None:
    snap = build_snapshot(tree_of(button("Save", identifier="save")), snapshot_id="s_aaaa")
    fresh = tree_of(RawNode(role="AXGroup", children=[button("Save…", 40, identifier="save")]), prefix="m")
    engine = StubEngine(fresh)
    assert live_handle(engine, snap, snap.all_elements[0]) == "m2"  # type: ignore[arg-type]
    assert engine.captures == [{"app": "com.example.demo", "window_id": 9}]


def test_stale_handle_is_re_found_by_path_then_by_role_and_name() -> None:
    snap = build_snapshot(tree_of(button("A"), button("B", 30)), snapshot_id="s_aaaa")
    same_layout = tree_of(button("A"), button("B", 30), prefix="m")
    assert live_handle(StubEngine(same_layout), snap, first(snap, "B")) == "m2"  # type: ignore[arg-type]
    reordered = tree_of(button("B", 30), RawNode(role="AXGroup", children=[button("A")]), prefix="m")
    assert live_handle(StubEngine(reordered), snap, first(snap, "A")) == "m3"  # type: ignore[arg-type]


def test_missing_element_raises_stale_ref() -> None:
    snap = build_snapshot(tree_of(button("Gone")), snapshot_id="s_aaaa")
    with pytest.raises(AXPilotError) as info:
        live_handle(StubEngine(tree_of(button("Other"), prefix="m")), snap, snap.all_elements[0])  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.STALE_REF
    assert "Gone" in info.value.message


def test_closed_window_becomes_stale_ref_but_other_errors_propagate() -> None:
    snap = build_snapshot(tree_of(button("OK")), snapshot_id="s_aaaa")
    gone = StubEngine(None, error=AXPilotError(ErrorCode.WINDOW_NOT_FOUND, "no window 9"))
    with pytest.raises(AXPilotError) as info:
        live_handle(gone, snap, snap.all_elements[0])  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.STALE_REF
    denied = StubEngine(None, error=AXPilotError(ErrorCode.PERMISSION_ACCESSIBILITY, "denied"))
    with pytest.raises(AXPilotError) as info:
        live_handle(denied, snap, snap.all_elements[0])  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.PERMISSION_ACCESSIBILITY


def test_match_locator_breaks_identifier_ties_by_path() -> None:
    root = RawNode(role="AXWindow", children=[button("X", identifier="dup"), button("Y", identifier="dup")])
    locator = Locator(
        pid=1, window_id=None, path=(("AXButton", 1),), ax_role="AXButton", identifier="dup", name="Y"
    )
    match = match_locator(root, locator)
    assert match is not None
    assert match.title == "Y"


def test_match_locator_ignores_ambiguous_names() -> None:
    root = RawNode(role="AXWindow", children=[RawNode(role="AXGroup", children=[button("OK"), button("OK")])])
    locator = Locator(
        pid=1, window_id=None, path=(("AXButton", 0),), ax_role="AXButton", identifier=None, name="OK"
    )
    assert match_locator(root, locator) is None


def test_relaunched_app_in_the_fake_engine(engine: FakeEngine, snapshot_of: Callable[..., Snapshot]) -> None:
    snap = snapshot_of("TextEdit")
    body = next(el for el in snap.all_elements if el.role == "textarea")
    engine.app_control("quit", "TextEdit")
    assert not engine.element_valid(body.handle)
    engine.app_control("launch", "TextEdit")  # new pid, fresh element handles
    handle = live_handle(engine, snap, body)  # re-found through the bundle id and window id
    assert handle == "textedit.body#2"
    assert engine.element_valid(handle)


def test_window_closed_in_the_fake_engine_is_stale(
    engine: FakeEngine, snapshot_of: Callable[..., Snapshot]
) -> None:
    snap = snapshot_of("TextEdit")
    body = next(el for el in snap.all_elements if el.role == "textarea")
    engine.window_control(301, "close")
    with pytest.raises(AXPilotError) as info:
        live_handle(engine, snap, body)
    assert info.value.code is ErrorCode.STALE_REF


def test_match_locator_breaks_identifier_ties_by_name_when_the_path_moved() -> None:
    root = RawNode(
        role="AXWindow",
        children=[
            RawNode(role="AXGroup", children=[button("X", identifier="dup"), button("Y", identifier="dup")])
        ],
    )
    locator = Locator(
        pid=1, window_id=None, path=(("AXButton", 5),), ax_role="AXButton", identifier="dup", name="Y"
    )
    match = match_locator(root, locator)
    assert match is not None
    assert match.title == "Y"
