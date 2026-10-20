# AXPilot: handoff for the next working session

Give this whole file to the next coding session (any account). It is self-contained: what the project is,
every decision made so far, the exact current state, the rules the owner set, and the ordered remaining work.

- **Repository:** `github.com/Vansh3503/Mac-Mcp` (private). Default branch `main`.
- **Working branch:** `feature/axpilot-foundation` (all work is here, not merged).
- **Last verified work commit:** see `git log --oneline -15`; build round 2 (Step 1) is complete (§5).
- **Owner:** Vansh Malhotra (GitHub `Vansh3503`).

---

## 0. Start here (5 minutes)

1. `git fetch origin feature/axpilot-foundation && git checkout feature/axpilot-foundation && git pull`.
2. `uv sync && uv run pytest -q -p no:cacheprovider` must be green before you change anything.
3. Read §5 for what is committed, then continue with the first open step in §6 (Step 1 is done).
4. **One session at a time.** Don't work on the branch from two sessions in parallel: both would edit the same
   files. Ask the owner which session owns the branch right now.

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
5. `docs/dev/handoff/build2-task-specs.md`: the detailed specs for build round 2 (each package is marked done or
   open).
6. `docs/dev/handoff/build1-followups.md`: deviations, contract issues and Mac-verification follow-ups reported by
   build round 1.
7. The two finished tool modules, which are the reference for every new tool: `src/axpilot/server/tools/observe.py`,
   `src/axpilot/server/tools/input.py` and their tests in `tests/server/tools/`.

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
| 9 | Tool parameter conventions | Keyword-only params; the result-shape param is `report` (`diff`/`snapshot`/`none`), not `return`; values that can be text or numbers are **strings** (schema portability); tool modules don't use `from __future__ import annotations` (the SDK derives schemas from annotations). |
| 10 | Element refs | **Stable and session-unique** (see §6, Step 1a). A ref keeps meaning the same element across snapshots while it exists; a gone element gives `STALE_REF`; numbers are never reused. |
| 11 | Input strategy | Accessibility first, then mouse/keystrokes as a single fallback; **never auto-retry input**. Keystrokes first for Electron/CEF/browser/Java/Qt/unknown app kinds; paste (clipboard restored) for text over 200 characters. |

## 4. Owner's working rules (must follow)

- **Commits and branch names must not mention Claude or Anthropic.** No `Co-Authored-By`/session trailers, no
  "claude/…" branches. Use neutral names (`feature/…`, `fix/…`).
- **Commit author:** `Vansh Malhotra <85389767+Vansh3503@users.noreply.github.com>`. Set it as repo-local git config
  in a fresh clone:
  `git config user.name "Vansh Malhotra" && git config user.email "85389767+Vansh3503@users.noreply.github.com" && git config commit.gpgsign false`
  (signing is off because a session's key would mark the owner's commits Unverified).
- Use Conventional Commits (`feat(server): …`, `fix(perception): …`, `wip(…)` only for interrupted work).
- Don't open pull requests unless the owner asks.
- Commit and push often (cloud containers are ephemeral).
- Never commit personal data in fixtures (the demo fixture uses placeholders such as `example-user`,
  `alex@example.com`, `Alex Example`).
- The old remote branch `claude/mac-mcp-plan` still exists and must be **deleted by the owner** in the GitHub UI
  (deletion was blocked from the session). It holds old commits with trailers; all its content is on
  `feature/axpilot-foundation` already.

## 5. Current state

### Done (committed on `feature/axpilot-foundation`, up to `8a13a66`)

