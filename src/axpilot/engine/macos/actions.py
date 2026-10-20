"""Operations on a single accessibility element: validity, re-reading, inspection, actions,
values, focus and text insertion.

Permission: **Accessibility**.

Timeouts on *actions*: when an app runs a modal loop in response to an action (``AXPress``
on "Save As…" opens a dialog with ``runModal``), the AX request only returns once the modal
ends, so the call fails with ``kAXErrorCannotComplete`` after the messaging timeout even
though the action was delivered. Retrying would press twice, so a timed-out action, value
change or text insertion is treated as delivered (and logged); the caller's follow-up
snapshot shows what happened. Reads that time out raise ``TIMEOUT``.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

import Foundation

from axpilot.engine.macos import attributes as A
from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.ax import AX, AXCallError, AXElement, ax_failure
from axpilot.engine.macos.tree import NodeReader
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import RawNode
from axpilot.perception.roles import short_role

_log = logging.getLogger(__name__)

TOGGLE_ROLES: frozenset[str] = frozenset({"AXCheckBox", "AXRadioButton", "AXDisclosureTriangle", "AXSwitch"})
"""Roles whose value is changed by pressing them (``AXValue`` is rarely settable)."""
POPUP_ROLES: frozenset[str] = frozenset({"AXPopUpButton"})
"""Roles whose value is chosen from a menu."""
DATE_ROLES: frozenset[str] = frozenset({"AXDateTimeArea", "AXDateField", "AXTimeField"})
"""Date pickers: their value is an ``NSDate`` (the server sends ISO 8601 text)."""

ARRAY_ATTRIBUTES: frozenset[str] = frozenset(
    {
        A.CHILDREN,
        A.VISIBLE_CHILDREN,
        A.ROWS,
        A.VISIBLE_ROWS,
        "AXColumns",
        "AXVisibleColumns",
        "AXSelectedChildren",
        "AXSelectedRows",
        "AXSelectedColumns",
        "AXSelectedCells",
        "AXVisibleCells",
        "AXChildrenInNavigationOrder",
        "AXLinkedUIElements",
        "AXServesAsTitleForUIElements",
        "AXContents",
        "AXTabs",
        "AXWindows",
        "AXSections",
        "AXCustomContent",
    }
)
"""Attributes reported as counts by :func:`element_attributes` (they can be huge)."""

SECRET_ATTRIBUTES: frozenset[str] = frozenset(
    {A.VALUE, A.SELECTED_TEXT, "AXVisibleCharacterRange", "AXSelectedTextRange", "AXSelectedTextRanges"}
)
"""Never read on secure text fields."""

MAX_INSPECT_ATTRIBUTES = 80
SETTABLE_PROBES: tuple[str, ...] = (
    A.VALUE,
    A.FOCUSED,
    A.SELECTED,
    A.SELECTED_TEXT,
    A.EXPANDED,
    A.POSITION,
    A.SIZE,
)
POPUP_OPEN_DELAY_S = 0.15
POPUP_OPEN_WAIT_S = 0.6
"""How long a pop-up's menu may take to appear (polled every :data:`POPUP_OPEN_DELAY_S`)."""


def process_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def require_element(ax: AX, handle: Any) -> AXElement:
    """``handle`` if it is an ``AXUIElementRef``; ``INVALID_ARGUMENT`` otherwise."""
    if not ax.is_element(handle):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Expected an accessibility element handle, got {type(handle).__name__}.",
            hint="Use refs from a `snapshot` taken with the macOS engine.",
        )
    return handle


def describe(ax: AX, element: AXElement) -> str:
    """``'button "Save"'``-style label for messages (best effort, never raises)."""
    try:
        attrs = ax.get_many(element, (A.ROLE, A.SUBROLE, A.TITLE, A.DESCRIPTION))
    except AXCallError:
        return "the element"
    role = short_role(A.text(attrs.get(A.ROLE)) or "AXUnknown", A.text(attrs.get(A.SUBROLE)))
    name = A.text(attrs.get(A.TITLE), 60) or A.text(attrs.get(A.DESCRIPTION), 60)
    return f'{role} "{name}"' if name else role


