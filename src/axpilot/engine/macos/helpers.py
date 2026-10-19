"""Platform-independent helpers for the macOS engine.

Nothing in this module imports PyObjC, so unlike the rest of :mod:`axpilot.engine.macos`
it imports (and is unit-tested) on every OS. The PyObjC-facing modules stay thin and hand
their decisions to these functions: AX error mapping, AXValue parsing, menu-title matching,
shortcut decoding, AX↔CGWindow matching, geometry, keystroke chunking, process ancestry,
installed-app discovery and Chromium/Electron detection.
"""

from __future__ import annotations

import math
import plistlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import US_ANSI_KEYCODES, US_ANSI_SHIFTED, to_glyphs
from axpilot.models import DisplayInfo, KeyCombo, Point, Rect
from axpilot.system.osascript import applescript_string

# =============================================================================== AX errors

AX_SUCCESS = 0
AX_FAILURE = -25200
AX_ILLEGAL_ARGUMENT = -25201
AX_INVALID_UI_ELEMENT = -25202
AX_INVALID_OBSERVER = -25203
AX_CANNOT_COMPLETE = -25204
AX_ATTRIBUTE_UNSUPPORTED = -25205
AX_ACTION_UNSUPPORTED = -25206
AX_NOTIFICATION_UNSUPPORTED = -25207
AX_NOT_IMPLEMENTED = -25208
AX_NOTIFICATION_ALREADY_REGISTERED = -25209
AX_NOTIFICATION_NOT_REGISTERED = -25210
AX_API_DISABLED = -25211
AX_NO_VALUE = -25212
AX_PARAMETERIZED_ATTRIBUTE_UNSUPPORTED = -25213
AX_NOT_ENOUGH_PRECISION = -25214

AX_ERROR_NAMES: dict[int, str] = {
    AX_SUCCESS: "kAXErrorSuccess",
    AX_FAILURE: "kAXErrorFailure",
    AX_ILLEGAL_ARGUMENT: "kAXErrorIllegalArgument",
    AX_INVALID_UI_ELEMENT: "kAXErrorInvalidUIElement",
    AX_INVALID_OBSERVER: "kAXErrorInvalidUIElementObserver",
    AX_CANNOT_COMPLETE: "kAXErrorCannotComplete",
    AX_ATTRIBUTE_UNSUPPORTED: "kAXErrorAttributeUnsupported",
    AX_ACTION_UNSUPPORTED: "kAXErrorActionUnsupported",
    AX_NOTIFICATION_UNSUPPORTED: "kAXErrorNotificationUnsupported",
    AX_NOT_IMPLEMENTED: "kAXErrorNotImplemented",
    AX_NOTIFICATION_ALREADY_REGISTERED: "kAXErrorNotificationAlreadyRegistered",
    AX_NOTIFICATION_NOT_REGISTERED: "kAXErrorNotificationNotRegistered",
    AX_API_DISABLED: "kAXErrorAPIDisabled",
    AX_NO_VALUE: "kAXErrorNoValue",
    AX_PARAMETERIZED_ATTRIBUTE_UNSUPPORTED: "kAXErrorParameterizedAttributeUnsupported",
    AX_NOT_ENOUGH_PRECISION: "kAXErrorNotEnoughPrecision",
}
"""``AXError`` values (``HIServices/AXError.h``) and their names."""

MISSING_VALUE_ERRORS: frozenset[int] = frozenset(
    {
        AX_NO_VALUE,
        AX_ATTRIBUTE_UNSUPPORTED,
        AX_NOT_IMPLEMENTED,
        AX_PARAMETERIZED_ATTRIBUTE_UNSUPPORTED,
        AX_FAILURE,
        AX_ILLEGAL_ARGUMENT,
    }
)
"""Errors that, for a *read*, simply mean "this element has no such value"."""

TIMEOUT_FRACTION = 0.8
"""A ``kAXErrorCannotComplete`` that took at least this fraction of the messaging timeout is
treated as a timeout (the app did not answer in time) rather than an immediate failure."""

AXOperation = Literal["read", "write", "action"]


def ax_error_name(code: int) -> str:
    """``"kAXErrorCannotComplete (-25204)"`` for a known code, ``"AXError -1"`` otherwise."""
    name = AX_ERROR_NAMES.get(code)
    return f"{name} ({code})" if name else f"AXError {code}"


def is_timeout(elapsed_s: float, timeout_s: float) -> bool:
    """True if a call that failed with ``kAXErrorCannotComplete`` ran into the timeout."""
    return timeout_s > 0 and elapsed_s >= timeout_s * TIMEOUT_FRACTION


def classify_ax_error(code: int, operation: AXOperation = "read") -> ErrorCode:
    """Map an ``AXError`` to the AXPilot :class:`ErrorCode` for ``operation``.

    ``kAXErrorAPIDisabled`` means Accessibility is not granted, ``kAXErrorInvalidUIElement``
    that the element is gone, and ``kAXErrorCannotComplete`` that the app did not answer.
    """
    if code == AX_API_DISABLED:
        return ErrorCode.PERMISSION_ACCESSIBILITY
    if code == AX_INVALID_UI_ELEMENT:
        return ErrorCode.STALE_REF
    if code == AX_CANNOT_COMPLETE:
        return ErrorCode.TIMEOUT
    if code in (AX_ILLEGAL_ARGUMENT, AX_NOT_ENOUGH_PRECISION):
        return ErrorCode.INVALID_ARGUMENT
    if code in (
        AX_ACTION_UNSUPPORTED,
        AX_ATTRIBUTE_UNSUPPORTED,
        AX_NO_VALUE,
        AX_NOT_IMPLEMENTED,
        AX_PARAMETERIZED_ATTRIBUTE_UNSUPPORTED,
    ):
        return ErrorCode.NOT_SUPPORTED if operation == "read" else ErrorCode.ELEMENT_NOT_ACTIONABLE
    if code == AX_FAILURE:
        return ErrorCode.INTERNAL if operation == "read" else ErrorCode.ELEMENT_NOT_ACTIONABLE
    return ErrorCode.INTERNAL


# ============================================================================== AXValues

AXVALUE_ILLEGAL = 0
AXVALUE_CGPOINT = 1
AXVALUE_CGSIZE = 2
AXVALUE_CGRECT = 3
AXVALUE_CFRANGE = 4
AXVALUE_AXERROR = 5

