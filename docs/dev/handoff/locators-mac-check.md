# Locators checked on a real Mac (PR #39, `feature/locators-l1`)

Checked on 2026-10-21 at `291944b`, in direct mode (`Desktop.direct(confirm=False)`), with throwaway
scripts outside the repository. No source code was changed.

- **Mac:** macOS 26.6.2 (25G83), Apple silicon (arm64), Python 3.12.13.
- **Terminal:** the Claude desktop app (Code tab). `axpilot doctor`: Accessibility and Screen Recording
  granted for direct mode (macOS attributes them to Claude Code); 8 ok, 1 warning (development build
  can't verify the launcher), 0 problems.
- **Setup:** `uv sync` clean; `tests/perception/test_locators.py` + `tests/server/tools/test_locate.py`:
  **156 passed** in 14.7 s.
- **Desktop left as found:** the two Finder windows opened for the checks were closed. TextEdit was
  already running with no windows before the check, so it was left running (still no windows). A Finder
  window that was already open was not touched. Nothing was typed or saved.

## Results

| Check | Result | Notes |
|---|---|---|
| A. Finder toolbar | **Pass** | Back button: no `AXIdentifier`. Recommended `{"role": "AXButton", "name": "back"}` (medium, unique), plus a path (low, unique). Warnings: no identifier, toolbar overflow. `find(locator=…)` returned the same ref, `count` 1, `unique` true. |
| B. Later session | **Pass**, with one caveat | New process, new refs: all 4 saved locators (Finder Back, Save sheet name field / Cancel / Save) gave `count` 1, `unique` true. The generated `axpilot-python` snippet resolved the locator but the click raised `ELEMENT_NOT_ACTIONABLE`: Back is **disabled** in a fresh window that has no history (expected behaviour, see below). |
| C. Flow, `locate(refs=…)` | **Pass** | All three sheet elements have an app-set `AXIdentifier`, so all were graded high. Each got the warning "inside a sheet". Script generated as expected. After Escape: `STALE_REF` with the "locate elements while their screen is open" hint. cmd+w closed the empty document without a prompt. |
| D. Several windows | **Pass** | 3 Finder windows. The element in a back window got `"window": {"title": …}` in both candidates, plus a warning that titles change. `find(locator=…)` found 1 match, unique, inside the back window's frame. |
| E. Checking locators | **Pass** | `role: AXButton`: count 9, unique false, warning. With `index: 0`: count 1, unique true, plus better suggestions (`within` a sidebar row, low; path, low). "Nope": count 0, unique false, warning, no error. SDK: `.click()` raised `LOCATOR_AMBIGUOUS` (count 9) and `ELEMENT_NOT_FOUND`; nothing was clicked. Locators without `"v"` are accepted. |
| F. Safety | **Pass** | Finder on `deny_apps`: `locate(locator=…)`, `find(locator=…)` (and `snapshot`) all raised `BLOCKED_BY_POLICY` with the deny-rule hint. |
| G. Electron (VS Code, read-only) | **Pass** | `app_kind` electron, `ax_quality` **partial** both times. 150 shown / 183 total both times (no growth on the second snapshot: an earlier session today had already switched on VS Code's tree). Explorer tab and Explorer heading: no `AXIdentifier` (and `AXDOMIdentifier` empty), so name locators (medium, unique) plus deep paths (low). OCR sub-check not run: it only applies when `ax_quality` is "poor". |
| H. Speed | **Pass** | Largest window: Finder, 81 elements (interactive) / 150 (full). `locate(ref)` median **0.54 s** (first call in a process 1.0 to 1.2 s). `find(locator)` median **0.44 s**. |
| I. macOS engine tests | **Pass** | `118 passed, 3 skipped in 10.60s` (skips: PyObjC-missing test, 2 clipboard tests that need `AXPILOT_MACOS_TESTS_MUTATE=1`). |

### Timings

| Call | Window | Time |
|---|---|---|
| `snapshot` | Finder, 75 elements | 0.36 to 0.49 s |
| `find("Back")` | Finder | 0.21 to 0.24 s |
| `locate(ref)` | Finder (first locate in the process) | 1.00 to 1.09 s |
| `locate(ref)` | Finder back window (D) | 0.25 s |
| `find(locator)` | Finder | 0.44 s; 0.63 s as the first call of a new process |
| `locate(refs)`, 3 elements | TextEdit Save sheet, 62 elements | 0.15 s |
| `find(locator)` | TextEdit Save sheet (new process) | 0.12 to 0.14 s |
| `snapshot` ×2 | VS Code, 150/183 elements | 0.76 s, 0.44 s |
| `locate(ref)` / `find(locator)` | VS Code | 0.79 to 0.89 s / 0.32 s |

## Which elements got which grade

- **`AXIdentifier` (high):** only TextEdit's Save sheet (a system save panel). The name field is
  `saveAsNameTextField`, Cancel is `CancelButton`, Save is `OKButton`.
- **Medium only (role + name):** Finder's toolbar Back button (name `back`, lowercase), VS Code's
  Explorer activity-bar tab (`AXRadioButton`, name `Explorer (⇧⌘E)`) and Explorer sidebar heading.
- **Low only:** a nameless button in a Finder sidebar row (the `index: 0` match in E): `within` the row
  plus `role` (low), or a path.
- Finder doesn't set an identifier on toolbar buttons. VS Code (Electron) sets neither `AXIdentifier`
  nor `AXDOMIdentifier` on the elements tried.

## Output of what failed or surprised

**B, running the generated snippet** (with `Desktop.connect()` replaced by `Desktop.direct(confirm=False)`):

```python
from axpilot import Desktop

BACK = {"v": 1, "app": "com.apple.finder", "role": "AXButton", "name": "back"}

with Desktop.connect() as mac:
    mac.find(locator=BACK).click()
```

```
axpilot.errors.AXPilotError: ELEMENT_NOT_ACTIONABLE: e221 button "back" is disabled.
```

The locator resolved to exactly one element in the new session; the click was refused because a Finder
window opened with `open` has no history, so Back is disabled. This is correct behaviour; the test plan
assumed Back would be enabled. To see the click succeed, the window first needs a navigation step (for
example opening a subfolder in the same window), which was not in the plan's steps.

**C, the generated script** (the steps follow the order of the refs passed, here name field, Cancel, Save):

```python
from axpilot import Desktop

SAVE_AS = {
    "v": 1,
    "app": "com.apple.TextEdit",
    "role": "AXTextField",
    "identifier": "saveAsNameTextField",
}
CANCEL = {
    "v": 1,
    "app": "com.apple.TextEdit",
    "role": "AXButton",
    "identifier": "CancelButton",
}
SAVE = {
    "v": 1,
    "app": "com.apple.TextEdit",
    "role": "AXButton",
    "identifier": "OKButton",
}

with Desktop.connect() as mac:
    mac.find(locator=SAVE_AS).type_text("your text")
    mac.find(locator=CANCEL).click()
    mac.find(locator=SAVE).click()
```

**C, after Escape:**

```
STALE_REF | e90 field "Save As:", e96 button "Cancel", e97 button "Save" are no longer in their window
(the screen changed since the snapshot). | hint: Locate elements while their screen is open: before
signing in, closing a sheet or moving on. Take a new snapshot to locate what is shown now.
```

## Bugs and suggestions

1. **Bug (minor): generated Python scripts have no trailing newline.** Saved as-is, they fail ruff
   `W292`, although commit `291944b` says they are ruff-clean.
   Steps: `locate(ref=<any>, formats=["axpilot-python"])`, write `snippets["axpilot-python"]` to
   `x.py`, run `uv run ruff check --config pyproject.toml x.py` → `W292 No newline at end of file`.
   Cause: `export_locators` returns `"\n".join(lines)` (`src/axpilot/perception/locators.py:872`);
   `tests/perception/test_locators.py:671` only checks line lengths.
2. **Suggestion: names that carry a keyboard shortcut.** VS Code's tab is named `Explorer (⇧⌘E)`, and
   the recommended locator matches that name exactly. Rebinding the shortcut breaks the locator.
   `{"name": "Explorer", "name_match": "prefix"}` would survive. Consider offering it when a name ends
   in a parenthesised shortcut.
3. **Suggestion: the no-identifier warning for Electron apps.** It recommends
   `setAccessibilityIdentifier(_:)` / `.accessibilityIdentifier(_:)`, which don't apply to Electron
   or web content.
4. **Cosmetic:** for a nameless element the warning reads "Finder sets no AXIdentifier on button, so
   the locator relies on its name or position" (no name to rely on).
5. **Side observation, not locator-related:** at the start, `list_windows("Finder")` and
   `list_windows("Code")` returned 0 windows although both apps had windows open (a Finder window
   that was already open, 3 VS Code windows). Later calls listed them. Likely cause, not verified:
   the windows were on another Space while the Claude app was in front.
