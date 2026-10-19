# Spike S4: screen capture with ScreenCaptureKit

**Questions.**

1. How fast is `SCScreenshotManager` (macOS 14+) through PyObjC, for a display at full size, at the
   default `max_edge` of 1280, and for a single window? How long does JPEG encoding take?
2. Does `SCContentFilter(display:excludingApplications:exceptingWindows:)` really remove an app
   from the capture? AXPilot relies on it to hide its own overlay and deny-listed apps.
3. How do the Screen Recording prompts behave on macOS 14, 15, 26 and 27, in particular the
   recurring "Allow for one month" prompt introduced in macOS 15? (Screen Recording is optional in
   AXPilot; this decides how prominently onboarding explains it.)

Attribution of the grant to `AXPilot.app` is spike S1's job (`probe.py --request-screen`). Here,
grant Screen Recording to the terminal that runs the script.

## Setup

```sh
python3 -m venv /tmp/s4 && /tmp/s4/bin/pip install pyobjc-framework-ScreenCaptureKit pyobjc-framework-Quartz
/tmp/s4/bin/python spikes/s4_capture/capture_probe.py status --request   # then allow, and restart the terminal
```

## Runs

```sh
P=/tmp/s4/bin/python; S=spikes/s4_capture/capture_probe.py
$P $S status
$P $S latency --runs 20 --label builtin-display
$P $S latency --runs 20 --window-of TextEdit --label textedit-window
$P $S exclude --app TextEdit --save --label exclude-textedit
```

Repeat `latency` on an external 4K/5K display and on a 1x display if available (latency grows with
pixels). For `exclude`, put a TextEdit window over a visually different background; the report's
`excluded_region_changed` must be true and `rest_stable` true. Look at the two saved PNGs too, then
delete them: they show your screen, and `results/*.png` is not git-ignored.

## Permission prompt protocol

Run this on a fresh user account or after `tccutil reset ScreenCapture`, and keep a dated log in the
results file:

| Step | Record |
|---|---|
| First `capture_probe.py latency` without permission | the prompt's exact wording and buttons; whether capture fails or returns only the desktop/our own windows |
| Grant in System Settings → Privacy & Security → Screen & System Audio Recording | whether macOS demands a restart of the app ("Quit & Reopen") |
| macOS 15+: `status --read-approvals` | the entry and date stored for the terminal (reading the replayd group container may itself prompt for "access data from other apps"; note that) |
| Each later prompt | the date, the wording ("… is requesting to bypass the system private window picker …", "Allow for One Month"), and what triggered it (time, reboot, update) |
| Under the launcher (S1 scenario A3) | that the prompt names AXPilot |

Known so far (verify, do not assume): macOS 15 re-prompts apps that use ScreenCaptureKit or the
legacy CGWindowList capture roughly monthly (betas did it weekly and after reboots); a
user-picked capture through `SCContentSharingPicker` needs no Screen Recording permission but needs
the user for every capture, so it does not suit an agent.

## What to record

- capture p50/p95 and JPEG encode p50/p95 per case, with the display resolution and scale;
- whether the excluded app's region changed and the rest stayed put;
- the prompt log above, per macOS version.

Decisions this feeds: the screenshot tool's latency budget and defaults (`max_edge`, format,
quality), whether exclusion filters are reliable enough to hide the overlay and deny-listed apps,
and the onboarding copy about the monthly prompt.
