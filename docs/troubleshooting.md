# Troubleshooting

## Start with `axpilot doctor`

```sh
axpilot doctor          # or, from a source checkout: uv run axpilot doctor
```

`doctor` checks the macOS version, permissions (and which app holds them), the launcher's cdhash,
the runtime, the host socket, connected clients and versions, and prints how to fix each problem.
Paste its output into bug reports.

Until `doctor` lands, the checks below cover the same ground.

## The client says the server failed to start

- **"command not found" / `ENOENT`.** GUI apps such as Claude Desktop don't use your shell's `PATH`.
  Put the **absolute path** in the config: `which uv` (from source) or `which axpilot` (installed).
  See [Connect a client](clients.md).
- **It works in a terminal but not in the client.** Run the exact command from the config in a
  terminal. From source it should wait silently for MCP input (press Ctrl-C to stop). If it prints
  an error, fix that first.
- **Wrong directory.** From source, `uv --directory /path/to/axpilot run …` must point at the clone
  (the folder with `pyproject.toml`).
- **Read the client's log.** Claude Desktop: `~/Library/Logs/Claude/mcp*.log`. Claude Code:
  `claude mcp list`, then `/mcp` inside a session. Cursor and VS Code show MCP output in their
  MCP settings or output panels.
- **Try the simulated desktop** to rule out permission problems: replace `--direct` with
  `--engine fake`. If that works, the problem is on the macOS side.
- **`HOST_UNAVAILABLE`: "its host did not open … within 20 s".** The first start after a login
  or an update can be slow. Run `axpilot doctor`; if the host is running now, restart the client
  (or reconnect the server: `/mcp` in Claude Code).

## `PERMISSION_ACCESSIBILITY`

macOS has not granted Accessibility to the app it holds responsible for AXPilot.

- **Direct mode:** grant it to the app that started AXPilot (your terminal, Claude, Cursor or
  VS Code), not to `python`, then **quit and reopen that app**. See
  [Permissions](permissions.md#direct-mode).
- **Launcher mode** *(v0.2)*: grant it to **AXPilot** in System Settings → Privacy & Security →
  Accessibility.
- **It is switched on but still fails.** The grant may belong to an older build of the app. Remove
  the entry with **–**, add the app again, and restart it. Or reset and re-grant:
  `tccutil reset Accessibility <bundle id>`.

## `LAUNCHER_CHANGED`

A new launcher version was installed, so macOS no longer honours the old grant, even if System
Settings still shows AXPilot as enabled. Run `axpilot doctor` and follow its steps, or:

```sh
tccutil reset Accessibility io.github.vansh3503.axpilot
```

then grant Accessibility to AXPilot again. Let AXPilot ask for it (use any tool and switch AXPilot on
from the prompt) rather than adding the app by hand with **+** in System Settings, which may leave the
old decision in place: see [When the launcher changes](permissions.md#when-the-launcher-changes). This
only happens when the changelog announces a new launcher version.

## Screenshots fail with `PERMISSION_SCREEN_RECORDING`

Grant **Screen & System Audio Recording** (macOS 14: *Screen Recording*) to the same app as
Accessibility and restart it. macOS 15 and later re-ask periodically; if you dismissed the prompt,
allow it again in System Settings. Snapshot-based tools work without this permission.

## The snapshot of an app is almost empty

- **Chrome, Electron apps (Slack, VS Code, Discord, …):** they build their accessibility tree only
  on request. AXPilot asks for it (`perception.enhance_electron_chrome = true`, the default); the
  first snapshot after launch can still be thin, so call `snapshot` again after a second.
- **Games, canvases, some cross-platform toolkits:** there is little or no accessibility tree. Use
  `screenshot` and `ocr` with screenshot coordinates.
- **Wrong window:** `snapshot` targets the frontmost app's focused window by default. Pass `app` or
  `window_id` (from `list_windows`).
- **More elements than shown:** the result says `42 of 318 elements shown`. Use `offset`, or `find`.

Please file an [app compatibility report](https://github.com/vansh3503/Mac-Mcp/issues/new?template=app_compat.yml)
for apps that don't work well.

## Error codes during a task

| Code | Usual cause | Fix |
|---|---|---|
| `STALE_REF` | The UI changed since the snapshot (a list scrolled, a dialog opened) | Call `snapshot` again and use the new refs |
| `USER_ACTIVE` | You moved the mouse or typed while the agent was acting | Let go for a moment; the agent retries. To tune it, see `safety.user_idle_threshold_s` |
| `PAUSED_BY_USER` | The kill switch was used *(v0.2)* | Resume from the menu bar when you want the agent to continue |
| `BLOCKED_BY_POLICY` | The app or window is deny-listed, not allowed, or a terminal | Intended. Change `[safety]` or enable `scripting` only if you really want this |
| `SECURE_INPUT_ACTIVE` | A password field is focused, or a terminal has *Secure Keyboard Entry* on | Finish that step yourself, or turn off Secure Keyboard Entry in the terminal's menu |
| `NEEDS_USER` | A system password or authorization dialog is open | Handle the dialog yourself |
| `RATE_LIMITED` | More than `max_actions_per_minute` input actions | The agent should slow down; raise the limit only if you're sure |
| `BUSY` | Another client is in the middle of an input action | Retry; avoid running two agents on the same Mac at once |
| `ELEMENT_NOT_FOUND` | A script's locator matches nothing: the element's window, sheet or menu is not open, or the app changed it | Open it first; check the locator with `locate(locator=…)`, or build a new one with `locate` |
| `LOCATOR_AMBIGUOUS` | A script's locator matches several elements | Build one for the element you mean with `locate` (or add `within`, `name` or `index`) |
| `TOOL_DISABLED` | The tool isn't in the current profile | Add it with `[tools] enable`, or use `--profile full` |
| `INTERNAL` | A bug | Report it with the incident id (`err_…`) and `axpilot doctor` output |

## Typing produces the wrong characters

AXPilot types text as Unicode, independent of your keyboard layout. Shortcuts (`press_keys`) are
mapped through the current layout, so `cmd+z` is the key labelled Z on AZERTY too. If a shortcut
goes to the wrong key, report your layout (System Settings → Keyboard → Text Input) in a bug.

## Clicks land in the wrong place

- Coordinates are screen **points**, not pixels. Coordinates read off a screenshot must be passed
  with `coordinate_space: "screenshot"` and that screenshot's `screenshot_id`.
- With several displays, displays left of or above the main one have negative coordinates.
- Prefer refs over coordinates whenever the element appears in a snapshot.

## Where the logs are

| File | Contents |
|---|---|
| `~/Library/Logs/AXPilot/axpilot.log` | Host and server log, including tracebacks for `INTERNAL` errors |
| `~/Library/Logs/AXPilot/launcher.log` | Launcher events and the host's startup output *(v0.2)* |
| `~/Library/Logs/AXPilot/audit.jsonl` | One line per tool call, arguments redacted by default |

## Inspecting the server by hand

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) lists the tools and lets you
call them interactively:

```sh
npx @modelcontextprotocol/inspector uv run axpilot stdio --engine fake     # simulated desktop
npx @modelcontextprotocol/inspector uv run axpilot stdio --direct          # your Mac
```

For accessibility details of a specific app, Apple's **Accessibility Inspector** (Xcode → Open
Developer Tool) shows the same tree AXPilot reads.

## Still stuck?

Search the [issues](https://github.com/vansh3503/Mac-Mcp/issues), ask in
[Discussions](https://github.com/vansh3503/Mac-Mcp/discussions), or open a
[bug report](https://github.com/vansh3503/Mac-Mcp/issues/new?template=bug_report.yml).
