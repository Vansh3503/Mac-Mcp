"""An in-memory macOS desktop: the engine behind tests, CI and ``--engine fake``.

The desktop is loaded from a JSON fixture (:meth:`FakeEngine.demo` uses the bundled
``fixtures/demo_desktop.json``)::

    {
      "os_version": "15.4",
      "displays": [{"display_id": 1, "frame": [0, 0, 1440, 900], "work_area": [0, 25, 1440, 875],
                    "scale": 2.0, "main": true, "name": "Built-in Retina Display"}],
      "apps": [{"name": "Notes", "bundle_id": "com.apple.Notes", "pid": 812,
                "path": "/System/Applications/Notes.app", "frontmost": false}],
      "windows": [{"window_id": 401, "app": "com.apple.Notes", "title": "Notes",
                   "frame": [0, 25, 1440, 875], "tree": {"role": "AXWindow", "children": [...]}}],
      "menus": {"com.apple.Notes": {"title": "", "children": [{"title": "Apple", ...}, ...]}},
      "clipboard": "some text",
      "permissions": {"accessibility": true, "screen_recording": true, "automation": {}}
    }

``windows`` are listed front-to-back. A window of an app that is not running (``"running":
false``), or one marked ``"open": false``, is only a *definition*: it opens when its app
launches (unless ``open`` is false) or when an ``open_window`` effect fires. Each opening gets
fresh element handles, so refs into a closed window go stale exactly like on a real Mac.

Tree nodes use the :mod:`axpilot.perception.serialize` format plus these fake-only keys:

* ``id``: stable fixture id, used as the element handle (``"<id>#2"`` after a re-open);
* ``on_press``: effect(s) applied when the element is pressed (``AXPress`` or a click);
* ``options``: the allowed values of a pop-up button or combo box;
* ``settable``: overrides whether ``set_value`` works on the element;
* ``min``/``max``/``step``: range of sliders and steppers.

Menu items (``title``, ``enabled``, ``checked``, ``shortcut`` in glyph form such as ``"⇧⌘S"``,
``children``) may add ``"separator": true``, ``"toggle": true`` (flip ``checked`` on select)
and ``on_select`` effects. Apps without fixture menus get a standard menu bar. Standard items
work without effects: Quit/Hide/Hide Others/Show All, Close/Minimize/Zoom/Enter Full Screen/
Bring All to Front, and Cut/Copy/Paste/Select All on the focused text element. Key presses
that match a menu shortcut of the frontmost app select that item.

Effects are dicts with one key (or lists of them): ``{"open_window": 302}``,
``{"close_window": 302}`` (``true`` = the element's own window),
``{"set_value": {"id": "notes.body", "value": ""}}``, ``{"focus": "notes.body"}``,
``{"show": "id"}``, ``{"hide": ["id", ...]}``, ``{"select": "row-id"}``,
``{"activate": "Notes"}``, ``{"launch": "com.apple.calculator"}``, ``{"clipboard": "text"}``.
Element references are fixture ids or ``AXIdentifier`` values.

App compatibility fixtures (all optional):

* an app's ``"bundle"`` describes its ``.app`` contents for app-kind detection:
  ``{"files": ["Contents/Frameworks/Electron Framework.framework/", ...],
  "plists": {"Contents/Info.plist": {...}}}`` (paths relative to the app's ``path``); see
  :meth:`FakeEngine.bundle_fs`;
* a window's ``"ocr"`` lists what text recognition sees in it,
  ``[{"text": "Brush", "frame": [x, y, w, h], "confidence": 0.9}, ...]`` (global points). It
  replaces the text derived from static-text nodes, so apps with a poor accessibility tree can
  still be "read";
* an ``AXScrollArea`` node with ``"content_rows"`` simulates a long, virtualized list. Its first
  ``AXTable``/``AXList``/``AXOutline`` child receives one ``AXRow`` (``AXCell`` →
  ``AXStaticText``) per row that intersects the scroll area::

      "content_rows": {"rows": ["text", ["cell 1", "cell 2"], ...], "row_height": 28,
                       "points_per_line": 10, "offset": 0}

  :meth:`FakeEngine.scroll` over the area moves the content by ``dy × points_per_line`` points
  (clamped at the top and the end), keeps the nodes of rows that stay visible, registers new
  rows with fresh handles and drops rows that scrolled out (their refs go stale). The
  collection's ``child_count`` reports the total row count, and an ``AXScrollBar`` child of the
  area gets ``value`` 0…1. :meth:`FakeEngine.content_scroll` reports the position.

Every state-changing call, and ``screenshot``, appends ``(kind, details)`` to
:attr:`FakeEngine.events`; pressing an element (``AXPress`` or a click) also appends
``("press", {"handle": ..., "name": ...})``. Tests can steer the environment through
:attr:`FakeEngine.hid_idle`, :attr:`FakeEngine.secure_input`,
:attr:`FakeEngine.permission_status` and :attr:`FakeEngine.clipboard`.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import json
import math
import posixpath
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import (
    AppAction,
    AppInfo,
    Capture,
    ClipboardContent,
    DisplayInfo,
    ImageFormat,
    KeyCombo,
    MenuNode,
    MouseButton,
    OcrLine,
    PermissionStatus,
    Point,
    RawNode,
    RawTree,
    Rect,
    WindowAction,
    WindowInfo,
)
from axpilot.perception.appkind import MemoryBundleFS
from axpilot.perception.roles import TEXT_INPUT_ROLES, is_interactive
from axpilot.perception.serialize import node_from_dict, rect_from_list, rect_to_list
from axpilot.perception.snapshot import node_name

FIXTURES_DIR = Path(__file__).with_name("fixtures")
DEMO_FIXTURE = FIXTURES_DIR / "demo_desktop.json"

_DESKTOP_KEYS = frozenset({"os_version", "displays", "apps", "windows", "menus", "clipboard", "permissions"})
_APP_KEYS = frozenset({"name", "bundle_id", "pid", "path", "running", "frontmost", "hidden", "bundle"})
_WINDOW_KEYS = frozenset(
    {"window_id", "app", "title", "frame", "display_id", "open", "minimized", "fullscreen", "tree", "ocr"}
)
_DISPLAY_KEYS = frozenset({"display_id", "frame", "work_area", "scale", "main", "name"})
_FAKE_NODE_KEYS = frozenset({"id", "on_press", "options", "settable", "min", "max", "step", "content_rows"})
_BUNDLE_KEYS = frozenset({"files", "plists"})
_OCR_KEYS = frozenset({"text", "frame", "confidence"})
_CONTENT_ROWS_KEYS = frozenset({"rows", "row_height", "points_per_line", "offset"})
_ROW_COLLECTION_ROLES = ("AXTable", "AXList", "AXOutline")
_DEFAULT_ROW_HEIGHT = 24.0
_DEFAULT_POINTS_PER_LINE = 10.0
"""Points one scroll "line" moves (macOS's default pixels-per-line for line-unit scroll events)."""
_CELL_TEXT_INSET = 4.0
_MENU_KEYS = frozenset(
    {"title", "enabled", "checked", "shortcut", "children", "separator", "toggle", "on_select"}
)
_EFFECT_KINDS = frozenset(
    {
        "open_window",
        "close_window",
        "set_value",
        "focus",
        "show",
        "hide",
        "select",
        "activate",
        "launch",
        "clipboard",
    }
)
_APP_ACTIONS = frozenset({"launch", "activate", "hide", "unhide", "quit", "force_quit"})
_WINDOW_ACTIONS = frozenset({"focus", "move", "resize", "minimize", "restore", "zoom", "fullscreen", "close"})
_MOUSE_BUTTONS = frozenset({"left", "right", "middle"})

_SETTABLE_ROLES = frozenset(
    {
        "AXTextField",
        "AXTextArea",
        "AXComboBox",
        "AXSlider",
        "AXIncrementor",
        "AXCheckBox",
        "AXRadioButton",
        "AXPopUpButton",
        "AXDateField",
        "AXTimeField",
    }
)
_SINGLE_LINE_ROLES = frozenset({"AXTextField", "AXComboBox"})
_NUMERIC_ROLES = frozenset({"AXSlider", "AXIncrementor"})

_DEFAULT_DISPLAYS: list[dict[str, Any]] = [
    {
        "display_id": 1,
        "frame": [0, 0, 1440, 900],
        "work_area": [0, 25, 1440, 875],
        "scale": 2.0,
        "main": True,
        "name": "Built-in Display",
    }
]

_MENU_BAR_HEIGHT = 25.0
_TITLE_BAR_HEIGHT = 28.0
_OCR_CONFIDENCE = 0.98

_DESKTOP_COLOR = (38, 70, 120)
_MENU_BAR_COLOR = (236, 236, 236)
_WINDOW_COLOR = (246, 246, 246)
_WINDOW_BORDER = (160, 160, 160)
_TITLE_BAR_COLOR = (222, 222, 222)
_FIELD_COLOR = (255, 255, 255)
_TEXT_COLOR = (120, 120, 120)
_CONTROL_COLOR = (10, 132, 255)

_MODIFIER_GLYPHS: dict[str, str] = {"⌃": "ctrl", "⌥": "option", "⇧": "shift", "⌘": "cmd"}
_KEY_GLYPHS: dict[str, str] = {
    "↩": "return",
    "⏎": "return",
    "⌅": "return",
    "⌫": "delete",
    "⌦": "forward_delete",
    "⎋": "escape",
    "⇥": "tab",
    "←": "left",
    "→": "right",
    "↑": "up",
    "↓": "down",
    "↖": "home",
    "↘": "end",
    "⇞": "page_up",
    "⇟": "page_down",
    "␣": "space",
    "space": "space",
}

Effect = Mapping[str, Any]


def _fixture_error(message: str) -> AXPilotError:
    return AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid fake desktop fixture: {message}",
        hint="See the format described in axpilot.engine.fake.",
    )


