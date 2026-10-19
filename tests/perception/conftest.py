"""Shared fixtures for perception tests: tiny tree builders and the demo desktop."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import Snapshot, SnapshotStore, build_snapshot
from axpilot.perception.serialize import assign_handles

NodeFactory = Callable[..., RawNode]
TreeFactory = Callable[..., RawTree]


def _node(
    role: str, *children: RawNode, frame: tuple[float, float, float, float] | None = None, **kw: Any
) -> RawNode:
    return RawNode(role=role, frame=Rect(*frame) if frame else None, children=list(children), **kw)


def _tree(
    *children: RawNode,
    window_frame: tuple[float, float, float, float] = (0, 0, 800, 600),
    menu_bar: RawNode | None = None,
    app_name: str = "Demo",
    bundle_id: str | None = "com.example.demo",
    pid: int = 100,
    window_id: int = 7,
    title: str = "Doc",
    **tree_kw: Any,
) -> RawTree:
    root = _node("AXWindow", *children, frame=window_frame, title=title, actions=("AXRaise",))
    assign_handles(root, menu_bar)
    app = AppInfo(name=app_name, pid=pid, bundle_id=bundle_id)
    window = WindowInfo(window_id=window_id, pid=pid, app=app_name, title=title, frame=Rect(*window_frame))
    return RawTree(app=app, window=window, root=root, menu_bar=menu_bar, **tree_kw)


@pytest.fixture
def node() -> NodeFactory:
    """``node("AXButton", *children, frame=(x, y, w, h), title=...)`` → RawNode."""
    return _node


@pytest.fixture
def make_tree() -> TreeFactory:
    """``make_tree(*children, window_frame=..., menu_bar=...)`` → RawTree with handles."""
    return _tree


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine.demo()


@pytest.fixture
def store() -> SnapshotStore:
    return SnapshotStore()


@pytest.fixture
def snapshot_of(engine: FakeEngine, store: SnapshotStore) -> Callable[..., Snapshot]:
    """Capture the demo desktop and store a snapshot: ``snapshot_of("Notes", mode="full")``."""

    def take(app: str | None = None, *, window_id: int | None = None, **kwargs: Any) -> Snapshot:
        tree = engine.capture_tree(app=app, window_id=window_id, include_menus=True)
        snap = build_snapshot(tree, snapshot_id=store.new_id(), **kwargs)
        store.put(snap)
        return snap

    return take