_AXVALUE_TYPE_NAMES: dict[str, int] = {
    "kAXValueCGPointType": AXVALUE_CGPOINT,
    "kAXValueTypeCGPoint": AXVALUE_CGPOINT,
    "kAXValueCGSizeType": AXVALUE_CGSIZE,
    "kAXValueTypeCGSize": AXVALUE_CGSIZE,
    "kAXValueCGRectType": AXVALUE_CGRECT,
    "kAXValueTypeCGRect": AXVALUE_CGRECT,
    "kAXValueCFRangeType": AXVALUE_CFRANGE,
    "kAXValueTypeCFRange": AXVALUE_CFRANGE,
    "kAXValueAXErrorType": AXVALUE_AXERROR,
    "kAXValueTypeAXError": AXVALUE_AXERROR,
}
_AXVALUE_FIELDS: dict[int, tuple[str, ...]] = {
    AXVALUE_CGPOINT: ("x", "y"),
    AXVALUE_CGSIZE: ("w", "h"),
    AXVALUE_CGRECT: ("x", "y", "w", "h"),
    AXVALUE_CFRANGE: ("location", "length"),
    AXVALUE_AXERROR: ("error",),
}
_AXVALUE_TYPE_RE = re.compile(r"type\s*=\s*(kAXValue\w+)")
_AXVALUE_FIELD_RE = re.compile(r"(\w+):(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)")


def parse_axvalue_description(text: str) -> tuple[int, tuple[float, ...]] | None:
    """Parse the ``CFCopyDescription`` of an ``AXValueRef``.

    This is the fallback when ``AXValueGetValue`` cannot decode a value, e.g.
    ``"<AXValue 0x6000…> {value = x:12.000000 y:34.000000 type = kAXValueCGPointType}"`` →
    ``(AXVALUE_CGPOINT, (12.0, 34.0))``. Returns ``None`` if the text is not recognized.
    """
    match = _AXVALUE_TYPE_RE.search(text)
    if not match:
        return None
    value_type = _AXVALUE_TYPE_NAMES.get(match.group(1))
    if value_type is None:
        return None
    start = text.find("value =")
    body = text[start if 0 <= start < match.start() else 0 : match.start()]
    fields = {key: float(number) for key, number in _AXVALUE_FIELD_RE.findall(body)}
    keys = _AXVALUE_FIELDS[value_type]
    if not all(key in fields for key in keys):
        return None
    return value_type, tuple(fields[key] for key in keys)


# ============================================================================== geometry


def frame_from(position: tuple[float, float] | None, size: tuple[float, float] | None) -> Rect | None:
    """A :class:`Rect` from an ``AXPosition`` and an ``AXSize`` (``None`` if either is missing
    or not finite)."""
    if position is None or size is None:
        return None
    values = (*position, *size)
    if not all(isinstance(v, int | float) and math.isfinite(v) for v in values):
        return None
    return Rect(float(position[0]), float(position[1]), float(size[0]), float(size[1]))


def cocoa_to_global(x: float, y: float, w: float, h: float, primary_height: float) -> Rect:
    """Convert a Cocoa rect (origin bottom-left of the primary display, y up) to global
    CoreGraphics points (origin top-left of the primary display, y down)."""
    return Rect(x, primary_height - (y + h), w, h)


def fit_size(width: float, height: float, max_edge: int) -> tuple[int, int]:
    """Pixel size for ``width``×``height`` scaled down (never up) so the longest edge is at
    most ``max_edge``. Both results are at least 1."""
    if max_edge < 1:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"max_edge must be ≥ 1, got {max_edge}.")
    width, height = max(float(width), 1.0), max(float(height), 1.0)
    factor = min(1.0, max_edge / max(width, height))
    return max(1, round(width * factor)), max(1, round(height * factor))


def display_for_rect(rect: Rect, displays: Sequence[DisplayInfo]) -> DisplayInfo | None:
    """The display showing most of ``rect`` (or the one nearest to it). ``None`` if there
    are no displays."""
    best: DisplayInfo | None = None
    best_area = 0.0
    for display in displays:
        overlap = display.frame.intersection(rect)
        area = overlap.area if overlap is not None else 0.0
        if area > best_area:
            best, best_area = display, area
    if best is not None:
        return best
    return display_for_point(rect.center, displays)


def display_for_point(point: Point, displays: Sequence[DisplayInfo]) -> DisplayInfo | None:
    """The display containing ``point``, else the nearest one (``None`` without displays)."""
    for display in displays:
        if display.frame.contains(point):
            return display
    if not displays:
        return None

    def distance(display: DisplayInfo) -> float:
        center = display.frame.center
        return math.hypot(center.x - point.x, center.y - point.y)

    return min(displays, key=distance)


def point_on_any_display(point: Point, displays: Sequence[DisplayInfo]) -> bool:
    """True if ``point`` lies on one of ``displays`` (edges included)."""
    for display in displays:
        frame = display.frame
        if frame.x <= point.x <= frame.x + frame.w and frame.y <= point.y <= frame.y + frame.h:
            return True
    return False


def normalized_box_to_rect(x: float, y: float, w: float, h: float, region: Rect) -> Rect:
    """Convert a Vision bounding box (normalized 0…1, origin bottom-left) to global points
    inside ``region`` (the screen area the image shows)."""
    return Rect(
        region.x + x * region.w,
        region.y + (1.0 - y - h) * region.h,
        w * region.w,
        h * region.h,
    )


def is_visible(frame: Rect | None, clip: Rect | None) -> bool:
    """Visibility heuristic for one AX node: a node with a real size that lies entirely
    outside its clipping scroll area is not visible. Unknown frames count as visible."""
    if frame is None or clip is None or frame.w <= 0 or frame.h <= 0:
        return True
    return frame.intersects(clip)


def node_visible(role: str, frame: Rect | None, clip: Rect | None) -> bool:
    """:func:`is_visible`, plus: a menu (``AXMenu``) without an on-screen frame is closed, so
    neither it nor its items are visible (closed pop-up and menu-button menus stay in the AX
    tree)."""
    if role == "AXMenu" and (frame is None or frame.w <= 0 or frame.h <= 0):
        return False
    return is_visible(frame, clip)


