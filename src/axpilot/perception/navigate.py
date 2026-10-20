"""Navigation helpers: getting around an app by scrolling (PLAN §6.9).

The ``explore`` / ``find(scroll=True)`` / ``scroll`` / ``read`` tools loop "snapshot → scroll →
snapshot" and need to answer three questions, which this module answers from snapshots alone:

* *What scrolls?* :func:`scroll_container_for` finds the scroll area, list, table, outline,
  web area or text area around an element (or the largest one in the window).
* *Did it move?* :func:`page_signature` hashes what is visible inside the container. An
  unchanged signature after a scroll means the end was reached (or the area does not scroll).
* *What is in it?* :func:`collect_rows` lists the rows (or text lines) inside the container
  (:func:`collect_items` with the ref of each line),
  and :func:`merge_pages` stitches successive pages together without repeating the overlap.

Elements belong to a container through their ``parent_ref`` chain. OCR pseudo-elements have
no parent, so they belong to a container when their center lies inside its frame.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from axpilot.models import Rect
from axpilot.perception.model import Element, Snapshot

CONTAINER_ROLES: frozenset[str] = frozenset(
    {"scrollarea", "list", "table", "outline", "webarea", "textarea", "browser", "grid"}
)
"""Short roles that can scroll their content."""
COLLECTION_ROLES: frozenset[str] = frozenset({"list", "table", "outline", "browser", "grid"})
"""Containers whose content is rows."""
INPUT_ROLES: frozenset[str] = frozenset({"field", "search", "textarea", "combobox"})
"""Roles whose text is their value (their name is only a label)."""

ROW_VISIBLE_MIN = 0.5
"""A row counts as visible when at least this share of its height lies inside the viewport."""
SIGNATURE_GRID = 2.0
"""Positions in :func:`page_signature` are rounded to this many points (jitter-tolerant)."""
SIGNATURE_LENGTH = 16
CELL_SEPARATOR = " | "
PART_SEPARATOR = " — "


# --------------------------------------------------------------------------- structure
class _Index:
    """Parent/child links of one snapshot, built once per helper call."""

    def __init__(self, snap: Snapshot) -> None:
        self.snap = snap
        self.by_ref: dict[str, Element] = {}
        self.children: dict[str, list[Element]] = {}
        for el in snap.all_elements:
            self.by_ref[el.ref] = el
            if el.parent_ref is not None:
                self.children.setdefault(el.parent_ref, []).append(el)

    def ancestors(self, el: Element) -> list[Element]:
        chain: list[Element] = []
        seen = {el.ref}
        parent = el.parent_ref
        while parent is not None and parent not in seen:
            seen.add(parent)
            found = self.by_ref.get(parent)
            if found is None:
                break
            chain.append(found)
            parent = found.parent_ref
        return chain

    def descendants(self, container: Element | None, *, include_ocr: bool = True) -> list[Element]:
        if container is None:
            return list(self.snap.all_elements)
        found: list[Element] = []
        seen = {container.ref}
        stack = list(reversed(self.children.get(container.ref, [])))
        while stack:
            el = stack.pop()
            if el.ref in seen:
                continue
            seen.add(el.ref)
            found.append(el)
            stack.extend(reversed(self.children.get(el.ref, [])))
        if include_ocr:
            found.extend(
                el
                for el in self.snap.all_elements
                if el.parent_ref is None and el.role == "ocrtext" and _contains_center(container, el)
            )
        return found


def _contains_center(container: Element, el: Element) -> bool:
    center = el.center
    return container.frame is not None and center is not None and container.frame.contains(center)


def ancestors(snap: Snapshot, el: Element) -> list[Element]:
    """``el``'s ancestors in the snapshot, nearest first (cycles and missing refs end the walk)."""
    return _Index(snap).ancestors(el)


def descendants(snap: Snapshot, container: Element | None) -> list[Element]:
    """Elements inside ``container`` in document order (every element for ``None``).

    Uses ``parent_ref`` links; parentless OCR elements count when their center lies inside the
    container's frame (they follow the AX descendants)."""
    return _Index(snap).descendants(container)


def _area(el: Element) -> float:
    return el.frame.area if el.frame is not None else 0.0


def _largest_container(snap: Snapshot) -> Element | None:
    candidates = [el for el in snap.all_elements if el.role in CONTAINER_ROLES]
    scrollable = [el for el in candidates if el.extra.get("scrollable")]
    pool = scrollable or candidates
    best: Element | None = None
    for el in pool:  # first one wins ties: document order
        if best is None or _area(el) > _area(best):
            best = el
    return best


def scroll_container_for(snap: Snapshot, el: Element | None) -> Element | None:
    """The element to scroll to move ``el``'s surroundings, or ``None``.

    * ``el`` itself if it is a container (scroll area, list, table, outline, web area, text
      area), else its nearest container ancestor;
    * without such an ancestor (OCR text, flat trees), the smallest container whose frame
      contains ``el``'s center;
    * for ``el=None``, the largest container in the snapshot, preferring scrollable ones.
    """
    if el is None:
        return _largest_container(snap)
    if el.role in CONTAINER_ROLES:
        return el
    for parent in _Index(snap).ancestors(el):
        if parent.role in CONTAINER_ROLES:
            return parent
    around = [
        c for c in snap.all_elements if c.role in CONTAINER_ROLES and c is not el and _contains_center(c, el)
    ]
    best: Element | None = None
    for candidate in around:
        if best is None or _area(candidate) < _area(best):
            best = candidate
    return best


