# Phase-0 spikes: maintainer runbook

Six time-boxed experiments (PLAN.md §15, Phase 0) that confirm or overturn the plan's riskiest
assumptions before the engine is built. Each ends in a results file under `docs/spikes/` and an
ADR in `docs/adr/`. **Run S1 first: it is the go/no-go for the whole launcher architecture
(ADR-2).** S5 and S6 can run in parallel with it; S2 to S4 need a Mac with PyObjC.

| Order | Spike | Question | Needs | Time-box |
|---|---|---|---|---|
| 1 | [S1 launcher and permissions](s1_launcher_tcc/README.md) | Does a child of the ad-hoc-signed `AXPilot.app` get its TCC grants on macOS 14/15/26/27, across Python changes, without quarantine? | Mac per macOS version, Xcode CLT, any Python 3.9+ | 3 days |
| 2 | [S5 CI permissions](s5_ci_permissions/README.md) | Can GitHub's macOS runners grant Accessibility, to Python and to the launcher? | a branch with the workflow template | 1 day |
| 3 | [S6 MCP interop](s6_mcp_interop/README.md) | Do Claude Desktop/Code, Cursor, VS Code, OpenAI Agents SDK and LangChain work with our server? | any OS (fake engine), the clients | 2 days |
| 4 | [S2 AX performance](s2_ax_perf/README.md) | Is PyObjC AX traversal fast enough, and how much does batching help? Chromium/Electron flags? | Mac, PyObjC, heavy apps | 1 day |
| 5 | [S3 input](s3_input/README.md) | AXPress vs CGEvent vs PostToPid per app; does yield-to-user's HID clock ignore our events; layouts via UCKeyTranslate | Mac, PyObjC, ~15 apps, extra keyboard layouts | 2 days |
| 6 | [S4 capture](s4_capture/README.md) | ScreenCaptureKit latency and exclusion filters; the monthly Screen Recording prompt | Mac (macOS 15+ for the prompt), PyObjC | 1 day |

## One-time setup

```sh
git clone https://github.com/vansh3503/Mac-Mcp && cd Mac-Mcp
xcode-select --install          # swiftc, codesign, lipo (S1, S5's launcher job)
uv sync                         # S6 and the kit's own tests
uv run pytest spikes/tests launcher/tests -q   # sanity check of the kit (macOS runs the launcher suite)
```

Machines: macOS 14, 15 and 26 can run in VMs (Tart, UTM; give each a real GUI session); macOS 27
needs Apple silicon. Test at least one Intel Mac on 14 or 15. Use a test user account where you
can: S1 resets TCC grants for AXPilot only, but S3 types into real apps and S4 captures your screen.

For the PyObjC spikes, one throwaway environment is enough:

```sh
python3 -m venv /tmp/spikes && /tmp/spikes/bin/pip install \
  pyobjc-framework-ApplicationServices pyobjc-framework-ScreenCaptureKit
```

and grant Accessibility (S2, S3) and Screen Recording (S4) to the terminal you run them from.

## Step by step

### 1. S1, the go/no-go (per macOS version)

```sh
spikes/s1_launcher_tcc/run_s1.sh --direct --label terminal               # control
tccutil reset All io.github.vansh3503.axpilot
spikes/s1_launcher_tcc/run_s1.sh --label fresh-grant --prompt --wait-for-trust 120
spikes/s1_launcher_tcc/run_s1.sh --label second-start --skip-build --skip-install
```

Observe that the system dialog names **AXPilot**, enable it, and expect `verdict: pass` on the
second start. Then work through every row of [`s1_launcher_tcc/MATRIX.md`](s1_launcher_tcc/MATRIX.md)
(Python changes, MCP clients, quarantine, launcher behaviour), and run the launcher's own suite
once per version: `launcher/build.sh && uv run pytest launcher/tests -v`.

Decide with the go/no-go section of the results template. If S1 fails on a supported version,
stop and evaluate ADR-2's fallbacks before spending time on S2 to S4.

### 2. S5 on CI

Copy `s5_ci_permissions/workflow.yml` to `.github/workflows/` on a branch, verify and pin the
actions, run it from the Actions tab, and compare `baseline.json` with `after.json` per job.

### 3. S6 on any machine

```sh
uv run python spikes/s6_mcp_interop/interop.py
```

then the per-client rows of [`s6_mcp_interop/CHECKLIST.md`](s6_mcp_interop/CHECKLIST.md) and the
two framework smoke scripts. Requires the CLI (`axpilot stdio --engine fake`).

### 4–6. S2, S3, S4 on a Mac

```sh
/tmp/spikes/bin/python spikes/s2_ax_perf/ax_perf.py --suite --runs 5
/tmp/spikes/bin/python spikes/s3_input/strategies.py plan spikes/s3_input/plan.example.json
/tmp/spikes/bin/python spikes/s3_input/hid_idle.py --interactive
python3 spikes/s3_input/layout_map.py --check-thread        # once per keyboard layout
/tmp/spikes/bin/python spikes/s4_capture/capture_probe.py latency --runs 20
/tmp/spikes/bin/python spikes/s4_capture/capture_probe.py exclude --app TextEdit --save
```

Each spike's README lists the exact runs, what to watch, and the decision it feeds.

## Recording results

1. Copy [`docs/spikes/RESULTS_TEMPLATE.md`](../docs/spikes/RESULTS_TEMPLATE.md) to
   `docs/spikes/S<n>-<short-name>.md` (for example `S1-launcher-tcc.md`) and fill it in: one
   environment row per machine, one result row per scenario and macOS version.
2. Every script writes JSON to its `results/` directory (git-ignored). Copy the reports you cite
   to `docs/spikes/evidence/S<n>/`, **after reviewing them**: they contain window titles, paths
   with your user name and parts of your environment. Never commit `s4_capture` screenshots.
3. Write the ADR (`docs/adr/`), linking the results file, and update PLAN.md where a decision
   changed (for S1, ADR-2's status line).

## The kit itself

The scripts are small and dependency-light on purpose: S1's probe and installer, S3's layout
mapper and S5's TCC helper use only the standard library (Python 3.9+), the PyObjC spikes import
PyObjC lazily, and S6 uses the project's own environment. Their platform-independent logic is
tested on Linux: `uv run pytest spikes/tests -q`, plus `uv run ruff check spikes`.
