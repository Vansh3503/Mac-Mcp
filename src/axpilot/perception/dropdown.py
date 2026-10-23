"""Find the options a dropdown shows once it is open: pure functions over raw accessibility trees.

The server's dropdown flow (:mod:`axpilot.server.dropdown`) opens a control and then looks for
what appeared, in this order:

1. ``menu``: an ``AXMenu`` child of the control (AppKit pop-up buttons and menu buttons);
2. ``app_menu``: any other menu that was not on screen before (a context menu, a pop-up whose
   menu belongs to the application, an ARIA ``role=menu`` in a web page);
3. ``page``: in web content, a list of options that appeared (an ARIA listbox: React Select,
   MUI, Headless UI, Slack and VS Code pickers), grouped by the list that holds them;
4. ``window``: a new window of the app holding a menu or a list (Java and Qt pop-ups).

"Appeared" means visible now and not visible before opening: :func:`visible_handles` of the
tree captured before is the baseline. Options are titled by :func:`option_title` and matched by
:func:`match_option` (exact, then unique prefix; case-insensitive; never a word inside a title).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import RawNode, Rect
from axpilot.perception.format import quote
from axpilot.perception.refs import HandleIndex, same_handle
from axpilot.perception.snapshot import clean_text, node_name, stringify_value

__all__ = [
    "LIST_ROLES",
    "OPTION_ROLES",
    "Option",
    "OptionList",
    "Signature",
    "find_node",
    "fold",
    "in_web_content",
    "match_option",
    "menu_options",
    "new_menus",
    "open_menu_of",
    "option_title",
    "options_in",
    "page_options",
    "shows_choice",
    "text_near",
    "visible_handles",
    "visible_signatures",
]

MAX_LISTED = 30
"""Options named in an error message at most."""

MENU_ROLE = "AXMenu"
LIST_ROLES: frozenset[str] = frozenset({"AXMenu", "AXList", "AXTable", "AXOutline"})
"""Roles of the containers that hold a dropdown's options."""
OPTION_ROLES: frozenset[str] = frozenset(
    {
        "AXMenuItem",
        "AXStaticText",
        "AXRow",
        "AXCell",
        "AXButton",
        "AXRadioButton",
        "AXCheckBox",
        "AXLink",
        "AXGroup",
    }
)
"""Roles an option can have: menu items (menus, ARIA menus), static text (Chromium and WebKit
expose ARIA ``option`` as ``AXStaticText``), rows (tables, outlines, Java lists) and the
clickable roles custom pickers use."""
COMBO_OPTION_ROLES: frozenset[str] = OPTION_ROLES | {"AXTextField"}
"""Roles of the options in a combo box's own list: on macOS 26 an AppKit combo box's list holds one
``AXTextField`` per row (TextEdit's font sizes)."""
WEB_AREA_ROLE = "AXWebArea"
MENU_BAR_ROLES: frozenset[str] = frozenset({"AXMenuBar", "AXMenuBarItem"})

Where = Literal["menu", "app_menu", "page", "window", "list"]


# --------------------------------------------------------------------------- matching
def fold(text: str | None) -> str:
    """Comparable form of a title: case-folded, outer spaces and a trailing "…" or "." removed,
    "..." read as "…", runs of whitespace collapsed."""
    cleaned = " ".join((text or "").replace("...", "…").split())
    return cleaned.rstrip("…").rstrip(".").strip().casefold()


def _unmarked(name: str) -> str:
    """``name`` from its first letter or digit: an emoji flag, bullet or check mark before it dropped."""
    return next((name[i:] for i, char in enumerate(name) if char.isalnum()), "")


