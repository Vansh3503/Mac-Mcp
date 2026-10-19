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
    ref: str                     # "e1", "e2", … unique within its snapshot, assigned in pre-order
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

@dataclass(slots=True)
class SnapshotDiff:
    before_id: str
    after_id: str
    app_changed: bool
    window_changed: bool
    focus_before: str | None     # element names, not refs (refs differ between snapshots)
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
Ref numbering is stable for the same tree (pre-order).

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
`def diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDiff` — match elements by
(identifier) → (locator path) → (ax_role, name, rounded frame) heuristics.

### `axpilot/perception/find.py`
`def find_elements(snap: Snapshot, *, text: str | None = None, role: str | None = None,
identifier: str | None = None, limit: int = 20) -> list[tuple[Element, float]]` — searches
`all_elements`; case-insensitive fuzzy match over name/value/identifier/description (exact > prefix >
substring > token overlap; `difflib` ratio as tiebreaker); `role` accepts short or AX roles. Sorted by score desc.

### `axpilot/perception/store.py`
```python
class SnapshotStore:
    def __init__(self, capacity: int = 32) -> None
    def new_id(self) -> str
    def put(self, snap: Snapshot) -> None
    def get(self, snapshot_id: str) -> Snapshot           # raises SNAPSHOT_NOT_FOUND
    def latest(self, pid: int | None = None) -> Snapshot | None
    def resolve(self, ref: str, snapshot_id: str | None = None) -> tuple[Snapshot, Element]
        # ref "e12" (or "s_ab12:e12"); snapshot_id None → latest snapshot; raises SNAPSHOT_NOT_FOUND / STALE_REF / INVALID_ARGUMENT
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
"menus": {"<bundle_id>": <MenuNode dict>}, "clipboard": "...", "permissions": {...}}`.
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
with a "Privacy & Security" window, two displays.

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
- observe: snapshot, screenshot, find, inspect, ocr, list_apps, list_windows, list_displays, wait_for, status
- input: click, type_text, press_keys, scroll, drag, move_mouse, set_value, perform_action, select_menu, fill_form
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
    def check_app(self, app: AppInfo | None, *, kind: Literal["observe","input","capture"],
                  window_title: str | None = None) -> None    # raises BLOCKED_BY_POLICY
    def check_secure_target(self, secure: bool) -> None        # typing into secure fields
    def requires_confirmation(self, tool: str, *, destructive: bool) -> bool
    def capture_exclusions(self) -> list[str]                  # bundle ids to hide from screenshots
    def is_terminal(self, app: AppInfo | None) -> bool
```
Rules: deny_apps (bundle id or case-insensitive name) block observe/input/capture; allow_apps (if set)
blocks everything else; deny_windows (`bundle::title-substring`) block when the window matches;
terminal apps are blocked for `input` unless `shell` is enabled; AXPilot never blocks *reading* its own
status. Messages must name the app and the rule.

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
`values`, `js` become `"<redacted N chars>"`; nested lists/dicts handled; everything else kept. Never
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
    def __init__(self, engine: Engine, *, yield_to_user: bool = True, idle_threshold_s: float = 1.0) -> None
    paused: bool; pause_reason: str | None
    def pause(self, reason: str = "kill switch") -> None
    def resume(self) -> None
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
  `kill_process(pid, *, force=False)` (refuses pid ≤ 1 and our own pid with `BLOCKED_BY_POLICY`).
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

# axpilot/server/app.py
def build_server(ctx: ServerContext, *, profile: str | None = None, naming: str | None = None,
                 client_name: str | None = None) -> MCPServer
```
Every tool is registered through `axpilot/server/registry.py` (`ToolSpec`: name, toolset, factory(ctx)->async fn,
title, description, read_only, destructive, idempotent, open_world, windows_mcp_name). A shared guard wraps
each tool: tool-enabled check → rate limit (input tools) → policy → confirmation (destructive) →
`control.input_turn()` (input tools) → engine call in a worker thread → audit. `AXPilotError` becomes
`CallToolResult(is_error=True, content=[TextContent(text=err.to_text())], meta={"axpilot/error": err.to_dict()})`.
Success returns `CallToolResult(content=[TextContent, (ImageContent)], structured_content=...)` with the
return annotation `Annotated[CallToolResult, <PydanticOutputModel>]` so `outputSchema` is advertised.
`tools/list` order = `TOOL_ORDER`. Tool descriptions are written for LLMs: one-line purpose, when to use,
key params, and what comes back.

---------------------------------------------------------------------------------------------------

## 7. Host, bridge, CLI

- `axpilot/bridge/protocol.py`: newline-delimited JSON. The bridge's first line is a hello:
  `{"axpilot_bridge": 1, "client": {"parent_app": str|None, "parent_bundle_id": str|None, "pid": int},
  "profile": str|None, "naming": str|None}`; the host replies `{"ok": true, "server_version": "..."}` or
  `{"ok": false, "error": {...}}`. After that both directions carry raw MCP JSON-RPC lines.
- `axpilot/bridge/stdio_pipe.py`: `async def run_bridge(socket_path, hello) -> int` pumps stdin→socket,
  socket→stdout until EOF. `axpilot/bridge/launch.py`: `ensure_host_running(timeout_s=10)` (on macOS:
  `open -g -a <launcher>`; elsewhere `HOST_UNAVAILABLE`).
- `axpilot/host/daemon.py`: `async def serve(ctx, socket_path)` — Unix socket server (0600, same-uid check via
  `SO_PEERCRED`/`LOCAL_PEERCRED`/`getpeereid`), hello/pairing, one MCP server session per connection over the
  socket streams. Runs on Linux with the fake engine (tests). `axpilot/host/pairing.py`: `PairingStore`.
- `axpilot/cli.py` (Typer): `stdio [--direct] [--engine auto|macos|fake] [--fixture PATH] [--profile]
  [--naming] [--yes]`, `http`, `host`, `setup`, `update`, `uninstall`, `doctor`, `connect <client>|--list
  [--dry-run] [--scope]`, `config get|set|edit|path|show`, `skill install`, `debug dump-tree`, `version`.
  `main()` is the console-script entry point.
- `axpilot/clients/`: one writer per client: `ClientTarget(name, display_name, config_path(scope), format=json|toml|yaml,
  servers_key)`, `connect(target, *, command, args, dry_run, scope) -> ConnectResult(path, changed, backup, snippet)`.
