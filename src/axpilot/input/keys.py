"""Keyboard shortcut parsing and the US-ANSI key-code table.

:func:`parse_combo` turns the many ways people (and models) write shortcuts into one
normalized :class:`~axpilot.models.KeyCombo`::

    parse_combo("cmd+shift+t")      == KeyCombo("t", frozenset({"cmd", "shift"}))
    parse_combo("⌘⇧T")              == KeyCombo("t", frozenset({"cmd", "shift"}))
    parse_combo("Command-Shift-T")  == KeyCombo("t", frozenset({"cmd", "shift"}))
    parse_combo("cmd + shift + t")  == KeyCombo("t", frozenset({"cmd", "shift"}))
    parse_combo("ctrl+alt+delete")  == KeyCombo("delete", frozenset({"ctrl", "option"}))

Normalization rules:

* Modifiers become ``cmd``, ``option``, ``ctrl``, ``shift`` or ``fn`` (see
  :data:`MODIFIER_ALIASES`). Separators are ``+``, ``-`` and whitespace; Apple glyphs
  (``⌘⌥⌃⇧``) need no separator.
* Named keys become the canonical names in :data:`KEY_NAMES` (``return``, ``escape``,
  ``delete`` (backspace), ``forward_delete``, ``page_up``, ``f5``…).
* Single characters are kept as typed, except ASCII letters, which are lowercased. An
  uppercase letter adds ``shift`` when no other modifier is held (``"T"`` → ``shift+t``).
  Next to ``cmd``/``ctrl``/``option``/``fn`` the case of a letter is ignored, matching how
  macOS menus display shortcuts (``⌘C`` is cmd+c; write ``⇧`` or ``shift`` explicitly).
* Shifted symbols stay as they are (``"cmd+?"`` keeps the key ``"?"``); the engine finds
  the physical key (and the implied shift) through the current keyboard layout, with
  :func:`ansi_keycode` as the fallback.
* The literal ``+``/``-`` keys are written doubled (``"cmd++"``, ``"cmd+-"``), after a glyph
  (``"⌘+"``) or by name (``"cmd+plus"``, ``"cmd+minus"``). A trailing ``^`` is the caret
  key; anywhere else ``^`` means ctrl.
* A combo of modifiers only taps the last one (``"fn"``, ``"cmd+shift"`` → shift with cmd
  held).

``str(parse_combo(s))`` is the canonical text form, and it always parses back to the same
combo. :func:`to_glyphs` renders the Apple-style form (``⇧⌘T``), which parses back too.
"""

from __future__ import annotations

import difflib
import unicodedata
from collections.abc import Iterable, Sequence

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import MODIFIERS, KeyCombo

__all__ = [
    "KEY_ALIASES",
    "KEY_NAMES",
    "MAX_COMBO_LENGTH",
    "MAX_SEQUENCE_LENGTH",
    "MODIFIER_ALIASES",
    "MODIFIER_FLAGS",
    "US_ANSI_KEYCODES",
    "US_ANSI_SHIFTED",
    "ansi_keycode",
    "modifier_mask",
    "parse_combo",
    "parse_sequence",
    "to_glyphs",
]

MAX_COMBO_LENGTH = 128
"""Longest accepted combo text, in characters."""

MAX_SEQUENCE_LENGTH = 200
"""Most combos accepted in one :func:`parse_sequence` call."""

_SEPARATORS = frozenset("+-")
_VARIATION_SELECTORS = str.maketrans("", "", "︎️")

# --------------------------------------------------------------------------- modifiers

MODIFIER_FLAGS: dict[str, int] = {
    "cmd": 0x100000,  # kCGEventFlagMaskCommand
    "shift": 0x20000,  # kCGEventFlagMaskShift
    "option": 0x80000,  # kCGEventFlagMaskAlternate
    "ctrl": 0x40000,  # kCGEventFlagMaskControl
    "fn": 0x800000,  # kCGEventFlagMaskSecondaryFn
}
"""``CGEventFlags`` mask for each canonical modifier."""

