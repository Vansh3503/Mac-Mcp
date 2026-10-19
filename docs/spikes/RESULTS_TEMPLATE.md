# Spike S&lt;n&gt;: &lt;title&gt; — results

<!--
Copy to docs/spikes/S<n>-<short-name>.md (e.g. S1-launcher-tcc.md) and fill in. One results row
per scenario and per macOS version / machine. Keep it factual: what you ran, what you saw.
Runbook: spikes/README.md. Scenario IDs: the spike's README (S1: spikes/s1_launcher_tcc/MATRIX.md).
-->

| | |
|---|---|
| Spike | S&lt;n&gt; — &lt;question in one line&gt; |
| Date(s) | YYYY-MM-DD … YYYY-MM-DD |
| Tester | |
| Repository commit | `git rev-parse --short HEAD` |
| Launcher build (S1, S5) | version, cdhash arm64 / x86_64 (from `launcher/build/BUILDINFO.txt`) |
| Status | in progress / done |

## Environments

| Machine | Chip | RAM | macOS (build) | Physical / VM | Python (path) | PyObjC | Other versions (Xcode, clients, frameworks) |
|---|---|---|---|---|---|---|---|
| e.g. MacBook Air | M2 | 16 GB | 15.5 (24F74) | physical | 3.12.8 (`uv python find 3.12`) | 12.0 | Xcode 26.0 |

## Results

| ID | Scenario | macOS / machine | Steps (exact command) | Observed | Pass / Fail | Evidence | Notes |
|---|---|---|---|---|---|---|---|
| A1 | fresh grant | 15.5 / M2 | `run_s1.sh --label fresh-grant --prompt --wait-for-trust 120` | dialog named "AXPilot"; trusted after 14 s | Pass | `evidence/S1/…-fresh-grant.json` | |

<!-- Pass / Fail is against the "Expected" column of the scenario list. Use "Blocked" with a reason
when a scenario could not run, and "n.a." when it does not apply to that version. -->

## Measurements (S2, S4; optional elsewhere)

| Target | Metric | p50 | p95 | Unit | Notes |
|---|---|---|---|---|---|
| Finder, 1,200-item list | snapshot walk, batched | | | ms | |

## Findings

- What surprised you, with the evidence.
- Behaviour that differs between macOS versions or architectures.
- Anything users will notice (prompts, wording, delays).

## Decision

- **Decision:** what the project will do, in one or two sentences.
- **Rationale:** the results above that support it.
- **Consequences:** changes to PLAN.md, CONTRACTS.md, defaults, docs or onboarding.
- **Follow-ups:** issues to open, re-tests for future macOS versions.
- **ADR:** `docs/adr/NNNN-<title>.md`

## Go / no-go for ADR-2 (S1 only)

The launcher architecture stands if every box is checked for every supported macOS version
(14, 15, 26 on arm64 and x86_64 where available; 27 on arm64).

- [ ] **A1** The Accessibility prompt names AXPilot, not Python or the terminal.
- [ ] **A2** After one grant, the host process (the probe) is trusted and reads another app's window over AX.
- [ ] **A3** The same holds for Screen Recording.
- [ ] **A4** Without the grant the probe reports `kAXErrorAPIDisabled` (the measurement works).
- [ ] **A5** macOS reports AXPilot.app as the responsible process (or: the API is gone and A1 plus the TCC log show it).
- [ ] **B1–B5** The grant survives interpreter changes, Python upgrades, recreated venvs and reinstalling identical launcher bytes.
- [ ] **B6** A changed launcher loses the grant (cdhash pinning confirmed; `doctor` can detect it).
- [ ] **C1–C5** Starting through `open -g -a` from Claude Desktop, Claude Code, Cursor, VS Code and terminals all pass.
- [ ] **D1, D2, D4** No quarantine and no Gatekeeper prompt for package-manager and command-line installs.
- [ ] **E1–E3** Logout, crashes and quitting behave (no orphaned hosts left running).

| macOS | arm64 | x86_64 | Notes |
|---|---|---|---|
| 14 | | | |
| 15 | | | |
| 26 | | | |
| 27 | | n.a. | |

**Verdict:** GO / GO with changes / NO-GO

- GO: keep ADR-2 as written.
- GO with changes: list them (for example a different install location, a login item, extra usage strings).
- NO-GO: choose ADR-2's fallback (a) a self-contained app bundle with embedded Python
  (PyInstaller or Briefcase), or (b) a small native helper for AX, input and capture; record
  why, and which failed scenario forces it.

**Rationale and residual risks:**
