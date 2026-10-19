# AXPilot: handoff for the next working session

Give this whole file to the next coding session (any account). It is self-contained: what the project is,
every decision made so far, the exact current state, the rules the owner set, and the ordered remaining work.

- **Repository:** `github.com/Vansh3503/Mac-Mcp` (private). Default branch `main`.
- **Working branch:** `feature/axpilot-foundation` (all work is here, not merged).
- **Last commit at handoff:** see `git log -1`. The most recent work commit is `99b08da` (`wip(server)`), which is
  intentionally incomplete (details below).
- **Owner:** Vansh Malhotra (GitHub `Vansh3503`).

---

## 1. What we are building

**AXPilot**, an open-source **MCP server for macOS**, the Mac counterpart of
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP). It lets **any AI agent or agentic framework** (Claude
Desktop/Code, Cursor, VS Code, Codex, Gemini CLI, LM Studio; OpenAI Agents SDK, LangGraph, CrewAI, Google ADK,
Pydantic AI, LlamaIndex, smolagents, MS Agent Framework, Vercel AI SDK, Mastra…) **see and operate almost any Mac
application**: read the UI, click, type, scroll, use toggles, sliders, pop-ups, tabs, tables, menus, dialogs, and
read long content.

**Positioning (owner's explicit instruction):** the product is the **MCP server for agents**. Automation scripts
(the Python `Desktop` SDK and the `axpilot call` CLI) are a secondary, supporting use case, not the headline.

**Quality bar (owner's explicit instruction):** production-grade, open-source quality. Proper error handling and
thorough testing (see `docs/dev/QUALITY.md`, which is mandatory). Cover the edge cases of real Mac apps and of
real agent frameworks.

## 2. Documents to read first (in the repo)

1. `docs/PLAN.md`: product plan, research, architecture, the full tool surface (§6, including §6.9 navigation,
   §6.10 app compatibility, §6.11 SDK/CLI), safety model (§7), install (§8), frameworks (§9), roadmap (§15).
2. `docs/dev/CONTRACTS.md`: exact module APIs (§1–§11). Code must match. Update it in the same change if a
   contract changes.
3. `docs/dev/QUALITY.md`: **mandatory** error-handling (A1–A9) and testing (B1–B8) rules.
4. `AGENTS.md` (and `CLAUDE.md`): rules for coding agents working on this repo.
5. `docs/dev/handoff/build2-task-specs.md`: the detailed specs for the unfinished build round 2.
6. `docs/dev/handoff/build1-followups.md`: deviations, contract issues and Mac-verification follow-ups reported by
   build round 1.

## 3. Decisions already made (don't reopen without the owner)

| # | Topic | Decision |
|---|---|---|
| 1 | Language | **Python 3.11+**, MCP Python SDK v2 (`mcp>=2.3,<3`, `MCPServer`), **PyObjC** for macOS APIs. |
| 2 | Signing (no $99 Apple account) | A **tiny frozen Swift launcher `AXPilot.app`**, ad-hoc signed, posix_spawns the Python host as its child so macOS TCC attributes permissions to the launcher (grant once; survives Python updates because the launcher's cdhash never changes). Distribute only via package managers (pip/uv/pipx), which don't set quarantine. Developer ID later if funded. **Spike S1 must verify this on real macOS.** |
| 3 | Name | **AXPilot**: package/CLI `axpilot`, app `AXPilot.app`, bundle id `io.github.vansh3503.axpilot`, env prefix `AXPILOT_`, MCP Registry `io.github.vansh3503/axpilot`. Repo rename `Mac-Mcp` → `axpilot` is the owner's call (not done). |
| 4 | Minimum macOS | **14 Sonoma**, Apple silicon + Intel; test 14/15/26/27. |
| 5 | `browser` + `computer` toolsets | **Ship in 1.0** (browser off by default; computer is an opt-in profile). |
| 6 | Windows-MCP names | **Yes**, as a `naming = "windows-mcp"` profile (PascalCase names and parameter shapes), not duplicate tools. |
| 7 | Telemetry | **None.** |
| 8 | Positioning | MCP for agents first; SDK/CLI scripting secondary. |

## 4. Owner's working rules (must follow)

- **Commits and branch names must not mention Claude or Anthropic.** No `Co-Authored-By`/session trailers, no
  "claude/…" branches. Use neutral names (`feature/…`, `fix/…`).
- **Commit author:** `Vansh Malhotra <85389767+Vansh3503@users.noreply.github.com>` (set as repo-local git config;
  commit signing is disabled locally because the session's key would mark the owner's commits Unverified).
- Use Conventional Commits (`feat(server): …`, `fix(perception): …`, `wip(…)` only for interrupted work).
- Don't open pull requests unless the owner asks.
- Commit and push often (cloud containers are ephemeral).
- The old remote branch `claude/mac-mcp-plan` still exists and must be **deleted by the owner** in the GitHub UI
  (deletion was blocked from the session). It holds old commits with trailers; all its content is on
  `feature/axpilot-foundation` already.

## 5. Current state

### Done (committed on `feature/axpilot-foundation`)

| Area | Path | Notes |
|---|---|---|
| Plan, contracts, quality bar | `docs/PLAN.md`, `docs/dev/CONTRACTS.md`, `docs/dev/QUALITY.md` | |
| Foundation | `src/axpilot/{__init__,errors,models,paths,config}.py`, `engine/{__init__,base}.py` | `Engine` ABC, `ErrorCode`, config precedence flags > env > TOML > defaults |
| Perception | `src/axpilot/perception/*` | snapshots with refs, pruning, text/JSON formats, diffs, fuzzy find, stores, locator re-resolution, serialization — ~350 tests |
| Fake engine | `src/axpilot/engine/fake.py` + `engine/fixtures/demo_desktop.json` | simulated desktop for tests and `--engine fake` |
| Input + policy | `src/axpilot/input/*`, `src/axpilot/policy/*` | key parsing, profiles/toolsets, safety rules, rate limit, audit, confirmations, control state — ~420 tests |
| System integrations | `src/axpilot/system/*` | runner, osascript, shell, Spotlight, Shortcuts, defaults, processes, sysinfo, browsers — ~380 tests |
| macOS engine | `src/axpilot/engine/macos/*` | PyObjC implementation of every `Engine` method. **Never run on real macOS yet.** |
| Launcher + spikes | `launcher/*`, `spikes/*`, `docs/spikes/RESULTS_TEMPLATE.md` | Swift launcher not compiled yet; spike runbook in `spikes/README.md` |
| Clients, setup, doctor | `src/axpilot/clients/*`, `src/axpilot/setup/*`, `src/axpilot/doctor.py` | 12 client config writers, runtime venv + launcher install, diagnostics — ~400 tests |
| Docs, CI, community | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `AGENTS.md`, `docs/*`, `docs/adr/*`, `.github/*`, `mkdocs.yml`, `packaging/*`, `skills/axpilot/SKILL.md` | |

### Partial (commit `99b08da`, `wip(server)`), from an interrupted run

- `src/axpilot/server/{context,registry,guard,results,schemas,app,instructions}.py`,
  `src/axpilot/server/tools/observe.py` (≈3k lines, **no server tests yet**).
- `src/axpilot/testing/{faulty_engine,harness}.py` (fault-injection kit).
- `src/axpilot/perception/{appkind,vision,navigate}.py` (app-kind detection, OCR fallback, navigation helpers) and
  fixture additions in `demo_desktop.json`; `policy/profiles.py` gained `explore` and `read`.
- **Treat all of it as unreviewed.** Keep what is correct, finish the rest.

### Test status at handoff (Linux)

`uv run pytest -q` → **1954 passed, 48 skipped (macOS-only), 3 failed**:
- `tests/engine/test_fake.py::test_list_apps_and_frontmost`: fixture additions changed the app list. Fix the
  fixture additions, not the test.
- `tests/perception/test_appkind.py::test_the_bundle_wins_over_well_known_ids`: new, partial module.
- `tests/input/test_keys.py::test_sequence_string_round_trips`: a real parser/formatter bug found by hypothesis.

### GitHub issues already created

#1 Phase 0 epic (with spike sub-issues #2–#7: S1 launcher TCC go/no-go, S2 AX performance, S3 input strategies,
S4 ScreenCaptureKit, S5 CI permissions, S6 MCP interop), #8–#12 Phase 1–5 epics, #13 post-1.0 backlog. Their bodies
link to `docs/PLAN.md` on `feature/axpilot-foundation`.

## 6. Remaining work, in order

### Step 1: finish build round 2 (the MCP server itself)

Follow `docs/dev/handoff/build2-task-specs.md` exactly. Packages:
1. **Core:** `server-core` (finish/verify the partial server core + observe tools + `axpilot.testing`, write all
   server tests), `compat-vision` (finish app-kind/vision/navigation helpers; fix the fixture so existing tests pass).
2. **Tools** (parallelizable): `tools-input` (click, type_text, press_keys, drag, move_mouse, set_value,
   perform_action, select_menu, fill_form), `tools-apps-system` (app, window, open, clipboard, notify,
   spotlight_search, system_info, system_control, processes), `tools-scripting-browser-computer-naming` (shell,
   applescript, shortcuts, defaults, browser_tabs, browser_read, browser_eval_js, computer, windows-mcp naming),
   `tools-navigate` (find with scrolling, scroll, explore, read), `host-bridge-cli` (Unix-socket host, pairing,
   stdio bridge, Streamable HTTP with token + Origin/Host checks, the full Typer CLI incl. `tools` and `call`),
   `sdk-examples` (`Desktop`/`AsyncDesktop` SDK over the same tools, example scripts, framework examples).
3. **Integrate:** whole suite + ruff + pyright green (including the 3 failing tests), all tool modules imported
   unconditionally, `TOOL_ORDER` ↔ registry consistency, golden files `tests/server/golden/tools_<profile>_<naming>.json`,
   `scripts/gen_tool_docs.py` → `docs/tools.md` (+ freshness test), CLI smoke (`axpilot tools --json`,
   `axpilot call status --engine fake`, `axpilot call snapshot --engine fake --arg app=Notes`, `axpilot doctor`),
   coverage gate (`[tool.coverage.report] fail_under` ≥ 85, enforced in CI), CHANGELOG.

### Step 2: hardening round (owner's request: "cover all edge cases; any app; any agent framework")

**Controls, handled by their role** (likely via a role-aware `set_value`, a `select_option` tool, and a
`handle_dialog` tool; update PLAN/CONTRACTS/golden files accordingly):
- toggles/switches/checkboxes set to a desired state (idempotent, read state first; mixed state)
- sliders (clamp to AXMinValue/AXMaxValue) and steppers (AXIncrement/AXDecrement until the target)
- pop-up buttons, combo boxes, menu buttons, radio groups, segmented controls, tab groups: select an option by
  title/index
- disclosure triangles and outline rows: expand/collapse via AXDisclosing
- date pickers, color wells, text/search/secure fields (clear, select-all, multi-line, submit)
- tables: select rows, multi-select (cmd/shift), sort by header, edit cells
- context menus (AXShowMenu / right-click), sheets, alerts, popovers (default/cancel buttons)
- open/save panels (navigate with ⌘⇧G, type the path, confirm)
- toolbar overflow (») and menu-bar extras

**UI conditions to handle:**
- nested and horizontal scroll areas; virtualized/lazy lists
- off-screen elements (scroll into view before acting); disabled elements (clear reason)
- animations (settle); modal dialogs blocking the window; apps not responding (timeouts)
- multiple windows, tabs and Spaces; multi-display and Retina coordinates
- localized/RTL UIs, non-US keyboard layouts, Secure Event Input

**Agent-framework and MCP edge cases:**
- **Portable input schemas:** must pass OpenAI strict-mode and Gemini function-declaration limits. That means
  flat objects, no surprising `$ref`/`$defs`/`oneOf`, names matching `^[a-zA-Z0-9_-]{1,64}$`, and descriptions
  on every parameter. Add a schema-portability test over every tool.
- **Self-sufficient text:** text content must work alone, for clients that ignore `structuredContent` or images.
- **Bounded results:** result size budgets with paging.
- **Concurrency and timeouts:** parallel tool calls (input serialized, `BUSY`); stay well under client-side
  timeouts (default calls ≤ 30 s).
- **Tool-name prefix:** an optional prefix for clients that don't namespace tools.
- **stdout hygiene on stdio:** nothing but JSON-RPC on stdout; test it.
- **Older clients:** negotiate with 2025-06-18 / 2025-11-25 clients.
- **Docs positioning:** the MCP server is the product; SDK/CLI are secondary.

### Step 3: quality audit

Audit every module against QUALITY.md (A1–A9, B1–B8), the safety model (PLAN §7), MCP compliance, PyObjC
correctness of `engine/macos` (against Apple/PyObjC docs), and docs accuracy. Use independent reviewers who try to
refute each finding. Fix the confirmed ones and keep everything green.

### Step 4: final verification and report

Run the full suite, lint, pyright, coverage and CLI smoke yourself. Commit and push. Update the GitHub issue
checklists. Report honestly, including what is still unverified on real macOS.

### Step 5 (needs the owner's Mac): spikes

Run `spikes/README.md` S1→S6 on a real Mac (S1 first: it is the go/no-go for the launcher design), build the
launcher (`launcher/build.sh`), run `AXPILOT_MACOS_TESTS=1 uv run pytest tests/engine -q`, and work through the
"Mac verification" items in `docs/dev/handoff/build1-followups.md`.

## 7. Environment and verified technical facts

- Tooling: `uv` (`uv sync`, `uv run pytest -q`, `uv run ruff check …`, `uv run pyright`), Python 3.12 in dev.
- **MCP SDK v2 facts verified in this repo:**
  - Setup: `from mcp.server import MCPServer`; `MCPServer(name=, instructions=, version=)`.
  - Registration: `server.add_tool(fn, name=, title=, description=, annotations=ToolAnnotations(read_only_hint=, destructive_hint=, idempotent_hint=, open_world_hint=))`.
  - Structured output: a tool returning `Annotated[CallToolResult, PydanticModel]` advertises `outputSchema`. Return `CallToolResult(content=[TextContent(type="text", text=…)], structured_content={…})`; it is validated unless `is_error=True`.
  - In-process tests: `async with Client(server) as c` negotiates protocol `2026-07-28`.
  - Imports: types come from `mcp_types`; `ToolError` from `mcp.server.mcpserver.exceptions`.
- PyObjC is only installed on macOS (`sys_platform == 'darwin'` markers). Only `engine/macos` and `host/ui` may
  import it; everything else must run and be tested on Linux.
- Paths are redirectable for hermetic tests: `AXPILOT_CONFIG_DIR`, `AXPILOT_HOME`, `AXPILOT_LOG_DIR`,
  `AXPILOT_SOCKET`, `AXPILOT_APP_PATH`.
- Try the server without a Mac: `uv run axpilot stdio --engine fake` (once the CLI exists; build round 2).
- Research basis (Oct 2026): MCP spec `2026-07-28` is stateless (handles in tool args, deterministic `tools/list`,
  SSE and logging deprecated); macOS 27 is Apple-silicon-only; Homebrew removes un-notarized casks; competitors:
  CursorTouch MacOS-MCP (Python, TCC issues), Peekaboo (Swift), mcp-server-macos-use (Swift), cua-driver (Rust).
