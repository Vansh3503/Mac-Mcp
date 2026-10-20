"""Budgeted capture of accessibility trees.

Permission: **Accessibility**.

Cost model: each node takes about three AX messages: one batched read of
:data:`~axpilot.engine.macos.attributes.NODE_ATTRIBUTES`, its action names, and a second batch
with ``AXValue`` and ``AXChildren``. ``AXValue`` is only requested once the first batch shows
the element is not a secure text field, so password values are never read. Long text areas
are read through ``AXStringForRange`` so a huge document is not copied in full.

Traversal is breadth-first, so when a budget (``max_nodes``, ``max_depth``, the deadline or
repeated timeouts) stops the capture early, the result still covers the whole window at a
shallower depth. Nodes cut off by a budget keep their child count in ``RawNode.child_count``
and the tree is marked ``truncated``.

Big collections (tables, outlines, lists with more than :data:`LARGE_COLLECTION` rows) list
only their on-screen rows (``AXVisibleRows`` / ``AXVisibleChildren``) and report the rest in
``child_count``. Nodes that lie entirely outside their scroll area are marked invisible and
their subtrees are not read.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from axpilot.engine.macos import attributes as A
from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.ax import AX, AXCallError, AXElement
from axpilot.models import RawNode, Rect

_log = logging.getLogger(__name__)

LARGE_COLLECTION = 50
"""Collections with more rows than this list only their visible rows."""

MAX_CONSECUTIVE_TIMEOUTS = 2
"""Stop a capture after this many node reads in a row time out (the app is hung)."""

COLLECTION_ROLES: frozenset[str] = frozenset({"AXTable", "AXOutline", "AXList", "AXBrowser", "AXGrid"})
ROW_COLLECTION_ROLES: frozenset[str] = frozenset({"AXTable", "AXOutline"})
"""Collections whose items are ``AXRows`` (the others use ``AXChildren``)."""

LEAF_ROLES: frozenset[str] = frozenset(
    {"AXScrollBar", "AXValueIndicator", "AXSplitter", "AXGrowArea", "AXBusyIndicator", "AXProgressIndicator"}
)
"""Roles whose children never matter to an agent."""

NO_VALUE_ROLES: frozenset[str] = frozenset(
    {
        "AXApplication",
        "AXWindow",
        "AXSheet",
        "AXDrawer",
        "AXGroup",
        "AXScrollArea",
        "AXSplitGroup",
        "AXToolbar",
        "AXWebArea",
        "AXTable",
        "AXOutline",
        "AXList",
        "AXBrowser",
        "AXGrid",
        "AXRow",
        "AXColumn",
        "AXMenuBar",
        "AXMenu",
        "AXLayoutArea",
        "AXLayoutItem",
        "AXTabGroup",
        "AXImage",
    }
)
"""Roles whose ``AXValue`` is absent or useless (skipping it saves a large transfer)."""

CONTAINER_WINDOW_ROLES: frozenset[str] = frozenset({"AXApplication", "AXWindow", "AXSheet", "AXDrawer"})
"""Roles whose ``AXFocused`` refers to the window, not to keyboard focus inside it."""

LONG_TEXT_ROLES: frozenset[str] = frozenset({"AXTextArea"})
"""Roles whose value can be huge; read through ``AXStringForRange`` when it is."""


@dataclass(slots=True)
class Budget:
    """Limits for one capture."""

    max_depth: int
    max_nodes: int
    deadline: float
    """``time.monotonic()`` value after which the capture stops."""

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    @classmethod
    def create(cls, max_depth: int, max_nodes: int, timeout_s: float) -> Budget:
        return cls(max_depth=max_depth, max_nodes=max_nodes, deadline=time.monotonic() + timeout_s)


@dataclass(slots=True)
class _Pending:
    node: RawNode
    children: list[AXElement]
    depth: int
    clip: Rect | None
    hidden: int = 0
    """Children known to exist but not listed (off-screen rows of a big collection)."""


@dataclass(slots=True)
class ReadResult:
    """One node plus what is needed to continue the traversal below it."""

    node: RawNode
    children: list[AXElement] = field(default_factory=list)
    hidden: int = 0


class NodeReader:
    """Reads one element into a :class:`RawNode`."""

    def __init__(self, ax: AX, max_value_chars: int = 2000) -> None:
        self.ax = ax
        self.max_value_chars = max_value_chars

    def read(
        self,
        element: AXElement,
        *,
        clip: Rect | None = None,
        want_children: bool = True,
        focused: AXElement | None = None,
    ) -> ReadResult:
        """Read ``element``. Raises :class:`AXCallError` if the element is gone, the app does
        not answer, or Accessibility is off."""
        ax = self.ax
        attrs = ax.get_many(element, A.NODE_ATTRIBUTES)
        role = A.text(attrs.get(A.ROLE)) or "AXUnknown"
        subrole = A.text(attrs.get(A.SUBROLE))
        frame = A.frame_of(ax, attrs)
        visible = h.node_visible(role, frame, clip)
        secure = subrole == A.SECURE_SUBROLE
        actions = self._actions(element)
        node = RawNode(
            role=role,
            subrole=subrole,
            title=A.text(attrs.get(A.TITLE)),
            description=A.text(attrs.get(A.DESCRIPTION)),
            help=A.text(attrs.get(A.HELP)),
            identifier=A.text(attrs.get(A.IDENTIFIER)) or A.text(attrs.get(A.DOM_IDENTIFIER)),
            placeholder=A.text(attrs.get(A.PLACEHOLDER)),
            label=A.label_of(ax, attrs.get(A.TITLE_UI_ELEMENT)),
            url=A.url_text(attrs.get(A.URL)),
            enabled=A.flag(attrs.get(A.ENABLED), default=True),
            focused=self._is_focused(element, role, attrs, focused),
            selected=A.flag(attrs.get(A.SELECTED)),
            expanded=A.optional_flag(attrs.get(A.EXPANDED)),
            secure=secure,
            visible=visible,
            frame=frame,
            actions=actions,
            handle=element,
        )
        read_children = want_children and visible and role not in LEAF_ROLES
        children, hidden = self._value_and_children(element, node, read_children)
        return ReadResult(node, children, hidden)

    # ------------------------------------------------------------------ pieces
    def _is_focused(
        self, element: AXElement, role: str, attrs: dict[str, Any], focused: AXElement | None
    ) -> bool:
        """The app's ``AXFocusedUIElement`` decides; the node's own ``AXFocused`` flag is only
        used when the app does not report one. Windows are never "the focused element" (their
        ``AXFocused`` means key window)."""
        if focused is not None:
            return self.ax.equal(element, focused)
        return role not in CONTAINER_WINDOW_ROLES and A.flag(attrs.get(A.FOCUSED))

    def _actions(self, element: AXElement) -> tuple[str, ...]:
        try:
            return self.ax.actions(element)
        except AXCallError as exc:
            if exc.stale or exc.permission or exc.timed_out:
                raise
            return ()

    def _value_and_children(
        self, element: AXElement, node: RawNode, read_children: bool
    ) -> tuple[list[AXElement], int]:
        role = node.role
        want_value = not node.secure and role not in NO_VALUE_ROLES
        collection = read_children and role in COLLECTION_ROLES
        names: list[str] = []
        if want_value:
            names.append(A.NUMBER_OF_CHARACTERS if role in LONG_TEXT_ROLES else A.VALUE)
        if read_children and not collection:
            names.append(A.CHILDREN)
        sort_button = node.subrole == A.SORT_BUTTON_SUBROLE
        if sort_button:
            names.append(A.SORT_DIRECTION)
        values = self.ax.get_many(element, names) if names else {}
        if want_value:
            node.value = self._value(element, role, values)
        if sort_button:  # a table column header: its value is how the column sorts
            node.value = h.sort_direction(values.get(A.SORT_DIRECTION)) or node.value
        if collection:
            return self._collection_children(element, role)
        return self._elements(values.get(A.CHILDREN)), 0

    def _value(self, element: AXElement, role: str, values: dict[str, Any]) -> Any:
        if role not in LONG_TEXT_ROLES:
            return A.node_value(self.ax, values.get(A.VALUE), self.max_value_chars)
        length = values.get(A.NUMBER_OF_CHARACTERS)
        if isinstance(length, int) and length > self.max_value_chars:
            prefix = self._string_prefix(element)
            if prefix is not None:
                return prefix + "…"
        return A.node_value(self.ax, self.ax.get(element, A.VALUE), self.max_value_chars)

    def _string_prefix(self, element: AXElement) -> str | None:
        try:
            value = self.ax.parameterized(
                element, A.STRING_FOR_RANGE, self.ax.make_range(0, self.max_value_chars)
            )
        except AXCallError as exc:
            if exc.stale or exc.permission:
                raise
            return None
        return str(value) if isinstance(value, str) else None

    def _elements(self, value: Any) -> list[AXElement]:
        if value is None:
            return []
        try:
            items = list(value)
        except TypeError:
            return []
        return [item for item in items if self.ax.is_element(item)]

    def _collection_children(self, element: AXElement, role: str) -> tuple[list[AXElement], int]:
        ax = self.ax
        items_attr = A.ROWS if role in ROW_COLLECTION_ROLES else A.CHILDREN
        visible_attr = A.VISIBLE_ROWS if role in ROW_COLLECTION_ROLES else A.VISIBLE_CHILDREN
        total = ax.count(element, items_attr)
        if total is None or total <= LARGE_COLLECTION:
            return self._elements(ax.get(element, A.CHILDREN)), 0
        visible = self._elements(ax.get(element, visible_attr))
        if not visible:
            visible = self._elements(ax.get_slice(element, items_attr, 0, LARGE_COLLECTION))
        return visible, max(total - len(visible), 0)


def capture_subtree(
    reader: NodeReader,
    root: AXElement,
    *,
    budget: Budget,
    focused: AXElement | None = None,
    extra_children: list[AXElement] | None = None,
) -> tuple[RawNode, int, bool]:
    """Capture the tree under ``root`` breadth-first within ``budget``.

    ``extra_children`` are appended to the root's children (attached dialogs). Returns
    ``(root node, node count, truncated)``. Raises :class:`AXCallError` only if the root
    itself cannot be read or Accessibility is off; unreadable descendants are skipped.
    """
    first = reader.read(root, focused=focused)
    first.children.extend(extra_children or [])
    queue: deque[_Pending] = deque(
        [
            _Pending(
                first.node,
                first.children,
                0,
                h.child_clip(first.node.role, first.node.frame, None),
                first.hidden,
            )
        ]
    )
    count = 1
    truncated = False
    timeouts = 0
    while queue:
        item = queue.popleft()
        if item.depth >= budget.max_depth:
            _cut(item, 0)
            truncated = truncated or bool(item.children)
            continue
        for index, child in enumerate(item.children):
            if count >= budget.max_nodes or budget.expired() or timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                _cut(item, index)
                for rest in queue:
                    _cut(rest, 0)
                return first.node, count, True
            try:
                result = reader.read(child, clip=item.clip, focused=focused)
            except AXCallError as exc:
                if exc.permission:
                    raise
                if exc.timed_out:
                    timeouts += 1
                    truncated = True  # the app did not answer: this part of the tree is missing
                else:
                    timeouts = 0  # the element vanished while we read: nothing is missing
                _log.debug("skipping an unreadable element under %s: %s", item.node.role, exc)
                continue
            timeouts = 0
            count += 1
            item.node.children.append(result.node)
            if result.children or result.hidden:
                child_clip = h.child_clip(result.node.role, result.node.frame, item.clip)
                queue.append(
                    _Pending(result.node, result.children, item.depth + 1, child_clip, result.hidden)
                )
        if item.hidden:
            item.node.child_count = len(item.children) + item.hidden
    return first.node, count, truncated


def _cut(item: _Pending, listed: int) -> None:
    """Record how many children a node has when the budget stopped before listing them all."""
    total = len(item.children) + item.hidden
    if total > len(item.node.children) or listed < len(item.children):
        item.node.child_count = total
