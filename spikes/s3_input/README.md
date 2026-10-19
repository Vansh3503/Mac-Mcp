# Spike S3: input strategies, yield-to-user and keyboard layouts

Three questions that shape `engine/input` and the action ladder of PLAN.md §5.5:

1. **Strategies** (`strategies.py`): where does `AXPress` work (background-safe, no cursor
   movement), where is a real CGEvent click needed, and does `CGEventPostToPid` reach an app in the
   background? Same for typing: `AXValue`/`AXSelectedText` vs Unicode keystrokes.
2. **Yield-to-user** (`hid_idle.py`): do the events AXPilot posts leave the HID idle clock alone,
   so `CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, …)` measures only
   the human? Which event tap and source state must the engine use?
3. **Layouts** (`layout_map.py`): does `UCKeyTranslate` through ctypes map characters to the right
   key codes on non-US layouts, and must Text Input Sources be called on the main thread?

## Setup

```sh
python3 -m venv /tmp/s3 && /tmp/s3/bin/pip install pyobjc-framework-ApplicationServices
```

Grant Accessibility to the app running the scripts (Terminal, iTerm, your IDE). `layout_map.py`
needs no PyObjC and no permission.

## 1. Strategy matrix

```sh
/tmp/s3/bin/python spikes/s3_input/strategies.py click --app Calculator --target 7
/tmp/s3/bin/python spikes/s3_input/strategies.py type --app TextEdit
cp spikes/s3_input/plan.example.json /tmp/plan.json   # extend it to ~15 apps
/tmp/s3/bin/python spikes/s3_input/strategies.py plan /tmp/plan.json --label sonoma-m1
```

Keep the terminal frontmost when you start, so the target app is in the background: the
background-safe strategies (`ax_press`, `cg_post_to_pid`, `ax_*`, `cg_unicode_pid`) run first;
`cg_click` and `cg_unicode` activate the app. The script asks before acting (`--yes` skips that),
detects the effect itself (window text changed; typed marker present), restores the cursor
position after CGEvent clicks, and warns when Secure Event Input is on.

Record, per app and strategy: AXError, effect yes/no, whether the app had to be frontmost, and
anything surprising (e.g. a click landing in the wrong place on a Retina external display).

## 2. HID idle

```sh
/tmp/s3/bin/python spikes/s3_input/hid_idle.py --interactive
/tmp/s3/bin/python spikes/s3_input/hid_idle.py --keys --label with-keys   # only if F18 is unmapped
```

Keep your hands off the mouse, trackpad and keyboard while the combinations run (about two
minutes). The baseline series must show no resets; otherwise something else is generating input,
so rerun. In the table, the row we can adopt is the one with **HID unaffected = True** for the
tap/source the engine will post with. The positive control must show real input resetting the HID
clock; without it, "unaffected" proves nothing.

## 3. Layouts

```sh
python3 spikes/s3_input/layout_map.py --label us --check-thread
# System Settings → Keyboard → Input Sources: add and select each layout, then rerun:
python3 spikes/s3_input/layout_map.py --label french-azerty
python3 spikes/s3_input/layout_map.py --label german
python3 spikes/s3_input/layout_map.py --label dvorak
python3 spikes/s3_input/layout_map.py --label russian --ascii-capable
python3 spikes/s3_input/layout_map.py --label japanese-romaji --ascii-capable
```

Check the shortcut table: on AZERTY, `a` must map to key code 12 and `q` to 0; on Dvorak `c` is
not 8. For non-Latin layouts the current layout may have few Latin letters; the ASCII-capable
layout is what shortcuts should use. `--check-thread` reports whether TIS crashed when called
from a worker thread (a negative return code is the signal number); if it did, the engine must
marshal layout lookups to the main thread.

## What to record

Use `docs/spikes/RESULTS_TEMPLATE.md`. The decisions this spike feeds:

- the default strategy ladder per intent, and an app-compatibility list of exceptions;
- whether `CGEventPostToPid` is worth offering for background shortcuts;
- the event tap and source state for synthetic input, and whether yield-to-user can rely on the
  HID clock;
- whether layout lookups need the main thread, and that non-US layouts map correctly.