MODIFIER_ALIASES: dict[str, str] = {
    **dict.fromkeys(
        ("cmd", "command", "meta", "super", "win", "cmdorctrl", "commandorcontrol"),
        "cmd",
    ),
    **dict.fromkeys(("option", "opt", "alt"), "option"),
    **dict.fromkeys(("ctrl", "control", "ctl"), "ctrl"),
    "shift": "shift",
    **dict.fromkeys(("fn", "globe", "function"), "fn"),
}
"""Modifier spellings (casefolded, without ``_``) → canonical modifier name."""

_GLYPH_MODIFIERS: dict[str, str] = {
    "⌘": "cmd",
    "⌥": "option",
    "⌃": "ctrl",
    "^": "ctrl",
    "⇧": "shift",
    "🌐": "fn",
}

_MODIFIER_GLYPHS: dict[str, str] = {"ctrl": "⌃", "option": "⌥", "shift": "⇧", "cmd": "⌘", "fn": "🌐"}
_GLYPH_ORDER: tuple[str, ...] = ("fn", "ctrl", "option", "shift", "cmd")
_CHARACTER_MODIFIERS = frozenset({"cmd", "ctrl", "option", "fn"})
"""Modifiers next to which letter case is ignored (``⌘C`` means cmd+c)."""

# ------------------------------------------------------------------------- named keys

_FUNCTION_KEYS: tuple[str, ...] = tuple(f"f{n}" for n in range(1, 21))
_KEYPAD_KEYS: tuple[str, ...] = (
    *(f"keypad_{n}" for n in range(10)),
    "keypad_decimal",
    "keypad_multiply",
    "keypad_plus",
    "keypad_minus",
    "keypad_divide",
    "keypad_equals",
    "keypad_enter",
    "keypad_clear",
)

KEY_NAMES: frozenset[str] = frozenset(
    {
        "return",
        "escape",
        "delete",
        "forward_delete",
        "space",
        "tab",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "page_up",
        "page_down",
        "caps_lock",
        "volume_up",
        "volume_down",
        "mute",
        "help",
        "eisu",
        "kana",
        *_FUNCTION_KEYS,
        *_KEYPAD_KEYS,
        *MODIFIERS,
    }
)
"""Canonical names of every non-character key. Modifier names are included because a
modifier can be tapped on its own (``KeyCombo("fn")``)."""


def _keypad_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    suffixes = {
        **{str(n): f"keypad_{n}" for n in range(10)},
        "decimal": "keypad_decimal",
        "period": "keypad_decimal",
        "dot": "keypad_decimal",
        "multiply": "keypad_multiply",
        "plus": "keypad_plus",
        "add": "keypad_plus",
        "minus": "keypad_minus",
        "subtract": "keypad_minus",
        "divide": "keypad_divide",
        "equals": "keypad_equals",
        "equal": "keypad_equals",
        "enter": "keypad_enter",
        "clear": "keypad_clear",
    }
    for prefix in ("keypad", "numpad", "kp"):
        for suffix, name in suffixes.items():
            aliases[prefix + suffix] = name
    return aliases


