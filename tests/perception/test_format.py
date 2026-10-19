from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import (
    Snapshot,
    build_snapshot,
    format_element_line,
    format_snapshot,
    snapshot_payload,
)
from axpilot.perception.format import LINE_VALUE_MAX, format_point, format_rect, quote
from axpilot.perception.serialize import assign_handles

NodeFactory = Callable[..., RawNode]
TreeFactory = Callable[..., RawTree]

LONG_BODY = "Milk, eggs, bread, coffee beans, olive oil, butter, flour, sugar, apples, pears, lemons"


def n(
    role: str, *children: RawNode, frame: tuple[float, float, float, float] | None = None, **kw: object
) -> RawNode:
    return RawNode(role=role, frame=Rect(*frame) if frame else None, children=list(children), **kw)  # type: ignore[arg-type]


def notes_tree() -> RawTree:
    rows = [
        n(
            "AXRow",
            n("AXCell", n("AXStaticText", value=title), n("AXStaticText", value=date)),
            frame=(201, 101 + i * 64, 285, 64),
            selected=i == 0,
        )
        for i, (title, date) in enumerate([("Shopping list", "10:42"), ("Trip ideas", "Yesterday")])
    ]
    root = n(
        "AXWindow",
        n("AXButton", subrole="AXCloseButton", frame=(8, 31, 14, 16), actions=("AXPress",)),
        n(
            "AXToolbar",
            n("AXButton", description="New Note", frame=(1166, 38, 28, 28), actions=("AXPress",)),
            n(
                "AXTextField",
                subrole="AXSearchField",
                description="Search",
                value="",
                frame=(1250, 40, 100, 24),
            ),
            frame=(0, 25, 1440, 52),
        ),
        n(
            "AXScrollArea",
            n("AXList", *rows, description="Notes", frame=(201, 77, 285, 1000)),
            n("AXScrollBar", frame=(486, 77, 15, 823)),
            frame=(201, 77, 300, 823),
        ),
        n("AXTextArea", description="Note body", value=LONG_BODY, focused=True, frame=(502, 77, 938, 823)),
        frame=(0, 25, 1440, 875),
        title="Notes",
        actions=("AXRaise",),
    )
    titles = ["Apple", "Notes", "File", "Edit", "Format", "View", "Window", "Help"]
    bar = n("AXMenuBar", *[n("AXMenuBarItem", title=title) for title in titles])
    assign_handles(root, bar)
    return RawTree(
        app=AppInfo("Notes", 812, "com.apple.Notes"),
        window=WindowInfo(101, 812, "Notes", "Notes", Rect(0, 25, 1440, 875)),
        root=root,
        menu_bar=bar,
        node_count=318,
    )


GOLDEN = "\n".join(
    [
        'app Notes (com.apple.Notes, pid 812) · window "Notes" [0,25 1440×875] · snapshot s_7f3a',
        'focused: e6 textarea "Note body"',
        'e1  button   "New Note"                (1180,52)',
        'e2  search   "Search"                  value=""  (1300,52)',
        'e3  list     "Notes"                   2 rows, scrollable',
        'e4    row    "Shopping list — 10:42"   selected  (344,133)',
        'e5    row    "Trip ideas — Yesterday"  (344,197)',
        'e6  textarea "Note body"               '
        'value="Milk, eggs, bread, coffee beans, olive oil, butter, flour, sugar, apples, pears…"'
        "  focused  (971,488)",
        "menus: Notes · File · Edit · Format · View · Window · Help   (use select_menu)",
        "6 of 6 elements shown (interactive).",
    ]
)

GOLDEN_PAGE = """\
app Notes (com.apple.Notes, pid 812) · window "Notes" [0,25 1440×875] · snapshot s_7f3a
focused: e6 textarea "Note body"
e1  button "New Note"  (1180,52)
e2  search "Search"    value=""  (1300,52)
e3  list   "Notes"     2 rows, scrollable
menus: Notes · File · Edit · Format · View · Window · Help   (use select_menu)
3 of 6 elements shown (interactive). More: offset=3."""


def test_format_snapshot_golden() -> None:
    assert format_snapshot(build_snapshot(notes_tree(), snapshot_id="s_7f3a")) == GOLDEN


def test_format_snapshot_golden_first_page_with_paging_hint() -> None:
    snap = build_snapshot(notes_tree(), snapshot_id="s_7f3a", max_elements=3)
    assert format_snapshot(snap) == GOLDEN_PAGE


def test_later_page_mentions_its_offset() -> None:
    snap = build_snapshot(notes_tree(), snapshot_id="s_7f3a", max_elements=3, offset=3)
    text = format_snapshot(snap)
    assert text.splitlines()[-1] == "3 of 6 elements shown (interactive, offset 3)."
    assert "e4    row" in text or "e4  row" in text


def test_header_without_bundle_id_or_window(make_tree: TreeFactory) -> None:
    tree = make_tree(bundle_id=None)
    tree.window = None
    text = format_snapshot(build_snapshot(tree, snapshot_id="s_abcd"))
    assert text.splitlines()[0] == "app Demo (pid 100) · no window · snapshot s_abcd"


def test_empty_interactive_snapshot_suggests_alternatives(make_tree: TreeFactory) -> None:
    text = format_snapshot(build_snapshot(make_tree(), snapshot_id="s_abcd"))
    assert text.splitlines()[-1] == (
        "0 of 0 elements shown (interactive). "
        'Nothing actionable found; try mode="full", find, or a screenshot.'
    )