CLIP_ROLES: frozenset[str] = frozenset({"AXScrollArea"})
"""Roles whose frame clips what their descendants can show."""
FLOATING_ROLES: frozenset[str] = frozenset({"AXPopover", "AXMenu"})
"""Roles that float above their parent and are not clipped by it."""


def child_clip(role: str, frame: Rect | None, clip: Rect | None) -> Rect | None:
    """The clip rect that applies to the children of a node."""
    if role in FLOATING_ROLES:
        return None
    if role in CLIP_ROLES and frame is not None and frame.w > 0 and frame.h > 0:
        if clip is None:
            return frame
        return clip.intersection(frame) or Rect(frame.x, frame.y, 0.0, 0.0)
    return clip


# ================================================================================= menus

_MENU_LIST_LIMIT = 25


def normalize_menu_title(text: str) -> str:
    """Casefold, treat ``…`` and ``...`` as the same, and collapse whitespace."""
    cleaned = text.replace("\u00a0", " ").replace("…", " ").replace("...", " ")
    return " ".join(cleaned.casefold().split())


def _menu_not_found(part: str, location: str, titles: Sequence[str]) -> AXPilotError:
    shown = [t for t in titles if t][:_MENU_LIST_LIMIT]
    more = len([t for t in titles if t]) - len(shown)
    available = ", ".join(shown) + (f", … ({more} more)" if more > 0 else "") if shown else "(empty)"
    return AXPilotError(
        ErrorCode.MENU_NOT_FOUND,
        f"No menu item {part!r} in {location}.",
        hint=f"Available: {available}.",
        details={"available": shown},
    )


def match_menu_title(titles: Sequence[str], wanted: str, location: str) -> int:
    """Index of the menu title matching ``wanted``.

    An exact match (after :func:`normalize_menu_title`) wins; otherwise a prefix match is
    accepted when exactly one title starts with ``wanted``. Empty titles (separators) never
    match. Raises ``MENU_NOT_FOUND`` when nothing (or more than one prefix) matches.
    """
    target = normalize_menu_title(wanted)
    normalized = [normalize_menu_title(t) if t else "" for t in titles]
    if target:
        for index, title in enumerate(normalized):
            if title and title == target:
                return index
        prefix = [index for index, title in enumerate(normalized) if title and title.startswith(target)]
        if len(prefix) == 1:
            return prefix[0]
        if prefix:
            names = ", ".join(titles[index] for index in prefix)
            raise AXPilotError(
                ErrorCode.MENU_NOT_FOUND,
                f"{wanted!r} is ambiguous in {location}: {names}.",
                hint="Use more of the item's title.",
                details={"candidates": [titles[index] for index in prefix]},
            )
    raise _menu_not_found(wanted, location, titles)


MENU_MOD_SHIFT = 1
MENU_MOD_OPTION = 2
MENU_MOD_CONTROL = 4
MENU_MOD_NO_COMMAND = 8


def decode_menu_modifiers(mask: int | None) -> frozenset[str]:
    """Decode ``AXMenuItemCmdModifiers``: 0 means ⌘ alone; bit 0 adds ⇧, bit 1 ⌥, bit 2 ⌃,
    and bit 3 removes ⌘."""
    value = int(mask or 0)
    mods: set[str] = set()
    if not value & MENU_MOD_NO_COMMAND:
        mods.add("cmd")
    if value & MENU_MOD_SHIFT:
        mods.add("shift")
    if value & MENU_MOD_OPTION:
        mods.add("option")
    if value & MENU_MOD_CONTROL:
        mods.add("ctrl")
    return frozenset(mods)


MENU_GLYPH_KEYS: dict[int, str] = {
    0x02: "tab",  # kMenuTabRightGlyph
    0x04: "keypad_enter",  # kMenuEnterGlyph
    0x09: "space",  # kMenuSpaceGlyph
    0x0A: "forward_delete",  # kMenuDeleteRightGlyph
    0x0B: "return",  # kMenuReturnGlyph
    0x0D: "return",  # kMenuNonmarkingReturnGlyph
    0x17: "delete",  # kMenuDeleteLeftGlyph
    0x1B: "escape",  # kMenuEscapeGlyph
    0x1C: "keypad_clear",  # kMenuClearGlyph
    0x62: "page_up",  # kMenuPageUpGlyph
    0x63: "caps_lock",  # kMenuCapsLockGlyph
    0x64: "left",  # kMenuLeftArrowGlyph
    0x65: "right",  # kMenuRightArrowGlyph
    0x66: "home",  # kMenuNorthwestArrowGlyph
    0x67: "help",  # kMenuHelpGlyph
    0x68: "up",  # kMenuUpArrowGlyph
    0x69: "end",  # kMenuSoutheastArrowGlyph
    0x6A: "down",  # kMenuDownArrowGlyph
    0x6B: "page_down",  # kMenuPageDownGlyph
    **{0x6F + n: f"f{n + 1}" for n in range(12)},  # kMenuF1Glyph … kMenuF12Glyph
    0x87: "f13",
    0x88: "f14",
    0x89: "f15",
    0x8F: "f16",
    0x90: "f17",
    0x91: "f18",
    0x92: "f19",
}
"""Carbon ``kMenu*Glyph`` codes (``AXMenuItemCmdGlyph``) → canonical key names."""

_FUNCTION_KEY_CHARS: dict[int, str] = {
    0xF700: "up",
    0xF701: "down",
    0xF702: "left",
    0xF703: "right",
    **{0xF704 + n: f"f{n + 1}" for n in range(20)},
    0xF728: "forward_delete",
    0xF729: "home",
    0xF72B: "end",
    0xF72C: "page_up",
    0xF72D: "page_down",
    0xF746: "help",
}
"""AppKit ``NS*FunctionKey`` private-use characters → canonical key names."""

_CONTROL_KEY_CHARS: dict[str, str] = {
    "\r": "return",
    "\n": "return",
    "\x03": "keypad_enter",
    "\t": "tab",
    "\x19": "tab",
    "\x08": "delete",
    "\x7f": "delete",
    "\x1b": "escape",
    " ": "space",
}

_NAMED_KEYCODES: dict[int, str] = {
    code: name for name, code in US_ANSI_KEYCODES.items() if len(name) > 1 and name not in ("cmd", "shift")
}
"""Virtual key codes of layout-independent keys (``AXMenuItemCmdVirtualKey``)."""


