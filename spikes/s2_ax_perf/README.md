# Spike S2: accessibility-tree performance in Python

**Question.** Is walking an app's AX tree through PyObjC fast enough for the v1 budget
(snapshot p50 < 400 ms on typical windows, PLAN.md §12), and how much does batching with
`AXUIElementCopyMultipleAttributeValues` buy over one call per attribute? Do Chromium and
Electron apps expose a usable tree once `AXManualAccessibility` (or `AXEnhancedUserInterface`)
is set?

The outcome sets the engine's defaults (batching, node and depth budgets, timeouts) and decides
whether the optional native traversal accelerator from ADR-1 is needed.

## Setup

```sh
python3 -m venv /tmp/s2 && /tmp/s2/bin/pip install pyobjc-framework-ApplicationServices
```

Grant Accessibility to the app that runs the script (Terminal, iTerm, or your IDE) in System
Settings → Privacy & Security → Accessibility. This spike measures Python, not the launcher.

## Runs to do

Open each app with a realistic, heavy window first:

| Target | Window to open |
|---|---|
| Finder | a folder with 1,000+ files in list view |
| System Settings | General → Storage (or another long pane) |
| Safari | a long article |
| Xcode | a project with the navigator, editor and inspector visible |
| Google Chrome | a page with a large DOM (e.g. a long GitHub PR diff) |
| Slack or another Electron app | a busy channel |
| VS Code | a large file with the explorer open |
| TextEdit, Notes | light baselines |

```sh
/tmp/s2/bin/python spikes/s2_ax_perf/ax_perf.py --suite --runs 5 --label suite-window
/tmp/s2/bin/python spikes/s2_ax_perf/ax_perf.py --suite --runs 3 --scope app --label suite-app
/tmp/s2/bin/python spikes/s2_ax_perf/ax_perf.py --app "Google Chrome" --electron --label chrome-flags
/tmp/s2/bin/python spikes/s2_ax_perf/ax_perf.py --app Slack --electron --label slack-flags
```

Useful options: `--max-nodes` (default 5000) and `--max-depth` (60) bound each walk; `--timeout`
sets the AX messaging timeout (1 s); `--keep-flags` leaves `AXManualAccessibility` set after the
test (by default the original values are restored, because `AXEnhancedUserInterface` can disturb
window animations and positioning in some apps).

## What to record

From the printed table and `results/<timestamp>-<label>.json`, per app:

- nodes and whether the walk was truncated;
- total time p50/p95 for `naive` and `batched`, and the speed-up;
- nodes/s and per-node latency p50/p95 (cross-process round trips dominate);
- for `--electron`: nodes before, after `AXManualAccessibility`, after `AXEnhancedUserInterface`,
  and the AXError of each set call.

Also note the Mac (chip, RAM), the macOS version, and whether the app was busy. Run on battery
and on power once: AX latency follows the target app's main-thread load.

## How to decide

- **Batching on by default** if `batched` beats `naive` by a clear margin (expected 3–10x).
- **Budget**: pick the default `max_elements`/depth so that the batched p95 stays under 400 ms
  for the heavy apps; record the node count where it crosses that line.
- **Native accelerator** (post-1.0): needed only if a typical window (not the worst case) misses
  the budget even with batching and pruning.
- **Chromium/Electron**: keep `enhance_electron_chrome = true` if the node count grows by an
  order of magnitude after setting the flag, and note which flag each app needs.
