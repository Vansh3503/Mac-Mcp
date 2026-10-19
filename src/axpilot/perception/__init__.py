"""Perception: snapshots with element refs, diffs, search and serialization (pure Python).

Typical flow::

    tree = engine.capture_tree(app="Notes", include_menus=True)
    snap = build_snapshot(tree, snapshot_id=store.new_id())
    store.put(snap)
    text = format_snapshot(snap)          # for the model
    data = snapshot_payload(snap)         # for structuredContent

App compatibility (``appkind``), the OCR fallback (``vision``) and scrolling helpers
(``navigate``)::

    profile = app_profile(tree.app, tree)                 # kind, ax_quality, strategies, notes
    if profile.ax_quality == "poor":
        lines = engine.ocr(engine.screenshot(window_id=tree.window.window_id))
        snap = merge_vision(snap, ocr_elements(lines))    # adds o1, o2, … pseudo-elements
    container = scroll_container_for(snap, None)
    rows = collect_rows(snap, container)
"""

from __future__ import annotations

from axpilot.perception.appkind import (
    APP_KINDS,
    STRATEGIES,
    AppKind,
    AppProfile,
    AxMetrics,
    AxQuality,
    BundleFS,
    LocalBundleFS,
    MemoryBundleFS,
    app_profile,
    assess_ax_quality,
    detect_app_kind,
    measure_ax,
)
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
from axpilot.perception.navigate import (
    collect_rows,
    merge_pages,
    overlap_length,
    page_signature,
    scroll_container_for,
    viewport_of,
)
from axpilot.perception.resolve import live_handle, match_locator
from axpilot.perception.roles import INTERACTIVE_ROLES, is_interactive, short_role
from axpilot.perception.serialize import tree_from_dict, tree_to_dict
from axpilot.perception.snapshot import build_snapshot, node_name
from axpilot.perception.store import CaptureStore, SnapshotStore, parse_ref
from axpilot.perception.vision import is_vision_element, merge_vision, ocr_elements

__all__ = [
    "APP_KINDS",
    "INTERACTIVE_ROLES",
    "STRATEGIES",
    "AppKind",
    "AppProfile",
    "AxMetrics",
    "AxQuality",
    "BundleFS",
    "CaptureStore",
    "Element",
    "LocalBundleFS",
    "Locator",
    "MemoryBundleFS",
    "Snapshot",
    "SnapshotDiff",
    "SnapshotMode",
    "SnapshotStore",
    "app_profile",
    "assess_ax_quality",
    "build_snapshot",
    "collect_rows",
    "detect_app_kind",
    "diff_payload",
    "diff_snapshots",
    "element_changes",
    "element_payload",
    "find_elements",
    "format_diff",
    "format_element_line",
    "format_snapshot",
    "is_interactive",
    "is_vision_element",
    "live_handle",
    "match_locator",
    "measure_ax",
    "merge_pages",
    "merge_vision",
    "node_name",
    "ocr_elements",
    "overlap_length",
    "page_signature",
    "parse_ref",
    "scroll_container_for",
    "short_role",
    "snapshot_payload",
    "tree_from_dict",
    "tree_to_dict",
    "viewport_of",
]