KEY_ALIASES: dict[str, str] = {
    **{name.replace("_", ""): name for name in KEY_NAMES if name not in MODIFIERS},
    # return / escape / delete
    **dict.fromkeys(("return", "enter", "ret", "↩", "⏎", "⌤"), "return"),
    **dict.fromkeys(("escape", "esc", "⎋"), "escape"),
    **dict.fromkeys(("delete", "backspace", "bksp", "⌫"), "delete"),
    **dict.fromkeys(("forwarddelete", "del", "fwddelete", "forwarddel", "⌦"), "forward_delete"),
    # whitespace keys
    **dict.fromkeys(("space", "spacebar", "␣"), "space"),
    **dict.fromkeys(("tab", "⇥"), "tab"),
    # navigation
    **dict.fromkeys(("up", "uparrow", "arrowup", "↑"), "up"),
    **dict.fromkeys(("down", "downarrow", "arrowdown", "↓"), "down"),
    **dict.fromkeys(("left", "leftarrow", "arrowleft", "←"), "left"),
    **dict.fromkeys(("right", "rightarrow", "arrowright", "→"), "right"),
    **dict.fromkeys(("home", "↖"), "home"),
    **dict.fromkeys(("end", "↘"), "end"),
    **dict.fromkeys(("pageup", "pgup", "⇞"), "page_up"),
    **dict.fromkeys(("pagedown", "pgdn", "pgdown", "⇟"), "page_down"),
    # locks and media
    **dict.fromkeys(("capslock", "caps", "⇪"), "caps_lock"),
    **dict.fromkeys(("volumeup", "volup"), "volume_up"),
    **dict.fromkeys(("volumedown", "voldown"), "volume_down"),
    **dict.fromkeys(("mute", "volumemute"), "mute"),
    # keypad
    **_keypad_aliases(),
    # punctuation by name (useful because "+" and "-" are separators)
    **dict.fromkeys(("plus",), "+"),
    **dict.fromkeys(("minus", "hyphen", "dash"), "-"),
    **dict.fromkeys(("equal", "equals"), "="),
    **dict.fromkeys(("comma",), ","),
    **dict.fromkeys(("period", "dot", "fullstop"), "."),
    **dict.fromkeys(("slash", "forwardslash"), "/"),
    **dict.fromkeys(("backslash",), "\\"),
    **dict.fromkeys(("semicolon",), ";"),
    **dict.fromkeys(("quote", "apostrophe"), "'"),
    **dict.fromkeys(("grave", "backtick", "backquote"), "`"),
    **dict.fromkeys(("leftbracket", "lbracket", "bracketleft"), "["),
    **dict.fromkeys(("rightbracket", "rbracket", "bracketright"), "]"),
    **dict.fromkeys(("caret",), "^"),
}
"""Key spellings (casefolded, without ``_``) → canonical key name or character."""

_KEY_GLYPHS: dict[str, str] = {
    "return": "↩",
    "escape": "⎋",
    "delete": "⌫",
    "forward_delete": "⌦",
    "tab": "⇥",
    "space": "Space",
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "home": "↖",
    "end": "↘",
    "page_up": "⇞",
    "page_down": "⇟",
    "caps_lock": "⇪",
}

# ------------------------------------------------------------------- virtual key codes

