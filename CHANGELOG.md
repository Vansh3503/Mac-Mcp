# Changelog

All notable changes to AXPilot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Tool names, parameters,
output schemas and error codes are public API: breaking changes to them are called out under
**Changed** or **Removed** with a migration note. The launcher (`AXPilot.app`) has its own version
line; a new launcher version requires a one-time permission re-grant, which is always flagged here.

## [Unreleased]

Pre-alpha. Nothing is published on PyPI yet.

### Added

- Project plan ([docs/PLAN.md](docs/PLAN.md)) and architecture decision records for language,
  architecture, minimum macOS, MCP surface, signing and distribution, privacy, and the name
  ([docs/adr/](docs/adr/README.md)).
- Package foundation: stable error codes with hints (`axpilot.errors`), platform-independent data
  types (`axpilot.models`), configuration with TOML file, `AXPILOT_<SECTION>_<KEY>` environment
  variables and CLI overrides (`axpilot.config`), and redirectable filesystem locations
  (`axpilot.paths`).
- The `Engine` interface, the in-memory `FakeEngine` with a demo desktop fixture for tests and
  `--engine fake`, and the PyObjC-based macOS engine.
- Perception: accessibility snapshots with refs, pruning, paging, diffs, fuzzy element search,
  the compact text format, and snapshot and capture stores.
- Stable element refs: a ref is unique per session, never reused for another element, and kept
  by its element across snapshots of a window, so refs held from earlier snapshots keep working
  after actions re-snapshot the UI. A ref whose element is gone, or a list row that scrolled
  away, gives `STALE_REF` instead of hitting another element. Diffs pair equal refs first.
- Navigation tools: `explore` (a map of an app: windows, menus with item counts, toolbar, tabs,
  sidebar, scroll areas, dialogs, focus and its compatibility profile), `find` (fuzzy search that
  can scroll a list or page until the element appears), `scroll` (by lines or pages, to the top or
  bottom, or an element into view; reports `moved` and `at_end`) and `read` (the whole content of a
  long list, table, text view or web page, joined page by page without repeats, with `complete`).
  Scrolling measures the app's line height, applies the input policy and lock, and every loop is
  bounded, returning partial results with a warning instead of failing.
- App and system tools: `app` (launch, activate, hide, unhide, quit, force-quit), `window` (focus,
  move, resize, minimize, restore, zoom, full screen, close), `open` (URLs, files and folders; only
  http(s), mailto, file and System Settings URLs unless scripting is enabled), `clipboard` (text,
  images, files), `notify`, `spotlight_search`, `system_info`, `system_control` (volume, mute,
  appearance) and `processes` (list; stopping one refuses system processes, AXPilot and protected
  apps before asking the user). A `manage` policy kind applies the deny lists to app and window
  management without the Terminal typing rule; protected apps are recognized by their bundle id,
  even renamed.
- Scripting tools, off by default: `shell` (zsh with the user's PATH), `applescript` (AppleScript
  and JXA), `shortcuts` (list and run) and `defaults` (read and write preferences). Every command,
  script, shortcut run and preference change is shown to the user for approval, after the call is
  validated; apps a script names and preference domains are checked against the deny lists.
