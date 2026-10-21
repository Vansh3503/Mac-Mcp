# Module contracts (internal)

This file is the source of truth for how AXPilot's modules talk to each other. Implementations
must match these signatures exactly. When a contract changes, update this file in the same PR.

Already implemented (read them first): `src/axpilot/{__init__,errors,models,paths,config}.py`,
`src/axpilot/engine/{__init__,base}.py`.

General rules
- Python ≥ 3.11, `from __future__ import annotations`, full type hints, docstrings on public API.
- Only `axpilot.engine.macos` and `axpilot.host.ui` may import PyObjC (`objc`, `AppKit`,
  `Quartz`, `ApplicationServices`, `ScreenCaptureKit`, `Vision`, `Foundation`). Everything else must
  import and pass tests on Linux.
- Errors: raise `AXPilotError(ErrorCode.X, "message", hint=...)`. Never leak raw exceptions to tools.
- No network access, no telemetry.
- Tests live in `tests/` mirroring the package path (`tests/perception/test_snapshot.py`, …),
  use plain pytest (async tests via `anyio`'s pytest plugin: `@pytest.mark.anyio`), and must pass
  with `uv run pytest` on Linux.

---------------------------------------------------------------------------------------------------

## 1. `axpilot.perception` — snapshots, refs, diffs (pure Python)

### `axpilot/perception/model.py`
```python
@dataclass(slots=True, eq=False)
class Locator:
    pid: int
    window_id: int | None
    path: tuple[tuple[str, int], ...]   # (ax_role, index among same-role siblings) from the root
    ax_role: str
    identifier: str | None
    name: str | None

@dataclass(slots=True, eq=False)
class Element:
    ref: str                     # "e12" ("o3" for OCR text); session-unique and carried across snapshots
                                 # (see refs.py); a plain build_snapshot numbers e1…eN in pre-order
    role: str                    # short role, see short_role()
    ax_role: str
    subrole: str | None
    name: str | None             # best label: title > label > description > placeholder (stripped, truncated 120)
    value: str | None            # stringified value, truncated to value_max_chars; None for secure fields
    frame: Rect | None
    depth: int
    enabled: bool
    focused: bool
    selected: bool
    expanded: bool | None
    secure: bool
    interactive: bool
    actions: tuple[str, ...]
    extra: dict[str, Any]        # e.g. {"rows": 482}, {"scrollable": True}, {"url": "..."}, {"identifier": "..."}
    locator: Locator
    handle: Any                  # engine handle (never serialized)
    parent_ref: str | None

SnapshotMode = Literal["interactive", "full", "text"]

@dataclass(slots=True)
class Snapshot:
    snapshot_id: str             # "s_" + 4-6 lowercase hex/alnum chars
    app: AppInfo
    window: WindowInfo | None
    mode: SnapshotMode
    elements: list[Element]      # the page returned to the model (after offset/max_elements)
    all_elements: list[Element]  # every candidate element (refs resolve against this)
    focused_ref: str | None
    total_nodes: int             # nodes in the raw tree
    offset: int
    truncated: bool              # engine stopped early (budgets/timeouts)
    menus: list[str]             # top-level menu titles (without the Apple menu) if captured
    created_at: float            # time.monotonic()
    ref_session: str | None = None  # RefCounter.session that numbered the refs (None: plain build_snapshot)
    def element(self, ref: str) -> Element | None   # O(1): lazily built ref index (init=False field)

@dataclass(slots=True)
class SnapshotDiff:
    before_id: str
    after_id: str
    app_changed: bool
    window_changed: bool
    focus_before: str | None     # element labels (name, or short role), not refs
    focus_after: str | None
    added: list[Element]         # elements of `after` with no match in `before`
    removed: list[Element]       # elements of `before` with no match in `after`
    changed: list[tuple[Element, Element]]  # matched (before, after) whose value/state changed
    def is_empty(self) -> bool: ...
```

### `axpilot/perception/roles.py`
- `short_role(ax_role: str, subrole: str | None = None) -> str` — AXButton→`button`,
  AXTextField→`field` (AXSearchField subrole→`search`, AXSecureTextField→`secure`), AXTextArea→`textarea`,
  AXStaticText→`text`, AXCheckBox→`checkbox` (AXToggle→`toggle`, AXSwitch→`switch`), AXRadioButton→`radio`
  (AXTabButton subrole→`tab`), AXPopUpButton→`popup`, AXMenuButton→`menubutton`, AXComboBox→`combobox`,
  AXSlider→`slider`, AXIncrementor→`stepper`, AXLink→`link`, AXRow→`row`, AXCell→`cell`, AXTable→`table`,
  AXOutline→`outline`, AXList→`list`, AXTabGroup→`tabs`, AXImage→`image`, AXWebArea→`webarea`,
  AXScrollArea→`scrollarea`, AXGroup→`group`, AXWindow→`window`, AXSheet→`sheet`, AXDialog/AXSystemDialog
  subrole→`dialog`, AXToolbar→`toolbar`, AXMenuBar→`menubar`, AXMenuBarItem→`menu`, AXMenu→`menu`,
  AXMenuItem→`menuitem`, AXDisclosureTriangle→`disclosure`, AXColorWell→`colorwell`, AXSplitter→`splitter`,
  AXHeading→`heading`, AXProgressIndicator→`progress`, AXBusyIndicator→`busy`, AXValueIndicator→`indicator`,
  AXDateField→`date`, AXApplication→`app`; otherwise the AX role without the `AX` prefix, lowercased.
- `INTERACTIVE_ROLES: frozenset[str]` (ax roles) and `is_interactive(node: RawNode) -> bool`:
  interactive role, or any action other than AXShowMenu/AXScrollToVisible, or a focusable text role.

### `axpilot/perception/snapshot.py`
```python
def build_snapshot(tree: RawTree, *, snapshot_id: str, mode: SnapshotMode = "interactive",
                   max_elements: int = 150, offset: int = 0, value_max_chars: int = 200) -> Snapshot
```
Pruning (mode `interactive`): skip `visible=False` nodes and their subtrees, zero-size frames, nodes
fully outside the window frame; keep interactive elements, plus non-interactive *context* elements that
carry a name and are direct ancestors-of-interest (lists/tables/outlines/scrollareas/webareas/tabs/
sheets/dialogs/toolbars/headings). Groups without a name are collapsed (their children promoted).
Lists/tables/outlines report `extra["rows"]`. Mode `full`: every visible node. Mode `text`: static text
and headings only (reading view). `elements` = `all_elements[offset:offset+max_elements]`.
Refs are *local*: `e1…eN` in pre-order (stable for the same tree). The server turns them into session
refs with `refs.carry_refs` (below).

### `axpilot/perception/refs.py`
```python
class RefCounter:                                   # one per SnapshotStore (= per server context)
    session: str                                    # random id of this counter
    def next(self, prefix: str = "e") -> str        # "e57": never issued before (thread-safe; "o" for OCR)
    def allocator(self, prefix: str = "e") -> Callable[[], str]
    def issued(self, prefix: str = "e") -> int      # highest number issued or reserved
    def reserve(self, refs: Iterable[str]) -> None  # never issue these numbers (refs numbered elsewhere)
def carry_refs(snap: Snapshot, previous: Snapshot | None, counter: RefCounter, *,
               history: Sequence[Snapshot] = ()) -> Snapshot
def view_of(snap: Snapshot) -> tuple[int, int | None]          # (pid, window_id)
def same_handle(a: Any, b: Any) -> bool                         # ==, guarded; None never matches
@dataclass(frozen=True) class MatchPass: name; kind: "handle"|"key"|"overlap"; key; unique; accept; structural
class ElementMatcher:                                # shared by carry_refs and diff_snapshots
    def __init__(self, before, after, *, allowed=None, on_match=None) -> None
    def pair(self, after_index: int, before_index: int) -> None
    def run(self, passes, *, before_indices=None, after_indices=None) -> dict[int, int]  # after → before
```
`carry_refs` returns a copy of `snap` whose refs are session refs: every element matching an element
of `previous` keeps that element's ref; elements of older `history` snapshots keep theirs when the engine
handle is equal; all others get fresh numbers from the counter in document order (`o` refs for OCR text,
never carried). Only snapshots of the same pid and window numbered by the same counter are used (a
`previous` that does not qualify is replaced by the first qualifying `history` snapshot); refs never cross
apps or windows. Matching passes, strongest first, each earlier element used at most once: engine handle
equality (same AX role; hashing guarded against `TypeError`, falling back to a linear scan) →
`AXIdentifier`+AX role (unique on both sides) → AX role+locator path+name (named only) → AX role+name+
rounded frame → AX role+name (unique on both sides) → position, for **unnamed** elements only, under a
parent that kept its ref (same path with overlapping frames, else intersection-over-union ≥ 0.5). A named
element is never matched by position alone. List rows (`AXRow`) are matched first; a row whose name
changed never keeps its ref (even with an equal handle: table views recycle rows), and an element inside a
row keeps its ref only if its row kept the row's ref. `parent_ref`, `focused_ref` and the page are
remapped; the copy's `ref_session` is `counter.session`.

### `axpilot/perception/format.py`
```python
def format_snapshot(snap: Snapshot) -> str          # compact text, see example below
def snapshot_payload(snap: Snapshot) -> dict        # JSON for structuredContent (see server schema)
def format_element_line(el: Element) -> str
def format_diff(diff: SnapshotDiff, max_items: int = 30) -> str
def diff_payload(diff: SnapshotDiff, max_items: int = 50) -> dict
```
Text format (indent 2 spaces per depth level relative to the shallowest shown element, capped at 6 levels):
```
app Notes (com.apple.Notes, pid 812) · window "Notes" [0,25 1440×875] · snapshot s_7f3a
focused: e7 textarea "Note body"
e1  button   "New Note"                 (1180,52)
e3  list     "Notes"  12 rows, scrollable
e4    row    "Shopping list — 10:42"  selected
e7  textarea "Note body"  value="Milk, eggs…"  (720,400)
menus: Notes · File · Edit · Format · View · Window · Help   (use select_menu)
42 of 318 elements shown (interactive). More: offset=42.
```
Coordinates shown are element **centers** in points, rounded to ints. States appended as words:
`disabled`, `selected`, `focused`, `expanded`/`collapsed`, `secure`. Values quoted and truncated with `…`.

### `axpilot/perception/diff.py`
`def diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDiff` — nothing is matched when the app
or window differs. Otherwise, when both snapshots share a `ref_session`, elements with the same ref are
paired first (refs are carried, so the same ref is the same element); the rest are matched by
(identifier) → (locator path + name) → (ax_role, name, rounded frame) → (ax_role, name, unique) →
(locator path, frames overlap) on `refs.ElementMatcher`; with shared refs the last pass only pairs unnamed
elements (a named element under another ref at the same place is another element, e.g. a list row showing
other content). `match_snapshots(before, after) -> dict[int, int]` exposes the pairing.

### `axpilot/perception/dropdown.py`
What an opened dropdown shows, over raw trees: `visible_handles(root) -> HandleIndex` (the baseline taken
before opening), `find_node(root, handle) -> (node, ancestors) | None`, `in_web_content(ancestors)`,
`option_title(node)`, `menu_options(menu) -> list[Option]` (titled visible `AXMenuItem`s; separators are
untitled), `open_menu_of(control)` (its visible `AXMenu` child or grandchild with items), `new_menus(root,
baseline)` (visible menus not visible before, outermost first, menu bar excluded), `page_options(root,
baseline, *, exclude=None) -> OptionList | None` (new titled option-like nodes in a web page, the outermost
of nested ones, grouped by their nearest `AXList`/`AXMenu`/`AXTable`/`AXOutline`; a real list wins, other
groups need two options), `options_in(root)` (a pop-up window's first menu, else its biggest list),
`shows_choice(node, title)` (value, title or description equal or containing it), `text_near(root, control,
title, levels=3)` (static text outside lists near the control), `fold(text)` and `match_option(names,
option) -> int` (exact, the first of identical titles, then unique prefix, also after leading marks such
as an emoji; case-insensitive, "…"/"..." and a trailing "." ignored; a word inside a longer title never
matches; `INVALID_ARGUMENT` listing the options, or the titles that contain it). `Option(title, node)`;
`OptionList(container, options, where: "menu"|"app_menu"|"page"|"window", in_web, window_id, control)`
(`control`: for `menu`, the control whose child the list is; its `AXExpanded`, once True, tells whether the
list is shown).

### `axpilot/perception/find.py`
`def find_elements(snap: Snapshot, *, text: str | None = None, role: str | None = None,
identifier: str | None = None, limit: int = 20) -> list[tuple[Element, float]]` — searches
`all_elements`; case-insensitive fuzzy match over name/value/identifier/description (exact > prefix >
substring > token overlap; `difflib` ratio as tiebreaker); `role` accepts short or AX roles. Sorted by score desc.

### `axpilot/perception/store.py`
```python
class SnapshotStore:
    def __init__(self, capacity: int = 32, *, refs: RefCounter | None = None) -> None
    refs: RefCounter                                       # the session's ref counter
    def new_id(self) -> str
    def put(self, snap: Snapshot) -> None                  # reserves its ref numbers; ValueError if another
                                                           # session's counter numbered it
    def get(self, snapshot_id: str) -> Snapshot           # raises SNAPSHOT_NOT_FOUND
    def latest(self, pid: int | None = None) -> Snapshot | None
    def snapshots_of(self, pid: int, window_id: int | None, *, limit: int | None = None) -> list[Snapshot]
        # stored snapshots of one app window, newest first
    def resolve(self, ref: str, snapshot_id: str | None = None) -> tuple[Snapshot, Element]
        # ref "e12", "o3" (or "s_ab12:e12"). With a snapshot id: only that snapshot. Without: the latest
        # snapshot, then every other stored snapshot numbered by `refs`, newest first (a session ref names
        # one element; plain local refs are positional, so such snapshots only answer as the latest).
        # STALE_REF when no snapshot lists it ("never issued" / "gone or expired"), when a newer snapshot of
        # the same window lists its element (equal handle) under another ref, or for an o-ref whose window
        # was captured again since. Raises SNAPSHOT_NOT_FOUND / STALE_REF / INVALID_ARGUMENT.
        # Callers still act through resolve.live_handle (element_valid, re-find by locator, STALE_REF).
        # Input tools aim at an element's geometry in the newest snapshot of its window that lists its
        # ref; when newer snapshots no longer list it, they re-read its frame through the live handle,
        # and refuse rows (and elements in rows) with STALE_REF, since table views reuse rows.
def describe_refs(snap: Snapshot) -> str                   # "e1–e12, e30, o7–o9" for error messages
class CaptureStore:  # same idea for screenshots (Capture objects), ids "c_xxxx"
    def __init__(self, capacity: int = 16) -> None
    def put(self, capture: Capture) -> str
    def get(self, capture_id: str | None) -> Capture     # None → latest; raises SNAPSHOT_NOT_FOUND
```
Thread-safe (a `threading.Lock`). LRU eviction.

### `axpilot/perception/resolve.py`
`def live_handle(engine: Engine, snap: Snapshot, el: Element) -> Any` — returns `el.handle` if
`engine.element_valid`; otherwise re-captures the tree for `snap.app.bundle_id or pid`/window and matches
by locator (identifier, then path, then role+name); raises `STALE_REF` if not found.

### `axpilot/perception/serialize.py`
JSON round-trip for fixtures and `axpilot debug dump-tree`:
`tree_to_dict(tree: RawTree) -> dict`, `tree_from_dict(data: dict) -> RawTree` (handles become string
ids `"n<index>"` in the fake engine). Format:
```json
{"app": {"name": "Notes", "pid": 812, "bundle_id": "com.apple.Notes"},
 "window": {"window_id": 101, "title": "Notes", "frame": [0,25,1440,875]},
 "root": {"role": "AXWindow", "title": "Notes", "frame": [0,25,1440,875], "children": [ ... ]},
 "menu_bar": { ... } }
```
Node keys mirror `RawNode` field names; `frame` is `[x,y,w,h]`; omitted keys take dataclass defaults.

---------------------------------------------------------------------------------------------------

## 2. `axpilot.engine.fake` — in-memory desktop (pure Python)

`class FakeEngine(Engine)`; `FakeEngine.demo() -> FakeEngine` (bundled fixture
`src/axpilot/engine/fixtures/demo_desktop.json`), `FakeEngine.from_fixture(path) -> FakeEngine`.
Desktop fixture = `{"os_version": "15.4", "displays": [...], "apps": [...], "windows": [{..., "tree": <node>}],
"menus": {"<bundle_id>": <MenuNode dict>}, "extras_menus": {"<bundle_id>": [<status item dicts>]},
"clipboard": "...", "permissions": {...}}`. `extras_menus` are the app's menu bar extras
(`menu_bar(..., extras=True)`, `menu_select(..., extras=True)`; a one-title path "clicks" the status item
and applies its `on_select`).
Simulation rules: `AXPress` on a button records the press in `engine.events` and, if the node has
`"on_press"` in the fixture (e.g. `{"open_window": 102}`, `{"set_value": {"id": "...", "value": ...}}`,
`{"focus": "<identifier>"}`), applies it; `set_value`/`insert_text`/`type_text` update the focused or
target text node's `value`; `press_key` records; clicks hit-test frames and focus/press the deepest
interactive node; `app_control`/`window_control` update state; `screenshot` returns a real tiny PNG/JPEG
(generated with Pillow) of the requested region size scaled to `max_edge`; `ocr` returns static text nodes
intersecting the capture region. Every mutating call appends to `engine.events: list[tuple[str, dict]]`
so tests can assert on it. `hid_idle_seconds()` returns a settable attribute (default 999).
Demo fixture must contain at least: Finder (front), TextEdit (document window with text area, toolbar,
format popup), Notes (list with rows, search field, New Note button), a login sheet with a secure field
(app "Login Demo"), Terminal (window with text area), 1Password (to test deny lists), System Settings
with a "Privacy & Security" window, Control Center with status items (Wi‑Fi, Battery, Clock), a TextEdit save panel (File › Save…; ⇧⌘G
opens its "Go to the folder" field), two displays. Fake-only node keys `default` (the button Return
presses from a one-line field of that sheet or window) and `keys` (shortcuts a visible element handles
itself) model AppKit's default buttons and panel key equivalents.
Element menus: a node's `menu` (flat menu items) is shown when it is pressed or sent `AXShowMenu`; a
pop-up with `options` and no `on_press` shows its options (the current one `selected`); an
`AXPopUpButton`/`AXMenuButton` with a fixture `AXMenu` child shows that child (web `<select>`). One menu is
open at a time, with fresh item handles, as the element's `AXMenu` child (`menu_at: "child"`, default) or the
last child of the window root (`"app"`). Pressing an item (AX or click) chooses it and closes the menu;
`AXCancel`, Escape, Return, pressing the owner again, a click outside (swallowed), another app coming
forward or the window closing close it without choosing; other keys and typing go nowhere while it is
open. `pointer_only: true` (a web control whose script listens to the mouse): `AXPress`, `AXShowMenu`,
`set_value` and `insert_text` change nothing (recorded as `ignored`), clicks work. `filters: {"list": id,
"limit": n}` on a text field: when its text changes, the list shows the first `n` children whose label
contains it. Clicks reach elements inside zero-size wrappers. Test helpers: `menu_state()` (the open
menu's owner and item titles) and `set_enabled(ref, enabled)`. The demo desktop also has menus on Notes'
Format and Share and Finder's Action buttons, an app-level pop-up menu (TextEdit's line spacing), a Google
Chrome checkout page (a `<select>`, a mouse-only combo box, an autocomplete that opens with Down and filters,
an ARIA menu), a Slack (Electron) picker and an IntelliJ (Java) combo box whose list is a window.

---------------------------------------------------------------------------------------------------

## 3. `axpilot.input` (pure Python)

### `axpilot/input/keys.py`
- `parse_combo(text: str) -> KeyCombo` — accepts `"cmd+shift+t"`, `"⌘⇧T"`, `"Command-Shift-T"`,
  `"ctrl+alt+delete"`. Modifier aliases: cmd/command/⌘/meta/super/win→`cmd`; option/opt/alt/⌥→`option`;
  ctrl/control/^/⌃→`ctrl`; shift/⇧→`shift`; fn/globe→`fn`. Key aliases: enter/return/↩→`return`, esc→`escape`,
  backspace/delete/⌫→`delete`, del/forwarddelete/⌦→`forward_delete`, space/spacebar→`space`, tab/⇥→`tab`,
  up/down/left/right (and arrows ↑↓←→), home, end, pageup/pgup→`page_up`, pagedown/pgdn→`page_down`,
  f1–f20, capslock→`caps_lock`, volumeup/volumedown/mute, single characters (letters lowercased unless
  shift is implied — uppercase letter adds `shift`). Raises `INVALID_ARGUMENT` for unknown keys.
- `parse_sequence(keys: str | Sequence[str]) -> list[KeyCombo]` — a list, or a string with spaces
  between combos (`"cmd+a delete"`).
- `KEY_NAMES: frozenset[str]` and `US_ANSI_KEYCODES: dict[str, int]` (macOS virtual key codes for named
  keys and ANSI characters — used by the engine as the fallback map).
- `MODIFIER_FLAGS: dict[str, int]` — CGEventFlags masks (cmd 0x100000, shift 0x20000, option 0x80000,
  ctrl 0x40000, fn 0x800000).

### `axpilot/input/coords.py`
`def to_screen_point(x: float, y: float, space: Literal["points","screenshot"], capture: Capture | None) -> Point`
(raises `INVALID_ARGUMENT` if `space="screenshot"` without a capture).

---------------------------------------------------------------------------------------------------

## 4. `axpilot.policy` (pure Python)

### `axpilot/policy/profiles.py`
```python
TOOLSETS: dict[str, tuple[str, ...]]   # observe, input, apps, system, scripting, browser, computer
TOOL_ORDER: tuple[str, ...]            # canonical deterministic order (toolset order, then tool order)
PROFILES: dict[str, frozenset[str]]
def enabled_tools(profile: str, enable: Sequence[str] = (), disable: Sequence[str] = ()) -> list[str]
def toolset_of(tool: str) -> str
```
Toolsets:
- observe: snapshot, screenshot, find, explore, read, inspect, ocr, list_apps, list_windows, list_displays, wait_for, status
- input: click, type_text, press_keys, scroll, drag, move_mouse, set_value, select_option, perform_action, select_menu, fill_form, handle_dialog
- apps: app, window, open
- system: clipboard, notify, spotlight_search, system_info, system_control, processes
- scripting: shell, applescript, shortcuts, defaults
- browser: browser_tabs, browser_read, browser_eval_js
- computer: computer

Profiles: `minimal` = snapshot, screenshot, click, type_text, press_keys, scroll, app, wait_for, status.
`standard` = observe − list_displays + input + apps + clipboard. `full` = observe + input + apps + system +
scripting + browser. `computer` = computer, status. `enable`/`disable` accept tool or toolset names
(unknown names raise `INVALID_ARGUMENT`). Result is ordered by `TOOL_ORDER`.

### `axpilot/policy/rules.py`
```python
TERMINAL_BUNDLE_IDS: frozenset[str]   # Terminal, iTerm2, Ghostty, Warp, kitty, Alacritty, WezTerm, Hyper, Tabby
class Policy:
    def __init__(self, safety: SafetyConfig, enabled: Iterable[str]) -> None
    def check_app(self, app: AppInfo | None, *, kind: Literal["observe","input","capture","manage"],
                  window_title: str | None = None) -> None    # raises BLOCKED_BY_POLICY
    def check_secure_target(self, secure: bool) -> None        # typing into secure fields
    def requires_confirmation(self, tool: str, *, destructive: bool) -> bool
    def capture_exclusions(self) -> list[str]                  # bundle ids to hide from screenshots
    def is_terminal(self, app: AppInfo | None) -> bool
```
Rules: deny_apps (bundle id or case-insensitive name) block observe/input/capture; allow_apps (if set)
blocks everything else; deny_windows (`bundle::title-substring`) block when the window matches;
terminal apps are blocked for `input` unless `shell` is enabled; AXPilot never blocks *reading* its own
status. `manage` (launching, activating, hiding, quitting an app; moving, resizing, closing its windows) gets
every rule of `input` (deny/allow lists, protected windows, AXPilot itself, authentication dialogs) except the
Terminal rule. Messages must name the app and the rule.

### `axpilot/policy/ratelimit.py`
`class RateLimiter(max_per_minute: int, clock=time.monotonic)`; `.check() -> None` raises `RATE_LIMITED`
(sliding 60 s window).

### `axpilot/policy/audit.py`
```python
@dataclass class AuditEvent: tool: str; client: str | None; args: dict; ok: bool;
    error_code: str | None; duration_ms: float; summary: str | None = None
class AuditLog:
    def __init__(self, path: Path | None, *, enabled: bool = True, log_args: str = "redacted") -> None
    def record(self, event: AuditEvent) -> None   # appends one JSON line with an ISO-8601 UTC timestamp
    def tail(self, n: int = 50) -> list[dict]
def redact_args(args: Mapping[str, Any], mode: str) -> dict
```
Redaction (`redacted`): string values of keys `text`, `value`, `source`, `script`, `command`, `input`,
`values`, `js`, `keys`, `shortcut`, `labels`, `locs`, `image_png_base64` become `"<redacted N chars>"`;
nested lists/dicts, pydantic models and dataclasses handled; everything else kept. Never
raises on I/O errors (log to stderr once). File created with mode 0600.

### `axpilot/policy/confirm.py`
```python
@dataclass class ConfirmRequest: tool: str; summary: str; detail: str | None = None; client: str | None = None
class Confirmer(Protocol):
    async def confirm(self, request: ConfirmRequest) -> bool: ...
class AutoConfirmer:  # allow: bool — used for tests and `--yes`
class DenyConfirmer:  # always False (headless default when no UI is available)
```

### `axpilot/policy/control.py`
```python
class ControlState:
    def __init__(self, engine: Engine, *, yield_to_user: bool = True, idle_threshold_s: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None
    paused: bool; pause_reason: str | None
    def pause(self, reason: str = "kill switch") -> None
    def resume(self) -> None
    def toggle_pause(self, reason: str = "kill switch") -> bool   # the kill switch; returns the new state
    def add_listener(self, listener: Callable[[str, str | None], None]) -> None  # turn_start/turn_end/paused/resumed
    def asking_user(self) -> ContextManager[None]       # a question is open: actions get NEEDS_USER
    def hold_question(self, key: str, ttl_s: float) -> None   # same, for a question the MCP client shows
    def release_question(self, key: str) -> bool         #   between two rounds of a call (until released/expired)
    waiting_for_user: bool
    @asynccontextmanager
    async def input_turn(self, timeout_s: float = 5.0)   # serializes input (one virtual hand);
        # raises PAUSED_BY_USER if paused, USER_ACTIVE if engine.hid_idle_seconds() < threshold,
        # BUSY if the lock cannot be acquired within timeout_s
```

---------------------------------------------------------------------------------------------------

## 5. `axpilot.system` — command-line integrations (pure Python, subprocess-based)

### `axpilot/system/runner.py`
```python
@dataclass class CommandResult: argv: list[str]; exit_code: int; stdout: str; stderr: str;
    timed_out: bool = False; truncated: bool = False
class CommandRunner:
    def run(self, argv: Sequence[str], *, input_text: str | None = None, timeout_s: float = 30,
            cwd: str | None = None, env: Mapping[str, str] | None = None, max_output: int = 20_000) -> CommandResult
class FakeRunner(CommandRunner):  # tests: register canned results by argv prefix; records calls
```
Never `shell=True` except inside `shell.run_shell`, which runs `[shell, "-lc", command]`.

### modules
- `osascript.py`: `run_applescript(runner, source, *, language="applescript"|"javascript", timeout_s) -> CommandResult`;
  `applescript_string(s: str) -> str` (quote/escape for embedding); maps "Not authorized to send Apple
  events" (-1743) to `PERMISSION_AUTOMATION`.
- `shell.py`: `run_shell(runner, command, *, shell="/bin/zsh", cwd=None, timeout_s=30, max_output=20000) -> CommandResult`.
- `spotlight.py`: `spotlight_search(runner, query, *, kind=None, scope=None, limit=20) -> list[str]` (mdfind; kind
  maps app/pdf/image/folder/document/... to kMDItemContentTypeTree queries; `-onlyin` for scope).
- `shortcuts.py`: `list_shortcuts(runner) -> list[str]`, `run_shortcut(runner, name, input_text=None, timeout_s=60) -> CommandResult`.
- `defaults.py`: `defaults_read(runner, domain, key=None) -> str`, `defaults_write(runner, domain, key, value, type_=None)`
  (type inferred: bool→-bool, int→-int, float→-float, str→-string).
- `processes.py`: `list_processes(runner, *, name=None, limit=50) -> list[ProcessInfo]` (`ps -axo pid=,ppid=,%cpu=,%mem=,comm=`),
  `kill_process(pid, *, force=False)` (refuses pid ≤ 1, our own pid and our parent's with `BLOCKED_BY_POLICY`);
  `check_kill_allowed(pid)` (the same refusal without signalling) and `process_exists(pid) -> bool` (signal 0), so
  the `processes` tool refuses before it asks the user.
- `sysinfo.py`: `system_info(runner) -> dict` (sw_vers, `sysctl -n hw.model hw.memsize machdep.cpu.brand_string`,
  `pmset -g batt`, appearance via `defaults read -g AppleInterfaceStyle`, uptime), `set_volume(runner, level|None, mute|None)`,
  `set_appearance(runner, dark: bool)` (osascript System Events).
- `browser.py`: `BROWSERS` table (Safari: `com.apple.Safari`; Chromium family: Chrome, Arc, Edge, Brave, Chromium, Vivaldi)
  and AppleScript builders/parsers: `list_tabs(runner, browser) -> list[TabInfo]`, `activate_tab`, `open_url`,
  `close_tab`, `eval_js(runner, browser, js, tab=None) -> str`, `frontmost_browser(runner) -> str | None`.

---------------------------------------------------------------------------------------------------

## 6. `axpilot.server` — MCP layer

```python
# axpilot/server/context.py
@dataclass
class ServerContext:
    config: Config
    engine: Engine
    policy: Policy
    snapshots: SnapshotStore
    captures: CaptureStore
    limiter: RateLimiter
    audit: AuditLog
    confirmer: Confirmer
    control: ControlState
    runner: CommandRunner
    @classmethod
    def create(cls, config: Config, engine: Engine, *, confirmer: Confirmer | None = None,
               runner: CommandRunner | None = None) -> ServerContext
    def build_snapshot(self, tree: RawTree, *, mode="interactive", max_elements=None, offset=0,
                       store: bool = True, previous: Snapshot | None = None) -> Snapshot
        # build_snapshot + refs.carry_refs from the store's counter (stored and unstored snapshots alike);
        # previous defaults to the latest stored snapshot of the same app window, and up to 8 older stored
        # snapshots of that window are searched by handle
    async def capture_tree(self, *, app=None, window_id=None, include_menus=False, kind="observe",
                           enhance=True) -> RawTree
        # policy checked before (the target) and after (what was captured); with neither app nor window_id
        # the frontmost app is pinned by pid once checked, so an app that comes forward in between is not read
    async def take_snapshot(self, *, app=None, window_id=None, mode="interactive", max_elements=None,
                            offset=0, include_menus=False, store=True, previous=None, enhance=True) -> Snapshot

# axpilot/server/app.py
def build_server(ctx: ServerContext, *, profile: str | None = None, naming: str | None = None,
                 client_name: str | None = None,
                 server_factory: Callable[..., AXPilotServer] = AXPilotServer) -> AXPilotServer
```
Every tool is registered through `axpilot/server/registry.py` (`ToolSpec`: name, toolset, factory(ctx)->async fn,
title, description, read_only, destructive, idempotent, open_world, input_action, windows_mcp_name, confirm_text,
confirm_when, confirms_itself). `confirm_when(args) -> bool` lets a destructive tool with harmless actions too
(`app`, `processes`) be confirmed only for some calls; tools that must resolve and policy-check their target first
return False there and ask with `ServerContext.confirm_action` once the target is known. A tool with
`confirms_itself=True` (`shell`, `applescript`, `shortcuts`, `defaults`, `browser_eval_js`) is never asked by the
guard: it validates the call, applies the policy, then calls `confirm_action` on every path, with the exact
command, script or page and code in the question (`destructive=False` on harmless paths such as `shortcuts` list
and `defaults` read, so only `[safety] confirm = "always"` asks there). A shared guard wraps
each tool: tool-enabled check → rate limit (input tools) → policy → confirmation (destructive) →
`control.input_turn()` (input tools) → engine call in a worker thread → audit. Confirmations go through
`ServerContext.ask_user`: in direct mode (`ctx.elicitation`, from `[safety] elicitation`, off with `--yes`) a client
that declared form elicitation is asked first (`axpilot/server/elicit.py`): on protocol ≤ 2025-11-25 with an
`elicitation/create` request during the call; on 2026-07-28 by raising `elicit.InputRequired`, which the guard
returns as the round's `InputRequiredResult` (audited as `INPUT_REQUIRED`). The client's retry re-runs the call, and
`ask_user` accepts the answer only if its sealed `request_state` names a nonce issued here and not yet used, younger
than the confirmation timeout, for the same question digest; meanwhile `control.hold_question` blocks every acting
tool (`NEEDS_USER`). Tools therefore ask before any side effect. Without the capability, or when the client fails
the request, the context's `Confirmer` decides (`DenyConfirmer` when nobody can be asked). `AXPilotError` becomes
`CallToolResult(is_error=True, content=[TextContent(text=err.to_text())], meta={"axpilot/error": err.to_dict()})`.
Success returns `CallToolResult(content=[TextContent, (ImageContent)], structured_content=...)` with the
return annotation `Annotated[CallToolResult, <PydanticOutputModel>]` so `outputSchema` is advertised.
`tools/list` order = `TOOL_ORDER`. Tool descriptions are written for LLMs: one-line purpose, when to use,
key params, and what comes back.

Naming (`axpilot/server/naming/`): `apply_naming(specs, naming) -> [(public_name, spec, adapter | None)]`, with
`adapter(fn, ctx) -> fn` giving a tool another parameter shape (it may call other tools' unguarded functions, which
then run inside the calling tool's guard; a tool it needs must be enabled, else `TOOL_DISABLED`). `default` keeps the
canonical names. `windows-mcp` (`naming/windows_mcp.py`, whose docstring has the full table) exposes Windows-MCP's
names and parameters: Click, Type, Scroll, Move (+ drag), Shortcut, Wait, WaitFor, MultiSelect, MultiEdit,
Screenshot, Snapshot, DisplayInventory, App, Process, Clipboard, Notification, ControlStatus, Shell (PowerShell),
Defaults (Registry) and Scrape; the other tools keep their parameters under PascalCase names (`NAMES`). Adapted
tools get their own description; the others get theirs with backticked tool names renamed, and so do the server
instructions. `label=12` means ref `e12`; `loc` is in screen points. Results keep AXPilot's structured content
(the windows-mcp `App` has its own output model covering app and window results). Audit records and the policy
use canonical names.

Advertised input schemas are portable (`axpilot/server/portable.py`, applied in `AXPilotServer.list_tools`):
optional parameters show their type without a `null` branch, `$ref`/`$defs` are inlined, nested titles and
`default: null` are dropped, and objects declare `additionalProperties: false`. Validation still uses the
function's model (an explicit `null` is accepted). Real unions (a list or its JSON text) keep `anyOf`.
`[tools] prefix` (`^[A-Za-z0-9_-]{0,24}$`) is put before every public name after the naming; audit records and
the policy keep canonical names; calling a tool without the prefix gets `INVALID_ARGUMENT` naming the prefixed one.

Controls (`axpilot/server/tools/controls.py`, input toolset):
- `select_option(ref, option | index, snapshot_id, report)`: pop-up buttons, menu buttons, web combo boxes and
  native combo boxes chosen by index → the dropdown flow below; a native combo box chosen by title →
  `set_value` with the text, falling back to the flow when its value cannot be set (`ELEMENT_NOT_ACTIONABLE`);
  radio group, tab group, segmented control, group → press the matching child (`radio`, `tab`, `button`,
  `toggle`, `checkbox`); list/table/outline → click the matching row; an option element itself (radio, tab, row)
  is pressed. Matching: `perception.dropdown.match_option`; ambiguity or no match is `INVALID_ARGUMENT` listing
  the options. An already selected option is not pressed again (a pop-up that already shows it is not opened).
- The dropdown flow (`axpilot/server/dropdown.py`): `choose_option(ctx, target, *, option, index) ->
  DropdownOutcome(strategy: "menu"|"menu_window"|"listbox"|None, chosen, message, attempts, warnings)`, also
  used by `set_value`/`fill_form` on pop-ups. `in_web(ctx, target)`: the control's locator path has an
  `AXWebArea`, or its app is Electron/CEF. Steps: capture the window (baseline: visible handles, the app's
  window ids); open (native: `AXPress`, else `AXShowMenu`, else a mouse click; web: a mouse click); poll up to
  2 s, re-capturing, for the control's menu, a new menu, (web) a new option list, or a new app window with a
  menu or list; a web text combo box with nothing shown gets Down; a web text combo box without a matching
  option gets the title typed once (filter: wait up to 1.5 s for a match). Pick (title or index; disabled →
  `ELEMENT_NOT_ACTIONABLE`), press (menu items and native lists: `AXPress`; web options: a mouse click on
  their visible part, `AXScrollToVisible` or `AXPress` when off screen, refused when covered by another app).
  Verify: a list still open is closed (warning); a menu button needs nothing more; the control showing the
  title passes; a native pop-up that does not is `ELEMENT_NOT_ACTIONABLE` ("it shows …"); a web control
  passes when static text near it or the option's selected state shows the choice, else a warning. Every
  failure or cancellation after opening (the kill switch included) runs a shielded, bounded clean-up: typed
  filter text is put back (select all, delete, retype; not while paused or in the background), and what was
  opened is located if needed and closed (`AXCancel`, else Escape when the app is frontmost, not paused and
  secure input is off). Input is never repeated; each rung is tried only when the previous visibly did
  nothing, and `attempts` lists them (`open:ax:AXPress`, `open:mouse`, `open:keys:down`, `filter:keys`,
  `pick:ax:AXPress`, `pick:mouse`).
- `handle_dialog(app, action=inspect|accept|cancel|press, button, text, report)`: the first `sheet`/`dialog`
  element of a full snapshot, else the window itself when its root subrole is `AXDialog`/`AXSystemDialog`.
  accept = OK/Allow/Continue/Done/Open/Save/Yes/Sign In/… else the bottom-rightmost enabled non-cancel button;
  cancel = Cancel/Not Now/Don't Allow/No/Close/… else Escape. `text` fills the first editable field (via
  `set_value`, so secure fields are refused). Destructive with `confirm_when` never: a button matching
  `DESTRUCTIVE_BUTTON_RE` (Delete, Replace, Don't Save, Trash…) asks the user first.

Opt-in tools (`axpilot/server/tools/scripting.py`, `browser.py`, `computer.py`):
- `shell(command, cwd="~", timeout_s=[scripting] shell_timeout_s)`: `zsh -lc` (the configured shell); a timeout above
  the configured limit is `INVALID_ARGUMENT`; a non-zero exit or a timeout is a normal result (`exit_code`,
  `timed_out`); output cut at `[scripting] output_max_chars`.
- `applescript(script, language=applescript|javascript, timeout_s=30)`: osascript with the script on stdin. Apps the
  script names literally (`application "X"`, `application id "…"`, `Application('X')`) are resolved against running
  and installed apps and checked with `kind="input"` (deny/allow lists, the Terminal rule); a System Events script
  is also checked against the frontmost app. Best effort, documented as such.
- `shortcuts(action=list|run, name, input, query, limit, timeout_s)` and `defaults(domain, action=read|write, key,
  value, type)`; the domain's app is checked (`observe` to read, `manage` to write).
- `browser_tabs(action=list|activate|open|close, browser, window, tab, url, new_window)`,
  `browser_read(browser, include_links, offset, max_chars)` and `browser_eval_js(js, browser, window, tab)`. The
  browser is the frontmost supported one, else the first running in `BROWSERS` order. `browser_read` reads the
  largest AXWebArea of the front window from the raw tree (headings `# …`, inline runs joined, fields, buttons;
  secure fields skipped), pages by characters (`next_offset`), and falls back to `document.body.innerText` only when
  the tree has no page text and `browser_eval_js` is enabled. Tabs whose title matches `deny_windows` are hidden
  from listings and cannot be activated, closed or scripted.
- `computer(action, coordinate, start_coordinate, text, scroll_direction, scroll_amount, duration,
  return_screenshot)`: actions `screenshot`, `left_click`, `right_click`, `middle_click`, `double_click`,
  `triple_click`, `mouse_move`, `left_click_drag`, `left_mouse_down`, `left_mouse_up`, `type`, `key`, `scroll`,
  `wait`, `cursor_position`. Coordinates are pixels of the last screenshot this server's `computer` took (state per
  `build_server`); input actions take the limiter and the input turn themselves (screenshots and the cursor do
  not), clicks/drags/scrolls hit-test the policy, `mouse_move` does not. `left_mouse_down` + `left_mouse_up` are
  performed together on release (click, or drag if the pointer moved). One scroll notch = 3 lines.

---------------------------------------------------------------------------------------------------

## 7. Host, bridge, CLI

- `axpilot/bridge/protocol.py`: newline-delimited JSON, at most `MAX_LINE_BYTES` (4 MiB) per line. The bridge's
  first line is a hello:
  `{"axpilot_bridge": 1, "client": {"parent_app": str|None, "parent_bundle_id": str|None, "pid": int},
  "profile": str|None, "naming": str|None}`; the host replies `{"ok": true, "server_version": "..."}` or
  `{"ok": false, "error": {...}}` (an `AXPilotError` dict). A hello with a newer `axpilot_bridge` version gets
  `NOT_SUPPORTED`; a malformed one, or an unknown profile or naming, `INVALID_ARGUMENT`. After the reply both
  directions carry raw MCP JSON-RPC lines; a line that is not JSON gets a JSON-RPC `-32700` parse error and the
  session goes on. `ClientIdentity.key` (bundle id, else app name, else `pid:<n>`) keys pairing decisions.
  `LineReader` reads lines from any anyio byte stream.
- `axpilot/bridge/stdio_pipe.py`: `async def run_bridge(socket_path, hello, *, read_input=None,
  write_output=None, reply_timeout_s=REPLY_TIMEOUT_S) -> int` sends the hello, waits for the reply (pairing may
  ask the user first: up to the confirmation limit + 15 s), raises the host's error on a refusal and
  `HOST_UNAVAILABLE` when the host is unreachable or silent, then pumps stdin→socket and socket→stdout. When
  stdin ends it half-closes the socket and keeps forwarding until the host closes; returns `0`. Only the host's
  bytes are written to stdout.
- `axpilot/bridge/offline.py`: the stand-in `axpilot stdio` serves when it cannot reach the host.
  `build_unavailable_server(error, config, *, profile=None, naming=None) -> UnavailableServer` lists the tools
  the host would offer (same names, descriptions and schemas, via `build_server(server_factory=...)`), puts the
  error and its hint at the start of the instructions, and answers every listed tool with `error_result(error)`
  (unknown names get the usual answers); it never calls its engine (an empty fake desktop) and audits nothing.
  `STAND_IN_ERRORS` = `HOST_UNAVAILABLE`, `USER_DENIED`, `BUSY`, `NOT_SUPPORTED`: raised by
  `ensure_host_running` or by `run_bridge` before the session starts, they are printed on stderr and the
  stand-in serves stdin/stdout (exit `0` when stdin ends); other errors (an unknown profile) still exit.
- `axpilot/bridge/launch.py`: `ensure_host_running(socket_path=None, *, timeout_s=10, ...) -> Path` returns at
  once if a host answers; on macOS runs `open -g -a <launcher>` and polls the socket; elsewhere (and when the
  launcher is missing) raises `HOST_UNAVAILABLE` with a hint to use `--direct` or `--engine fake`.
  `identify_parent()` walks the parent processes with `ps` and names the outermost enclosing `.app` (name and
  `CFBundleIdentifier`), else the nearest ancestor's command; it never raises.
- `axpilot/bridge/client.py`: `host_transport(socket_path=None, hello=None)`, an async context manager yielding
  MCP `(read_stream, write_stream)` to the host, for `mcp.Client(host_transport())` in-process (the SDK's
  `Desktop.connect()` and the tests).
- `axpilot/input/hotkeys.py`: `parse_hotkey(text) -> Hotkey(key, key_code, modifiers)` (US-ANSI key code,
  `carbon_modifiers`, `label` such as `⌃⌥⌘.`); a key needs cmd, ctrl or option, never fn (`INVALID_ARGUMENT`).
  `[safety] kill_switch` is validated with it at config load.
- `axpilot/host/killswitch.py` (pure): `KillSwitch(control, hotkey).pressed() -> bool` toggles
  `ControlState.toggle_pause` (presses < 0.5 s apart count once; never raises). `Indicator(view, hotkey_label)`
  listens to `ControlState.add_listener` events (`turn_start`/`turn_end`/`paused`/`resumed`) and its `tick()`
  (main thread) shows "controlling" while a turn runs and 2 s after, "paused" while paused, else hides.
- `axpilot/host/ui/` (PyObjC, macOS only, not yet run on a Mac): `start(control, *, kill_switch, indicator) ->
  UI | None` starts `AppKitPump` (accessory `NSApplication`; `pump()` dispatches queued events, turns the run
  loop and runs the ticks), registers `CarbonHotkey` (`RegisterEventHotKey` through ctypes) and the `Overlay`
  (one click-through, all-Spaces window per screen, `NSWindowSharingNone`). Each part degrades to a warning;
  `None` when nothing runs. The CLI starts it for `stdio --direct` and `host` with a non-fake engine on macOS
  and passes `ui.pump` to `axpilot.runloop`.
- `axpilot/host/daemon.py`: `async def serve(ctx, socket_path=None, *, pairing=None, pairing_confirmer=None,
  max_clients=8, parent_pid=None, task_status=...)` and the `Host` class behind it. With `parent_pid`
  (`launcher_pid()`: the parent pid when `AXPILOT_LAUNCHER_VERSION` is set, i.e. AXPilot.app started the host)
  serving ends once that process is no longer the parent (`watch_parent`, polled every `PARENT_POLL_S` = 1 s): a
  force-quit launcher leaves no host holding the socket. The socket is created `0600` inside a `0700`
  folder; a live host on the path is `HOST_UNAVAILABLE`, a stale socket is removed, any other file in the way
  is never deleted. Each connection: peer uid check (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS; another
  user is refused with `BLOCKED_BY_POLICY`), hello within `hello_timeout_s`, `BUSY` past `max_clients`, the
  pairing decision, then one MCP server session (`build_server(ctx, profile, naming, client_name)` with
  `mode="host"` and the client's pairing in `status`). One broken connection never stops the host; the socket
  is removed when `serve` ends. `axpilot host` stops cleanly on SIGTERM/SIGINT.
- Pairing (`axpilot/host/pairing.py`): `PairingStore(path=paths.pairing_file())` with `decision(key)`,
  `remember(key, "always"|"denied", display=...)`, `forget(key)`, `entries()`. JSON
  `{"version": 1, "clients": {key: {"decision", "decided_at", "display"}}}`, written atomically with `0600`; a
  damaged file is moved to `<name>.corrupt` and everyone is asked again. The host asks once per client app
  (one question at a time, through the pairing confirmer: a macOS dialog from `host/dialogs.py`, or `--yes`);
  a denial is remembered and refused without asking, an unanswered question is not remembered. With
  `[safety] require_pairing = false` nobody is asked.
- `axpilot/host/session.py`: `serve_session(server, reader, send)` runs one MCP session over a `LineReader` and
  a send function; `run_lowlevel` isolates the one private SDK attribute it needs (`MCPServer._lowlevel_server`,
  pinned by a test).
- `axpilot/host/http.py`: `build_http_app(ctx, *, token, host, port, allowed_origins, ...)` is the SDK's
  Streamable HTTP app at `/mcp` behind `AccessGuard`: a `Host` header other than the bound address and port, or
  an `Origin` not in `--allow-origin`, gets `403`; a missing or wrong bearer token gets `401` (constant-time
  comparison). No CORS headers. `check_bind` refuses non-loopback addresses unless `allow_remote` and a TLS
  certificate and key are given (`BLOCKED_BY_POLICY`). `get_token(store="auto"|"keychain"|"file", *, path,
  keychain) -> StoredToken(value, location)` keeps the token (≥ 32 characters, created on first use) in the macOS
  login keychain (`axpilot/system/keychain.py`: `/usr/bin/security`, the secret only on stdin/stdout, every write
  read back), moving a token from `<app support>/http-token` there; the `0600` file (`load_or_create_token`) is the
  fallback when the keychain fails and the store off macOS. `[server] token_store` selects it.
- `axpilot/cli.py` (Typer): `stdio [--direct] [--engine auto|macos|fake] [--fixture PATH] [--profile]
  [--naming] [--yes]` (in-process with `--direct`, `--engine fake` or `--fixture`, else the bridge),
  `http [--host] [--port] [--allow-remote --tls-cert --tls-key] [--allow-origin ...] [--show-token]`,
  `host [--engine] [--socket] [--max-clients] [--yes]`, `setup`, `update`, `uninstall [--remove-launcher]
  [--yes]`, `doctor`, `connect <client>|--list [--dry-run] [--scope] [--project-dir] [--force]`, `disconnect`,
  `config get|set|edit|path|show`, `skill install [--target claude|claude-project|codex] [--dest] [--force]`,
  `debug dump-tree`, `tools`, `call`, `version`; most take `--json`. `main()` is the console-script entry
  point and `python -m axpilot` runs it; `python -m axpilot.host` (what `AXPilot.app` starts, `HOST_ARGS` in
  `setup/runtime.py`) runs `axpilot host` with its arguments. Exit codes per QUALITY.md A9: `0` ok, `1` operational error, `2` usage
  error (including `INVALID_ARGUMENT`), `3` missing permission, `4` unsupported OS, `130` interrupted. Errors
  print `error: CODE: message` and `hint: ...` on stderr (`{"error": {...}}` on stdout with `--json` and for
  `call`); tracebacks only with `--debug`. Without `--yes`, confirmations use a macOS dialog, and are declined
  where no dialog can be shown (Linux, the fake engine).
- `axpilot/runloop.py`: keeps the main thread's Cocoa run loop turning (keyboard layout, `NSWorkspace` app
  list). `main_thread_pump() -> Pump | None` (`axpilot.engine.macos.runloop.pump` on macOS's main thread, else
  `None`); `async run_with_pump(main, pump=None, interval=PUMP_INTERVAL_S)` awaits `main()` while a loop
  callback calls `pump(0)` every 50 ms (exceptions pass through unchanged; used by every `cli._run`);
  `start_ticking(pump, interval) -> stop` (a no-op without a pump or outside asyncio; `AsyncDesktop` while
  open); `wait_pumping(future, pump=None, interval)` waits for a `concurrent.futures.Future` while pumping and
  cancels it if the wait is interrupted (the sync `Desktop`). A failing pump is logged once and stops ticking.
- `axpilot/clients/`: one writer per client: `ClientTarget(name, display_name, config_path(scope), format=json|toml|yaml,
  servers_key)`, `connect(target, *, command, args, dry_run, scope) -> ConnectResult(path, changed, backup, snippet)`.

---------------------------------------------------------------------------------------------------

## 8. App compatibility and vision fallback (PLAN §6.10)

- `axpilot/perception/appkind.py`
  - `AppKind = Literal["native", "catalyst", "ios", "electron", "cef", "chromium_browser", "safari", "firefox",
    "java", "qt", "office", "adobe", "unknown"]`
  - `def detect_app_kind(bundle_id: str | None, bundle_path: str | None, *, fs=os) -> AppKind` — inspects the bundle:
    `Contents/Frameworks/Electron Framework.framework`, `Chromium Embedded Framework.framework`, `Qt*.framework`,
    Java runtimes (`Contents/PlugIns/*jdk*`, `Contents/runtime`, `JavaAppLauncher`), Info.plist
    (`UIDeviceFamily`/`LSRequiresIPhoneOS` → ios; `UIApplicationSceneManifest` + Catalyst → catalyst), known bundle-id
    prefixes (`com.microsoft.`, `com.adobe.`, Chromium browsers, Safari, Firefox). Pure and testable with a fake fs.
  - `def assess_ax_quality(tree: RawTree) -> Literal["good", "partial", "poor"]` — from node count, share of
    interactive nodes with names, presence of a web area with content, frames present.
  - `@dataclass AppProfile(kind, ax_quality, strategies: list[str], notes: list[str])` and
    `def app_profile(app: AppInfo, tree: RawTree | None) -> AppProfile`.
- `axpilot/perception/vision.py`
  - `def ocr_elements(lines: list[OcrLine], *, start_index: int = 1, min_confidence: float = 0.3) -> list[Element]` —
    pseudo-elements with refs `o1…`, role `ocrtext`, frame from OCR, `interactive=True` (clickable by center),
    `handle=None`, locator path empty. Merges words on the same line, drops duplicates of AX element names.
  - `def merge_vision(snap: Snapshot, elements: list[Element], *, max_elements: int | None = None,
    allocate: Callable[[], str] | None = None) -> Snapshot` — appends to `all_elements` and the page, keeping AX
    elements first; the store resolves `o` refs too (click falls back to coordinates). With `allocate` (the
    server passes `SnapshotStore.refs.allocator("o")`) o-refs are session-unique, never reused; otherwise they
    continue after the snapshot's own. o-refs cannot be re-validated, so they resolve only until their window
    is captured again.
- Engines: `MacEngine.capture_tree` keeps enabling AX for Electron/Chromium (already implemented) for the app it
  captures, unless the caller passes `enhance=False` (`Engine.capture_tree`): captures that only report on an app
  the call did not target pass it (`observe_after` reporting the frontmost app after the target quit). The server
  decides when to add vision (`mode="auto"`: `ax_quality == "poor"` and screen recording permitted; `mode="vision"`: always).

## 9. Navigation helpers (PLAN §6.9) — `axpilot/perception/navigate.py`
- `def scroll_container_for(snap: Snapshot, el: Element | None) -> Element | None` — nearest ancestor scrollarea/
  list/table/outline/webarea/textarea (or the largest scroll area in the window when `el` is None).
- `def page_signature(snap: Snapshot, container: Element | None) -> str` — stable hash of visible content inside the
  container, used to detect "did not move" / end reached.
- `def merge_pages(pages: list[list[str]]) -> list[str]` — joins text lines from successive pages, removing the
  overlap between page *n*'s tail and page *n+1*'s head (longest suffix/prefix match).
- `def collect_rows(snap, container, *, visible_only=True) -> list[str]` and
  `def collect_items(snap, container, *, visible_only=True) -> list[tuple[str | None, str]]` — the container's
  content as lines (rows of lists/tables with cells joined by `" | "`, text lines otherwise); `collect_items` adds the
  ref of the element each line comes from (`None` for the lines of a text area's value).
- The tools `explore`, `find(scroll=True)`, `scroll`, `read` are implemented in `axpilot/server/tools/navigate.py`
  using these helpers plus engine calls; each loop is bounded (`max_scrolls` ≤ 50, overall deadline ≤ 60 s) and
  reports `at_end`, `scrolls`, `complete`. One scroll loop serves all of them: scroll, settle, re-snapshot the
  window in `full` mode (refs carried from the previous step; intermediate snapshots are not stored, the final one
  is), compare `page_signature` (unchanged = did not move). It measures how far the content moved (the container's
  content frame, or the median movement of elements listed under the same ref) and calibrates the height of a
  scroll "line" per app; the first page in an uncalibrated app starts with a 3-line probe, so a page (80% of the
  viewport) never skips content. A scroll area's document (the list/table/web area inside it) is used as the
  container. Scrolling is input: the `input` policy applies (Terminal rule, deny lists) and every scroll holds the
  input lock (`scroll` through the guard; `find`/`read`, which are read-only tools, take one turn per scroll and
  count once against the rate limit). When a loop runs out of time or scrolls, or the user takes over
  (`USER_ACTIVE`/`BUSY`) or the app stops answering (`TIMEOUT`) after the first scroll, the results so far come
  back with `complete=false` and a warning; `PAUSED_BY_USER`, policy and permission errors are raised. `read`
  joins pages by ref (`merge_items`: exact even when lines repeat), falling back to the longest text overlap when
  the app rebuilds its elements on scroll (a page without overlap is reported as a possible gap); text areas are
  read whole from their live `AXValue`; output is capped at 60 000 characters (`truncated`). `find` counts a match
  with score ≥ 0.48 (a substring of the name or better) as found; it scans down to the end, then from the top down
  to where it started. `scroll(scroll_into_view=ref)` tries `AXScrollToVisible`, then scrolls the element's
  container toward it.

## 10. Automation SDK (PLAN §6.11) — `axpilot/sdk/`
- `AsyncDesktop` (`axpilot/sdk/desktop.py`) holds an `mcp.Client` session: in-process with
  `AsyncDesktop.direct(config=None, engine=None, *, profile=None, confirm=None)` (macOS engine, the user's config;
  `confirm` True/False/a Confirmer, default a dialog on a Mac) and `AsyncDesktop.fake(fixture=None, *, config=None,
  profile="full", confirm=True)` (`make_test_context` + `FakeEngine`), or through the host with
  `AsyncDesktop.connect(socket_path=None, *, profile=None)` (`bridge.client.host_transport`, naming `default`;
  `HOST_UNAVAILABLE`, `USER_DENIED`… raised as is). `AsyncDesktop.from_context(ctx)` serves an existing context.
  Use `async with` (or `open()`/`aclose()` in one task); the first call opens lazily.
- `Desktop` is the blocking twin: an `AsyncDesktop` driven through an anyio blocking portal (its own event loop
  thread; the MCP session lives in one portal task via `wrap_async_context_manager`). Same constructors and
  methods, callable from any thread; `with Desktop.fake() as mac:` or `close()`.
- Methods mirror the tools with Pythonic signatures: `status`, `snapshot`, `screenshot`, `find`, `explore`, `read`,
  `inspect`, `ocr`, `list_apps/windows/displays`, `wait_for(…, timeout_s)`, `click`, `type_text`, `press_keys`,
  `scroll`, `scroll_into_view`, `drag`, `move_mouse`, `set_value`, `perform_action`, `menu(*path)`, `menus`,
  `fill_form({ref: value})`, `app(name)` → handle, `app_action`, `window`, `open_target`, `get/set_clipboard`,
  `notify`, `shell`, `applescript`, `browser_read`, and `call(tool, **args)` for anything else. `None`
  arguments are left out (the tool's default applies). They call the registered tools (no second code path),
  so policy, confirmations, audit (client "AXPilot SDK (<script>)") and error codes are identical.
- Results (`axpilot/sdk/results.py`) keep `data` (structured content), `text` and `images`: `ToolResult`,
  `ActionResult` (`message`, `strategy`, `snapshot_id`, `diff`, `changed`), `SnapshotResult` (`elements`,
  `focused`, `find(name, role=, exact=)` → handle, `find_all`, `element(ref)`), `FindResult` (`found`, `matches`,
  `first`; handle methods act on the best match), `ReadResult` (`content`, `lines`/`rows`, `complete`,
  `at_end`, `pages`), `ScreenshotResult` (`image`, `save(path)`). `ElementHandle`/`AsyncElementHandle`
  (`click`, `type_text`, `set_value`, `perform_action`, `inspect`, `scroll_into_view`) and
  `AppHandle`/`AsyncAppHandle` (`launch`, `activate`, `hide`, `quit`, `windows`, `snapshot`).
- A failed call raises `AXPilotError.from_dict(result.meta["axpilot/error"])` (unknown codes → `INTERNAL`).
- `from axpilot import Desktop, AsyncDesktop` is a lazy re-export (`import axpilot` stays cheap).
- `examples/scripts/*.py` run with `--fake` in CI; `examples/frameworks/*_example.py` and `ts/*_example.ts` are
  syntax-checked only.

## 11. CLI for scripts
- `axpilot tools [--profile ...] [--naming ...] [--json]` lists tools; `--json` prints
  `{"tools": [{"name", "title", "description", "input_schema", "output_schema", "annotations"}], "count"}` with
  MCP's annotation names (`readOnlyHint`, ...).
- `axpilot call <tool> [--arg key=value ...] [--json '{...}'] [--engine auto|macos|fake] [--fixture PATH]
  [--profile] [--naming] [--yes]` runs one tool in this process and prints `structuredContent` (or
  `{"error": {...}}`) as JSON on stdout. `--arg` values are parsed as JSON when they parse (`max_elements=5`,
  `modifiers=["cmd"]`), else kept as strings; `--arg` pairs override keys from `--json`. Exit codes per
  QUALITY.md A9 (an unknown tool or bad arguments: `2`).
