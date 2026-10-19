# Permissions

macOS protects the screen, the accessibility tree and other apps with privacy permissions (the TCC
system). AXPilot needs **one** permission to work and asks for the others only when a tool needs
them. AXPilot uses them locally only: it has **no telemetry** and sends nothing anywhere.

| Permission | Needed for | Without it |
|---|---|---|
| **Accessibility** (required) | Reading the UI (`snapshot`, `find`, `inspect`, `list_windows`, `wait_for`), every input tool, menus, app and window control | Tools return `PERMISSION_ACCESSIBILITY` |
| **Screen Recording** (optional) | `screenshot`, `ocr`, `snapshot` with `include_screenshot`, the `computer` profile | Those tools return `PERMISSION_SCREEN_RECORDING`; everything snapshot-based keeps working |
| **Automation** (on demand, per target app) | Tools that send Apple Events: `applescript`, `browser_tabs`, `browser_eval_js`, appearance changes in `system_control` (all opt-in or planned) | macOS asks the first time; if denied, the tool returns `PERMISSION_AUTOMATION` |

Later toolsets (calendars, reminders, contacts; post-1.0) will ask for their own permissions.

## Who receives the permission

macOS checks a permission against the **responsible app** of the process making the call, which is
usually the app that started it, not `python`. That is why AXPilot has two modes.

### Launcher mode *(planned for v0.2)*

`axpilot setup` installs a tiny app, `~/Applications/AXPilot.app`. Clients start the `axpilot stdio`
bridge, which needs no permissions and talks to the AXPilot host over a local socket. The host runs
as a child of `AXPilot.app`, so macOS holds **AXPilot** responsible:

- you grant Accessibility **once, to "AXPilot"**, and Claude, Cursor, VS Code, your terminal and
  scripts can all use it;
- the grant survives AXPilot updates, because the launcher's bytes don't change between releases;
- you never have to grant permissions to a terminal or to an AI client.

This depends on macOS letting a child process use its parent app's grants, which spike S1 is
verifying on macOS 14, 15, 26 and 27 ([ADR-2](adr/0002-architecture-launcher-host-bridge.md)).

### Direct mode

`axpilot stdio --direct` (the only mode today) runs everything inside the process the client starts.
macOS then holds **that client** responsible:

| Started from | Grant the permission to |
|---|---|
| Terminal, iTerm2, Ghostty, … | That terminal app |
| VS Code or Cursor (MCP config or integrated terminal) | Visual Studio Code or Cursor |
| Claude Desktop | Claude |
| Claude Code in a terminal | The terminal app |
| A script or agent framework | Whatever app started the script (usually your terminal or IDE) |

Granting Accessibility to a terminal or an IDE lets **everything you run from it** control your Mac,
not just AXPilot. Prefer launcher mode once it ships, and remove the grant when you no longer need it.

## Granting Accessibility

1. Open **System Settings → Privacy & Security → Accessibility**. (`axpilot setup` and
   `axpilot doctor` will open the right pane for you; *planned*.)
2. Turn on **AXPilot** (launcher mode) or the app from the table above (direct mode). If it isn't
   listed, click **+** and choose it from `~/Applications` or `/Applications`.
3. **Restart** that app. macOS applies a new grant to new processes only. In launcher mode, AXPilot
   restarts its host for you *(planned)*.

Don't add `python` itself. The interpreters uv manages are symlinks, a grant to an interpreter path
breaks when the interpreter changes, and it would apply to every Python script on your Mac.

## Screen Recording

**System Settings → Privacy & Security → Screen & System Audio Recording** (called *Screen Recording*
on macOS 14). Grant it to the same app as Accessibility, then restart that app.

macOS 15 and later periodically ask you to confirm that an app may keep recording the screen. If you
decline, screenshots stop working until you allow it again; snapshot-based tools are unaffected.
AXPilot is designed around the accessibility tree precisely so that Screen Recording can stay
optional. AXPilot's own on-screen indicator and the apps on the deny-list are excluded from its
screenshots *(planned)*.

## Automation (Apple Events)

Some tools control apps through Apple Events (AppleScript). macOS asks the first time a given app is
targeted: *"AXPilot wants access to control Safari"*. You can review the decisions in
**System Settings → Privacy & Security → Automation**.

Running JavaScript in a browser page (`browser_eval_js`) additionally needs the browser's
*Allow JavaScript from Apple Events* setting (Safari: Develop menu; Chrome: View → Developer). All
of these tools belong to the `scripting` or `browser` toolsets, which are **off by default**.

## When the launcher changes

The launcher is ad-hoc signed, so macOS pins the grant to its exact code hash (cdhash). AXPilot keeps
the launcher frozen; a new launcher version is rare and always announced in the changelog. When it
happens, System Settings may still show AXPilot as enabled while macOS no longer honours the grant.
Tools then return `LAUNCHER_CHANGED`, and `axpilot doctor` guides you through a one-time re-grant.
See [ADR-5](adr/0005-signing-and-distribution.md).

## Resetting permissions

`tccutil` clears a permission decision for one app (by bundle id), so macOS asks again:

```sh
tccutil reset Accessibility io.github.vansh3503.axpilot
tccutil reset ScreenCapture io.github.vansh3503.axpilot
tccutil reset AppleEvents   io.github.vansh3503.axpilot
tccutil reset All           io.github.vansh3503.axpilot     # everything for AXPilot
```

For direct mode, use the bundle id of your terminal or client instead (for example
`com.apple.Terminal`; find one with `osascript -e 'id of app "Cursor"'`).

## Secure input

When a password field is focused, or a terminal has *Secure Keyboard Entry* enabled, macOS blocks
synthetic keystrokes for everyone. This is not a permission you can grant: AXPilot detects it and
returns `SECURE_INPUT_ACTIVE`, and the agent should ask you to finish that step. AXPilot also refuses
to type into password fields by default (`allow_secure_fields = false`).