US_ANSI_KEYCODES: dict[str, int] = {
    # Letters (kVK_ANSI_*), in key-code order.
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "o": 0x1F,
    "u": 0x20,
    "i": 0x22,
    "p": 0x23,
    "l": 0x25,
    "j": 0x26,
    "k": 0x28,
    "n": 0x2D,
    "m": 0x2E,
    # Digits.
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "9": 0x19,
    "7": 0x1A,
    "8": 0x1C,
    "0": 0x1D,
    # Punctuation.
    "=": 0x18,  # kVK_ANSI_Equal
    "-": 0x1B,  # kVK_ANSI_Minus
    "]": 0x1E,  # kVK_ANSI_RightBracket
    "[": 0x21,  # kVK_ANSI_LeftBracket
    "'": 0x27,  # kVK_ANSI_Quote
    ";": 0x29,  # kVK_ANSI_Semicolon
    "\\": 0x2A,  # kVK_ANSI_Backslash
    ",": 0x2B,  # kVK_ANSI_Comma
    "/": 0x2C,  # kVK_ANSI_Slash
    ".": 0x2F,  # kVK_ANSI_Period
    "`": 0x32,  # kVK_ANSI_Grave
    # Layout-independent keys.
    "return": 0x24,  # kVK_Return
    "tab": 0x30,  # kVK_Tab
    "space": 0x31,  # kVK_Space
    "delete": 0x33,  # kVK_Delete (backspace)
    "escape": 0x35,  # kVK_Escape
    "cmd": 0x37,  # kVK_Command
    "shift": 0x38,  # kVK_Shift
    "caps_lock": 0x39,  # kVK_CapsLock
    "option": 0x3A,  # kVK_Option
    "ctrl": 0x3B,  # kVK_Control
    "fn": 0x3F,  # kVK_Function
    "volume_up": 0x48,  # kVK_VolumeUp
    "volume_down": 0x49,  # kVK_VolumeDown
    "mute": 0x4A,  # kVK_Mute
    "help": 0x72,  # kVK_Help
    "home": 0x73,  # kVK_Home
    "page_up": 0x74,  # kVK_PageUp
    "forward_delete": 0x75,  # kVK_ForwardDelete
    "end": 0x77,  # kVK_End
    "page_down": 0x79,  # kVK_PageDown
    "left": 0x7B,  # kVK_LeftArrow
    "right": 0x7C,  # kVK_RightArrow
    "down": 0x7D,  # kVK_DownArrow
    "up": 0x7E,  # kVK_UpArrow
    "eisu": 0x66,  # kVK_JIS_Eisu
    "kana": 0x68,  # kVK_JIS_Kana
    # Function keys.
    "f1": 0x7A,
    "f2": 0x78,
    "f3": 0x63,
    "f4": 0x76,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f8": 0x64,
    "f9": 0x65,
    "f10": 0x6D,
    "f11": 0x67,
    "f12": 0x6F,
    "f13": 0x69,
    "f14": 0x6B,
    "f15": 0x71,
    "f16": 0x6A,
    "f17": 0x40,
    "f18": 0x4F,
    "f19": 0x50,
    "f20": 0x5A,
    # Keypad (kVK_ANSI_Keypad*).
    "keypad_decimal": 0x41,
    "keypad_multiply": 0x43,
    "keypad_plus": 0x45,
    "keypad_clear": 0x47,
    "keypad_divide": 0x4B,
    "keypad_enter": 0x4C,
    "keypad_minus": 0x4E,
    "keypad_equals": 0x51,
    "keypad_0": 0x52,
    "keypad_1": 0x53,
    "keypad_2": 0x54,
    "keypad_3": 0x55,
    "keypad_4": 0x56,
    "keypad_5": 0x57,
    "keypad_6": 0x58,
    "keypad_7": 0x59,
    "keypad_8": 0x5B,
    "keypad_9": 0x5C,
}
"""macOS virtual key codes (Carbon ``HIToolbox/Events.h``) for every name in
:data:`KEY_NAMES` and the unshifted characters of the US-ANSI layout. Engines use it when
the current layout cannot be queried."""

US_ANSI_SHIFTED: dict[str, str] = {
    "~": "`",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
}
"""Shifted US-ANSI characters → the unshifted character on the same physical key."""


def ansi_keycode(key: str) -> tuple[int, bool]:
    """Return ``(virtual key code, needs_shift)`` for ``key`` on a US-ANSI keyboard.

    ``key`` is a canonical name from :data:`KEY_NAMES` or a single character (shifted
    symbols and uppercase letters report ``needs_shift=True``). Raises ``INVALID_ARGUMENT``
    for characters the US-ANSI layout cannot type (use ``type_text`` for those).
    """
    if key in US_ANSI_KEYCODES:
        return US_ANSI_KEYCODES[key], False
    if key in US_ANSI_SHIFTED:
        return US_ANSI_KEYCODES[US_ANSI_SHIFTED[key]], True
    if len(key) == 1 and key.isascii() and key.isupper():
        return US_ANSI_KEYCODES[key.lower()], True
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Key {key!r} has no key code on a US-ANSI keyboard.",
        hint="Use `type_text` to enter characters that are not on the keyboard.",
    )


def modifier_mask(modifiers: Iterable[str]) -> int:
    """OR together the :data:`MODIFIER_FLAGS` of canonical modifier names."""
    mask = 0
    for name in modifiers:
        try:
            mask |= MODIFIER_FLAGS[name]
        except KeyError:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown modifier {name!r}.",
                hint=f"Modifiers are {', '.join(MODIFIERS)}.",
            ) from None
    return mask