def test_truncated_capture_is_called_out(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(node("AXButton", title="A", frame=(0, 0, 10, 10)), truncated=True)
    footer = format_snapshot(build_snapshot(tree, snapshot_id="s_abcd")).splitlines()[-1]
    assert footer.startswith("1 of 1 elements shown (interactive).")
    assert "too large to capture completely" in footer


def test_indentation_is_relative_and_capped_at_six_levels(node: NodeFactory, make_tree: TreeFactory) -> None:
    leaf = node("AXButton", title="Leaf", frame=(0, 0, 10, 10))
    for level in range(9):
        leaf = node("AXGroup", leaf, description=f"G{level}", frame=(0, 0, 100, 100))
    snap = build_snapshot(make_tree(leaf), snapshot_id="s_abcd")
    lines = format_snapshot(snap).splitlines()[1:-1]
    ref_width = max(len(el.ref) for el in snap.elements) + 2
    indents = [
        line.index(el.role, len(el.ref)) - ref_width for el, line in zip(snap.elements, lines, strict=True)
    ]
    assert indents == [0, 2, 4, 6, 8, 10, 12, 12, 12, 12]
    # A page that starts deep in the tree is indented relative to its shallowest element.
    deep_page = build_snapshot(make_tree(leaf), snapshot_id="s_abcd", offset=8)
    assert format_snapshot(deep_page).splitlines()[1:3] == [
        'e9   group    "G0"',
        'e10    button "Leaf"  (5,5)',
    ]


def test_quote_escapes_and_truncates() -> None:
    assert quote('say "hi"\nnow') == '"say \\"hi\\"\\nnow"'
    assert quote("abcdef", 4) == '"abc…"'


def test_points_and_rects_are_rounded() -> None:
    assert format_point(Rect(0, 0, 3, 3).center) == "(2,2)"
    assert format_rect(Rect(-10.4, 25.6, 100.2, 50)) == "[-10,26 100×50]"


def test_text_values_are_cut_for_display_but_kept_in_json(node: NodeFactory, make_tree: TreeFactory) -> None:
    field = node("AXTextArea", description="Body", value="x" * 150, frame=(0, 0, 100, 100))
    snap = build_snapshot(make_tree(field), snapshot_id="s_abcd")
    line = format_element_line(snap.elements[0])
    assert f'value="{"x" * (LINE_VALUE_MAX - 1)}…"' in line
    assert snapshot_payload(snap)["elements"][0]["value"] == "x" * 150


def test_format_element_line_is_unpadded(node: NodeFactory, make_tree: TreeFactory) -> None:
    tree = make_tree(
        node(
            "AXCheckBox", title="Wrap", value=1, enabled=False, frame=(10, 10, 20, 20), actions=("AXPress",)
        ),
        node("AXButton", frame=(40, 10, 20, 20), actions=("AXPress",)),
    )
    checkbox, unnamed = build_snapshot(tree, snapshot_id="s_abcd").elements
    assert format_element_line(checkbox) == 'e1 checkbox "Wrap"  disabled, checked  (20,20)'
    assert format_element_line(unnamed) == "e2 button  (50,20)"


def test_snapshot_payload_shape_is_json_safe() -> None:
    snap = build_snapshot(notes_tree(), snapshot_id="s_7f3a", max_elements=4)
    payload = snapshot_payload(snap)
    assert set(payload) == {
        "snapshot_id",
        "app",
        "window",
        "mode",
        "focused_ref",
        "elements",
        "shown",
        "total",
        "offset",
        "next_offset",
        "truncated",
        "menus",
    }
    assert payload["app"] == {"name": "Notes", "bundle_id": "com.apple.Notes", "pid": 812}
    assert payload["window"] == {"window_id": 101, "title": "Notes", "frame": [0, 25, 1440, 875]}
    assert (payload["shown"], payload["total"], payload["offset"], payload["next_offset"]) == (4, 6, 0, 4)
    assert payload["focused_ref"] == "e6"
    row = payload["elements"][3]
    assert row == {
        "ref": "e4",
        "role": "row",
        "name": "Shopping list — 10:42",
        "value": None,
        "frame": [201, 101, 285, 64],
        "center": [343.5, 133.0],
        "states": ["selected"],
        "actions": [],
        "depth": 1,
        "parent_ref": "e3",
        "extra": {},
    }
    assert json.loads(json.dumps(payload)) == payload


def test_snapshot_payload_without_window(make_tree: TreeFactory) -> None:
    tree = make_tree()
    tree.window = None
    payload = snapshot_payload(build_snapshot(tree, snapshot_id="s_abcd"))
    assert payload["window"] is None
    assert payload["elements"] == []
    assert payload["next_offset"] is None


@pytest.mark.parametrize("app", ["Notes", "Finder", "TextEdit", "Login Demo", "System Settings", "Terminal"])
def test_demo_snapshots_render_and_stay_compact(snapshot_of: Callable[..., Snapshot], app: str) -> None:
    snap = snapshot_of(app)
    text = format_snapshot(snap)
    lines = text.splitlines()
    assert lines[0].startswith(f"app {app} (")
    assert len(lines) == len(snap.elements) + 2 + (1 if snap.focused_ref else 0) + (1 if snap.menus else 0)
    assert len(text) < 6000  # roughly < 2k tokens for a typical window
    json.dumps(snapshot_payload(snap))


def test_demo_secure_field_shows_no_value(snapshot_of: Callable[..., Snapshot]) -> None:
    text = format_snapshot(snapshot_of("Login Demo"))
    line = next(line for line in text.splitlines() if " secure " in line)
    assert "value=" not in line
    assert "correct horse" not in text
