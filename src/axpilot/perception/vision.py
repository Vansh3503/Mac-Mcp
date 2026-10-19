"""Vision fallback: OCR text as clickable pseudo-elements (refs ``o1``, ``o2``, …).

Apps with a poor accessibility tree (canvases, games, remote desktops, some Java/Qt apps) still
draw text on screen. :func:`ocr_elements` turns OCR results into :class:`Element` objects that
look like snapshot elements: role ``ocrtext``, a frame in screen points, ``interactive=True``
(the server clicks their center), no engine handle and an empty locator path.
:func:`merge_vision` appends them to a snapshot after the AX elements, dropping text that the
AX tree already names and anything inside a secure field.

Processing in :func:`ocr_elements`:

1. drop lines below ``min_confidence``, empty text and boxes without a positive size;
2. group boxes into visual lines: same baseline (bottoms within :data:`BASELINE_TOLERANCE` of
   the smaller height), similar height and overlapping vertically;
3. within a line, merge neighbours closer than :data:`WORD_GAP` × line height into one
   element (words → phrases); larger gaps (columns) stay separate; an exact duplicate box
   (same text, mostly overlapping) is dropped;
4. order top-to-bottom, then left-to-right. The result does not depend on input order.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import OcrLine, Rect
from axpilot.perception.model import Element, Locator, Snapshot
from axpilot.perception.snapshot import NAME_MAX_CHARS, clean_text, truncate

OCR_ROLE = "ocrtext"
"""Short role of OCR pseudo-elements."""
OCR_AX_ROLE = "OCRText"
"""Synthetic "AX role" of OCR pseudo-elements (deliberately not an ``AX…`` name)."""
OCR_REF_PREFIX = "o"

BASELINE_TOLERANCE = 0.35
"""Boxes are on one line when their bottoms differ by at most this share of the smaller height."""
HEIGHT_RATIO_MAX = 1.8
"""…and their heights differ by at most this factor…"""
VERTICAL_OVERLAP_MIN = 0.5
"""…and they overlap vertically by at least this share of the smaller height."""
WORD_GAP = 1.0
"""Neighbours on a line merge when the gap between them is at most this many line heights."""
DUPLICATE_OVERLAP = 0.5
"""Boxes with the same text overlapping by more than this share of the narrower one are duplicates."""
DEDUPE_MARGIN = 6.0
"""Points added around an AX element's frame when deciding whether OCR text lies on it."""

CONTAINER_ROLES: frozenset[str] = frozenset(
    {
        "window",
        "sheet",
        "dialog",
        "popover",
        "group",
        "splitgroup",
        "scrollarea",
        "webarea",
        "list",
        "table",
        "outline",
        "browser",
        "grid",
        "tabs",
        "toolbar",
        "radiogroup",
    }
)
"""Short roles of large containers: their names are titles, so OCR text that merely *occurs* in
such a name (``"general"`` in a web area called ``"Slack | general"``) is not a duplicate."""


@dataclass(slots=True)
class _Box:
    text: str
    confidence: float
    frame: Rect

    @property
    def bottom(self) -> float:
        return self.frame.y + self.frame.h

    @property
    def right(self) -> float:
        return self.frame.x + self.frame.w

    @property
    def center_y(self) -> float:
        return self.frame.y + self.frame.h / 2


# --------------------------------------------------------------------------- text helpers
def normalize_text(text: str | None) -> str:
    """Case-folded, whitespace-collapsed text with ellipses unified and edge punctuation
    (``.:…``) stripped, for comparing OCR output with AX names."""
    if not text:
        return ""
    folded = " ".join(text.replace("…", "...").casefold().split())
    return folded.strip(" .:")


def is_vision_element(el: Element) -> bool:
    """True for OCR pseudo-elements (they have no AX handle; act on them by coordinates)."""
    return el.ax_role == OCR_AX_ROLE


def ocr_ref_index(ref: str) -> int | None:
    """``7`` for ``"o7"``; ``None`` for anything that is not an OCR ref."""
    if ref.startswith(OCR_REF_PREFIX) and ref[1:].isdigit() and not ref[1:].startswith("0"):
        return int(ref[1:])
    return None


# --------------------------------------------------------------------------- grouping
def _usable(line: OcrLine, min_confidence: float) -> _Box | None:
    text = clean_text(line.text)
    frame = line.frame
    confidence = line.confidence
    if not text or frame is None or not isinstance(confidence, int | float):
        return None
    values = (frame.x, frame.y, frame.w, frame.h, float(confidence))
    if not all(math.isfinite(v) for v in values) or frame.w <= 0 or frame.h <= 0:
        return None
    if confidence < min_confidence:
        return None
    return _Box(text=text, confidence=float(confidence), frame=frame)


