"""The key-code table must match Carbon's HIToolbox/Events.h exactly."""

from __future__ import annotations

import string

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import (
    KEY_NAMES,
    MODIFIER_FLAGS,
    US_ANSI_KEYCODES,
    US_ANSI_SHIFTED,
    ansi_keycode,
    modifier_mask,
)
from axpilot.models import MODIFIERS

# Independent transcription of the kVK_* constants (decimal, as in many references).
EVENTS_H = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "return": 36,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "tab": 48,
    "space": 49,
    "`": 50,
    "delete": 51,
    "escape": 53,
    "cmd": 55,
    "shift": 56,
    "caps_lock": 57,
    "option": 58,
    "ctrl": 59,
    "fn": 63,
    "f17": 64,
    "keypad_decimal": 65,
    "keypad_multiply": 67,
    "keypad_plus": 69,
    "keypad_clear": 71,
    "volume_up": 72,
    "volume_down": 73,
    "mute": 74,
    "keypad_divide": 75,
    "keypad_enter": 76,
    "keypad_minus": 78,
    "f18": 79,
    "f19": 80,
    "keypad_equals": 81,
    "keypad_0": 82,
    "keypad_1": 83,
    "keypad_2": 84,
    "keypad_3": 85,
    "keypad_4": 86,
    "keypad_5": 87,
    "keypad_6": 88,
    "keypad_7": 89,
    "f20": 90,
    "keypad_8": 91,
    "keypad_9": 92,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f3": 99,
    "f8": 100,
    "f9": 101,
    "eisu": 102,
    "f11": 103,
    "kana": 104,
    "f13": 105,
    "f16": 106,
    "f14": 107,
    "f10": 109,
    "f12": 111,
    "f15": 113,
    "help": 114,
    "home": 115,
    "page_up": 116,
    "forward_delete": 117,
    "f4": 118,
    "end": 119,
    "f2": 120,
    "page_down": 121,
    "f1": 122,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}


def test_table_matches_events_h() -> None:
    assert US_ANSI_KEYCODES == EVENTS_H


def test_key_codes_are_unique() -> None:
    codes = list(US_ANSI_KEYCODES.values())
    assert len(codes) == len(set(codes))


def test_every_named_key_has_a_code() -> None:
    assert US_ANSI_KEYCODES.keys() >= KEY_NAMES


def test_every_ansi_character_has_a_code() -> None:
    for ch in string.ascii_lowercase + string.digits + "-=[]\\;',./`":
        assert ch in US_ANSI_KEYCODES


def test_shifted_symbols_map_to_unshifted_keys() -> None:
    assert set(US_ANSI_SHIFTED) == set('~!@#$%^&*()_+{}|:"<>?')
    assert all(base in US_ANSI_KEYCODES for base in US_ANSI_SHIFTED.values())


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("a", (0x00, False)),
        ("return", (0x24, False)),
        ("?", (0x2C, True)),
        ("+", (0x18, True)),
        ("_", (0x1B, True)),
        ("A", (0x00, True)),
        ("~", (0x32, True)),
    ],
)
def test_ansi_keycode(key: str, expected: tuple[int, bool]) -> None:
    assert ansi_keycode(key) == expected


@pytest.mark.parametrize("key", ["é", "€", "no_such_key", ""])
def test_ansi_keycode_rejects_unmapped_keys(key: str) -> None:
    with pytest.raises(AXPilotError) as info:
        ansi_keycode(key)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_modifier_flags_are_cg_event_masks() -> None:
    assert MODIFIER_FLAGS == {
        "cmd": 0x00100000,
        "shift": 0x00020000,
        "option": 0x00080000,
        "ctrl": 0x00040000,
        "fn": 0x00800000,
    }
    assert set(MODIFIER_FLAGS) == set(MODIFIERS)


def test_modifier_mask() -> None:
    assert modifier_mask([]) == 0
    assert modifier_mask(["cmd", "shift"]) == 0x120000
    assert modifier_mask(frozenset(MODIFIERS)) == 0x9E0000
    with pytest.raises(AXPilotError):
        modifier_mask(["hyper"])
