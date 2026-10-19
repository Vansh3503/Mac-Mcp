"""Thread-safe LRU stores for snapshots and screenshots, addressed by short ids.

Ids look random (``s_7f3a``, ``c_k2p9``) but come from a counter mixed with a per-process
random salt: unique within a process (for 36⁴ ids, far beyond the store capacities) and
different across restarts, so an id held over from a previous server process is reported
as expired instead of silently resolving to an unrelated snapshot.
"""

from __future__ import annotations

import itertools
import math
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Capture
from axpilot.perception.model import Element, Snapshot

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_REF_PATTERN = re.compile(r"^(?:(?P<sid>s_[0-9a-z]{4,6}):)?(?P<ref>[eo][1-9][0-9]*)$")
"""Element refs: ``e12`` (accessibility elements) or ``o3`` (OCR text, see ``perception.vision``)."""


def _base36(value: int, width: int) -> str:
    digits: list[str] = []
    while value:
        value, digit = divmod(value, 36)
        digits.append(_ALPHABET[digit])
    return "".join(reversed(digits)).rjust(width, "0")


class IdFactory:
    """Produces ``<prefix><4 base-36 chars>`` ids: a salted permutation of a counter."""

    def __init__(self, prefix: str, width: int = 4) -> None:
        self._prefix = prefix
        self._width = width
        self._modulus = 36**width
        self._salt = secrets.randbelow(self._modulus)
        self._stride = self._pick_stride()
        self._counter = itertools.count()
        self._lock = threading.Lock()

    def _pick_stride(self) -> int:
        """A random stride coprime to the modulus (so the sequence is a full-period permutation),
        taken from the middle third of the range so consecutive ids never look sequential."""
        third = self._modulus // 3
        while True:
            stride = third + secrets.randbelow(third)
            if math.gcd(stride, self._modulus) == 1:
                return stride

    def next(self, taken: Callable[[str], bool] | None = None) -> str:
        """A fresh id; ``taken`` can veto ids still in use (only possible after wrap-around)."""
        with self._lock:
            for _ in range(self._modulus):
                n = next(self._counter)
                ident = self._prefix + _base36((self._salt + n * self._stride) % self._modulus, self._width)
                if taken is None or not taken(ident):
                    return ident
        raise AXPilotError(ErrorCode.INTERNAL, f"Ran out of {self._prefix!r} ids.")  # pragma: no cover


def parse_ref(ref: str) -> tuple[str | None, str]:
    """Split ``"e12"``, ``"o3"`` or ``"s_ab12:e12"`` into ``(snapshot_id | None, "e12")``.

    Raises ``INVALID_ARGUMENT`` for anything else."""
    match = _REF_PATTERN.match(ref.strip().lower()) if isinstance(ref, str) else None
    if match is None:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{ref!r} is not an element ref.",
            hint='Refs look like "e12" or "o3" (or "s_ab12:e12"); take them from the latest snapshot.',
        )
    return match.group("sid"), match.group("ref")


def _ref_ranges(snap: Snapshot) -> str:
    """`` (it has e1–e40, o1–o5)`` for a STALE_REF message."""
    ocr = sum(1 for el in snap.all_elements if el.ref.startswith("o"))
    ranges = [f"e1–e{snap.total - ocr}"] if snap.total - ocr else []
    if ocr:
        ranges.append(f"o1–o{ocr}")
    return f" (it has {', '.join(ranges)})" if ranges else " (it has no elements)"


