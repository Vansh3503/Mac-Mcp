# Spike S5: Accessibility on CI runners

**Question.** Can GitHub-hosted macOS runners run AX, CGEvent and ScreenCaptureKit code, so that
the engine tests (PLAN.md §12) and ideally the launcher path run in CI? Concretely:

1. Which process does TCC hold responsible for a step's `python` on a runner, and what does the
   runner image already grant?
2. Does inserting rows into TCC.db (possible because SIP is disabled on hosted runners) make
   `AXIsProcessTrusted()` true for the runner's Python? For the AXPilot bundle id?
3. Does the `xa11y/setup-a11y` action do the same with less code?

## Files

| File | Purpose |
|---|---|
| `workflow.yml` | GitHub Actions template with three jobs: `python-direct` (no grant vs TCC.db), `setup-a11y`, and `launcher` (build, test, install, grant the bundle, run S1 through LaunchServices). Not active where it is. |
| `grant_tcc.py` | Writes or lists TCC.db grants by column name (works across schema versions), derives the stored code requirement with `codesign`/`csreq`, restarts tccd. Stdlib only. |

Every job runs the S1 probe (`spikes/s1_launcher_tcc/probe.py`) before and after granting, and
uploads the JSON reports, the TCC rows and a `log show` excerpt of the TCC subsystem.

## Running it

1. Create a branch, copy the template: `cp spikes/s5_ci_permissions/workflow.yml .github/workflows/spike-s5.yml`.
2. Verify and pin the actions. `actions/checkout`, `actions/setup-python` and
   `actions/upload-artifact` are referenced by major tag as placeholders; pin each to a commit SHA.
   For `xa11y/setup-a11y`, read its README first: what it grants, to which binary, and its inputs.
3. Push, then Actions → spike-s5-ci-permissions → Run workflow.
4. Download the artifacts and compare `baseline.json` with `after.json` per job.

`grant_tcc.py` can be tried locally against a copy of a database (`--db-path`), but never point it
at a real Mac's TCC.db: it needs SIP disabled and bypasses the consent the system is built around.

## What to record

Per runner image (`macos-14`, `macos-15`, later `macos-26`) and method:

| Field (from the reports) | Why |
|---|---|
| `baseline.responsibility.path` | who TCC blames for a step's process on a runner |
| rows printed by "Runner facts" | what the image pre-grants (and to whom) |
| `after.summary.ax_trusted`, `ax_read_ok` | whether the grant took effect |
| `after.summary.screen_recording_preflight` | the same for Screen Recording |
| launcher job: the S1 verdict and `launcher.log` | whether the launcher path works in CI |
| `tcc.log` | tccd's own view of each check, to explain surprises |

## How to decide

- **Engine tests in CI**: adopt whichever method makes `ax_trusted` and `ax_read_ok` true for the
  runner's Python (expected: granting the *responsible* process found in the baseline, or the
  runner image already covers it). Put the grant step in `ci.yml`'s macOS job.
- **Launcher path in CI**: if the `launcher` job passes, add a nightly launcher-mode job;
  otherwise keep launcher-mode testing manual (PLAN.md §12 "Launcher mode") and say so.
- If nothing works, the engine tests stay on a self-hosted Mac (PLAN.md §16 mitigation).
