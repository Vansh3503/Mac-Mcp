"""Input helpers that need no OS access: key-combo parsing and coordinate mapping."""

from __future__ import annotations

from axpilot.input.coords import COORDINATE_SPACES, CoordinateSpace, to_screen_point
from axpilot.input.keys import (
    KEY_NAMES,
    MODIFIER_FLAGS,
    US_ANSI_KEYCODES,
    ansi_keycode,
    modifier_mask,
    parse_combo,
    parse_sequence,
    to_glyphs,
)

__all__ = [
    "COORDINATE_SPACES",
    "KEY_NAMES",
    "MODIFIER_FLAGS",
    "US_ANSI_KEYCODES",
    "CoordinateSpace",
    "ansi_keycode",
    "modifier_mask",
    "parse_combo",
    "parse_sequence",
    "to_glyphs",
    "to_screen_point",
]
