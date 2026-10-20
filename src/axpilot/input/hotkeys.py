"""Global hotkeys such as the kill switch (``[safety] kill_switch``).

:func:`parse_hotkey` turns ``"ctrl+option+cmd+."`` (shown as ⌃⌥⌘.) into a :class:`Hotkey`: the
US-ANSI virtual key code and modifiers that the Carbon ``RegisterEventHotKey`` call in
:mod:`axpilot.host.ui.hotkey` registers. Carbon hotkeys are bound to a physical key, so on other
layouts the kill switch is the key in the US "." position (to check on a Mac with such a layout).
"""

from __future__ import annotations

from dataclasses import dataclass

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import ansi_keycode, parse_combo

__all__ = ["CARBON_MODIFIERS", "DEFAULT_KILL_SWITCH", "Hotkey", "parse_hotkey"]

DEFAULT_KILL_SWITCH = "ctrl+option+cmd+."
CARBON_MODIFIERS: dict[str, int] = {"cmd": 0x0100, "shift": 0x0200, "option": 0x0800, "ctrl": 0x1000}
"""Carbon ``cmdKey``/``shiftKey``/``optionKey``/``controlKey`` (``RegisterEventHotKey``)."""
_GLYPHS = {"ctrl": "⌃", "option": "⌥", "shift": "⇧", "cmd": "⌘"}
_GLYPH_ORDER = ("ctrl", "option", "shift", "cmd")


@dataclass(frozen=True, slots=True)
class Hotkey:
    """A global hotkey: a US-ANSI virtual key code plus modifiers."""

    key: str
    key_code: int
    modifiers: frozenset[str]

    @property
    def carbon_modifiers(self) -> int:
        """The modifier mask for ``RegisterEventHotKey``."""
        mask = 0
        for name in self.modifiers:
            mask |= CARBON_MODIFIERS[name]
        return mask

    @property
    def label(self) -> str:
        """How the Mac writes it: ``⌃⌥⌘.``."""
        glyphs = "".join(_GLYPHS[name] for name in _GLYPH_ORDER if name in self.modifiers)
        return glyphs + (self.key.upper() if len(self.key) == 1 else self.key)


def parse_hotkey(text: str) -> Hotkey:
    """The :class:`Hotkey` for ``text`` (``"ctrl+option+cmd+."``).

    Raises ``INVALID_ARGUMENT`` for a sequence, ``fn`` (Carbon hotkeys cannot use it), a key
    without ⌘, ⌃ or ⌥ (it would take a plain key away from every app), or a key that has no
    US-ANSI key code."""
    combo = parse_combo(text)
    if "fn" in combo.modifiers:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"The kill switch {text!r} cannot use fn.")
    if not combo.modifiers & {"cmd", "ctrl", "option"}:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"The kill switch {text!r} needs cmd, ctrl or option, so it does not take a key from other apps.",
            hint='The default is "ctrl+option+cmd+." (⌃⌥⌘.).',
        )
    code, needs_shift = ansi_keycode(combo.key)
    modifiers = frozenset(combo.modifiers | ({"shift"} if needs_shift else set()))
    return Hotkey(key=combo.key, key_code=code, modifiers=modifiers)
