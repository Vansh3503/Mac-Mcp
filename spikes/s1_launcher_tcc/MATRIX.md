# S1 scenario matrix

Run every scenario on each macOS version and record the outcome in
`docs/spikes/S1-launcher-tcc.md` (format: [`docs/spikes/RESULTS_TEMPLATE.md`](../../docs/spikes/RESULTS_TEMPLATE.md)).
Keep the JSON reports; they are the evidence. Apple silicon is required for macOS 27; test Intel
on 14, 15 and 26 at least once.

`R` below is `spikes/s1_launcher_tcc/run_s1.sh`. "Pass" means the report's `summary.verdict` is
`pass`, unless the row says otherwise.

## A. Grants are attributed to AXPilot.app

| ID | Scenario | Steps | Expected |
|---|---|---|---|
| A0 | Control from a terminal | `R --direct --label terminal` | verdict `control`; `responsible_path` is the terminal app; trusted only if the terminal has Accessibility |
| A1 | Fresh Accessibility grant | `tccutil reset All io.github.vansh3503.axpilot`; `R --label fresh-grant --prompt --wait-for-trust 120`; enable AXPilot in System Settings when asked | the dialog names **AXPilot** (not python, not the terminal); then pass |
| A2 | Grant persists for a new process | `R --label second-start --skip-build --skip-install` | pass with no prompt |
| A3 | Fresh Screen Recording grant | `tccutil reset ScreenCapture io.github.vansh3503.axpilot`; `R --label screen-grant --request-screen --skip-build --skip-install`, allow in System Settings, then `R --label screen-after --skip-build --skip-install` (macOS applies the grant to the next process) | the dialog names AXPilot; afterwards `screen_recording_preflight` and `other_app_window_titles_visible` are true |
| A4 | Denied stays denied | `tccutil reset Accessibility io.github.vansh3503.axpilot`; `R --label no-grant --skip-build --skip-install` | verdict `fail` with `kAXErrorAPIDisabled` (-25211): proves the probe can tell |
| A5 | Responsibility API | any launcher run | `responsibility.available` true and `responsible_is_launcher` true; if the API is missing, record that and rely on A1's dialog and the TCC log |

## B. Grant survives changes on the Python side

Each starts from a passing A2 and must pass **without** a new prompt.

| ID | Scenario | Steps |
|---|---|---|
| B1 | Different interpreter, same version line | `R --label other-python --skip-build --skip-install --python <another python3.x>` (e.g. switch between `/usr/bin/python3` and a uv-managed one: `uv python find 3.12`) |
| B2 | Python upgrade | `uv python install 3.13`; `R --label python-upgrade --skip-build --skip-install --python "$(uv python find 3.13)"` |
| B3 | Runtime venv recreated (`uv tool install --reinstall axpilot`) | `uv venv /tmp/axp-rt && R --label venv-recreated --skip-build --skip-install --python /tmp/axp-rt/bin/python`; once axpilot is on PyPI, repeat with a real `uv tool install --reinstall axpilot` |
| B4 | Symlinked interpreter | point `--python` at a symlink (uv venvs use one) and at its target; both pass |
| B5 | Identical launcher reinstalled | `R --label same-bytes-reinstalled --skip-build` (copy again, same cdhash) |
| B6 | Negative control: launcher changed | copy `launcher/` to a temp dir, bump `CFBundleVersion` in its Info.plist, build with `AXPILOT_LAUNCHER_OUT=/tmp/axp-b6 /tmp/launcher-copy/build.sh`, install that bundle with `install.py bundle`, run `R --label launcher-changed --skip-build --skip-install` | **fail** expected (new cdhash): confirms the grant is pinned to the cdhash, which `doctor` must detect as `LAUNCHER_CHANGED` |

## C. Started from MCP clients

The bridge will run `open -g -a AXPilot` from inside the client's process tree. Configure
`open_from_client.sh <client>` as an MCP server in each client (see the script header; the client
will show the server as failed, which is expected), restart the client, then read both reports.

| ID | Client | Expected |
|---|---|---|
| C1 | Claude Desktop | `<client>-direct`: Claude responsible; `<client>-launcher`: pass |
| C2 | Claude Code (CLI in a terminal; run `R --skip-build --skip-install --label claude-code` through its Bash tool) | launcher run passes |
| C3 | Cursor (MCP config, and its integrated terminal) | launcher run passes |
| C4 | VS Code (Copilot MCP config, and its integrated terminal) | launcher run passes |
| C5 | Terminal and iTerm2 | launcher run passes; control attributed to the terminal |
| C6 | At login (optional): add AXPilot as a login item with runtime.json pointing at the probe | pass after reboot and login |

## D. Installation, quarantine and Gatekeeper

| ID | Scenario | Expected |
|---|---|---|
| D1 | Plain copy (what `run_s1.sh` does) | `bundle.quarantined` false; no Gatekeeper dialog |
| D2 | From a wheel with pip and uv (once the release pipeline exists): `pip install <wheel>` / `uv tool install <wheel>`, then `axpilot setup` | no quarantine on the installed bundle; executable bit kept; no Gatekeeper dialog |
| D3 | Negative control: download `AXPilot.app.zip` with Safari and unzip in Finder | quarantine present; Gatekeeper blocks or asks for "Open Anyway": documents what users of a browser download would see |
| D4 | `curl -LO` the zip and `ditto -x -k` it | no quarantine (confirms command-line downloads are safe) |
| D5 | Homebrew formula (later) | no quarantine, signature unchanged (`codesign --verify --strict`), same cdhash as published |

## E. Launcher behaviour on a real system

Covered in depth by `uv run pytest launcher/tests` (run it once per macOS version too).

| ID | Scenario | Expected |
|---|---|---|
| E1 | Logout and login with the host running | the launcher exits cleanly (launcher.log shows the forwarded SIGTERM), no crash report |
| E2 | `kill -9` the launcher while the host runs | the host survives as an orphan; note how quickly it notices (`os.getppid()` becomes 1) |
| E3 | Activity Monitor → Quit AXPilot | SIGTERM is forwarded; both exit |

## Versions

Fill one copy of this grid per run in the results file:

| ID | 14.x (arm64) | 14.x (x86_64) | 15.x (arm64) | 26.x (arm64) | 27.x (arm64) |
|---|---|---|---|---|---|
| A0–A5 | | | | | |
| B1–B6 | | | | | |
| C1–C6 | | | | | |
| D1–D5 | | | | | |
| E1–E3 | | | | | |
