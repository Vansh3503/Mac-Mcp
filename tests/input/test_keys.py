from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import (
    KEY_ALIASES,
    KEY_NAMES,
    MAX_SEQUENCE_LENGTH,
    parse_combo,
    parse_sequence,
    to_glyphs,
)
from axpilot.models import MODIFIERS, KeyCombo


def combo(key: str, *mods: str) -> KeyCombo:
    return KeyCombo(key, frozenset(mods))


def assert_invalid(text: object) -> AXPilotError:
    with pytest.raises(AXPilotError) as info:
        parse_combo(text)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    return info.value


# ------------------------------------------------------------------- contract examples


@pytest.mark.parametrize(
    "text",
    [
        "cmd+shift+t",
        "⌘⇧T",
        "⇧⌘T",
        "Command-Shift-T",
        "cmd + shift + t",
        "CMD+SHIFT+t",
        "command+shift+t",
        "meta+shift+t",
        "super+shift+t",
        "win+shift+t",
        "CmdOrCtrl+Shift+T",
        "shift+cmd+t",
        "cmd shift t",
        "⌘ ⇧ t",
    ],
)
def test_cmd_shift_t_spellings(text: str) -> None:
    assert parse_combo(text) == combo("t", "cmd", "shift")


def test_ctrl_alt_delete() -> None:
    assert parse_combo("ctrl+alt+delete") == combo("delete", "ctrl", "option")


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("cmd", "cmd"),
        ("command", "cmd"),
        ("⌘", "cmd"),
        ("meta", "cmd"),
        ("super", "cmd"),
        ("win", "cmd"),
        ("option", "option"),
        ("opt", "option"),
        ("alt", "option"),
        ("⌥", "option"),
        ("ctrl", "ctrl"),
        ("control", "ctrl"),
        ("^", "ctrl"),
        ("⌃", "ctrl"),
        ("shift", "shift"),
        ("⇧", "shift"),
        ("fn", "fn"),
        ("globe", "fn"),
        ("🌐", "fn"),
    ],
)
def test_modifier_aliases(alias: str, canonical: str) -> None:
    assert parse_combo(f"{alias}+a") == combo("a", canonical)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("enter", "return"),
        ("return", "return"),
        ("↩", "return"),
        ("esc", "escape"),
        ("escape", "escape"),
        ("backspace", "delete"),
        ("delete", "delete"),
        ("⌫", "delete"),
        ("del", "forward_delete"),
        ("forwarddelete", "forward_delete"),
        ("forward_delete", "forward_delete"),
        ("⌦", "forward_delete"),
        ("space", "space"),
        ("spacebar", "space"),
        ("tab", "tab"),
        ("⇥", "tab"),
        ("up", "up"),
        ("↑", "up"),
        ("down", "down"),
        ("↓", "down"),
        ("left", "left"),
        ("←", "left"),
        ("right", "right"),
        ("→", "right"),
        ("ArrowLeft", "left"),
        ("home", "home"),
        ("end", "end"),
        ("pageup", "page_up"),
        ("pgup", "page_up"),
        ("PageUp", "page_up"),
        ("pagedown", "page_down"),
        ("pgdn", "page_down"),
        ("f1", "f1"),
        ("F12", "f12"),
        ("f20", "f20"),
        ("capslock", "caps_lock"),
        ("volumeup", "volume_up"),
        ("volumedown", "volume_down"),
        ("mute", "mute"),
        ("numpad5", "keypad_5"),
        ("keypad_enter", "keypad_enter"),
    ],
)
def test_key_aliases(alias: str, canonical: str) -> None:
    assert parse_combo(alias) == combo(canonical)
    assert parse_combo(f"cmd+{alias}") == combo(canonical, "cmd")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("page up", combo("page_up")),
        ("page-down", combo("page_down")),
        ("cmd+page up", combo("page_up", "cmd")),
        ("⌘⇧page down", combo("page_down", "cmd", "shift")),
        ("forward delete", combo("forward_delete")),
        ("caps lock", combo("caps_lock")),
        ("volume-up", combo("volume_up")),
        ("up arrow", combo("up")),
        ("keypad 5", combo("keypad_5")),
    ],
)
def test_multi_word_key_names(text: str, expected: KeyCombo) -> None:
    assert parse_combo(text) == expected