def match_option(names: Sequence[str], option: str) -> int:
    """Index of the name matching ``option`` (case-insensitive): the exact title (the first if the
    same title is listed twice, as TextEdit lists recent fonts again), else the only title that
    starts with it, also after leading marks such as an emoji flag. A word inside a longer title
    never matches ("Bold" is not "Arial Rounded MT Bold"); the error names those titles. Raises
    ``INVALID_ARGUMENT`` (listing the choices) otherwise."""
    wanted = fold(option)
    folded = [fold(name) for name in names]
    exact = [i for i, name in enumerate(folded) if name == wanted]
    if exact and len({names[i] for i in exact}) == 1:
        return exact[0]
    for matches in (
        exact,
        [i for i, name in enumerate(folded) if wanted and name.startswith(wanted)],
        [i for i, name in enumerate(folded) if wanted and _unmarked(name).startswith(wanted)],
    ):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            shown = ", ".join(quote(names[i], 40) for i in matches[:MAX_LISTED])
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{option!r} matches several options: {shown}.",
                hint="Use more of the option's title, or its index (1-based).",
                details={"options": [names[i] for i in matches[:MAX_LISTED]]},
            )
    inside = [names[i] for i, name in enumerate(folded) if wanted and wanted in name][:MAX_LISTED]
    if inside:
        shown = ", ".join(quote(name, 40) for name in inside)
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"No option is or starts with {option!r}; it is part of {shown}.",
            hint="Pass the full title of the option you mean (or its index, 1-based).",
            details={"options": inside},
        )
    shown = ", ".join(quote(name, 40) for name in names[:MAX_LISTED]) or "none"
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"No option matches {option!r}. Options: {shown}.",
        hint="Pass the exact title (or index, 1-based).",
        details={"options": list(names[:MAX_LISTED])},
    )


# --------------------------------------------------------------------------- data
@dataclass(slots=True)
class Option:
    """One choice of an open dropdown."""

    title: str
    node: RawNode

    @property
    def enabled(self) -> bool:
        return self.node.enabled

    @property
    def selected(self) -> bool:
        return self.node.selected


@dataclass(slots=True)
class OptionList:
    """What an open dropdown shows: the container, its options, and where it was found."""

    container: RawNode
    options: list[Option]
    where: Where
    in_web: bool = False
    """The container lies in web content (an ``AXWebArea``): options are clicked like a user would."""
    window_id: int | None = None
    """``window``: the id of the window that holds the list."""
    control: RawNode | None = None
    """``menu``: the control that shows the list as its child."""

    def titles(self) -> list[str]:
        return [option.title for option in self.options]


# --------------------------------------------------------------------------- tree helpers
def _on_screen(frame: Rect | None) -> bool:
    return frame is None or (frame.w > 0 and frame.h > 0)


def visible_nodes(root: RawNode) -> Iterator[tuple[RawNode, tuple[RawNode, ...]]]:
    """Visible nodes in pre-order with their ancestors (root first). Invisible subtrees and the
    menu bar are skipped."""
    stack: list[tuple[RawNode, tuple[RawNode, ...]]] = [(root, ())]
    while stack:
        node, ancestors = stack.pop()
        if not node.visible or node.role in MENU_BAR_ROLES:
            continue
        yield node, ancestors
        inner = (*ancestors, node)
        stack.extend((child, inner) for child in reversed(node.children))


def visible_handles(root: RawNode) -> HandleIndex:
    """Handles of the nodes visible in ``root``: the baseline that tells what appeared later."""
    return HandleIndex((node.handle, index) for index, (node, _) in enumerate(visible_nodes(root)))


Signature = tuple[str, str | None, tuple[int, int, int, int] | None]
"""What a node looks like on screen: its role, title and frame (rounded to points)."""


def _signature(node: RawNode) -> Signature:
    frame = node.frame
    box = (round(frame.x), round(frame.y), round(frame.w), round(frame.h)) if frame is not None else None
    return (node.role, option_title(node), box)


def visible_signatures(root: RawNode) -> frozenset[Signature]:
    """Role, title and frame of the nodes visible in ``root``: a baseline that still recognises a
    node the app re-created with a new identity (VS Code rebuilds its page when a dropdown opens)."""
    return frozenset(_signature(node) for node, _ in visible_nodes(root))


def _was_visible(baseline: HandleIndex, node: RawNode, seen: frozenset[Signature] = frozenset()) -> bool:
    if node.handle is not None and baseline.find(node.handle):
        return True
    return bool(seen) and _signature(node) in seen