# --------------------------------------------------------------------------- viewports
_EMPTY = Rect(0.0, 0.0, 0.0, 0.0)


def _clip(rect: Rect | None, other: Rect | None) -> Rect | None:
    if rect is None:
        return other
    if other is None:
        return rect
    return rect.intersection(other) or _EMPTY


def _viewport(index: _Index, container: Element | None) -> Rect | None:
    """The on-screen part of ``container``: its frame clipped by its scroll-area ancestors (or
    the collapsed scroll area recorded in ``extra["viewport"]``) and the window (a table's own
    frame spans its whole content). ``None`` if nothing has a frame."""
    window = index.snap.window.frame if index.snap.window is not None else None
    if container is None:
        return window
    rect = container.frame
    recorded = container.extra.get("viewport")
    if isinstance(recorded, list | tuple) and len(recorded) == 4:
        rect = _clip(rect, Rect(*(float(v) for v in recorded)))
    for parent in index.ancestors(container):
        if parent.role == "scrollarea":
            rect = _clip(rect, parent.frame)
    return _clip(rect, window)


def viewport_of(snap: Snapshot, container: Element | None) -> Rect | None:
    """The part of the screen where ``container``'s content is visible (``None`` if unknown).

    Scroll at its center: a list's own frame can extend far beyond the visible area."""
    return _viewport(_Index(snap), container)


def _visible_share(el: Element, viewport: Rect | None) -> float:
    """Share of ``el``'s height inside ``viewport`` (1.0 when either has no frame)."""
    if viewport is None or el.frame is None or el.frame.h <= 0:
        return 1.0
    clipped = el.frame.intersection(viewport)
    return clipped.h / el.frame.h if clipped is not None else 0.0


# --------------------------------------------------------------------------- signatures
def page_signature(snap: Snapshot, container: Element | None) -> str:
    """A stable hash (16 hex chars) of what is visible inside ``container``.

    Covers each visible descendant's role, name, value and position relative to the
    container's viewport (its frame clipped by enclosing scroll areas and the window, rounded
    to :data:`SIGNATURE_GRID` points), plus the container's own value. Two snapshots with the
    same signature show the same content in the same place: a scroll that leaves it unchanged
    did not move (the end was reached). ``None`` hashes the whole snapshot.
    """
    index = _Index(snap)
    viewport = _viewport(index, container)
    origin_x = viewport.x if viewport is not None else 0.0
    origin_y = viewport.y if viewport is not None else 0.0
    items: list[Any] = [container.value if container is not None else None]
    if container is not None and container.frame is not None:
        # A document view (a text area, a table) spans its whole content: scrolling moves it, and a
        # text area has no children whose positions would show that (seen in TextEdit, macOS 26).
        items.append(
            [
                round((container.frame.x - origin_x) / SIGNATURE_GRID),
                round((container.frame.y - origin_y) / SIGNATURE_GRID),
            ]
        )
    for el in index.descendants(container):
        if _visible_share(el, viewport) <= 0.0:
            continue
        position = None
        if el.frame is not None:
            position = [
                round((el.frame.x - origin_x) / SIGNATURE_GRID),
                round((el.frame.y - origin_y) / SIGNATURE_GRID),
                round(el.frame.w / SIGNATURE_GRID),
                round(el.frame.h / SIGNATURE_GRID),
            ]
        items.append([el.role, el.name, None if el.secure else el.value, position])
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SIGNATURE_LENGTH]


# --------------------------------------------------------------------------- page merging
def _norm(line: str) -> str:
    return " ".join(str(line).split())


def overlap_length(tail: Sequence[str], head: Sequence[str]) -> int:
    """Length of the longest suffix of ``tail`` that equals a prefix of ``head``.

    Lines compare whitespace-insensitively (runs of spaces and newlines are equal, leading and
    trailing spaces ignored). Linear time (Knuth-Morris-Pratt prefix function)."""
    if not tail or not head:
        return 0
    pattern = [_norm(line) for line in head]
    text = [_norm(line) for line in tail[-len(head) :]]
    failure = [0] * len(pattern)
    k = 0
    for i in range(1, len(pattern)):
        while k and pattern[i] != pattern[k]:
            k = failure[k - 1]
        if pattern[i] == pattern[k]:
            k += 1
        failure[i] = k
    matched = 0
    for line in text:
        while matched and (matched == len(pattern) or line != pattern[matched]):
            matched = failure[matched - 1]
        if line == pattern[matched]:
            matched += 1
    return matched