def _check_keys(data: Any, allowed: frozenset[str], where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise _fixture_error(f"{where} must be an object.")
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise _fixture_error(f"{where} has unknown key(s): {', '.join(unknown)}.")
    return data


def _effects(value: Any, where: str) -> list[Effect]:
    """Normalize an ``on_press``/``on_select`` value to a list of single-key effect dicts."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[Effect] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or len(item) != 1:
            raise _fixture_error(f"{where}[{index}] must be an object with exactly one effect.")
        kind = next(iter(item))
        if kind not in _EFFECT_KINDS:
            raise _fixture_error(f"{where}[{index}] has unknown effect {kind!r}.")
        out.append(item)
    return out


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    )


def _check_content_rows(node: RawNode, spec: Any, where: str) -> None:
    """Validate a ``content_rows`` spec on its (already parsed) scroll-area node."""
    data = _check_keys(spec, _CONTENT_ROWS_KEYS, where)
    if node.role != "AXScrollArea" or node.frame is None:
        raise _fixture_error(f"{where}: content_rows belongs on an AXScrollArea with a frame.")
    collections = [child for child in node.children if child.role in _ROW_COLLECTION_ROLES]
    if not collections:
        raise _fixture_error(f"{where}: the scroll area needs an AXTable, AXList or AXOutline child.")
    if any(child.role == "AXRow" for child in collections[0].children):
        raise _fixture_error(f"{where}: rows are generated; remove the AXRow children of the collection.")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise _fixture_error(f"{where}.rows must be a non-empty list.")
    for index, row in enumerate(rows):
        cells = [row] if isinstance(row, str) else row
        if not isinstance(cells, list) or not cells or not all(isinstance(cell, str) for cell in cells):
            raise _fixture_error(f"{where}.rows[{index}] must be a string or a non-empty list of strings.")
    for key in ("row_height", "points_per_line"):
        if key in data and not _positive_number(data[key]):
            raise _fixture_error(f"{where}.{key} must be a positive number.")
    offset = data.get("offset", 0)
    if not isinstance(offset, int | float) or isinstance(offset, bool) or not 0 <= offset < math.inf:
        raise _fixture_error(f"{where}.offset must be a number of points ≥ 0.")


def parse_shortcut(text: str | None) -> KeyCombo | None:
    """Parse a menu shortcut in glyph form (``"⇧⌘S"``, ``"⌃⌘F"``, ``"⌘,"``) into a key combo."""
    if not text:
        return None
    modifiers: set[str] = set()
    index = 0
    while index < len(text) - 1 and text[index] in _MODIFIER_GLYPHS:
        modifiers.add(_MODIFIER_GLYPHS[text[index]])
        index += 1
    key = text[index:]
    key = _KEY_GLYPHS.get(key, _KEY_GLYPHS.get(key.lower(), key))
    if len(key) == 1:
        key = key.lower()
    return KeyCombo(key, frozenset(modifiers))


def _menu_norm(text: str) -> str:
    return " ".join(text.replace("…", " ").replace("...", " ").casefold().split())


# --------------------------------------------------------------------------- state objects
@dataclass(slots=True, eq=False)
class _App:
    name: str
    bundle_id: str | None
    path: str | None
    pid: int = 0
    hidden: bool = False
    focused: str | None = None
    """Handle of the element that has keyboard focus in this app."""

    @property
    def running(self) -> bool:
        return self.pid > 0

    @property
    def key(self) -> str:
        return self.bundle_id or self.name

    def matches(self, query: str) -> bool:
        """Name, bundle id, pid (when running) or full bundle path, like ``AppInfo.matches``."""
        if AppInfo(self.name, self.pid, self.bundle_id).matches(query) and query.strip() != "0":
            return True
        return self.path is not None and query.strip().rstrip("/").casefold() == self.path.casefold()


@dataclass(slots=True, eq=False)
class _Window:
    window_id: int
    app: _App
    title: str
    frame: Rect
    root: RawNode
    display_id: int | None
    minimized: bool = False
    fullscreen: bool = False
    restore_frame: Rect | None = None
    ocr: list[OcrLine] | None = None
    """Fixture OCR text (global points); ``None`` derives OCR from static-text nodes."""


@dataclass(slots=True, eq=False)
class _ScrollContent:
    """A scroll area whose rows are generated from ``content_rows`` (a virtualized list)."""

    area: RawNode
    collection: RawNode
    window: _Window
    rows: list[list[str]]
    row_height: float
    points_per_line: float
    offset: float = 0.0
    live: dict[int, RawNode] = field(default_factory=dict)
    """Row index → node, for the rows currently materialized."""
    selected: set[int] = field(default_factory=set)

    @property
    def viewport(self) -> Rect:
        return self.area.frame or Rect(0, 0, 0, 0)

    @property
    def max_offset(self) -> float:
        return max(0.0, len(self.rows) * self.row_height - self.viewport.h)

    def visible_range(self) -> range:
        """Indices of rows that intersect the viewport at the current offset."""
        if self.viewport.h <= 0:
            return range(0)
        first = int(self.offset // self.row_height)
        last = min(len(self.rows), math.ceil((self.offset + self.viewport.h) / self.row_height))
        return range(first, last)


@dataclass(slots=True, eq=False)
class _Registration:
    """Bookkeeping while registering the nodes of one newly opened window."""

    window: _Window
    raw: Mapping[int, Mapping[str, Any]]
    """Fixture dict of each parsed node, keyed by ``id(node)``."""
    generation: int
    focused: list[str] = field(default_factory=list)


@dataclass(slots=True, eq=False)
class _MenuItem:
    title: str
    enabled: bool = True
    checked: bool = False
    shortcut: str | None = None
    toggle: bool = False
    effects: list[Effect] = field(default_factory=list)
    children: list[_MenuItem] = field(default_factory=list)

    @property
    def separator(self) -> bool:
        return not self.title


def _item(title: str, shortcut: str | None = None, *children: _MenuItem, enabled: bool = True) -> _MenuItem:
    return _MenuItem(title=title, shortcut=shortcut, enabled=enabled, children=list(children))


_SEPARATOR = _MenuItem(title="", enabled=False)


def _apple_menu() -> _MenuItem:
    return _item(
        "Apple",
        None,
        _item("About This Mac"),
        _SEPARATOR,
        _item("System Settings…"),
        _item("App Store"),
        _SEPARATOR,
        _item("Recent Items", None, _item("Clear Menu")),
        _SEPARATOR,
        _item("Force Quit…", "⌥⌘⎋"),
        _SEPARATOR,
        _item("Sleep"),
        _item("Restart…"),
        _item("Shut Down…"),
        _SEPARATOR,
        _item("Lock Screen", "⌃⌘Q"),
        _item("Log Out…", "⇧⌘Q"),
    )


def _standard_menus(app_name: str) -> _MenuItem:
    """The menu bar every Cocoa app has, used for apps the fixture gives no menus."""
    return _item(
        "",
        None,
        _apple_menu(),
        _item(
            app_name,
            None,
            _item(f"About {app_name}"),
            _SEPARATOR,
            _item("Settings…", "⌘,"),
            _SEPARATOR,
            _item(f"Hide {app_name}", "⌘H"),
            _item("Hide Others", "⌥⌘H"),
            _item("Show All"),
            _SEPARATOR,
            _item(f"Quit {app_name}", "⌘Q"),
        ),
        _item("File", None, _item("Close Window", "⌘W")),
        _item(
            "Edit",
            None,
            _item("Undo", "⌘Z", enabled=False),
            _item("Redo", "⇧⌘Z", enabled=False),
            _SEPARATOR,
            _item("Cut", "⌘X"),
            _item("Copy", "⌘C"),
            _item("Paste", "⌘V"),
            _item("Select All", "⌘A"),
        ),
        _item("View", None, _item("Enter Full Screen", "⌃⌘F")),
        _item(
            "Window", None, _item("Minimize", "⌘M"), _item("Zoom"), _SEPARATOR, _item("Bring All to Front")
        ),
        _item("Help", None, _item(f"{app_name} Help")),
    )


def _parse_menu(data: Any, where: str) -> _MenuItem:
    menu = _check_keys(data, _MENU_KEYS, where)
    if menu.get("separator"):
        return _MenuItem(title="", enabled=False)
    title = menu.get("title", "")
    if not isinstance(title, str):
        raise _fixture_error(f"{where}.title must be a string.")
    children_data = menu.get("children", [])
    if not isinstance(children_data, list):
        raise _fixture_error(f"{where}.children must be a list.")
    return _MenuItem(
        title=title,
        enabled=bool(menu.get("enabled", True)),
        checked=bool(menu.get("checked", False)),
        shortcut=menu.get("shortcut"),
        toggle=bool(menu.get("toggle", False)),
        effects=_effects(menu.get("on_select"), f"{where}.on_select"),
        children=[_parse_menu(child, f"{where}.children[{i}]") for i, child in enumerate(children_data)],
    )


def _walk_visible(root: RawNode) -> Iterator[RawNode]:
    """Pre-order traversal that skips invisible subtrees."""
    stack = [root]
    while stack:
        node = stack.pop()
        if not node.visible:
            continue
        yield node
        stack.extend(reversed(node.children))


def _node_label(node: RawNode) -> str | None:
    return node_name(node) or node.identifier


def _is_secure(node: RawNode) -> bool:
    return node.secure or node.subrole == "AXSecureTextField"


def _shift(rect: Rect | None, dx: float, dy: float) -> Rect | None:
    return Rect(rect.x + dx, rect.y + dy, rect.w, rect.h) if rect is not None else None


def _fit(width: float, height: float, max_edge: int) -> tuple[int, int]:
    factor = min(1.0, max_edge / max(width, height, 1e-9))
    return max(1, round(width * factor)), max(1, round(height * factor))


# --------------------------------------------------------------------------- the engine
class FakeEngine(Engine):
    """A simulated desktop. Thread-safe; every public method holds one re-entrant lock."""

    name = "fake"

    def __init__(self, desktop: Mapping[str, Any]) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        """Log of state-changing calls: ``(kind, details)``."""
        self.hid_idle: float = 999.0
        """Returned by :meth:`hid_idle_seconds`; set it low to simulate an active user."""
        self.secure_input: bool = False
        """Returned by :meth:`secure_input_active`; when true, synthetic keys are refused."""
        self.scroll_offsets: dict[str | None, tuple[float, float]] = {}
        """Accumulated ``(dx, dy)`` per scrolled scroll-area handle (``None``: no scroll area)."""
        self._lock = threading.RLock()
        self._handle_counter = itertools.count(1)
        self._nodes: dict[str, RawNode] = {}
        self._parents: dict[str, RawNode | None] = {}
        self._node_windows: dict[str, _Window] = {}
        self._behaviour: dict[str, dict[str, Any]] = {}
        self._secrets: dict[str, str] = {}
        self._select_all: set[str] = set()
        self._fixture_handles: dict[str, str] = {}
        self._generations: dict[int, int] = {}
        self._windows: list[_Window] = []
        self._frontmost: _App | None = None
        self._scroll_states: dict[str, _ScrollContent] = {}
        self._bundle_files: list[str] = []
        self._bundle_plists: dict[str, Mapping[str, Any]] = {}
        self._load(desktop)

    # ------------------------------------------------------------------ construction
    @classmethod
    def demo(cls) -> FakeEngine:
        """The bundled demo desktop (Finder, TextEdit, Notes, Login Demo, Terminal, …)."""
        return cls.from_fixture(DEMO_FIXTURE)

    @classmethod
    def from_fixture(cls, path: str | Path) -> FakeEngine:
        """Load a desktop fixture, or a single tree dumped by ``axpilot debug dump-tree``."""
        fixture = Path(path)
        try:
            data = json.loads(fixture.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Fixture {fixture} does not exist.") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Cannot read fixture {fixture}: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FakeEngine:
        """Build from a desktop dict, or from a tree dict (``tree_to_dict`` output)."""
        if isinstance(data, Mapping) and "root" in data and "apps" not in data:
            data = _desktop_from_tree(data)
        return cls(data)

    def _load(self, desktop: Mapping[str, Any]) -> None:
        desktop = _check_keys(desktop, _DESKTOP_KEYS, "desktop")
        self._os_version = str(desktop.get("os_version", "15.4"))
        self._displays = self._parse_displays(desktop.get("displays", _DEFAULT_DISPLAYS))
        self._apps, frontmost = self._parse_apps(desktop.get("apps"))
        self._bundle_fs = MemoryBundleFS(self._bundle_files, self._bundle_plists)
        self._menus = self._parse_menus(desktop.get("menus") or {})
        self._window_defs = self._parse_window_defs(desktop.get("windows") or [])
        self.clipboard: ClipboardContent = self._parse_clipboard(desktop.get("clipboard"))
        self.permission_status: PermissionStatus = self._parse_permissions(desktop.get("permissions"))
        self._validate_references()
        for definition in self._window_defs.values():
            app = self._definition_app(definition)
            if app.running and definition.get("open", True):
                self._windows.append(self._instantiate(definition, take_focus=False))
        running = [app for app in self._apps if app.running]
        front = frontmost or (self._windows[0].app if self._windows else (running[0] if running else None))
        if front is not None:
            self._activate(front)
        main = self._main_display()
        self._cursor = main.frame.center

    def _parse_displays(self, items: Any) -> list[DisplayInfo]:
        if not isinstance(items, list) or not items:
            raise _fixture_error("displays must be a non-empty list.")
        displays: list[DisplayInfo] = []
        for index, item in enumerate(items):
            where = f"displays[{index}]"
            data = _check_keys(item, _DISPLAY_KEYS, where)
            frame = rect_from_list(data.get("frame"), f"{where}.frame")
            displays.append(
                DisplayInfo(
                    display_id=int(data.get("display_id", index + 1)),
                    frame=frame,
                    work_area=rect_from_list(data["work_area"], f"{where}.work_area")
                    if "work_area" in data
                    else frame,
                    scale=float(data.get("scale", 1.0)),
                    main=bool(data.get("main", False)),
                    name=data.get("name"),
                )
            )
        if not any(display.main for display in displays):
            displays[0].main = True
        return displays

    def _parse_apps(self, items: Any) -> tuple[list[_App], _App | None]:
        if not isinstance(items, list):
            raise _fixture_error("apps must be a list.")
        apps: list[_App] = []
        frontmost: _App | None = None
        pending_pids: list[_App] = []
        for index, item in enumerate(items):
            where = f"apps[{index}]"
            data = _check_keys(item, _APP_KEYS, where)
            if not isinstance(data.get("name"), str) or not data["name"]:
                raise _fixture_error(f"{where}.name must be a non-empty string.")
            pid = int(data.get("pid", 0))
            running = bool(data.get("running", pid > 0))
            app = _App(
                name=data["name"],
                bundle_id=data.get("bundle_id"),
                path=data.get("path"),
                pid=pid if running else 0,
                hidden=bool(data.get("hidden", False)),
            )
            if running and pid <= 0:
                pending_pids.append(app)
            if data.get("frontmost") and running and frontmost is None:
                frontmost = app
            if "bundle" in data:
                self._collect_bundle(app, data["bundle"], f"{where}.bundle")
            apps.append(app)
        keys = [app.key for app in apps]
        if len(set(keys)) != len(keys):
            raise _fixture_error("app bundle ids (or names) must be unique.")
        self._pids = itertools.count(max([app.pid for app in apps] + [1000]) + 1)
        for app in pending_pids:
            app.pid = next(self._pids)
        return apps, frontmost

    def _collect_bundle(self, app: _App, data: Any, where: str) -> None:
        """Add an app's ``bundle`` description to the in-memory bundle file system."""
        bundle = _check_keys(data, _BUNDLE_KEYS, where)
        if not app.path or not app.path.startswith("/"):
            raise _fixture_error(f"{where} needs the app to have an absolute 'path'.")
        files = bundle.get("files", [])
        plists = bundle.get("plists", {})
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise _fixture_error(f"{where}.files must be a list of relative paths.")
        if not isinstance(plists, Mapping) or not all(isinstance(v, Mapping) for v in plists.values()):
            raise _fixture_error(f"{where}.plists must map relative paths to objects.")
        for relative in [*files, *plists]:
            parts = str(relative).strip("/").split("/")
            if not relative or str(relative).startswith("/") or ".." in parts:
                raise _fixture_error(f"{where} path {relative!r} must be relative to the bundle.")
        self._bundle_files.extend(posixpath.join(app.path, item) for item in files)
        self._bundle_plists.update({posixpath.join(app.path, k): v for k, v in plists.items()})

    def bundle_fs(self) -> MemoryBundleFS:
        """The fixture apps' bundles as a read-only file system for
        :func:`axpilot.perception.appkind.detect_app_kind` (apps without a ``bundle`` have
        no files, so detection falls back to their bundle ids)."""
        return self._bundle_fs

    def _parse_menus(self, data: Any) -> dict[str, _MenuItem]:
        if not isinstance(data, Mapping):
            raise _fixture_error("menus must be an object keyed by bundle id.")
        menus: dict[str, _MenuItem] = {}
        for key, value in data.items():
            app = next((a for a in self._apps if key in (a.bundle_id, a.name)), None)
            if app is None:
                raise _fixture_error(f"menus has an entry for unknown app {key!r}.")
            where = f"menus[{key!r}]"
            root = (
                _MenuItem(title="", children=[_parse_menu(c, f"{where}[{i}]") for i, c in enumerate(value)])
                if isinstance(value, list)
                else _parse_menu(value, where)
            )
            if not root.children or root.children[0].title != "Apple":
                root.children.insert(0, _apple_menu())
            menus[app.key] = root
        return menus

    def _parse_window_defs(self, items: Any) -> dict[int, Mapping[str, Any]]:
        if not isinstance(items, list):
            raise _fixture_error("windows must be a list.")
        defs: dict[int, Mapping[str, Any]] = {}
        self._definition_apps: dict[int, _App] = {}
        self._window_ocr: dict[int, list[OcrLine]] = {}
        for index, item in enumerate(items):
            where = f"windows[{index}]"
            data = _check_keys(item, _WINDOW_KEYS, where)
            window_id = data.get("window_id")
            if not isinstance(window_id, int) or isinstance(window_id, bool) or window_id in defs:
                raise _fixture_error(f"{where}.window_id must be a unique integer.")
            app = self._fixture_app(data["app"]) if isinstance(data.get("app"), str) else None
            if app is None:
                raise _fixture_error(f"{where}.app must name an app from 'apps'.")
            rect_from_list(data.get("frame"), f"{where}.frame")
            if "ocr" in data:
                self._window_ocr[window_id] = self._parse_ocr(data["ocr"], f"{where}.ocr")
            defs[window_id] = data
            self._definition_apps[window_id] = app
        return defs

    @staticmethod
    def _parse_ocr(items: Any, where: str) -> list[OcrLine]:
        if not isinstance(items, list):
            raise _fixture_error(f"{where} must be a list of {{text, frame, confidence}} objects.")
        lines: list[OcrLine] = []
        for index, item in enumerate(items):
            entry = _check_keys(item, _OCR_KEYS, f"{where}[{index}]")
            text = entry.get("text")
            confidence = entry.get("confidence", _OCR_CONFIDENCE)
            if not isinstance(text, str) or not text.strip():
                raise _fixture_error(f"{where}[{index}].text must be a non-empty string.")
            if (
                not isinstance(confidence, int | float)
                or isinstance(confidence, bool)
                or not 0.0 <= confidence <= 1.0
            ):
                raise _fixture_error(f"{where}[{index}].confidence must be a number from 0 to 1.")
            frame = rect_from_list(entry.get("frame"), f"{where}[{index}].frame")
            lines.append(OcrLine(text=text, confidence=float(confidence), frame=frame))
        return lines

    @staticmethod
    def _parse_clipboard(data: Any) -> ClipboardContent:
        if data is None:
            return ClipboardContent()
        if isinstance(data, str):
            return ClipboardContent(text=data)
        if isinstance(data, Mapping):
            files = data.get("files") or []
            return ClipboardContent(text=data.get("text"), files=[str(f) for f in files])
        raise _fixture_error("clipboard must be a string or an object with 'text'/'files'.")

    @staticmethod
    def _parse_permissions(data: Any) -> PermissionStatus:
        if data is None:
            return PermissionStatus(accessibility=True, screen_recording=True, responsible_app="AXPilot")
        if not isinstance(data, Mapping):
            raise _fixture_error("permissions must be an object.")
        return PermissionStatus(
            accessibility=bool(data.get("accessibility", True)),
            screen_recording=bool(data.get("screen_recording", True)),
            automation={str(k): bool(v) for k, v in dict(data.get("automation") or {}).items()},
            responsible_app=data.get("responsible_app", "AXPilot"),
            notes=[str(note) for note in data.get("notes") or []],
        )

    def _validate_references(self) -> None:
        """Check that every effect points at something the fixture defines."""
        ids: set[str] = set()
        identifiers: set[str] = set()
        effects: list[tuple[str, Effect]] = []

        def collect(node: RawNode, data: Mapping[str, Any]) -> None:
            if "id" in data:
                fid = data["id"]
                if (
                    not isinstance(fid, str)
                    or not fid
                    or (fid[0] == "n" and fid[1:].isdigit())
                    or set(fid) & {"#", ":"}
                ):
                    raise _fixture_error(
                        f"node id {fid!r} is invalid: it must not look like 'n12' or contain '#'/':'."
                    )
                if fid in ids:
                    raise _fixture_error(f"node id {fid!r} is used twice.")
                ids.add(fid)
            if node.identifier:
                identifiers.add(node.identifier)
            if "content_rows" in data:
                _check_content_rows(
                    node, data["content_rows"], f"content_rows of {data.get('id') or node.role}"
                )
            effects.extend(
                (f"on_press of {data.get('id') or node.role}", e)
                for e in _effects(data.get("on_press"), "on_press")
            )

        for window_id, definition in self._window_defs.items():
            tree = definition.get("tree")
            if tree is not None:
                node_from_dict(
                    tree, extra_keys=_FAKE_NODE_KEYS, on_node=collect, where=f"window {window_id}.tree"
                )

        def collect_menu(item: _MenuItem) -> None:
            effects.extend((f"menu item {item.title!r}", e) for e in item.effects)
            for child in item.children:
                collect_menu(child)

        for menu in self._menus.values():
            collect_menu(menu)
        known = ids | identifiers
        for where, effect in effects:
            ((kind, arg),) = effect.items()
            for ref in self._effect_refs(kind, arg):
                if ref not in known:
                    raise _fixture_error(f"{where}: effect {kind!r} refers to unknown element {ref!r}.")
            if kind in ("open_window", "close_window") and arg is not True and arg not in self._window_defs:
                raise _fixture_error(f"{where}: effect {kind!r} refers to unknown window {arg!r}.")
            if kind in ("activate", "launch") and (
                not isinstance(arg, str) or self._fixture_app(arg) is None
            ):
                raise _fixture_error(f"{where}: effect {kind!r} refers to unknown app {arg!r}.")

    @staticmethod
    def _effect_refs(kind: str, arg: Any) -> list[str]:
        if kind in ("focus", "select"):
            return [str(arg)]
        if kind in ("show", "hide"):
            return [str(a) for a in arg] if isinstance(arg, list) else [str(arg)]
        if kind == "set_value":
            if not isinstance(arg, Mapping) or "id" not in arg or "value" not in arg:
                raise _fixture_error("set_value effects need {'id': ..., 'value': ...}.")
            return [str(arg["id"])]
        return []

    def _fixture_app(self, query: str) -> _App | None:
        return next((app for app in self._apps if app.matches(query) or app.bundle_id == query), None)

    def _definition_app(self, definition: Mapping[str, Any]) -> _App:
        return self._definition_apps[definition["window_id"]]

    # ------------------------------------------------------------------ window instances
    def _instantiate(self, definition: Mapping[str, Any], *, take_focus: bool = True) -> _Window:
        """Create a live window (fresh handles) from its fixture definition.

        With ``take_focus`` the element marked ``focused`` in the fixture becomes the app's
        focused element (a newly opened window is key); otherwise only if the app has none."""
        window_id: int = definition["window_id"]
        generation = self._generations[window_id] = self._generations.get(window_id, 0) + 1
        app = self._definition_app(definition)
        frame = rect_from_list(definition["frame"])
        title = str(definition.get("title", ""))
        raw: dict[int, Mapping[str, Any]] = {}
        root = node_from_dict(
            definition.get("tree")
            or {"role": "AXWindow", "subrole": "AXStandardWindow", "actions": ["AXRaise"]},
            extra_keys=_FAKE_NODE_KEYS,
            on_node=lambda node, data: raw.__setitem__(id(node), data),
        )
        if root.role == "AXWindow":
            root.title = title
            root.frame = frame
        window = _Window(
            window_id=window_id,
            app=app,
            title=title,
            frame=frame,
            root=root,
            display_id=definition.get("display_id") or self._display_for(frame).display_id,
            minimized=bool(definition.get("minimized", False)),
            fullscreen=bool(definition.get("fullscreen", False)),
        )
        if window_id in self._window_ocr:
            window.ocr = [dataclasses.replace(line) for line in self._window_ocr[window_id]]
        batch = _Registration(window=window, raw=raw, generation=generation)
        self._register(root, None, batch)
        self._init_scroll_contents(window, generation)
        if batch.focused and (take_focus or app.focused is None):
            app.focused = batch.focused[0]
        return window

    def _register(self, node: RawNode, parent: RawNode | None, batch: _Registration) -> None:
        window = batch.window
        data = batch.raw.get(id(node), {})
        fixture_id = data.get("id")
        if fixture_id:
            handle = fixture_id if batch.generation == 1 else f"{fixture_id}#{batch.generation}"
            self._fixture_handles[fixture_id] = handle
        else:
            handle = f"n{next(self._handle_counter)}"
        node.handle = handle
        self._nodes[handle] = node
        self._parents[handle] = parent
        self._node_windows[handle] = window
        behaviour = {key: data[key] for key in _FAKE_NODE_KEYS if key in data and key != "id"}
        if behaviour:
            self._behaviour[handle] = behaviour
        if _is_secure(node):
            node.secure = True
            if node.value is not None:
                self._secrets[handle] = str(node.value)
                node.value = None
        if node.focused:
            node.focused = False  # focus lives on the app, see capture_tree
            batch.focused.append(handle)
        for child in node.children:
            self._register(child, node, batch)

    def _unregister(self, window: _Window) -> None:
        self._unregister_subtree(window.root, window.app)

    def _unregister_subtree(self, root: RawNode, app: _App) -> None:
        for node in root.walk():
            handle = node.handle
            self._nodes.pop(handle, None)
            self._parents.pop(handle, None)
            self._node_windows.pop(handle, None)
            self._behaviour.pop(handle, None)
            self._secrets.pop(handle, None)
            self._select_all.discard(handle)
            self._scroll_states.pop(handle, None)
            if app.focused == handle:
                app.focused = None

    # ------------------------------------------------------------------ virtualized lists
    def _init_scroll_contents(self, window: _Window, generation: int) -> None:
        """Materialize the visible rows of every ``content_rows`` scroll area of a new window."""
        for node in list(window.root.walk()):
            spec = self._behaviour.get(node.handle, {}).get("content_rows")
            if spec is None:
                continue
            collection = next(child for child in node.children if child.role in _ROW_COLLECTION_ROLES)
            state = _ScrollContent(
                area=node,
                collection=collection,
                window=window,
                rows=[[row] if isinstance(row, str) else [str(cell) for cell in row] for row in spec["rows"]],
                row_height=float(spec.get("row_height", _DEFAULT_ROW_HEIGHT)),
                points_per_line=float(spec.get("points_per_line", _DEFAULT_POINTS_PER_LINE)),
            )
            state.offset = min(max(float(spec.get("offset", 0.0)), 0.0), state.max_offset)
            self._scroll_states[node.handle] = state
            self._layout_rows(state, generation)

    def _layout_rows(self, state: _ScrollContent, generation: int = 1) -> None:
        """Place the collection and its visible rows for the current offset (rows that stay
        visible keep their nodes; new ones get fresh handles; hidden ones are unregistered)."""
        view = state.viewport
        collection = state.collection
        for index, node in state.live.items():
            if node.selected:
                state.selected.add(index)
            else:
                state.selected.discard(index)
        wanted = state.visible_range()
        for index in [i for i in state.live if i not in wanted]:
            self._unregister_subtree(state.live.pop(index), state.window.app)
        top = view.y - state.offset
        width = collection.frame.w if collection.frame is not None else view.w
        collection.frame = Rect(view.x, top, width, len(state.rows) * state.row_height)
        collection.child_count = len(state.rows)
        batch = _Registration(window=state.window, raw={}, generation=generation)
        for index in wanted:
            frame = Rect(view.x, top + index * state.row_height, width, state.row_height)
            node = state.live.get(index)
            if node is None:
                node = self._row_node(state.rows[index], frame, selected=index in state.selected)
                self._register(node, collection, batch)
                state.live[index] = node
            else:
                self._move_subtree(node, frame)
        others = [child for child in collection.children if child.role != "AXRow"]
        collection.children = others + [state.live[index] for index in wanted]
        for child in state.area.children:
            if child.role == "AXScrollBar":
                child.value = round(state.offset / state.max_offset, 4) if state.max_offset else 0.0

    @staticmethod
    def _row_node(cells: list[str], frame: Rect, *, selected: bool) -> RawNode:
        width = frame.w / max(len(cells), 1)
        children: list[RawNode] = []
        for column, text in enumerate(cells):
            cell = Rect(frame.x + column * width, frame.y, width, frame.h)
            inner = Rect(
                cell.x + _CELL_TEXT_INSET,
                cell.y + _CELL_TEXT_INSET,
                max(cell.w - 2 * _CELL_TEXT_INSET, 1.0),
                max(cell.h - 2 * _CELL_TEXT_INSET, 1.0),
            )
            text_node = RawNode(role="AXStaticText", value=text, frame=inner)
            children.append(RawNode(role="AXCell", frame=cell, children=[text_node]))
        return RawNode(role="AXRow", selected=selected, frame=frame, children=children)

    @staticmethod
    def _move_subtree(node: RawNode, frame: Rect) -> None:
        if node.frame is None:
            node.frame = frame
            return
        dx, dy = frame.x - node.frame.x, frame.y - node.frame.y
        for item in node.walk():
            item.frame = _shift(item.frame, dx, dy)

    def _scroll_content(self, handle: str | None, dy: float) -> dict[str, Any]:
        """Apply a vertical scroll to a ``content_rows`` area; details for the scroll event."""
        state = self._scroll_states.get(handle) if handle is not None else None
        if state is None:
            return {}
        before = state.offset
        state.offset = min(max(before + dy * state.points_per_line, 0.0), state.max_offset)
        moved = state.offset != before
        if moved:
            self._layout_rows(state)
        return {"moved": moved, "offset": state.offset, "at_end": state.offset >= state.max_offset}

    def content_scroll(self, ref: str) -> dict[str, Any]:
        """Scroll position of a ``content_rows`` area, by fixture id, ``AXIdentifier`` or handle
        (a test helper): ``offset`` (points), ``max_offset``, ``first``/``last`` visible row
        indices, ``total`` rows, ``at_top``, ``at_end``. Raises ``STALE_REF`` if unknown."""
        with self._lock:
            node = self._nodes.get(ref) or self._lookup(ref)
            state = self._scroll_states.get(node.handle) if node is not None else None
            if state is None:
                raise AXPilotError(ErrorCode.STALE_REF, f"{ref!r} is not an open content_rows scroll area.")
            visible = state.visible_range()
            return {
                "offset": state.offset,
                "max_offset": state.max_offset,
                "first": visible.start,
                "last": visible.stop - 1,
                "total": len(state.rows),
                "at_top": state.offset <= 0.0,
                "at_end": state.offset >= state.max_offset,
            }

    def _open_window(self, window_id: int) -> _Window:
        window = self._open_window_by_id(window_id)
        if window is None:
            definition = self._window_defs[window_id]
            app = self._definition_app(definition)
            if not app.running:
                self._launch(app)
            window = self._open_window_by_id(window_id) or self._instantiate(definition)
            if window not in self._windows:
                self._windows.insert(0, window)
        window.minimized = False
        self._raise_window(window)
        return window

    def _open_window_by_id(self, window_id: int) -> _Window | None:
        return next((w for w in self._windows if w.window_id == window_id), None)

    def _close_window(self, window: _Window) -> None:
        if window in self._windows:
            self._windows.remove(window)
        self._unregister(window)

    def _set_frame(self, window: _Window, frame: Rect) -> None:
        dx, dy = frame.x - window.frame.x, frame.y - window.frame.y
        for node in window.root.walk():
            if node is not window.root:
                node.frame = _shift(node.frame, dx, dy)
        if window.ocr is not None:
            for line in window.ocr:
                line.frame = _shift(line.frame, dx, dy) or line.frame
        window.frame = frame
        if window.root.role == "AXWindow":
            window.root.frame = frame
        window.display_id = self._display_for(frame).display_id

    # ------------------------------------------------------------------ apps & focus helpers
    def _activate(self, app: _App) -> None:
        app.hidden = False
        self._frontmost = app
        mine = [w for w in self._windows if w.app is app]
        self._windows = mine + [w for w in self._windows if w.app is not app]

    def _raise_window(self, window: _Window) -> None:
        self._activate(window.app)
        self._windows.remove(window)
        self._windows.insert(0, window)

    def _refresh_frontmost(self) -> None:
        current = self._frontmost
        if current is not None and current.running and not current.hidden:
            return
        candidates = [w.app for w in self._windows if w.app.running and not w.app.hidden]
        candidates += [a for a in self._apps if a.running and not a.hidden]
        self._frontmost = candidates[0] if candidates else None

    def _launch(self, app: _App) -> None:
        app.pid = next(self._pids)
        app.hidden = False
        opened = [  # the app has no focus yet, so its frontmost window's focused element wins
            self._instantiate(definition, take_focus=False)
            for definition in self._window_defs.values()
            if definition.get("open", True) and self._definition_app(definition) is app
        ]
        self._windows = opened + self._windows

    def _quit(self, app: _App) -> None:
        for window in [w for w in self._windows if w.app is app]:
            self._close_window(window)
        app.pid = 0
        app.hidden = False
        app.focused = None
        if self._frontmost is app:
            self._frontmost = None
        self._refresh_frontmost()

    def _running_app(self, query: str) -> _App:
        running = [app for app in self._apps if app.running]
        exact = [app for app in running if app.matches(query)]
        if exact:
            return exact[0]
        folded = query.strip().casefold()
        partial = [app for app in running if folded and folded in app.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        raise AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"No running app matches {query!r}.",
            hint="Use `list_apps` to see running apps, or `app` with action='launch'.",
        )

    def _installed_app(self, query: str) -> _App:
        exact = [app for app in self._apps if app.matches(query)]
        if not exact:
            base = Path(query.rstrip("/")).name.casefold()
            exact = [app for app in self._apps if app.path and Path(app.path).name.casefold() == base]
        if exact:
            return exact[0]
        folded = query.strip().casefold()
        partial = [app for app in self._apps if folded and folded in app.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        raise AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"No installed app matches {query!r}.",
            hint="Use `list_apps` with include_installed=true to see what can be launched.",
        )

    def _target_app(self, app: str | None) -> _App:
        if app is None:
            if self._frontmost is None:
                raise AXPilotError(ErrorCode.APP_NOT_FOUND, "No app is frontmost.")
            return self._frontmost
        return self._running_app(app)

    def _front_window_of(self, app: _App) -> _Window | None:
        return next((w for w in self._windows if w.app is app and not w.minimized), None)

    def _visible_windows(self) -> list[_Window]:
        return [w for w in self._windows if not w.minimized and w.app.running and not w.app.hidden]

    def _window(self, window_id: int) -> _Window:
        window = self._open_window_by_id(window_id)
        if window is None:
            raise AXPilotError(
                ErrorCode.WINDOW_NOT_FOUND,
                f"No open window has id {window_id}.",
                hint="Use `list_windows` to get current window ids.",
            )
        return window

    def _main_display(self) -> DisplayInfo:
        return next(display for display in self._displays if display.main)

    def _display_for(self, rect: Rect) -> DisplayInfo:
        center = rect.center
        for display in self._displays:
            if display.frame.contains(center):
                return display
        overlaps = [(d, d.frame.intersection(rect)) for d in self._displays]
        best = max(overlaps, key=lambda pair: pair[1].area if pair[1] else 0.0)
        return best[0] if best[1] is not None else self._main_display()

    def _app_info(self, app: _App) -> AppInfo:
        return AppInfo(
            name=app.name,
            pid=app.pid,
            bundle_id=app.bundle_id,
            path=app.path,
            frontmost=app is self._frontmost and app.running,
            hidden=app.hidden,
        )

    def _window_info(self, window: _Window) -> WindowInfo:
        front = self._front_window_of(window.app)
        return WindowInfo(
            window_id=window.window_id,
            pid=window.app.pid,
            app=window.app.name,
            title=window.title,
            frame=window.frame,
            bundle_id=window.app.bundle_id,
            display_id=window.display_id,
            focused=window is front and window.app is self._frontmost,
            main=window is front,
            minimized=window.minimized,
            fullscreen=window.fullscreen,
        )

    # ------------------------------------------------------------------ element helpers
    def _record(self, kind: str, **details: Any) -> None:
        self.events.append((kind, details))

    def _require_accessibility(self) -> None:
        if not self.permission_status.accessibility:
            raise AXPilotError(ErrorCode.PERMISSION_ACCESSIBILITY, "Accessibility permission is not granted.")

    def _require_keyboard(self) -> None:
        self._require_accessibility()
        if self.secure_input:
            raise AXPilotError(
                ErrorCode.SECURE_INPUT_ACTIVE, "Secure Event Input is on; synthetic keystrokes are blocked."
            )

    def _node(self, handle: Any) -> RawNode:
        node = self._nodes.get(handle) if isinstance(handle, str) else None
        if node is None:
            raise AXPilotError(ErrorCode.STALE_REF, f"Element {handle!r} no longer exists.")
        return node

    def _copy_node(self, node: RawNode, app: _App) -> RawNode:
        return dataclasses.replace(
            node,
            children=[],
            focused=node.handle is not None and node.handle == app.focused,
            value=None if node.secure else node.value,
        )

    def _copy_tree(
        self, root: RawNode, app: _App, max_depth: int, max_nodes: int
    ) -> tuple[RawNode, int, bool]:
        count = 0
        truncated = False

        def visit(source: RawNode, depth: int) -> RawNode:
            nonlocal count, truncated
            count += 1
            copy = self._copy_node(source, app)
            if not source.children:
                return copy
            if depth >= max_depth:
                truncated = True
                copy.child_count = len(source.children)
                return copy
            for child in source.children:
                if count >= max_nodes:
                    truncated = True
                    copy.child_count = len(source.children)
                    break
                copy.children.append(visit(child, depth + 1))
            return copy

        return visit(root, 0), count, truncated

    def resolve_ref(self, ref: str) -> str:
        """Current handle of a fixture id or ``AXIdentifier`` (a test helper).

        Raises ``STALE_REF`` if no open window has such an element."""
        with self._lock:
            node = self._lookup(ref)
            if node is None:
                raise AXPilotError(ErrorCode.STALE_REF, f"No open element has id or identifier {ref!r}.")
            return node.handle

    def secure_value(self, handle: str) -> str | None:
        """What was typed into a secure field (a test helper; never exposed through the engine API)."""
        with self._lock:
            return self._secrets.get(handle)

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        """Details of every recorded event of one kind, oldest first."""
        return [details for event_kind, details in self.events if event_kind == kind]

    def set_permissions(
        self, *, accessibility: bool | None = None, screen_recording: bool | None = None
    ) -> None:
        """Flip simulated permissions (a test helper)."""
        with self._lock:
            if accessibility is not None:
                self.permission_status.accessibility = accessibility
            if screen_recording is not None:
                self.permission_status.screen_recording = screen_recording

    def _lookup(self, ref: str) -> RawNode | None:
        handle = self._fixture_handles.get(ref)
        if handle is not None and handle in self._nodes:
            return self._nodes[handle]
        for window in self._windows:
            for node in window.root.walk():
                if node.identifier == ref:
                    return node
        return None

    def _app_of(self, node: RawNode) -> _App:
        return self._node_windows[node.handle].app

    def _focused_text(self, app: _App | None) -> RawNode | None:
        if app is None or app.focused is None:
            return None
        node = self._nodes.get(app.focused)
        if node is None or node.role not in TEXT_INPUT_ROLES or not node.enabled:
            return None
        return node

    def _text_of(self, node: RawNode) -> str:
        if node.secure:
            return self._secrets.get(node.handle, "")
        return "" if node.value is None else str(node.value)

    def _store_text(self, node: RawNode, text: str) -> None:
        if node.secure:
            self._secrets[node.handle] = text
        else:
            node.value = text

    def _insert(self, node: RawNode, text: str) -> None:
        if node.role in _SINGLE_LINE_ROLES:
            text = text.replace("\r", "").replace("\n", "")
        replace_all = node.handle in self._select_all
        self._select_all.discard(node.handle)
        self._store_text(node, text if replace_all else self._text_of(node) + text)

    def _ancestor(self, node: RawNode, role: str, max_levels: int = 3) -> RawNode | None:
        parent = self._parents.get(node.handle)
        for _ in range(max_levels):
            if parent is None or parent.role == role:
                return parent
            parent = self._parents.get(parent.handle)
        return None

    def _select_row(self, row: RawNode, *, extend: bool = False) -> None:
        parent = self._parents.get(row.handle)
        if parent is not None and not extend:
            for sibling in parent.children:
                if sibling.role == row.role:
                    sibling.selected = False
        row.selected = not row.selected if extend else True

    def _select_radio(self, radio: RawNode) -> None:
        parent = self._parents.get(radio.handle)
        siblings = parent.children if parent is not None else [radio]
        for sibling in siblings:
            if sibling.role == "AXRadioButton":
                sibling.value = 1 if sibling is radio else 0

    def _press(self, node: RawNode, *, modifiers: frozenset[str] = frozenset()) -> None:
        """What happens when the user activates an element (AXPress or a click)."""
        self._record("press", handle=node.handle, name=_node_label(node))
        if node.role == "AXCheckBox":
            node.value = 0 if node.value not in (0, False, None, "0") else 1
        elif node.role == "AXRadioButton":
            self._select_radio(node)
        elif node.role == "AXRow":
            self._select_row(node, extend="cmd" in modifiers)
        elif node.role == "AXDisclosureTriangle":
            node.expanded = not node.expanded
            row = self._ancestor(node, "AXRow")
            if row is not None:
                row.expanded = node.expanded
        elif node.role in TEXT_INPUT_ROLES:
            self._app_of(node).focused = node.handle
        effects = _effects(self._behaviour.get(node.handle, {}).get("on_press"), "on_press")
        if effects:
            self._apply_effects(effects, self._node_windows.get(node.handle))

    # ------------------------------------------------------------------ effects
    def _apply_effects(self, effects: Sequence[Effect], source: _Window | None) -> None:
        for effect in effects:
            ((kind, arg),) = effect.items()
            self._apply_effect(kind, arg, source)

    def _apply_effect(self, kind: str, arg: Any, source: _Window | None) -> None:
        if kind == "open_window":
            self._open_window(int(arg))
        elif kind == "close_window":
            window = source if arg is True else self._open_window_by_id(int(arg))
            if window is not None:
                self._close_window(window)
        elif kind in ("activate", "launch"):
            app = self._installed_app(str(arg))
            if not app.running:
                self._launch(app)
            self._activate(app)
        elif kind == "clipboard":
            self.clipboard = ClipboardContent(text=str(arg))
        else:
            self._apply_element_effect(kind, arg)

    def _apply_element_effect(self, kind: str, arg: Any) -> None:
        refs = self._effect_refs(kind, arg)
        for ref in refs:
            node = self._lookup(ref)
            if node is None:
                self._record("effect_skipped", effect=kind, ref=ref)
                continue
            if kind == "focus":
                self._app_of(node).focused = node.handle
            elif kind in ("show", "hide"):
                node.visible = kind == "show"
            elif kind == "select":
                self._select_row(node)
            elif kind == "set_value":
                if node.role in TEXT_INPUT_ROLES:
                    self._store_text(node, str(arg["value"]))
                else:
                    node.value = arg["value"]

    # ------------------------------------------------------------------ menus
    def _menu_root(self, app: _App) -> _MenuItem:
        root = self._menus.get(app.key)
        if root is None:
            root = self._menus[app.key] = _standard_menus(app.name)
        return root

    def _menu_handle(self, app: _App, indices: Sequence[int]) -> str:
        return f"menu:{app.pid}:" + "/".join(str(i) for i in indices)

    def _menu_from_handle(self, handle: Any) -> tuple[_App, list[_MenuItem]] | None:
        if not isinstance(handle, str) or not handle.startswith("menu:"):
            return None
        try:
            _, pid_text, path_text = handle.split(":", 2)
            pid = int(pid_text)
            indices = [int(part) for part in path_text.split("/")] if path_text else []
        except ValueError:
            return None
        app = next((a for a in self._apps if a.running and a.pid == pid), None)
        if app is None:
            return None
        items: list[_MenuItem] = []
        level = self._menu_root(app).children
        for index in indices:
            if not 0 <= index < len(level):
                return None
            items.append(level[index])
            level = level[index].children
        return app, items

    def _menu_node(self, item: _MenuItem, app: _App, indices: list[int], depth: int) -> MenuNode:
        children: list[MenuNode] = []
        if depth > 0:
            children = [
                self._menu_node(child, app, [*indices, i], depth - 1)
                for i, child in enumerate(item.children)
                if not child.separator
            ]
        return MenuNode(
            title=item.title,
            enabled=item.enabled,
            checked=item.checked,
            shortcut=item.shortcut,
            children=children,
            handle=self._menu_handle(app, indices),
        )

    def _menu_bar_node(self, app: _App) -> RawNode:
        display = self._main_display().frame
        bar = RawNode(
            role="AXMenuBar",
            frame=Rect(display.x, display.y, display.w, _MENU_BAR_HEIGHT),
            handle=self._menu_handle(app, []),
        )
        x = display.x + 10
        for index, top in enumerate(self._menu_root(app).children):
            if top.separator:
                continue
            width = 34.0 if index == 0 else 16.0 + 7.5 * len(top.title)
            items = [
                RawNode(
                    role="AXMenuItem",
                    title=child.title,
                    enabled=child.enabled,
                    visible=False,
                    actions=("AXPress", "AXCancel"),
                    handle=self._menu_handle(app, [index, child_index]),
                )
                for child_index, child in enumerate(top.children)
                if not child.separator
            ]
            menu = RawNode(
                role="AXMenu", visible=False, children=items, handle=f"{self._menu_handle(app, [index])}/menu"
            )
            bar.children.append(
                RawNode(
                    role="AXMenuBarItem",
                    title=top.title,
                    enabled=top.enabled,
                    frame=Rect(x, display.y, width, _MENU_BAR_HEIGHT),
                    actions=("AXPress", "AXCancel"),
                    children=[menu],
                    handle=self._menu_handle(app, [index]),
                )
            )
            x += width
        return bar

    @staticmethod
    def _match_menu(items: list[_MenuItem], part: str, location: str) -> _MenuItem:
        wanted = _menu_norm(part)
        candidates = [item for item in items if not item.separator]
        titles = ", ".join(item.title for item in candidates) or "(empty)"
        if wanted:
            exact = [item for item in candidates if _menu_norm(item.title) == wanted]
            if exact:
                return exact[0]
            prefix = [item for item in candidates if _menu_norm(item.title).startswith(wanted)]
            if len(prefix) == 1:
                return prefix[0]
            if prefix:
                raise AXPilotError(
                    ErrorCode.MENU_NOT_FOUND,
                    f"{part!r} is ambiguous in {location}: " + ", ".join(item.title for item in prefix) + ".",
                    hint="Use more of the item's title.",
                )
        raise AXPilotError(
            ErrorCode.MENU_NOT_FOUND, f"No menu item {part!r} in {location}.", hint=f"Available: {titles}."
        )

    def _choose_menu_item(self, app: _App, path: list[str], item: _MenuItem, *, via: str) -> None:
        self._record("menu_select", app=app.key, path=path, via=via)
        if item.toggle:
            item.checked = not item.checked
        if item.effects:
            self._apply_effects(item.effects, self._front_window_of(app))
        else:
            self._standard_menu_command(app, item.title)

    def _standard_menu_command(self, app: _App, title: str) -> None:
        command = _menu_norm(title)
        window = self._front_window_of(app)
        if command.startswith("quit "):
            self._quit(app)
        elif command == "hide others":
            for other in self._apps:
                if other is not app and other.running:
                    other.hidden = True
        elif command.startswith("hide "):
            app.hidden = True
            self._refresh_frontmost()
        elif command == "show all":
            for other in self._apps:
                other.hidden = False
        elif command in ("close", "close window") and window is not None:
            self._close_window(window)
        elif command == "minimize" and window is not None:
            window.minimized = True
        elif command == "zoom" and window is not None:
            self._zoom(window)
        elif command == "enter full screen" and window is not None:
            self._fullscreen(window)
        elif command == "bring all to front":
            self._activate(app)
        else:
            self._edit_command(app, command)

    def _edit_command(self, app: _App, command: str) -> None:
        node = self._focused_text(app)
        if node is None:
            return
        if command == "select all":
            self._select_all.add(node.handle)
        elif command in ("copy", "cut") and not node.secure:
            self.clipboard = ClipboardContent(text=self._text_of(node))
            if command == "cut":
                self._store_text(node, "")
                self._select_all.discard(node.handle)
        elif command.startswith("paste") and self.clipboard.text is not None:
            self._insert(node, self.clipboard.text)

    def _shortcut_item(self, app: _App, combo: KeyCombo) -> tuple[list[str], _MenuItem] | None:
        wanted = (combo.key.casefold(), combo.modifiers)

        def search(items: list[_MenuItem], trail: list[str]) -> tuple[list[str], _MenuItem] | None:
            for item in items:
                if item.separator or not item.enabled:
                    continue
                if item.children:
                    found = search(item.children, [*trail, item.title])
                    if found:
                        return found
                    continue
                shortcut = parse_shortcut(item.shortcut)
                if shortcut is not None and (shortcut.key.casefold(), shortcut.modifiers) == wanted:
                    return [*trail, item.title], item
            return None

        return search(self._menu_root(app).children, [])

    # ------------------------------------------------------------------ window geometry
    def _display_of(self, window: _Window) -> DisplayInfo:
        found = next((d for d in self._displays if d.display_id == window.display_id), None)
        return found or self._display_for(window.frame)

    def _zoom(self, window: _Window) -> None:
        if window.fullscreen:
            return  # like macOS: zoom does nothing in full screen
        if window.restore_frame is not None:
            self._set_frame(window, window.restore_frame)
            window.restore_frame = None
        else:
            window.restore_frame = window.frame
            self._set_frame(window, self._display_of(window).work_area)

    def _fullscreen(self, window: _Window) -> None:
        if window.fullscreen:
            return
        window.restore_frame = window.restore_frame or window.frame
        self._set_frame(window, self._display_of(window).frame)
        window.fullscreen = True

    def _restore(self, window: _Window) -> None:
        window.minimized = False
        window.fullscreen = False
        if window.restore_frame is not None:
            self._set_frame(window, window.restore_frame)
            window.restore_frame = None

    # ------------------------------------------------------------------ hit testing
    def _window_at(self, point: Point) -> _Window | None:
        return next((w for w in self._visible_windows() if w.frame.contains(point)), None)

    def _hit(self, node: RawNode, point: Point, root: RawNode) -> RawNode | None:
        """Deepest, topmost interactive node under ``point`` (later siblings are on top)."""
        if not node.visible:
            return None
        frame = node.frame
        if frame is not None and (frame.w <= 0 or frame.h <= 0 or not frame.contains(point)):
            return None
        for child in reversed(node.children):
            found = self._hit(child, point, root)
            if found is not None:
                return found
        if frame is not None and node is not root and is_interactive(node):
            return node
        return None

    def _scroll_area_at(self, window: _Window, point: Point) -> RawNode | None:
        areas = [
            node
            for node in _walk_visible(window.root)
            if node.role == "AXScrollArea" and node.frame is not None and node.frame.contains(point)
        ]
        return areas[-1] if areas else None

    # ================================================================== Engine API
    # ------------------------------------------------------------------ environment
    def permissions(self) -> PermissionStatus:
        with self._lock:
            return dataclasses.replace(
                self.permission_status,
                automation=dict(self.permission_status.automation),
                notes=list(self.permission_status.notes),
            )

    def os_version(self) -> str:
        return self._os_version

    # ------------------------------------------------------------------ apps, windows, displays
    def list_apps(self, include_installed: bool = False) -> list[AppInfo]:
        with self._lock:
            return [self._app_info(app) for app in self._apps if app.running or include_installed]

    def frontmost_app(self) -> AppInfo | None:
        with self._lock:
            return self._app_info(self._frontmost) if self._frontmost is not None else None

    def list_windows(self, app: str | None = None) -> list[WindowInfo]:
        with self._lock:
            target = self._running_app(app) if app is not None else None
            return [
                self._window_info(window)
                for window in self._windows
                if not window.app.hidden and (target is None or window.app is target)
            ]

    def list_displays(self) -> list[DisplayInfo]:
        with self._lock:
            return [dataclasses.replace(display) for display in self._displays]

    def app_at_point(self, point: Point) -> AppInfo | None:
        with self._lock:
            window = self._window_at(point)
            return self._app_info(window.app) if window is not None else None

    # ------------------------------------------------------------------ accessibility tree
    def capture_tree(
        self,
        *,
        app: str | None = None,
        window_id: int | None = None,
        include_menus: bool = False,
        max_depth: int = 40,
        max_nodes: int = 4000,
        timeout_s: float = 5.0,
    ) -> RawTree:
        with self._lock:
            self._require_accessibility()
            if max_depth < 0 or max_nodes < 1 or timeout_s <= 0:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, "max_depth must be ≥ 0, max_nodes ≥ 1 and timeout_s > 0."
                )
            window: _Window | None
            if window_id is not None:
                window = self._window(window_id)
                target = window.app
                if app is not None and not target.matches(app):
                    raise AXPilotError(
                        ErrorCode.WINDOW_NOT_FOUND,
                        f"Window {window_id} belongs to {target.name}, not {app!r}.",
                    )
            else:
                target = self._target_app(app)
                window = self._front_window_of(target)
            if window is not None:
                root, count, truncated = self._copy_tree(window.root, target, max_depth, max_nodes)
            else:
                root = RawNode(role="AXApplication", title=target.name, handle=f"app:{target.pid}")
                count, truncated = 1, False
            return RawTree(
                app=self._app_info(target),
                window=self._window_info(window) if window is not None else None,
                root=root,
                menu_bar=self._menu_bar_node(target) if include_menus else None,
                node_count=count,
                truncated=truncated,
            )

    def element_valid(self, handle: Any) -> bool:
        with self._lock:
            if not isinstance(handle, str):
                return False
            if handle in self._nodes or self._app_root(handle) is not None:
                return True
            return self._menu_from_handle(handle) is not None

    def _app_root(self, handle: Any) -> RawNode | None:
        if not isinstance(handle, str) or not handle.startswith("app:"):
            return None
        app = next((a for a in self._apps if a.running and handle == f"app:{a.pid}"), None)
        return RawNode(role="AXApplication", title=app.name, handle=handle) if app is not None else None

    def refresh_element(self, handle: Any) -> RawNode:
        with self._lock:
            self._require_accessibility()
            app_root = self._app_root(handle)
            if app_root is not None:
                return app_root
            node = self._node(handle)
            return self._copy_node(node, self._app_of(node))

    def element_attributes(self, handle: Any) -> dict[str, Any]:
        with self._lock:
            self._require_accessibility()
            app_root = self._app_root(handle)
            if app_root is not None:
                return {"AXRole": "AXApplication", "AXTitle": app_root.title}
            node = self._node(handle)
            app = self._app_of(node)
            parent = self._parents.get(node.handle)
            frame = node.frame
            attributes: dict[str, Any] = {
                "AXRole": node.role,
                "AXSubrole": node.subrole,
                "AXTitle": node.title,
                "AXDescription": node.description,
                "AXValue": None if node.secure else node.value,
                "AXHelp": node.help,
                "AXIdentifier": node.identifier,
                "AXPlaceholderValue": node.placeholder,
                "AXTitleUIElement": node.label,
                "AXURL": node.url,
                "AXEnabled": node.enabled,
                "AXFocused": node.handle == app.focused,
                "AXSelected": node.selected,
                "AXExpanded": node.expanded,
                "AXPosition": [frame.x, frame.y] if frame is not None else None,
                "AXSize": [frame.w, frame.h] if frame is not None else None,
                "AXChildrenCount": len(node.children),
                "AXActionNames": list(node.actions),
                "AXParentRole": parent.role if parent is not None else None,
                "AXWindowTitle": self._node_windows[node.handle].title,
            }
            return {key: value for key, value in attributes.items() if value is not None}

    # ------------------------------------------------------------------ AX actions
    def perform_action(self, handle: Any, action: str) -> None:
        with self._lock:
            self._require_accessibility()
            menu = self._menu_from_handle(handle)
            if menu is not None and handle not in self._nodes:
                app, items = menu
                if action not in ("AXPress", "AXPick") or not items or items[-1].children:
                    raise AXPilotError(
                        ErrorCode.ELEMENT_NOT_ACTIONABLE, f"Cannot {action} this menu element."
                    )
                self.menu_select(app.key, [item.title for item in items])
                return
            node = self._node(handle)
            if action not in node.actions:
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"This {node.role} does not support {action}.",
                    hint="Supported actions: " + (", ".join(node.actions) or "none") + ".",
                )
            if not node.enabled:
                raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"This {node.role} is disabled.")
            self._record("perform_action", handle=node.handle, action=action, name=_node_label(node))
            if action == "AXPress":
                self._press(node)
            elif action in ("AXIncrement", "AXDecrement"):
                self._step(node, 1 if action == "AXIncrement" else -1)
            elif action == "AXRaise":
                self._raise_window(self._node_windows[node.handle])

    def _step(self, node: RawNode, direction: int) -> None:
        behaviour = self._behaviour.get(node.handle, {})
        step = float(behaviour.get("step", 1))
        current = float(node.value or 0)
        node.value = self._clamp(node, current + direction * step)

    def _clamp(self, node: RawNode, number: float) -> float | int:
        behaviour = self._behaviour.get(node.handle, {})
        if "min" in behaviour:
            number = max(number, float(behaviour["min"]))
        if "max" in behaviour:
            number = min(number, float(behaviour["max"]))
        return int(number) if number.is_integer() else number

    def set_value(self, handle: Any, value: str | float | bool) -> None:
        with self._lock:
            self._require_accessibility()
            node = self._node(handle)
            behaviour = self._behaviour.get(node.handle, {})
            if not behaviour.get("settable", node.role in _SETTABLE_ROLES):
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"The value of this {node.role} cannot be set.",
                    hint="Click or press it instead.",
                )
            if not node.enabled:
                raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"This {node.role} is disabled.")
            self._record("set_value", handle=node.handle, value="<secure>" if node.secure else value)
            self._assign_value(node, value, behaviour)

    def _assign_value(self, node: RawNode, value: str | float | bool, behaviour: Mapping[str, Any]) -> None:
        role = node.role
        if role in _NUMERIC_ROLES:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, f"A {role} needs a number, got {value!r}."
                ) from exc
            node.value = self._clamp(node, number)
        elif role == "AXCheckBox":
            node.value = self._toggle_value(value)
        elif role == "AXRadioButton":
            if self._toggle_value(value):
                self._select_radio(node)
            else:
                node.value = 0
        elif role == "AXPopUpButton" or (role == "AXComboBox" and "options" in behaviour):
            node.value = self._option(node, str(value), behaviour, strict=role == "AXPopUpButton")
        elif role in TEXT_INPUT_ROLES:
            self._select_all.discard(node.handle)
            text = str(value)
            self._store_text(node, text.replace("\n", "") if role in _SINGLE_LINE_ROLES else text)
        else:
            node.value = value

    @staticmethod
    def _toggle_value(value: str | float | bool) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float):
            return 2 if value == 2 else int(bool(value))
        text = str(value).strip().casefold()
        if text in ("1", "true", "on", "yes", "checked"):
            return 1
        if text in ("0", "false", "off", "no", "unchecked", ""):
            return 0
        if text in ("2", "mixed"):
            return 2
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Expected true/false for a checkbox, got {value!r}.")

    @staticmethod
    def _option(node: RawNode, value: str, behaviour: Mapping[str, Any], *, strict: bool) -> str:
        options = [str(option) for option in behaviour.get("options", [])]
        for option in options:
            if option.casefold() == value.strip().casefold():
                return option
        if strict and options:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{value!r} is not an option of this pop-up.",
                hint="Options: " + ", ".join(options) + ".",
            )
        return value

    def focus(self, handle: Any) -> None:
        with self._lock:
            self._require_accessibility()
            node = self._node(handle)
            if not node.enabled:
                raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"This {node.role} is disabled.")
            self._record("focus", handle=node.handle)
            self._select_all.discard(node.handle)
            self._app_of(node).focused = node.handle

    def insert_text(self, handle: Any, text: str) -> bool:
        with self._lock:
            self._require_accessibility()
            node = self._node(handle)
            if node.role not in TEXT_INPUT_ROLES or not node.enabled:
                return False
            details = {"length": len(text)} if node.secure else {"text": text}
            self._record("insert_text", handle=node.handle, secure=node.secure, **details)
            self._insert(node, text)
            return True

    def scroll_to_visible(self, handle: Any) -> bool:
        with self._lock:
            self._require_accessibility()
            node = self._node(handle)
            if "AXScrollToVisible" not in node.actions:
                return False
            self._record("scroll_to_visible", handle=node.handle)
            return True

    # ------------------------------------------------------------------ synthetic input
    def mouse_move(self, point: Point, duration_s: float = 0.0) -> None:
        with self._lock:
            self._require_accessibility()
            self._record("mouse_move", point=point.to_list(), duration_s=duration_s)
            self._cursor = point

    def mouse_click(
        self,
        point: Point,
        button: MouseButton = "left",
        count: int = 1,
        modifiers: frozenset[str] = frozenset(),
    ) -> None:
        with self._lock:
            self._require_accessibility()
            if button not in _MOUSE_BUTTONS or count < 1:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, f"Bad click: button={button!r}, count={count}."
                )
            self._cursor = point
            window = self._window_at(point)
            target = self._hit(window.root, point, window.root) if window is not None else None
            self._record(
                "mouse_click",
                point=point.to_list(),
                button=button,
                count=count,
                modifiers=sorted(modifiers),
                window_id=window.window_id if window is not None else None,
                target=target.handle if target is not None else None,
            )
            if window is None or button != "left":
                return
            if self._windows[0] is not window or self._frontmost is not window.app:
                self._raise_window(window)
            if target is not None and target.enabled:
                self._click(target, count, modifiers)

    def _click(self, node: RawNode, count: int, modifiers: frozenset[str]) -> None:
        if node.role in TEXT_INPUT_ROLES:
            app = self._app_of(node)
            app.focused = node.handle
            if count >= 3:
                self._select_all.add(node.handle)
            else:
                self._select_all.discard(node.handle)
        elif node.role == "AXRow" or "AXPress" in node.actions:
            self._press(node, modifiers=modifiers)

    def mouse_drag(
        self,
        start: Point,
        end: Point,
        duration_s: float = 0.3,
        modifiers: frozenset[str] = frozenset(),
        button: MouseButton = "left",
    ) -> None:
        with self._lock:
            self._require_accessibility()
            if button not in _MOUSE_BUTTONS:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown mouse button {button!r}.")
            self._record(
                "mouse_drag",
                start=start.to_list(),
                end=end.to_list(),
                duration_s=duration_s,
                modifiers=sorted(modifiers),
                button=button,
            )
            self._cursor = end

    def scroll(self, point: Point | None, dx: float, dy: float) -> None:
        with self._lock:
            self._require_accessibility()
            at = point if point is not None else self._cursor
            window = self._window_at(at)
            area = self._scroll_area_at(window, at) if window is not None else None
            handle = area.handle if area is not None else None
            content = self._scroll_content(handle, dy)
            self._record("scroll", point=at.to_list(), dx=dx, dy=dy, target=handle, **content)
            if point is not None:
                self._cursor = point
            old_x, old_y = self.scroll_offsets.get(handle, (0.0, 0.0))
            self.scroll_offsets[handle] = (old_x + dx, old_y + dy)

    def press_key(self, combo: KeyCombo) -> None:
        with self._lock:
            self._require_keyboard()
            self._record("press_key", key=combo.key, modifiers=sorted(combo.modifiers), combo=str(combo))
            app = self._frontmost
            if app is None:
                return
            found = self._shortcut_item(app, combo) if combo.modifiers - {"shift"} else None
            if found is not None:
                path, item = found
                self._choose_menu_item(app, path, item, via="shortcut")
                return
            self._edit_key(app, combo)

    def _edit_key(self, app: _App, combo: KeyCombo) -> None:
        node = self._focused_text(app)
        if node is None:
            return
        key = combo.key
        if combo.modifiers & {"cmd"} and key in ("a", "c", "x", "v"):
            self._edit_command(app, {"a": "select all", "c": "copy", "x": "cut", "v": "paste"}[key])
        elif key in ("delete", "forward_delete"):
            if node.handle in self._select_all:
                self._select_all.discard(node.handle)
                self._store_text(node, "")
            elif key == "delete":
                self._store_text(node, self._text_of(node)[:-1])
        elif key == "return" and node.role == "AXTextArea":
            self._insert(node, "\n")
        elif key == "tab" and not combo.modifiers - {"shift"}:
            self._move_focus(node, backwards="shift" in combo.modifiers)
        elif key == "space" and not combo.modifiers:
            self._insert(node, " ")
        else:
            self._select_all.discard(node.handle)

    def _move_focus(self, node: RawNode, *, backwards: bool) -> None:
        window = self._node_windows[node.handle]
        fields = [n for n in _walk_visible(window.root) if n.role in TEXT_INPUT_ROLES and n.enabled]
        if len(fields) < 2 or node not in fields:
            return
        step = -1 if backwards else 1
        nxt = fields[(fields.index(node) + step) % len(fields)]
        window.app.focused = nxt.handle
        self._select_all.add(nxt.handle)  # tabbing into a field selects its contents

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        with self._lock:
            self._require_keyboard()
            node = self._focused_text(self._frontmost)
            if node is None:
                self._record("type_text", target=None, text=text)
                return
            if node.secure:
                self._record("type_text", target=node.handle, secure=True, length=len(text))
            else:
                self._record("type_text", target=node.handle, text=text)
            self._insert(node, text)

    def cursor_position(self) -> Point:
        with self._lock:
            return self._cursor

    def hid_idle_seconds(self) -> float:
        return float(self.hid_idle)

    def secure_input_active(self) -> bool:
        return bool(self.secure_input)

    # ------------------------------------------------------------------ app & window control
    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        with self._lock:
            if action not in _APP_ACTIONS:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Unknown app action {action!r}.",
                    hint="Use one of: " + ", ".join(sorted(_APP_ACTIONS)) + ".",
                )
            target = self._installed_app(app) if action == "launch" else self._running_app(app)
            self._record("app_control", action=action, app=target.key)
            if action == "launch":
                if not target.running:
                    self._launch(target)
                self._activate(target)
            elif action == "activate":
                self._activate(target)
            elif action == "hide":
                target.hidden = True
                self._refresh_frontmost()
            elif action == "unhide":
                target.hidden = False
            else:
                self._quit(target)
                return None
            return self._app_info(target)

    def window_control(
        self, window_id: int, action: WindowAction, rect: Rect | None = None
    ) -> WindowInfo | None:
        with self._lock:
            self._require_accessibility()
            if action not in _WINDOW_ACTIONS:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Unknown window action {action!r}.",
                    hint="Use one of: " + ", ".join(sorted(_WINDOW_ACTIONS)) + ".",
                )
            window = self._window(window_id)
            if action in ("move", "resize") and rect is None:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Window action {action!r} needs a rect.")
            if action == "resize" and rect is not None and (rect.w <= 0 or rect.h <= 0):
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A window size must be positive.")
            self._record(
                "window_control",
                window_id=window_id,
                action=action,
                rect=rect_to_list(rect) if rect is not None else None,
            )
            self._window_action(window, action, rect)
            return self._window_info(window) if action != "close" else None

    def _window_action(self, window: _Window, action: str, rect: Rect | None) -> None:
        frame = window.frame
        if action == "focus":
            window.minimized = False
            self._raise_window(window)
        elif action == "move" and rect is not None:
            self._set_frame(window, Rect(rect.x, rect.y, frame.w, frame.h))
        elif action == "resize" and rect is not None:
            self._set_frame(window, Rect(frame.x, frame.y, rect.w, rect.h))
        elif action == "minimize":
            window.minimized = True
        elif action == "restore":
            self._restore(window)
        elif action == "zoom":
            self._zoom(window)
        elif action == "fullscreen":
            self._fullscreen(window)
        elif action == "close":
            self._close_window(window)

    def open_target(self, target: str, with_app: str | None = None, reveal: bool = False) -> None:
        with self._lock:
            if not target or not target.strip():
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Nothing to open: target is empty.")
            app: _App | None = None
            if with_app:
                app = self._installed_app(with_app)
            elif reveal:
                app = next((a for a in self._apps if a.bundle_id == "com.apple.finder"), None)
            self._record("open", target=target, with_app=app.key if app else None, reveal=reveal)
            if app is not None:
                if not app.running:
                    self._launch(app)
                self._activate(app)

    # ------------------------------------------------------------------ menus
    def menu_bar(self, app: str | None = None, depth: int = 2) -> MenuNode:
        with self._lock:
            self._require_accessibility()
            target = self._target_app(app)
            root = self._menu_root(target)
            node = self._menu_node(root, target, [], max(depth, 0))
            node.title = target.name
            return node

    def menu_select(self, app: str | None, path: Sequence[str]) -> list[str]:
        with self._lock:
            self._require_accessibility()
            if not path:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A menu path needs at least one title.")
            target = self._target_app(app)
            items = self._menu_root(target).children
            matched: list[str] = []
            item: _MenuItem | None = None
            for part in path:
                item = self._match_menu(items, str(part), " > ".join(matched) or f"{target.name}'s menu bar")
                matched.append(item.title)
                if not item.enabled:
                    raise AXPilotError(
                        ErrorCode.ELEMENT_NOT_ACTIONABLE, f"Menu item {' > '.join(matched)} is disabled."
                    )
                items = item.children
            assert item is not None
            if item.children:
                titles = ", ".join(child.title for child in item.children if not child.separator)
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"{' > '.join(matched)} is a submenu, not a command.",
                    hint=f"Add one of its items to the path: {titles}.",
                )
            self._choose_menu_item(target, matched, item, via="menu")
            return matched

    # ------------------------------------------------------------------ capture & vision
    def screenshot(
        self,
        *,
        display_id: int | None = None,
        window_id: int | None = None,
        rect: Rect | None = None,
        max_edge: int = 1280,
        fmt: ImageFormat = "jpeg",
        quality: int = 80,
        exclude_bundle_ids: Sequence[str] = (),
    ) -> Capture:
        with self._lock:
            if not self.permission_status.screen_recording:
                raise AXPilotError(
                    ErrorCode.PERMISSION_SCREEN_RECORDING, "Screen Recording permission is not granted."
                )
            if fmt not in ("png", "jpeg"):
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT, f"Unknown image format {fmt!r}; use png or jpeg."
                )
            if max_edge < 1 or not 1 <= quality <= 100:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "max_edge must be ≥ 1 and quality 1–100.")
            region, scale = self._capture_region(display_id, window_id, rect)
            width, height = _fit(region.w * scale, region.h * scale, max_edge)
            image = self._render(region, width, height, frozenset(exclude_bundle_ids))
            buffer = io.BytesIO()
            if fmt == "png":
                image.save(buffer, format="PNG")
            else:
                image.save(buffer, format="JPEG", quality=quality)
            self._record(
                "screenshot",
                region=rect_to_list(region),
                width=width,
                height=height,
                format=fmt,
                exclude_bundle_ids=sorted(exclude_bundle_ids),
            )
            return Capture(data=buffer.getvalue(), format=fmt, width=width, height=height, region=region)

    def _capture_region(
        self, display_id: int | None, window_id: int | None, rect: Rect | None
    ) -> tuple[Rect, float]:
        if rect is not None:
            if rect.w <= 0 or rect.h <= 0:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A capture rect must have a positive size.")
            return rect, self._display_for(rect).scale
        if window_id is not None:
            window = self._window(window_id)
            return window.frame, self._display_for(window.frame).scale
        if display_id is not None:
            display = next((d for d in self._displays if d.display_id == display_id), None)
            if display is None:
                raise AXPilotError(
                    ErrorCode.DISPLAY_NOT_FOUND,
                    f"No display has id {display_id}.",
                    hint="Use `list_displays` to see display ids.",
                )
            return display.frame, display.scale
        main = self._main_display()
        return main.frame, main.scale

    def _render(self, region: Rect, width: int, height: int, excluded: frozenset[str]) -> Image.Image:
        image = Image.new("RGB", (width, height), _DESKTOP_COLOR)
        draw = ImageDraw.Draw(image)
        scale_x, scale_y = width / region.w, height / region.h

        def box(rect: Rect) -> tuple[float, float, float, float]:
            x0 = (rect.x - region.x) * scale_x
            y0 = (rect.y - region.y) * scale_y
            return x0, y0, x0 + max(rect.w * scale_x - 1, 0), y0 + max(rect.h * scale_y - 1, 0)

        for display in self._displays:
            draw.rectangle(
                box(Rect(display.frame.x, display.frame.y, display.frame.w, _MENU_BAR_HEIGHT)),
                fill=_MENU_BAR_COLOR,
            )
        for window in reversed(self._visible_windows()):
            if window.app.bundle_id in excluded or window.app.name in excluded:
                continue
            frame = window.frame
            draw.rectangle(box(frame), fill=_WINDOW_COLOR, outline=_WINDOW_BORDER)
            draw.rectangle(box(Rect(frame.x, frame.y, frame.w, _TITLE_BAR_HEIGHT)), fill=_TITLE_BAR_COLOR)
            for node in _walk_visible(window.root):
                if node is window.root or node.frame is None or node.frame.w <= 0 or node.frame.h <= 0:
                    continue
                if node.role in TEXT_INPUT_ROLES:
                    draw.rectangle(box(node.frame), fill=_FIELD_COLOR, outline=_WINDOW_BORDER)
                elif node.role == "AXStaticText":
                    draw.rectangle(box(node.frame), fill=_TEXT_COLOR)
                elif is_interactive(node):
                    draw.rectangle(box(node.frame), outline=_CONTROL_COLOR)
        return image

    def ocr(self, capture: Capture, languages: Sequence[str] | None = None) -> list[OcrLine]:
        with self._lock:
            visible = self._visible_windows()
            lines: list[OcrLine] = []
            for index, window in enumerate(visible):
                in_front = visible[:index]
                for text, frame, confidence in self._window_text(window):
                    if not text.strip() or not frame.intersects(capture.region):
                        continue
                    if not frame.intersects(window.frame):
                        continue  # scrolled out of its window
                    center = frame.center
                    if any(front.frame.contains(center) for front in in_front):
                        continue  # hidden behind a window in front
                    lines.append(OcrLine(text=text, confidence=confidence, frame=frame))
            lines.sort(key=lambda line: (round(line.frame.y), line.frame.x))
            return lines

    @staticmethod
    def _window_text(window: _Window) -> Iterator[tuple[str, Rect, float]]:
        """What OCR sees in a window: its fixture ``ocr`` lines, else its static-text nodes."""
        if window.ocr is not None:
            for line in window.ocr:
                yield line.text, line.frame, line.confidence
            return
        for node in _walk_visible(window.root):
            if node.role != "AXStaticText" or node.frame is None:
                continue
            yield node.title or ("" if node.value is None else str(node.value)), node.frame, _OCR_CONFIDENCE

    # ------------------------------------------------------------------ clipboard & notifications
    def clipboard_get(self) -> ClipboardContent:
        with self._lock:
            content = self.clipboard
            types: list[str] = []
            if content.text is not None:
                types.append("public.utf8-plain-text")
            if content.image_png is not None:
                types.append("public.png")
            if content.files:
                types.append("public.file-url")
            return ClipboardContent(
                text=content.text, image_png=content.image_png, files=list(content.files), types=types
            )

    def clipboard_set(
        self,
        *,
        text: str | None = None,
        image_png: bytes | None = None,
        files: Sequence[str] | None = None,
    ) -> None:
        with self._lock:
            self._record(
                "clipboard_set",
                text=text,
                image_png=len(image_png) if image_png is not None else None,
                files=list(files or []),
            )
            self.clipboard = ClipboardContent(text=text, image_png=image_png, files=list(files or []))

    def notify(self, title: str, message: str, subtitle: str | None = None) -> None:
        with self._lock:
            self._record("notify", title=title, message=message, subtitle=subtitle)

    def close(self) -> None:
        """Nothing to release; kept for interface parity."""


def _desktop_from_tree(data: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a single dumped tree into a one-app, one-window desktop."""
    app = data.get("app")
    if not isinstance(app, Mapping) or "name" not in app:
        raise _fixture_error("a tree dump needs an 'app' with a name.")
    app_entry = {
        key: app[key] for key in ("name", "bundle_id", "pid", "path") if key in app and app[key] is not None
    }
    app_entry["frontmost"] = True
    app_entry["running"] = True
    root = data["root"]
    windows: list[dict[str, Any]] = []
    window = data.get("window")
    if isinstance(window, Mapping):
        windows.append(
            {
                "window_id": window.get("window_id", 1),
                "app": app_entry.get("bundle_id") or app_entry["name"],
                "title": window.get("title", ""),
                "frame": window.get("frame") or root.get("frame") or [0, 25, 1440, 875],
                "tree": root,
            }
        )
    return {"apps": [app_entry], "windows": windows}
