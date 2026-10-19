# Mac-MCP — Project Plan

> An open-source MCP server that lets any AI agent see and operate a local Mac,
> the macOS counterpart of [Windows-MCP](https://github.com/CursorTouch/Windows-MCP).
> It installs in minutes, is safe by default, and works with any MCP client or agent framework.

Status: **Draft v1, for review** · Last updated: 2026-10-19 · License: Apache-2.0 (already in repo)

---

## 0. TL;DR

| Topic | Recommendation |
|---|---|
| **What** | A local MCP server that exposes macOS UI perception (accessibility tree, screenshots, OCR) and control (mouse, keyboard, menus, apps, windows, plus opt-in scripting) as MCP tools. |
| **Core language** | **Swift 6**, native frameworks (Accessibility/AX, CGEvent, ScreenCaptureKit, Vision, AppKit). See [ADR-1](#adr-1-language-swift-native-core). |
| **Process model** | A **signed, notarized menu-bar host app (`MacMCP.app`)** holds the macOS permissions and runs the engine. A tiny `mac-mcp` CLI bridges each MCP client to the app over a local socket. You grant permissions **once**, and every client (Claude Desktop, Claude Code, Cursor, VS Code, Python scripts, and so on) can use them. See [ADR-2](#adr-2-architecture-host-app--thin-cli-bridge). |
| **MCP** | Tools with annotations and `outputSchema`, Streamable HTTP and stdio transports (no deprecated SSE), and a **stateless design** with server-minted handles, matching the 2026-07-28 spec. |
| **Install** | `brew install --cask`, DMG, `npx`, `uvx`, a `.mcpb` one-click bundle for Claude Desktop, and an MCP Registry entry. A `mac-mcp connect <client>` command writes the client config for you. |
| **Frameworks** | Works with anything that speaks MCP: OpenAI Agents SDK, LangChain/LangGraph, CrewAI, Google ADK, Microsoft Agent Framework, Pydantic AI, LlamaIndex, smolagents, Claude Agent SDK, Vercel AI SDK, Mastra, and others. Ships tested examples, an Agent Skill, and an optional "computer-use" compatibility profile. |
| **Safety** | Off-by-default scripting, a native confirmation dialog, a kill-switch hotkey, yield-to-user, app deny-lists, secure-field protection, screenshot redaction, per-client pairing, an audit log, and **no telemetry by default**. |
| **Min OS** | macOS 14 Sonoma+ (universal binary: Apple silicon + Intel), tested on 14/15/26/27. |
| **Timeline** | About 12 weeks to 1.0 for one maintainer: a public preview around week 9, then 1.0 around week 12. See [§15](#15-roadmap--milestones). |
| **Cost** | Apple Developer Program at **$99/yr**, which signing and notarization require. Homebrew is removing casks that fail Gatekeeper checks (Sept 2026). |

---

## 1. Goals, non-goals, users

### Goals
1. **Any agent can operate any Mac app.** It should work with apps that have good accessibility support, and degrade gracefully (vision/OCR) for apps that don't.
2. **Two-minute setup on a fresh Mac.** One install command, one permission grant, and one click or command to connect a client.
3. **Framework-agnostic.** Standard MCP over stdio and Streamable HTTP. No custom SDK is needed, and there are copy-paste examples for the top frameworks.
4. **Safe by default.** It runs on people's personal machines, so the default posture is conservative and every risky capability is opt-in.
5. **Token-efficient and model-agnostic.** Compact text UI snapshots let text-only and local models work, and vision stays optional.
6. **Open-source quality.** Signed releases, tests, evals, docs, and a clear contribution path.

### Non-goals (v1)
- Being an agent itself. Mac-MCP provides tools; the agent loop lives in the client or framework.
- Remote fleet management or cloud-hosted Macs. The target is local use. Remote access is opt-in and advanced.
- Mac App Store distribution. Accessibility automation is incompatible with the App Sandbox.
- Bypassing macOS security: no TCC hacks, no automating system authentication dialogs, no private-API exploits.
- A general file-system server. The official Filesystem MCP server already covers that, so we interoperate with it.
- Games or other non-accessible canvases, beyond best-effort screenshot plus coordinates.

### Target users
- Developers using Claude Code, Cursor, Codex, Gemini CLI, and similar tools who want their agent to drive GUI apps (QA, demos, "do this in the app").
- Agent builders in Python and TypeScript frameworks who need a reliable "Mac hands and eyes" tool.
- Power users automating personal workflows with desktop assistants (Claude Desktop, LM Studio, local LLMs).
- QA teams running GUI tests driven by natural language.

---

## 2. Landscape research (what already exists)

| Project | Stack | Traction (approx.) | What we learn |
|---|---|---|---|
| **CursorTouch/Windows-MCP** | Python, UIAutomation | ~9.5k★ | The reference UX: about 20 tools (Click/Type/Scroll/Shortcut/Snapshot/Screenshot/App/PowerShell/FileSystem/…), `uvx` install, a Claude Desktop extension, config TOML, a desktop-control "gate" that yields to the user, and bearer/OAuth for HTTP. Telemetry is **on by default**. |
| **CursorTouch/MacOS-MCP** | Python + PyObjC | ~210★ | A Mac sibling of the above. Its known issues are almost all **TCC permission identity problems** (uv Python symlinks can't be granted, child-process permission failures), which suggests a Python interpreter is a weak permission holder. |
| **steipete/Peekaboo** | Swift (+ Node launcher) | ~5.3k★ | A CLI, MCP server, and menu-bar app. It uses opaque element IDs from a `see` snapshot, background input, and handles permissions through a signed app. macOS 15+. It is very broad and includes its own agent. |
| **mediar-ai/mcp-server-macos-use** | Swift | ~360★ | An "act-then-traverse" pattern: each action returns the updated accessibility tree, optionally as a **diff**. Build-from-source only. |
| **trycua/cua — cua-driver** | Rust + app bundle | (large monorepo) | Explicitly solves TCC with a **standalone `CuaDriver.app` daemon** that holds grants, with the CLI proxying to it. It offers background input without stealing focus, plus a "direct mode" that inherits the host's grants. |
| tmc/axmcp, macbot-mcp, others | Go / misc | small | They confirm the AX-first approach and coordinate-free clicking. |

**Platform context**
- **MCP spec 2026-07-28** is now final. It is stateless (no `initialize`, no sessions), adds `server/discover`, and says to use **explicit server-minted handles in tool arguments** for cross-call state. It also asks servers to return tools in deterministic order, deprecates the HTTP+SSE transport and the Logging, Sampling and Roots features, and moves Tasks to an extension.
- **Official SDKs**: the Python SDK v2.x (stable since 2026-07-28) supports the new spec plus older revisions. The official Swift SDK (0.12.x) covers **2025-11-25**, which clients still negotiate.
- **Apple**: WWDC26 expanded **App Intents** (Siri, Spotlight semantic index, View Annotations). Third-party reports describe an `mcpbridge` for Xcode 27, but I found **no Apple source saying macOS exposes a system-wide MCP host**. GUI automation still needs Accessibility.
- **macOS 27** shipped on 2026-09-14 and **runs on Apple silicon only**. macOS 26 is the last Intel release.
- **Homebrew** now audits casks for code signing and notarization. Casks that fail are being removed (deadline reported as Sept 2026).

### Why build this (and how we differentiate)
The space is real but crowded, and Peekaboo in particular is strong. Building our own is worth it only if we commit to these points. Otherwise, contributing to an existing project would be cheaper.

1. **A Windows-MCP-style experience on Mac.** A familiar tool vocabulary, with a mapping table ([§6.7](#67-windows-mcp-parity-map)), so cross-platform agents and prompts port easily.
2. **Spec-first MCP.** Clean tool schemas, annotations, structured output, deterministic order, stateless handles, and no deprecated transports.
3. **Safety and privacy as the headline feature.** No telemetry, client pairing, kill switch, confirmations, redaction, and audit. Nobody else makes this the default.
4. **Zero-friction install for everyone.** Brew, DMG, npx, uvx, mcpb, and the registry all lead to the same signed app, and `mac-mcp connect` configures 10+ clients.
5. **A framework integration kit.** Tested examples in CI, an Agent Skill, and a computer-use compatibility profile.
6. **Published evals.** A reproducible macOS task suite whose success rates are published each release.
7. **Focused scope.** We provide OS hands and eyes and leave the agent to the client.

---

## 3. macOS technical realities (constraints that shape the design)

| Reality | Consequence |
|---|---|
| **TCC "responsible process".** Accessibility, Screen Recording, and Automation grants are checked against the *responsible app*. For an MCP server spawned by Claude Desktop that is Claude.app (through its helper); from a terminal it is Terminal or iTerm. Grants given to `python`/`node` often don't apply, and apps missing usage strings get **silent denials**. | The server must have **its own stable, signed app identity**, launched by LaunchServices so that it is its own responsible process. → Host-app architecture. |
| **The code-signing identity anchors TCC grants.** Ad-hoc signatures change every build, so grants reset. | Release with **Developer ID** signing and notarization, and develop with a stable local signing identity (documented in CONTRIBUTING). |
| **Accessibility (AX) API**: `AXUIElement` cross-process calls. They are slow on large trees, and an unresponsive app can block each call for seconds. | Use batched attribute reads (`AXUIElementCopyMultipleAttributeValues`), `AXUIElementSetMessagingTimeout`, depth/count budgets, background queues, and caching invalidated by `AXObserver`. |
| **Chrome and Electron** expose a thin tree unless asked. | Set `AXManualAccessibility` / `AXEnhancedUserInterface` on the app element (with an opt-out) to enable full web content trees. |
| **Screen capture**: `CGWindowListCreateImage` is deprecated and ScreenCaptureKit is the path forward (`SCScreenshotManager` needs macOS 14+). Sequoia and later show a **recurring "Allow for one month" prompt** to apps that capture outside the system picker. | Make **Screen Recording optional**: AX-only mode works without it, and we explain the monthly prompt in the docs and UI. Use `SCContentFilter` to **exclude our own overlay and deny-listed apps** from captures. |
| **Window titles from CGWindowList** need Screen Recording. | Prefer AX for window lists, which works with Accessibility alone. |
| **Synthetic input** (CGEvent) needs Accessibility. It requires focus and moves the real cursor. | Use an "AX-first" ladder: `AXPress` and `AXSetValue` work in the background without stealing focus, and synthesized events are the fallback (see [§5.5](#55-action-strategies)). |
| **Secure Event Input** (password fields, some terminals) blocks synthetic keystrokes. | Detect it with `IsSecureEventInputEnabled()`, return a clear `SECURE_INPUT_ACTIVE` error, and never type into `AXSecureTextField` unless explicitly allowed. |
| **Keyboard layouts**: key codes differ across layouts (AZERTY, Dvorak, JIS…). | Type text with `CGEventKeyboardSetUnicodeString` (layout-independent). Map shortcut characters to key codes through `TISCopyCurrentKeyboardLayoutInputSource` + `UCKeyTranslate`. |
| **Coordinates**: global point space, Retina scaling, multiple displays with negative origins, and downscaled screenshots. | All public coordinates are in **screen points**. Screenshots return a `screenshot_id` with scale and origin so the server can map model pixel coordinates back. |
| **Spaces / Mission Control / Stage Manager** have no public API to enumerate other Spaces' windows. | Document the limits. Operate on the current Space, with best-effort switching through shortcuts. |
| **System auth dialogs** (SecurityAgent) can't be automated, by design. | Detect them and return `NEEDS_USER` so the agent asks the human. |
| **App Sandbox** is incompatible with controlling other apps. | Distribute outside the Mac App Store, with Hardened Runtime and the `com.apple.security.automation.apple-events` entitlement. |
| **Automation (Apple Events)** permission is per target app and prompts on first use. | The host app declares `NSAppleEventsUsageDescription`, and the docs list which tools trigger which prompts. |
| **Notifications, EventKit, and Contacts** need a real app bundle with usage strings. These fail for most MCP servers spawned by other apps. | Our host app can offer **reliable PIM toolsets** (Calendar, Reminders, Contacts) later. This is a differentiator. |

---

## 4. Key decisions (ADR summaries)

Each becomes a file in `docs/adr/` during Phase 0. The spikes may overturn any of them.

### ADR-1: Language: Swift native core
**Decision:** Swift 6 (strict concurrency), Swift Package Manager for the libraries and CLI, and an Xcode project (generated with XcodeGen) for the app bundle.

| | Swift-native (recommended) | Python + PyObjC (like Windows-MCP / MacOS-MCP) | Hybrid: Python MCP layer + Swift helper app |
|---|---|---|---|
| TCC permission reliability | ✅ signed app identity | ❌ interpreter path/identity problems (seen in MacOS-MCP issues) | ✅ helper holds the permissions |
| Performance (AX, capture, OCR) | ✅ native | ⚠️ bridge overhead, GIL | ✅ native in the helper |
| End-user install | ✅ one signed app, no runtime | ⚠️ needs Python/uv | ⚠️ needs Python and the app; version skew |
| MCP spec currency | ⚠️ Swift SDK lags (2025-11-25) | ✅ Python SDK v2 = 2026-07-28 | ✅ |
| Contributor pool | ⚠️ smaller | ✅ large | ⚠️ two languages |
| Maintenance | ✅ one language, one build | ✅ | ❌ two pipelines plus an IPC contract |

**Why:** the hard, differentiating part is native macOS work: permissions, AX, capture, input. The MCP layer is thin, and we isolate it behind an adapter (`MCPAdapter`) so we can upgrade or swap the SDK. To close the spec gap we either contribute 2026-07-28 support upstream or implement `server/discover` ourselves. Our tool design is already stateless.

**If you'd rather stay in Python:** choose the *hybrid*. Keep the MCP layer, config, and CLI in Python (official SDK v2, `uvx mac-mcp`) and put all OS calls in a small signed Swift helper app reached over a socket. A pure PyObjC server will hit the same permission problems MacOS-MCP has.

### ADR-2: Architecture: host app + thin CLI bridge
- `MacMCP.app` (a menu-bar app, LSUIElement) runs the engine **and** the MCP server. It listens on a per-user Unix domain socket and optionally on localhost Streamable HTTP.
- The MCP client spawns `mac-mcp stdio`, a byte pipe between stdio and the socket. It launches the app with `open -g -b <bundle-id>` if the app isn't running.
- **Direct mode** (`mac-mcp stdio --direct`) embeds the engine in the CLI process, so permissions attach to the launching app. This is for development, CI, or users who don't want the app.
- Precedent: cua-driver's `CuaDriver.app` daemon, and Peekaboo's app plus daemon.

### ADR-3: Minimum macOS 14 Sonoma, universal binary
- macOS 14 is required for `SCScreenshotManager` and gives a modern AX baseline. We build a universal binary (arm64 + x86_64) because macOS 14–26 still run on Intel Macs.
- CI test matrix: GitHub `macos-14`, `macos-15`, `macos-26` images, plus manual or self-hosted testing on macOS 27.
- Review in 2027: consider macOS 15+ and dropping Intel once macOS 26 ages out.

### ADR-4: MCP protocol surface
- **Tools first.** Every tool has `inputSchema`, `outputSchema` + `structuredContent`, a compact text `content`, and **annotations** (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). `tools/list` order is deterministic.
- **Stateless handles.** `snapshot_id`, `screenshot_id`, and element `ref`s are server-minted and passed as arguments. There is no per-session state, matching 2026-07-28 and safe for multiple clients.
- **Transports**: stdio (default) and Streamable HTTP. **No SSE**, which is deprecated.
- `instructions` field: a short operating guide for any client (observe → act → verify; prefer refs; use menus).
- Optional extras: resources (`macmcp://snapshot/{id}`, `macmcp://screenshot/{id}`), progress notifications for `wait_for`, and later the Tasks extension.
- Confirmations: a native dialog in the host app works with **every** client. Elicitation through MRTR is used where the client supports it, mainly in direct mode.
- Logging goes to stderr/OSLog and optionally OpenTelemetry, because MCP logging is deprecated.

### ADR-5: Distribution
The Developer ID-signed and notarized app is the single source of truth. Every channel (brew, DMG, npx, uvx, mcpb) installs or launches that same app. Updates use Sparkle 2 (EdDSA-signed) for DMG installs and `brew upgrade` for cask installs.

### ADR-6: Privacy
**No telemetry.** We may later add opt-in crash reports with explicit consent. This is a deliberate contrast with existing servers.

### ADR-7: Naming
- Working name **Mac-MCP**; binary `mac-mcp`; app `MacMCP.app`; bundle ID e.g. `io.github.vansh3503.MacMCP` (switch to an org domain if one is created).
- Package names: **npm `mac-mcp` is already taken** (a 0.0.0 placeholder), so use a scope such as `@vansh3503/mac-mcp` or an org scope. **PyPI `mac-mcp` is available** as of this check, so reserve it early. `macos-mcp` is taken on both registries by other projects.
- Consider a distinctive brand name to avoid confusion with CursorTouch's "MacOS-MCP" (open question).

---

## 5. Architecture

### 5.1 Component diagram

```
 MCP clients                        Agent frameworks
 (Claude Desktop/Code, Cursor,      (OpenAI Agents SDK, LangGraph, CrewAI,
  VS Code, Codex, Gemini CLI,        ADK, Pydantic AI, LlamaIndex, smolagents,
  LM Studio, Zed, Windsurf, …)       MS Agent Framework, Vercel AI SDK, Mastra…)
        │ spawn `mac-mcp stdio`                 │ Streamable HTTP
        ▼                                       │ 127.0.0.1:<port> + bearer token
 ┌────────────────────┐  Unix socket (0600,     │
 │ mac-mcp  (CLI)     │  peer-checked)          │
 │ stdio ⇄ socket pipe├────────────┐           │
 │ doctor / connect / │            ▼           ▼
 │ config / skill     │   ┌─────────────────────────────────────────────┐
 └────────────────────┘   │  MacMCP.app  (menu-bar host; Developer ID)   │
                          │                                             │
                          │  MCP Server layer   (MCPAdapter → swift-sdk) │
                          │   tools · schemas · handles · transports     │
                          │  ───────────────────────────────────────────│
                          │  Policy & Safety                             │
                          │   profiles/toolsets · app allow/deny ·       │
                          │   client pairing · confirmations · rate      │
                          │   limit · audit log · kill switch · yield    │
                          │  ───────────────────────────────────────────│
                          │  Engine (MacMCPEngine, no MCP dependency)    │
                          │   Perception: AX snapshot · refs · diff ·    │
                          │     ScreenCaptureKit · Vision OCR            │
                          │   Actuation: AX actions · CGEvent mouse/kbd ·│
                          │     menus · layout-aware keys                │
                          │   Apps/Windows/Displays (NSWorkspace, AX)    │
                          │   System: clipboard · Shortcuts · AppleScript│
                          │     /JXA · shell · Spotlight · notifications │
                          │  ───────────────────────────────────────────│
                          │  UI: onboarding · permission status ·        │
                          │   overlay/indicator · activity log ·         │
                          │   "Connect a client" · settings              │
                          └─────────────────────────────────────────────┘
          TCC grants (Accessibility, Screen Recording, Automation) → MacMCP.app
```

### 5.2 Swift modules

| Module | Responsibility | Depends on |
|---|---|---|
| `MacMCPEngine` | All macOS perception and actuation. A pure library with no MCP types, usable by other Swift apps. | AppKit, ApplicationServices, ScreenCaptureKit, Vision, Carbon (TIS) |
| `MacMCPPolicy` | Profiles, allow/deny lists, confirmation rules, rate limits, audit log | Engine (for hit-testing app under point) |
| `MacMCPServer` | Tool definitions, schemas, handle store, result formatting, transports | Engine, Policy, `MCPAdapter` |
| `MCPAdapter` | Thin wrapper over the official `modelcontextprotocol/swift-sdk`, so the SDK can be swapped or upgraded | swift-sdk |
| `MacMCPBridge` | Socket framing, peer verification, app discovery and launch | Foundation, Security |
| `mac-mcp` (executable) | `stdio`, `http`, `doctor`, `connect`, `config`, `skill`, `debug` subcommands | swift-argument-parser, Bridge, (Server for `--direct`) |
| `MacMCPApp` (Xcode target) | Menu-bar UI, onboarding, overlay, hotkey, settings, Sparkle | SwiftUI/AppKit, Server |
| `TestBench` (Xcode target) | A fixture app with every control type, for end-to-end tests | SwiftUI/AppKit |

### 5.3 Concurrency and multi-client
- The engine is an `actor`. AX reads run on a bounded background pool with per-call messaging timeouts (default 1 s) so one hung app can't stall the server.
- **Single "virtual hand":** all input actions are serialized through one queue, while observations run concurrently.
- Multiple clients can connect at once. A per-client *control lease* is post-1.0. Until then, conflicting input simply queues and a short timeout returns `BUSY`.
- **Yield-to-user:** before each input action, the server checks recent hardware HID activity (`CGEventSourceSecondsSinceLastEventType(.hidSystemState, …)`; spike S3 validates that it excludes our synthetic events). If the user is active, the action pauses or returns `USER_ACTIVE`.

### 5.4 Snapshot, refs, and diffs (the perception model)
- A `snapshot` walks the target (by default the frontmost app's focused window, plus menu bar, dialogs, and sheets) and produces:
  - **Interactive elements** with short refs (`e1…eN`): role, label/title, value (secure values never returned), enabled/selected/focused state, frame (points), and supported AX actions.
  - Context: app, bundle ID, pid, window title and frame, focused element, scroll containers, and a truncation notice.
- **Pruning rules:** drop invisible, off-screen, zero-size, and purely decorative nodes; collapse single-child groups; cap depth and count (default ~150 elements, with paging via `offset`); summarize long lists ("list 'Messages' (482 rows; showing 1–25)").
- **Ref resolution:** the server keeps an LRU of `snapshot_id → {ref → AXUIElement + locator path}`. When an `AXUIElement` has gone stale, it re-resolves through the locator (role/identifier/title/index path). If that fails, it returns `STALE_REF` with a hint to re-snapshot.
- **Act → observe:** every action supports `return: none | diff | snapshot` (default `diff`). After acting, the server waits for the UI to settle (AX notifications quiet for ~150 ms, or a timeout) and returns what changed: focus, added/removed elements, new windows or dialogs, and value changes. That saves a separate round-trip and tokens.

Example text output (the `content` the model reads; the same data is also in `structuredContent`):
```
app Notes (com.apple.Notes, pid 812) · window "Notes" [0,25 1440×875] · snapshot s_7f3a
focused: e7 textarea "Note body"
e1  button   "New Note"                 (1180,52)
e2  field    "Search"   value=""        (1300,52)
e3  list     "Notes"    12 rows, scrollable
e4    row    "Shopping list — 10:42"  selected
e5    row    "Trip ideas — Yesterday"
e6  button   "Share"                    (1395,52)
e7  textarea "Note body" value="Milk, eggs…" (720,400)
menu: Notes · File · Edit · Format · View · Window · Help   (use select_menu)
42 of 318 elements shown (interactive only). Use find / offset for more.
```

### 5.5 Action strategies
| Intent | Strategy ladder (`strategy: auto`) |
|---|---|
| Click an element | 1) `AXPress` / appropriate AX action (background-safe, no cursor move) → 2) raise window + activate app → synthesized click at element center → 3) report failure, with a diff showing nothing changed |
| Type text | 1) `AXSetValue` / `AXSelectedText` insert on the target field (fast; some apps ignore it) → 2) Unicode keystrokes via CGEvent → 3) paste via clipboard (save/restore clipboard) for long text |
| Keyboard shortcut | Layout-aware key codes plus modifiers via CGEvent to the frontmost app. Optionally `CGEventPostToPid` for background delivery where apps honor it (spike S3) |
| Scroll | `AXScrollToVisible` for a ref when available → scroll-wheel CGEvents at the element or point |
| Menu command | AX menu-bar traversal (`select_menu ["File","Export As…"]`), fuzzy-matched. This is often the most reliable way to drive Mac apps. |
| Coordinate click | Allowed (vision agents need it), but **hit-tested first**: we resolve the app/window under the point and apply policy. A click on a deny-listed app is blocked. |

### 5.6 Error model
Tool errors return `isError: true` with a stable `code` and an actionable `hint`, for example:
`PERMISSION_ACCESSIBILITY`, `PERMISSION_SCREEN_RECORDING`, `PERMISSION_AUTOMATION(app)`, `APP_NOT_FOUND`, `WINDOW_NOT_FOUND`, `STALE_REF`, `ELEMENT_NOT_ACTIONABLE`, `BLOCKED_BY_POLICY`, `USER_DENIED`, `USER_ACTIVE`, `PAUSED_BY_USER`, `SECURE_INPUT_ACTIVE`, `NEEDS_USER` (system auth dialog), `TIMEOUT`, `BUSY`, `UNSUPPORTED_OS`.

---

## 6. Tool surface (v1)

Design principles: there are **few, orthogonal tools** (too many tools hurts model accuracy). `action` enums replace tool sprawl where natural, and **profiles** let small or local models load a minimal set.

### 6.1 Profiles and toolsets
| Profile | Tools | Use |
|---|---|---|
| `minimal` (~8) | snapshot, screenshot, click, type_text, press_keys, scroll, app, wait_for | Small or local models, tight context |
| `standard` (**default**, ~20) | minimal + find, inspect, select_menu, set_value, fill_form, drag, window, open, list_windows, clipboard, status, … | Most users |
| `full` | standard + `system` extras, plus `scripting` and `browser` (each still gated by policy) | Power users |

Toolsets can be toggled individually (`[tools] enable = ["scripting"]`). `scripting`, `browser`, and `computer` are **off by default**.

### 6.2 Toolset `observe` (all `readOnlyHint: true`)
| Tool | Purpose | Key params |
|---|---|---|
| `snapshot` | UI state with element refs (see §5.4) | `app?`, `window_id?`, `mode: interactive\|full\|text`, `max_elements`, `offset`, `include_screenshot`, `annotate` (set-of-mark boxes on screenshot), `include_menus` |
| `screenshot` | Capture display, window, or region; scaled; returns image + `screenshot_id` + scale/origin | `target: screen\|window\|region`, `display_id?`, `window_id?`, `rect?`, `max_edge` (default 1280), `format` |
| `find` | Search elements across an app/window | `text` (fuzzy over title/label/value/description), `role?`, `identifier?`, `app?`, `limit` |
| `inspect` | Full attributes, actions, and ancestry of a ref | `ref` |
| `ocr` | Vision text recognition of a window or region, with boxes | `window_id?`, `rect?`, `languages?` |
| `list_apps` | Running apps (and optionally installed apps) | `include_installed` |
| `list_windows` | Windows: id, app, title, frame, display, minimized/focused | `app?` |
| `list_displays` | Displays, frames, scale, work areas | — |
| `wait_for` | Poll until a condition holds or times out (≤ 60 s, with progress) | `condition: element\|element_gone\|window\|app_running\|text_visible\|idle\|duration`, `query`, `timeout_ms` |
| `status` | Server version, permissions, paused/active, profile, connected clients | — |

### 6.3 Toolset `input`
| Tool | Purpose | Annotations |
|---|---|---|
| `click` | Click a `ref` or a point; `button`, `count`, `modifiers`, `strategy`, `return` | not read-only |
| `type_text` | Type into the focused element or a `ref`; `clear`, `submit`, `strategy` | not read-only |
| `press_keys` | Shortcut(s), e.g. `"cmd+shift+t"` or a sequence `["cmd+a","delete"]`; `repeat` | not read-only |
| `scroll` | Scroll a ref or point by lines/pages, or `scroll_into_view: ref` | idempotent-ish |
| `drag` | Drag from ref/point to ref/point; `duration`, `modifiers` | not read-only |
| `move_mouse` | Hover (tooltips, hover menus) | |
| `set_value` | Set slider/checkbox/popup/text value through AX | |
| `perform_action` | Raw AX action on a ref (`AXShowMenu`, `AXIncrement`, `AXConfirm`, `AXRaise`, …) | |
| `select_menu` | List or select menu-bar items by path for an app; also status-bar extras | |
| `fill_form` | Set many fields in one call (`[{ref, value}]`), which replaces Windows-MCP's MultiEdit | |

Coordinate inputs accept `coordinate_space: points | screenshot` (+ `screenshot_id`), so vision models can click on what they saw.

### 6.4 Toolset `apps`
| Tool | Purpose |
|---|---|
| `app` | `action: launch\|activate\|hide\|quit\|force_quit` by name or bundle ID (`force_quit` is destructive and needs confirmation) |
| `window` | `action: focus\|move\|resize\|minimize\|restore\|zoom\|fullscreen\|close` by `window_id` or `ref` |
| `open` | Open a URL or file, optionally `with_app`; `reveal` in Finder |

### 6.5 Toolset `system` (on by default except where noted)
| Tool | Purpose | Notes |
|---|---|---|
| `clipboard` | Get/set text, image, or file URLs | Reads are audited; can be disabled |
| `notify` | Show a macOS notification through the host app | Needs a real bundle, so it is reliable here |
| `spotlight_search` | `mdfind` queries, by kind, scope, and limit | read-only |
| `system_info` | OS, hardware, battery, appearance, volume, displays, uptime | read-only. No Wi-Fi SSID, which needs Location. |
| `system_control` | Volume/mute, light/dark appearance | Uses System Events (Automation prompt) |
| `processes` | List processes, or kill one (kill is destructive and needs confirmation) | |

### 6.6 Opt-in toolsets
- **`scripting`** (off by default; `destructiveHint: true`; confirmation by default)
  - `shell`: `zsh -lc`, with `cwd`, `timeout_s` (max 120), `env`; returns stdout, stderr, and exit code (truncated)
  - `applescript`: AppleScript or JXA (`language`), with a timeout
  - `shortcuts`: `list` (read-only) / `run` Apple Shortcuts with input. This reaches thousands of App Intents actions.
  - `defaults`: read or write app preferences (writes are destructive), the Mac equivalent of Windows' Registry tool
- **`browser`** (off by default): `browser_tabs` (list/activate/open/close tabs in Safari, Chrome, Arc, Edge, Brave via AppleScript), `browser_read` (page text through the AX web area), and `browser_eval_js` (needs the browser's "Allow JavaScript from Apple Events"; scripting-level risk).
- **`computer`** (compatibility profile, off by default): a single `computer` tool using the common computer-use action vocabulary (`screenshot`, `left_click`, `right_click`, `double_click`, `mouse_move`, `left_click_drag`, `type`, `key`, `scroll`, `wait`, `cursor_position`) in screenshot-pixel space. This lets existing vision-first computer-use loops plug in with no prompt changes.

### 6.7 Windows-MCP parity map
| Windows-MCP | Mac-MCP | Notes |
|---|---|---|
| Click | `click` | + ref-based AX press |
| Type | `type_text` | + AX value / paste strategies |
| Scroll | `scroll` | + scroll_into_view |
| Move | `move_mouse`, `drag` | |
| Shortcut | `press_keys` | layout-aware |
| Wait / WaitFor | `wait_for` (`duration` / conditions) | |
| MultiSelect | `click` with `modifiers:["cmd"]` (or `refs[]`) | |
| MultiEdit | `fill_form` | |
| Screenshot | `screenshot` | ScreenCaptureKit |
| Snapshot | `snapshot` | refs + diffs |
| DisplayInventory | `list_displays` | |
| Scrape | `browser_read` (+ use client's web fetch) | |
| App | `app`, `window`, `open` | |
| PowerShell | `shell`, `applescript` | opt-in |
| FileSystem | — (use the official Filesystem MCP) + `spotlight_search`, `open` | |
| Registry | `defaults` | opt-in |
| Process | `processes` | |
| Clipboard | `clipboard` | |
| Notification | `notify` | |
| ControlStatus | `status` | |
| *(Mac-only)* | `select_menu`, `set_value`, `perform_action`, `ocr`, `shortcuts`, `find`, `inspect` | |

---

## 7. Security and safety model

### 7.1 Threat model (what we defend against)
1. **Prompt injection from screen content.** A web page or email tells the agent to do harmful things. We can't fully solve this, so we limit the blast radius (gating, confirmations, deny-lists) and warn in the `instructions` and docs that on-screen text is untrusted.
2. **Confused deputy / local TCC bypass.** Any local same-user process could use our Accessibility grant through the socket or HTTP port. **Mitigation: client pairing.** The first time a new client connects, the host app identifies it by the code signature or bundle of the CLI's parent process (for example "Claude", "Cursor", or "python3 launched by Terminal") and shows *"Allow «Claude» to control this Mac?"* (Allow once / Always / Deny). Decisions are remembered per signing identity. HTTP tokens live in the Keychain and are revealed only through the app UI.
3. **Runaway agent.** Loops or mis-clicks cause damage. Mitigations: rate limits, kill switch, yield-to-user, confirmations for destructive tools, and Trash rather than delete where we touch files.
4. **Data exfiltration.** Screenshots, clipboard, and secure fields could leak. Secure-field values are never read, deny-listed apps are excluded from captures, and clipboard reads are audited and can be disabled.
5. **Network exposure.** The HTTP port could be reachable by others. It binds to `127.0.0.1` only, requires a bearer token, validates Origin and Host headers (anti-DNS-rebinding), and has no CORS. Remote binding needs `--allow-remote` plus TLS, and we recommend SSH or Tailscale instead.
6. **Supply chain.** Signed and notarized builds, SHA-256 checksums, GitHub build-provenance attestations, pinned dependencies, Dependabot, CodeQL (Swift), and trusted publishing (OIDC) to npm and PyPI.

### 7.2 Controls and defaults
| Control | Default |
|---|---|
| Profile | `standard`. `scripting`, `browser`, and `computer` are off. |
| Confirmation policy | `destructive` (native dialog for shell, AppleScript, `force_quit`, `defaults write`, process kill). Options: `never \| destructive \| always`. |
| Kill switch | Global hotkey **⌃⌥⌘.** plus a menu-bar "Stop". Cancels in-flight actions and pauses until resumed. |
| Yield to user | On. Physical mouse or keyboard activity pauses agent input. |
| Visual indicator | On. A screen-edge glow, click ripples, and an "Agent is controlling — ⌃⌥⌘. to stop" pill, excluded from our own screenshots. |
| App deny-list | Keychain Access, common password managers (1Password, Bitwarden, …), and the Privacy & Security pane in System Settings |
| Terminal rule | Typing into Terminal, iTerm2, Ghostty, Warp, and similar apps is effectively shell access, so it is **blocked unless the `scripting` toolset is enabled** |
| Secure fields | Values never returned. Typing into them is refused unless `allow_secure_fields = true`. |
| Screenshot redaction | Deny-listed apps are excluded through `SCContentFilter` |
| Rate limit | 120 input actions/min (configurable) |
| Audit log | On. `~/Library/Logs/MacMCP/audit.jsonl`: timestamp, client, tool, redacted args, outcome. Viewable in the app. |
| Telemetry | **None** |

`SECURITY.md` covers private vulnerability reporting (GitHub Security Advisories), supported versions, and the threat model above.

---

## 8. Installation and client integration

### 8.1 Install channels (all end at the same signed app)
| Channel | Command | Notes |
|---|---|---|
| Homebrew (own tap → later homebrew/cask) | `brew install --cask vansh3503/tap/mac-mcp` | Installs `MacMCP.app` and symlinks `mac-mcp` into the brew bin |
| DMG / ZIP | GitHub Releases | Drag to Applications; Sparkle auto-update |
| npm launcher | `npx -y @vansh3503/mac-mcp stdio` | A tiny JS shim: finds the app, or downloads it, verifies SHA-256 + `codesign --verify --strict` + Team ID + `spctl --assess`, installs it to `~/Applications`, then `exec`s `mac-mcp stdio` |
| PyPI launcher | `uvx mac-mcp stdio` | Same as above in Python, for Python-first users and frameworks |
| MCPB bundle | Download `mac-mcp.mcpb` → double-click | One-click install in Claude Desktop (bundles the launcher and manifest) |
| MCP Registry | `io.github.vansh3503/mac-mcp` (`server.json`) | Lists the npm, PyPI, and mcpb packages for discovery by registry-aware clients |
| Source | `git clone` → `make dev` | Developers; direct mode |

### 8.2 First-run experience
1. Open MacMCP.app. The onboarding wizard opens.
2. **Accessibility** (required): a button opens the exact System Settings pane (`x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`). Status turns green live, by polling `AXIsProcessTrusted`.
3. **Screen Recording** (optional, for screenshots and OCR): same flow, with an explanation of the monthly re-confirmation on macOS 15+.
4. **Automation** prompts appear on demand per app. The wizard explains them.
5. **Connect a client**: detected clients are listed with "Connect" buttons, the same as running `mac-mcp connect`.
6. Try it: "Ask your agent: *Open TextEdit and write a haiku*".

### 8.3 `mac-mcp` CLI
```
mac-mcp stdio [--direct] [--profile minimal|standard|full]   # what MCP clients run
mac-mcp http  [--port 7788] [--allow-remote --tls-cert … --tls-key …]
mac-mcp connect <client> [--dry-run] [--scope user|project]  # write client config (with backup)
mac-mcp connect --list                                       # detect installed clients
mac-mcp doctor                                               # OS, permissions, app, socket, clients, versions
mac-mcp config get|set|edit|path
mac-mcp skill install [--target claude|codex|gemini|cursor|dir]
mac-mcp debug dump-tree --app TextEdit > fixture.json        # for bug reports & test fixtures
```

### 8.4 Clients supported by `mac-mcp connect` (v1 target)
Claude Desktop, Claude Code, Cursor, VS Code (Copilot), Windsurf, Zed, OpenAI Codex CLI, Gemini CLI, LM Studio, Goose, Cline, Continue. Each has a config writer and a doc page. Config paths and keys are verified per client in Phase 3, because they change often.

Generic config, which also works for any client not listed:
```json
{
  "mcpServers": {
    "mac": { "command": "mac-mcp", "args": ["stdio"] }
  }
}
```
Claude Code: `claude mcp add mac -- mac-mcp stdio`

---

## 9. Agent framework integration

Anything that speaks MCP works through stdio (`mac-mcp stdio`) or Streamable HTTP (`http://127.0.0.1:7788/mcp` + `Authorization: Bearer …`). We ship a **tested example per framework** in `examples/`, run by a CI smoke test that checks tool listing plus one `status` call. Framework APIs move fast, so we pin versions in each example and re-verify every release.

| Framework | Lang | Integration (verify API at implementation time) | Example |
|---|---|---|---|
| OpenAI Agents SDK | Py/TS | `MCPServerStdio` / `MCPServerStreamableHttp` | `examples/python/openai_agents.py` |
| LangChain / LangGraph | Py/TS | `langchain-mcp-adapters` `MultiServerMCPClient` | `examples/python/langgraph_agent.py` |
| CrewAI | Py | `MCPServerAdapter` (crewai-tools) | `examples/python/crewai_crew.py` |
| Google ADK | Py | `McpToolset` + stdio connection params | `examples/python/google_adk.py` |
| Microsoft Agent Framework | Py/.NET | `MCPStdioTool` | `examples/python/ms_agent_framework.py` |
| Pydantic AI | Py | `MCPServerStdio` | `examples/python/pydantic_ai.py` |
| LlamaIndex | Py | `BasicMCPClient` + `McpToolSpec` | `examples/python/llamaindex.py` |
| smolagents | Py | `MCPClient` / `ToolCollection.from_mcp` | `examples/python/smolagents.py` |
| Claude Agent SDK | Py/TS | `mcp_servers` option | `examples/python/claude_agent_sdk.py` |
| Agno, Strands | Py | `MCPTools` / `MCPClient` | `examples/python/…` |
| Vercel AI SDK | TS | AI SDK MCP client | `examples/ts/ai_sdk.ts` |
| Mastra | TS | `@mastra/mcp` `MCPClient` | `examples/ts/mastra.ts` |
| Local models (Ollama, LM Studio) | — | Through any framework above, or LM Studio's native MCP; use `--profile minimal` | `docs/local-models.md` |

Sketch (OpenAI Agents SDK):
```python
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async with MCPServerStdio(params={"command": "mac-mcp", "args": ["stdio"]},
                          cache_tools_list=True) as mac:
    agent = Agent(name="Mac operator",
                  instructions="Use snapshot before acting; prefer element refs.",
                  mcp_servers=[mac])
    result = await Runner.run(agent, "Open Notes and create a note titled 'Groceries'")
```

Sketch (LangChain / LangGraph):
```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent

client = MultiServerMCPClient({"mac": {"command": "mac-mcp", "args": ["stdio"], "transport": "stdio"}})
agent = create_agent("<your-model>", await client.get_tools())
```

Integration helpers that work for any client or framework:
- **Server `instructions`**: an operating guide sent to every MCP client.
- **Agent Skill** (`skills/mac-mcp/SKILL.md`, open Agent Skills format): an observe → act → verify loop, when to use menus or refs or coordinates, recovery from `STALE_REF`, and safety etiquette. Install it with `mac-mcp skill install`.
- **`computer` profile**: for vision-first computer-use agent loops.
- **Stable tool names and schemas** under SemVer, so framework users' prompts don't break.

---

## 10. Configuration

Precedence: CLI flags > `MAC_MCP_*` environment variables > config file > defaults. File: `~/.config/mac-mcp/config.toml`, also editable from the app's Settings.

```toml
[server]
http_enabled = false
http_host = "127.0.0.1"
http_port = 7788          # token is generated and stored in the Keychain

[tools]
profile = "standard"      # minimal | standard | full
enable  = []              # e.g. ["scripting", "browser"]
disable = []              # e.g. ["clipboard"]

[safety]
confirm = "destructive"   # never | destructive | always
yield_to_user = true
indicator = true
deny_apps = ["com.apple.keychainaccess", "com.1password.1password"]
allow_apps = []           # if non-empty: only these apps may be controlled
allow_secure_fields = false
max_actions_per_minute = 120

[capture]
max_edge = 1280
format = "jpeg"           # jpeg | png
quality = 80

[perception]
max_elements = 150
ax_timeout_ms = 1000
enhance_electron_chrome = true

[audit]
enabled = true
log_args = "redacted"     # none | redacted | full
```

---

## 11. Repository structure

```
mac-mcp/
├─ Package.swift                    # SwiftPM: Engine, Policy, Server, MCPAdapter, Bridge, CLI
├─ project.yml                      # XcodeGen: MacMCPApp, TestBench
├─ Sources/
│  ├─ MacMCPEngine/{Accessibility,Snapshot,Input,Capture,Vision,Apps,System}/
│  ├─ MacMCPPolicy/
│  ├─ MacMCPServer/{Tools,Schemas,Handles,Transports}/
│  ├─ MCPAdapter/
│  ├─ MacMCPBridge/
│  └─ mac-mcp/                      # CLI entry (swift-argument-parser)
├─ App/MacMCPApp/                   # menu-bar app (SwiftUI/AppKit), Sparkle, onboarding
├─ App/TestBench/                   # fixture app for E2E
├─ Tests/{EngineTests,PolicyTests,ServerContractTests,E2ETests}/
├─ Tests/Fixtures/ax-trees/*.json   # recorded AX trees for offline perception tests
├─ packaging/{homebrew,npm,pypi,mcpb,dmg}/  + server.json
├─ skills/mac-mcp/SKILL.md
├─ examples/{python,ts}/            # one per framework, CI smoke-tested
├─ evals/{tasks/*.yaml,runner/,results/}
├─ docs/                            # docs site (MkDocs Material or Starlight) + adr/
├─ .github/{workflows,ISSUE_TEMPLATE}/ + PULL_REQUEST_TEMPLATE.md
├─ README.md  CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md  CHANGELOG.md
├─ AGENTS.md / CLAUDE.md            # guidance for AI coding agents working on the repo
└─ LICENSE (Apache-2.0)
```

> Note: the current `.gitignore` is the Python template. Replace or extend it with Swift/Xcode
> entries (`.build/`, `DerivedData/`, `*.xcuserstate`, `xcuserdata/`) in Phase 0.

---

## 12. Testing, QA and evals

| Layer | What | Where it runs |
|---|---|---|
| Unit | Key-combo parser, layout mapping, pruning, diff, ref resolution, policy decisions, config | Any macOS runner |
| Perception (offline) | Snapshot formatting and pruning over **recorded AX tree fixtures** (`debug dump-tree`), plus token-count budgets | Any macOS runner |
| MCP contract | `tools/list` **golden file** (catches accidental schema breaks), every tool against a `FakeEngine`, error codes, both transports, version negotiation; MCP Inspector CLI | Any macOS runner |
| E2E | TestBench app (buttons, fields, secure field, table, outline, menus, sheet, web view, slider, popup) driven through the real engine | GitHub macOS runners **if** AX can be granted in CI (spike S5: `xa11y/setup-a11y` or per-user TCC.db), otherwise self-hosted |
| Real-app smoke | Finder, TextEdit, Notes, Safari, System Settings, Calculator, Chrome, an Electron app | Nightly self-hosted or a pre-release manual checklist |
| Security | Policy bypass attempts (coordinate click onto a deny-listed app, typing into Terminal with scripting off, secure field, unpaired client), HTTP auth/Origin/Host tests, argument fuzzing | CI |
| Clean install | Every install channel on a **fresh macOS VM** (Tart or UTM), timed against the 2-minute goal | Pre-release |
| Evals | `evals/tasks/*.yaml`: setup, goal, success check (verified by AX/AppleScript state). Runner drives an agent framework against Mac-MCP and reports success rate, steps, tokens, and time. ~30 tasks for v1; consider a macOSWorld subset for external comparability. | Manual/nightly; results published per release |
| Performance | Snapshot p50/p95 latency, action latency, tokens per snapshot on fixtures | CI trend (regression alerts) |

**v1 quality targets (to calibrate in Phase 1):** snapshot p50 < 300 ms on typical windows; a default snapshot < ~2.5k tokens; first action within 2 minutes of install; eval success ≥ the baseline set in Phase 4, and no worse in later releases.

---

## 13. CI/CD and release engineering

- **`ci.yml`** (PRs): swift-format/SwiftLint → universal build → unit, perception, contract, and security tests on `macos-15` + `macos-26` → E2E job → CodeQL → example smoke tests.
- **`release.yml`** (on tag `v*`, via release-please + Conventional Commits):
  1. Build universal → sign with **Developer ID Application** (Hardened Runtime, entitlements) → `notarytool submit --wait` → `stapler staple`
  2. DMG + ZIP → Sparkle appcast (EdDSA) → GitHub Release with SHA-256 + **build provenance attestations**
  3. Bump the Homebrew tap → publish the npm and PyPI launchers (**trusted publishing / OIDC**) → `mcpb pack` → `mcp-publisher publish` to the MCP Registry
- **Secrets:** Developer ID cert (.p12 + password), App Store Connect API key for notarization, Sparkle EdDSA key, tap token. Keep them in protected `release` environments with required reviewers.
- **Versioning:** SemVer. Tool names and schemas are public API, so breaking changes require a major version (enforced by the golden-file test). The launchers check app version compatibility and prompt for an update.

---

## 14. Open-source project setup

- **README**: a 30-second pitch, demo GIF/video, a quickstart per install channel, a client matrix, a safety summary, and links to docs.
- **Docs site** (GitHub Pages): install, permissions explained (with screenshots), each client, each framework, tools reference (generated from schemas), configuration, security model, troubleshooting (`doctor`), FAQ, `llms.txt`.
- **CONTRIBUTING**: Xcode setup, **stable local code-signing identity so TCC grants survive rebuilds**, `tccutil reset Accessibility <bundle-id>`, running TestBench, recording AX fixtures, and adding a tool (checklist: schema, annotations, policy, tests, docs).
- **Governance**: CODE_OF_CONDUCT (Contributor Covenant), SECURITY.md, issue templates (a bug template asks for `mac-mcp doctor` output plus an AX dump), PR template, labels (`good first issue`, `app-compat`, `tool-request`), GitHub Discussions, and a public roadmap (GitHub Projects).
- **App-compatibility list**: a community-maintained table of apps × tested status, which drives `app-compat` issues.
- **Launch**: the MCP Registry, plus directories (Glama, PulseMCP, Smithery, awesome-mcp-servers), HN "Show HN", r/macapps, r/LocalLLaMA, and MCP community channels.

---

## 15. Roadmap and milestones

Assumes one maintainer working with AI coding assistance. Weeks are relative to kickoff.

### Phase 0: Foundations and spikes (weeks 1–2)
- Confirm the decisions in §17. **Enroll in the Apple Developer Program** (individual enrollment is fast; an organization needs a D-U-N-S number and can take weeks). Reserve the package names (PyPI `mac-mcp`, npm scope, Homebrew tap repo).
- Scaffold the repo (SwiftPM + XcodeGen), the CI skeleton, community files, and the docs skeleton. Fix `.gitignore`.
- **Timeboxed spikes**, each producing an ADR:
  - **S1 Permissions:** the host app plus socket bridge. Grant once, then verify it works when launched from Claude Desktop, Claude Code (terminal), Cursor, VS Code, and a Python script. Prototype client pairing.
  - **S2 AX performance:** snapshot budgets on heavy apps (Xcode, Chrome, Slack/Electron, a large Finder folder, System Settings), plus the Chrome/Electron accessibility flags.
  - **S3 Input:** `AXPress`/`AXSetValue` vs CGEvent vs `CGEventPostToPid` across 15 apps, which apps work in the background, and HID-idle detection for yield-to-user.
  - **S4 Capture:** ScreenCaptureKit latency, window/app exclusion, and the monthly prompt's behavior on 15/26/27.
  - **S5 CI permissions:** granting Accessibility on GitHub-hosted runners.
  - **S6 MCP SDK:** swift-sdk stdio + Streamable HTTP server, `structuredContent`, annotations, and interop with Claude Desktop/Code, Cursor, VS Code, OpenAI Agents SDK, and LangChain adapters. Decide whether to contribute 2026-07-28 support upstream.
- **Exit:** ADRs merged; go/no-go on the architecture.

### Phase 1: Engine and MVP tools in direct mode (weeks 3–5) → `v0.1.0-alpha`
- Engine: AX snapshot, pruning, refs, and diff; layout-aware input; apps, windows, and displays; screenshot.
- Tools: the `minimal` profile + `find`, `inspect`, `select_menu`, `wait_for`, `list_*`, `status`.
- `mac-mcp stdio --direct`, `mac-mcp doctor`, `debug dump-tree`; unit, perception, and contract tests; TestBench v1.
- **Exit:** Claude Code and Claude Desktop complete 10 scripted tasks (TestBench, TextEdit, Notes, Finder), and snapshot p50 < 300 ms on TestBench.

### Phase 2: Host app and safety (weeks 6–7) → `v0.2.0-beta`
- MacMCP.app: onboarding, permission status, socket server, multi-client support, client pairing, overlay, kill switch, yield-to-user, activity log, settings.
- Policy engine: profiles and toolsets, app allow/deny with hit-testing, secure-field and Terminal rules, confirmations (native + elicitation), rate limit, audit log.
- Streamable HTTP (localhost + Keychain token + Origin/Host checks).
- **Exit:** the security test suite is green, and a single permission grant works across ≥ 4 clients.

### Phase 3: Distribution and one-command integration (weeks 8–9) → `v0.3.0` **public preview**
- Signing and notarization pipeline, DMG, Sparkle, Homebrew tap, npm/PyPI launchers, MCPB, Registry `server.json`.
- `mac-mcp connect` for 12 clients, the docs site, README, and a demo video.
- **Exit:** on a fresh VM, each channel goes from install to first action in **< 2 minutes**.

### Phase 4: System toolsets, OCR, frameworks, evals (weeks 10–11) → `v0.9.0-rc`
- `system`, `scripting`, `browser`, and `computer` toolsets; Vision OCR; `fill_form`.
- `examples/` for 12+ frameworks with CI smoke tests; the Agent Skill.
- An eval suite of ~30 tasks with baseline results for 2–3 models; performance tuning; checks on non-US keyboard layouts and non-English UIs.
- **Exit:** all examples green; eval baseline published.

### Phase 5: Hardening and 1.0 launch (week 12) → `v1.0.0`
- Threat-model review, argument fuzzing, macOS 27 validation, docs polish, and freezing the tool API for 1.x. Launch posts and directory submissions.

### Post-1.0 backlog (prioritize from community feedback)
- Native **PIM toolsets** (Calendar/Reminders through EventKit, Contacts), which are reliable because the host app owns its usage strings.
- Full MCP **2026-07-28** support (if not already in 1.0), the Tasks extension for long-running operations, and OpenTelemetry tracing.
- Per-client **control lease** and multi-agent coordination; remote access with OAuth (2026-07-28 auth, Client ID Metadata Documents).
- Recording and replay of action sequences ("macros"), exportable as Agent Skills or Shortcuts.
- Deeper **App Intents/Shortcuts** integration, and tracking Apple's agent and MCP direction to complement any system-level support it ships.
- Per-app adapters (Finder, Mail, Xcode), a plugin API for community toolsets, and localization of the host app.

---

## 16. Risks and mitigations

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| A macOS update changes TCC or AX behavior | Med / High | Test the macOS beta every June; isolate engine code behind the engine API; keep direct mode as a fallback |
| No Apple Developer account means unsigned builds (TCC resets, Gatekeeper friction, no Homebrew cask) | Med / High | Budget $99/yr in Phase 0; fall back to source and direct mode with clear docs |
| The Swift MCP SDK lags the spec | High / Med | Use the `MCPAdapter` boundary; contribute upstream; 2025-11-25 remains negotiable by clients |
| Inconsistent app accessibility (Electron, custom UIs, games) | High / Med | Chrome/Electron accessibility flags, OCR + screenshot + coordinates fallback, and a community app-compat list |
| The monthly Screen Recording re-prompt annoys users | High / Low | AX-first design so Screen Recording is optional; explain it in the UI |
| Prompt injection leads to harmful actions | Med / High | Safe defaults, confirmations, deny-lists, kill switch, audit log, and clear docs |
| Crowded space with an established competitor (Peekaboo) | High / Med | Differentiate (§2) on safety, spec quality, Windows-MCP parity, framework kit, and published evals; collaborate where it makes sense |
| CI can't grant Accessibility | Med / Med | Spike S5; offline fixture tests; a self-hosted runner (Mac mini) for E2E |
| Apple ships a native agent/MCP layer | Low–Med / Med | Position Mac-MCP as generic GUI hands and eyes; integrate with App Intents/Shortcuts rather than compete |
| Maintainer bandwidth | Med / Med | Keep the scope small (§1 non-goals), a strong contributor guide, and good-first-issues |

---

## 17. Decisions needed from you

1. **Language:** Swift-native (recommended) vs the Python hybrid (Python MCP layer + Swift helper). The repo's current `.gitignore` is Python, so confirm before scaffolding.
2. **Apple Developer Program:** will you enroll (individual or organization)? This decides signed and notarized releases and the Homebrew cask.
3. **Name/brand and namespace:** keep "Mac-MCP"? Create a GitHub org? Choose the npm scope and bundle ID.
4. **Minimum macOS:** 14 Sonoma (recommended, broader reach) vs 15 Sequoia (simpler).
5. **Scope of v1:** include the `browser` and `computer` toolsets in 1.0, or defer them to 1.1?
6. **Windows-MCP compatibility:** do we also want PascalCase tool aliases (`Click`, `Type`, …) so Windows-MCP prompts work unchanged?

---

## 18. Sources

- Windows-MCP: https://github.com/CursorTouch/Windows-MCP
- MacOS-MCP (CursorTouch): https://github.com/CursorTouch/MacOS-MCP
- Peekaboo: https://github.com/steipete/Peekaboo
- mcp-server-macos-use: https://github.com/mediar-ai/mcp-server-macos-use
- cua / cua-driver: https://github.com/trycua/cua/tree/main/libs/cua-driver
- tmc/axmcp: https://pkg.go.dev/github.com/tmc/axmcp@v0.2.0
- MCP spec 2026-07-28 changelog: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx
- MCP Python SDK releases: https://github.com/modelcontextprotocol/python-sdk/releases
- MCP Swift SDK releases: https://github.com/modelcontextprotocol/swift-sdk/releases
- MCPB (MCP Bundles): https://claude.com/docs/connectors/building/mcpb
- TCC responsible-process reports: https://github.com/anthropics/claude-code/issues/36832 · https://claudeissues.com/issue/63032-claude-desktops-info-plist-missing-tcc-usage-strings-blocks-all-eventkit-based-m
- Screen Recording monthly prompt: https://9to5mac.com/2024/08/14/macos-sequoia-screen-recording-prompt-monthly/
- Homebrew Gatekeeper/notarization policy: https://workbrew.com/blog/homebrew-5-0-0
- macOS 27 Apple-silicon-only: https://appleinsider.com/articles/26/06/08/macos-27-compatibility-list-focuses-entirely-on-apple-silicon
- WWDC26 App Intents: https://developer.apple.com/videos/play/wwdc2026/345/ · https://developer.apple.com/wwdc26/guides/macos/
- Granting Accessibility in CI: https://github.com/xa11y/setup-a11y
- macOSWorld benchmark: https://arxiv.org/abs/2506.04135v2
- Agent Skills standard: https://agentskills.io/