def _shortcut_key(cmd_char: str | None, glyph: int | None, virtual_key: int | None) -> str | None:
    if cmd_char:
        char = cmd_char[0]
        if char in _CONTROL_KEY_CHARS:
            return _CONTROL_KEY_CHARS[char]
        if ord(char) in _FUNCTION_KEY_CHARS:
            return _FUNCTION_KEY_CHARS[ord(char)]
        if unicodedata.category(char) not in ("Cc", "Co", "Cn"):
            return char.lower() if char.isascii() and char.isalpha() else char
    if glyph:
        name = MENU_GLYPH_KEYS.get(int(glyph))
        if name:
            return name
    if virtual_key is not None:
        return _NAMED_KEYCODES.get(int(virtual_key))
    return None


def menu_shortcut(
    cmd_char: str | None,
    modifiers: int | None,
    glyph: int | None = None,
    virtual_key: int | None = None,
) -> str | None:
    """The shortcut of a menu item in Apple glyph form (``"⇧⌘S"``), or ``None``.

    The key comes from ``AXMenuItemCmdChar`` (letters are shown uppercase, as in menus),
    then ``AXMenuItemCmdGlyph``, then ``AXMenuItemCmdVirtualKey``; modifiers from
    ``AXMenuItemCmdModifiers`` (see :func:`decode_menu_modifiers`).
    """
    key = _shortcut_key(cmd_char, glyph, virtual_key)
    if key is None:
        return None
    return to_glyphs(KeyCombo(key, decode_menu_modifiers(modifiers)))


def menu_item_shortcut(
    cmd_char: Any, modifiers: Any, glyph: Any = None, virtual_key: Any = None
) -> str | None:
    """:func:`menu_shortcut` from a menu item's raw ``AXMenuItemCmd*`` attribute values.

    The command character is used exactly as read: shortcuts such as ⌘Space, ⌘↩ and ⌃⇥ *are*
    a whitespace character, so it must not be stripped like a title. Non-integer modifiers,
    glyphs and key codes count as absent; the virtual key is only consulted when the item has
    no command character (apps often leave a stale ``0`` there)."""
    char = str(cmd_char)[:1] if isinstance(cmd_char, str) and cmd_char else None
    return menu_shortcut(
        char,
        _int_or_none(modifiers),
        _int_or_none(glyph),
        _int_or_none(virtual_key) if char is None else None,
    )


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def bring_to_front(
    *,
    request: Callable[[], None],
    ax_raise: Callable[[], None] | None,
    is_front: Callable[[], bool],
    wait: Callable[[Callable[[], bool], float], bool],
    timeout_s: float,
) -> bool:
    """Bring an app to the front; True once it is.

    macOS 14+ activation is cooperative: ``activateWithOptions:`` from a process that is not
    active itself (the AXPilot host never is) is usually ignored. So the LaunchServices
    ``request`` and, with Accessibility, ``ax_raise`` (setting ``AXFrontmost``, which is
    honoured) go out in the same attempt, followed by one wait of ``timeout_s``, instead of
    trying Accessibility only after a full timeout. ``ax_raise`` must handle its own errors."""
    request()
    if is_front():
        return True
    if ax_raise is not None:
        ax_raise()
    return wait(is_front, timeout_s)


# =============================================================================== windows


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    """What both the AX side and the CGWindowList side know about a window."""

    frame: Rect | None
    title: str | None = None


_TITLE_MISMATCH_COST = 10.0
_TITLE_ONLY_COST = 100.0
_ORDER_COST = 0.001


def _title_key(title: str | None) -> str:
    return " ".join(title.casefold().split()) if title else ""


def _frame_delta(a: Rect | None, b: Rect | None) -> float:
    if a is None or b is None:
        return math.inf
    return max(abs(a.x - b.x), abs(a.y - b.y), abs(a.w - b.w), abs(a.h - b.h))


def _pair_cost(ax: WindowGeometry, cg: WindowGeometry, rank_gap: int, tolerance: float) -> float | None:
    delta = _frame_delta(ax.frame, cg.frame)
    ax_title, cg_title = _title_key(ax.title), _title_key(cg.title)
    titles_known = bool(ax_title and cg_title)
    same_title = titles_known and ax_title == cg_title
    if delta <= tolerance:
        cost = delta + (_TITLE_MISMATCH_COST if titles_known and not same_title else 0.0)
    elif same_title:
        cost = _TITLE_ONLY_COST + (0.0 if math.isinf(delta) else min(delta, 1e6) * _ORDER_COST)
    else:
        return None
    return cost + rank_gap * _ORDER_COST


def match_windows(
    ax_windows: Sequence[WindowGeometry],
    cg_windows: Sequence[WindowGeometry],
    *,
    tolerance: float = 4.0,
) -> dict[int, int]:
    """Match AX windows to CGWindowList entries of the same process.

    There is no public API that maps an ``AXUIElement`` window to its ``CGWindowID``, so we
    pair them heuristically: frames equal within ``tolerance`` points (both APIs use global
    top-left points), preferring equal titles when both sides know them (``kCGWindowName``
    needs Screen Recording), and breaking ties between identical frames by z-order (both lists
    are front-to-back). A title-only match is accepted as a last resort, for windows that
    moved between the two reads. Returns ``{ax_index: cg_index}``, one-to-one.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, ax in enumerate(ax_windows):
        for j, cg in enumerate(cg_windows):
            cost = _pair_cost(ax, cg, abs(i - j), tolerance)
            if cost is not None:
                candidates.append((cost, i, j))
    candidates.sort()
    matched: dict[int, int] = {}
    used: set[int] = set()
    for _, i, j in candidates:
        if i in matched or j in used:
            continue
        matched[i] = j
        used.add(j)
    return matched


# ================================================================================ typing

MAX_EVENT_UTF16 = 20
"""``CGEventKeyboardSetUnicodeString`` carries at most 20 UTF-16 code units per event."""

TypeSegment = tuple[Literal["text", "key"], str]


def utf16_length(text: str) -> int:
    """Number of UTF-16 code units in ``text`` (astral characters count twice)."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _extends_cluster(char: str) -> bool:
    code = ord(char)
    return (
        unicodedata.combining(char) != 0
        or unicodedata.category(char) in ("Mn", "Me", "Mc")
        or 0xFE00 <= code <= 0xFE0F  # variation selectors
        or 0xE0100 <= code <= 0xE01EF
        or 0x1F3FB <= code <= 0x1F3FF  # emoji skin tones
        or 0xE0020 <= code <= 0xE007F  # emoji tag sequences
    )


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


