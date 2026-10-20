"""``axpilot.input.hotkeys``: the kill-switch hotkey."""

from __future__ import annotations

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.hotkeys import CARBON_MODIFIERS, DEFAULT_KILL_SWITCH, parse_hotkey


def test_the_default_kill_switch() -> None:
    hotkey = parse_hotkey(DEFAULT_KILL_SWITCH)
    assert hotkey.key == "."
    assert hotkey.key_code == 47  # kVK_ANSI_Period
    assert hotkey.modifiers == {"ctrl", "option", "cmd"}
    assert (
        hotkey.carbon_modifiers
        == CARBON_MODIFIERS["ctrl"] | CARBON_MODIFIERS["option"] | CARBON_MODIFIERS["cmd"]
    )
    assert hotkey.label == "⌃⌥⌘."


@pytest.mark.parametrize(
    ("text", "label", "code"),
    [
        ("⌃⌥⌘.", "⌃⌥⌘.", 47),
        ("cmd+shift+escape", "⇧⌘escape", 53),
        ("ctrl+option+k", "⌃⌥K", 40),
        ("cmd+?", "⇧⌘?", 44),
    ],
)
def test_other_hotkeys(text: str, label: str, code: int) -> None:
    hotkey = parse_hotkey(text)
    assert hotkey.label == label
    assert hotkey.key_code == code


@pytest.mark.parametrize("text", ["a", "shift+a", "fn+cmd+a", "cmd+é", "", "cmd+nosuchkey"])
def test_refused_hotkeys(text: str) -> None:
    with pytest.raises(AXPilotError) as info:
        parse_hotkey(text)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