| Area | Path | Notes |
|---|---|---|
| Plan, contracts, quality bar | `docs/PLAN.md`, `docs/dev/CONTRACTS.md`, `docs/dev/QUALITY.md` | |
| Foundation | `src/axpilot/{__init__,errors,models,paths,config}.py`, `engine/{__init__,base}.py` | `Engine` ABC, `ErrorCode`, config precedence flags > env > TOML > defaults |
| Perception | `src/axpilot/perception/*` | snapshots with refs, pruning, text/JSON formats, diffs, fuzzy find, stores, locator re-resolution, serialization, app-kind detection (`appkind.py`), OCR fallback (`vision.py`), navigation helpers (`navigate.py`: scroll containers, viewports, page signatures, row collection, page merging) |
| Fake engine | `src/axpilot/engine/fake.py` + `engine/fixtures/demo_desktop.json` | simulated desktop (Notes, Mail with 150 rows, Terminal, 1Password, a login form, System Settings…) for tests and `--engine fake`; reports `AXMinValue`/`AXMaxValue` for ranged controls |
| Input + policy | `src/axpilot/input/*`, `src/axpilot/policy/*` | key parsing, profiles/toolsets, safety rules (deny lists, Terminal rule, secure fields), rate limit, audit with redaction, confirmations, control state (input lock, pause, yield-to-user) |
| System integrations | `src/axpilot/system/*` | runner, osascript, shell, Spotlight, Shortcuts, defaults, processes, sysinfo, browsers (modules only; the tools that expose them are open) |
| macOS engine | `src/axpilot/engine/macos/*` | PyObjC implementation of every `Engine` method. **Never run on real macOS yet.** |
| Launcher + spikes | `launcher/*`, `spikes/*`, `docs/spikes/RESULTS_TEMPLATE.md` | Swift launcher not compiled yet; spike runbook in `spikes/README.md` |
| Clients, setup, doctor | `src/axpilot/clients/*`, `src/axpilot/setup/*`, `src/axpilot/doctor.py` | 12 client config writers, runtime venv + launcher install, diagnostics |
| **Server core** | `src/axpilot/server/{context,registry,guard,results,schemas,app,instructions}.py`, `src/axpilot/testing/{faulty_engine,harness}.py` | reviewed and fully tested (guard: every error path; app: registry rules, SDK-level errors). Guard order: enabled → rate limit (input) → confirmation (destructive) → input turn (input) → engine call with deadline → audit. Unexpected exceptions become `INTERNAL` with an incident id, never a traceback. |
| **Observe tools (9)** | `src/axpilot/server/tools/observe.py` | snapshot, screenshot, inspect, ocr, list_apps, list_windows, list_displays, wait_for, status; 65 tests incl. error matrix and fault injection |
| **Input tools (9)** | `src/axpilot/server/tools/input.py` | click, type_text, press_keys, drag, move_mouse, set_value, perform_action, select_menu, fill_form; 73 tests. Role-aware `set_value`: toggles set idempotently (radio "off" refused), sliders/steppers clamped with a warning, read-only steppers stepped (≤ 200 steps), pop-up selection verified. Coordinate targets are hit-tested against the policy. |
| Docs, CI, community | `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `AGENTS.md`, `docs/*`, `docs/adr/*`, `.github/*`, `mkdocs.yml`, `packaging/*`, `skills/axpilot/SKILL.md` | |

Bugs found and fixed by the new tests: collapsed scroll areas lost their viewport (a 150-row table reported a
4,200-pt frame, so scrolling at its "center" missed the screen); `collect_rows` on a scroll area duplicated rows
and cells; key sequences with `+`/`-`; app-kind fallback order.

### Test status (Linux, end of build round 2)

`uv run pytest -q -p no:cacheprovider --cov --cov-branch` → **2880 passed, 50 skipped (macOS-only / launcher /
root-only), 0 failed; 94% branch coverage** (gate: `fail_under = 90` in `pyproject.toml`, enforced by CI's
`pytest --cov`). `ruff check`, `ruff format --check` and `pyright` are clean. The full run takes about 5 minutes
without coverage and 13 with it.

### Build round 2 (done, one commit per package)

| Package | Commit | Notes |
|---|---|---|
| 1a stable refs | `355e68e` | session-unique refs carried across snapshots of a window; list rows that scrolled away give `STALE_REF` |
| 1b navigate | `e7b934a` | find (with scrolling), scroll, explore, read; every loop bounded, partial results with warnings |
| 1c apps & system | `982bfdb` | app, window, open (scheme allowlist), clipboard, notify, spotlight_search, system_info, system_control, processes; `manage` policy kind |
| 1d host, bridge, CLI | `54681b4` | Unix-socket host with pairing, stdio bridge with auto-start, Streamable HTTP, the full Typer CLI; real subprocess tests |
| 1e scripting, browser, computer | `9e994e9` | `confirms_itself` tools show the exact command/script; AppleScript injection tests; `computer` with per-server screenshot state |
| 1f windows-mcp naming | `97050d4` | adapters get `(fn, ctx)`; every tool module is required |
| 1g SDK & examples | `c763685` | `Desktop` (portal thread) / `AsyncDesktop`; 5 scripts run with `--fake`; 11 framework examples (`*_example.py`) |
| 1h integrate | see `git log` | golden files (`tests/server/golden`, `AXPILOT_UPDATE_GOLDEN=1`), `scripts/gen_tool_docs.py` → `docs/tools.md` with a freshness test, coverage gate |

Not verified on a real Mac: everything in `engine/macos` (including the run-loop pump), the dialog confirmer,
`ensure_host_running` (`open -g -a`), `LOCAL_PEERCRED`, parent-app identification, `python -m axpilot.host` under
the real launcher, and every AppleScript the browser/scripting tools send. All of it is covered by Linux tests with
fakes only.

### GitHub issues already created

#1 Phase 0 epic (with spike sub-issues #2–#7: S1 launcher TCC go/no-go, S2 AX performance, S3 input strategies,
S4 ScreenCaptureKit, S5 CI permissions, S6 MCP interop), #8–#12 Phase 1–5 epics, #13 post-1.0 backlog. Their bodies
link to `docs/PLAN.md` on `feature/axpilot-foundation`.

## 6. Remaining work, in order

### Step 1: finish build round 2 — **done** (commits in §5)

Specs: `docs/dev/handoff/build2-task-specs.md`. Kept for reference:

**1a. Stable refs (core; do first).** The bug: refs are numbered `e1…eN` per snapshot and a bare ref resolves
against the latest snapshot of *any* app. After an action re-snapshots the window, an old `e29` (the Note body)
silently hits a different element (a list row). After snapshotting Mail, a Notes `e12` hits Mail's `e12`. Design:
- Ref numbers are unique per server context. `SnapshotStore` owns a thread-safe counter; a number is never reused
  for a different element, even after the element disappears, and never across apps or windows.
- New `perception/refs.py` with `carry_refs(snap, previous, allocate)`. Elements matching the previous snapshot of
  the same app and window keep its ref; others get fresh numbers. Match passes, strongest first, each previous
  element used once:
  1. engine handle equality (Mac handles compare with `CFEqual`; guard hashing)
  2. `AXIdentifier` + AX role
  3. locator path + name
  4. AX role + name + rounded frame
  5. AX role + name when unique on both sides

  **A named element is never matched by position alone**, so a reused virtualized row with new content gets a new
  ref. Position-only matching is allowed only for unnamed elements of the same role with overlapping frames.
  Consider sharing one matcher with `perception/diff.py`.
- Remap `parent_ref`, `focused_ref` and the page. `Snapshot.element()` stays O(1) through a lazy index field.
- `ServerContext.build_snapshot(..., previous=None)` defaults to the store's latest snapshot of the same pid.
  `input.observe_after` passes `previous=before`.
- `diff_snapshots` pairs equal refs first, so one element no longer shows up as both "removed e29" and "added e29".
- `SnapshotStore.resolve(ref)` without a snapshot id tries the latest snapshot, then older stored snapshots (newest
  first), always through `live_handle`. Not found anywhere gives `STALE_REF` with a hint. Fix the STALE_REF message,
  which assumes contiguous `e1–eN`.
- Make OCR refs (`o…`) session-unique too, or document that they are per-snapshot.
- Update CONTRACTS, PLAN wording, server instructions and `skills/axpilot/SKILL.md`.
- Tests: every pass; named-element position trap; uniqueness across apps and windows; no reuse; store fallback;
  diff pairing. Also end to end: snapshot Notes, click New Note, then `type_text` into the *old* Note-body ref must
  reach the note body. Add a hypothesis property: one ref never names two elements.

**1b. `tools-navigate`:** find (with scrolling), scroll (an input tool: rate limit, policy, input turn), explore
(app map), read (collect long lists exactly once, `complete`/`at_end`, optional restore). Every loop is bounded by a
count and a ≤ 60 s deadline, returning partial results with `complete=false` and a warning instead of `TIMEOUT`.
Uses the helpers in `perception/navigate.py` (`scroll_container_for`, `viewport_of`, `page_signature`,
`collect_rows`, `merge_pages`).

**1c. `tools-apps-system`:** app, window, open (scheme allowlist), clipboard, notify, spotlight_search,
system_info, system_control, processes (kill destructive → confirmation; refuse pid ≤ 1 and our own pid).

**1d. `host-bridge-cli`:**
- the Unix-socket host (0600 socket in a 0700 dir, stale-socket cleanup, peer uid check), pairing store
- the stdio bridge with `ensure_host_running`
- Streamable HTTP: loopback only, bearer token, Origin/Host checks
- the full Typer CLI: `stdio --direct/--engine fake`, `http`, `host`, `setup`, `doctor`, `connect`, `config`,
  `skill install`, `debug dump-tree`, `tools`, `call`, `version`
- exit codes 0/1/2/3/4/130, no tracebacks without `--debug`, nothing but JSON-RPC on stdout in stdio mode
- one real subprocess test of `uv run axpilot stdio --engine fake`

**1e. `tools-scripting-browser-computer`:** shell, applescript, shortcuts, defaults (destructive ones confirm with
the exact command shown), browser_tabs, browser_read, browser_eval_js, and the single `computer` tool (screenshot-
pixel coordinates, policy hit-testing). Prove injection safety for quotes, backslashes and newlines.

**1f. `naming-windows-mcp`** (split out of 1e; needs all tools registered): the `windows-mcp` naming profile with
argument adapters (`loc=[x,y]` → `x,y`, `clicks` → `count`, PowerShell → Shell, Registry → Defaults, Mac-only tools
in PascalCase) and contract tests through that naming.

**1g. `sdk-examples`** (secondary): `Desktop`/`AsyncDesktop` over the same registered tools via an in-process MCP
client, `Desktop.fake()/direct()/connect()`, typed results, example scripts runnable with `--fake`, framework
examples with `"<your-model>"` placeholders.

**1h. Integrate:**
- Get the whole suite, ruff and pyright green.
- Move every tool module to `REQUIRED_TOOL_MODULES` in `server/app.py`. Today only `observe` is required; the
  others are skipped if missing.
- Check that `TOOL_ORDER` and the registry agree.
- Golden files `tests/server/golden/tools_<profile>_<naming>.json`, regenerated only with `AXPILOT_UPDATE_GOLDEN=1`.
- `scripts/gen_tool_docs.py` → `docs/tools.md`, with a freshness test.
- CLI smoke: `axpilot tools --json`, `axpilot call status --engine fake`,
  `axpilot call snapshot --engine fake --arg app=Notes`, `axpilot doctor`.
- Coverage gate (`fail_under` ≥ 85, enforced in CI).
- Sync `SKILL.md`, then update CHANGELOG and this file.

### Step 2: hardening round (owner's request: "cover all edge cases; any app; any agent framework")

**Status: core done in `3a54f05`.** Delivered: `select_option` (pop-ups, combo boxes, radio and tab
groups, segmented controls, lists and outline rows, by title or index) and `handle_dialog` (sheets,
alerts and dialog windows: inspect, accept, cancel, press a button by title, fill the first text field;
buttons that delete or discard ask first); portable input schemas for every tool under both namings
(no `$ref`/`$defs`, no `null` unions, `additionalProperties: false`, a description on every parameter;
`tests/server/test_portability.py`); the `[tools] prefix` setting; self-sufficient, bounded text for
every observe call and older protocol versions (`tests/server/test_hardening.py`); `shortcuts` waits
30 s by default so calls stay under client timeouts. Already covered before: disclosure triangles
(`set_value`), multi-select with modifiers (`click`), right-click (`click button=right`) and
`AXShowMenu` (`perform_action`), off-screen and disabled elements, `SECURE_INPUT_ACTIVE`.

**Still open** (no dedicated handling yet; agents can get there with the generic tools): date pickers,
color wells, table header sort and cell editing, open/save panels (⌘⇧G + path), toolbar overflow (»),
menu-bar extras. UI conditions that only a real Mac can exercise: Spaces, multi-display and Retina
coordinates, RTL and localized UIs, non-US keyboard layouts, animations settling.

The original brief, kept for reference:

**Controls, handled by their role** (`set_value` is already role-aware for toggles, sliders, steppers and
pop-ups; add a `select_option` tool and a `handle_dialog` tool; update PLAN/CONTRACTS/golden files accordingly):
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
- off-screen elements (scroll into view before acting; `click` already does this); disabled elements (clear reason)
- animations (settle); modal dialogs blocking the window; apps not responding (timeouts)
- multiple windows, tabs and Spaces; multi-display and Retina coordinates
- localized/RTL UIs, non-US keyboard layouts, Secure Event Input

**Agent-framework and MCP edge cases:**
- **Portable input schemas:** must pass OpenAI strict-mode and Gemini function-declaration limits. That means
  flat objects, no surprising `$ref`/`$defs`/`oneOf`, avoiding the `anyOf: [T, null]` pattern where possible, names
  matching `^[a-zA-Z0-9_-]{1,64}$`, and descriptions on every parameter. Add a schema-portability test over every
  tool.
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

**Findings fixed so far** (each with a test):
- `cf43771` B7: the SDK's `Desktop.direct()` test wrote the real `~/Library/Logs/AXPilot`; sdk tests now get a tmp
  `HOME` and `AXPILOT_*` folders. A full run with `HOME` pointed at an empty folder leaves it empty. The root
  `tests/conftest.py` (ruled out by AGENTS.md) became `axpilot.testing.host.short_socket_dir` + per-folder fixtures.
- `1c51a63` safety: `click` (and `handle_dialog`) fell back to a mouse click when an AX press hit the engine
  deadline, although the abandoned press may still land: a possible double press. Now `TIMEOUT` with a hint.
- `cebbb18` A8: silent `pass`/`suppress(Exception)` on disconnects and late cancels now log at debug.
- `a6749d3` B2: fault-injection and policy tests for `select_option`/`handle_dialog`; toolset contract, README
  table (now tested), CONTRIBUTING "Adding a tool" checklist and QUALITY.md brought in line with the code.
- `14ac35d` config: misspelled `AXPILOT_<SECTION>_<KEY>` variables were ignored silently (now a warning with a suggestion);
  four settings were undocumented; `tests/test_config.py` added (there were no direct config tests).
- `76a6aba` docs: commands shipped in round 2 were still marked *planned* (connect, setup, update, uninstall, doctor,
  `debug dump-tree`, the `computer` profile, `ocr`).
- `c439cc8` A5: `read`/`find` restore spun on zero-line scrolls until the time budget ran out when half a line was
  left (`round(0.5) == 0`).

Safety review (S-findings), each with a test:
- `a5eaa22`: no tool may act while AXPilot is asking the user something (pairing or a confirmation dialog).
- `48c0ac6`: `open` no longer runs code or app URLs through files (`.command`, `.app`, `.webloc`…).
- `1e8832f`: `press_keys` with `app` and `select_menu` respect `deny_windows`.
- `41d4694`: `fill_form` values and typed keys stay out of the audit log.
- `0e11d39`: drop points and every scroll step are hit-tested like coordinate targets (a covering window of
  another app is refused).
- `d04bb5e`: keystrokes into the *focused* field (`type_text`/`press_keys` without a ref, `computer type`) follow
  the secure-field rule.
- Finding 7, checked and left as designed (documented here):
  - The bridge hello may ask for any profile: profiles are per client by design (`axpilot stdio --profile`),
    and pairing plus the safety deny-lists still apply.
  - The rate limit is **one host-wide budget** that all clients share, because each connection's context
    shares the host's `RateLimiter`. One hand on one desktop is the point, but a runaway client can starve the
    others until its actions age out (60 s). A per-client budget belongs with the per-client control lease
    (post-1.0, PLAN §5.3).

macOS engine review (M-findings; Linux tests cover the logic, a Mac must confirm the behaviour):
- `3802403` (blocker): `python -m axpilot.host`, the exact command `AXPilot.app` runs (`HOST_ARGS`), failed:
  the host package had no `__main__`. The launcher tests use a stub host and never caught it.
- `002bcd5` M1: nothing turned the main Cocoa run loop (the CLI and host run asyncio on the main thread). So the
  keyboard-layout lookup (scheduled with `callAfter`) always fell back to US-ANSI after a 0.5 s wait, and
  `NSWorkspace`'s app list could go stale. `axpilot.runloop` now turns it every 50 ms in every CLI command, in
  the sync `Desktop` while it waits and in `AsyncDesktop` while it is open.
- `a0a9bbb` M2: activation set `AXFrontmost` only after a full 1.5 s timeout, although macOS 14+ ignores
  `activateWithOptions:` from background processes; both now go out in the first attempt.
- `a0a9bbb` M3: menu shortcuts that are whitespace characters (⌘Space, ⌘↩, ⌃⇥) were lost because the command
  character was stripped like a title.
- `064196e` M4: the engine factory never checked for macOS 14 (ADR-0003); older systems now get `UNSUPPORTED_OS`.
- The reviewer could not settle these without a Mac (add them to the Step 5 checks): whether ScreenCaptureKit and
  `NSWorkspace` completion handlers fire without the run loop (believed yes: private queues); whether `AXPress` on
  pop-up buttons and menu-bar items blocks for the full 1 s messaging timeout; whether clearing the NumericPad and
  SecondaryFn flags on arrow/F-key events breaks ⇧/⌘-arrow shortcuts in some apps; how synthetic line scrolling
  behaves with natural scrolling; whether Java/Qt apps accept 20-character Unicode key events; whether
  `IsSecureEventInputEnabled` is safe off the main thread.

Checked and found fine: the MCP surface (capabilities come from the SDK's subscription model; errors are
`isError` results with a code and hint; unknown tools give `INVALID_ARGUMENT`), stderr excerpts ≤ 500 chars,
clipboard restore in `finally`, audit redaction of the new tools' text, every loop in the tools bounded.

### Step 4: final verification and report

Run the full suite, lint, pyright, coverage and CLI smoke yourself. Commit and push. Update the GitHub issue
checklists. Report honestly, including what is still unverified on real macOS.

### Step 5 (needs the owner's Mac): spikes

Run `spikes/README.md` S1→S6 on a real Mac (S1 first: it is the go/no-go for the launcher design), build the
launcher (`launcher/build.sh`), run `AXPILOT_MACOS_TESTS=1 uv run pytest tests/engine -q`, and work through the
"Mac verification" items in `docs/dev/handoff/build1-followups.md`.

## 7. Environment and verified technical facts

- Tooling: `uv` (`uv sync`, `uv run pytest -q -p no:cacheprovider`, `uv run ruff check …`, `uv run pyright`),
  Python 3.12 in dev. `pytest-xdist` is not installed (no `-n`); the full suite takes about 2 minutes.
- **MCP SDK v2 facts verified in this repo:**
  - Setup: `from mcp.server import MCPServer`; `MCPServer(name=, instructions=, version=)`.
  - Registration: `server.add_tool(fn, name=, title=, description=, annotations=ToolAnnotations(read_only_hint=, destructive_hint=, idempotent_hint=, open_world_hint=))`. In this repo tools register through the `@tool(...)` decorator in `server/registry.py`.
  - Structured output: a tool returning `Annotated[CallToolResult, PydanticModel]` advertises `outputSchema`. Return `CallToolResult(content=[TextContent(type="text", text=…)], structured_content={…})`; it is validated unless `is_error=True`.
  - In-process tests: `async with Client(server) as c` negotiates protocol `2026-07-28`. The test helpers are in `axpilot.testing` (`make_test_context`, `connected_client`, `call`, `structured`).
  - Imports: types come from `mcp_types`; `ToolError` from `mcp.server.mcpserver.exceptions`.
- The `full` profile includes the `shell` tool, which lifts the Terminal rule; test the Terminal rule with the
  `standard` profile.
- PyObjC is only installed on macOS (`sys_platform == 'darwin'` markers). Only `engine/macos` and `host/ui` may
  import it; everything else must run and be tested on Linux.
- Paths are redirectable for hermetic tests: `AXPILOT_CONFIG_DIR`, `AXPILOT_HOME`, `AXPILOT_LOG_DIR`,
  `AXPILOT_SOCKET`, `AXPILOT_APP_PATH`.
- Try the server without a Mac: `uv run axpilot stdio --engine fake` (once Step 1d lands).
- Research basis (Oct 2026): MCP spec `2026-07-28` is stateless (handles in tool args, deterministic `tools/list`,
  SSE and logging deprecated); macOS 27 is Apple-silicon-only; Homebrew removes un-notarized casks; competitors:
  CursorTouch MacOS-MCP (Python, TCC issues), Peekaboo (Swift), mcp-server-macos-use (Swift), cua-driver (Rust).