ZWJ = "\u200d"
"""Zero-width joiner: glues emoji into one sequence."""


def text_clusters(text: str) -> list[str]:
    """Split ``text`` into user-perceived characters (an approximation of grapheme clusters
    that keeps combining marks, ZWJ emoji sequences and flag pairs together)."""
    clusters: list[str] = []
    joined = False
    for char in text:
        joins_previous = joined or _extends_cluster(char) or char == ZWJ
        pairs_flag = (
            bool(clusters)
            and _is_regional_indicator(char)
            and len(clusters[-1]) == 1
            and _is_regional_indicator(clusters[-1])
        )
        if clusters and (joins_previous or pairs_flag):
            clusters[-1] += char
        else:
            clusters.append(char)
        joined = char == ZWJ
    return clusters


def _split_long_cluster(cluster: str, max_units: int) -> list[str]:
    pieces: list[str] = [""]
    for char in cluster:
        if pieces[-1] and utf16_length(pieces[-1] + char) > max_units:
            pieces.append("")
        pieces[-1] += char
    return pieces


_SPECIAL_KEYS: dict[str, str] = {"\n": "return", "\t": "tab"}


def type_segments(text: str, max_units: int = MAX_EVENT_UTF16) -> list[TypeSegment]:
    """Plan how to type ``text``: Unicode chunks of at most ``max_units`` UTF-16 units that
    never split a character, plus ``return``/``tab`` key presses for line breaks and tabs.

    ``\\r\\n`` and ``\\r`` count as one line break. Other control characters raise
    ``INVALID_ARGUMENT``. ``max_units=1`` puts every character in its own chunk.
    """
    max_units = max(1, min(int(max_units), MAX_EVENT_UTF16))
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    segments: list[TypeSegment] = []
    buffer = ""
    for cluster in text_clusters(normalized):
        special = _SPECIAL_KEYS.get(cluster)
        if special is not None:
            if buffer:
                segments.append(("text", buffer))
                buffer = ""
            segments.append(("key", special))
            continue
        bad = next((c for c in cluster if unicodedata.category(c) == "Cc"), None)
        if bad is not None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Cannot type the control character U+{ord(bad):04X}.",
                hint="Use `press_keys` for special keys.",
            )
        if buffer and utf16_length(buffer + cluster) > max_units:
            segments.append(("text", buffer))
            buffer = ""
        if utf16_length(cluster) > MAX_EVENT_UTF16:
            segments.extend(("text", piece) for piece in _split_long_cluster(cluster, MAX_EVENT_UTF16))
            continue
        buffer += cluster
    if buffer:
        segments.append(("text", buffer))
    return segments


# ================================================================================ motion

MAX_SCROLL_LINES = 5000
"""Largest scroll amount accepted in one call, in lines."""


def scroll_steps(amount: float, max_step: int = 10) -> list[int]:
    """Split a line count into wheel events of at most ``max_step`` lines.

    The amount is rounded half away from zero; any non-zero amount scrolls at least one line.
    Raises ``INVALID_ARGUMENT`` for non-finite or huge amounts.
    """
    if not math.isfinite(amount):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Scroll amount must be finite, got {amount!r}.")
    lines = math.floor(abs(amount) + 0.5)
    if lines == 0 and amount != 0:
        lines = 1
    if lines > MAX_SCROLL_LINES:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Scrolling {lines} lines at once is too much (limit {MAX_SCROLL_LINES}).",
        )
    sign = -1 if amount < 0 else 1
    steps: list[int] = []
    while lines > 0:
        step = min(lines, max_step)
        steps.append(sign * step)
        lines -= step
    return steps


def wheel_deltas(dx: float, dy: float) -> tuple[list[int], list[int]]:
    """Wheel values for a scroll of ``dx``/``dy`` lines in AXPilot's convention.

    Positive ``dy`` reveals content further down, but a positive CGEvent wheel value scrolls
    *up*, so the sign is flipped (likewise for ``dx``). Returns ``(vertical, horizontal)``
    per-event deltas, padded to the same length.
    """
    vertical = [-step for step in scroll_steps(dy)]
    horizontal = [-step for step in scroll_steps(dx)]
    length = max(len(vertical), len(horizontal))
    vertical += [0] * (length - len(vertical))
    horizontal += [0] * (length - len(horizontal))
    return vertical, horizontal