def _same_line(a: _Box, b: _Box) -> bool:
    small = min(a.frame.h, b.frame.h)
    large = max(a.frame.h, b.frame.h)
    if large > small * HEIGHT_RATIO_MAX:
        return False
    if abs(a.bottom - b.bottom) > BASELINE_TOLERANCE * small:
        return False
    overlap = min(a.bottom, b.bottom) - max(a.frame.y, b.frame.y)
    return overlap >= VERTICAL_OVERLAP_MIN * small


def _sort_key(box: _Box) -> tuple[float, float, float, float, float, str, float]:
    f = box.frame
    return (box.center_y, f.x, f.y, f.w, f.h, box.text, -box.confidence)


def _group_rows(boxes: list[_Box]) -> list[list[_Box]]:
    """Greedy grouping in reading order; each row keeps its first box as the anchor."""
    rows: list[list[_Box]] = []
    for box in sorted(boxes, key=_sort_key):
        target = None
        for row in reversed(rows):
            anchor = row[0]
            if anchor.center_y < box.center_y - 2 * max(anchor.frame.h, box.frame.h):
                break  # rows further up cannot match (boxes arrive sorted by center)
            if _same_line(anchor, box):
                target = row
                break
        if target is None:
            rows.append([box])
        else:
            target.append(box)
    return rows


def _union(a: Rect, b: Rect) -> Rect:
    x1, y1 = min(a.x, b.x), min(a.y, b.y)
    x2, y2 = max(a.x + a.w, b.x + b.w), max(a.y + a.h, b.y + b.h)
    return Rect(x1, y1, x2 - x1, y2 - y1)


def _is_duplicate(current: _Box, box: _Box) -> bool:
    overlap = min(current.right, box.right) - max(current.frame.x, box.frame.x)
    narrower = min(current.frame.w, box.frame.w)
    return (
        normalize_text(current.text) == normalize_text(box.text)
        and narrower > 0
        and overlap > DUPLICATE_OVERLAP * narrower
    )


def _merge_row(row: list[_Box]) -> list[_Box]:
    ordered = sorted(row, key=lambda b: (b.frame.x, b.frame.y, b.frame.w, b.text, -b.confidence))
    merged: list[_Box] = []
    for box in ordered:
        if merged:
            current = merged[-1]
            if _is_duplicate(current, box):
                continue
            height = max(current.frame.h, box.frame.h)
            if box.frame.x - current.right <= WORD_GAP * height:
                merged[-1] = _Box(
                    text=f"{current.text} {box.text}",
                    confidence=min(current.confidence, box.confidence),
                    frame=_union(current.frame, box.frame),
                )
                continue
        merged.append(box)
    return merged


def group_ocr_lines(lines: Iterable[OcrLine], *, min_confidence: float = 0.3) -> list[OcrLine]:
    """Filter, merge and order OCR boxes (steps 1-4 of the module docstring) as :class:`OcrLine`."""
    boxes = [box for line in lines if (box := _usable(line, min_confidence)) is not None]
    merged = [box for row in _group_rows(boxes) for box in _merge_row(row)]
    return [OcrLine(text=box.text, confidence=box.confidence, frame=box.frame) for box in merged]


# --------------------------------------------------------------------------- elements
def _element(index: int, line: OcrLine, pid: int, window_id: int | None) -> Element:
    name = truncate(line.text, NAME_MAX_CHARS)
    extra: dict[str, object] = {"source": "ocr", "confidence": round(line.confidence, 3)}
    if name != line.text:
        extra["text"] = line.text
    return Element(
        ref=f"{OCR_REF_PREFIX}{index}",
        role=OCR_ROLE,
        ax_role=OCR_AX_ROLE,
        subrole=None,
        name=name,
        value=None,
        frame=line.frame,
        depth=0,
        enabled=True,
        focused=False,
        selected=False,
        expanded=None,
        secure=False,
        interactive=True,
        actions=(),
        extra=extra,
        locator=Locator(
            pid=pid, window_id=window_id, path=(), ax_role=OCR_AX_ROLE, identifier=None, name=name
        ),
        handle=None,
        parent_ref=None,
    )


