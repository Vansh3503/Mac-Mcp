# Spike S1: launcher and permissions — results

| | |
|---|---|
| Spike | S1 — does a child of the ad-hoc-signed `AXPilot.app` get its own TCC grants, and keep them across Python changes? |
| Date(s) | 2026-10-20 |
| Tester | Vansh Malhotra (owner), with a coding session driving the scripts |
| Repository commit | `d756272` (launcher sources), runs on `feature/v1-mac-verification` |
| Launcher build (S1, S5) | launcher 1.0.0, built locally: cdhash arm64 `ebedf54c30e860dbdf557417c2cf0a0df642c60a` / x86_64 `600d0ea39efd2edc5c0ca4b1472693e5974ee605` (CI's macos-14 build gives `ca3fde20…` / `560c01f5…`: another toolchain, see Findings) |
| Status | in progress: macOS 26 arm64 done for A, B and D1/D4; C and E partly; other macOS versions not run |

## Environments

| Machine | Chip | RAM | macOS (build) | Physical / VM | Python (path) | PyObjC | Other versions (Xcode, clients, frameworks) |
|---|---|---|---|---|---|---|---|
| MacBook Pro (MacBookPro18,3), MDM-managed | M1 Pro | 16 GB | 26.6.2 (25G83) | physical | probe: 3.14.6 (`/opt/homebrew/bin/python3`); also 3.9.6, 3.12.13, 3.13.14 | n/a (the probe is stdlib + ctypes) | Command Line Tools 26.5 (Swift 6.3.2, SDK macosx26.5), no full Xcode; Claude desktop 2.37337.1 with Claude Code 2.1.295 |

## Results

`R` is `spikes/s1_launcher_tcc/run_s1.sh`. Every launcher run was started from a Claude Code session
(Claude desktop app, Code tab) through its shell tool, so the "client" for all rows below is
Claude Code. Reports are in the git-ignored `spikes/s1_launcher_tcc/results/` on the test Mac.

| ID | Scenario | macOS / machine | Steps (exact command) | Observed | Pass / Fail | Notes |
|---|---|---|---|---|---|---|
| A0 | control | 26.6.2 / M1 Pro | `R --direct --label terminal` | responsible process: Claude Code's own binary, `~/Library/Application Support/Claude/claude-code/2.1.295/<hash>/claude.app/Contents/MacOS/claude`; trusted, Screen Recording granted | Pass (control) | the responsible path changes with every Claude Code version: the problem ADR-2 solves |
| A1 | fresh grant | 26.6.2 / M1 Pro | `R --label fresh-grant --prompt --wait-for-trust 120` | the system dialog named **AXPilot**; after the owner switched it on, AX reads of another app succeeded, but `AXIsProcessTrusted()` stayed `false` for the whole 120 s | Pass (dialog, grant applies) | the stale `AXIsProcessTrusted()` is explained under Findings |
| A2 | second start | 26.6.2 / M1 Pro | `R --label second-start --skip-build --skip-install` | trusted, AX read of another app OK, no prompt | Pass | |
| A3 | Screen Recording | 26.6.2 / M1 Pro | `R --label a3-screen-grant --request-screen --skip-build --skip-install`, allow, then `R --label a3-screen-after --skip-build --skip-install` | the dialog named **AXPilot**; the next process has `CGPreflightScreenCaptureAccess()` true and sees other apps' window titles | Pass | the grant applies to the next process, as MATRIX.md expects |
| A4 | denied stays denied | 26.6.2 / M1 Pro | owner: `tccutil reset Accessibility io.github.vansh3503.axpilot`; `R --label a4-no-grant --skip-build --skip-install` | verdict `fail`, every read `-25211` (`kAXErrorAPIDisabled`) | Pass | |
| A5 | responsibility API | 26.6.2 / M1 Pro | every launcher run | `responsibility_get_pid_responsible_for_pid` available; responsible = `~/Applications/AXPilot.app/Contents/MacOS/AXPilot`; tccd's log names `io.github.vansh3503.axpilot` as responsible and checks the cdhash requirement | Pass | |
| B1 | other interpreter | 26.6.2 / M1 Pro | `R … --python /usr/bin/python3` (3.9.6) and a uv venv's 3.12 | pass, no prompt | Pass | |
| B2 | Python upgrade | 26.6.2 / M1 Pro | `R … --python "$(uv python find 3.13)"` | pass, no prompt | Pass | |
| B3 | venv recreated | 26.6.2 / M1 Pro | `uv venv --python 3.12 <scratch>/axp-rt`; `R … --python <scratch>/axp-rt/bin/python` | pass, no prompt | Pass | a real `uv tool install --reinstall axpilot` needs the PyPI release |
| B4 | symlinked interpreter | 26.6.2 / M1 Pro | the venv's `bin/python` (a symlink) and its target | both pass | Pass | |
| B5 | identical bytes reinstalled | 26.6.2 / M1 Pro | `R --label b5-same-bytes-reinstalled --skip-build` | pass, no prompt | Pass | |
| B6 | launcher changed | 26.6.2 / M1 Pro | copy of `launcher/` with `CFBundleVersion` `1.0.0.b6`, built to a scratch folder, installed with `install.py bundle`; `R --label b6-launcher-changed --skip-build --skip-install` | new cdhash `e192a44d…`: reads `-25211`; reinstalling the original bytes (`R --label b6-original-restored --skip-build`) brings the grant back with no prompt | Pass (fails as expected, then recovers) | the grant is pinned to the cdhash; the entry survives a mismatching build |
| C2 | Claude Code | 26.6.2 / M1 Pro | all runs above, from Claude Code's shell tool | launcher runs pass | Pass | Claude Code inside the Claude desktop app; the CLI in a terminal tab not run separately |
| C1, C3–C6 | other clients | 26.6.2 / M1 Pro | | | Not run yet | Cursor and iTerm2 are not installed; Claude Desktop and VS Code are covered by the end-to-end client checks |
| D1 | plain copy | 26.6.2 / M1 Pro | `install.py bundle` (what `run_s1.sh` does) | no `com.apple.quarantine` (only `com.apple.provenance`); no Gatekeeper dialog | Pass | |
| D4 | `curl` + `ditto` | 26.6.2 / M1 Pro | `curl -LO http://127.0.0.1:8766/AXPilot.app.zip && ditto -x -k AXPilot.app.zip out` | no quarantine on the zip or the bundle; `codesign --verify --strict` passes; same cdhash | Pass | |
| D2, D3, D5 | wheel, browser download, Homebrew | | | | Not run yet | D2 needs a built wheel and `axpilot setup` |
| E1–E3 | logout, `kill -9`, quit | | | | Not run yet | run with the real host |

## Findings

- **The design works on macOS 26.6.** The prompt names AXPilot, the grant goes to the
  launcher's child, it survives every interpreter change tried (system 3.9, Homebrew 3.14, uv 3.12
  and 3.13, venvs and their symlinks), and a launcher with a different cdhash loses it.
