"""Accessibility attribute names and conversion of AX values to plain Python.

Permission: reading attributes needs **Accessibility**.

AX returns Foundation/CoreFoundation objects through PyObjC (``NSString`` subclasses of
``str``, ``NSNumber`` as ``int``/``float``/``bool``, ``NSArray``, ``NSURL``,
``NSAttributedString``, ``AXValueRef`` structs, nested ``AXUIElementRef``\\ s…). The functions
here turn them into JSON-safe Python values without ever sending more than the one extra
message needed to summarize a nested element.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import Foundation

from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.ax import AX, AXCallError, AXElement
from axpilot.models import Rect

# ------------------------------------------------------------------ attribute names
ROLE = "AXRole"
SUBROLE = "AXSubrole"
ROLE_DESCRIPTION = "AXRoleDescription"
TITLE = "AXTitle"
DESCRIPTION = "AXDescription"
VALUE = "AXValue"
VALUE_DESCRIPTION = "AXValueDescription"
HELP = "AXHelp"
IDENTIFIER = "AXIdentifier"
DOM_IDENTIFIER = "AXDOMIdentifier"
PLACEHOLDER = "AXPlaceholderValue"
TITLE_UI_ELEMENT = "AXTitleUIElement"
URL = "AXURL"
ENABLED = "AXEnabled"
FOCUSED = "AXFocused"
SELECTED = "AXSelected"
EXPANDED = "AXExpanded"
POSITION = "AXPosition"
SIZE = "AXSize"
CHILDREN = "AXChildren"
VISIBLE_CHILDREN = "AXVisibleChildren"
ROWS = "AXRows"
VISIBLE_ROWS = "AXVisibleRows"
PARENT = "AXParent"
WINDOW = "AXWindow"
WINDOWS = "AXWindows"
MAIN = "AXMain"
MINIMIZED = "AXMinimized"
FULL_SCREEN = "AXFullScreen"
MODAL = "AXModal"
FOCUSED_WINDOW = "AXFocusedWindow"
MAIN_WINDOW = "AXMainWindow"
FOCUSED_UI_ELEMENT = "AXFocusedUIElement"
FOCUSED_APPLICATION = "AXFocusedApplication"
FRONTMOST = "AXFrontmost"
HIDDEN = "AXHidden"
MENU_BAR = "AXMenuBar"
EXTRAS_MENU_BAR = "AXExtrasMenuBar"
CLOSE_BUTTON = "AXCloseButton"
ZOOM_BUTTON = "AXZoomButton"
MINIMIZE_BUTTON = "AXMinimizeButton"
FULL_SCREEN_BUTTON = "AXFullScreenButton"
SELECTED_TEXT = "AXSelectedText"
NUMBER_OF_CHARACTERS = "AXNumberOfCharacters"
STRING_FOR_RANGE = "AXStringForRange"
MENU_ITEM_CMD_CHAR = "AXMenuItemCmdChar"
MENU_ITEM_CMD_MODIFIERS = "AXMenuItemCmdModifiers"
MENU_ITEM_CMD_GLYPH = "AXMenuItemCmdGlyph"
MENU_ITEM_CMD_VIRTUAL_KEY = "AXMenuItemCmdVirtualKey"
MENU_ITEM_MARK_CHAR = "AXMenuItemMarkChar"
MANUAL_ACCESSIBILITY = "AXManualAccessibility"
ENHANCED_USER_INTERFACE = "AXEnhancedUserInterface"

# ------------------------------------------------------------------ actions
PRESS = "AXPress"
CANCEL = "AXCancel"
RAISE = "AXRaise"
SCROLL_TO_VISIBLE = "AXScrollToVisible"

SECURE_SUBROLE = "AXSecureTextField"

NODE_ATTRIBUTES: tuple[str, ...] = (
    ROLE,
    SUBROLE,
    TITLE,
    DESCRIPTION,
    HELP,
    IDENTIFIER,
    DOM_IDENTIFIER,
    PLACEHOLDER,
    TITLE_UI_ELEMENT,
    URL,
    ENABLED,
    FOCUSED,
    SELECTED,
    EXPANDED,
    POSITION,
    SIZE,
)
"""Read in one batch for every tree node. ``AXValue`` is deliberately *not* here: it is read
in a second step only once we know the element is not a secure text field."""

TEXT_MAX_CHARS = 500
"""Longest title/description/help text kept per attribute."""


def text(value: Any, limit: int = TEXT_MAX_CHARS) -> str | None:
    """A stripped, truncated string, or ``None`` for empty/non-text values."""
    if value is None:
        return None
    if isinstance(value, Foundation.NSAttributedString):
        value = value.string()
    if not isinstance(value, str):
        return None
    cleaned = str(value).strip()
    return h.truncate_text(cleaned, limit) if cleaned else None


def flag(value: Any, default: bool = False) -> bool:
    """A boolean attribute (``NSNumber`` 0/1 or ``bool``) with a default for missing ones."""
    if value is None:
        return default
    if isinstance(value, bool | int | float):
        return bool(value)
    return default


def optional_flag(value: Any) -> bool | None:
    """Like :func:`flag`, but ``None`` when the attribute is missing."""
    return bool(value) if isinstance(value, bool | int | float) else None


def point_of(ax: AX, value: Any) -> tuple[float, float] | None:
    if value is None or not ax.is_value(value):
        return None
    decoded = ax.decode_value(value)
    if decoded is None or decoded[0] != h.AXVALUE_CGPOINT:
        return None
    return decoded[1][0], decoded[1][1]


def size_of(ax: AX, value: Any) -> tuple[float, float] | None:
    if value is None or not ax.is_value(value):
        return None
    decoded = ax.decode_value(value)
    if decoded is None or decoded[0] != h.AXVALUE_CGSIZE:
        return None
    return decoded[1][0], decoded[1][1]


def frame_of(ax: AX, attrs: dict[str, Any]) -> Rect | None:
    """The element frame in global points from batched ``AXPosition``/``AXSize``."""
    return h.frame_from(point_of(ax, attrs.get(POSITION)), size_of(ax, attrs.get(SIZE)))


def url_text(value: Any) -> str | None:
    if isinstance(value, Foundation.NSURL):
        absolute = value.absoluteString()
        return str(absolute) if absolute is not None else None
    return text(value, 2000)


def label_of(ax: AX, element: AXElement | None) -> str | None:
    """Text of a label element (``AXTitleUIElement``): its value or title."""
    if element is None or not ax.is_element(element):
        return None
    try:
        attrs = ax.get_many(element, (VALUE, TITLE))
    except AXCallError:
        return None
    return text(attrs.get(VALUE)) or text(attrs.get(TITLE))


def _finite(number: float) -> float | None:
    return number if math.isfinite(number) else None


def _date_text(value: Any) -> str:
    seconds = float(value.timeIntervalSince1970())
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC).isoformat()


def node_value(ax: AX, value: Any, max_chars: int) -> Any:
    """A node's ``AXValue`` as ``str``/``int``/``float``/``bool``/``list`` (or ``None``).

    Nested elements (e.g. the selected tab of a tab group) become ``None``; structs become
    lists of numbers.
    """
    return to_python(ax, value, max_chars=max_chars, elements="drop")


def to_python(
    ax: AX,
    value: Any,
    *,
    max_chars: int = 2000,
    elements: str = "summary",
    depth: int = 0,
    max_items: int = 50,
) -> Any:
    """Convert an AX/Foundation value to JSON-safe Python.

    ``elements`` decides what nested ``AXUIElementRef`` values become: ``"summary"``
    (``{"role": …, "title": …}``, one extra AX message each) or ``"drop"`` (``None``).
    Arrays are cut to ``max_items`` and nesting to three levels.
    """
    if value is None or isinstance(value, Foundation.NSNull):
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return _finite(float(value))
    if isinstance(value, str):
        return h.truncate_text(str(value), max_chars)
    if isinstance(value, Foundation.NSAttributedString):
        return h.truncate_text(str(value.string()), max_chars)
    if isinstance(value, Foundation.NSURL):
        return url_text(value)
    if isinstance(value, Foundation.NSDate):
        return _date_text(value)
    if ax.is_value(value):
        decoded = ax.decode_value(value)
        return list(decoded[1]) if decoded is not None else None
    if ax.is_element(value):
        return element_summary(ax, value) if elements == "summary" and depth < 2 else None
    if depth >= 3:
        return None
    if isinstance(value, (Foundation.NSArray, list, tuple)):
        items = list(value)
        converted = [
            to_python(ax, item, max_chars=max_chars, elements=elements, depth=depth + 1)
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            converted.append(f"… {len(items) - max_items} more")
        return converted
    if isinstance(value, (Foundation.NSDictionary, dict)):
        return {
            str(key): to_python(ax, item, max_chars=max_chars, elements=elements, depth=depth + 1)
            for key, item in list(value.items())[:max_items]
        }
    return h.truncate_text(str(value), max_chars)


def element_summary(ax: AX, element: AXElement) -> dict[str, Any] | None:
    """``{"role", "subrole", "title"}`` of a nested element (one batched read)."""
    try:
        attrs = ax.get_many(element, (ROLE, SUBROLE, TITLE, DESCRIPTION))
    except AXCallError:
        return None
    summary = {
        "role": text(attrs.get(ROLE)),
        "subrole": text(attrs.get(SUBROLE)),
        "title": text(attrs.get(TITLE)) or text(attrs.get(DESCRIPTION)),
    }
    return {key: value for key, value in summary.items() if value is not None}