def _fail(ax: AX, exc: AXCallError, element: AXElement, doing: str) -> AXPilotError:
    if exc.stale:
        return AXPilotError(
            ErrorCode.STALE_REF,
            f"{doing} failed: the element no longer exists.",
            details={"ax_error": h.ax_error_name(exc.code)},
        )
    return ax_failure(exc, f"{doing} on {describe(ax, element)}", timeout_s=ax.timeout_s)


# ------------------------------------------------------------------ validity & reading


def element_valid(ax: AX, handle: Any) -> bool:
    """False if the element was destroyed or its app quit."""
    if not ax.is_element(handle):
        return False
    try:
        ax.get(handle, A.ROLE)
    except AXCallError as exc:
        if exc.stale:
            return False
        if exc.permission:
            raise ax_failure(exc, "Checking an element") from exc
        if exc.code == h.AX_CANNOT_COMPLETE:
            pid = ax.pid(handle)
            return pid is not None and process_alive(pid)
        return True
    return True


def refresh_element(reader: NodeReader, handle: Any, focused: AXElement | None = None) -> RawNode:
    """Re-read one element (children left empty). Raises ``STALE_REF`` if it is gone."""
    element = require_element(reader.ax, handle)
    try:
        return reader.read(element, want_children=False, focused=focused).node
    except AXCallError as exc:
        raise _fail(reader.ax, exc, element, "Reading the element") from exc


def element_attributes(ax: AX, handle: Any, max_chars: int = 2000) -> dict[str, Any]:
    """Every readable attribute as JSON-safe values, for ``inspect``.

    Array attributes (children, rows…) are reported as ``<name>Count``; nested elements as
    ``{"role", "subrole", "title"}`` summaries; secure text fields never expose their text.
    Also adds ``AXActionNames``, ``AXSettableAttributes``, ``AXParentRole`` and
    ``AXWindowTitle``.
    """
    element = require_element(ax, handle)
    try:
        names = ax.attribute_names(element)[:MAX_INSPECT_ATTRIBUTES]
        secure = A.text(ax.get(element, A.SUBROLE)) == A.SECURE_SUBROLE
        scalar = [n for n in names if n not in ARRAY_ATTRIBUTES and not (secure and n in SECRET_ATTRIBUTES)]
        values = ax.get_many(element, scalar)
        out: dict[str, Any] = {}
        for name in scalar:
            if name in values:
                converted = A.to_python(ax, values[name], max_chars=max_chars)
                if converted is not None:
                    out[name] = converted
        for name in names:
            if name in ARRAY_ATTRIBUTES:
                count = ax.count(element, name)
                if count is not None:
                    out[f"{name}Count"] = count
        if A.CHILDREN in names:
            out["AXChildrenCount"] = out.get(f"{A.CHILDREN}Count", 0)
        out["AXActionNames"] = list(ax.actions(element))
        out["AXSettableAttributes"] = [n for n in SETTABLE_PROBES if n in names and ax.settable(element, n)]
    except AXCallError as exc:
        raise _fail(ax, exc, element, "Inspecting the element") from exc
    parent = out.get(A.PARENT)
    if isinstance(parent, dict) and parent.get("role"):
        out["AXParentRole"] = parent["role"]
    window = out.get(A.WINDOW)
    if isinstance(window, dict) and window.get("title"):
        out["AXWindowTitle"] = window["title"]
    if secure:
        out["secure"] = True
    return out


# ------------------------------------------------------------------ actions


def _enabled(ax: AX, element: AXElement) -> bool:
    return A.flag(ax.get(element, A.ENABLED), default=True)


def perform_action(ax: AX, handle: Any, action: str) -> None:
    """Perform ``action`` (``AXPress``, ``AXShowMenu``, ``AXIncrement``…).

    Raises ``ELEMENT_NOT_ACTIONABLE`` if the element is disabled or does not list the action,
    ``STALE_REF`` if it is gone.
    """
    element = require_element(ax, handle)
    if not action or not action.strip():
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The action name is empty.")
    try:
        actions = ax.actions(element)
        if actions and action not in actions:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{describe(ax, element)} does not support {action}.",
                hint="Supported actions: " + ", ".join(actions) + ".",
                details={"actions": list(actions)},
            )
        if not _enabled(ax, element):
            raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{describe(ax, element)} is disabled.")
        _deliver(ax, element, action)
    except AXCallError as exc:
        raise _fail(ax, exc, element, action) from exc