- **`AXIsProcessTrusted()` only updates while the main run loop runs.** In a process that never
  turns its main run loop (the plain probe), the call kept returning `false` after the grant, while
  real AX reads already worked (seen in A1, and again in a second-by-second poll). The same poll
  with `CFRunLoopRunInMode` between samples saw it flip to `true` in the same second as the first
  successful read and tccd's "Allowed" record. AXPilot's CLI, SDK and host pump the main run loop
  (`axpilot.runloop`, every 50 ms), so their permission checks see a new grant without a restart;
  any other long-running process that calls it must pump too, or re-check in a new process.
- **System Settings can toggle twice.** During the re-grant, tccd logged "Allowed" and then
  "Denied" 1.3 s later (both "System Set"), and the switch then showed on while the record said
  denied. Switching it off and on again fixed it. Worth a line in the troubleshooting guide: if
  AXPilot is switched on but still refused, switch it off and on once.
- **The cdhash depends on the toolchain.** The same `launcher/` sources give `ebedf54c…`
  (arm64) with Command Line Tools 26.5 and `ca3fde20…` with CI's macos-14 Xcode. Users must get the
  one bundle `launcher.yml` builds, as `launcher/README.md` says; never rebuild it on install.
- **Grants of the responsible app before AXPilot.** Run directly (the control), the responsible app
  is Claude Code's versioned binary under `~/Library/Application Support/Claude/claude-code/<version>/`,
  so direct mode's grant is tied to one Claude Code version.

## Decision

- **Decision:** keep ADR-2's frozen launcher on macOS 26 (arm64). Not yet decided for macOS 14, 15
  and 27 or Intel: no such machine was available.
- **Rationale:** A1–A5, B1–B6, D1 and D4 pass as expected on macOS 26.6.2.
- **Consequences:** docs: the exact prompts, "switch it off and on" and "restart after an update"
  in `docs/permissions.md` and `docs/troubleshooting.md`.
- **Follow-ups:** the same rows on macOS 14, 15 and 27 and on an Intel Mac; C1, C3–C6, D2, D3, D5
  and E1–E3; the host should exit when its launcher dies (`os.getppid()` becomes 1; build-1
  follow-up, not implemented).
- **ADR:** [ADR-0002](../adr/0002-architecture-launcher-host-bridge.md)

## Go / no-go for ADR-2 (S1 only)

- [x] **A1** The Accessibility prompt names AXPilot, not Python or the terminal. *(macOS 26 arm64)*
- [x] **A2** After one grant, the host process (the probe) is trusted and reads another app's window over AX. *(macOS 26 arm64)*
- [x] **A3** The same holds for Screen Recording. *(macOS 26 arm64)*
- [x] **A4** Without the grant the probe reports `kAXErrorAPIDisabled` (the measurement works). *(macOS 26 arm64)*
- [x] **A5** macOS reports AXPilot.app as the responsible process. *(macOS 26 arm64)*
- [x] **B1–B5** The grant survives interpreter changes, Python upgrades, recreated venvs and reinstalling identical launcher bytes. *(macOS 26 arm64)*
- [x] **B6** A changed launcher loses the grant (cdhash pinning confirmed). *(macOS 26 arm64)*
- [ ] **C1–C5** Starting through `open -g -a` from Claude Desktop, Claude Code, Cursor, VS Code and terminals all pass. *(C2 only so far)*
- [ ] **D1, D2, D4** No quarantine and no Gatekeeper prompt for package-manager and command-line installs. *(D1, D4 pass; D2 open)*
- [ ] **E1–E3** Logout, crashes and quitting behave (no orphaned hosts left running).

| macOS | arm64 | x86_64 | Notes |
|---|---|---|---|
| 14 | not run | not run | |
| 15 | not run | not run | |
| 26 | GO for A, B, D1, D4 (26.6.2) | not run | C, D2 and E open |
| 27 | not run | n.a. | |

**Verdict:** GO on macOS 26 arm64 for the core question (grants attributed to AXPilot and kept
across Python changes); the full verdict waits for the other versions and the open rows.

**Rationale and residual risks:** macOS 14/15 and Intel are untested; tccd's behaviour has changed
between releases before. The double toggle in System Settings is a usability risk, not a design one.