- Browser tools, off by default: `browser_tabs` (list, switch, open, close in Safari and Chromium
  browsers), `browser_read` (the page's text through accessibility, in reading order, paged) and
  `browser_eval_js` (approved per script, showing the page and the code). URLs and scripts are
  embedded as escaped AppleScript strings; tabs matching `deny_windows` are hidden.
- The `computer` tool and profile: screenshot-pixel computer use (clicks, drags, typing, keys with
  xdotool-style names, scrolling, waiting, the cursor position) with the same policy hit-testing,
  rate limit, input lock and kill switch as the other input tools.
- The automation SDK: `from axpilot import Desktop` (blocking, thread-safe) or `AsyncDesktop`, with
  typed results and element handles (`mac.find("Share").click()`), on the simulated desktop
  (`Desktop.fake()`), in-process (`Desktop.direct()`) or through AXPilot.app
  (`Desktop.connect()`). Scripts use the same tools, policy, confirmations, audit log and error
  codes as agents.
- Examples: five automation scripts that run against the simulated desktop, and agent examples
  for eleven frameworks (OpenAI Agents SDK, LangGraph, CrewAI, Google ADK, Pydantic AI,
  LlamaIndex, smolagents, Microsoft Agent Framework, Claude Agent SDK, Vercel AI SDK, Mastra).
- `select_option` (pop-ups, combo boxes, radio and tab groups, segmented controls, list rows by
  title or index) and `handle_dialog` (read a sheet or dialog; accept, cancel or press a button;
  buttons that delete or discard ask first; in open and save panels, `path` goes to a folder or
  file with Go to the folder first).
- Menu bar extras: `select_menu` with `extras: true` lists and chooses status items (Wi‑Fi,
  Battery, an app's icon; Control Center by default); a one-title path clicks the item. Menu
  titles match across hyphen variants ("wi-fi" finds "Wi‑Fi").
- Date pickers: `set_value` takes ISO 8601 dates and times (`"2026-10-20"`, `"2026-10-20 14:30"`,
  `"14:30"` for a time field), sends them as dates and checks what the picker shows; snapshots
  show `AXDateTimeArea` pickers as `date` and time fields as `time`, in local time.
- Table column headers show how the table is sorted (`value` "ascending"/"descending"); color wells
  get a clear `ELEMENT_NOT_ACTIONABLE` that points to the Colors panel; the Agent Skill covers
  editing table cells and toolbar items hidden behind `»`.
- Portable input schemas that OpenAI, Gemini and Anthropic function calling all accept (no `null`
  unions or `$ref`s, `additionalProperties: false`, every parameter described), and an optional
  tool-name prefix (`[tools] prefix = "mac_"`).
- The tool reference ([docs/tools.md](docs/tools.md)) is generated from the tool registry
  (`scripts/gen_tool_docs.py`), and golden files pin `tools/list` for every profile and naming.
- The `windows-mcp` tool naming (`[tools] naming` or `--naming windows-mcp`): Windows-MCP's tool
  names and parameter shapes (`Click(loc=[x, y])`, `Type(text, label=12)`, `Shortcut`, `App`,
  `MultiSelect`, `Shell` for PowerShell, `Defaults` for Registry, …) on top of AXPilot's tools, so
  Windows-MCP prompts and agents carry over to macOS.
- The AXPilot host and the stdio bridge: MCP clients run `axpilot stdio`, which starts
  `AXPilot.app` if needed and connects to the host's private Unix socket (`0600` in a `0700`
  folder, same-user only). The host asks once per client app whether it may connect, remembers
  the answer, gives each client its own profile and naming, and limits concurrent clients.
  `axpilot stdio --direct` and `--engine fake` run the server in-process instead.
- Streamable HTTP (`axpilot http`): loopback only by default, a bearer token stored privately,
  `Host` and `Origin` checks and no CORS; other addresses need `--allow-remote` and TLS.
- The `axpilot` command: `stdio`, `http`, `host`, `setup`, `update`, `uninstall`, `doctor`,
  `connect`/`disconnect`, `config`, `skill install`, `tools`, `call`, `debug dump-tree` and
  `version`, with documented exit codes, `--json` output, one-line errors and tracebacks only with
  `--debug`. The Agent Skill ships inside the package.
- Input helpers: key-combo parsing (`cmd+shift+t`, `⌘⇧T`, `Command-Shift-T`) and screenshot-to-screen
  coordinate mapping.
- Policy layer: profiles and toolsets, app and window deny-lists, the Terminal rule, secure-field
  protection, confirmations, rate limiting, an audit log with argument redaction, and input
  serialization with yield-to-user and pause.
- Command-line integrations: a subprocess runner with timeouts, AppleScript/JXA, shell, Spotlight,
  Shortcuts, `defaults`, processes, system information and browser tab scripting.
- The frozen launcher source (`launcher/`), its build script and tests, and harnesses for
  spikes S1–S6.
- Documentation site skeleton, contributor guide, security policy, code of conduct, AGENTS.md,
  issue and pull request templates, CI workflow, Dependabot and pre-commit configuration, the
  Agent Skill (`skills/axpilot/SKILL.md`), MCP Registry metadata and the installer script
  (`packaging/`).

[Unreleased]: https://github.com/vansh3503/Mac-Mcp/commits/main
