"""Snapshot invariants over random accessibility trees (QUALITY.md §B3): refs are unique and dense,
every page is a slice of ``all_elements``, paging covers every element exactly once, and a secure
field's value never appears in any output."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import build_snapshot, format_snapshot, snapshot_payload
from axpilot.perception.serialize import assign_handles

ROLES = (
    "AXButton",
    "AXTextField",
    "AXTextArea",
    "AXCheckBox",
    "AXPopUpButton",
    "AXLink",
    "AXStaticText",
    "AXGroup",
    "AXList",
    "AXRow",
    "AXScrollArea",
    "AXImage",
)
SECRET = "SECRET"
"""Secure values contain this; no other generated text can (names use lower-case letters only)."""

names = st.none() | st.text(alphabet="abcdefghij ", min_size=1, max_size=12)
frames = st.builds(
    Rect,
    st.integers(-50, 820),
    st.integers(-50, 620),
    st.integers(0, 300),
    st.integers(0, 200),
)


@st.composite
def leaves(draw: st.DrawFn) -> RawNode:
    secure = draw(st.booleans()) and draw(st.booleans())
    role = "AXTextField" if secure else draw(st.sampled_from(ROLES))
    return RawNode(
        role=role,
        subrole="AXSecureTextField" if secure and draw(st.booleans()) else None,
        title=draw(names),
        description=draw(names),
        value=f"{SECRET}{draw(st.integers(0, 999))}" if secure else draw(names),
        secure=secure,
        enabled=draw(st.booleans()),
        visible=draw(st.integers(0, 9)) > 0,
        frame=draw(frames),
        actions=("AXPress",) if role in ("AXButton", "AXCheckBox", "AXLink") else (),
    )


def _with_children(children: st.SearchStrategy[list[RawNode]]) -> st.SearchStrategy[RawNode]:
    return st.builds(
        lambda role, title, frame, kids: RawNode(role=role, title=title, frame=frame, children=kids),
        st.sampled_from(("AXGroup", "AXList", "AXScrollArea", "AXRow", "AXSplitGroup", "AXToolbar")),
        names,
        frames,
        children,
    )


nodes = st.recursive(leaves(), lambda children: _with_children(st.lists(children, max_size=5)), max_leaves=40)


def make_tree(children: list[RawNode]) -> RawTree:
    root = RawNode(role="AXWindow", title="Doc", frame=Rect(0, 0, 800, 600), children=children)
    assign_handles(root, None)
    app = AppInfo(name="Demo", pid=100, bundle_id="com.example.demo")
    window = WindowInfo(window_id=7, pid=100, app="Demo", title="Doc", frame=Rect(0, 0, 800, 600))
    return RawTree(app=app, window=window, root=root)


@settings(max_examples=150, deadline=None)
@given(
    st.lists(nodes, max_size=6),
    st.sampled_from(("interactive", "full")),
    st.integers(1, 12),
)
def test_snapshot_invariants(children: list[RawNode], mode: str, page_size: int) -> None:
    tree = make_tree(children)
    first = build_snapshot(tree, snapshot_id="s_1", mode=mode, max_elements=page_size)  # type: ignore[arg-type]
    refs = [el.ref for el in first.all_elements]
    assert refs == [f"e{i}" for i in range(1, len(refs) + 1)]  # unique and dense, in pre-order

    seen: list[str] = []
    offset = 0
    while True:
        page = build_snapshot(tree, snapshot_id="s_1", mode=mode, max_elements=page_size, offset=offset)  # type: ignore[arg-type]
        assert [el.ref for el in page.all_elements] == refs  # paging never renumbers
        assert page.elements == page.all_elements[offset : offset + page_size]
        seen += [el.ref for el in page.elements]
        for output in (format_snapshot(page), json.dumps(snapshot_payload(page), default=str)):
            assert SECRET not in output
        if not page.elements:
            break
        offset += page_size
    assert seen == refs  # every element exactly once

    for el in first.all_elements:
        if el.secure:
            assert el.value is None
