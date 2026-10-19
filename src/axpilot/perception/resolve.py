"""Turn a snapshot element back into a live engine handle, re-finding it if needed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import RawNode
from axpilot.perception.model import Element, Locator, Snapshot
from axpilot.perception.snapshot import iter_with_paths, node_at_path, node_name

if TYPE_CHECKING:
    from axpilot.engine.base import Engine

_GONE_CODES = frozenset({ErrorCode.APP_NOT_FOUND, ErrorCode.WINDOW_NOT_FOUND, ErrorCode.STALE_REF})


def _stale(el: Element, snap: Snapshot, reason: str) -> AXPilotError:
    label = f'{el.role} "{el.name}"' if el.name else el.role
    return AXPilotError(
        ErrorCode.STALE_REF,
        f"{el.ref} ({label}) from snapshot {snap.snapshot_id} is gone: {reason}.",
    )


def match_locator(root: RawNode, locator: Locator) -> RawNode | None:
    """Find the node ``locator`` describes in a freshly captured tree.

    Tries, in order: the ``AXIdentifier`` (ties broken by path, then name); the path, if the
    node there has the same role and the same name (or the element never had a name); a
    unique node with the same AX role and name. A named element whose name changed is *not*
    matched by position alone: acting on whatever now sits there could hit the wrong control.
    """
    indexed = list(iter_with_paths(root))
    if locator.identifier:
        same_id = [
            (n, p) for n, p in indexed if n.identifier == locator.identifier and n.role == locator.ax_role
        ]
        if len(same_id) == 1:
            return same_id[0][0]
        for node, path in same_id:
            if path == locator.path:
                return node
        named = [n for n, _ in same_id if node_name(n) == locator.name]
        if named:
            return named[0]
    at_path = node_at_path(root, locator.path)
    if at_path is not None and at_path.role == locator.ax_role and node_name(at_path) == locator.name:
        return at_path
    if locator.name:
        same_name = [n for n, _ in indexed if n.role == locator.ax_role and node_name(n) == locator.name]
        if len(same_name) == 1:
            return same_name[0]
    return None


def live_handle(engine: Engine, snap: Snapshot, el: Element) -> Any:
    """``el.handle`` if the engine says it is still valid; otherwise re-capture the snapshot's
    app/window and re-find the element by its locator. Raises ``STALE_REF`` if it is gone."""
    if el.handle is not None and engine.element_valid(el.handle):
        return el.handle
    app = snap.app.bundle_id or str(snap.app.pid)
    window_id = snap.window.window_id if snap.window else None
    try:
        tree = engine.capture_tree(app=app, window_id=window_id)
    except AXPilotError as exc:
        if exc.code in _GONE_CODES:
            raise _stale(el, snap, "its app or window is no longer open") from exc
        raise
    node = match_locator(tree.root, el.locator)
    if node is None or node.handle is None:
        raise _stale(el, snap, "it is no longer in the window")
    return node.handle