def _deliver(ax: AX, element: AXElement, action: str) -> None:
    """Perform an action, treating a timeout as delivered (see the module docstring)."""
    try:
        ax.perform(element, action)
    except AXCallError as exc:
        if exc.code == h.AX_CANNOT_COMPLETE and exc.timed_out:
            _log.info("%s: the app did not reply in time; assuming the action was delivered", action)
            return
        raise


def scroll_to_visible(ax: AX, handle: Any) -> bool:
    """``AXScrollToVisible`` if the element supports it."""
    element = require_element(ax, handle)
    try:
        if A.SCROLL_TO_VISIBLE not in ax.actions(element):
            return False
        _deliver(ax, element, A.SCROLL_TO_VISIBLE)
    except AXCallError as exc:
        raise _fail(ax, exc, element, A.SCROLL_TO_VISIBLE) from exc
    return True


# ------------------------------------------------------------------ values


def set_value(ax: AX, handle: Any, value: str | float | bool) -> None:
    """Set the element's value the way its role expects.

    Checkboxes, switches, radio buttons and disclosure triangles are pressed when their state
    differs; pop-up buttons open their menu and pick the matching item; everything else gets
    ``AXValue`` (converted to the type of the current value).
    """
    element = require_element(ax, handle)
    try:
        attrs = ax.get_many(element, (A.ROLE, A.SUBROLE, A.ENABLED))
        role = A.text(attrs.get(A.ROLE)) or "AXUnknown"
        if not A.flag(attrs.get(A.ENABLED), default=True):
            raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{describe(ax, element)} is disabled.")
        if role in TOGGLE_ROLES:
            _set_toggle(ax, element, role, value)
        elif role in POPUP_ROLES and not ax.settable(element, A.VALUE):
            select_popup_option(ax, element, str(value))
        else:
            _set_ax_value(ax, element, role, value)
    except AXCallError as exc:
        raise _fail(ax, exc, element, "Setting the value") from exc


def _set_toggle(ax: AX, element: AXElement, role: str, value: str | float | bool) -> None:
    want = h.parse_toggle(value)
    current = A.flag(ax.get(element, A.VALUE))
    if role == "AXRadioButton" and not want and current:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{describe(ax, element)} cannot be turned off directly.",
            hint="Select another radio button in the same group instead.",
        )
    if current != want:
        _deliver(ax, element, A.PRESS)


def _set_ax_value(ax: AX, element: AXElement, role: str, value: str | float | bool) -> None:
    if not ax.settable(element, A.VALUE):
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"The value of {describe(ax, element)} cannot be set.",
            hint="Use `click` or `type_text` instead.",
        )
    if role in DATE_ROLES:
        converted: Any = _ns_date(value)
    else:
        current = A.node_value(ax, ax.get(element, A.VALUE), 100)
        converted = h.coerce_value(current, value, role)
    try:
        ax.set(element, A.VALUE, converted)
    except AXCallError as exc:
        if exc.code == h.AX_CANNOT_COMPLETE and exc.timed_out:
            _log.info("AXValue: the app did not reply in time; assuming the value was set")
            return
        raise


def _ns_date(value: str | float | bool) -> Any:
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"A date picker needs an ISO 8601 date, got {value!r}."
        ) from exc
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(moment.timestamp())


def _child_with_role(ax: AX, element: AXElement, role: str) -> AXElement | None:
    for child in list(ax.get(element, A.CHILDREN) or []):
        if ax.is_element(child) and A.text(ax.get(child, A.ROLE)) == role:
            return child
    return None


def _on_screen(ax: AX, element: AXElement) -> bool:
    frame = A.frame_of(ax, ax.get_many(element, (A.POSITION, A.SIZE)))
    return frame is not None and frame.w > 0 and frame.h > 0


def _app_menu(ax: AX, element: AXElement) -> AXElement | None:
    """An open menu of ``element``'s application outside its windows (``AXMenu`` children of the
    application element): where some pop-ups and every context menu show their items."""
    pid = ax.pid(element)
    if pid is None:
        return None
    for child in list(ax.get(ax.application(pid), A.CHILDREN) or []):
        if ax.is_element(child) and A.text(ax.get(child, A.ROLE)) == A.MENU and _on_screen(ax, child):
            return child
    return None


