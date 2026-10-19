"""Property: over any sequence of UI changes and snapshots, one ref never names two elements.

The simulated desktop below has look-alike controls in two apps, a panel window that closes and
re-opens (fresh engine handles each time), elements that hide and show, a static text whose
content changes, nested unnamed groups, and a virtualized list whose rows are re-created as it
scrolls. Element identity is the fixture id (an engine handle without its ``#<generation>``
suffix) or, for generated list rows, the row's text plus the path inside the row.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Hashable, Iterator
from typing import Any, TypeVar

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Point, RawNode, RawTree
from axpilot.perception import Snapshot
from axpilot.testing import make_test_context


def _button(ident: str, title: str, x: float, y: float, **extra: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "role": "AXButton",
        "title": title,
        "frame": [x, y, 70, 24],
        "actions": ["AXPress"],
        **extra,
    }


DESKTOP: dict[str, Any] = {
    "displays": [{"display_id": 1, "frame": [0, 0, 1440, 900], "main": True}],
    "apps": [
        {"name": "Alpha", "bundle_id": "com.example.alpha", "pid": 100, "frontmost": True},
        {"name": "Beta", "bundle_id": "com.example.beta", "pid": 200},
    ],
    "windows": [
        {
            "window_id": 10,
            "app": "com.example.alpha",
            "title": "Main",
            "frame": [0, 25, 1000, 800],
            "tree": {
                "id": "a.root",
                "role": "AXWindow",
                "children": [
                    {
                        "id": "a.toolbar",
                        "role": "AXToolbar",
                        "title": "Tools",
                        "frame": [0, 25, 1000, 40],
                        "children": [
                            _button("a.add", "Add", 10, 30, on_press={"show": "a.extra"}),
                            _button("a.remove", "Remove", 90, 30, on_press={"hide": "a.extra"}),
                            _button("a.open", "Open Panel", 170, 30, on_press={"open_window": 11}),
                            _button(
                                "a.busy",
                                "Busy",
                                250,
                                30,
                                on_press={"set_value": {"id": "a.status", "value": "Status: busy"}},
                            ),
                            _button(
                                "a.idle",
                                "Idle",
                                330,
                                30,
                                on_press={"set_value": {"id": "a.status", "value": "Status: idle"}},
                            ),
                        ],
                    },
                    {
                        "id": "a.status",
                        "role": "AXStaticText",
                        "value": "Status: idle",
                        "frame": [10, 80, 200, 18],
                    },
                    {
                        "id": "a.field",
                        "role": "AXTextField",
                        "description": "Query",
                        "value": "",
                        "frame": [10, 110, 300, 24],
                    },
                    _button("a.extra", "Extra", 10, 150, visible=False),
                    {
                        "id": "a.outer",
                        "role": "AXGroup",
                        "frame": [400, 100, 300, 120],
                        "children": [
                            {
                                "id": "a.inner",
                                "role": "AXGroup",
                                "frame": [400, 100, 300, 120],
                                "children": [
                                    {
                                        "id": "a.wrap",
                                        "role": "AXCheckBox",
                                        "title": "Wrap",
                                        "value": 0,
                                        "frame": [410, 110, 80, 20],
                                        "actions": ["AXPress"],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "id": "a.scroll",
                        "role": "AXScrollArea",
                        "frame": [0, 300, 600, 300],
                        "content_rows": {
                            "rows": [[f"Row {i}", f"detail {i}"] for i in range(60)],
                            "row_height": 30,
                            "points_per_line": 10,
                        },
                        "children": [
                            {
                                "id": "a.table",
                                "role": "AXTable",
                                "title": "Items",
                                "frame": [0, 300, 600, 300],
                            }
                        ],
                    },
                ],
            },
        },
        {
            "window_id": 11,
            "app": "com.example.alpha",
            "title": "Panel",
            "frame": [700, 100, 300, 200],
            "open": False,
            "tree": {
                "id": "p.root",
                "role": "AXWindow",
                "children": [
                    _button("p.apply", "Apply", 710, 120, on_press={"close_window": True}),
                    {
                        "id": "p.label",
                        "role": "AXStaticText",
                        "value": "Panel text",
                        "frame": [710, 160, 200, 18],
                    },
                    {
                        "id": "p.field",
                        "role": "AXTextField",
                        "description": "Query",
                        "value": "",
                        "frame": [710, 190, 200, 24],
                    },
                ],
            },
        },
        {
            "window_id": 20,
            "app": "com.example.beta",
            "title": "Beta",
            "frame": [0, 25, 1000, 800],
            "tree": {
                "id": "b.root",
                "role": "AXWindow",
                "children": [
                    _button("b.add", "Add", 10, 30),  # a look-alike of a.add in another app
                    {
                        "id": "b.field",
                        "role": "AXTextField",
                        "description": "Query",
                        "value": "",
                        "frame": [10, 110, 300, 24],
                    },
                ],
            },
        },
    ],
}

T = TypeVar("T")

_GENERATION = re.compile(r"#\d+$")
PRESSABLE = ("a.add", "a.remove", "a.open", "a.busy", "a.idle", "p.apply")


def _texts(node: RawNode) -> str:
    return " | ".join(str(n.value) for n in node.walk() if n.role == "AXStaticText")


def identities(tree: RawTree) -> dict[Any, Hashable]:
    """Engine handle → element identity for every node of a captured tree."""
    found: dict[Any, Hashable] = {}

    def visit(node: RawNode, row: tuple[str, tuple[int, ...]] | None, path: tuple[int, ...]) -> None:
        handle = node.handle
        if isinstance(handle, str) and not handle.startswith("n"):
            found[handle] = _GENERATION.sub("", handle)
        elif node.role == "AXRow":
            row = (_texts(node), ())
            found[handle] = ("row", row[0], ())
        elif row is not None:
            found[handle] = ("row", row[0], path)
        else:
            found[handle] = ("unknown", handle)  # no such nodes in this fixture
        for index, child in enumerate(node.children):
            visit(child, row, (*path, index) if row is not None else ())

    visit(tree.root, None, ())
    return found


def check_snapshot(snap: Snapshot, session: str) -> None:
    refs = [el.ref for el in snap.all_elements]
    assert len(refs) == len(set(refs)), "refs are unique within a snapshot"
    assert all(ref.startswith("e") for ref in refs)
    assert snap.ref_session == session
    known = set(refs)
    assert all(el.parent_ref is None or el.parent_ref in known for el in snap.all_elements)
    assert snap.focused_ref is None or snap.focused_ref in known
    start = snap.offset
    assert snap.elements == snap.all_elements[start : start + len(snap.elements)]
    assert all(snap.element(ref) is el for ref, el in zip(refs, snap.all_elements, strict=True))


snapshots = st.tuples(
    st.just("snapshot"),
    st.sampled_from([("Alpha", None), ("Alpha", 11), ("Beta", None)]),
    st.sampled_from(["interactive", "full", "text"]),
    st.booleans(),  # store it?
    st.integers(min_value=-1, max_value=4),  # previous: -1 = default, else an earlier snapshot (any app)
)
actions = st.lists(
    st.one_of(
        snapshots,
        st.tuples(st.just("press"), st.sampled_from(PRESSABLE)),
        snapshots,
        st.tuples(st.just("scroll"), st.integers(min_value=-40, max_value=40)),
        st.tuples(st.just("type"), st.sampled_from(["", "a", "query", "other text"])),
    ),
    min_size=1,
    max_size=25,
)


def attempt(fn: Callable[[], T]) -> tuple[T | None, ErrorCode | None]:
    """``(result, None)``, or ``(None, code)`` when ``fn`` raises an :class:`AXPilotError`."""
    try:
        return fn(), None
    except AXPilotError as err:
        return None, err.code


def _press(engine: FakeEngine, ident: str) -> None:
    _, code = attempt(lambda: engine.perform_action(engine.resolve_ref(ident), "AXPress"))
    # its window is closed or it is hidden: nothing to press
    assert code in (None, ErrorCode.STALE_REF, ErrorCode.ELEMENT_NOT_ACTIONABLE)


def _type(engine: FakeEngine, text: str) -> None:
    engine.set_value(engine.resolve_ref("a.field"), text)


def run(steps: list[tuple[Any, ...]]) -> Iterator[tuple[Snapshot, dict[Any, Hashable]]]:
    engine = FakeEngine.from_dict(DESKTOP)
    ctx = make_test_context(engine=engine)
    taken: list[Snapshot] = []
    for step in steps:
        if step[0] == "press":
            _press(engine, step[1])
        elif step[0] == "scroll":
            engine.scroll(Point(300, 450), 0, step[1])
        elif step[0] == "type":
            _type(engine, step[1])
        else:
            _, (app, window_id), mode, store, previous_index = step
            tree, code = attempt(functools.partial(engine.capture_tree, app=app, window_id=window_id))
            if tree is None:
                assert code is ErrorCode.WINDOW_NOT_FOUND  # the panel is not open
                continue
            previous = taken[previous_index] if 0 <= previous_index < len(taken) else None
            snap = ctx.build_snapshot(tree, mode=mode, store=store, previous=previous)
            check_snapshot(snap, ctx.snapshots.refs.session)
            taken.append(snap)
            yield snap, identities(tree)
            for ref in {el.ref for s in taken for el in s.all_elements}:
                found, code = attempt(functools.partial(ctx.snapshots.resolve, ref))
                if found is None:
                    stored_any = len(ctx.snapshots) > 0
                    assert code is (ErrorCode.STALE_REF if stored_any else ErrorCode.SNAPSHOT_NOT_FOUND)
                    continue
                holder, el = found
                assert el.ref == ref
                assert holder.element(ref) is el


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(actions)
def test_one_ref_never_names_two_different_elements(steps: list[tuple[Any, ...]]) -> None:
    named: dict[str, Hashable] = {}
    for snap, identity in run(steps):
        for el in snap.all_elements:
            who = identity[el.handle]
            assert named.setdefault(el.ref, who) == who, f"{el.ref} named {named[el.ref]} and then {who}"


def test_scenario_with_every_kind_of_change() -> None:
    """A fixed walk through the same desktop (a readable example of what the property covers)."""
    steps: list[tuple[Any, ...]] = [
        ("snapshot", ("Alpha", None), "interactive", True, -1),
        ("snapshot", ("Beta", None), "interactive", True, -1),
        ("press", "a.add"),
        ("press", "a.busy"),
        ("snapshot", ("Alpha", None), "full", True, -1),
        ("press", "a.open"),
        ("snapshot", ("Alpha", 11), "interactive", True, -1),
        ("press", "p.apply"),
        ("press", "a.open"),
        ("snapshot", ("Alpha", 11), "interactive", True, -1),
        ("scroll", 40),
        ("snapshot", ("Alpha", None), "interactive", False, 0),
        ("scroll", -40),
        ("press", "a.remove"),
        ("snapshot", ("Alpha", None), "text", True, -1),
        ("snapshot", ("Alpha", None), "interactive", True, -1),
    ]
    named: dict[str, Hashable] = {}
    refs_of_add: set[str] = set()
    panel_apply: set[str] = set()
    for snap, identity in run(steps):
        for el in snap.all_elements:
            who = identity[el.handle]
            assert named.setdefault(el.ref, who) == who
            if who == "a.add":
                refs_of_add.add(el.ref)
            if who == "p.apply":
                panel_apply.add(el.ref)
    assert len(refs_of_add) == 1  # carried through every snapshot of the main window
    assert len(panel_apply) == 1  # the re-opened panel's button kept its ref (path and name)
    assert "b.add" in named.values()