def motion_steps(duration_s: float, rate_hz: float = 60.0, minimum: int = 2, maximum: int = 600) -> int:
    """Number of intermediate mouse events for a move or drag lasting ``duration_s``."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        return minimum
    return max(minimum, min(maximum, round(duration_s * rate_hz)))


def interpolate(start: Point, end: Point, steps: int) -> list[Point]:
    """``steps`` evenly spaced points from ``start`` (excluded) to ``end`` (included)."""
    steps = max(1, steps)
    return [
        Point(start.x + (end.x - start.x) * i / steps, start.y + (end.y - start.y) * i / steps)
        for i in range(1, steps + 1)
    ]


# ============================================================================ media keys

MEDIA_KEY_TYPES: dict[str, int] = {"volume_up": 0, "volume_down": 1, "mute": 7}
"""``NX_KEYTYPE_*`` codes of the media keys AXPilot can press."""


def media_key_event_data(key_type: int, down: bool) -> tuple[int, int]:
    """``(modifierFlags, data1)`` for an ``NSEventTypeSystemDefined`` (subtype 8) media key."""
    state = 0xA if down else 0xB
    return state << 8, (key_type << 16) | (state << 8)


# ======================================================================== process lineage


def parse_ps_table(text: str) -> dict[int, tuple[int, str]]:
    """Parse ``ps -axo pid=,ppid=,comm=`` output into ``{pid: (ppid, executable)}``."""
    table: dict[int, tuple[int, str]] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2])
    return table


def ancestry(pid: int, table: Mapping[int, tuple[int, str]], limit: int = 64) -> list[tuple[int, str]]:
    """``[(pid, executable), (parent pid, executable), …]`` up to launchd, cycle-safe."""
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    current = pid
    while current in table and current not in seen and len(chain) < limit:
        seen.add(current)
        parent, executable = table[current]
        chain.append((current, executable))
        if parent <= 0:
            break
        current = parent
    return chain


def app_bundle_of(executable: str) -> str | None:
    """The outermost ``.app`` bundle containing ``executable`` (``None`` if there is none,
    or if the bundle lives inside a framework, like the ``Python.app`` of framework builds)."""
    parts = PurePosixPath(executable).parts
    for index, part in enumerate(parts):
        if part.endswith(".app"):
            if any(p.endswith(".framework") for p in parts[:index]):
                return None
            return str(PurePosixPath(*parts[: index + 1]))
    return None


def responsible_app_name(executables: Iterable[str]) -> str | None:
    """Name of the app macOS most likely attributes permissions to.

    ``executables`` is the process chain from our own process up to launchd. TCC charges the
    nearest ancestor that is an app bundle (Terminal, iTerm2, Claude, AXPilot…); for helper
    apps nested in a bundle (``Code Helper (Plugin).app`` inside ``Visual Studio Code.app``)
    the outermost bundle is reported.
    """
    for executable in executables:
        bundle = app_bundle_of(executable)
        if bundle:
            return PurePosixPath(bundle).stem
    return None


# ========================================================================= installed apps

APPLICATION_DIRS: tuple[str, ...] = ("/Applications", "/System/Applications", "~/Applications")
"""Folders scanned for installed apps (each plus its ``Utilities`` subfolder)."""


def scan_app_bundles(dirs: Iterable[str | Path] = APPLICATION_DIRS) -> list[Path]:
    """``.app`` bundles directly inside each folder and its ``Utilities`` subfolder."""
    found: list[Path] = []
    seen: set[str] = set()
    for raw in dirs:
        base = Path(raw).expanduser()
        for folder in (base, base / "Utilities"):
            try:
                entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
            except OSError:
                continue
            for entry in entries:
                key = str(entry)
                if entry.suffix == ".app" and entry.is_dir() and key not in seen:
                    seen.add(key)
                    found.append(entry)
    return found


@dataclass(frozen=True, slots=True)
class BundleInfo:
    """The parts of an app's ``Info.plist`` that AXPilot uses."""

    path: str
    name: str
    bundle_id: str | None
    bundle_name: str | None = None
    display_name: str | None = None

    def matches_name(self, query: str) -> bool:
        """True if ``query`` equals the file name, bundle name or display name (casefolded)."""
        q = query.strip().casefold().removesuffix(".app")
        names = (self.name, self.bundle_name, self.display_name)
        return bool(q) and any(n is not None and n.casefold() == q for n in names)


def read_bundle_info(path: str | Path) -> BundleInfo:
    """Read ``Contents/Info.plist`` of an app bundle (missing or broken plists are tolerated:
    the bundle id is then ``None``)."""
    bundle = Path(path)
    data: dict[str, Any] = {}
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as fh:
            loaded = plistlib.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, plistlib.InvalidFileException, ValueError):
        data = {}

    def text(key: str) -> str | None:
        value = data.get(key)
        if not isinstance(value, str):
            return None
        return value.strip() or None

    return BundleInfo(
        path=str(bundle),
        name=bundle.name.removesuffix(".app"),
        bundle_id=text("CFBundleIdentifier"),
        bundle_name=text("CFBundleName"),
        display_name=text("CFBundleDisplayName"),
    )


# ===================================================================== Chromium / Electron

CHROMIUM_BUNDLE_PREFIXES: tuple[str, ...] = (
    "com.google.Chrome",
    "org.chromium.Chromium",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "com.vivaldi.Vivaldi",
    "com.operasoftware.Opera",
    "company.thebrowser.",
    "com.openai.atlas",
)
"""Bundle-id prefixes of Chromium-based browsers (Chrome, Edge, Brave, Vivaldi, Opera, Arc…)."""

ChromiumKind = Literal["electron", "chromium"]


def chromium_kind(bundle_path: str | None, bundle_id: str | None) -> ChromiumKind | None:
    """``"electron"`` for Electron apps, ``"chromium"`` for Chromium-based browsers and CEF
    apps, else ``None``. Detection looks at the bundle id and the bundle's frameworks."""
    if bundle_path:
        frameworks = Path(bundle_path) / "Contents" / "Frameworks"
        if (frameworks / "Electron Framework.framework").is_dir():
            return "electron"
        if (frameworks / "Chromium Embedded Framework.framework").is_dir():
            return "chromium"
    if bundle_id and bundle_id.startswith(CHROMIUM_BUNDLE_PREFIXES):
        return "chromium"
    if bundle_path:
        frameworks = Path(bundle_path) / "Contents" / "Frameworks"
        patterns = (
            "*.framework/Helpers/* Helper (Renderer).app",
            "*.framework/Versions/*/Helpers/* Helper (Renderer).app",
        )
        try:
            for pattern in patterns:
                if next(frameworks.glob(pattern), None) is not None:
                    return "chromium"
        except OSError:
            return None
    return None


# ================================================================================ misc

AUTH_AGENT_BUNDLE_IDS: frozenset[str] = frozenset(
    {
        "com.apple.SecurityAgent",
        "com.apple.LocalAuthentication.UIAgent",
        "com.apple.coreautha",
        "com.apple.CoreAuthUI",
    }
)
AUTH_AGENT_NAMES: frozenset[str] = frozenset(
    {"SecurityAgent", "coreautha", "LocalAuthenticationUIAgent", "CoreAuthUI"}
)


def is_auth_agent(name: str | None, bundle_id: str | None) -> bool:
    """True for the processes that show system authentication prompts (never automated)."""
    return (bundle_id is not None and bundle_id in AUTH_AGENT_BUNDLE_IDS) or (
        name is not None and name in AUTH_AGENT_NAMES
    )


_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")


def looks_like_url(target: str) -> bool:
    """True for ``scheme:…`` strings (``https://…``, ``mailto:…``, ``file:///…``)."""
    return bool(_URL_RE.match(target.strip())) and not target.strip().startswith(("/", "~"))


def looks_like_bundle_id(text: str) -> bool:
    """True for reverse-DNS identifiers such as ``com.apple.Safari`` (not ``Safari.app``)."""
    value = text.strip()
    return bool(_BUNDLE_ID_RE.match(value)) and not value.casefold().endswith(".app")