def ocr_elements(
    lines: Sequence[OcrLine],
    *,
    start_index: int = 1,
    min_confidence: float = 0.3,
    exclude_names: Iterable[str | None] = (),
    pid: int = 0,
    window_id: int | None = None,
) -> list[Element]:
    """OCR results as pseudo-elements ``o<start_index>``, ``o<start_index+1>``, … in reading order.

    Words on the same baseline merge into phrases (see the module docstring). Lines whose
    normalized text equals one of ``exclude_names`` (typically the AX element names) are
    dropped; :func:`merge_vision` additionally drops text lying on an AX element that already
    names it. ``pid``/``window_id`` fill the locators. Raises ``INVALID_ARGUMENT`` for
    ``start_index < 1`` or a ``min_confidence`` outside 0…1.
    """
    if start_index < 1:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"start_index must be at least 1, got {start_index}.")
    if not 0.0 <= min_confidence <= 1.0:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"min_confidence must be between 0 and 1, got {min_confidence}."
        )
    excluded = {norm for name in exclude_names if (norm := normalize_text(name))}
    grouped = group_ocr_lines(lines, min_confidence=min_confidence)
    kept = [line for line in grouped if normalize_text(line.text) not in excluded]
    return [_element(start_index + offset, line, pid, window_id) for offset, line in enumerate(kept)]


# --------------------------------------------------------------------------- merging
def _expanded(rect: Rect, margin: float) -> Rect:
    return Rect(rect.x - margin, rect.y - margin, rect.w + 2 * margin, rect.h + 2 * margin)


def _lies_on(el: Element, ocr: Element) -> bool:
    if el.frame is None or ocr.frame is None:
        return False
    return _expanded(el.frame, DEDUPE_MARGIN).contains(ocr.frame.center)


def _texts(el: Element) -> list[str]:
    texts = [normalize_text(el.name)]
    if not el.secure:
        texts.append(normalize_text(el.value))
    return [text for text in texts if text]


def _covered(ocr: Element, existing: Sequence[Element]) -> bool:
    """True if ``ocr`` duplicates an existing element or lies inside a secure field."""
    text = normalize_text(ocr.name)
    if not text:
        return True
    for el in existing:
        if el.secure and _lies_on(el, ocr):
            return True  # never surface text drawn inside a password field
        for known in _texts(el):
            if known == text and (el.frame is None or ocr.frame is None or _lies_on(el, ocr)):
                return True
            if len(text) >= 2 and text in known and el.role not in CONTAINER_ROLES and _lies_on(el, ocr):
                return True
    return False


def _page(snap: Snapshot, merged: list[Element], max_elements: int | None) -> list[Element]:
    if max_elements is not None:
        return merged[snap.offset : snap.offset + max_elements]
    if snap.next_offset is None:
        return merged[snap.offset :]
    return list(snap.elements)


def merge_vision(snap: Snapshot, elements: Sequence[Element], *, max_elements: int | None = None) -> Snapshot:
    """A copy of ``snap`` with OCR ``elements`` appended after its AX elements.

    OCR elements are dropped when their center lies outside the snapshot's window (text of
    another app showing through the capture), when an AX (or earlier OCR) element already
    shows the same text at that place (same normalized name/value, or a non-container name
    containing it, with the OCR text lying on the element's frame), or when they lie inside a
    secure field. Survivors are renumbered
    ``o<n>``, continuing after any OCR refs already in ``snap`` (so merging twice adds nothing
    new), and their locators get the snapshot's pid and window.

    The page (``elements``) is ``all_elements[offset:offset + max_elements]`` when
    ``max_elements`` is given; otherwise OCR elements join the page only if it was the last
    page, so paging still covers every element exactly once.
    """
    if max_elements is not None and max_elements < 1:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"max_elements must be at least 1, got {max_elements}."
        )
    known: list[Element] = list(snap.all_elements)
    indices = [index for el in snap.all_elements if (index := ocr_ref_index(el.ref)) is not None]
    next_index = max(indices, default=0) + 1
    pid = snap.app.pid
    window_id = snap.window.window_id if snap.window is not None else None
    bounds = snap.window.frame if snap.window is not None else None
    added: list[Element] = []
    for el in elements:
        center = el.center
        if bounds is not None and (center is None or not bounds.contains(center)):
            continue
        if _covered(el, known):
            continue
        renumbered = dataclasses.replace(
            el,
            ref=f"{OCR_REF_PREFIX}{next_index}",
            depth=0,
            parent_ref=None,
            handle=None,
            extra=dict(el.extra),
            locator=Locator(
                pid=pid, window_id=window_id, path=(), ax_role=OCR_AX_ROLE, identifier=None, name=el.name
            ),
        )
        next_index += 1
        added.append(renumbered)
        known.append(renumbered)
    merged = [*snap.all_elements, *added]
    return dataclasses.replace(snap, all_elements=merged, elements=_page(snap, merged, max_elements))
