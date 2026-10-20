"""Plain data types shared by the engine, perception, policy and server layers.

Everything here is platform-independent and dependency-free.

Coordinates are global screen **points** (not pixels) with the origin at the top-left
corner of the main display, matching CoreGraphics. Displays left of or above the main
display have negative coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MouseButton = Literal["left", "right", "middle"]
AppAction = Literal["launch", "activate", "hide", "unhide", "quit", "force_quit"]
WindowAction = Literal["focus", "move", "resize", "minimize", "restore", "zoom", "fullscreen", "close"]
ImageFormat = Literal["png", "jpeg"]

MODIFIERS: tuple[str, ...] = ("ctrl", "option", "shift", "cmd", "fn")
"""Canonical modifier names, in the order they are displayed (macOS convention ⌃⌥⇧⌘)."""

_SEPARATOR_KEY_NAMES: dict[str, str] = {"+": "plus", "-": "minus"}


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def to_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1)]


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> Point:
        return Point(self.x + self.w / 2, self.y + self.h / 2)

    @property
    def area(self) -> float:
        return max(self.w, 0.0) * max(self.h, 0.0)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.x + self.w and self.y <= p.y < self.y + self.h

    def intersects(self, other: Rect) -> bool:
        return not (
            other.x >= self.x + self.w
            or other.x + other.w <= self.x
            or other.y >= self.y + self.h
            or other.y + other.h <= self.y
        )

    def intersection(self, other: Rect) -> Rect | None:
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2, y2 = min(self.x + self.w, other.x + other.w), min(self.y + self.h, other.y + other.h)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def union(self, other: Rect) -> Rect:
        """The smallest rectangle containing both."""
        x1, y1 = min(self.x, other.x), min(self.y, other.y)
        x2, y2 = max(self.x + self.w, other.x + other.w), max(self.y + self.h, other.y + other.h)
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def to_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1), round(self.w, 1), round(self.h, 1)]


@dataclass(slots=True)
class AppInfo:
    name: str
    pid: int
    bundle_id: str | None = None
    path: str | None = None
    frontmost: bool = False
    hidden: bool = False

    def matches(self, query: str) -> bool:
        """True if ``query`` equals the name or bundle id (case-insensitive) or the pid."""
        q = query.strip().casefold()
        if not q:
            return False
        if q == str(self.pid):
            return True
        if self.bundle_id and q == self.bundle_id.casefold():
            return True
        return q == self.name.casefold() or q.removesuffix(".app") == self.name.casefold()


@dataclass(slots=True)
class WindowInfo:
    window_id: int
    pid: int
    app: str
    title: str
    frame: Rect
    bundle_id: str | None = None
    display_id: int | None = None
    focused: bool = False
    main: bool = False
    minimized: bool = False
    fullscreen: bool = False


@dataclass(slots=True)
class DisplayInfo:
    display_id: int
    frame: Rect
    work_area: Rect
    scale: float
    main: bool = False
    name: str | None = None


@dataclass(slots=True)
class PermissionStatus:
    accessibility: bool
    screen_recording: bool
    automation: dict[str, bool] = field(default_factory=dict)
    responsible_app: str | None = None
    """Best-effort name of the app macOS attributes our permission checks to."""
    notes: list[str] = field(default_factory=list)
    automation_unasked: list[str] = field(default_factory=list)
    """Targets in ``automation`` (``False``) that macOS has not asked the user about yet: it asks
    the first time a tool needs one. The others there were denied."""


@dataclass(slots=True, eq=False)
class RawNode:
    """One accessibility element as captured by an engine.

    ``value`` is never populated for secure (password) fields: engines must leave it
    ``None`` when ``secure`` is true. ``handle`` is an opaque engine object (an
    ``AXUIElementRef`` on macOS, an id in the fake engine) used to act on the element.
    """

    role: str
    subrole: str | None = None
    title: str | None = None
    description: str | None = None
    value: Any = None
    help: str | None = None
    identifier: str | None = None
    placeholder: str | None = None
    label: str | None = None
    url: str | None = None
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    expanded: bool | None = None
    secure: bool = False
    visible: bool = True
    frame: Rect | None = None
    actions: tuple[str, ...] = ()
    child_count: int | None = None
    """Total number of children when ``children`` was truncated by capture budgets."""
    children: list[RawNode] = field(default_factory=list)
    handle: Any = field(default=None, repr=False)

    def walk(self):
        """Pre-order traversal of this node and its descendants."""
        stack: list[RawNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


@dataclass(slots=True)
class RawTree:
    """The accessibility tree of one app (usually scoped to one window)."""

    app: AppInfo
    window: WindowInfo | None
    root: RawNode
    menu_bar: RawNode | None = None
    node_count: int = 0
    truncated: bool = False
    """True when the engine stopped early because of ``max_nodes``/``max_depth``/timeouts."""
    elapsed_ms: float = 0.0


@dataclass(slots=True)
class Capture:
    """An encoded screenshot plus the screen region (in points) it covers."""

    data: bytes = field(repr=False)
    format: ImageFormat
    width: int
    height: int
    region: Rect

    @property
    def scale_x(self) -> float:
        """Image pixels per screen point, horizontally."""
        return self.width / self.region.w if self.region.w else 1.0

    @property
    def scale_y(self) -> float:
        return self.height / self.region.h if self.region.h else 1.0

    def to_points(self, px: float, py: float) -> Point:
        """Map a pixel coordinate in this image back to global screen points."""
        return Point(self.region.x + px / self.scale_x, self.region.y + py / self.scale_y)

    def to_pixels(self, p: Point) -> tuple[float, float]:
        return ((p.x - self.region.x) * self.scale_x, (p.y - self.region.y) * self.scale_y)


@dataclass(slots=True)
class OcrLine:
    text: str
    confidence: float
    frame: Rect


@dataclass(slots=True, eq=False)
class MenuNode:
    title: str
    enabled: bool = True
    checked: bool = False
    shortcut: str | None = None
    children: list[MenuNode] = field(default_factory=list)
    handle: Any = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class KeyCombo:
    """A key plus modifiers. ``key`` is a normalized name (see ``axpilot.input.keys``)."""

    key: str
    modifiers: frozenset[str] = frozenset()

    def __str__(self) -> str:
        mods = [m for m in MODIFIERS if m in self.modifiers]
        # "+" and "-" separate keys in combo strings, so those keys are written by name
        # ("cmd+plus", not "cmd++"); otherwise "a + b" would be ambiguous in key sequences.
        key = _SEPARATOR_KEY_NAMES.get(self.key, self.key)
        return "+".join([*mods, key])


@dataclass(slots=True)
class ClipboardContent:
    text: str | None = None
    image_png: bytes | None = field(default=None, repr=False)
    files: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