def looks_like_app_path(text: str) -> bool:
    """True for paths to app bundles (``/Applications/Safari.app``, ``~/Applications/X.app``)."""
    value = text.strip().rstrip("/")
    return value.startswith(("/", "~")) or "/" in value


NOTIFY_TITLE_MAX = 120
NOTIFY_MESSAGE_MAX = 1000


def truncate_text(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, ending with ``…`` when shortened."""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def notification_script(title: str, message: str, subtitle: str | None = None) -> str:
    """AppleScript that posts a user notification, with every string safely escaped."""
    if not (title or "").strip() and not (message or "").strip():
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A notification needs a title or a message.")
    parts = [f"display notification {applescript_string(truncate_text(message or '', NOTIFY_MESSAGE_MAX))}"]
    if title:
        parts.append(f"with title {applescript_string(truncate_text(title, NOTIFY_TITLE_MAX))}")
    if subtitle:
        parts.append(f"subtitle {applescript_string(truncate_text(subtitle, NOTIFY_TITLE_MAX))}")
    return " ".join(parts)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def is_png(data: bytes) -> bool:
    """True if ``data`` starts with the PNG signature."""
    return data[:8] == PNG_SIGNATURE


_TRUE_WORDS = frozenset({"true", "1", "yes", "on", "checked", "selected"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off", "unchecked", "deselected"})


def parse_toggle(value: str | float | bool) -> bool:
    """Interpret ``value`` as on/off for checkboxes, switches and radio buttons."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    word = str(value).strip().casefold()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Expected on/off (true/false) for a checkbox, got {value!r}.",
    )


def coerce_value(current: Any, value: str | float | bool, role: str) -> Any:
    """Convert ``value`` to the type of the element's current ``AXValue``.

    Numbers stay numbers (``"0.5"`` becomes ``0.5`` for a slider), text stays text. Raises
    ``INVALID_ARGUMENT`` when a numeric element gets something that is not a number.
    """
    if isinstance(current, bool):
        return parse_toggle(value)
    if isinstance(current, int | float) or role in ("AXSlider", "AXIncrementor", "AXLevelIndicator"):
        if isinstance(value, bool):
            return int(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"A {role} needs a number, got {value!r}."
            ) from None
        if not math.isfinite(number):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"A {role} needs a finite number.")
        if isinstance(current, int) and number.is_integer():
            return int(number)
        return number
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ======================================================================= keyboard layout

KeyPosition = tuple[int, bool, bool]
"""``(virtual key code, needs shift, needs option)`` for typing a character."""

UC_SHIFT_STATE = 0x02
"""``(shiftKey >> 8) & 0xFF``: the shift bit of ``UCKeyTranslate``'s modifier state."""
UC_OPTION_STATE = 0x08
"""``(optionKey >> 8) & 0xFF``: the option bit of ``UCKeyTranslate``'s modifier state."""

LAYOUT_STATES: tuple[tuple[int, bool, bool], ...] = (
    (0, False, False),
    (UC_SHIFT_STATE, True, False),
    (UC_OPTION_STATE, False, True),
    (UC_SHIFT_STATE | UC_OPTION_STATE, True, True),
)
"""Modifier states tried for every key, simplest first (a character keeps the first match)."""

KEYPAD_KEYCODES: frozenset[int] = frozenset(
    {0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51, *range(0x52, 0x5A), 0x5B, 0x5C}
)
"""Numeric keypad keys: used for a character only when no main-keyboard key types it."""


def _printable(text: str) -> bool:
    return len(text) == 1 and unicodedata.category(text) not in ("Cc", "Cf", "Co", "Cn", "Cs", "Zl", "Zp")


def build_char_map(
    translate: Callable[[int, int], str],
    keycodes: Iterable[int] = range(128),
) -> dict[str, KeyPosition]:
    """Map each character the layout can type to the simplest key that types it.

    ``translate(keycode, modifier_state)`` returns what the key produces (``UCKeyTranslate``
    with dead keys disabled). Main-keyboard keys win over keypad keys, and fewer modifiers win
    over more (so ``*`` is shift+8 on US, not the keypad ``*``).
    """
    codes = list(keycodes)
    main = [c for c in codes if c not in KEYPAD_KEYCODES]
    keypad = [c for c in codes if c in KEYPAD_KEYCODES]
    mapping: dict[str, KeyPosition] = {}
    for group in (main, keypad):
        for state, shift, option in LAYOUT_STATES:
            for code in group:
                char = translate(code, state)
                if _printable(char) and char != " " and char not in mapping:
                    mapping[char] = (code, shift, option)
    return mapping


def us_ansi_char_map() -> dict[str, KeyPosition]:
    """The US-ANSI character map (used when the current layout cannot be read)."""
    mapping: dict[str, KeyPosition] = {}
    for char, code in US_ANSI_KEYCODES.items():
        if len(char) == 1:
            mapping[char] = (code, False, False)
            if char.isalpha():
                mapping[char.upper()] = (code, True, False)
    for shifted, base in US_ANSI_SHIFTED.items():
        mapping[shifted] = (US_ANSI_KEYCODES[base], True, False)
    return mapping


@dataclass(frozen=True, slots=True)
class KeyboardLayout:
    """Characters of one keyboard layout and the keys that type them."""

    source_id: str | None
    chars: Mapping[str, KeyPosition]
    fallback: bool = False
    """True when this is the built-in US-ANSI map, not the user's real layout."""

    def lookup(self, char: str) -> KeyPosition | None:
        """The key for ``char`` (letters also match their other case plus/minus shift)."""
        found = self.chars.get(char)
        if found is not None:
            return found
        if char.isalpha() and char.lower() != char and char.lower() in self.chars:
            code, _, option = self.chars[char.lower()]
            return code, True, option
        return None


# ============================================================================ HID idle


