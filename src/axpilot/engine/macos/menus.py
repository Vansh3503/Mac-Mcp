"""Menus: read an app's menu bar and choose menu items by path.

Permission: **Accessibility**.

The menu bar is ``AXMenuBar`` → ``AXMenuBarItem`` (one per top-level menu, the Apple menu
first) → ``AXMenu`` → ``AXMenuItem`` (→ ``AXMenu`` for submenus). Separators are menu items
without a title and are skipped.

Choosing an item (:meth:`MenuService.select`) walks that hierarchy *without* opening the
menus and presses only the final item, which AppKit performs directly (the approach
Hammerspoon's ``selectMenuItem`` has used for years; it is fast and does not flash menus).
Menus that are filled lazily when they open (Safari's History, "Open With…", recent items)
have no items until then, so when a level is empty or the wanted title is missing, the
menus above it are opened with ``AXPress`` and the level is read again. Every menu opened
this way is closed again (``AXCancel``, then Escape) if the selection fails.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from axpilot.engine.macos import attributes as A
from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.ax import AX, AXCallError, AXElement, ax_failure
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import MenuNode, RawNode

_log = logging.getLogger(__name__)

ITEM_ATTRIBUTES: tuple[str, ...] = (
    A.ROLE,
    A.TITLE,
    A.ENABLED,
    A.MENU_ITEM_MARK_CHAR,
    A.MENU_ITEM_CMD_CHAR,
    A.MENU_ITEM_CMD_MODIFIERS,
    A.MENU_ITEM_CMD_GLYPH,
    A.MENU_ITEM_CMD_VIRTUAL_KEY,
    A.CHILDREN,
)
BAR_ITEM_ATTRIBUTES: tuple[str, ...] = (
    A.ROLE,
    A.TITLE,
    A.ENABLED,
    A.SELECTED,
    A.POSITION,
    A.SIZE,
    A.CHILDREN,
)
MENU_ACTIONS: tuple[str, ...] = (A.PRESS, A.CANCEL)
"""Every menu (bar) item supports these; listed without asking each item."""
MAX_MENU_ITEMS = 3000
"""Upper bound on items read for one menu bar (huge dynamic menus exist)."""
OPEN_TIMEOUT_S = 0.5
"""Messaging timeout while opening a menu (the reply can wait for menu tracking)."""
OPEN_SETTLE_S = 0.2
"""Pause after opening a menu so lazily built items exist."""


@dataclass(slots=True)
class _Entry:
    element: AXElement
    title: str
    enabled: bool
    attrs: dict[str, Any]


class MenuService:
    """Menu reading and selection for one engine."""

    def __init__(self, ax: AX, press_escape: Callable[[], None]) -> None:
        self._ax = ax
        self._press_escape = press_escape

    # ------------------------------------------------------------------ reading
    def menu_bar_element(self, app_element: AXElement, app_name: str) -> AXElement:
        try:
            bar = self._ax.get(app_element, A.MENU_BAR)
        except AXCallError as exc:
            raise ax_failure(
                exc, f"Reading the menu bar of {app_name}", timeout_s=self._ax.timeout_s
            ) from exc
        if bar is None or not self._ax.is_element(bar):
            raise AXPilotError(
                ErrorCode.MENU_NOT_FOUND,
                f"{app_name} has no menu bar.",
                hint="Background and menu-bar-only apps have no menus; activate a regular app.",
            )
        return bar

    def _children(self, element: AXElement) -> list[AXElement]:
        return [c for c in list(self._ax.get(element, A.CHILDREN) or ()) if self._ax.is_element(c)]

    def _submenu(self, attrs: dict[str, Any]) -> AXElement | None:
        for child in list(attrs.get(A.CHILDREN) or ()):
            if self._ax.is_element(child):
                return child
        return None

    def _entries(self, elements: list[AXElement], attributes: tuple[str, ...]) -> list[_Entry]:
        entries = []
        for element in elements:
            try:
                attrs = self._ax.get_many(element, attributes)
            except AXCallError as exc:
                if exc.permission:
                    raise
                continue
            entries.append(
                _Entry(element, A.text(attrs.get(A.TITLE)) or "", A.flag(attrs.get(A.ENABLED), True), attrs)
            )
        return entries

    def _menu_node(self, entry: _Entry, depth: int, budget: list[int]) -> MenuNode:
        attrs = entry.attrs
        node = MenuNode(
            title=entry.title,
            enabled=entry.enabled,
            checked=bool(A.text(attrs.get(A.MENU_ITEM_MARK_CHAR))),
            shortcut=self._shortcut(attrs),
            handle=entry.element,
        )
        submenu = self._submenu(attrs)
        if depth > 0 and submenu is not None and budget[0] > 0:
            items = self._entries(self._children(submenu), ITEM_ATTRIBUTES)
            for item in items:
                if not item.title or budget[0] <= 0:
                    continue
                budget[0] -= 1
                node.children.append(self._menu_node(item, depth - 1, budget))
        return node

    @staticmethod
    def _shortcut(attrs: dict[str, Any]) -> str | None:
        modifiers = attrs.get(A.MENU_ITEM_CMD_MODIFIERS)
        glyph = attrs.get(A.MENU_ITEM_CMD_GLYPH)
        virtual_key = attrs.get(A.MENU_ITEM_CMD_VIRTUAL_KEY)
        return h.menu_shortcut(
            A.text(attrs.get(A.MENU_ITEM_CMD_CHAR), 4),
            int(modifiers) if isinstance(modifiers, int) else None,
            int(glyph) if isinstance(glyph, int) else None,
            int(virtual_key)
            if isinstance(virtual_key, int) and attrs.get(A.MENU_ITEM_CMD_CHAR) is None
            else None,
        )

    def read(self, app_element: AXElement, app_name: str, depth: int = 2) -> MenuNode:
        """The menu bar as a :class:`MenuNode` tree: the root (titled with the app name) holds
        the top-level menus, Apple menu first; ``depth`` counts levels below the root."""
        bar = self.menu_bar_element(app_element, app_name)
        root = MenuNode(title=app_name, handle=bar)
        if depth <= 0:
            return root
        budget = [MAX_MENU_ITEMS]
        try:
            for entry in self._entries(self._children(bar), ITEM_ATTRIBUTES):
                if entry.title:
                    root.children.append(self._menu_node(entry, depth - 1, budget))
        except AXCallError as exc:
            raise ax_failure(exc, f"Reading the menus of {app_name}", timeout_s=self._ax.timeout_s) from exc
        return root

    def raw_menu_bar(self, app_element: AXElement, app_name: str) -> RawNode:
        """The menu bar as :class:`RawNode`\\ s (top level + first level), for snapshots.

        Items of closed menus are marked invisible; a menu that is open right now (its menu
        bar item is selected) is visible.
        """
        bar = self.menu_bar_element(app_element, app_name)
        bar_attrs = self._ax.get_many(bar, (A.POSITION, A.SIZE))
        root = RawNode(role="AXMenuBar", frame=A.frame_of(self._ax, bar_attrs), handle=bar)
        budget = MAX_MENU_ITEMS
        for entry in self._entries(self._children(bar), BAR_ITEM_ATTRIBUTES):
            is_open = A.flag(entry.attrs.get(A.SELECTED))
            item = RawNode(
                role="AXMenuBarItem",
                title=entry.title or None,
                enabled=entry.enabled,
                selected=is_open,
                frame=A.frame_of(self._ax, entry.attrs),
                actions=MENU_ACTIONS,
                handle=entry.element,
            )
            submenu = self._submenu(entry.attrs)
            if submenu is not None and budget > 0:
                menu = RawNode(role="AXMenu", visible=is_open, handle=submenu)
                for child in self._entries(self._children(submenu), ITEM_ATTRIBUTES):
                    if not child.title or budget <= 0:
                        continue
                    budget -= 1
                    menu.children.append(
                        RawNode(
                            role="AXMenuItem",
                            title=child.title,
                            enabled=child.enabled,
                            visible=is_open,
                            actions=MENU_ACTIONS,
                            handle=child.element,
                        )
                    )
                item.children.append(menu)
            root.children.append(item)
        return root

    # ------------------------------------------------------------------ selecting
    def _open(self, element: AXElement) -> None:
        self._ax.set_timeout(element, OPEN_TIMEOUT_S)
        try:
            self._ax.perform(element, A.PRESS)
        except AXCallError as exc:
            if exc.code != h.AX_CANNOT_COMPLETE:
                raise
        finally:
            self._ax.set_timeout(element, self._ax.timeout_s)
        time.sleep(OPEN_SETTLE_S)

    def _open_chain(self, chain: list[_Entry], opened: list[AXElement]) -> None:
        for entry in chain:
            if not any(self._ax.equal(entry.element, done) for done in opened):
                self._open(entry.element)
                opened.append(entry.element)

    def _level(self, parent: _Entry | None, bar: AXElement) -> list[_Entry]:
        if parent is None:
            return self._entries(self._children(bar), ITEM_ATTRIBUTES)
        attrs = self._ax.get_many(parent.element, (A.CHILDREN,))
        submenu = self._submenu(attrs)
        return [] if submenu is None else self._entries(self._children(submenu), ITEM_ATTRIBUTES)

    def _cancel(self, opened: list[AXElement]) -> None:
        if not opened:
            return
        try:
            self._ax.perform(opened[0], A.CANCEL)
        except AXCallError as exc:
            _log.debug("AXCancel on the menu failed (%s); pressing Escape", exc)
            try:
                self._press_escape()
            except AXPilotError as escape_error:
                _log.warning("could not close the menu: %s", escape_error)

    def select(self, app_element: AXElement, app_name: str, path: list[str]) -> list[str]:
        """Choose the item at ``path`` and return the exact titles that matched."""
        if not path or any(not str(part).strip() for part in path):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A menu path needs one or more non-empty titles.")
        bar = self.menu_bar_element(app_element, app_name)
        opened: list[AXElement] = []
        try:
            return self._walk(bar, app_name, [str(p) for p in path], opened)
        except AXCallError as exc:
            self._cancel(opened)
            raise ax_failure(
                exc, f"Choosing {' > '.join(path)} in {app_name}", timeout_s=self._ax.timeout_s
            ) from exc
        except AXPilotError:
            self._cancel(opened)
            raise

    def _walk(self, bar: AXElement, app_name: str, path: list[str], opened: list[AXElement]) -> list[str]:
        chain: list[_Entry] = []
        matched: list[str] = []
        for index, part in enumerate(path):
            parent = chain[-1] if chain else None
            location = " > ".join(matched) or f"{app_name}'s menu bar"
            level = self._level(parent, bar)
            if parent is not None and not level:
                self._open_chain(chain, opened)
                level = self._level(parent, bar)
            try:
                found = h.match_menu_title([e.title for e in level], part, location)
            except AXPilotError:
                if parent is None or any(self._ax.equal(parent.element, done) for done in opened):
                    raise
                self._open_chain(chain, opened)
                level = self._level(parent, bar)
                found = h.match_menu_title([e.title for e in level], part, location)
            entry = level[found]
            matched.append(entry.title)
            if not entry.enabled:
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"Menu item {' > '.join(matched)} is disabled.",
                    hint="Its command is not available right now (e.g. nothing is selected).",
                )
            chain.append(entry)
            if index == len(path) - 1:
                self._choose(entry, matched)
        return matched

    def _choose(self, entry: _Entry, matched: list[str]) -> None:
        submenu = self._submenu(entry.attrs)
        children = self._entries(self._children(submenu), ITEM_ATTRIBUTES) if submenu is not None else []
        titles = [c.title for c in children if c.title]
        if titles:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{' > '.join(matched)} is a submenu, not a command.",
                hint="Add one of its items to the path: " + ", ".join(titles[:25]) + ".",
            )
        try:
            self._ax.perform(entry.element, A.PRESS)
        except AXCallError as exc:
            if exc.code == h.AX_CANNOT_COMPLETE and exc.timed_out:
                _log.info("menu item %s: no reply in time (modal?); assuming it was chosen", matched[-1])
                return
            raise