def open_menu_of(ax: AX, popup: AXElement) -> AXElement | None:
    """The menu a pressed pop-up shows: its own ``AXMenu`` child when on screen, else an open
    application-level menu, else the (frameless) child menu itself. Polls for up to
    :data:`POPUP_OPEN_WAIT_S`; ``None`` if no menu appears."""
    deadline = time.monotonic() + POPUP_OPEN_WAIT_S
    while True:
        child = _child_with_role(ax, popup, A.MENU)
        if child is not None and _on_screen(ax, child):
            return child
        detached = _app_menu(ax, popup)
        if detached is not None:
            return detached
        if time.monotonic() >= deadline:
            return child
        time.sleep(POPUP_OPEN_DELAY_S)


def _cancel(ax: AX, *elements: AXElement | None) -> None:
    for element in elements:
        if element is None:
            continue
        try:
            ax.perform(element, A.CANCEL)
            return
        except AXCallError as exc:
            _log.debug("AXCancel failed: %s", exc)


def select_popup_option(ax: AX, popup: AXElement, option: str) -> str:
    """Open a pop-up button's menu and press the item matching ``option`` (same tolerant
    matching as menus). The menu is the pop-up's ``AXMenu`` child, or an application-level menu
    when the pop-up's menu is not its child (:func:`open_menu_of`). Returns the chosen title;
    cancels the menu on failure. (The tools use :mod:`axpilot.server.dropdown`, which also
    finds menu windows and web lists; this is the engine-level ``set_value`` on a pop-up.)"""
    label = describe(ax, popup)
    _deliver(ax, popup, A.PRESS)
    time.sleep(POPUP_OPEN_DELAY_S)
    menu = open_menu_of(ax, popup)
    if menu is None:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"The menu of {label} did not open.",
            hint="Click the pop-up button and pick the item with `click`.",
        )
    items = [item for item in list(ax.get(menu, A.CHILDREN) or []) if ax.is_element(item)]
    titles = [A.text(ax.get(item, A.TITLE)) or "" for item in items]
    try:
        index = h.match_menu_title(titles, option, f"the options of {label}")
    except AXPilotError as exc:
        _cancel(ax, menu, popup)
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{option!r} is not an option of {label}.",
            hint=exc.hint,
            details=exc.details,
        ) from exc
    if not A.flag(ax.get(items[index], A.ENABLED), default=True):
        _cancel(ax, menu, popup)
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE, f"The option {titles[index]!r} of {label} is disabled."
        )
    _deliver(ax, items[index], A.PRESS)
    return titles[index]


# ------------------------------------------------------------------ focus & text


def focus(ax: AX, handle: Any) -> None:
    """``AXFocused = true``. Raises ``ELEMENT_NOT_ACTIONABLE`` if the element cannot take
    keyboard focus."""
    element = require_element(ax, handle)
    try:
        if not _enabled(ax, element):
            raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{describe(ax, element)} is disabled.")
        if not ax.settable(element, A.FOCUSED):
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{describe(ax, element)} cannot take keyboard focus.",
                hint="Click it instead.",
            )
        ax.set(element, A.FOCUSED, True)
    except AXCallError as exc:
        if exc.code == h.AX_CANNOT_COMPLETE and exc.timed_out:
            return
        raise _fail(ax, exc, element, "Focusing") from exc


def insert_text(ax: AX, handle: Any, text: str) -> bool:
    """Insert ``text`` at the insertion point by setting ``AXSelectedText``.

    Returns False (without raising) when the element does not support it, so the caller can
    fall back to keystrokes. A timed-out insertion counts as done to avoid typing twice.
    """
    element = require_element(ax, handle)
    try:
        if not ax.settable(element, A.SELECTED_TEXT):
            return False
        ax.set(element, A.SELECTED_TEXT, text)
    except AXCallError as exc:
        if exc.stale or exc.permission:
            raise _fail(ax, exc, element, "Inserting text") from exc
        if exc.code == h.AX_CANNOT_COMPLETE and exc.timed_out:
            _log.info("AXSelectedText: the app did not reply in time; assuming the text was inserted")
            return True
        _log.debug("AXSelectedText insertion failed: %s", exc)
        return False
    return True