def test_multi_word_names_do_not_merge_across_plus() -> None:
    assert_invalid("cmd+f1+0")  # not silently F10
    assert_invalid("page+up")


# ---------------------------------------------------------------- characters and case


def test_lowercase_letter_has_no_shift() -> None:
    assert parse_combo("t") == combo("t")


def test_uppercase_letter_alone_implies_shift() -> None:
    assert parse_combo("T") == combo("t", "shift")
    assert parse_combo("shift+T") == combo("t", "shift")


@pytest.mark.parametrize("text", ["cmd+C", "⌘C", "Cmd+C", "ctrl+C", "alt+C", "fn+C"])
def test_letter_case_ignored_next_to_command_modifiers(text: str) -> None:
    result = parse_combo(text)
    assert result.key == "c"
    assert "shift" not in result.modifiers


@pytest.mark.parametrize("symbol", ["?", "!", "@", "{", "|", ":", "~", "_"])
def test_shifted_symbols_are_kept_verbatim(symbol: str) -> None:
    assert parse_combo(f"cmd+{symbol}") == combo(symbol, "cmd")


@pytest.mark.parametrize("char", ["é", "ß", "ü", "ñ", "€", "§", "É"])
def test_non_ascii_characters_are_kept(char: str) -> None:
    assert parse_combo(f"option+{char}") == combo(char, "option")


def test_decomposed_unicode_is_normalized() -> None:
    assert parse_combo("cmd+é") == combo("é", "cmd")


def test_digits_and_punctuation() -> None:
    assert parse_combo("cmd+1") == combo("1", "cmd")
    assert parse_combo("⌃⌥⌘.") == combo(".", "ctrl", "option", "cmd")
    assert parse_combo("cmd+,") == combo(",", "cmd")
    assert parse_combo("cmd+=") == combo("=", "cmd")
    assert parse_combo("cmd+/") == combo("/", "cmd")


# ------------------------------------------------------------------ separator literals


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+", combo("+")),
        ("-", combo("-")),
        ("cmd++", combo("+", "cmd")),
        ("cmd+-", combo("-", "cmd")),
        ("cmd--", combo("-", "cmd")),
        ("cmd-+", combo("+", "cmd")),
        ("cmd + +", combo("+", "cmd")),
        ("cmd -", combo("-", "cmd")),
        ("⌘+", combo("+", "cmd")),
        ("⌘-", combo("-", "cmd")),
        ("cmd+shift+-", combo("-", "cmd", "shift")),
        ("cmd+plus", combo("+", "cmd")),
        ("cmd+minus", combo("-", "cmd")),
        ("^+", combo("+", "ctrl")),
    ],
)
def test_plus_and_minus_keys(text: str, expected: KeyCombo) -> None:
    assert parse_combo(text) == expected


@pytest.mark.parametrize("text", ["cmd+", "cmd-", "a-", "shift+cmd+"])
def test_dangling_separator_is_an_error_with_a_hint(text: str) -> None:
    error = assert_invalid(text)
    assert error.hint is not None
    assert "double it" in error.hint


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("^c", combo("c", "ctrl")),
        ("^+c", combo("c", "ctrl")),
        ("^", combo("^")),
        ("cmd+^", combo("^", "cmd")),
        ("⌘^", combo("^", "cmd")),
        ("^^", combo("^", "ctrl")),
        ("cmd+caret", combo("^", "cmd")),
    ],
)
def test_caret_is_ctrl_except_as_the_last_key(text: str, expected: KeyCombo) -> None:
    assert parse_combo(text) == expected