# ----------------------------------------------------------------------------- parsing


def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        message,
        hint=hint
        or (
            "Write shortcuts like 'cmd+shift+t', '⌘⇧T' or 'ctrl+alt+delete'. Keys: single "
            "characters or names such as return, escape, delete, tab, space, up, page_down, f5."
        ),
    )


def _clean(text: str) -> str:
    """NFC-normalize (so "é" is one character) and drop emoji variation selectors."""
    return unicodedata.normalize("NFC", text).translate(_VARIATION_SELECTORS)


def _normalize_word(word: str) -> str:
    return word.casefold().replace("_", "")


def _suggestion(word: str) -> str | None:
    vocabulary = [*MODIFIER_ALIASES, *KEY_ALIASES]
    matches = difflib.get_close_matches(_normalize_word(word), vocabulary, n=1, cutoff=0.75)
    if not matches:
        return None
    match = matches[0]
    return MODIFIER_ALIASES.get(match) or KEY_ALIASES[match]


def _split_trailing_literal(text: str) -> tuple[str, str | None]:
    """Split off a trailing literal ``+``/``-`` key. Returns ``(rest, literal)``."""
    if text[-1] not in _SEPARATORS:
        return text, None
    if len(text) == 1:
        return "", text
    before, literal = text[:-1], text[-1]
    stripped = before.rstrip()
    if before != stripped or stripped[-1] in _SEPARATORS:
        # "cmd + +", "cmd++", "cmd+-": the char before the literal was a separator.
        rest = stripped[:-1] if stripped[-1] in _SEPARATORS else stripped
        return rest, literal
    if stripped[-1] in _GLYPH_MODIFIERS:
        return stripped, literal  # "⌘+", "⌃-"
    raise _invalid(
        f"{text!r} ends with {literal!r} but no key follows it.",
        hint=f"To press the {literal!r} key itself, double it ('cmd{literal}{literal}') "
        f"or use its name ('cmd+{'plus' if literal == '+' else 'minus'}').",
    )


def _split_pieces(text: str) -> list[tuple[str, bool]]:
    """Split on separators. Each piece carries ``joinable``: True when only whitespace or
    ``-`` separated it from the previous piece (so "page up" may form one key name)."""
    pieces: list[tuple[str, bool]] = []
    current: list[str] = []
    joinable = False
    for ch in text:
        if ch in _SEPARATORS or ch.isspace():
            if current:
                pieces.append(("".join(current), joinable))
                current = []
                joinable = True
            if ch == "+":
                joinable = False
        else:
            current.append(ch)
    if current:
        pieces.append(("".join(current), joinable))
    return pieces


_Atom = tuple[str, str, bool]
"""``(kind, value, joinable)`` with kind ``"mod"`` or ``"word"``."""


def _atoms(text: str, *, trailing_caret_is_key: bool) -> list[_Atom]:
    """Tokenize into modifier atoms (from glyphs) and word atoms."""
    atoms: list[_Atom] = []
    pieces = _split_pieces(text)
    for index, (piece, joinable) in enumerate(pieces):
        is_last = trailing_caret_is_key and index == len(pieces) - 1
        pos = 0
        while pos < len(piece) and piece[pos] in _GLYPH_MODIFIERS:
            if piece[pos] == "^" and pos == len(piece) - 1 and is_last:
                break  # a trailing caret is the "^" key itself
            atoms.append(("mod", _GLYPH_MODIFIERS[piece[pos]], False))
            pos += 1
        if pos < len(piece):
            atoms.append(("word", piece[pos:], joinable and pos == 0))
    return atoms


def _resolve_char(ch: str, text: str) -> tuple[str, str]:
    if alias := KEY_ALIASES.get(ch):
        return "key", alias
    if not ch.isprintable() or ch.isspace():
        raise _invalid(f"Unsupported key character {ch!r} in {text!r}.")
    if ch.isascii() and ch.isupper():
        return "upper", ch.lower()
    return "key", ch