def find_node(root: RawNode, handle: object) -> tuple[RawNode, tuple[RawNode, ...]] | None:
    """The node with ``handle`` (visible or not) and its ancestors, or ``None``."""
    stack: list[tuple[RawNode, tuple[RawNode, ...]]] = [(root, ())]
    while stack:
        node, ancestors = stack.pop()
        if same_handle(node.handle, handle):
            return node, ancestors
        inner = (*ancestors, node)
        stack.extend((child, inner) for child in reversed(node.children))
    return None


def in_web_content(ancestors: Sequence[RawNode]) -> bool:
    """True if one of ``ancestors`` is a web area (the node is part of a web page)."""
    return any(node.role == WEB_AREA_ROLE for node in ancestors)


def option_title(node: RawNode) -> str | None:
    """An option's title: its label, else its value, else the text inside it."""
    return node_name(node) or clean_text(stringify_value(node.value, 200))


# --------------------------------------------------------------------------- menus
def menu_options(menu: RawNode) -> list[Option]:
    """The titled, visible items of a menu (separators have no title)."""
    options: list[Option] = []
    for child in menu.children:
        if child.role != "AXMenuItem" or not child.visible or not _on_screen(child.frame):
            continue
        title = clean_text(child.title) or option_title(child)
        if title:
            options.append(Option(title, child))
    return options


def open_menu_of(control: RawNode) -> RawNode | None:
    """The visible menu with items that ``control`` shows as its child (or grandchild)."""
    for child in control.children:
        candidates = [child, *child.children] if child.role != MENU_ROLE else [child]
        for node in candidates:
            if node.role == MENU_ROLE and node.visible and _on_screen(node.frame) and menu_options(node):
                return node
    return None


def combo_list(control: RawNode, *, in_web: bool = False) -> OptionList | None:
    """The options of the list a combo box shows inside itself (an AppKit combo box's list, possibly
    in a scroll area), or ``None``."""
    for node, _ in visible_nodes(control):
        if (
            node is control
            or node.role == MENU_ROLE
            or node.role not in LIST_ROLES
            or not _on_screen(node.frame)
        ):
            continue
        options = _list_children(node, COMBO_OPTION_ROLES)
        if options:
            return OptionList(node, options, "list", in_web=in_web)
    return None


def combo_arrow(control: RawNode) -> RawNode | None:
    """The button of a combo box that opens its list (its arrow): a pressable ``AXButton`` child."""
    buttons = [c for c in control.children if c.role == "AXButton" and c.visible and "AXPress" in c.actions]
    return buttons[-1] if buttons else None


def new_menus(root: RawNode, baseline: HandleIndex) -> list[tuple[RawNode, tuple[RawNode, ...]]]:
    """Visible menus with items that were not visible in the baseline (outermost first)."""
    found: list[tuple[RawNode, tuple[RawNode, ...]]] = []
    for node, ancestors in visible_nodes(root):
        if node.role != MENU_ROLE or not _on_screen(node.frame) or _was_visible(baseline, node):
            continue
        if any(menu is ancestor for menu, _ in found for ancestor in ancestors):
            continue  # a submenu of a menu already found
        if menu_options(node):
            found.append((node, ancestors))
    return found


# --------------------------------------------------------------------------- web lists and windows
def _container_of(ancestors: tuple[RawNode, ...]) -> RawNode | None:
    for node in reversed(ancestors):
        if node.role in LIST_ROLES:
            return node
    return ancestors[-1] if ancestors else None


