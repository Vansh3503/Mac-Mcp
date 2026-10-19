"""Synthetic keyboard input: layout-aware shortcuts and Unicode typing.

Permission: **Accessibility** (to post events). Keystrokes are discarded by macOS while
Secure Event Input is on; :class:`~axpilot.engine.macos.engine.MacEngine` checks that first.

* Shortcuts (``press``) resolve characters through the user's current keyboard layout (see
  :mod:`~axpilot.engine.macos.carbon`), so ``cmd+z`` hits the key that types "z" on AZERTY,
  QWERTZ or Dvorak; named keys (return, arrows, F-keys…) use their fixed virtual key codes.
  Modifiers are pressed as real modifier keys (``kCGEventFlagsChanged``) *and* set as flags on
  the key events, which is what every app expects. Volume and mute go through the system-defined
  media-key events, like the hardware keys.
* Text (``type_text``) is sent as Unicode strings attached to key events
  (``CGEventKeyboardSetUnicodeString``), at most 20 UTF-16 units per event, so it is
  independent of the layout and works for any script or emoji. Line breaks and tabs are sent as
  the Return and Tab keys.
"""

from __future__ import annotations

import time

import AppKit
import Quartz as Q

from axpilot.engine.macos import helpers as h
from axpilot.engine.macos.carbon import LayoutProvider
from axpilot.engine.macos.input import EventPoster
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import MODIFIER_FLAGS, US_ANSI_KEYCODES, ansi_keycode
from axpilot.models import MODIFIERS, KeyCombo

MODIFIER_ORDER: tuple[str, ...] = ("ctrl", "option", "shift", "cmd", "fn")
"""Order in which modifier keys go down (and, reversed, come up)."""

KEY_GAP_S = 0.005
"""Pause between the events of one shortcut."""
CHUNK_GAP_S = 0.01
"""Pause between Unicode chunks while typing (keeps fast typists' apps from dropping input)."""
MEDIA_KEY_SUBTYPE = 8
"""``NX_SUBTYPE_AUX_CONTROL_BUTTONS``: subtype of system-defined media key events."""


class Keyboard:
    """Posts keyboard events through an :class:`EventPoster`."""

    def __init__(self, poster: EventPoster, layouts: LayoutProvider) -> None:
        self._poster = poster
        self._layouts = layouts

    # ------------------------------------------------------------------ resolution
    def resolve(self, key: str) -> tuple[int, frozenset[str]]:
        """Virtual key code for ``key`` plus the modifiers the layout needs to type it."""
        if len(key) > 1 or key in MODIFIERS:
            code = US_ANSI_KEYCODES.get(key)
            if code is None:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"Unknown key {key!r}.",
                    hint="Use a key name such as return, tab, escape, f5, left, or a single character.",
                )
            return code, frozenset()
        if key == " ":
            return US_ANSI_KEYCODES["space"], frozenset()
        position = self._layouts.current().lookup(key)
        if position is not None:
            code, shift, option = position
        else:
            code, shift = ansi_keycode(key)
            option = False
        implied = {"shift"} if shift else set()
        if option:
            implied.add("option")
        return code, frozenset(implied)

    # ------------------------------------------------------------------ events
    def _key_event(self, keycode: int, down: bool, flags: int) -> None:
        event = Q.CGEventCreateKeyboardEvent(self._poster.source, keycode, down)
        if event is not None:
            Q.CGEventSetFlags(event, flags)
        self._poster.post(event)

    def _modifier_event(self, name: str, down: bool, flags: int) -> None:
        event = Q.CGEventCreateKeyboardEvent(self._poster.source, US_ANSI_KEYCODES[name], down)
        if event is not None:
            Q.CGEventSetType(event, Q.kCGEventFlagsChanged)
            Q.CGEventSetFlags(event, flags)
        self._poster.post(event)

    def _media_key(self, name: str) -> None:
        key_type = h.MEDIA_KEY_TYPES[name]
        for down in (True, False):
            flags, data1 = h.media_key_event_data(key_type, down)
            make = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_  # noqa: E501
            ns_event = make(
                AppKit.NSEventTypeSystemDefined, (0.0, 0.0), flags, 0.0, 0, None, MEDIA_KEY_SUBTYPE, data1, -1
            )
            self._poster.post(ns_event.CGEvent() if ns_event is not None else None)
            time.sleep(KEY_GAP_S)

    # ------------------------------------------------------------------ public
    def press(self, combo: KeyCombo) -> None:
        """Press and release ``combo`` (modifiers down, key down/up, modifiers up)."""
        if combo.key in h.MEDIA_KEY_TYPES and not combo.modifiers:
            self._media_key(combo.key)
            return
        unknown = combo.modifiers - set(MODIFIER_FLAGS)
        if unknown:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, f"Unknown modifier(s): {', '.join(sorted(unknown))}."
            )
        if combo.key in MODIFIERS:
            self._tap_modifier(combo.key, combo.modifiers - {combo.key})
            return
        keycode, implied = self.resolve(combo.key)
        held = [m for m in MODIFIER_ORDER if m in combo.modifiers | implied]
        flags = 0
        try:
            for name in held:
                flags |= MODIFIER_FLAGS[name]
                self._modifier_event(name, True, flags)
                time.sleep(KEY_GAP_S)
            self._key_event(keycode, True, flags)
            time.sleep(KEY_GAP_S)
            self._key_event(keycode, False, flags)
        finally:
            for name in reversed(held):
                flags &= ~MODIFIER_FLAGS[name]
                self._modifier_event(name, False, flags)
                time.sleep(KEY_GAP_S)

    def _tap_modifier(self, key: str, others: frozenset[str]) -> None:
        held = [m for m in MODIFIER_ORDER if m in others]
        flags = 0
        try:
            for name in held:
                flags |= MODIFIER_FLAGS[name]
                self._modifier_event(name, True, flags)
            self._modifier_event(key, True, flags | MODIFIER_FLAGS[key])
            time.sleep(KEY_GAP_S)
            self._modifier_event(key, False, flags)
        finally:
            for name in reversed(held):
                flags &= ~MODIFIER_FLAGS[name]
                self._modifier_event(name, False, flags)

    def _unicode(self, text: str) -> None:
        units = h.utf16_length(text)
        for down in (True, False):
            event = Q.CGEventCreateKeyboardEvent(self._poster.source, 0, down)
            if event is not None:
                Q.CGEventSetFlags(event, 0)
                # PyObjC converts the str to UTF-16 and checks ``units`` against its length.
                Q.CGEventKeyboardSetUnicodeString(event, units, text)
            self._poster.post(event)

    def type_text(self, text: str, interval_s: float = 0.0) -> None:
        """Type ``text``. With ``interval_s`` > 0 each character is a separate keystroke
        followed by that pause; otherwise characters go in chunks of up to 20 UTF-16 units."""
        max_units = 1 if interval_s > 0 else h.MAX_EVENT_UTF16
        pause = interval_s if interval_s > 0 else CHUNK_GAP_S
        for kind, payload in h.type_segments(text, max_units):
            if kind == "key":
                self.press(KeyCombo(payload))
            else:
                self._unicode(payload)
            time.sleep(pause)