def _resolve_word(word: str, text: str) -> tuple[str, str]:
    """Resolve one word to ``("mod"|"key"|"upper", name)``. Raises on unknown words."""
    if len(word) == 1:
        return _resolve_char(word, text)
    normalized = _normalize_word(word)
    if modifier := MODIFIER_ALIASES.get(normalized):
        return "mod", modifier
    if key := KEY_ALIASES.get(normalized):
        return "key", key
    suggestion = _suggestion(word)
    hint = f"Did you mean {suggestion!r}?" if suggestion else None
    raise _invalid(f"Unknown key {word!r} in {text!r}.", hint=hint)


def _merged_alias(group: Sequence[_Atom]) -> str | None:
    """Multi-word key names written with spaces or hyphens ("page up", "volume-up")."""
    if any(kind != "word" for kind, _, _ in group) or not all(j for _, _, j in group[1:]):
        return None
    if len(group[0][1]) < 2:
        return None  # "f 1" stays two keys
    return KEY_ALIASES.get(_normalize_word("".join(value for _, value, _ in group)))


def _resolve_atoms(atoms: list[_Atom], text: str) -> list[tuple[str, str]]:
    resolved: list[tuple[str, str]] = []
    index = 0
    while index < len(atoms):
        kind, value, _ = atoms[index]
        if kind == "mod":
            resolved.append(("mod", value))
            index += 1
            continue
        for span in (3, 2):
            group = atoms[index : index + span]
            alias = _merged_alias(group) if len(group) == span else None
            if alias is not None:
                resolved.append(("key", alias))
                index += span
                break
        else:
            resolved.append(_resolve_word(value, text))
            index += 1
    return resolved


def _build_combo(resolved: list[tuple[str, str]], text: str) -> KeyCombo:
    modifiers = [value for kind, value in resolved if kind == "mod"]
    keys = [(kind, value) for kind, value in resolved if kind != "mod"]
    if len(keys) > 1:
        names = " and ".join(repr(value) for _, value in keys)
        raise _invalid(
            f"{text!r} has more than one key ({names}).",
            hint="Press keys one after another with a sequence: a list, or combos separated by "
            "spaces ('cmd+a delete').",
        )
    if not keys:
        if not modifiers:
            raise _invalid(f"No key in {text!r}.")
        key, uppercase = modifiers[-1], False
    else:
        kind, key = keys[0]
        uppercase = kind == "upper"
    mods = set(modifiers)
    mods.discard(key)
    if uppercase and not mods & _CHARACTER_MODIFIERS:
        mods.add("shift")
    return KeyCombo(key, frozenset(mods))


def parse_combo(text: str) -> KeyCombo:
    """Parse one shortcut such as ``"cmd+shift+t"``, ``"⌘⇧T"`` or ``"Command-Shift-T"``.

    See the module docstring for the accepted forms. Raises ``INVALID_ARGUMENT`` for
    empty input, unknown key names and combos with more than one non-modifier key.
    """
    if not isinstance(text, str):
        raise _invalid(f"A key combo must be a string, not {type(text).__name__}.")
    cleaned = _clean(text).strip()
    if not cleaned:
        raise _invalid("The key combo is empty.")
    if len(cleaned) > MAX_COMBO_LENGTH:
        raise _invalid(f"The key combo is too long ({len(cleaned)} characters).")
    rest, literal = _split_trailing_literal(cleaned)
    resolved = _resolve_atoms(_atoms(rest, trailing_caret_is_key=literal is None), cleaned)
    if literal is not None:
        resolved.append(("key", literal))
    return _build_combo(resolved, cleaned)


def _needs_more(chunk: str) -> bool:
    """True if ``chunk`` ends with a separator that still expects a key (``"cmd+"``)."""
    if len(chunk) < 2 or chunk[-1] not in _SEPARATORS:
        return False
    before = chunk[-2]
    return before not in _SEPARATORS and before not in _GLYPH_MODIFIERS


