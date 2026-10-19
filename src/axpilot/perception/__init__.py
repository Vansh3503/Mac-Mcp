"""Perception: snapshots with element refs, diffs, search and serialization (pure Python).

Typical flow::

    tree = engine.capture_tree(app="Notes", include_menus=True)
    snap = build_snapshot(tree, snapshot_id=store.new_id())
    store.put(snap)
    text = format_snapshot(snap)          # for the model
    data = snapshot_payload(snap)         # for structuredContent
"""

from __future__ import annotations

from axpilot.perception.diff import diff_snapshots, element_changes
from axpilot.perception.find import find_elements
from axpilot.perception.format import (
    diff_payload,
    element_payload,
    format_diff,
    format_element_line,
    format_snapshot,
    snapshot_payload,
)
from axpilot.perception.model import Element, Locator, Snapshot, SnapshotDiff, SnapshotMode
from axpilot.perception.resolve import live_handle, match_locator
from axpilot.perception.roles import INTERACTIVE_ROLES, is_interactive, short_role
from axpilot.perception.serialize import tree_from_dict, tree_to_dict
from axpilot.perception.snapshot import build_snapshot, node_name
from axpilot.perception.store import CaptureStore, SnapshotStore, parse_ref

__all__ = [
    "INTERACTIVE_ROLES",
    "CaptureStore",
    "Element",
    "Locator",
    "Snapshot",
    "SnapshotDiff",
    "SnapshotMode",
    "SnapshotStore",
    "build_snapshot",
    "diff_payload",
    "diff_snapshots",
    "element_changes",
    "element_payload",
    "find_elements",
    "format_diff",
    "format_element_line",
    "format_snapshot",
    "is_interactive",
    "live_handle",
    "match_locator",
    "node_name",
    "parse_ref",
    "short_role",
    "snapshot_payload",
    "tree_from_dict",
    "tree_to_dict",
]
