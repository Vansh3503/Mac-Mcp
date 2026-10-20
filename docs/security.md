# Security model

AXPilot lets software click, type and read on your Mac with your permissions. This page explains
what it protects against, its default controls, and their limits. To report a vulnerability, follow
[SECURITY.md](https://github.com/vansh3503/Mac-Mcp/blob/main/SECURITY.md) (privately, never in a
public issue).

> **Status:** pre-alpha. The policy layer (deny-lists, Terminal rule, secure fields, confirmations,
> rate limit, audit, yield-to-user) is being built first, for v0.1. Controls that belong to the host
> app (kill switch, indicator, client pairing) arrive with v0.2, and the HTTP transport and release
> hardening with v0.2 and v0.3. Each control below is marked accordingly.

## Principles

- **Local only.** AXPilot runs on your Mac, as you. It has **no telemetry**, no accounts and no
  cloud component ([ADR-6](adr/0006-privacy-no-telemetry.md)). Snapshots and screenshots are kept
  in memory for a short time so refs can be resolved, and go only to the client that asked.
- **Safe by default.** Every capability with a large blast radius (shell, AppleScript, browser
  JavaScript, typing into terminals) is off until you turn it on.
- **Least surprise.** AXPilot shows when an agent is in control *(v0.2)*, yields to you when you
  touch the mouse or keyboard, and asks before destructive actions.
- **Honest limits.** AXPilot is a set of guard rails around a powerful capability, not a sandbox.

## Threat model

| # | Threat | Mitigations | Status |
|---|---|---|---|
| 1 | **Prompt injection from screen content.** A web page, email or document contains text that tries to steer the agent ("ignore your instructions and…"). | Risky toolsets off by default; confirmations for destructive actions; app and window deny-lists; Terminal rule; rate limit; audit log; server `instructions` and the Agent Skill tell agents that screen content is untrusted. | v0.1 |
| 2 | **Confused deputy / local TCC bypass.** Another local process uses AXPilot's permissions through its socket or HTTP port. | Unix socket with mode `0600` and a same-uid peer check; **client pairing**: the first connection from a new client app asks *"Allow Claude to see and control this Mac through AXPilot?"* (Allow or Deny, remembered per app); the HTTP token is kept in the login keychain (a private `0600` file where the keychain can't be used). | v0.2 |
| 3 | **Runaway agent.** An agent loops, acts too fast, or keeps going after you want it to stop. | Rate limit (120 input actions/min); yield-to-user; confirmations; **kill switch** (⌃⌥⌘. or menu-bar *Stop*: cancels in-flight work and pauses, so tools return `PAUSED_BY_USER`); Trash rather than delete. | Rate limit and yield v0.1; kill switch v0.2 |
| 4 | **Data exfiltration.** Reading secrets through the UI, screenshots or the clipboard. | Secure-field values are never returned; deny-listed apps are excluded from snapshots and screenshots, and deny-listed windows from snapshots and window captures; clipboard reads are audited and can be disabled; audit log redacts typed text. | v0.1 |
| 5 | **Network exposure.** The optional HTTP transport is reached by other machines or by web pages (DNS rebinding). | Binds to `127.0.0.1`; bearer token required; `Origin` and `Host` checked; no CORS. Remote access needs `--allow-remote` plus TLS; SSH or Tailscale are recommended instead. | v0.2 |
| 6 | **Supply chain and launcher integrity.** A tampered package, dependency or launcher. | PyPI trusted publishing with attestations; locked dependencies (`uv.lock`); Dependabot; code scanning; a published cdhash per launcher version, verified by `axpilot doctor`. The launcher is ad-hoc signed, so there is no signing key to leak. | v0.3 |

Client pairing **raises the bar but is not a hard boundary** against malware that already runs as
your user: such code can read your files and could, for example, rewrite the runtime AXPilot starts.
macOS itself does not separate processes of the same user from each other.

## Policy rules

Before every tool call that touches an app, the policy layer checks, in order (the first match wins):

1. **AXPilot's own windows** can always be read and never controlled, so an agent can't approve its
   own confirmation dialogs or change AXPilot's settings.
2. **System authentication dialogs** (password prompts) are never operated: `NEEDS_USER`.
3. **`deny_apps`** blocks matching apps for reading, input and capture. Default: Keychain Access,
   Passwords, 1Password, Bitwarden, LastPass, Dashlane, KeePassXC.
4. **`allow_apps`**, when non-empty, blocks every other app.
5. **`deny_windows`** blocks single windows of otherwise allowed apps. Default: the Privacy & Security
   pane of System Settings, so an agent can't grant itself or others permissions.
6. **Terminal rule:** typing into Terminal, iTerm2, Ghostty, Warp, kitty, Alacritty, WezTerm, Hyper
   or Tabby is blocked unless the `shell` tool is enabled, because it is equivalent to shell access.
   For the same reason `open` won't run code without it: files the Mac would execute (`.command`,
   `.tool`, `.terminal`, `.jar`, executables) and anything opened with a terminal app are blocked,
   and URL files (`.webloc`…) follow the same rule as app URL schemes.

Coordinate clicks are **hit-tested** first: AXPilot finds the app and window under the point and
applies the same rules, so a click can't land on a deny-listed app by coordinates.