# -------------------------------------------------------------------- modifier-only taps


def test_single_modifier_is_tapped() -> None:
    assert parse_combo("shift") == combo("shift")
    assert parse_combo("fn") == combo("fn")
    assert parse_combo("⌘") == combo("cmd")


def test_modifier_only_combo_taps_the_last_modifier() -> None:
    assert parse_combo("cmd+shift") == combo("shift", "cmd")
    assert parse_combo("⇧⌘") == combo("cmd", "shift")


def test_duplicate_modifiers_collapse() -> None:
    assert parse_combo("cmd+command+⌘+t") == combo("t", "cmd")
    assert parse_combo("cmd+cmd") == combo("cmd")


def test_whitespace_and_variation_selectors_are_ignored() -> None:
    assert parse_combo("  cmd+t\n") == combo("t", "cmd")
    assert parse_combo("🌐️+f") == combo("f", "fn")


# ------------------------------------------------------------------------------ errors


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_empty_combo(text: str) -> None:
    assert_invalid(text)


def test_more_than_one_key() -> None:
    error = assert_invalid("cmd+a+b")
    assert "'a' and 'b'" in error.message
    assert error.hint is not None
    assert "sequence" in error.hint


def test_unknown_key_suggests_a_correction() -> None:
    error = assert_invalid("cmd+retrun")
    assert "retrun" in error.message
    assert error.hint == "Did you mean 'return'?"


def test_unknown_modifier_suggests_a_correction() -> None:
    error = assert_invalid("comand+t")
    assert error.hint == "Did you mean 'cmd'?"


@pytest.mark.parametrize("text", ["cmd+\x00", "cmd+\x7f", "T⌘", "cmd+abc", "cmd+⌘⌘a b"])
def test_garbage_is_rejected(text: str) -> None:
    assert_invalid(text)


@pytest.mark.parametrize("value", [None, 42, ["cmd+t"], b"cmd+t"])
def test_non_string_is_rejected(value: object) -> None:
    assert_invalid(value)


def test_overlong_combo_is_rejected() -> None:
    assert_invalid("cmd+" * 100 + "t")


# ---------------------------------------------------------------------------- sequences


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("cmd+a delete", [combo("a", "cmd"), combo("delete")]),
        (["cmd+a", "delete"], [combo("a", "cmd"), combo("delete")]),
        (("cmd+c", "cmd+v"), [combo("c", "cmd"), combo("v", "cmd")]),
        ("cmd + shift + t", [combo("t", "cmd", "shift")]),
        ("Command - Shift - T", [combo("t", "cmd", "shift")]),
        ("down down return", [combo("down"), combo("down"), combo("return")]),
        ("cmd++ cmd+-", [combo("+", "cmd"), combo("-", "cmd")]),
        ("- -", [combo("-"), combo("-")]),
        ("cmd+ a", [combo("a", "cmd")]),
        ("page up down", [combo("page_up"), combo("down")]),
        ("tab  tab\treturn", [combo("tab"), combo("tab"), combo("return")]),
        ("⌘C ⌘V", [combo("c", "cmd"), combo("v", "cmd")]),
        ("cmd + +", [combo("+", "cmd")]),
    ],
)
def test_parse_sequence(keys: str | list[str], expected: list[KeyCombo]) -> None:
    assert parse_sequence(keys) == expected


def test_list_items_are_single_combos() -> None:
    assert parse_sequence(["cmd + shift + t"]) == [combo("t", "cmd", "shift")]
    with pytest.raises(AXPilotError):
        parse_sequence(["cmd+a delete"])


@pytest.mark.parametrize("keys", ["", "   ", [], ()])
def test_empty_sequence(keys: str | list[str]) -> None:
    with pytest.raises(AXPilotError) as info:
        parse_sequence(keys)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_sequence_reports_the_bad_combo() -> None:
    with pytest.raises(AXPilotError) as info:
        parse_sequence("cmd+a retrun")
    assert "retrun" in info.value.message


