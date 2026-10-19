"""Compare two snapshots of the same app ("what did my action change?").

Elements of the two snapshots are matched in passes, strongest evidence first; each
element is matched at most once:

1. same ``AXIdentifier`` (and AX role);
2. same locator path *and* same name;
3. same AX role, name and frame (rounded to whole points);
4. same AX role and name, when that pair is unique among the unmatched elements on both sides
   (handles elements that moved because something was inserted above them);
5. same locator path alone, when the frames overlap (a label whose text changed, or a
   Play button that became Pause).

Unmatched elements of ``after`` are *added*, unmatched elements of ``before`` are *removed*,
and matched pairs whose name, value or state differ are *changed*. When the app or window
differs between the snapshots nothing is matched: every element of ``after`` is new to the
agent, and pairing look-alike controls of two different windows would only mislead it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Hashable
from typing import Any

from axpilot.perception.model import Element, Snapshot, SnapshotDiff

_COMPARED_FIELDS: tuple[str, ...] = ("name", "value", "enabled", "selected", "focused", "expanded")

KeyFn = Callable[[Element], Hashable | None]


def element_changes(before: Element, after: Element) -> dict[str, tuple[Any, Any]]:
    """Fields (name, value and states) that differ, as ``{field: (before, after)}`` in a fixed order."""
    changes: dict[str, tuple[Any, Any]] = {}
    for field in _COMPARED_FIELDS:
        old, new = getattr(before, field), getattr(after, field)
        if old != new:
            changes[field] = (old, new)
    return changes


def _rounded_frame(el: Element) -> tuple[int, int, int, int] | None:
    if el.frame is None:
        return None
    return (round(el.frame.x), round(el.frame.y), round(el.frame.w), round(el.frame.h))


def _by_identifier(el: Element) -> Hashable | None:
    return (el.ax_role, el.identifier) if el.identifier else None


def _by_path_and_name(el: Element) -> Hashable | None:
    return (el.locator.window_id, el.locator.path, el.name)


def _by_role_name_frame(el: Element) -> Hashable | None:
    frame = _rounded_frame(el)
    return (el.ax_role, el.name, frame) if frame is not None else None


def _by_role_and_name(el: Element) -> Hashable | None:
    return (el.ax_role, el.name) if el.name else None


def _by_path(el: Element) -> Hashable | None:
    return (el.locator.window_id, el.locator.path)


def _frames_overlap(before: Element, after: Element) -> bool:
    if before.frame is None or after.frame is None:
        return True
    return before.frame.intersects(after.frame)


class _Matcher:
    """Greedy multi-pass matching of ``after`` elements onto ``before`` elements."""

    def __init__(self, before: list[Element], after: list[Element]) -> None:
        self.before = before
        self.after = after
        self.pairs: dict[int, int] = {}  # after index -> before index
        self._taken: set[int] = set()  # matched before indices

    def run(self) -> dict[int, int]:
        self._pass(_by_identifier)
        self._pass(_by_path_and_name)
        self._pass(_by_role_name_frame)
        self._pass(_by_role_and_name, unique=True)
        self._pass(_by_path, accept=_frames_overlap)
        return self.pairs

    def _pass(
        self,
        key_fn: KeyFn,
        *,
        unique: bool = False,
        accept: Callable[[Element, Element], bool] | None = None,
    ) -> None:
        free_before = [i for i in range(len(self.before)) if i not in self._taken]
        free_after = [i for i in range(len(self.after)) if i not in self.pairs]
        index: dict[Hashable, list[int]] = {}
        for i in free_before:
            key = key_fn(self.before[i])
            if key is not None:
                index.setdefault(key, []).append(i)
        after_counts: Counter[Hashable] = Counter()
        if unique:
            after_counts.update(k for k in (key_fn(self.after[i]) for i in free_after) if k is not None)
        for i in free_after:
            key = key_fn(self.after[i])
            candidates = index.get(key) if key is not None else None
            if not candidates:
                continue
            if unique and (len(candidates) != 1 or after_counts[key] != 1):
                continue
            if accept is not None and not accept(self.before[candidates[0]], self.after[i]):
                continue
            j = candidates.pop(0)
            self.pairs[i] = j
            self._taken.add(j)


def _focus_label(el: Element | None) -> str | None:
    if el is None:
        return None
    return el.name or el.role


def diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDiff:
    """Compute what changed from ``before`` to ``after`` (see the module docstring)."""
    window_before = before.window.window_id if before.window else None
    window_after = after.window.window_id if after.window else None
    app_changed = (before.app.pid, before.app.bundle_id) != (after.app.pid, after.app.bundle_id)
    window_changed = window_before != window_after
    same_view = not (app_changed or window_changed)
    pairs = _Matcher(before.all_elements, after.all_elements).run() if same_view else {}
    matched_before = set(pairs.values())
    added = [el for i, el in enumerate(after.all_elements) if i not in pairs]
    removed = [el for j, el in enumerate(before.all_elements) if j not in matched_before]
    changed = [
        (before.all_elements[pairs[i]], after.all_elements[i])
        for i in sorted(pairs)
        if element_changes(before.all_elements[pairs[i]], after.all_elements[i])
    ]
    focus_before, focus_after = before.focused_element, after.focused_element
    return SnapshotDiff(
        before_id=before.snapshot_id,
        after_id=after.snapshot_id,
        app_changed=app_changed,
        window_changed=window_changed,
        focus_before=_focus_label(focus_before),
        focus_after=_focus_label(focus_after),
        added=added,
        removed=removed,
        changed=changed,
        app_before=before.app.name,
        app_after=after.app.name,
        window_before=before.window.title if before.window else None,
        window_after=after.window.title if after.window else None,
        focus_after_ref=focus_after.ref if focus_after else None,
    )