class IdleEstimator:
    """How long the *user* has been idle, even though our own events are hardware-level.

    ``CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, …)`` may count
    events we post at ``kCGHIDEventTap``. So before each post we note when the newest HID event
    happened: if it is newer than our previous post, it was the user's. When the newest HID event
    is (within ``slack_s``) one of ours, the user's idle time is measured from their last
    activity instead.

    ``read_idle()`` returns seconds since the last HID event; ``clock()`` is monotonic.
    """

    def __init__(
        self,
        read_idle: Callable[[], float],
        clock: Callable[[], float],
        slack_s: float = 0.1,
    ) -> None:
        self._read_idle = read_idle
        self._clock = clock
        self._slack = slack_s
        self._last_post: float | None = None
        self._user_last: float | None = None

    def before_post(self) -> None:
        """Call right before posting a synthetic event."""
        now = self._clock()
        last_hid = now - max(self._read_idle(), 0.0)
        if self._last_post is None or last_hid > self._last_post + self._slack:
            self._user_last = last_hid

    def after_post(self) -> None:
        """Call right after posting a synthetic event."""
        self._last_post = self._clock()

    def idle_seconds(self) -> float:
        """Seconds since the user's last mouse/keyboard event."""
        now = self._clock()
        idle = max(self._read_idle(), 0.0)
        last_hid = now - idle
        ours_is_newest = self._last_post is not None and last_hid <= self._last_post + self._slack
        if ours_is_newest and self._user_last is not None:
            return max(now - self._user_last, idle)
        return idle


# ============================================================================ screenshots


@dataclass(frozen=True, slots=True)
class CapturePlan:
    """What to capture: the screen ``region`` (global points) on ``display``, the output size
    in pixels, and for rect captures the ``source_rect`` relative to the display's origin."""

    region: Rect
    display: DisplayInfo
    width: int
    height: int
    source_rect: Rect | None = None


def capture_plan(
    displays: Sequence[DisplayInfo],
    *,
    display_id: int | None = None,
    window_frame: Rect | None = None,
    rect: Rect | None = None,
    max_edge: int = 1280,
) -> CapturePlan:
    """Plan a screenshot of a rect, a window or a display (in that order of precedence).

    A rect spanning several displays is cut to the display showing most of it. The output
    size is the region at the display's backing scale, downscaled to ``max_edge``. Raises
    ``INVALID_ARGUMENT`` for empty or off-screen rects and ``DISPLAY_NOT_FOUND``.
    """
    if not displays:
        raise AXPilotError(ErrorCode.DISPLAY_NOT_FOUND, "No display is connected.")
    source_rect: Rect | None = None
    if rect is not None:
        values = (rect.x, rect.y, rect.w, rect.h)
        if not all(math.isfinite(v) for v in values) or rect.w <= 0 or rect.h <= 0:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "A capture rect must have a positive, finite size."
            )
        display = display_for_rect(rect, displays)
        region = display.frame.intersection(rect) if display is not None else None
        if display is None or region is None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"The rect {rect.to_list()} is outside every display."
            )
        source_rect = Rect(region.x - display.frame.x, region.y - display.frame.y, region.w, region.h)
    elif window_frame is not None:
        display = display_for_rect(window_frame, displays)
        if display is None:  # pragma: no cover - displays is not empty
            raise AXPilotError(ErrorCode.DISPLAY_NOT_FOUND, "No display shows the window.")
        region = window_frame
    else:
        if display_id is None:
            display = next((d for d in displays if d.main), displays[0])
        else:
            found = next((d for d in displays if d.display_id == display_id), None)
            if found is None:
                raise AXPilotError(
                    ErrorCode.DISPLAY_NOT_FOUND,
                    f"No display has id {display_id}.",
                    hint="Use `list_displays` to see display ids.",
                )
            display = found
        region = display.frame
    width, height = fit_size(region.w * display.scale, region.h * display.scale, max_edge)
    return CapturePlan(region=region, display=display, width=width, height=height, source_rect=source_rect)


# ==================================================================================== OCR


def resolve_languages(requested: Sequence[str], supported: Sequence[str]) -> list[str]:
    """Map requested OCR languages onto Vision's identifiers (``"en"`` → ``"en-US"``,
    case-insensitive). Raises ``INVALID_ARGUMENT`` naming the unsupported ones."""
    by_key = {code.casefold(): code for code in supported}
    resolved: list[str] = []
    unknown: list[str] = []
    for raw in requested:
        code = raw.strip().replace("_", "-")
        key = code.casefold()
        match = by_key.get(key) or next((s for k, s in by_key.items() if k.split("-")[0] == key), None)
        if match is None:
            unknown.append(raw)
        elif match not in resolved:
            resolved.append(match)
    if unknown:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            "Unsupported OCR language(s): " + ", ".join(unknown) + ".",
            hint="Supported: " + ", ".join(supported) + ".",
        )
    return resolved


# ============================================================================ hit testing


class HitWindow(Protocol):
    """What :func:`hit_test` needs to know about a window (``CGWindow`` satisfies it)."""

    @property
    def pid(self) -> int: ...
    @property
    def owner(self) -> str: ...
    @property
    def frame(self) -> Rect: ...
    @property
    def layer(self) -> int: ...
    @property
    def alpha(self) -> float: ...


WINDOW_SERVER_OWNER = "Window Server"
MENU_BAR_LAYERS: frozenset[int] = frozenset({24, 25})
"""``kCGMainMenuWindowLevel`` and ``kCGStatusWindowLevel``."""
OVERLAY_COVERAGE = 0.95
"""A raised window covering this much of its display is treated as a click-through overlay."""

_FRONTMOST = "frontmost"


def hit_test(
    windows: Sequence[HitWindow],
    point: Point,
    *,
    own_pid: int,
    displays: Sequence[DisplayInfo],
) -> HitWindow | Literal["frontmost"] | None:
    """The window that receives a click at ``point``, from a front-to-back window list.

    Windows of every level count (menus, popovers, panels, status items and the Dock all take
    clicks), except AXPilot's own windows, fully transparent ones, desktop-level ones, and
    raised windows covering a whole display (overlays from screen tools that let clicks
    through). The menu bar belongs to the frontmost app (``"frontmost"``). ``None`` means the
    desktop.
    """
    for window in windows:
        if window.pid == own_pid or window.alpha <= 0 or window.layer < 0 or not window.frame.contains(point):
            continue
        if window.owner == WINDOW_SERVER_OWNER:
            if window.layer in MENU_BAR_LAYERS:
                return _FRONTMOST
            continue
        if window.layer > 0:
            display = display_for_rect(window.frame, displays)
            if display is not None and display.frame.area > 0:
                overlap = display.frame.intersection(window.frame)
                if overlap is not None and overlap.area >= OVERLAY_COVERAGE * display.frame.area:
                    continue
        return window
    return None