def _split_sequence(text: str) -> list[str]:
    """Group whitespace-separated chunks into combos (``"cmd + shift + t"`` is one combo)."""
    combos: list[str] = []
    for chunk in text.split():
        if combos and (
            _needs_more(combos[-1]) or (chunk[0] in _SEPARATORS and combos[-1][-1] not in _SEPARATORS)
        ):
            combos[-1] += chunk
        else:
            combos.append(chunk)
    return combos


def _parse_joined(chunks: list[str], index: int) -> tuple[KeyCombo, int]:
    """Parse the combo starting at ``chunks[index]``; returns it and how many chunks it used.

    A chunk that fails on its own is retried joined with the next one or two chunks, so a
    spaced key name such as ``"page up"`` still works. The single-chunk error is raised if
    no join parses either.
    """
    try:
        return parse_combo(chunks[index]), 1
    except AXPilotError as error:
        first_error = error
    for span in (2, 3):
        if index + span <= len(chunks):
            try:
                return parse_combo(" ".join(chunks[index : index + span])), span
            except AXPilotError:
                pass
    raise first_error


def _parse_chunks(chunks: list[str]) -> list[KeyCombo]:
    combos: list[KeyCombo] = []
    index = 0
    while index < len(chunks):
        parsed, used = _parse_joined(chunks, index)
        combos.append(parsed)
        index += used
    return combos


def parse_sequence(keys: str | Sequence[str]) -> list[KeyCombo]:
    """Parse several combos pressed one after another.

    ``keys`` is a list of combos (``["cmd+a", "delete"]``) or one string with spaces between
    combos (``"cmd+a delete"``). Spaces around ``+`` keep a combo together
    (``"cmd + shift + t"`` is one combo). Raises ``INVALID_ARGUMENT`` for empty input,
    more than :data:`MAX_SEQUENCE_LENGTH` combos, or any invalid combo.
    """
    if isinstance(keys, str):
        cleaned = _clean(keys)
        if len(cleaned) > MAX_COMBO_LENGTH * MAX_SEQUENCE_LENGTH:
            raise _invalid(f"The key sequence is too long ({len(cleaned)} characters).")
        combos = _parse_chunks(_split_sequence(cleaned))
    elif isinstance(keys, Sequence):
        if len(keys) > MAX_SEQUENCE_LENGTH:
            raise _invalid(f"Too many key combos ({len(keys)}); the limit is {MAX_SEQUENCE_LENGTH}.")
        combos = [parse_combo(item) for item in keys]
    else:
        raise _invalid(f"Keys must be a string or a list of strings, not {type(keys).__name__}.")
    if not combos:
        raise _invalid("No keys to press.")
    if len(combos) > MAX_SEQUENCE_LENGTH:
        raise _invalid(f"Too many key combos ({len(combos)}); the limit is {MAX_SEQUENCE_LENGTH}.")
    return combos


def _is_ascii_letter(key: str) -> bool:
    return len(key) == 1 and key.isascii() and key.isalpha()


def to_glyphs(combo: KeyCombo) -> str:
    """Render ``combo`` the way macOS menus show shortcuts, e.g. ``"⇧⌘T"`` or ``"⌃⌥⌘↩"``.

    Modifiers come first in Apple's order (🌐⌃⌥⇧⌘). The result parses back to ``combo``.
    """
    glyphs = "".join(_MODIFIER_GLYPHS[m] for m in _GLYPH_ORDER if m in combo.modifiers)
    key = combo.key
    if key in _MODIFIER_GLYPHS:
        key_text = _MODIFIER_GLYPHS[key]
    elif key in _KEY_GLYPHS:
        key_text = _KEY_GLYPHS[key]
    elif key in _FUNCTION_KEYS or (_is_ascii_letter(key) and combo.modifiers):
        key_text = key.upper()  # "F5", and "⌘T" like macOS menus (a bare "T" would add shift)
    else:
        key_text = key
    return glyphs + key_text