**Secure fields** (password fields) never return their values, in any mode. Typing into them is
refused unless `allow_secure_fields = true`, whether the agent names the field or types into
whatever has focus (`type_text` without a ref, characters sent with `press_keys` or `computer`).
When macOS's Secure Event Input is on, synthetic keystrokes are impossible anyway
(`SECURE_INPUT_ACTIVE`).

Policy errors name the app and the rule, and say how *the user* can change it. Agents are told not
to work around a `BLOCKED_BY_POLICY` result.

## Confirmations

With `safety.confirm = "destructive"` (the default), AXPilot asks you before running `shell`,
`applescript`, `browser_eval_js` or a shortcut, force-quitting an app, writing `defaults`, or killing
a process. The question shows exactly what will run: the command line, the script, the page and its
JavaScript, the shortcut and its input, or the `defaults write` command (long ones are shortened,
with a note of how much is hidden). AXPilot validates the call and applies the policy first, so you
are never asked about something that would be refused anyway.

Scripts are checked against the app rules on a best-effort basis: apps an AppleScript or JXA script
names literally (`tell application "Notes"`) must be allowed (`deny_apps`, `allow_apps`, and the
Terminal rule), and a System Events script must be allowed for the frontmost app. A script can
build names at run time, so this guards against mistakes, not against a determined script: enable
`applescript` and `shell` only when you trust the agent with what they can reach.

- In launcher mode the host shows a **native dialog**, which works with every client *(v0.2)*.
- In direct mode (`axpilot stdio --direct`, `--engine fake`, `axpilot http`), AXPilot asks in the
  **MCP client's own window** when the client supports elicitation (`safety.elicitation`, on by
  default): a form with one *Allow* checkbox. Only *accept* with *Allow* checked approves. On the
  stateless 2026-07-28 protocol the call ends with the question and the client calls again with your
  answer; that answer counts once, only for the same call (same tool, same arguments, same question)
  and only within `confirm_timeout_s`. A client without elicitation, or one that fails to show the
  question, falls back to the dialog (on a Mac) or to "no". `--yes` approves without asking.
- With no way to ask, the answer is **no** (`USER_DENIED`). Unanswered confirmations time out after
  120 seconds, also as `USER_DENIED`.
- While a question is on screen, every tool that acts (clicks, typing, scrolling, app and window
  changes, scripts…) waits with `NEEDS_USER`, for every connected client, so an agent can't answer
  the dialog itself with a parallel tool call. Reading tools keep working. This includes a question
  shown in the MCP client, which is itself a window on the same Mac.
- Elicitation is only as trustworthy as the client: a client may show the question to you, or
  answer it by itself (some agent frameworks do). Use clients that show it, or turn
  `safety.elicitation` off to keep the dialog or the safe "no".

`confirm = "always"` asks before every call except the read-only `observe` tools; `"never"`
disables confirmations and is only appropriate in a disposable environment such as a VM.

## Yield to user and the kill switch

Before each input action AXPilot checks how long the hardware mouse and keyboard have been idle. If
you used them within the last second (`safety.user_idle_threshold_s`), the action is refused with
`USER_ACTIVE`, so you and the agent don't fight over the cursor. Input actions are serialized: one
"virtual hand" at a time.

The kill switch *(v0.2)*, **⌃⌥⌘.** or *Stop* in the menu bar, cancels in-flight work and pauses
AXPilot until you resume it. While paused, every tool that changes something returns
`PAUSED_BY_USER`; reading tools keep working.

## Audit log

Every tool call is appended as one JSON line to `~/Library/Logs/AXPilot/audit.jsonl` (mode `0600`):
timestamp (UTC), tool, client, arguments, outcome, error code and duration. With the default
`log_args = "redacted"`, the values of `text`, `value`, `values`, `source`, `script`, `command`,
`input`, `js` and `keys` (and Windows-MCP's `shortcut`, `labels` and `locs`, and clipboard images) are
replaced by `<redacted N chars>`, wherever they appear in the arguments. Use `none` to log no arguments, or `full`
only when debugging. A failure to write the audit log never fails a tool call.

## Recommendations

- Keep `scripting` and `browser` off unless a task needs them, and prefer enabling single tools
  (`enable = ["shortcuts"]`) over whole toolsets.
- For focused automations, set `allow_apps` to just the apps involved.
- Add your own sensitive apps (banking, messaging, HR tools) to `deny_apps`.
- Run untrusted or long unattended agents in a macOS virtual machine.
- Review `audit.jsonl` after unattended runs.
- In direct mode, remember that the Accessibility grant belongs to your terminal or client app and
  applies to everything it runs.

## Known limitations

- AXPilot sees what apps expose through accessibility. An app that shows a secret in an ordinary
  text element (not a secure field) exposes it to snapshots and screenshots, unless the app or
  window is deny-listed.
- Deny-lists match bundle ids and names. A renamed or re-bundled app is not covered until you add it.
- Screenshots and OCR capture whatever is visible in allowed apps, including notifications from
  other apps that overlap them. A deny-listed *window* (as opposed to app) is blocked from snapshots
  and window captures, but can still appear in a full-display screenshot.
- Confirmations protect against unexpected actions, not against a user approving a malicious one.
  Read the confirmation text.