class SnapshotStore:
    """Keeps the most recently used snapshots so refs can be resolved by later tool calls."""

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._items: OrderedDict[str, Snapshot] = OrderedDict()
        self._created: dict[str, int] = {}
        self._sequence = itertools.count()
        self._ids = IdFactory("s_")
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, snapshot_id: object) -> bool:
        with self._lock:
            return snapshot_id in self._items

    def new_id(self) -> str:
        """A new, unused snapshot id (``"s_"`` + 4 lowercase alphanumerics)."""
        return self._ids.next(taken=self.__contains__)

    def put(self, snap: Snapshot) -> None:
        """Store ``snap`` (replacing one with the same id), evicting the least recently used."""
        with self._lock:
            self._items[snap.snapshot_id] = snap
            self._items.move_to_end(snap.snapshot_id)
            self._created[snap.snapshot_id] = next(self._sequence)
            while len(self._items) > self._capacity:
                evicted, _ = self._items.popitem(last=False)
                self._created.pop(evicted, None)

    def get(self, snapshot_id: str) -> Snapshot:
        """The snapshot with this id (marks it recently used). Raises ``SNAPSHOT_NOT_FOUND``."""
        key = snapshot_id.strip().lower() if isinstance(snapshot_id, str) else snapshot_id
        with self._lock:
            snap = self._items.get(key)
            if snap is None:
                raise AXPilotError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    f"Snapshot {snapshot_id!r} does not exist or has expired.",
                )
            self._items.move_to_end(key)
            return snap

    def latest(self, pid: int | None = None) -> Snapshot | None:
        """The most recently *created* snapshot (optionally of the app with ``pid``)."""
        with self._lock:
            candidates = [
                (self._created[sid], snap)
                for sid, snap in self._items.items()
                if pid is None or snap.app.pid == pid
            ]
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else None

    def resolve(self, ref: str, snapshot_id: str | None = None) -> tuple[Snapshot, Element]:
        """Find the element for ``ref`` (``"e12"``, OCR text ``"o3"``, or ``"s_ab12:e12"``).

        Without a snapshot id (in the ref or the argument) the latest snapshot is used.
        Raises ``INVALID_ARGUMENT`` (malformed ref, conflicting ids), ``SNAPSHOT_NOT_FOUND``
        or ``STALE_REF`` (the snapshot has no such element).
        """
        ref_sid, element_ref = parse_ref(ref)
        wanted = snapshot_id.strip().lower() if snapshot_id else None
        if ref_sid and wanted and ref_sid != wanted:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Ref {ref!r} belongs to snapshot {ref_sid}, but snapshot_id {snapshot_id!r} was given.",
                hint="Pass matching values, or just the ref.",
            )
        sid = ref_sid or wanted
        if sid is None:
            snap = self.latest()
            if snap is None:
                raise AXPilotError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    "There is no snapshot to resolve refs against yet.",
                    hint="Call `snapshot` first, then use the refs it returns.",
                )
            self.get(snap.snapshot_id)  # mark as recently used
        else:
            snap = self.get(sid)
        element = snap.element(element_ref)
        if element is None:
            raise AXPilotError(
                ErrorCode.STALE_REF,
                f"Ref {element_ref} is not part of snapshot {snap.snapshot_id}{_ref_ranges(snap)}.",
            )
        return snap, element

    def clear(self) -> None:
        """Forget every snapshot."""
        with self._lock:
            self._items.clear()
            self._created.clear()


class CaptureStore:
    """Keeps recent screenshots so pixel coordinates can be mapped back to screen points."""

    def __init__(self, capacity: int = 16) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._items: OrderedDict[str, Capture] = OrderedDict()
        self._latest: str | None = None
        self._ids = IdFactory("c_")
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, capture_id: object) -> bool:
        with self._lock:
            return capture_id in self._items

    def put(self, capture: Capture) -> str:
        """Store a capture and return its new id (``"c_"`` + 4 lowercase alphanumerics)."""
        capture_id = self._ids.next(taken=self.__contains__)
        with self._lock:
            self._items[capture_id] = capture
            self._latest = capture_id
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return capture_id

    def get(self, capture_id: str | None) -> Capture:
        """The capture with this id, or the latest one for ``None``. Raises ``SNAPSHOT_NOT_FOUND``."""
        with self._lock:
            key = self._latest if capture_id is None else capture_id.strip().lower()
            capture = self._items.get(key) if key is not None else None
            if key is None or capture is None:
                if capture_id is None:
                    raise AXPilotError(
                        ErrorCode.SNAPSHOT_NOT_FOUND,
                        "There is no screenshot yet.",
                        hint="Call `screenshot` first.",
                    )
                raise AXPilotError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    f"Screenshot {capture_id!r} does not exist or has expired.",
                    hint="Take a new screenshot and use its screenshot_id.",
                )
            self._items.move_to_end(key)
            return capture