def test_sequence_length_limit() -> None:
    assert len(parse_sequence(["a"] * MAX_SEQUENCE_LENGTH)) == MAX_SEQUENCE_LENGTH
    with pytest.raises(AXPilotError):
        parse_sequence(["a"] * (MAX_SEQUENCE_LENGTH + 1))
    with pytest.raises(AXPilotError):
        parse_sequence(" ".join(["a"] * (MAX_SEQUENCE_LENGTH + 1)))


@pytest.mark.parametrize("keys", [None, 5, [1, 2]])
def test_sequence_rejects_non_strings(keys: object) -> None:
    with pytest.raises(AXPilotError):
        parse_sequence(keys)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------- glyphs


@pytest.mark.parametrize(
    ("value", "glyphs"),
    [
        (combo("t", "cmd", "shift"), "⇧⌘T"),
        (combo("return", "ctrl", "option", "cmd"), "⌃⌥⌘↩"),
        (combo("t"), "t"),
        (combo("t", "shift"), "⇧T"),
        (combo("f5", "fn"), "🌐F5"),
        (combo("space", "ctrl"), "⌃Space"),
        (combo("shift"), "⇧"),
        (combo("+", "cmd"), "⌘+"),
        (combo("page_down"), "⇟"),
        (combo("volume_up", "cmd"), "⌘volume_up"),
    ],
)
def test_to_glyphs(value: KeyCombo, glyphs: str) -> None:
    assert to_glyphs(value) == glyphs
    assert parse_combo(glyphs) == value


# ------------------------------------------------------------------- round-trip properties

_GLYPH_KEYS = frozenset(k for k in KEY_ALIASES if len(k) == 1)
_ASCII_KEYS = [
    c
    for c in string.printable
    if c not in string.whitespace and c not in string.ascii_uppercase and c not in _GLYPH_KEYS
]
_KEYS = st.one_of(
    st.sampled_from(sorted(KEY_NAMES)),
    st.sampled_from(_ASCII_KEYS),
    st.sampled_from(["é", "ß", "ü", "ñ", "€", "§", "É", "ø", "¥"]),
)


@st.composite
def combos(draw: st.DrawFn) -> KeyCombo:
    key = draw(_KEYS)
    mods = draw(st.frozensets(st.sampled_from(MODIFIERS)))
    return KeyCombo(key, mods - {key})


@given(combos())
def test_str_round_trips(value: KeyCombo) -> None:
    assert parse_combo(str(value)) == value


@given(combos())
def test_glyphs_round_trip(value: KeyCombo) -> None:
    assert parse_combo(to_glyphs(value)) == value


@given(st.lists(combos(), min_size=1, max_size=8))
def test_sequence_string_round_trips(values: list[KeyCombo]) -> None:
    assert parse_sequence(" ".join(str(v) for v in values)) == values


def parse_or_error(text: str) -> tuple[KeyCombo | None, AXPilotError | None]:
    try:
        return parse_combo(text), None
    except AXPilotError as error:
        return None, error


@given(st.text(max_size=40))
def test_arbitrary_text_parses_or_raises_invalid_argument(text: str) -> None:
    result, error = parse_or_error(text)
    if error is not None:
        assert error.code is ErrorCode.INVALID_ARGUMENT
    else:
        assert result is not None
        assert parse_combo(str(result)) == result
        assert result.modifiers <= set(MODIFIERS)
        assert result.key not in result.modifiers


@given(st.text(alphabet="cmdshiftaopnr+-⌘⇧⌥⌃^ ", max_size=24))
def test_combo_like_text_never_crashes(text: str) -> None:
    result, error = parse_or_error(text)
    if error is not None:
        assert error.code is ErrorCode.INVALID_ARGUMENT
    else:
        assert result is not None
        assert parse_combo(str(result)) == result