def merge_pages(pages: Sequence[Sequence[str]]) -> list[str]:
    """Join the text lines of successive pages, dropping each page's overlap with what came
    before (the longest suffix/prefix match, whitespace-insensitive).

    Exact when lines are distinct; with repeated lines a longer coincidental overlap may be
    removed, so callers that know the scroll distance should prefer it. Original strings are
    kept (the first occurrence wins)."""
    merged: list[str] = []
    for page in pages:
        lines = list(page)
        skip = overlap_length(merged, lines)
        merged.extend(lines[skip:])
    return merged


# --------------------------------------------------------------------------- rows and lines
def element_text(el: Element) -> str | None:
    """The text an element shows: the value of inputs (else their label), the name of
    everything else (else its value). Secure values are never returned."""
    value = None if el.secure else el.value
    if el.role in INPUT_ROLES:
        return value or el.name
    return el.name or value


def _owning_collection(index: _Index, row: Element) -> Element | None:
    for parent in index.ancestors(row):
        if parent.role in COLLECTION_ROLES:
            return parent
    return None


def _joined(texts: Sequence[str | None], separator: str) -> str | None:
    parts: list[str] = []
    for text in texts:
        if text and (not parts or parts[-1] != text):
            parts.append(text)
    return separator.join(parts) if parts else None


def _row_text(index: _Index, row: Element) -> str:
    inner = [el for el in index.descendants(row, include_ocr=False) if el.role != "row"]
    cells = [el for el in index.children.get(row.ref, []) if el.role == "cell"]
    if cells:
        texts = [
            _joined([element_text(el) for el in index.descendants(cell, include_ocr=False)], " ")
            or element_text(cell)
            for cell in cells
        ]
        return CELL_SEPARATOR.join(text or "" for text in texts)
    return (
        _joined([element_text(el) for el in inner if el.role != "cell"], PART_SEPARATOR)
        or element_text(row)
        or ""
    )


Item = tuple[str | None, str]
"""One line of content: ``(ref of the element it comes from, text)``; the ref is ``None`` for
lines of a text area's value."""


def _reading_items(elements: Sequence[Element]) -> list[Item]:
    items: list[Item] = []
    for el in elements:
        if el.role in CONTAINER_ROLES or el.role == "group":
            continue
        text = element_text(el)
        if text and (not items or items[-1][1] != text):
            items.append((el.ref, text))
    return items


def _top_collection(index: _Index, container: Element, inside: Sequence[Element]) -> Element | None:
    """The largest list/table/outline directly inside ``container`` (not nested in another
    collection), or ``None``."""
    best: Element | None = None
    for el in inside:
        if el.role not in COLLECTION_ROLES:
            continue
        chain = index.ancestors(el)
        if container not in chain:
            continue
        between = chain[: chain.index(container)]
        if any(parent.role in COLLECTION_ROLES for parent in between):
            continue
        if best is None or _area(el) > _area(best):
            best = el
    return best


def collect_rows(snap: Snapshot, container: Element | None, *, visible_only: bool = True) -> list[str]:
    """The content of ``container`` as text lines, in order.

    * Lists, tables and outlines: one line per row owned by the container. Table cells are
      joined with ``" | "``; other row parts (static texts) with ``" — "``. Rows without any
      text are skipped, and with ``visible_only`` (the default) so are rows less than half
      inside the viewport (clipped at an edge; the next page shows them whole), which keeps
      successive pages consistent for :func:`merge_pages`. Without row elements, the
      container's direct children (then OCR text inside it) are the rows.
    * Text areas: the lines of the value (snapshots truncate long values; read the element's
      attributes for the full text).
    * Anything else (scroll areas, web areas, ``None`` for the whole snapshot): the text of each
      element in document order, skipping containers and consecutive repeats.
    """
    return [text for _, text in collect_items(snap, container, visible_only=visible_only)]


def collect_items(snap: Snapshot, container: Element | None, *, visible_only: bool = True) -> list[Item]:
    """:func:`collect_rows` with the ref of the element each line comes from (the row, the
    child or the text element; ``None`` for the lines of a text area's value).

    Refs stay with their element across snapshots, so pages of a scrolled container can be
    joined by ref instead of by text (exact even when lines repeat)."""
    if container is None:
        return _reading_items(snap.all_elements)
    if container.role == "textarea":
        value = None if container.secure else container.value
        return [(None, line) for line in value.splitlines()] if value else []
    index = _Index(snap)
    inside = index.descendants(container)
    if container.role not in COLLECTION_ROLES:
        inner = _top_collection(index, container, inside)
        if inner is not None:  # a scroll area around a list/table: its rows are the content
            return collect_items(snap, inner, visible_only=visible_only)
        return _reading_items(inside)
    viewport = _viewport(index, container) if visible_only else None
    rows = [
        el
        for el in inside
        if el.role == "row"
        and _owning_collection(index, el) is container
        and _visible_share(el, viewport) >= ROW_VISIBLE_MIN
    ]
    if rows:
        return [(row.ref, text) for row in rows if (text := _row_text(index, row)).strip(" |—")]
    children: list[Item] = [
        (el.ref, text) for el in index.children.get(container.ref, []) if (text := element_text(el))
    ]
    if children:
        return children
    return _reading_items([el for el in inside if el.role == "ocrtext"])
