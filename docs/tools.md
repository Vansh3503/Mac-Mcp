# Tools

> **Status:** this is the planned 1.0 tool surface ([PLAN §6](PLAN.md#6-tool-surface-v10)). No tool is
> released yet. The **Target** column says which pre-release is expected to include each tool:
> `v0.1` (Phase 1, direct mode), `v0.9` (Phase 4, full toolsets), or `1.0` (by 1.0, possibly earlier).
> Parameter lists show the key parameters only. Once the server ships, this page will be generated
> from the tool schemas, and each tool's own description (what `tools/list` returns) is authoritative.

## Concepts

**Snapshots and refs.** `snapshot` returns the interactive elements of a window as short refs
(`e1`, `e2`, …) inside a snapshot (`s_7f3a`). Input tools take a `ref`. A ref belongs to its snapshot:
pass a bare ref to use the latest snapshot, or qualify it (`s_7f3a:e12`). When the UI has changed,
AXPilot tries to re-find the element; if it can't, you get `STALE_REF` and should take a new snapshot.

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

Coordinates in snapshots are element centres in screen points. The same data is returned as
`structuredContent`, described by each tool's `outputSchema`.

**Coordinates.** The public API uses global screen **points** (origin at the top-left of the main
display; displays to the left or above have negative coordinates). Tools that take a point also
accept `coordinate_space: "screenshot"` with a `screenshot_id`, so a model can pass pixel
coordinates it read off a (possibly downscaled) screenshot and AXPilot maps them back.

**Act, then observe.** Input tools take `return: "none" | "diff" | "snapshot"` (default `diff`).
After acting, AXPilot waits for the UI to settle (about 150 ms of accessibility quiet, or a timeout)
and reports what changed: focus, added and removed elements, new windows or dialogs, changed values.

**Strategies.** `strategy: "auto"` tries the most robust approach first and records what it tried:

| Intent | Strategy ladder |
|---|---|
| Click an element | Accessibility action (`AXPress`, background-safe) → raise window and synthesize a click at the element centre → report failure |
| Type text | Set `AXValue` / insert at the selection → Unicode keystrokes (independent of keyboard layout) → clipboard paste for long text (clipboard restored) |
| Shortcut | Layout-aware key codes plus modifiers |
| Scroll | `AXScrollToVisible` for refs → scroll-wheel events |
| Menu command | Accessibility traversal of the menu bar with fuzzy path matching |
| Coordinate click | Allowed, but hit-tested first: the app under the point must pass the safety policy |

Input actions are never retried automatically.

**Annotations.** Every tool declares MCP annotations: `readOnlyHint` (all `observe` tools),
`destructiveHint`, `idempotentHint` and `openWorldHint`. Destructive actions require confirmation
by default ([Configuration](configuration.md#safety)).

## Profiles

| Profile | Tools | Use |
|---|---|---|
| `minimal` | `snapshot`, `screenshot`, `click`, `type_text`, `press_keys`, `scroll`, `app`, `wait_for`, `status` | Small or local models |
| `standard` (default) | `observe` (without `list_displays`) + `input` + `apps` + `clipboard` | Most users |
| `full` | `observe` + `input` + `apps` + `system` + `scripting` + `browser` (each still policy-gated) | Power users |
| `computer` | `computer`, `status` | Vision-first computer-use loops |

Add or remove tools or whole toolsets with `[tools] enable` / `disable` (for example
`enable = ["scripting"]`). `tools/list` always returns tools in the same order: toolset order, then
the order in the tables below.

## `observe` (read-only)

| Tool | Purpose | Key parameters | Target |
|---|---|---|---|
| `snapshot` | UI state of an app or window, with refs | `app`, `window_id`, `mode` (`interactive` \| `full` \| `text`), `max_elements` (150), `offset`, `include_screenshot`, `annotate` (set-of-mark), `include_menus` | v0.1 |
| `screenshot` | Capture a display, window or region; returns the image plus `screenshot_id`, scale and origin | `target`, `display_id`, `window_id`, `rect`, `max_edge` (1280), `format` | v0.1 |
| `find` | Fuzzy search for elements by text, role or identifier | `text`, `role`, `identifier`, `app`, `limit` | v0.1 |
| `inspect` | All attributes, actions and ancestry of one element | `ref` | v0.1 |
| `ocr` | Recognize text (with boxes) using Apple's Vision framework | `window_id`, `rect`, `languages` | v0.9 |
| `list_apps` | Running apps (optionally installed ones) | `include_installed` | v0.1 |
| `list_windows` | Windows: id, app, title, frame, state | `app` | v0.1 |
| `list_displays` | Displays: frames, scale, work areas | none | v0.1 |
| `wait_for` | Wait (≤ 60 s, with progress) for a condition | `condition` (`element` \| `element_gone` \| `window` \| `app_running` \| `text_visible` \| `idle` \| `duration`), `query`, `timeout_ms` | v0.1 |
| `status` | Version, permissions, launcher check, paused state, profile, clients | none | v0.1 |

## `input`

| Tool | Purpose | Key parameters | Target |
|---|---|---|---|
| `click` | Click an element or a point | `ref` or `x`/`y` (+ `coordinate_space`, `screenshot_id`), `button`, `count`, `modifiers` (e.g. `["cmd"]` to multi-select), `strategy`, `return` | v0.1 |
| `type_text` | Type into the focused element or a ref | `text`, `ref`, `clear`, `submit`, `strategy`, `return` | v0.1 |
| `press_keys` | Keyboard shortcuts and keys | `keys` (`"cmd+shift+t"`, `"⌘⇧T"`, or a sequence such as `["cmd+a", "delete"]`), `repeat` | v0.1 |
| `scroll` | Scroll an element or point, or scroll an element into view | `ref` or point, `direction`, `amount` (lines or pages), `scroll_into_view` | v0.1 |
| `drag` | Drag from a ref or point to a ref or point | `from`, `to`, `duration`, `modifiers` | 1.0 |
| `move_mouse` | Move the pointer (hover) | point or `ref` | 1.0 |
| `set_value` | Set a slider, checkbox, pop-up or text value through Accessibility | `ref`, `value` | 1.0 |
| `perform_action` | Perform a raw Accessibility action (`AXShowMenu`, `AXIncrement`, `AXConfirm`, `AXRaise`, …) | `ref`, `action` | 1.0 |
| `select_menu` | List or choose menu-bar items (and status-bar extras) by path; often the most reliable way to drive a Mac app | `app`, `path` (e.g. `["File", "Export…"]`), `list` | v0.1 |
| `fill_form` | Set many fields at once | `fields: [{ref, value}]` | v0.9 |

## `apps`

| Tool | Purpose | Key parameters | Target |
|---|---|---|---|
| `app` | Launch, activate, hide, quit or force-quit an app (`force_quit` is destructive) | `action` (`launch` \| `activate` \| `hide` \| `quit` \| `force_quit`), `app` (name or bundle id) | v0.1 |
| `window` | Focus, move, resize, minimize, restore, zoom, full-screen or close a window | `action`, `window_id` or `app`, `x`/`y`/`width`/`height` | 1.0 |
| `open` | Open a URL or file, optionally with a given app or revealed in Finder | `target`, `with_app`, `reveal` | 1.0 |

## `system`

| Tool | Purpose | Key parameters | Target |
|---|---|---|---|
| `clipboard` | Read or write text, images or files (reads are audited and can be disabled) | `action` (`get` \| `set`), `text`, … | 1.0 |
| `notify` | Show a macOS notification | `title`, `message`, `subtitle` | v0.9 |
| `spotlight_search` | Search files with Spotlight (`mdfind`) | `query`, `kind`, `scope`, `limit` | v0.9 |
| `system_info` | macOS version, hardware, battery, appearance, uptime | none | v0.9 |
| `system_control` | Volume and appearance (light or dark) | `volume`, `mute`, `dark` | v0.9 |
| `processes` | List processes; kill one (kill is destructive) | `action`, `name`, `pid`, `force` | v0.9 |

## `scripting` (off by default, destructive, confirmation by default)

| Tool | Purpose | Key parameters | Target |
|---|---|---|---|
| `shell` | Run a command with `zsh -lc` | `command`, `cwd`, `timeout_s` (≤ 120) | v0.9 |
| `applescript` | Run AppleScript or JavaScript for Automation | `source`, `language` | v0.9 |
| `shortcuts` | List (read-only) or run Apple Shortcuts | `action`, `name`, `input` | v0.9 |
| `defaults` | Read or write app preferences (writes are destructive) | `action`, `domain`, `key`, `value`, `type` | v0.9 |

Enabling `scripting` also lifts the Terminal rule (typing into terminal apps), because both amount to
shell access.

## `browser` (off by default)

| Tool | Purpose | Key parameters | Target |
|---|---|---|---|
| `browser_tabs` | List, activate, open or close tabs in Safari, Chrome, Arc, Edge, Brave and other Chromium browsers | `action`, `browser`, `url`, `tab` | v0.9 |
| `browser_read` | Read a page's text through its accessibility web area | `browser`, `tab` | v0.9 |
| `browser_eval_js` | Run JavaScript in a tab (needs the browser's *Allow JavaScript from Apple Events*; confirmation by default) | `js`, `browser`, `tab` | v0.9 |

## `computer` (the `computer` profile)

One `computer` tool with the common computer-use actions, in screenshot-pixel space, so existing
vision-first agent loops work without prompt changes: `screenshot`, `left_click`, `right_click`,
`double_click`, `mouse_move`, `left_click_drag`, `type`, `key`, `scroll`, `wait`,
`cursor_position`. Target: v0.9.

## Windows-MCP compatibility

AXPilot's tools map onto [Windows-MCP](https://github.com/CursorTouch/Windows-MCP)'s:

| Windows-MCP | AXPilot |
|---|---|
| Click / Type / Scroll / Move / Shortcut | `click` / `type_text` / `scroll` / `move_mouse` + `drag` / `press_keys` |
| Wait / WaitFor | `wait_for` |
| MultiSelect / MultiEdit | `click` with `modifiers: ["cmd"]` / `fill_form` |
| Screenshot / Snapshot / DisplayInventory | `screenshot` / `snapshot` / `list_displays` |
| Scrape | `browser_read` (plus your client's web fetch) |
| App | `app`, `window`, `open` |
| PowerShell | `shell`, `applescript` |
| FileSystem | none: use the official Filesystem MCP server, plus `spotlight_search` and `open` |
| Registry | `defaults` |
| Process / Clipboard / Notification / ControlStatus | `processes` / `clipboard` / `notify` / `status` |
| *(Mac only)* | `select_menu`, `set_value`, `perform_action`, `ocr`, `shortcuts`, `find`, `inspect` |

*Planned (v0.9):* `[tools] naming = "windows-mcp"` (or `--naming windows-mcp`) exposes the tools
under Windows-MCP's PascalCase names and parameter shapes (`Click`, `Type`, `Snapshot`, …), with
platform-specific tools renamed honestly (`PowerShell` → `Shell`, `Registry` → `Defaults`). It is a
profile, not duplicate tools: each server exposes one naming scheme.

## Error codes

Failed calls return `isError: true`, a text line `CODE: message` with a `Hint:`, and the same data
in `_meta["axpilot/error"]` (`code`, `message`, `hint`, `details`). Codes are stable public API.

| Code | Meaning | What the agent should do |
|---|---|---|
| `PERMISSION_ACCESSIBILITY` | Accessibility is not granted | Ask the user to grant it ([Permissions](permissions.md)) |
| `PERMISSION_SCREEN_RECORDING` | Screen Recording is not granted | Use snapshot-based tools, or ask the user |
| `PERMISSION_AUTOMATION` | macOS refused Apple Events to the target app | Ask the user to allow it under Automation |
| `LAUNCHER_CHANGED` | The launcher changed and needs a one-time re-grant | Ask the user to run `axpilot doctor` |
| `UNSUPPORTED_OS` | macOS older than 14, or not macOS | Stop |
| `ENGINE_UNAVAILABLE` | The macOS engine can't start here | Stop; check the installation |
| `HOST_UNAVAILABLE` | The bridge can't reach or start the host | Ask the user to run `axpilot doctor` |
| `APP_NOT_FOUND` / `WINDOW_NOT_FOUND` / `DISPLAY_NOT_FOUND` / `MENU_NOT_FOUND` | The target doesn't exist | Check names with `list_apps`, `list_windows`, `list_displays` or `select_menu` (list) |
| `SNAPSHOT_NOT_FOUND` | The snapshot or screenshot id expired | Take a new `snapshot` or `screenshot` |
| `STALE_REF` | The element changed or disappeared since the snapshot | Take a new `snapshot` and use the new refs |
| `ELEMENT_NOT_ACTIONABLE` | The element can't do that (disabled, unsupported action); `details.attempts` lists the strategies tried | Try another approach (menu, keyboard, `set_value`) |
| `BLOCKED_BY_POLICY` | AXPilot's safety policy refused | Don't work around it; tell the user |
| `TOOL_DISABLED` | The tool is not enabled in this profile | Use another tool, or tell the user |
| `USER_DENIED` | The user declined the confirmation | Respect it; don't retry |
| `USER_ACTIVE` | The user is using the mouse or keyboard | Wait a moment, then retry once or twice |
| `PAUSED_BY_USER` | The user hit the kill switch | Stop and ask the user before continuing |
| `RATE_LIMITED` | Too many input actions per minute | Slow down and verify results |
| `SECURE_INPUT_ACTIVE` | Secure keyboard entry blocks synthetic keystrokes | Ask the user to do this step |
| `NEEDS_USER` | A system dialog (e.g. a password prompt) needs the user | Ask the user to handle it |
| `CLIENT_NOT_PAIRED` | This client isn't approved to control the Mac | Ask the user to approve it |
| `INVALID_ARGUMENT` | Bad, missing or conflicting arguments | Fix the call |
| `TIMEOUT` | The operation exceeded its deadline | Check state with `snapshot`, then decide |
| `BUSY` | Another input action holds the "virtual hand" | Retry shortly |
| `NOT_SUPPORTED` | Not available on this macOS, app or version | Use another approach |
| `COMMAND_FAILED` | An external command exited with an error (stderr summarized in `details`) | Read the details; fix or report |
| `INTERNAL` | An unexpected error, with an incident id (`err_…`) | Report it with the incident id |
