# Spike S1: launcher and permissions (go/no-go for ADR-2)

**Question.** When the ad-hoc-signed `AXPilot.app` starts the Python host with `posix_spawn`,
does the host get the Accessibility and Screen Recording grants made to "AXPilot", on macOS
14, 15, 26 and 27? Does the grant survive Python and runtime changes, and is there no
quarantine or Gatekeeper prompt when the bundle is installed by a package manager?

If the answer is no, ADR-2 switches to fallback (a), an app bundle with embedded Python, or (b),
a native helper. Everything else in the project depends on this, so run S1 first.

## What is here

| File | Purpose |
|---|---|
| `probe.py` | The measurement. Stdlib + ctypes only, so any Python 3.9+ runs it (no PyObjC). |
| `run_s1.sh` | Builds the launcher, installs it like pip would, points runtime.json at the probe, starts it with `open -g -a`, prints the report. |
| `install.py` | The copy step (contents and mode bits only, no xattrs) and the runtime.json writer. |
| `open_from_client.sh` | Scenario C: the same start, triggered from inside an MCP client. |
| `MATRIX.md` | The scenarios to run on each macOS version, with expected outcomes. |
| `results/` | JSON reports (git-ignored; copy the relevant numbers into `docs/spikes/`). |

## Prerequisites

- A Mac on the macOS version under test, logged in to a normal GUI session. A VM (Tart, UTM) is
  fine for 14/15/26; use real hardware at least once on Apple silicon.
- Xcode Command Line Tools (`xcode-select --install`) to build the launcher.
- Any Python 3.9+ (`/usr/bin/python3` is fine). Nothing to `pip install`.
- This repository checked out. Nothing else from AXPilot is needed.

## Quick start

```sh
# 1. Control: the probe as a child of your terminal (Terminal or iTerm is responsible).
spikes/s1_launcher_tcc/run_s1.sh --direct --label terminal

# 2. First launch through AXPilot.app; the probe asks for Accessibility.
tccutil reset Accessibility io.github.vansh3503.axpilot
spikes/s1_launcher_tcc/run_s1.sh --label fresh-grant --prompt --wait-for-trust 120
#    The system dialog must name "AXPilot". Open System Settings → Privacy & Security →
#    Accessibility, switch AXPilot on; the probe notices within a second.

# 3. Same bytes, new process: the grant must still apply, without any prompt.
spikes/s1_launcher_tcc/run_s1.sh --label regrant-not-needed --skip-build --skip-install
```

`run_s1.sh` prints the verdict and the key facts, then the full report path. A **pass** means:
the probe ran under the launcher, `AXIsProcessTrusted()` is true, a real AX read of the frontmost
window succeeded, macOS reports `AXPilot.app` as the responsible process (when that private API
is available), and the bundle has no `com.apple.quarantine`. Then continue with every row of
[`MATRIX.md`](MATRIX.md).

## Reading a report

| Field | Meaning |
|---|---|
| `summary.verdict` | `pass` / `fail` under the launcher, `control` for direct runs, `inconclusive` if AX could not even be loaded |
| `summary.ax_trusted` | `AXIsProcessTrusted()` in the probe process |
| `summary.ax_read_ok` | at least one AX read of another app succeeded and none returned `kAXErrorAPIDisabled` |
| `accessibility.errors` | the AXError of every read (`-25211` = no Accessibility permission) |
| `accessibility.app_title`, `window_title` | what was read: the proof that access is real |
| `screen_recording.preflight` | `CGPreflightScreenCaptureAccess()` |
| `summary.other_app_window_titles_visible` | other apps' window titles in `CGWindowListCopyWindowInfo`, which requires Screen Recording |
| `responsibility` | `responsibility_get_pid_responsible_for_pid(getpid())` and its executable; `available: false` if the private call is gone |
| `process.parent_chain` | pid, ppid, name and path up to launchd; under the launcher: probe → AXPilot → launchd |
| `process.env` | `AXPILOT_LAUNCHER_*`, `__CFBundleIdentifier`, `XPC_SERVICE_NAME`, `PATH`, … as seen by the host |
| `bundle` | xattrs, quarantine details, code signature per architecture, executable bit of the installed bundle |

## Useful commands

```sh
tccutil reset Accessibility io.github.vansh3503.axpilot    # forget the Accessibility grant
tccutil reset ScreenCapture io.github.vansh3503.axpilot    # forget the Screen Recording grant
tccutil reset All io.github.vansh3503.axpilot              # forget everything for AXPilot
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
xattr -l ~/Applications/AXPilot.app                        # look for com.apple.quarantine
codesign -dvvv ~/Applications/AXPilot.app 2>&1 | grep CDHash
log show --last 5m --predicate 'subsystem == "com.apple.TCC"' --info | grep -i axpilot
tail -f ~/Library/Logs/AXPilot/launcher.log
```

The unified log query is the best evidence of *which* identity tccd evaluated: look for lines
naming `io.github.vansh3503.axpilot` (or your terminal) as the responsible or accessing process.
Save an excerpt with the result when something looks surprising.

## Pitfalls

- If AXPilot is already running, `open` only activates it and drops `--args`; `run_s1.sh` quits
  it first. Do not use `open -n`.
- Running `AXPilot.app/Contents/MacOS/AXPilot` directly from a terminal makes the terminal
  responsible; the launcher logs a warning. Always go through `open`.
- System Settings may keep showing "AXPilot" switched on after the bundle's cdhash changed; the
  grant no longer applies. Remove the entry with "−" or use `tccutil reset`.
- `AXIsProcessTrusted()` can lag for a second after the switch is flipped; `--wait-for-trust`
  polls for that reason.
- Some apps have no focused window (`kAXErrorNoValue`). Put a normal window (TextEdit, Finder)
  in front, or pass a pid with `probe.py --pid`.
