# AXPilot: MCP server for macOS (Project Plan)

> An open-source MCP server that lets any AI agent see and operate a local Mac,
> the macOS counterpart of [Windows-MCP](https://github.com/CursorTouch/Windows-MCP).
> It installs in minutes, is safe by default, and works with any MCP client or agent framework.

Status: **Draft v2. Decisions from review applied (see [§0.1](#01-decisions-log)).** · Last updated: 2026-10-19 · License: Apache-2.0

> **Name:** "AXPilot" is the proposed name (see [ADR-7](#adr-7-name-axpilot)). Until it's confirmed,
> treat `axpilot` / `AXPilot` in this document as a placeholder that is easy to search and replace.

---

## 0. TL;DR

| Topic | Plan |
|---|---|
| **What** | A local MCP server that exposes macOS UI perception (accessibility tree, screenshots, OCR) and control (mouse, keyboard, menus, apps, windows, browser, opt-in scripting) as MCP tools. |
| **Language** | **Python 3.11+**: the official MCP Python SDK v2 (supports the 2026-07-28 spec) plus **PyObjC** for the native macOS APIs. |
| **The permission problem, solved for free** | A **tiny, frozen native launcher app (`AXPilot.app`)** starts the Python host as its child. macOS attributes permissions to the launcher, so you grant Accessibility **once** and every client (Claude Desktop, Claude Code, Cursor, VS Code, scripts) can use it, across all Python updates. The launcher is ad-hoc signed, which is free. No $99 Apple account is needed for v1. |
| **MCP** | Tools with annotations and `outputSchema`, stdio and Streamable HTTP transports (no deprecated SSE), and a stateless design with server-minted handles. |
| **Install** | `uv tool install axpilot && axpilot setup` (or `pipx`, or a one-line `install.sh`). `axpilot connect <client>` writes the config for 12 clients. MCP Registry listing, plus an optional `.mcpb` bundle for Claude Desktop and a Homebrew tap. |
| **Frameworks** | Anything that speaks MCP: OpenAI Agents SDK, LangChain/LangGraph, CrewAI, Google ADK, Microsoft Agent Framework, Pydantic AI, LlamaIndex, smolagents, Claude Agent SDK, Vercel AI SDK, Mastra, and others. Ships tested examples, an Agent Skill, a **`computer` compatibility profile**, and a **Windows-MCP naming profile**. |
| **Safety** | Off-by-default scripting, native confirmations, a kill-switch hotkey, yield-to-user, app deny-lists, secure-field protection, screenshot redaction, client pairing, an audit log, and **no telemetry**. |
| **Min OS** | **macOS 14 Sonoma+**, on Apple silicon and Intel. Tested on 14/15/26/27. |
| **Timeline** | About 12 weeks to 1.0: an alpha around week 5, a public preview around week 9, and 1.0 around week 12. |

### 0.1 Decisions log
| # | Question | Decision |
|---|---|---|
| 1 | Language | **Python** (ADR-1 (§4)) |
| 2 | Apple Developer Program ($99/yr) | **Not for v1.** Free alternative: a frozen ad-hoc-signed launcher, distributed only through package managers (no quarantine, so no Gatekeeper prompt). Upgrade to Developer ID later if sponsors fund it (ADR-5 (§4)) |
| 3 | Name | Proposed **AXPilot** (`axpilot` is free on PyPI and npm) ([ADR-7](#adr-7-name-axpilot)) |
| 4 | Minimum macOS | **macOS 14 Sonoma** ([ADR-3](#adr-3-minimum-macos-14-sonoma)) |
| 5 | `browser` + `computer` toolsets in 1.0 | **Yes**. Both ship in 1.0. `browser` is off by default and `computer` is an opt-in profile. |
| 6 | Windows-MCP-compatible tool names | **Yes**, as a `naming = "windows-mcp"` profile ([§6.8](#68-windows-mcp-naming-profile)) |

---

## 1. Goals, non-goals, users

### Goals
1. **Any agent can operate any Mac app.** It should work with apps that have good accessibility support, and degrade gracefully (OCR, screenshots, coordinates) for apps that don't.
2. **Two-minute setup on a fresh Mac.** One install command, one permission grant, and one command to connect a client.
3. **Framework-agnostic.** Standard MCP over stdio and Streamable HTTP, with copy-paste examples for the top frameworks.
4. **Safe by default.** It runs on personal machines, so every risky capability is opt-in.
5. **Token-efficient and model-agnostic.** Compact text UI snapshots let text-only and local models work, and vision stays optional.
6. **Open-source quality.** Tests, evals, docs, reproducible releases, and an easy contributor path (it's Python).

### Non-goals (v1)
- Being an agent itself. AXPilot provides tools; the agent loop lives in the client or framework.
- Remote fleets or cloud Macs. The target is local use, and remote access is opt-in and advanced.
- Mac App Store distribution, which is incompatible with Accessibility automation.
- Bypassing macOS security: no TCC hacks, no automating system authentication dialogs.
- A general file-system server. We interoperate with the official Filesystem MCP server instead.
- Games and non-accessible canvases, beyond best-effort screenshot plus coordinates.

### Target users
Developers using Claude Code, Cursor, Codex, or Gemini CLI who want GUI control; agent builders in Python and TypeScript frameworks; power users with desktop assistants and local LLMs; QA teams running natural-language GUI tests.

---

## 2. Landscape research

| Project | Stack | Traction (approx.) | What we learn |
|---|---|---|---|
| **CursorTouch/Windows-MCP** | Python, UIAutomation | ~9.5k★ | The reference UX: about 20 tools, `uvx` install, a Claude Desktop extension, config TOML, a desktop-control gate, and bearer/OAuth for HTTP. Telemetry is **on by default**. |
| **CursorTouch/MacOS-MCP** | Python + PyObjC | ~210★ | Our closest peer: same language and similar tools. Its known issues are mostly **TCC permission identity problems** (uv Python symlinks can't be granted, child-process permission failures). Our launcher design targets exactly this. |
| **steipete/Peekaboo** | Swift (+ Node launcher) | ~5.3k★ | CLI, MCP server, and menu-bar app. Opaque element IDs from a snapshot, background input, and permissions held by a signed app. macOS 15+. |
| **mediar-ai/mcp-server-macos-use** | Swift | ~360★ | "Act then traverse": each action returns the updated accessibility tree, optionally as a **diff**. |
| **trycua/cua: cua-driver** | Rust + app bundle | (large monorepo) | Solves TCC with a standalone `CuaDriver.app` daemon that holds grants, with the CLI proxying to it, plus a "direct mode". |

**Platform context**
- **MCP spec 2026-07-28** is final. It is stateless (no `initialize`, no sessions), and cross-call state must use **server-minted handles in tool arguments**. Servers should return tools in deterministic order. HTTP+SSE and the Logging, Sampling and Roots features are deprecated, and Tasks became an extension.
- **MCP Python SDK v2** (`mcp` 2.3.0, Python ≥ 3.10) is stable and serves 2026-07-28 plus older revisions. `FastMCP` was renamed `MCPServer`, with the same decorator API.
- **PyObjC 12.x** (Python ≥ 3.10) wraps ApplicationServices (AX), Quartz (CGEvent), ScreenCaptureKit, Vision, and AppKit.
- **Apple**: WWDC26 expanded App Intents. I found no Apple source saying macOS exposes a system-wide MCP host, so GUI automation still needs Accessibility. **macOS 27 is Apple-silicon-only.**
- **Homebrew** is removing casks that fail Gatekeeper (Sept 2026). This affects casks only. Our main channel is PyPI.

### How we differentiate (especially from MacOS-MCP, our closest peer)
1. **Permissions that just work.** A frozen launcher holds a stable identity, so you grant once and it works for every client and every update.
2. **Safety and privacy as the headline feature.** No telemetry, client pairing, kill switch, confirmations, redaction, and audit.
3. **Spec-first MCP.** Schemas, annotations, structured output, deterministic order, stateless handles, and no SSE.
4. **Windows-MCP compatibility.** A naming profile lets Windows-MCP prompts and agents port unchanged.
5. **A framework integration kit.** Tested examples in CI, an Agent Skill, and a `computer` profile.
6. **Published evals.** A reproducible macOS task suite, with results published each release.

(Contributing to MacOS-MCP instead is a valid alternative. We build our own because the launcher and safety architecture are fundamental changes, not patches.)

---

## 3. macOS technical realities (constraints that shape the design)

| Reality | Consequence |
|---|---|
| **TCC "responsible process".** Accessibility, Screen Recording, and Automation are checked against the *responsible app*. Spawned by Claude Desktop, that is Claude.app; from a terminal, it is Terminal or iTerm. Grants to `python` often don't apply, and missing usage strings cause **silent denials**. | Run the host as a **child of our own app bundle** launched by LaunchServices. The bundle is the responsible process, so permissions attach to it. |
| **How TCC pins identity.** An ad-hoc-signed app is pinned to its exact **cdhash**, so any binary change resets the grant. A certificate-signed app is pinned to a designated requirement that survives rebuilds signed with the same certificate. | Keep the launcher **tiny and frozen**: it rarely changes, so the grant persists. All logic lives in Python, which can change freely (see ADR-5 in §4). |
| **Gatekeeper only assesses quarantined files.** Browsers set `com.apple.quarantine`, while command-line downloaders (curl, and very likely pip and uv; to verify) don't. | Distribute through **package managers**, not browser downloads, so the un-notarized launcher never triggers "unidentified developer". |
| **Accessibility (AX) API**: cross-process calls. They are slow on big trees, and hung apps block calls for seconds. | Batch reads (`AXUIElementCopyMultipleAttributeValues`), `AXUIElementSetMessagingTimeout`, depth/count budgets, a thread pool, and caching invalidated by `AXObserver`. Python bridge overhead is measured in spike S2. |
| **Chrome and Electron** expose a thin tree by default. | Set `AXManualAccessibility` / `AXEnhancedUserInterface` on the app element (with an opt-out). |
| **Screen capture**: ScreenCaptureKit is the modern path (`SCScreenshotManager` needs macOS 14+). macOS 15+ shows a recurring **"Allow for one month"** prompt. | **Screen Recording is optional**, because AX-only mode works without it. Use `SCContentFilter` to exclude our overlay and deny-listed apps. |
| **CGWindowList titles** need Screen Recording. | Prefer AX for window lists. |
| **Synthetic input** (CGEvent) needs focus and moves the real cursor. | AX-first strategy ladder (see [§5.5](#55-action-strategies)). |
| **Secure Event Input** blocks synthetic keys. | Detect it with `IsSecureEventInputEnabled()` and return `SECURE_INPUT_ACTIVE`. Never type into `AXSecureTextField` by default. |
| **Keyboard layouts** differ (AZERTY, Dvorak, JIS…). | Type text with Unicode CGEvents (layout-independent). Map shortcuts with `TISCopyCurrentKeyboardLayoutInputSource` + `UCKeyTranslate`, called through `ctypes` because PyObjC's Carbon coverage is partial. |
| **Coordinates**: points vs pixels, Retina, multi-display negative origins, downscaled screenshots. | The public API uses **screen points**. Screenshots return `screenshot_id` + scale/origin so model pixel coordinates can be mapped back. |
| **Spaces / Stage Manager** have no public API. | Work on the current Space and document the limits. |
| **System auth dialogs** can't be automated. | Return `NEEDS_USER`. |
| **Automation (Apple Events)** prompts per target app. | The launcher's Info.plist declares `NSAppleEventsUsageDescription`. Document which tools trigger which prompts. |
| **Notifications, EventKit, and Contacts** need a bundle with usage strings. | The launcher bundle declares them, which makes the post-1.0 PIM toolsets feasible. |

---

## 4. Key decisions (ADRs)

These become files in `docs/adr/` in Phase 0. The spikes can overturn them.

### ADR-1: Language: Python + PyObjC, with a frozen native launcher
- **Python 3.11+** (stdlib `tomllib`; `mcp` and `pyobjc` need ≥ 3.10). Use `uv` for environments and builds.
- **MCP:** the official `mcp` SDK v2 (`MCPServer`), pinned `>=2.3,<3`. This gives first-class 2026-07-28 support and backward compatibility. Standalone FastMCP 3.x is the fallback if we need its middleware.
- **macOS APIs:** `pyobjc-core` + framework packages: `ApplicationServices` (AX), `Quartz` (CGEvent, CGWindow), `Cocoa`/AppKit (UI, NSWorkspace, pasteboard), `ScreenCaptureKit`, `Vision`. Use `ctypes` for the few Carbon TIS/UCKeyTranslate calls.
- **Other deps (kept minimal):** `pydantic` (via mcp), `typer` (CLI), `Pillow` (resize and annotate screenshots), `tomli-w`.
- **Native code** is limited to the ~100-line launcher (Swift or C), built **once per launcher version**. No other native code is needed. If spike S2 shows AX traversal is too slow, we add an optional native accelerator later.

### ADR-2: Architecture: frozen launcher → Python host, plus a thin stdio bridge
- `AXPilot.app` (frozen launcher, LSUIElement) is launched via LaunchServices (`open -g -a AXPilot`, or a login item). It reads `runtime.json` and **spawns (does not exec)** the Python host: `<runtime>/bin/python -m axpilot.host`. As the child of the launcher, the host's responsible process is `AXPilot.app`, so TCC grants made to AXPilot apply. The launcher forwards signals and restarts the child with backoff.
- The **Python host** runs the engine, the policy layer, the menu-bar UI (PyObjC/AppKit), and the MCP server. It listens on a per-user **Unix socket** and optionally on localhost Streamable HTTP.
- MCP clients spawn `axpilot stdio`, a **bridge** between stdio and the socket that auto-launches the app if needed. The bridge needs no permissions.
- **Direct mode** (`axpilot stdio --direct`) runs the host in-process, so permissions attach to the launching app. This is for development, CI, and users who prefer it.
- **This rests on the main assumption to verify (spike S1):** that a child of an ad-hoc-signed bundle inherits that bundle's TCC grants on macOS 14, 15, 26, and 27. Fallbacks, in order:
  - (a) Build the host as a self-contained app with embedded Python (PyInstaller or Briefcase), so AX calls run inside the bundle's own process.
  - (b) Move the AX, input, and capture calls into a small native helper (the hybrid model).

### ADR-3: Minimum macOS 14 Sonoma
- It is required for `SCScreenshotManager`, gives a modern AX baseline, and still covers Intel Macs on macOS 14–26. macOS 13 is end-of-life.
- The launcher is a **universal2** binary (arm64 + x86_64). Python wheels resolve per architecture through uv and pip.
- Test matrix: GitHub `macos-14`, `macos-15`, `macos-26` runners, plus manual or self-hosted macOS 27.

### ADR-4: MCP protocol surface
- **Tools** with `inputSchema`, `outputSchema` + `structuredContent`, a compact text `content`, and annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). `tools/list` order is deterministic.
- **Stateless handles:** `snapshot_id`, `screenshot_id`, and element `ref`s are passed as arguments. There is no per-session state, and multiple clients are safe.
- **Transports:** stdio (default) and Streamable HTTP. **No SSE.**
- A server `instructions` operating guide; optional resources (`axpilot://snapshot/{id}`); progress notifications for `wait_for`; the Tasks extension later.
- Confirmations: a **native dialog in the host** works with every client. Elicitation through MRTR is used when the client supports it (direct mode).
- Logs go to stderr, `~/Library/Logs/AXPilot/`, and optionally OpenTelemetry (MCP logging is deprecated).

### ADR-5: Signing and distribution without a paid Apple account
Apple's fee waiver covers only nonprofits, accredited education, and government, not individuals. So:

| Tier | How | Cost | Trade-offs |
|---|---|---|---|
| **1. Frozen ad-hoc launcher (v1 default)** | The launcher is built once per *launcher version* in CI, `codesign -s -` (ad-hoc), published as a release asset with its **cdhash**, and embedded byte-identical in every wheel. `axpilot setup` copies it to `~/Applications`. | Free | ✅ The grant persists across all Python releases because the launcher bytes never change. ✅ No signing key that could leak. ⚠️ A new launcher version means a one-time re-grant (`doctor` detects this and guides you). ⚠️ No browser-download distribution. |
| 2. Self-signed project certificate | Sign the launcher with a long-lived self-signed cert. Its designated requirement survives launcher rebuilds. | Free | ⚠️ If the private key leaks, an attacker could sign a binary that silently inherits users' grants. ⚠️ Gatekeeper still requires "Open Anyway" for downloads. **Not recommended.** |
| 3. Developer ID + notarization | Sign and notarize with an Apple Developer account. | $99/yr | ✅ DMG, Homebrew cask, Sparkle, no Gatekeeper friction. ⚠️ Switching changes the identity, so a one-time re-grant. Adopt it later via GitHub Sponsors / Open Collective funding. |

**Distribution rule for v1:** the launcher only reaches users through **package managers** (pip/uv/pipx), plus a Homebrew formula once verified. These don't apply quarantine, so Gatekeeper never blocks the un-notarized launcher (spike S1 verifies this on each macOS version). `axpilot doctor` verifies the installed launcher's cdhash against the published value, which is also a tamper check.

### ADR-6: Privacy
**No telemetry**, ever by default. Opt-in crash reports with explicit consent can come later.

### ADR-7: Name: AXPilot
- **Why:** "AX" is macOS's Accessibility API, the core of how it works, and "pilot" says it drives your Mac. `axpilot` is **available on both PyPI and npm**, and the name avoids Apple trademarks ("Mac", "Cocoa", "Tahoe"), whose guidelines discourage use in product names. Descriptive use such as "MCP server *for macOS*" is fine in taglines.
- Identifiers: package and CLI `axpilot`; app `AXPilot.app`; bundle ID `io.github.vansh3503.axpilot`; env prefix `AXPILOT_`; MCP Registry name `io.github.vansh3503/axpilot`.
- Alternatives if you prefer: **Pomme** (`pomme-mcp`, French for "apple"; free on PyPI and npm) or **Dexter** (`dexter-mcp`, as in "dexterity").
- Repo: rename `Mac-Mcp` → `axpilot` (GitHub redirects old URLs). Use the tagline "AXPilot: the MCP server for macOS" and topics `mcp`, `macos`, `computer-use`, `accessibility` for discoverability. Reserve the PyPI name in Phase 0.

---

## 5. Architecture

### 5.1 Component diagram

```
 MCP clients                          Agent frameworks
 (Claude Desktop/Code, Cursor,        (OpenAI Agents SDK, LangGraph, CrewAI, ADK,
  VS Code, Codex, Gemini CLI,          Pydantic AI, LlamaIndex, smolagents,
  LM Studio, Zed, Windsurf, …)         MS Agent Framework, Vercel AI SDK, Mastra…)
        │ spawn `axpilot stdio`                   │ Streamable HTTP
        ▼                                         │ 127.0.0.1:<port> + bearer token
 ┌────────────────────┐   Unix socket (0600)      │
 │ axpilot bridge     │───────────────┐           │
 │ (Python, no perms) │               ▼           ▼
 └────────────────────┘   ┌─────────────────────────────────────────────┐
                          │ AXPilot.app  — frozen native launcher        │
                          │  (ad-hoc signed, LSUIElement, usage strings) │
                          │        │ posix_spawn (child keeps the app as  │
                          │        ▼  its TCC "responsible process")      │
                          │  Python host  (axpilot.host)                 │
                          │   ├ MCP server  (mcp SDK v2 · MCPServer)     │
                          │   ├ Policy & safety: profiles · allow/deny · │
                          │   │  pairing · confirmations · rate limit ·  │
                          │   │  audit · kill switch · yield-to-user     │
                          │   ├ Engine (PyObjC): AX snapshot/refs/diff · │
                          │   │  CGEvent input · ScreenCaptureKit ·      │
                          │   │  Vision OCR · apps/windows/menus ·       │
                          │   │  clipboard · Shortcuts · AppleScript ·   │
                          │   │  shell · browser                          │
                          │   └ UI (AppKit): menu bar · onboarding ·     │
                          │      overlay · activity log · settings       │
                          └─────────────────────────────────────────────┘
          TCC grants (Accessibility, Screen Recording, Automation) → AXPilot.app
```

### 5.2 Python packages (`src/axpilot/`)

| Package | Responsibility | Imports PyObjC? |
|---|---|---|
| `engine/` | All macOS perception and actuation (`ax/`, `input/`, `capture/`, `vision/`, `apps.py`, `windows.py`, `menus.py`, `system/`, `browser/`) | **Yes** (the only place) |
| `engine/fake.py` | `FakeEngine`, backed by recorded AX-tree fixtures, used by tests on Linux | No |
| `policy/` | Profiles, toolsets, allow/deny, hit-testing, confirmations, rate limits, pairing, audit | No (uses the engine interface) |
| `server/` | MCP tool definitions (pydantic models → schemas), handle store, formatting, naming profiles, transports | No |
| `host/` | Socket server, NSApplication main loop, menu bar, overlay, hotkey, onboarding | Yes |
| `bridge/` | stdio ⇄ socket pipe, launch and discovery of `AXPilot.app` | No |
| `clients/` | Config writers for each MCP client (`connect`) | No |
| `cli.py` | `stdio`, `http`, `setup`, `doctor`, `connect`, `config`, `skill`, `update`, `debug` | — |
| `launcher/` (repo root, not in `src/`) | ~100-line Swift/C launcher source, Info.plist, build script | — |

Because `engine/` is behind an interface and PyObjC imports are isolated there, **the server, policy, and formatting code runs and is tested on Linux CI**. Only the engine and end-to-end tests need macOS runners.

### 5.3 Threads, concurrency, multi-client
- **Main thread:** the anyio/asyncio loop running the MCP server and socket. On macOS it turns the main thread's Cocoa run loop every 50 ms (`axpilot.runloop`), because some work only happens there: the keyboard-layout lookup (Text Input Sources must run on the main thread; workers schedule it with `PyObjCTools.AppHelper.callAfter`) and `NSWorkspace`'s app list. The sync SDK `Desktop` turns it while it waits for each call, and `AsyncDesktop` while it is open. With the kill switch or the indicator on (macOS, real engine), `host/ui`'s `AppKitPump` replaces that pump: it starts `NSApplication` as an accessory app and dispatches its events in the same 50 ms pass, so the Carbon hotkey and the overlay windows work without `NSApp.run()` (not yet verified on a Mac). *Planned:* the menu bar and `AXObserver` run-loop sources.
- **Worker threads:** blocking AX and CGEvent calls go through a bounded thread pool with per-call AX messaging timeouts (default 1 s).
- **Single "virtual hand":** input actions are serialized globally, while observations run concurrently. Multiple clients can connect. Conflicting input queues, and a short timeout returns `BUSY`. A per-client control lease is post-1.0.
- **Yield-to-user:** before each input action, check hardware HID idle time (`CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, …)`; spike S3 validates that it excludes our synthetic events). If the user is active, pause or return `USER_ACTIVE`.

### 5.4 Snapshot, refs, diffs (perception model)
- `snapshot` walks the target (by default the frontmost app's focused window plus menu bar, sheets, and dialogs) and returns **interactive elements** with short refs (`e1…eN`). Each has role, label/title, value (secure values never returned), state, frame in points, and AX actions. It also returns context (app, bundle ID, pid, window, focus, scroll areas) and a truncation notice.
- **Pruning:** drop invisible, off-screen, zero-size, and decorative nodes; collapse single-child groups; cap depth and count (default ~150, paged with `offset`); summarize long lists.
- **Refs:** an LRU of `snapshot_id → {ref → AXUIElement + locator path}`. Refs are **unique per session and stable**: numbers come from one counter per server and are never reused for another element (not after it disappears, not in another window or app), and when a window is captured again every element that is still there keeps its ref (matched by AX handle, identifier, path and name, role/name/frame; named elements are never matched by position, and recycled list rows get new refs). So a ref from any recent snapshot keeps naming its element, and a bare ref is looked up in the latest snapshot, then in older ones. When the element's handle has gone stale, the server re-finds it through the locator. If that fails, it returns `STALE_REF` with a hint. OCR refs (`o…`) are never reused either, but cannot be re-checked: they work until their window is captured again.
- **Act → observe:** actions take `report: none | diff | snapshot` (default `diff`). After acting, the server waits for the UI to settle (~150 ms of AX quiet, or a timeout) and returns the changes: focus, added/removed elements, new windows or dialogs, value changes.

Example `content` (the same data is in `structuredContent`):
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
| Click an element | 1) `AXPress` / matching AX action (background-safe) → 2) raise window + activate app → CGEvent click at element center → 3) report failure, with a diff showing nothing changed |
| Type text | 1) `AXValue` / `AXSelectedText` insert → 2) Unicode CGEvent keystrokes → 3) clipboard paste (save and restore) for long text |
| Shortcut | Layout-aware key codes plus modifiers via CGEvent. Optionally `CGEventPostToPid` (spike S3) |
| Scroll | `AXScrollToVisible` for refs → scroll-wheel CGEvents |
| Menu command | AX menu-bar traversal, fuzzy path match (often the most reliable way to drive Mac apps) |
| Coordinate click | Allowed, but **hit-tested first**: resolve the app/window under the point and apply policy (a deny-listed app means the click is blocked) |

### 5.6 Error model
`isError: true` + a stable `code` + an actionable `hint`: `PERMISSION_ACCESSIBILITY`, `PERMISSION_SCREEN_RECORDING`, `PERMISSION_AUTOMATION`, `LAUNCHER_CHANGED` (re-grant needed), `APP_NOT_FOUND`, `WINDOW_NOT_FOUND`, `STALE_REF`, `ELEMENT_NOT_ACTIONABLE`, `BLOCKED_BY_POLICY`, `USER_DENIED`, `USER_ACTIVE`, `PAUSED_BY_USER`, `SECURE_INPUT_ACTIVE`, `NEEDS_USER`, `TIMEOUT`, `BUSY`, `UNSUPPORTED_OS`.

---

## 6. Tool surface (v1.0)

Principles: **few, orthogonal tools**; `action` enums instead of tool sprawl; **profiles** for small models.

### 6.1 Profiles and toolsets
| Profile | Contents | Use |
|---|---|---|
| `minimal` (~8) | snapshot, screenshot, click, type_text, press_keys, scroll, app, wait_for | Small or local models |
| `standard` (**default**, ~20) | minimal + find, inspect, select_menu, set_value, fill_form, drag, window, open, list_windows, clipboard, status, … | Most users |
| `full` | standard + system extras + `scripting` + `browser` (each still policy-gated) | Power users |
| `computer` | A single `computer` tool (see §6.6) | Vision-first computer-use loops |

Toolsets toggle individually (`[tools] enable = ["browser"]`). `scripting` and `browser` are **off by default**.

### 6.2 `observe` (all `readOnlyHint: true`)
| Tool | Purpose | Key params |
|---|---|---|
| `snapshot` | UI state with refs | `app?`, `window_id?`, `mode: interactive\|full\|text`, `max_elements`, `offset`, `include_screenshot`, `annotate` (set-of-mark), `include_menus` |
| `screenshot` | Display/window/region capture; returns image + `screenshot_id` + scale/origin | `target`, `display_id?`, `window_id?`, `rect?`, `max_edge` (1280), `format` |
| `find` | Fuzzy element search | `text`, `role?`, `identifier?`, `app?`, `limit` |
| `inspect` | Full attributes, actions, ancestry of a ref | `ref` |
| `ocr` | Vision text recognition with boxes | `window_id?`, `rect?`, `languages?` |
| `list_apps` | Running (and optionally installed) apps | `include_installed` |
| `list_windows` | Windows: id, app, title, frame, state | `app?` |
| `list_displays` | Displays, frames, scale, work areas | — |
| `wait_for` | Wait for a condition (≤ 60 s, with progress) | `condition: element\|element_gone\|window\|app_running\|text_visible\|idle\|duration`, `query`, `timeout_ms` |
| `status` | Version, permissions, launcher check, paused state, profile, clients | — |

### 6.3 `input`
| Tool | Purpose |
|---|---|
| `click` | Click a `ref` or a point; `button`, `count`, `modifiers` (e.g. `["cmd"]` for multi-select), `strategy`, `return` |
| `type_text` | Type into the focused element or a `ref`; `clear`, `submit`, `strategy` |
| `press_keys` | `"cmd+shift+t"` or a sequence like `["cmd+a","delete"]`; `repeat` |
| `scroll` | A ref or point; lines/pages; `scroll_into_view: ref` |
| `drag` | ref/point → ref/point; `duration`, `modifiers` |
| `move_mouse` | Hover |
| `set_value` | Slider, checkbox, popup, or text value through AX |
| `perform_action` | Raw AX action (`AXShowMenu`, `AXIncrement`, `AXConfirm`, `AXRaise`, …) |
| `select_menu` | List or select menu-bar items (and status-bar extras) by path |
| `fill_form` | Set many fields at once, `[{ref, value}]` |

Coordinates accept `coordinate_space: points | screenshot` (+ `screenshot_id`).

### 6.4 `apps`
`app` (`launch|activate|hide|quit|force_quit`; `force_quit` is destructive), `window` (`focus|move|resize|minimize|restore|zoom|fullscreen|close`), `open` (URL or file, `with_app`, `reveal`).

### 6.5 `system`
`clipboard` (text/image/files; reads audited), `notify`, `spotlight_search` (mdfind), `system_info`, `system_control` (volume, appearance), `processes` (list; kill is destructive).

### 6.6 Opt-in toolsets (all ship in 1.0)
- **`scripting`** (off by default, `destructiveHint`, confirmation by default): `shell` (`zsh -lc`, cwd, timeout ≤ 120 s), `applescript` (AppleScript/JXA), `shortcuts` (list is read-only / run Apple Shortcuts), `defaults` (read/write preferences; writes destructive).
- **`browser`** (off by default): `browser_tabs` (Safari/Chrome/Arc/Edge/Brave via AppleScript: list/activate/open/close), `browser_read` (page text through the AX web area), `browser_eval_js` (needs "Allow JavaScript from Apple Events"; confirmation by default).
- **`computer` profile**: one `computer` tool with the common computer-use actions (`screenshot`, `left_click`, `right_click`, `double_click`, `mouse_move`, `left_click_drag`, `type`, `key`, `scroll`, `wait`, `cursor_position`) in screenshot-pixel space. This lets existing vision-first loops plug in without prompt changes.

### 6.7 Windows-MCP parity map
| Windows-MCP | AXPilot (default naming) |
|---|---|
| Click / Type / Scroll / Move / Shortcut | `click` / `type_text` / `scroll` / `move_mouse`+`drag` / `press_keys` |
| Wait / WaitFor | `wait_for` |
| MultiSelect / MultiEdit | `click` + `modifiers:["cmd"]` / `fill_form` |
| Screenshot / Snapshot / DisplayInventory | `screenshot` / `snapshot` / `list_displays` |
| Scrape | `browser_read` (+ client's web fetch) |
| App | `app`, `window`, `open` |
| PowerShell | `shell`, `applescript` |
| FileSystem | — (official Filesystem MCP) + `spotlight_search`, `open` |
| Registry | `defaults` |
| Process / Clipboard / Notification / ControlStatus | `processes` / `clipboard` / `notify` / `status` |
| *(Mac-only)* | `select_menu`, `set_value`, `perform_action`, `ocr`, `shortcuts`, `find`, `inspect` |

### 6.8 Windows-MCP naming profile
- `[tools] naming = "windows-mcp"` (or `--naming windows-mcp`) exposes the tools under **Windows-MCP's PascalCase names and parameter shapes** (`Click`, `Type`, `Scroll`, `Move`, `Shortcut`, `Wait`, `WaitFor`, `MultiSelect`, `MultiEdit`, `Screenshot`, `Snapshot`, `DisplayInventory`, `App`, `Process`, `Clipboard`, `Notification`, `ControlStatus`, `Scrape`), mapped onto our implementations.
- Platform-specific tools are renamed honestly: `PowerShell` → `Shell`, and `Registry` → `Defaults`. Mac-only tools keep PascalCase forms (`SelectMenu`, `OCR`, …).
- It is **a profile, not duplicates.** Each server exposes one naming scheme, because duplicate tools confuse models and waste context.
- Parameter mapping is verified against Windows-MCP's current schemas in Phase 4, with a contract test per mapped tool.

### 6.9 Navigation: getting around any app
Agents need to *roam* through an app, not just click what's on screen. These tools are part of `observe` (and in the `standard` profile):

| Tool | What it does |
|---|---|
| `explore` | A map of an app in one call: its windows, menu bar (top-level menus with item counts), toolbar buttons, tabs, sidebar/outline sections, focused element, scrollable areas, plus the app's **compatibility profile** (§6.10). This is the first call for an unfamiliar app. |
| `find` with `scroll: true` | Search for an element; if it isn't visible, scroll the nearest (or a given) scroll area page by page until it appears, the end is reached, or `max_scrolls` runs out. Lazily loaded lists, long settings panes and feeds work. |
| `scroll` | Scroll a specific scroll area `ref` by lines/pages, `to: top\|bottom`, or `scroll_into_view: ref`. Reports whether the area moved and whether it hit the end. |
| `read` | Read the full content of a document, web page, text view, list or table by scrolling through it, de-duplicating overlap between pages, and returning text (or rows) with `complete: true/false`. Suitable for long chats, documents, logs and search results. |

All of them return what changed, so the agent knows whether it is making progress (no infinite scrolling loops: the result says `at_end: true`).

### 6.10 App compatibility: "works with almost any Mac app"
Mac apps expose very different accessibility trees. AXPilot detects the **app kind** and adapts. Every `snapshot`/`explore` reports `app_kind`, `ax_quality` (`good | partial | poor`) and the strategy in use.

| App kind (detected from the bundle) | Examples | Strategy |
|---|---|---|
| Native AppKit / SwiftUI | Finder, Notes, Mail, Xcode, System Settings | Full AX tree; AX actions first |
| Mac Catalyst / iPad apps on Apple silicon | Podcasts-style Catalyst apps, iOS apps | AX tree (UIKit bridge); coordinate fallback for gestures |
| Electron / Chromium Embedded Framework | Slack, VS Code, Discord, Notion, Spotify | Turn on `AXManualAccessibility` / `AXEnhancedUserInterface` to expose the DOM-backed tree; web-area traversal |
| Chromium browsers / Safari / Firefox | Chrome, Arc, Edge, Brave, Safari, Firefox | AX web areas, plus the `browser` toolset (tabs, page text, JS when allowed) |
| Java (Swing/JavaFX) / Qt | JetBrains IDEs, some enterprise apps, Qt tools | Their AX bridges (often partial); a quality check decides whether to add vision |
| Microsoft Office / Adobe / custom-drawn UIs | Word, Excel, Photoshop | Larger budgets and menus first; OCR fallback for canvases |
| Games / canvas / remote desktops | Games, VNC, Figma canvas | **Vision mode**: screenshot + OCR text boxes become clickable pseudo-elements (`o1…`); coordinates as the last resort |

- `snapshot(mode="auto")` (the default) adds OCR-derived elements automatically when `ax_quality` is `poor` and Screen Recording is granted. `mode="vision"` forces it.
- A community **app-compatibility table** (docs) records tested apps, the kind detected, what works, and known gaps. `app-compat` issues feed it.

### 6.11 Automation scripts: SDK and CLI
AXPilot is also an automation library, not only an MCP server. Scripts use **the same engine, policy, audit and error codes** as agents.

```python
from axpilot import Desktop

with Desktop.connect() as mac:            # via AXPilot.app (its permissions), or Desktop.direct()
    notes = mac.app("Notes").activate()
    mac.menu("File", "New Note")
    mac.type_text("Groceries\nMilk, eggs")
    for row in mac.read(app="Notes", target="list").rows:
        print(row)
    mac.find("Share", scroll=True).click()
```
- `Desktop` (sync) and `AsyncDesktop` (async) return typed result objects and raise `AXPilotError` with the same codes.
- `Desktop.fake()` uses the simulated desktop, so automation scripts can be unit-tested in CI without a Mac.
- CLI for shell scripts and other languages: `axpilot tools` (list tools and schemas) and `axpilot call <tool> --arg key=value | --json '{...}'` (prints the result as JSON; exit codes per QUALITY.md A9).
- Later (post-1.0): record and replay of action sequences as scripts.

---

## 7. Security and safety model

### 7.1 Threat model
1. **Prompt injection from screen content.** Limit the blast radius (gating, confirmations, deny-lists), and warn in `instructions` and docs that on-screen text is untrusted.
2. **Confused deputy / local TCC bypass.** Other local processes could use our grants through the socket or port. Mitigation is **client pairing**: on the first connection from a new client (identified best-effort by the bridge's parent app bundle, e.g. "Claude", "Cursor", "Terminal → python"), the host asks *"Allow «Claude» to control this Mac?"* (Once / Always / Deny). The socket is `0600` with same-uid checks, and the HTTP token lives in the Keychain. Pairing raises the bar but is **not** a hard boundary against same-user malware, and the docs say so.
3. **Runaway agent.** Rate limits, kill switch, yield-to-user, confirmations, Trash rather than delete.
4. **Data exfiltration.** Secure fields are never read, deny-listed apps are excluded from capture, and clipboard reads are audited and can be disabled.
5. **Network exposure.** Bind to `127.0.0.1`, require a token, check Origin and Host, no CORS. Remote access needs `--allow-remote` + TLS; we recommend SSH or Tailscale instead.
6. **Supply chain and launcher integrity.** PyPI **trusted publishing** with attestations, pinned and locked deps (`uv.lock`), Dependabot, CodeQL (Python), and a launcher cdhash published per version and verified by `doctor`. Because the ad-hoc launcher has **no signing key**, there is nothing to leak.

### 7.2 Defaults
| Control | Default |
|---|---|
| Profile | `standard`; `scripting` and `browser` off |
| Confirmations | `destructive` (shell, AppleScript, JS eval, force quit, `defaults` write, process kill) |
| Kill switch | **⌃⌥⌘.** + menu-bar Stop (cancels in-flight work and pauses) |
| Yield to user | On |
| Indicator | Edge glow + click ripples + "Agent is controlling" pill, excluded from our own screenshots |
| App deny-list | Keychain Access, password managers, the Privacy & Security pane in System Settings |
| Terminal rule | Typing into Terminal, iTerm2, Ghostty, Warp, and similar apps is **blocked unless the `shell` tool (in the `scripting` toolset) is enabled**, because it is equivalent to shell access |
| Secure fields | Values never returned; typing refused unless `allow_secure_fields` |
| Rate limit | 120 input actions/min |
| Audit log | `~/Library/Logs/AXPilot/audit.jsonl` (redacted args) |
| Telemetry | **None** |

---

## 8. Installation and client integration

### 8.1 Install channels
| Channel | Command | Notes |
|---|---|---|
| **uv (recommended)** | `uv tool install axpilot` then `axpilot setup` | Persistent install. uv fetches Python if needed |
| pipx | `pipx install axpilot` then `axpilot setup` | |
| One-liner | `curl -LsSf https://vansh3503.github.io/axpilot/install.sh \| sh` | Installs uv if missing, then does the above. The script is short and readable, and we document the manual path too |
| uvx (try it) | `uvx axpilot stdio` | Works because `setup` creates a dedicated runtime (below), so ephemeral uvx caches don't matter |
| MCP Registry | `io.github.vansh3503/axpilot` (`server.json`, PyPI package) | The PyPI README carries the registry ownership marker |
| `.mcpb` (Claude Desktop) | One-click bundle | **Verify in Phase 3** how MCPB runs a uv/Python server and handles the launcher. Ships only if the flow stays reliable |
| Homebrew tap (formula) | `brew install vansh3503/tap/axpilot` | A formula, not a cask, so it is unaffected by the cask notarization policy. **Verify** that brew doesn't modify or re-sign the launcher in the keg |
| Source | `git clone` → `uv sync` → `uv run axpilot stdio --direct` | Contributors |

### 8.2 What `axpilot setup` does
1. Checks macOS ≥ 14 and the architecture.
2. Creates a **dedicated runtime** at `~/Library/Application Support/AXPilot/runtime/` (a uv venv with a pinned `axpilot`), so the host never depends on ephemeral uvx caches or project venvs.
3. Copies the **launcher** from the package to `~/Applications/AXPilot.app` (byte-identical, so the cdhash is verified), and writes `runtime.json`.
4. Optionally registers a login item.
5. Opens **onboarding**: Accessibility (required; deep-links the exact System Settings pane and shows live status), Screen Recording (optional; explains the monthly re-prompt), Automation (on demand).
6. Offers **"Connect a client"** for the detected clients.

`axpilot update` upgrades the runtime. Because the launcher is untouched, no re-grant is needed.

### 8.3 CLI
```
axpilot stdio [--direct] [--profile …] [--naming default|windows-mcp]   # what MCP clients run
axpilot http  [--port 7788] [--allow-remote --tls-cert … --tls-key …]
axpilot setup | update | uninstall
axpilot connect <client> [--dry-run] [--scope user|project]   # writes client config (with backup)
axpilot connect --list
axpilot doctor          # OS, permissions, launcher cdhash, runtime, socket, clients, versions
axpilot config get|set|edit|path
axpilot skill install [--target claude|codex|gemini|cursor|dir]
axpilot debug dump-tree --app TextEdit > fixture.json
```

### 8.4 Clients for `axpilot connect` (v1 target)
Claude Desktop, Claude Code, Cursor, VS Code (Copilot), Windsurf, Zed, OpenAI Codex CLI, Gemini CLI, LM Studio, Goose, Cline, Continue. Config paths and keys are verified per client in Phase 3.

Generic config:
```json
{ "mcpServers": { "axpilot": { "command": "axpilot", "args": ["stdio"] } } }
```
Claude Code: `claude mcp add axpilot -- axpilot stdio`

---

## 9. Agent framework integration

Every framework connects through stdio (`axpilot stdio`) or Streamable HTTP (`http://127.0.0.1:7788/mcp` + bearer token). The repo ships a **tested example per framework** in `examples/`, run by a CI smoke test (list tools + `status`) against the `FakeEngine` on Linux. Framework versions are pinned per example and re-verified each release.

| Framework | Lang | Integration (verify the API at implementation time) |
|---|---|---|
| OpenAI Agents SDK | Py/TS | `MCPServerStdio` / `MCPServerStreamableHttp` |
| LangChain / LangGraph | Py/TS | `langchain-mcp-adapters` `MultiServerMCPClient` |
| CrewAI | Py | `MCPServerAdapter` (crewai-tools) |
| Google ADK | Py | `McpToolset` |
| Microsoft Agent Framework | Py/.NET | `MCPStdioTool` |
| Pydantic AI | Py | `MCPServerStdio` |
| LlamaIndex | Py | `BasicMCPClient` + `McpToolSpec` |
| smolagents | Py | `MCPClient` / `ToolCollection.from_mcp` |
| Claude Agent SDK | Py/TS | `mcp_servers` option |
| Agno / Strands | Py | `MCPTools` / `MCPClient` |
| Vercel AI SDK / Mastra | TS | AI SDK MCP client / `@mastra/mcp` |
| Local models (Ollama, LM Studio) | — | Any framework above, or LM Studio's native MCP; use `--profile minimal` |

```python
# OpenAI Agents SDK
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async with MCPServerStdio(params={"command": "axpilot", "args": ["stdio"]},
                          cache_tools_list=True) as mac:
    agent = Agent(name="Mac operator",
                  instructions="Use snapshot before acting; prefer element refs.",
                  mcp_servers=[mac])
    result = await Runner.run(agent, "Open Notes and create a note titled 'Groceries'")
```
```python
# LangChain / LangGraph
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent

client = MultiServerMCPClient({"axpilot": {"command": "axpilot", "args": ["stdio"], "transport": "stdio"}})
agent = create_agent("<your-model>", await client.get_tools())
```

Helpers for any client or framework: the server `instructions` guide; an **Agent Skill** (`skills/axpilot/SKILL.md`, open Agent Skills format) covering the observe → act → verify loop, refs vs menus vs coordinates, and recovery; the **`computer` profile**; the **`windows-mcp` naming profile**; and **stable tool names and schemas under SemVer**.

---

## 10. Configuration

Precedence: CLI flags > `AXPILOT_*` env vars > `~/.config/axpilot/config.toml` > defaults. The file is also editable from the menu-bar Settings.

```toml
[server]
http_enabled = false
http_host = "127.0.0.1"
http_port = 7788          # token generated and stored in the Keychain

[tools]
profile = "standard"      # minimal | standard | full | computer
naming  = "default"       # default | windows-mcp
enable  = []              # e.g. ["scripting", "browser"]
disable = []

[safety]
confirm = "destructive"   # never | destructive | always
yield_to_user = true
indicator = true
deny_apps = ["com.apple.keychainaccess", "com.1password.1password"]
allow_apps = []           # non-empty → only these apps can be controlled
allow_secure_fields = false
max_actions_per_minute = 120

[capture]
max_edge = 1280
format = "jpeg"
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
axpilot/
├─ pyproject.toml  uv.lock  .python-version
├─ src/axpilot/
│  ├─ cli.py  config.py  errors.py  permissions.py  __main__.py
│  ├─ server/{app.py, handles.py, format.py, naming/{default.py,windows_mcp.py},
│  │          tools/{observe,input,apps,system,scripting,browser,computer}.py}
│  ├─ engine/{base.py, fake.py, ax/{element,snapshot,prune,refs,diff,observer}.py,
│  │          input/{mouse,keyboard,layout,strategies}.py, capture/{sck,annotate}.py,
│  │          vision/ocr.py, apps.py, windows.py, displays.py, menus.py,
│  │          system/{clipboard,spotlight,shortcuts,osascript,shell,defaults,processes}.py,
│  │          browser/{tabs,read,js}.py}
│  ├─ policy/{profiles,rules,hittest,confirm,pairing,ratelimit,audit}.py
│  ├─ host/{daemon,socket_server,menubar,overlay,hotkey,onboarding,settings}.py
│  ├─ bridge/{stdio_pipe,launch}.py
│  ├─ clients/{claude_desktop,claude_code,cursor,vscode,windsurf,zed,codex,gemini,lmstudio,goose,cline,continue_}.py
│  └─ _launcher/                    # populated at build time with the frozen AXPilot.app (verified by checksum)
├─ launcher/{main.swift|main.c, Info.plist, build.sh, CDHASH}   # built once per launcher version
├─ testbench/                       # PyObjC/AppKit fixture app with every control type
├─ tests/{unit,contract,engine,e2e,security}/  tests/fixtures/ax-trees/*.json
├─ skills/axpilot/SKILL.md
├─ examples/{python,ts}/            # one per framework, CI smoke-tested
├─ evals/{tasks/*.yaml, runner/, results/}
├─ packaging/{server.json, mcpb/, homebrew/, install.sh}
├─ docs/  (MkDocs Material → GitHub Pages; adr/; PLAN.md)
├─ .github/{workflows,ISSUE_TEMPLATE}/  PULL_REQUEST_TEMPLATE.md
├─ README.md  CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md  CHANGELOG.md  AGENTS.md
└─ LICENSE (Apache-2.0)
```
Tooling: `uv`, `ruff` (lint + format), `pyright`, `pytest` (+ `anyio`, `hypothesis` for argument fuzzing, `syrupy` for golden files), `pre-commit`.

---

## 12. Testing, QA and evals

| Layer | What | Runs on |
|---|---|---|
| Unit | Key parser, layout mapping, pruning, diff, refs, policy, config, naming profiles | **Linux** + macOS |
| Perception (offline) | Snapshot formatting over **recorded AX-tree fixtures** (`debug dump-tree`), token budgets | **Linux** |
| MCP contract | `tools/list` **golden files** for each profile and naming scheme (catches breaking changes), every tool against `FakeEngine`, error codes, both transports, protocol-version negotiation, MCP Inspector CLI | **Linux** |
| Framework examples | Each `examples/` script lists tools + calls `status` against `FakeEngine` | **Linux** |
| Engine | Real AX, CGEvent, and ScreenCaptureKit calls against TestBench in **direct mode** (Accessibility granted to the runner's Python via `xa11y/setup-a11y` or the per-user TCC.db; spike S5) | macOS 14/15/26 runners |
| Launcher mode | Launcher → host → bridge path; grant inheritance; `doctor` | Manual or self-hosted per release (CI grants for the launcher are part of spike S5) |
| Real-app smoke | Finder, TextEdit, Notes, Safari, Chrome, System Settings, Calculator, an Electron app | Nightly self-hosted or a pre-release checklist |
| Security | Policy bypass attempts (coordinate click onto a deny-listed app, typing into Terminal, secure field, unpaired client), HTTP auth/Origin/Host, fuzzing | Linux + macOS |
| Clean install | Every channel on a **fresh macOS VM** (Tart/UTM), timed against the 2-minute goal; check for no quarantine and no Gatekeeper prompt | Pre-release |
| Evals | ~30 TOML tasks (goal → state-based checks through read-only tools, plus a reference solution); `python -m axpilot.evals` runs an agent (scripted, or model-backed via `module:factory`) on a fresh simulated desktop per task and reports success, steps, tokens and time (36 tasks done; see docs/evals.md). A macOSWorld subset is optional | CI (scripted agent); model baselines manual, published per release |
| Performance | Snapshot p50/p95, action latency, tokens per snapshot | CI trend |

**v1 targets (calibrated in Phase 1):** snapshot p50 < 400 ms on typical windows (a Python budget; S2 may tighten it); a default snapshot < ~2.5k tokens; first action within 2 minutes of install.

---

## 13. CI/CD and release engineering

- **`ci.yml`** (PRs): ruff → pyright → Linux tests (unit, perception, contract, examples, security) → macOS engine tests (14/15/26) → CodeQL.
- **`launcher.yml`** (manual, rare): build the universal2 launcher → ad-hoc sign → record its **cdhash** → publish a `launcher-vN` release asset with SHA-256 + build-provenance attestation. Changing the launcher is a deliberate, documented event because it forces a one-time re-grant.
- **`release.yml`** (tag `v*`, release-please + Conventional Commits):
  1. Fetch the pinned `launcher-vN` asset and verify its checksum and cdhash
  2. Build the wheel (`macosx_14_0_universal2` tag, because it embeds the launcher) + sdist
  3. Publish to PyPI via **trusted publishing** (OIDC, attestations)
  4. Create a GitHub Release
  5. Publish to the MCP Registry (`mcp-publisher`, GitHub OIDC)
  6. Bump the Homebrew tap formula, then deploy the docs
- **Versioning:** SemVer. Tool names and schemas are public API, enforced by the golden files. The launcher has its own version line.

---

## 14. Open-source project setup
- **README**: pitch, demo GIF, 3-command quickstart, client matrix, safety summary, links.
- **Docs site**: install, permissions explained (with screenshots), each client, each framework, tools reference generated from schemas, config, security model, troubleshooting (`doctor`), FAQ, `llms.txt`.
- **CONTRIBUTING**: `uv sync`; direct mode for development (grant Accessibility to your terminal); recording AX fixtures; adding a tool (checklist: schema, annotations, policy, tests, golden file, docs); `tccutil reset Accessibility io.github.vansh3503.axpilot`.
- **Community**: Contributor Covenant, SECURITY.md (GitHub private advisories), issue templates (a bug template asks for `axpilot doctor` + an AX dump), labels (`good first issue`, `app-compat`, `tool-request`), Discussions, a public roadmap, and an app-compatibility table.
- **Funding**: GitHub Sponsors / Open Collective, with the first goal being the Apple Developer Program for tier-3 signing.
- **Launch**: MCP Registry, Glama, PulseMCP, Smithery, awesome-mcp-servers, Show HN, r/macapps, r/LocalLLaMA, and MCP community channels.

---

## 15. Roadmap and milestones (one maintainer + AI assistance)

### Phase 0: Foundations and spikes (weeks 1–2)
- Reserve the name (PyPI `axpilot`, npm `axpilot` defensively, repo rename). Scaffold (pyproject, src layout, ruff/pyright/pytest, CI skeleton, docs skeleton, community files). The `.gitignore` is already Python, so add macOS entries (`.DS_Store`, `*.app` build outputs).
- **Spikes (timeboxed, each ends in an ADR):**
  - **S1 Launcher and permissions (critical, first):** does a child of an ad-hoc-signed `AXPilot.app` inherit its Accessibility and Screen Recording grants on macOS 14/15/26/27? Does the grant survive Python and runtime upgrades? Is there no quarantine or Gatekeeper prompt when the app is installed from a wheel via uv or pipx? Does it work when the bridge is spawned by Claude Desktop, Claude Code, Cursor, and VS Code? **If not**, switch to fallback (a) or (b) from ADR-2.
  - **S2 AX performance in Python:** snapshot budgets on heavy apps (Xcode, Chrome, Slack/Electron, a large Finder folder, System Settings); batching; the Chrome/Electron flags.
  - **S3 Input:** `AXPress`/`AXSetValue` vs CGEvent vs `CGEventPostToPid` across 15 apps; HID-idle detection for yield-to-user; layout mapping through ctypes.
  - **S4 Capture:** ScreenCaptureKit through PyObjC (latency, exclusion filters), and the monthly prompt on 15/26/27.
  - **S5 CI permissions:** granting Accessibility on GitHub macOS runners (to Python, and ideally to the launcher).
  - **S6 MCP:** `mcp` v2 `MCPServer` with stdio + Streamable HTTP over a Unix-socket bridge, `structuredContent`, annotations, and interop with Claude Desktop/Code, Cursor, VS Code, OpenAI Agents SDK, and LangChain adapters.
- **Exit:** ADRs merged; go/no-go on the launcher architecture.

### Phase 1: Engine and MVP tools in direct mode (weeks 3–5) → `v0.1.0a1`
- Engine: AX snapshot, prune, refs, diff; layout-aware input; apps, windows, displays; screenshot.
- Tools: `minimal` + `find`, `inspect`, `select_menu`, `wait_for`, `list_*`, `status`.
- `axpilot stdio --direct`, `doctor`, `debug dump-tree`; FakeEngine + fixtures; unit, perception, and contract tests; TestBench v1.
- **Exit:** Claude Code and Claude Desktop complete 10 scripted tasks (TestBench, TextEdit, Notes, Finder) in direct mode.

### Phase 2: Launcher, host app, safety (weeks 6–7) → `v0.2.0b1`
- The launcher (`launcher-v1`); host with socket server, bridge, menu bar, onboarding, overlay, kill switch, yield-to-user, activity log, settings; `setup`, `update`.
- Policy: profiles, toolsets, naming profiles; allow/deny + hit-testing; secure-field and Terminal rules; confirmations (native + elicitation); pairing; rate limit; audit.
- Streamable HTTP (localhost + Keychain token + Origin/Host checks).
- **Exit:** the security suite is green, and **one grant works across ≥ 4 clients and survives an `axpilot update`**.

### Phase 3: Distribution and one-command integration (weeks 8–9) → `v0.3.0` **public preview**
- PyPI trusted publishing, `install.sh`, `connect` for 12 clients, MCP Registry, the Homebrew tap formula (after verification), the `.mcpb` feasibility check, the docs site, README, and a demo video.
- **Exit:** on a fresh VM, uv/pipx/install.sh each go from install to first action in **< 2 minutes** with no Gatekeeper prompt.

### Phase 4: Full toolsets, OCR, frameworks, evals (weeks 10–11) → `v0.9.0rc1`
- `system`, `scripting`, **`browser`**, and the **`computer` profile**; Vision OCR; `fill_form`; the **`windows-mcp` naming profile** with contract tests.
- `examples/` for 12+ frameworks; the Agent Skill.
- An eval suite of ~30 tasks with a baseline for 2–3 models; performance tuning; non-US keyboard layouts and non-English UIs.
- **Exit:** all examples green; eval baseline published.

### Phase 5: Hardening and 1.0 (week 12) → `v1.0.0`
- Threat-model review, fuzzing, macOS 27 validation, docs polish, and freezing the 1.x tool API. Launch.

### Post-1.0 backlog
PIM toolsets (EventKit Calendar/Reminders, Contacts) through the launcher's usage strings · tier-3 signing (Developer ID) when funded, enabling DMG/cask · the Tasks extension and OpenTelemetry · per-client control lease · remote access with OAuth · macros/recording exportable as Agent Skills or Shortcuts · deeper App Intents/Shortcuts integration · per-app adapters (Finder, Mail, Xcode) · a plugin API for community toolsets · an optional native accelerator for AX traversal.

---

## 16. Risks and mitigations

| Risk | L / I | Mitigation |
|---|---|---|
| **Child-process grant inheritance doesn't work** (or changes in a macOS update) | Med / **High** | Spike S1 first. Fallbacks: (a) an embedded-Python app bundle, (b) a native helper. Test the macOS beta every June |
| pip/uv set quarantine on some setups, triggering Gatekeeper | Low / High | Verify in S1. `setup` checks the xattr and explains "Open Anyway" if present. Tier-3 signing later |
| Launcher changes force re-grants | Low / Med | Keep it minimal and rarely changed; `doctor` detects and guides; version it separately |
| Python AX traversal too slow | Med / Med | Batching, pruning, scoping, and caching; an optional native accelerator later |
| AppKit main loop + asyncio integration bugs | Med / Med | A clear thread model (§5.3) and an early prototype in S6 |
| Inconsistent app accessibility | High / Med | Chrome/Electron flags, OCR/screenshot fallback, a community app-compat list |
| Monthly Screen Recording re-prompt | High / Low | AX-first design, so Screen Recording is optional |
| Prompt injection | Med / High | Safe defaults, confirmations, deny-lists, kill switch, audit |
| Overlap with MacOS-MCP and Peekaboo | High / Med | Differentiate (§2); collaborate where sensible |
| CI can't grant Accessibility | Med / Med | Linux-heavy test pyramid with FakeEngine; self-hosted Mac mini for E2E |
| Maintainer bandwidth | Med / Med | Tight scope, a strong contributor guide, good-first-issues |

---

## 17. Remaining open questions
1. Confirm the name **AXPilot** (or pick Pomme or Dexter), and whether to rename the GitHub repo now.
2. Personal namespace (`io.github.vansh3503`) or a new GitHub org (better for a community project; an org can be added later with redirects).
3. Is it OK to commit to **no Homebrew cask and no DMG until funded** (package-manager installs only in v1)?

---

## 18. Sources
- Windows-MCP: https://github.com/CursorTouch/Windows-MCP
- MacOS-MCP (CursorTouch): https://github.com/CursorTouch/MacOS-MCP
- Peekaboo: https://github.com/steipete/Peekaboo
- mcp-server-macos-use: https://github.com/mediar-ai/mcp-server-macos-use
- cua-driver: https://github.com/trycua/cua/tree/main/libs/cua-driver
- MCP spec 2026-07-28 changelog: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx
- MCP Python SDK releases: https://github.com/modelcontextprotocol/python-sdk/releases
- MCPB: https://claude.com/docs/connectors/building/mcpb
- TCC responsible-process reports: https://github.com/anthropics/claude-code/issues/36832 · https://claudeissues.com/issue/63032-claude-desktops-info-plist-missing-tcc-usage-strings-blocks-all-eventkit-based-m
- Ad-hoc cdhash pinning vs certificate-based designated requirement: https://git.bdeshi.space/bdeshi/shannoncoat/src/branch/main/.env.example · yabai self-signed cert guide: https://github.com/koekeishiya/yabai/wiki/Installing-yabai-(from-HEAD)
- Quarantine is set by the downloading app (curl doesn't): https://unit42.paloaltonetworks.com/gatekeeper-bypass-macos
- Apple Developer fee waiver (orgs only): https://www.imore.com/apple-developer-program-fee-waivers-now-available-heres-how-apply
- Screen Recording monthly prompt: https://9to5mac.com/2024/08/14/macos-sequoia-screen-recording-prompt-monthly/
- Homebrew Gatekeeper policy: https://workbrew.com/blog/homebrew-5-0-0
- macOS 27 Apple-silicon-only: https://appleinsider.com/articles/26/06/08/macos-27-compatibility-list-focuses-entirely-on-apple-silicon
- WWDC26 App Intents: https://developer.apple.com/videos/play/wwdc2026/345/
- CI Accessibility grants: https://github.com/xa11y/setup-a11y
- macOSWorld: https://arxiv.org/abs/2506.04135v2
- Agent Skills: https://agentskills.io/
