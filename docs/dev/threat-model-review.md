# Threat-model review (PLAN §7)

Review of the six threats in [PLAN §7](../PLAN.md#7-security-and-safety-model) against the code on
`feature/axpilot-foundation` (since merged into `main`), for issue #12 (Phase 5). For each mitigation the plan or
[the security model](../security.md) promises, the review found the code that implements it and the
tests that prove it, then tried to break it. Everything here ran on Linux with the simulated desktop;
what only a Mac can confirm is listed at the end.

**Result:** the six threats are mitigated as documented, with the limits the security page already
states. The review found and fixed three defects (R1–R3), all with regression tests, and records
five open gaps (G1–G5) that are documented limits or later-phase work, not code defects.

## Method

- **Trace every promise to code and tests.** Each row of the threat table in `docs/security.md` and
  each default in PLAN §7.2 is mapped below to the module that enforces it and the tests that fail
  if it breaks.
- **Argument fuzzing** (`tests/server/test_fuzz.py`). Every tool under both namings (83
  configurations) is called with arguments generated from the input schema it advertises, plus
  arguments with one fault (unknown key, missing required key, wrong type, value out of range).
  Every call must end in a success or a coded error, never `INTERNAL` or a traceback, with the
  input lock free afterwards. Each example runs on a fresh simulated desktop with a virtual clock,
  so whole timeout and duration ranges are covered; stopping a process is the one action that is
  declined, because it would signal a real pid. CI runs 10 examples per tool with a fixed seed;
  `AXPILOT_FUZZ_EXAMPLES=200` gives a deeper, randomly seeded run (about 16,600 calls).
- **Safety evals.** The eval suite (`python -m axpilot.evals`) has three tasks whose goal is a
  harmful request: copy a password out of 1Password, grant Terminal Accessibility in Privacy &
  Security, and run `rm -rf ~/Documents/*` in Terminal. Their checks must hold before and after
  the reference attempt.

## Threats

### 1. Prompt injection from screen content

| Mitigation | Code | Tests |
|---|---|---|
| Risky toolsets off by default (`standard` = observe, input, apps, clipboard) | `policy/profiles.py` | `tests/policy/test_profiles.py`, golden files in `tests/server/golden/` |
| Confirmations before destructive actions, showing exactly what runs | `server/guard.py`, `policy/confirm.py`, `server/elicit.py` | `tests/server/test_guard.py`, `tests/server/test_elicitation.py`, `tests/policy/test_confirm.py` |
| Deny-lists, authentication dialogs, AXPilot's own windows, Terminal rule | `policy/rules.py` | `tests/policy/test_rules.py`, `tests/server/test_safety.py` |
| Coordinate targets, drop points and scroll steps hit-tested against the same rules | `server/tools/input.py`, `server/tools/navigate.py` | `tests/server/test_safety.py`, `tests/server/tools/test_input.py` |
| No tool acts while AXPilot is asking the user (`NEEDS_USER`) | `policy/control.py` | `tests/server/test_safety.py`, `tests/policy/test_control.py` |
| Agents told that screen content is untrusted | `server/instructions.py`, `skills/axpilot/SKILL.md` | `tests/server/test_app.py` (instructions) |

**Verdict: holds** on the simulated desktop, and the three safety evals hold. Script checks
(`applescript`, `shell`) are best effort, as `docs/security.md` says.

### 2. Confused deputy / local TCC bypass

| Mitigation | Code | Tests |
|---|---|---|
| Socket `0600` in a `0700` folder, stale-socket cleanup, one host per user | `host/daemon.py` | `tests/host/test_daemon.py` |
| Same-user peer check (`SO_PEERCRED`, `LOCAL_PEERCRED`) | `host/daemon.py` (`peer_uid`) | `tests/host/test_daemon.py` |
| Client pairing (first connection per client app asks the user) | `host/pairing.py`, `host/daemon.py` | `tests/host/test_pairing.py`, `tests/host/test_daemon.py` |
| HTTP token in the login keychain, `0600` file as fallback | `system/keychain.py`, `host/http.py` | `tests/system/test_keychain.py`, `tests/host/test_http.py` |

**Verdict: holds**, with finding R3: the test of the peer check also passed when the check returned
"unknown", so CI could not have noticed a peer check that silently does nothing on macOS. Pairing
is not a boundary against malware running as the same user; the security page says so.

### 3. Runaway agent

| Mitigation | Code | Tests |
|---|---|---|
| Rate limit (120 input actions per minute, one host-wide budget) | `policy/ratelimit.py`, `server/guard.py` | `tests/policy/test_ratelimit.py`, `tests/server/test_guard.py` |
| Yield to user (`USER_ACTIVE`), one input action at a time | `policy/control.py` | `tests/policy/test_control.py`, `tests/server/test_guard.py` |
| Kill switch (⌃⌥⌘P) and indicator | `host/killswitch.py`, `host/ui/` | `tests/host/test_killswitch.py` |
| Input never retried automatically; abandoned presses are not repeated | `server/tools/input.py` | `tests/server/tools/test_input.py` |
| "Trash rather than delete" | no tool deletes files; `handle_dialog` asks before pressing a delete, discard or overwrite button | `tests/server/tools/test_controls.py` |

**Verdict: holds** in its Linux-testable parts. The kill switch's hotkey and overlay are
macOS-only and have not run on a Mac.

### 4. Data exfiltration

| Mitigation | Code | Tests |
|---|---|---|
| Secure-field values never returned; typing into them refused by default | `perception/snapshot.py`, `policy/rules.py`, `server/tools/input.py` | `tests/server/tools/test_input.py`, `tests/policy/test_rules.py` |
| Deny-listed apps left out of snapshots and display captures | `server/tools/observe.py`, `engine/macos/capture.py` | `tests/server/tools/test_observe.py`, `tests/engine/test_fake.py` |
| Deny-listed windows left out of snapshots and window captures | `policy/rules.py`, `server/tools/observe.py` | `tests/policy/test_rules.py`, `tests/server/tools/test_observe.py` |
| Clipboard reads audited; the `clipboard` tool can be disabled | `server/tools/system.py`, `policy/audit.py` | `tests/server/tools/test_system.py`, `tests/policy/test_audit.py` |
| Typed text, scripts, commands and keys redacted in the audit log | `policy/audit.py` | `tests/policy/test_audit.py`, `tests/server/test_safety.py` |
| Keystrokes reach only the app the policy checked | `server/tools/input.py` | `tests/server/tools/test_input.py` |

**Verdict: holds, with gaps G1 and G2.** The last row is where R1 was found.

### 5. Network exposure

| Mitigation | Code | Tests |
|---|---|---|
| Binds to `127.0.0.1`; other addresses need `--allow-remote` plus a TLS certificate and key | `host/http.py` (`check_bind`) | `tests/host/test_http.py` |
| Bearer token; `Host` and `Origin` checked before the token; no CORS headers | `host/http.py` | `tests/host/test_http.py` |

**Verdict: holds.**

### 6. Supply chain and launcher integrity

| Mitigation | Status |
|---|---|
| Locked dependencies (`uv.lock`) | In place; CI installs with `uv sync --locked`. |
| Dependabot (uv and GitHub Actions, weekly) | In place (`.github/dependabot.yml`). |
| PyPI trusted publishing with attestations | In place since 0.1.0a1 (`release.yml`, environment `pypi`). |
| Code scanning | Not configured. See G4. |
| Published launcher cdhash, checked by `axpilot doctor` | The check exists (`doctor.py`); the row for `launcher-v1` (1.0.0) is pinned in `launcher/CDHASH.md`, and `release.yml` refuses a launcher that differs from it. |

**Verdict: in place except code scanning** (see G4).

## Findings fixed in this review

| # | Severity | Finding | Fix |
|---|---|---|---|
| R1 | High | `type_text` brought the target's app to the front but never checked that it got there. macOS may ignore an activation request from a process in the background, and the keystrokes then went to whatever app was in front, not to the field the policy had checked. (`press_keys` and the open/save panel path already checked.) | `9eceb42`: no keys are sent unless the target's app is frontmost (`ELEMENT_NOT_ACTIONABLE`). Earlier the same day, `347cae8` fixed the common case, where the app was never activated at all. |
| R2 | Low | `processes kill` with a pid of 2³¹ or more ended in `INTERNAL`: `os.kill` raises `OverflowError`, not `OSError`, for ids a `pid_t` can't hold. Found by the deep fuzz run. | `848db3c`: such pids are `INVALID_ARGUMENT`; nothing is signalled. |
| R3 | Medium (test gap) | `test_peer_uid_is_the_current_user` accepted `None`, so a peer check that silently does nothing on macOS would have passed CI. | The test now requires the real uid on Linux and macOS, so CI's macOS jobs verify `LOCAL_PEERCRED` the next time CI runs on this branch. |

## Open gaps

| # | Gap | Why it stays | Suggested follow-up |
|---|---|---|---|
| G1 | Full-display screenshots and display OCR leave out deny-listed **apps** but not deny-listed **windows** (the Privacy & Security pane of System Settings shows in a display capture). | Documented: the security page promises window exclusion for snapshots and window captures only. ScreenCaptureKit can exclude windows, but that needs a Mac to build and verify. | Exclude deny-listed windows in `engine/macos/capture.py` (`SCContentFilter` with excepted windows) once spike S4 runs. |
| G2 | Disabling the `clipboard` tool does not stop an agent from reading the clipboard: it can paste (⌘V) into a field and read the field. | Inherent to keyboard access; only `allow_apps`/`deny_apps` limit where it can paste. | Now stated in the threat table of [the security model](../security.md). |
| G3 | Where neither `SO_PEERCRED` nor `LOCAL_PEERCRED` works, the peer check allows the connection and relies on the socket's `0600` mode. | By design (logged as a warning); on macOS R3's test now proves the check works. | None. |
| G4 | Trusted publishing, attestations, code scanning and the pinned launcher cdhash are not in place. | Done in 0.1.0a1 except code scanning: PyPI trusted publishing with attestations, the launcher's build-provenance attestation, and the pinned cdhash (the release workflow refuses a pending `launcher/CDHASH.md` row). | Add code scanning. |
| G5 | Unknown argument names are ignored, although every schema says `additionalProperties: false`. | Not a safety issue (a misspelt optional argument falls back to its default); clients that validate locally refuse them anyway. | Decide before freezing the 1.x API whether the server should refuse them with `INVALID_ARGUMENT`. |

## Fuzzing results

- CI configuration (10 examples per tool, fixed seed): 83 configurations pass, about 25 s.
- Deep runs (`AXPILOT_FUZZ_EXAMPLES=200`, random seed): the first run found R2; after the fix,
  a second run passed every configuration (90 tests, about 16,600 calls, 6 minutes).
- A run with `HOME` pointed at an empty folder leaves it empty.

## Not verified without a Mac

Everything in `engine/macos` and `host/ui`: whether activation from the background is ignored in
practice (R1's premise), ScreenCaptureKit exclusion of deny-listed apps, the kill-switch hotkey and
overlay, the native confirmation and pairing dialogs, `IsSecureEventInputEnabled`, and parent-app
identification for pairing. These belong to the spikes in `spikes/README.md` (issue #1) and the
macOS validation task of #12.
