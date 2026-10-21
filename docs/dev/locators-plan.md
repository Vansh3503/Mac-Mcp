# Plan: stable locators for automation scripts (*planned*)

Status: **Draft for the owner's review.** Nothing in this document is implemented yet. · 2026-10-21

> **The use case in one sentence.** Someone has a Mac app and wants an automation script for it
> (a regression test, a nightly job, a QA suite). They ask their agent in VS Code (or Cursor,
> Claude Code, any MCP client): *"Give me the locator of the Download button at the top of MyApp
> and write a script that clicks it."* The agent uses AXPilot to look at the real app, picks a
> locator that will still work tomorrow, proves that it matches exactly one element, and writes it
> into a script in the format the user wants (the AXPilot SDK, the `axpilot` CLI, Appium Mac2,
> AppleScript, XCUITest).

This keeps the agent at the centre (the owner's positioning in HANDOFF §1): the agent *authors*
the script with AXPilot's tools, and the script then runs through the same server, policy and
audit as the agent does.

---

## 1. Who needs this and what they do today

| Person | Their app | What they want | What they use today |
|---|---|---|---|
| QA / test engineer | Their company's Mac app, or a vendor app they must test | Regression scripts that survive UI changes | Accessibility Inspector (Xcode) by hand, Appium Inspector, trial and error with AppleScript |
| App developer | Their own AppKit, SwiftUI or Electron app | UI tests and smoke tests in CI | XCUITest recorder in Xcode, writing `accessibilityIdentifier`s by hand |
| Ops / power user | Any third-party app (an export button, a sync button) | A scheduled job that clicks something | AppleScript `System Events`, coordinate clicks with `cliclick` / pyautogui (break on every layout change) |
| Agent builder | Any app | A deterministic script for the steps an agent figured out once | Copying refs and coordinates out of a transcript (they don't survive the session) |

The pain is the same in every row: finding *how to address* one specific element in a way that is
unique now and stays valid when the window moves, the language changes or the app updates.

## 2. What AXPilot already has, and what is missing

Already there (verified in the code):

- `explore` lists an app's toolbar buttons, tabs, sidebars and menus: the "what is at the top of
  this window" question.
- `find(text, role, identifier, app, window_id, container, scroll)` finds the element and gives
  it a ref; `inspect(ref)` returns its `identifier` (`AXIdentifier`), `locator_path`
  (`AXToolbar[0]/AXButton[3]`-style steps), live attributes and ancestors.
- `perception/model.py` `Locator` (pid, window, path, AX role, identifier, name) and
  `perception/resolve.py` `match_locator()` already re-find an element in a fresh capture:
  identifier first, then path-with-same-name, then a unique role + name. This is the right core.
- The Python SDK (`Desktop.connect()`, `mac.find(...).first.click()`) and `axpilot call <tool>`.
- `axpilot connect vscode --scope project` writes `.vscode/mcp.json` for VS Code (Copilot).

Missing for this use case:

| Gap | Why it matters |
|---|---|
| **Refs are session-scoped.** `e14` means nothing in tomorrow's run, and every `axpilot call` starts a new in-process server, so a ref from one `call` is gone in the next. | A script needs something it can store in a file. |
| **No portable locator format.** `Locator` is internal (it holds a pid and a window id) and is not serialisable or accepted by any tool. | The agent has nothing to paste into the script. |
| **No uniqueness proof.** `find` returns ranked fuzzy matches; nothing says "this description matches exactly one element in this window". | A locator that matches two buttons clicks the wrong one. |
| **No stability judgement.** An `AXIdentifier` of `_NS:123` (Interface Builder's auto id), a localized title and a positional path all look alike today. | The agent can't tell a durable locator from a brittle one, so neither can the user. |
| **Exact matching for scripts.** `find` is fuzzy by design (good for agents); `identifier` matching is case-insensitive. | Scripts must be deterministic: exact by default, fuzzy only when asked. |
| **No export to other frameworks.** | Many teams already have Appium, AppleScript or XCUITest suites. |
| **No "the element under my mouse".** | The fastest way for a human to say *which* Download button. |

## 3. User journey in VS Code

**Setup (once).** `axpilot setup`, then in the project folder `axpilot connect vscode --scope
project` (Copilot agent mode reads `servers.axpilot` from `.vscode/mcp.json`). Users of the Claude
Code extension run `claude mcp add axpilot -- axpilot stdio`; Cursor, Cline and Continue work the
same way through `axpilot connect`.

**The conversation.**

> **User:** In MyApp, give me a stable locator for the Download button in the toolbar, and add a
> pytest that clicks it and waits for the Downloads sheet.

What the agent does (each step is one tool call; nothing is clicked until the user asks):

1. `explore(app="MyApp")`: sees the toolbar: `e12 button "Back"`, `e13 button "Share"`,
   `e14 button "Download"`.
2. `locate(ref="e14", formats=["axpilot-python"])` (new): gets ranked candidate locators, each
   with its match count in the live window and a stability grade, and a ready snippet.
3. Optionally `locate(locator={...}, highlight=true)` (new): AXPilot's overlay draws a box around
   the matched element so the user can confirm "yes, that one" without anything being clicked.
4. Writes `tests/test_download.py` with the recommended locator and an explicit wait.
5. Verifies the script's locators read-only: `find(locator={...})` (new parameter) returns
   `unique: true` for each. It runs the test itself (which clicks) only if the user asks.

What `locate` returns (text `content`; the same data is in `structuredContent`):

```
locate e14 button "Download" · MyApp (com.example.MyApp) · window "Library" · snapshot s_9k2p
recommended  1 identifier          high    unique  {"app": "com.example.MyApp", "role": "button", "identifier": "downloadButton"}
             2 name within toolbar medium  unique  {"app": "com.example.MyApp", "within": [{"role": "toolbar"}], "role": "button", "name": "Download"}
             3 path                low     unique  {"app": "com.example.MyApp", "path": "AXToolbar[0]/AXButton[2]"}
warnings: "Download" is UI text: it changes with the app's language and copy edits.
snippets: axpilot-python (structuredContent.snippets)
```

The script the agent writes:

```python
from axpilot import Desktop

DOWNLOAD = {"app": "com.example.MyApp", "role": "button", "identifier": "downloadButton"}
DOWNLOADS_SHEET = {"app": "com.example.MyApp", "role": "sheet"}


def test_download_opens_the_downloads_sheet() -> None:
    with Desktop.connect() as mac:
        mac.app("com.example.MyApp").activate()
        mac.locate(DOWNLOAD).click(timeout=10)
        mac.locate(DOWNLOADS_SHEET).wait(timeout=10)
```

**Without an agent.** The same engine is on the command line for people who just want the string:

```sh
axpilot locate --app MyApp --role button --text Download --format appium-mac2
axpilot locate --app MyApp --at-cursor --delay 3       # hover the element within 3 s
axpilot locate --check '{"app": "com.example.MyApp", "identifier": "downloadButton"}'
```

## 4. The locator format (public API, versioned)

A locator is a small JSON object. It never contains a pid, a window id, a ref or a coordinate
(they don't survive a relaunch), and never contains a field's *value* (user data, and it changes).

| Field | Meaning | Matching |
|---|---|---|
| `v` | Format version, `1`. Optional on input, always present on output. | Unknown versions → `INVALID_ARGUMENT`. |
| `app` | Bundle id (preferred, written by `locate`) or app name. | Bundle id exact; name as `app` arguments resolve today. |
| `window` | Optional. `{"title": "…", "match": "exact" \| "prefix"}`, `{"kind": "main" \| "focused" \| "sheet" \| "dialog"}` or `{"index": 0}`. Omitted: the app's focused window, then its main window. | `locate` adds a title only when it is needed to disambiguate (titles often carry document names or other personal data). |
| `within` | Optional list of ancestor steps, outermost first, each `{role?, identifier?, name?}`. | A subsequence of the element's ancestors, not necessarily direct parents, so a wrapper group added in an app update doesn't break it. |
| `role` | Short role (`button`) or AX role (`AXButton`). | Exact. |
| `subrole` | Optional AX subrole (`AXCloseButton`, `AXSearchField`). | Exact. |
| `identifier` | `AXIdentifier`. | Exact and case-sensitive (unlike `find`'s fuzzy filter). |
| `name` | The element's label as snapshots show it (title > label > description > placeholder). | Whitespace-normalized exact by default; `name_match: "prefix" \| "contains"` for names such as "Download (3)". |
| `index` | 0-based position among the matches, in document order. | Last resort; graded `low`. |
| `path` | `AXToolbar[0]/AXButton[2]` from the window, as `inspect.locator_path`. | Alternative to the fields above; graded `low`. |
| `menu` | A menu-bar path, `["File", "Download…"]`, instead of an element. | As `select_menu` matches today. A menu command is often the most robust way to drive a Mac app. |

Matching runs on the **raw, unpruned** captured tree (as `match_locator` does today), so changes
to snapshot pruning never change what a locator means. A locator matches 0, 1 or many elements;
only 1 is a success for an action.

*Optional (owner's call, §11):* a one-line string form for readability, parsed into the same
object: `button#downloadButton`, `toolbar >> button "Download"`, `sheet "Downloads" >> button
"Save"[0]`, with the app given separately (`mac.app("MyApp").locate("button#downloadButton")`).

## 5. How candidates are generated and graded

For one element, `locate` builds candidates from the most to the least durable, checks each
against a fresh capture of the window, and recommends the first one that is **unique**:

| # | Strategy | Grade | Downgraded when |
|---|---|---|---|
| 1 | `identifier` (+ `role`) | `high` | The id looks generated: `_NS:<n>` (Interface Builder), UUID-like, all digits, or ends in a counter that differs between siblings → `low`, with a warning. |
| 2 | `menu` path (the element is a menu item) | `high` | The title is dynamic ("Undo Typing"). |
| 3 | `role` + `name` | `medium` | The name contains digits, a percentage, a date or a file name → offer `name_match: "prefix"` and warn. |
| 4 | `role` + `name` scoped by `within` (the nearest ancestor with an identifier, else the nearest toolbar, sheet, dialog, tab group or named group) | `medium` | As above. |
| 5 | `role` + `name` + `index` | `low` | Always low: siblings appearing or disappearing shift it. |
| 6 | `path` | `low` | Always low: any layout change breaks it. |
| — | Screen coordinates | never recommended | Shown only when nothing else works (OCR text, custom-drawn canvas), labelled `fragile`, with a warning. |

Rules that hold for every candidate:

- **Uniqueness is proven, not assumed.** If the window's capture was truncated by budgets, the
  candidate is marked `unique: unknown`, graded one step lower, and the warning says so.
- **Secure fields** get role/name/identifier locators like any element; their value is never read.
- **Electron and Chromium apps** need `AXManualAccessibility`, which AXPilot sets; exports to tools
  that don't set it (Appium Mac2, AppleScript) say so in a warning.
- **Toolbar items** can move into the » overflow menu in a narrow window; `locate` warns when the
  element is in a toolbar.
- **Transient elements** (in an open menu, popover or sheet) get a warning that the script has to
  open their container first; `locate` names the container.
- **When the user owns the app** and there is no usable identifier, the warning says how to add
  one: `setAccessibilityIdentifier(_:)` (AppKit), `.accessibilityIdentifier(_:)` (SwiftUI), an
  HTML `id` for web content (exposed as `AXDOMIdentifier`; to verify on a Mac in L3).

The key invariant, tested on every fixture: **the recommended locator, matched against the same
capture, returns exactly the element it was generated from.**

## 6. Tool, SDK and CLI surface

### 6.1 New tool: `locate` (observe toolset; `standard` and `full`; Windows-MCP name `Locate`)

Read-only and idempotent. Exactly one of:

| Param | Meaning |
|---|---|
| `ref` (+ `snapshot_id`) | Build locators for an element from a snapshot, `find` or `explore`. |
| `locator` | Check a locator: how many elements it matches now, with their refs. This is how an agent repairs a script whose locator stopped working. |
| `point` (+ `coordinate_space`, `screenshot_id`) | The element under a point (*L3*). |
| `at_cursor` (+ `delay_ms` ≤ 10000) | The element under the mouse pointer, optionally after a delay so the user can hover it (*L3*). |

Plus `formats` (subset of §7; default `["axpilot"]`, the JSON only), `max_candidates` (default 3)
and `highlight` (draw the overlay box for ~2 s when the indicator is enabled; *L3*).

Returns `element`, `candidates` (`locator`, `strategy`, `grade`, `matches`, `unique`, `reasons`),
`recommended` (index), `snippets` (`{format: text}`), `snapshot_id` and `warnings`.

Errors: `STALE_REF`, `SNAPSHOT_NOT_FOUND`, `APP_NOT_FOUND`, `WINDOW_NOT_FOUND`, `INVALID_ARGUMENT`
(bad locator, unknown format, several targets), `NOT_SUPPORTED` (an OCR `o`-ref: it has no
accessibility element to address), `BLOCKED_BY_POLICY`, `PERMISSION_ACCESSIBILITY`, `TIMEOUT`.

Why a new tool and not `inspect(locators=true)`: `inspect` describes, `locate` also *checks
against the live window* and takes a locator or a point as input. The cost is one more tool in
`standard` (not `minimal`).

### 6.2 Additive parameters on existing tools

| Tool | Change | Phase |
|---|---|---|
| `find` | `locator` (exact matching per §4); the result gains `unique`. | L1 |
| `wait_for` | `locator` for `condition: element \| element_gone`. | L2 |
| `click`, `type_text`, `set_value`, `select_option`, `perform_action`, `scroll` | `locator` as an alternative to `ref`, resolved right before acting; 0 or >1 matches is an error and nothing is done. Needed by `axpilot call` and non-Python scripts, whose refs don't survive between calls. | L2 (owner's call, §11) |

All additive: no existing parameter changes meaning. Policy runs on the resolved element exactly as
for a ref (app deny-list, Terminal rule, secure fields), so a locator can't reach anything a ref
can't.

### 6.3 Error codes

New, additive codes (QUALITY.md A2 asks for the most specific code; nothing existing fits):

- `ELEMENT_NOT_FOUND`: a locator matched nothing (after `timeout`, for waits).
- `LOCATOR_AMBIGUOUS`: a locator matched several elements; the message gives the count and the
  hint names the fields that would tell them apart.

`find(locator=…)` and `locate(locator=…)` don't raise them (they report `matches` and `unique`);
input tools and the SDK do. The CLI exits with `1` (operational error, QUALITY.md A9) and prints
the code in `--json` output.

### 6.4 SDK

```python
download = mac.locate(DOWNLOAD)            # lazy: nothing is resolved yet
download.click(timeout=10)                 # waits for exactly one match, then clicks
download.type_text("…"); download.set_value(True); download.inspect()
download.exists; download.count            # check now, without waiting
download.wait(timeout=10); download.wait_gone(timeout=10)
mac.app("MyApp").locate("button#downloadButton")   # string form, if adopted
Locator.from_json(...) / .to_json()        # load locators from a file the agent wrote
```

A locator handle is resolved again for every action, so a script never holds a stale ref (the
model Playwright's locators use). With 0 matches after the timeout it raises `ELEMENT_NOT_FOUND`;
with several, `LOCATOR_AMBIGUOUS` at once. `Desktop.fake()` resolves locators against the
simulated desktop, so script logic is unit-testable in CI.

### 6.5 CLI

`axpilot locate` (§3) wraps the tool: `--app`, `--text`, `--role`, `--identifier`,
`--at-cursor [--delay]`, `--check <json>`, `--format` (repeatable) and `--json`. It takes no
`--ref`, because refs don't outlive the process.

### 6.6 For agents: MCP prompt and the Agent Skill

- `skills/axpilot/SKILL.md` gains a "Writing automation scripts" section: locate, never paste
  refs or coordinates into scripts; prefer identifiers; check `unique`; add waits; never write
  typed secrets or clipboard contents into a script; verify read-only before running.
- An MCP prompt `write_automation_script(app, goal, format)` that loads the same guidance. VS Code
  and Claude Code list MCP prompts as slash commands (the exact spelling per client is verified in
  L2). This is optional: the tool descriptions and the skill already carry the workflow.

## 7. Export formats

Pure formatting functions over a locator (no PyObjC, tested on Linux with golden files).

| Format | Output (for the identifier case) | Fallback without an identifier | Phase |
|---|---|---|---|
| `axpilot` | The JSON locator | — | L1 |
| `axpilot-python` | `mac.locate({...}).click()` | Same, with `name`/`within` | L1 |
| `axpilot-cli` | `axpilot call click --json '{"locator": {...}}'` | Same | L2 (needs §6.2 input `locator`) |
| `appium-mac2` | `driver.find_element(AppiumBy.ACCESSIBILITY_ID, "downloadButton")` | `-ios class chain`: ``**/XCUIElementTypeToolbar/**/XCUIElementTypeButton[`title == "Download"`]`` | L3 |
| `xcuitest` (Swift) | `app.buttons["downloadButton"].click()` | `app.toolbars.buttons["Download"]` | L3 |
| `applescript` | ``click (first button of toolbar 1 of window 1 whose value of attribute "AXIdentifier" is "downloadButton")`` inside `tell application "System Events" to tell process "MyApp"` | `button "Download"` (title) or `… whose description is "Download"` (toolbar buttons often have only a description) | L3 |

The AX → XCUIElement mapping (`AXButton` → `XCUIElementTypeButton`, `AXTitle` → `title`,
`AXDescription` → `label`, `AXIdentifier` → `identifier`, …) and the AppleScript element classes
are written from documentation and **must be checked on a Mac against real Appium Mac2 and
`osascript` runs before L3 ships**; each exporter lists the roles it can't express and falls back
to a warning instead of guessing. Later candidates: Hammerspoon `hs.axuielement`, Playwright for
Electron apps (from `AXDOMIdentifier`).

## 8. Safety and privacy

- `locate` is read-only and goes through the shared guard like `inspect`: policy check
  (`kind="observe"`), audit with redacted arguments. Deny-listed apps give `BLOCKED_BY_POLICY`;
  `point`/`at_cursor` hit-test first, so pointing at Keychain Access is refused too.
- Locators and snippets never contain values, typed text, clipboard contents or secure-field data.
  Window titles appear only when they are needed to disambiguate, and `locate` says when it added
  one.
- `highlight` uses the existing indicator overlay, which is excluded from screenshots.
- Scripts gain no new power: the SDK and CLI talk to the same server, so the deny-list, Terminal
  rule, confirmations, rate limit, kill switch and yield-to-user all apply to every scripted action.
- No network, no telemetry. Exports are text; AXPilot never contacts an Appium server.

## 9. Phases

Sizes are rough (S ≈ 1–2 days, M ≈ 3–5 days, L ≈ 1–2 weeks with Mac verification).

| Phase | Scope | Size | Exit criteria |
|---|---|---|---|
| **L1: core** | `perception/locators.py` (format, exact matcher on the raw tree, candidate generation, grading, generated-id detection); `locate(ref \| locator)`; `find(locator=…)`; `axpilot` and `axpilot-python` exports; CONTRACTS.md; tools/list goldens regenerated deliberately; docs (`tools.md` via the generator, a "Locators" page); the SKILL.md section | M | Round-trip invariant holds for every element of every fixture; every error code of `locate` tested; policy-bypass tests green |
| **L2: scripts** | SDK `locate()` / `Locator`; `wait_for(locator=…)`; `locator` on input tools (if agreed); `ELEMENT_NOT_FOUND` / `LOCATOR_AMBIGUOUS`; `axpilot locate`; `axpilot-cli` export; `examples/scripts/click_by_locator.py`; a "Write automation scripts with your agent (VS Code)" guide; optional MCP prompt | M | Example script passes against `Desktop.fake()` in CI; CLI exit codes tested; evals gain "locate the X button" tasks checked by resolving the locator |
| **L3: pointing and other frameworks** | `locate(point \| at_cursor, highlight)` (a new `Engine` method for the element under a point, FakeEngine included); `appium-mac2`, `xcuitest`, `applescript` exports; `AXDOMIdentifier` for web content | L | Mac checklist: Finder, Safari, TextEdit, an Electron app; each exported snippet run by hand against the real tool |
| **L4: post-1.0** | A menu-bar "copy locator under the pointer" hotkey; recording a sequence of clicks into a script (needs a listen-only event tap, likely the Input Monitoring permission, and a privacy design: never record typed text); self-healing suggestions when a locator breaks | — | Separate proposal; needs the owner's go-ahead |

Suggested placement in the roadmap ([PLAN.md](../PLAN.md) §15):
L1–L2 before the public preview (`v0.3.0`), L3 with Phase 4, L4 in the post-1.0 backlog (it
already lists macros and recording).

## 10. Testing (per QUALITY.md)

- `tests/perception/test_locators.py`: parsing and validation (every invalid shape →
  `INVALID_ARGUMENT`), exact vs prefix vs contains names, `within` as a subsequence, `index`,
  `path`, truncated captures (`unique: unknown`), generated-id detection, secure fields, menus.
- Property tests (`hypothesis`): random trees from the fixture generator; the round-trip invariant;
  a locator never matches across apps or windows it doesn't name.
- Export goldens in `tests/perception/golden/locators/`, one file per format and strategy.
- `tests/server/tools/`: `locate` and `find(locator=…)` against `FakeEngine`, every error code,
  fault injection (raw exceptions → `INTERNAL`, the server survives), policy-bypass attempts
  (a locator naming a deny-listed app, `at_cursor` over Keychain Access, a locator into Terminal
  for input tools).
- SDK and CLI tests with `Desktop.fake()` and the CLI runner; no `sleep` over 0.2 s (the
  `timeout` waits use the injected clock).
- macOS tests (`macos`, and `AXPILOT_MACOS_TESTS_MUTATE=1` for the clicking ones): the round-trip
  on real Finder, TextEdit and Safari windows.

## 11. Decisions for the owner

1. **Tool name and shape:** a new `locate` tool (recommended) or `inspect(locators=true)`.
2. **`locator` on input tools** (§6.2): recommended, because `axpilot call` and non-Python
   scripts can't use refs; it touches six tool schemas (additive).
3. **New error codes** `ELEMENT_NOT_FOUND` and `LOCATOR_AMBIGUOUS` (recommended) or reuse
   `INVALID_ARGUMENT` with a reason field.
4. **String form** of locators (§4): adopt in L2, or JSON only.
5. **Which exports ship in 1.0:** recommended `axpilot`, `axpilot-python`, `axpilot-cli`,
   `appium-mac2`, `applescript`; `xcuitest` if the Mac check is cheap.
6. **Priority:** L1–L2 before the public preview, or after 1.0.