def page_options(
    root: RawNode,
    baseline: HandleIndex,
    *,
    exclude: object = None,
    seen: frozenset[Signature] = frozenset(),
) -> OptionList | None:
    """A list of options that appeared in a web page: new, visible, titled option-like nodes
    (the outermost of nested ones), grouped by the list that holds them. The group in a real
    list (``AXList``, ``AXMenu``, ``AXTable``, ``AXOutline``) with the most options wins; other
    groups need at least two. ``exclude`` is the control's handle.

    A group that holds a list is a container, never an option: a page may re-create its outer
    groups when a dropdown opens (VS Code's workbench does), and those new, titled groups would
    otherwise be taken as one option holding the whole list. ``seen`` (:func:`visible_signatures`
    of the baseline) also counts as already visible a node that looks the same as before."""
    nodes = list(visible_nodes(root))
    holds_list = {id(outer) for node, ancestors in nodes if node.role in LIST_ROLES for outer in ancestors}
    groups: dict[int, tuple[RawNode, list[Option], tuple[RawNode, ...]]] = {}
    chosen: set[int] = set()
    for node, ancestors in nodes:
        if node.role not in OPTION_ROLES or not _on_screen(node.frame) or same_handle(node.handle, exclude):
            continue
        if node.role == "AXGroup" and id(node) in holds_list:
            continue
        if _was_visible(baseline, node, seen) or any(id(ancestor) in chosen for ancestor in ancestors):
            continue
        title = option_title(node)
        container = _container_of(ancestors)
        if not title or container is None:
            continue
        chosen.add(id(node))
        entry = groups.setdefault(id(container), (container, [], ancestors[: ancestors.index(container)]))
        entry[1].append(Option(title, node))
    ranked = sorted(
        groups.values(), key=lambda entry: (entry[0].role in LIST_ROLES, len(entry[1])), reverse=True
    )
    for container, options, outer in ranked:
        if container.role in LIST_ROLES or len(options) >= 2:
            where: Where = "app_menu" if container.role == MENU_ROLE else "page"
            return OptionList(container, options, where, in_web=in_web_content((*outer, container)))
    return None


def _list_children(container: RawNode, roles: frozenset[str] = OPTION_ROLES) -> list[Option]:
    options: list[Option] = []
    for child in container.children:
        if child.role in roles and child.visible and _on_screen(child.frame):
            title = option_title(child)
            if title:
                options.append(Option(title, child))
    return options


def options_in(root: RawNode) -> OptionList | None:
    """The options a pop-up window shows: its first menu with items, else the list (table,
    outline) with the most titled items."""
    best: tuple[RawNode, list[Option], tuple[RawNode, ...]] | None = None
    for node, ancestors in visible_nodes(root):
        if node.role == MENU_ROLE and _on_screen(node.frame):
            items = menu_options(node)
            if items:
                return OptionList(node, items, "window", in_web=in_web_content(ancestors))
        elif node.role in LIST_ROLES:
            items = _list_children(node)
            if items and (best is None or len(items) > len(best[1])):
                best = (node, items, ancestors)
    if best is None:
        return None
    return OptionList(best[0], best[1], "window", in_web=in_web_content(best[2]))


# --------------------------------------------------------------------------- verification
_LABEL_SEPARATORS = (". ", ": ", " — ", " – ", " - ", " (")
"""What separates an option's label from its description in a longer option title."""


def shows_choice(node: RawNode, title: str) -> bool:
    """True if the control shows ``title``: its value, title or description equals it, (for
    longer texts such as "Sort: Newest") contains it, or is the option's leading label (VS Code's
    rows read "afterDelay. An editor with changes is…" while the box shows "afterDelay")."""
    wanted = fold(title)
    if not wanted:
        return False
    for text in (stringify_value(node.value, 400), node.title, node.description):
        shown = fold(text)
        if shown and (
            shown == wanted
            or wanted in shown
            or any(wanted.startswith(shown + sep) for sep in _LABEL_SEPARATORS)
        ):
            return True
    return False


def text_near(root: RawNode, control: object, title: str, *, levels: int = 3) -> bool:
    """True if static text showing ``title`` lies within ``levels`` ancestors of the control,
    outside any list (a web picker that shows the chosen value next to its input)."""
    found = find_node(root, control)
    if found is None:
        return False
    _, ancestors = found
    if not ancestors:
        return False
    scope = ancestors[max(len(ancestors) - levels, 0)]
    wanted = fold(title)
    for node, inner in visible_nodes(scope):
        if node.role != "AXStaticText" or any(a.role in LIST_ROLES for a in inner):
            continue
        if fold(option_title(node)) == wanted:
            return True
    return False
