# Locators for automation scripts

An agent acts on elements by **ref** (`e12`), but a ref only lasts for one session: tomorrow's run
of a script gets different refs. A **locator** names an element by what the app says about it, so a
script can find the same element again in any later session:

```json
{"v": 1, "app": "com.example.MyApp", "role": "AXButton", "identifier": "downloadButton"}
```

Use locators when you want a script, a UI test or a scheduled job for a Mac app, and let your agent
find the right one for you.

## With an agent (VS Code, Cursor, Claude Code…)

Connect AXPilot to your client ([Connect a client](clients.md)), for example
`axpilot connect vscode --scope project` in your project folder, and ask:

> Give me a stable locator for the Download button in MyApp's toolbar, and write a script that
> clicks it.

The agent finds the button (`explore`, `find`), calls `locate` with its ref and gets locators, best
first, each one checked against a fresh capture of the window so that it names **only** that
element:

```
locate e14 button "Download" · MyApp (com.example.MyApp) · window "Library" · snapshot s_9k2p
recommended: {"v": 1, "app": "com.example.MyApp", "role": "AXButton", "identifier": "downloadButton"}  (identifier · high · unique)
alternative: {"v": 1, "app": "com.example.MyApp", "role": "AXButton", "name": "Download"}  (name · medium · unique)
alternative: {"v": 1, "app": "com.example.MyApp", "path": "AXToolbar[0]/AXButton[2]"}  (path · low · unique)
warning: The element is in a toolbar: in a narrow window it can move into the toolbar's overflow menu (»), where this locator no longer finds it.
```

With `formats: ["axpilot-python"]` it also returns a ready script:

```python
from axpilot import Desktop

DOWNLOAD = {'v': 1, 'app': 'com.example.MyApp', 'role': 'AXButton', 'identifier': 'downloadButton'}

with Desktop.connect() as mac:
    mac.find(locator=DOWNLOAD).click()
```

`locate` and `find(locator=…)` are read-only: checking a locator never clicks anything. The agent
calls `locate` only when you ask for locators, a script or a test; ordinary tasks don't compute any.

## A whole flow

> Open MyApp, sign in, export this month's report, and give me all the locators as a script.

The agent does the task screen by screen. On each screen, before acting there, it locates every
element it is about to use in one call, `locate(refs=["e3", "e5", "e9"])`: once you sign in or a
sheet closes, the elements of that screen are gone and can't be checked any more. Each call returns
one entry per element in `items` and, with `formats: ["axpilot-python"]`, one script for those steps
(a secure field reads its text from an environment variable, so no password ends up in the script):

```python
import os

from axpilot import Desktop

EMAIL = {'v': 1, 'app': 'com.example.MyApp', 'role': 'AXTextField', 'identifier': 'login.email'}
PASSWORD = {'v': 1, 'app': 'com.example.MyApp', 'role': 'AXTextField', 'identifier': 'login.password'}
SIGN_IN = {'v': 1, 'app': 'com.example.MyApp', 'role': 'AXButton', 'identifier': 'login.submit'}

with Desktop.connect() as mac:
    mac.find(locator=EMAIL).type_text("your text")
    mac.find(locator=PASSWORD).type_text(os.environ["PASSWORD"])  # a secure field: needs [safety] allow_secure_fields
    mac.find(locator=SIGN_IN).click()
```

At the end, the agent joins the screens' parts into one script in the order of the steps.

## Grades

| Grade | Strategy | Keeps working when… | Breaks when… |
|---|---|---|---|
| `high` | The element's `AXIdentifier` | the window is resized, the layout changes, the app's language changes | the developer renames the identifier |
| `medium` | Role and name, optionally inside its toolbar, sheet, list, named group or table row | the layout changes | the wording or the language of the app changes |
| `low` | Role inside its container, a position among similar elements, or the path in the window | nothing moves | anything is added, removed or reordered |

Identifiers that a toolkit made up (Interface Builder's `_NS:123`, numbers, UUIDs) change between
builds and are graded `low`. When an element has no identifier, the result says so. If you build the
app, give the element one: `setAccessibilityIdentifier(_:)` in AppKit, `.accessibilityIdentifier(_:)`
in SwiftUI.

When a window has more elements than one capture reads, uniqueness can't be proven: `unique` is
`null` and every grade drops by one.

## The format

Every field given must hold (AND). A locator never contains a process id, a window id, a ref, a
coordinate, or the value of a field.

| Field | Meaning |
|---|---|
| `v` | Format version, `1`. |
| `app` | Bundle id (what `locate` writes) or app name. Required. |
| `window` | `{"title": "Library"}` (with `"match": "prefix"` if you like) or `{"index": 1}` (front to back). Leave it out for the app's focused window; `locate` only adds it when the element is in another window. |
| `within` | Ancestors the element is inside, outermost first, each `{role, identifier, name}`. They need not be direct parents. |
| `role` | `AXButton` (what `locate` writes), or a short role as snapshots show it (`button`). |
| `subrole` | An AX subrole such as `AXSearchField`. |
| `identifier` | The `AXIdentifier`, compared exactly (case matters). |
| `name` | The element's name as snapshots show it. Whitespace is collapsed and `...` equals `…`; case matters. `name_match: "prefix"` or `"contains"` for names that carry a count (`"Download (3)"`). |
| `index` | Picks one of the matches, in document order. A last resort. |
| `path` | The position in the window's tree, `"AXToolbar[0]/AXButton[2]"` (as `inspect`'s `locator_path`). |

Unknown fields are refused rather than ignored, so a typo never widens what a locator matches.

## Using locators

- **In an agent:** `find(locator=…)` returns the matching elements with refs, `count` (all matches)
  and `unique`; `scroll: true` scrolls a long list until the element appears. `locate(locator=…)`
  also suggests a better locator when the one you have is weak.
- **In Python:** `mac.find(locator=LOCATOR)` with the [automation SDK](frameworks.md#scripts-without-an-agent).
  `.click()`, `.type_text(…)` and the other element methods act only on a unique match: no match
  raises `ELEMENT_NOT_FOUND`, several raise `LOCATOR_AMBIGUOUS`.
- **From the shell:** `axpilot call find --json '{"locator": {...}}'` prints the matches as JSON.

*Planned:* `mac.locate(locator)` handles that wait for the element, `locator` on the input tools
(so `axpilot call click` takes one), the element under the mouse pointer, and exports to Appium
Mac2, XCUITest and AppleScript. The design is in
[docs/dev/locators-plan.md](https://github.com/vansh3503/Mac-Mcp/blob/main/docs/dev/locators-plan.md).
