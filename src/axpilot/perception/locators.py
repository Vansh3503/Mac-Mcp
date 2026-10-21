"""Portable locators: describe an element so a script can find it again in a later session.

Refs (``e14``) live for one server session. A *locator* is a small JSON object that names an
element by what the app says about it (its ``AXIdentifier``, role and name, the containers it
sits in, or as a last resort its position) and survives relaunches::

    {"v": 1, "app": "com.example.MyApp", "role": "AXButton", "identifier": "downloadButton"}

:func:`parse_locator` validates one (``INVALID_ARGUMENT``), :func:`match_elements` finds the
elements it describes in a snapshot, :func:`candidates_for` builds ranked locators for one
element and proves each one unique, and :func:`export_locator` renders one for a script.

Matching is exact: every field given must hold (AND). Roles are an AX role (``AXButton``) or a
short role (``button``); identifiers compare exactly; names compare after collapsing whitespace
and writing ``...`` as ``…`` (case matters), or by prefix/substring with ``name_match``;
``within`` steps match the element's ancestors as a subsequence (not necessarily direct
parents); ``index`` picks one match in document order. Matching runs over the elements of a
``full`` snapshot (every visible element of the window); OCR text never matches.

A locator never holds a pid, a window id, a ref, a coordinate or a field's value.
"""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import WindowInfo
from axpilot.perception.model import Element, Snapshot
from axpilot.perception.roles import TEXT_INPUT_ROLES, short_role
from axpilot.perception.vision import is_vision_element

__all__ = [
    "EXPORT_FORMATS",
    "LOCATOR_VERSION",
    "Candidate",
    "ExportFormat",
    "Grade",
    "LocatorSpec",
    "LocatorStep",
    "NameMatch",
    "Strategy",
    "WindowSelector",
    "candidates_for",
    "default_window",
    "export_locator",
    "export_locators",
    "format_path",
    "is_unique",
    "locator_warnings",
    "looks_generated",
    "match_elements",
    "parse_locator",
    "parse_path",
    "pick_window",
    "window_selector_for",
]

LOCATOR_VERSION = 1
"""Version of the locator format (the ``v`` field)."""

NameMatch = Literal["exact", "prefix", "contains"]
WindowMatch = Literal["exact", "prefix"]
Grade = Literal["high", "medium", "low"]
"""How likely a locator keeps working: ``high`` survives language and layout changes, ``medium``
survives layout changes, ``low`` breaks when the window's layout changes."""
Strategy = Literal["identifier", "name", "name_within", "role_within", "index", "path"]
ExportFormat = Literal["axpilot", "axpilot-python"]
EXPORT_FORMATS: tuple[ExportFormat, ...] = ("axpilot", "axpilot-python")

Path = tuple[tuple[str, int], ...]

MAX_STEPS = 5
MAX_PATH_STEPS = 64
MAX_INDEX = 9999
MAX_WINDOW_INDEX = 99
_MAX_TEXT = 500
_MAX_ID = 256
_MAX_ROLE = 64

_KEYS = frozenset(
    {"v", "app", "window", "within", "role", "subrole", "identifier", "name", "name_match", "index", "path"}
)
_STEP_KEYS = frozenset({"role", "identifier", "name"})
_WINDOW_KEYS = frozenset({"title", "match", "index"})
_NAME_MATCHES: tuple[NameMatch, ...] = ("exact", "prefix", "contains")
_WINDOW_MATCHES: tuple[WindowMatch, ...] = ("exact", "prefix")
_GRADE_RANK: dict[Grade, int] = {"high": 0, "medium": 1, "low": 2}
_LOWER: dict[Grade, Grade] = {"high": "medium", "medium": "low", "low": "low"}

_PATH_STEP = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\[(\d{1,5})\]")
_GENERATED_ID = re.compile(
    r"_NS:\d+"  # Interface Builder's automatic identifiers
    r"|\d+"
    r"|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"|[0-9A-Fa-f]{16,}"
)
_DIGIT = re.compile(r"\d")
_PREFIX_TRAILER = " \t-–—:#([{·,"
_MIN_PREFIX = 3
_MAX_SCOPE_STEPS = 3

SCOPE_ROLES: frozenset[str] = frozenset(
    {
        "AXToolbar",
        "AXSheet",
        "AXPopover",
        "AXTabGroup",
        "AXMenu",
        "AXList",
        "AXTable",
        "AXOutline",
        "AXBrowser",
        "AXGrid",
        "AXWebArea",
        "AXRadioGroup",
        "AXScrollArea",
        "AXGroup",
        "AXSplitGroup",
        "AXRow",
    }
)
"""Ancestors a locator may name in ``within`` to tell similar elements apart (a named row: "the
Open button in the row of Report.pdf")."""

_NAMED_SCOPES_ONLY: frozenset[str] = frozenset({"AXScrollArea", "AXGroup", "AXSplitGroup", "AXRow"})
"""Scope roles that are only worth naming when they have a name (unnamed ones are layout noise)."""

LABEL_ROLES: frozenset[str] = frozenset(
    {
        "AXButton",
        "AXMenuButton",
        "AXMenuItem",
        "AXMenuBarItem",
        "AXPopUpButton",
        "AXRadioButton",
        "AXCheckBox",
        "AXDisclosureTriangle",
    }
)
"""Controls whose name is a label that may carry a count (``"Download (3)"``): they also get a
locator by the start of the name. Rows and text are left alone: there a number usually is
part of what the item is (``"Report 2024.pdf"``)."""

_TRANSIENT_ROLES: dict[str, str] = {"AXMenu": "menu", "AXPopover": "popover", "AXSheet": "sheet"}

_EXAMPLE_HINT = (
    'A locator is an object such as {"app": "com.apple.finder", "role": "AXButton", "name": "Back"}; '
    "locate(ref=…) builds one for an element."
)


# --------------------------------------------------------------------------- the format
@dataclass(frozen=True, slots=True)
class LocatorStep:
    """One ancestor a locator's element must be inside (``within``)."""

    role: str | None = None
    identifier: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The step as JSON (only the fields that are set)."""
        data: dict[str, Any] = {}
        if self.role is not None:
            data["role"] = self.role
        if self.identifier is not None:
            data["identifier"] = self.identifier
        if self.name is not None:
            data["name"] = self.name
        return data


@dataclass(frozen=True, slots=True)
class WindowSelector:
    """Which of the app's windows to search: by title, or by position front to back."""

    title: str | None = None
    match: WindowMatch = "exact"
    index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """The selector as JSON (only the fields that are set)."""
        if self.index is not None:
            return {"index": self.index}
        data: dict[str, Any] = {"title": self.title}
        if self.match != "exact":
            data["match"] = self.match
        return data


@dataclass(frozen=True, slots=True)
class LocatorSpec:
    """A parsed, validated locator (see the module docstring for the matching rules)."""

    app: str
    """Bundle id (what :func:`candidates_for` writes) or app name."""
    window: WindowSelector | None = None
    """``None``: the app's focused (or main) window, as a snapshot without ``window_id`` takes."""
    within: tuple[LocatorStep, ...] = ()
    """Ancestors, outermost first."""
    role: str | None = None
    subrole: str | None = None
    identifier: str | None = None
    name: str | None = None
    name_match: NameMatch = "exact"
    index: int | None = None
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """The locator as JSON, with ``v`` first and only the fields that are set."""
        data: dict[str, Any] = {"v": LOCATOR_VERSION, "app": self.app}
        if self.window is not None:
            data["window"] = self.window.to_dict()
        if self.within:
            data["within"] = [step.to_dict() for step in self.within]
        if self.role is not None:
            data["role"] = self.role
        if self.subrole is not None:
            data["subrole"] = self.subrole
        if self.identifier is not None:
            data["identifier"] = self.identifier
        if self.name is not None:
            data["name"] = self.name
            if self.name_match != "exact":
                data["name_match"] = self.name_match
        if self.index is not None:
            data["index"] = self.index
        if self.path is not None:
            data["path"] = format_path(self.path)
        return data

    def to_json(self) -> str:
        """:meth:`to_dict` as one line of JSON."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(", ", ": "))

    def describe(self) -> str:
        """A short human description for messages (``AXButton "Back" in com.apple.finder``)."""
        parts = [self.role or "element"]
        if self.identifier is not None:
            parts.append(f"#{self.identifier}")
        if self.name is not None:
            parts.append(json.dumps(self.name, ensure_ascii=False))
        if self.path is not None:
            parts.append(f"at {format_path(self.path)}")
        if self.index is not None:
            parts.append(f"[{self.index}]")
        return " ".join(parts) + f" in {self.app}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One locator :func:`candidates_for` proposes for an element."""

    spec: LocatorSpec
    strategy: Strategy
    grade: Grade
    matches: int
    """Elements the locator matches in the snapshot (always 1 for a returned candidate)."""
    unique: bool | None
    """``True``: proven unique in the window; ``None``: unique in a capture that was cut short."""
    reasons: tuple[str, ...] = field(default=())


# --------------------------------------------------------------------------- parsing
def _invalid(message: str) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=_EXAMPLE_HINT)


def _text(value: Any, where: str, limit: int, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{where} must be a string, got {type(value).__name__}.")
    text = value.strip() if strip else value
    if not text.strip():
        raise _invalid(f"{where} must not be empty.")
    if len(text) > limit:
        raise _invalid(f"{where} is longer than {limit} characters.")
    return text


def _integer(value: Any, where: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{where} must be an integer, got {type(value).__name__}.")
    if not 0 <= value <= maximum:
        raise _invalid(f"{where} must be between 0 and {maximum}, got {value}.")
    return value


def _check_keys(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise _invalid(
            f"{where} has unknown field(s): {', '.join(unknown)}. Known: {', '.join(sorted(allowed))}."
        )


def normalize_name(text: str) -> str:
    """Collapse whitespace and write ``...`` as ``…`` (how locator names are compared)."""
    return " ".join(text.replace("...", "…").split())


def _name(value: Any, where: str) -> str:
    return normalize_name(_text(value, where, _MAX_TEXT))


def _step(value: Any, where: str) -> LocatorStep:
    if not isinstance(value, Mapping):
        raise _invalid(f'{where} must be an object such as {{"role": "AXToolbar"}}.')
    data = cast(Mapping[str, Any], value)
    _check_keys(data, _STEP_KEYS, where)
    if not any(data.get(key) is not None for key in _STEP_KEYS):
        raise _invalid(f"{where} needs a role, identifier or name.")
    return LocatorStep(
        role=_text(data["role"], f"{where}.role", _MAX_ROLE) if data.get("role") is not None else None,
        identifier=(
            _text(data["identifier"], f"{where}.identifier", _MAX_ID, strip=False)
            if data.get("identifier") is not None
            else None
        ),
        name=_name(data["name"], f"{where}.name") if data.get("name") is not None else None,
    )


def _window(value: Any) -> WindowSelector:
    if not isinstance(value, Mapping):
        raise _invalid('locator.window must be an object such as {"title": "Library"} or {"index": 0}.')
    data = cast(Mapping[str, Any], value)
    _check_keys(data, _WINDOW_KEYS, "locator.window")
    title, index, match = data.get("title"), data.get("index"), data.get("match")
    if (title is None) == (index is None):
        raise _invalid("locator.window needs exactly one of title or index.")
    if index is not None:
        if match is not None:
            raise _invalid("locator.window.match only applies to a title.")
        return WindowSelector(index=_integer(index, "locator.window.index", MAX_WINDOW_INDEX))
    if match is not None and match not in _WINDOW_MATCHES:
        raise _invalid(f"locator.window.match must be one of: {', '.join(_WINDOW_MATCHES)}.")
    return WindowSelector(
        title=_text(title, "locator.window.title", _MAX_TEXT), match=cast(WindowMatch, match or "exact")
    )


def parse_path(value: Any) -> Path:
    """``"AXToolbar[0]/AXButton[2]"`` (or ``["AXToolbar[0]", "AXButton[2]"]``, the form of
    ``inspect``'s ``locator_path``) → ``(("AXToolbar", 0), ("AXButton", 2))``.

    Each step is an AX role and the index among the siblings with that role, from the window.
    Raises ``INVALID_ARGUMENT``."""
    if isinstance(value, str):
        text = value.strip().removeprefix("/")
        parts = text.split("/") if text else []
    elif isinstance(value, list | tuple):
        parts = []
        for item in cast(Sequence[Any], value):
            if not isinstance(item, str):
                raise _invalid('locator.path steps must be strings such as "AXButton[2]".')
            parts.append(item.strip())
    else:
        raise _invalid('locator.path must be a string such as "AXToolbar[0]/AXButton[2]".')
    if not parts:
        raise _invalid("locator.path must have at least one step.")
    if len(parts) > MAX_PATH_STEPS:
        raise _invalid(f"locator.path has more than {MAX_PATH_STEPS} steps.")
    path: list[tuple[str, int]] = []
    for part in parts:
        found = _PATH_STEP.fullmatch(part)
        if found is None:
            raise _invalid(f'locator.path step {part!r} is not of the form "AXRole[index]".')
        path.append((found.group(1), int(found.group(2))))
    return tuple(path)


def format_path(path: Path) -> str:
    """``(("AXToolbar", 0), ("AXButton", 2))`` → ``"AXToolbar[0]/AXButton[2]"``."""
    return "/".join(f"{role}[{index}]" for role, index in path)


def parse_locator(data: Any) -> LocatorSpec:
    """Validate a locator object (a JSON object as a mapping) into a :class:`LocatorSpec`.

    Unknown fields are refused rather than ignored, so a typo never widens what a locator
    matches. Raises ``INVALID_ARGUMENT`` naming the bad field."""
    if not isinstance(data, Mapping):
        raise _invalid("A locator must be an object.")
    raw = cast(Mapping[str, Any], data)
    _check_keys(raw, _KEYS, "locator")
    version = raw.get("v")
    if version is not None and (isinstance(version, bool) or version != LOCATOR_VERSION):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unsupported locator version {version!r}; this AXPilot reads version {LOCATOR_VERSION}.",
            hint="Update AXPilot, or build the locator again with locate.",
        )
    if raw.get("app") is None:
        raise _invalid("locator.app is required (a bundle id such as com.apple.finder, or an app name).")
    within_raw = raw.get("within")
    within: tuple[LocatorStep, ...] = ()
    if within_raw is not None:
        if not isinstance(within_raw, list | tuple):
            raise _invalid("locator.within must be a list of steps, outermost first.")
        steps = cast(Sequence[Any], within_raw)
        if len(steps) > MAX_STEPS:
            raise _invalid(f"locator.within has more than {MAX_STEPS} steps.")
        within = tuple(_step(item, f"locator.within[{i}]") for i, item in enumerate(steps))
    name_match = raw.get("name_match")
    if name_match is not None:
        if name_match not in _NAME_MATCHES:
            raise _invalid(f"locator.name_match must be one of: {', '.join(_NAME_MATCHES)}.")
        if raw.get("name") is None:
            raise _invalid("locator.name_match needs a name.")
    spec = LocatorSpec(
        app=_text(raw["app"], "locator.app", _MAX_ID),
        window=_window(raw["window"]) if raw.get("window") is not None else None,
        within=within,
        role=_text(raw["role"], "locator.role", _MAX_ROLE) if raw.get("role") is not None else None,
        subrole=(
            _text(raw["subrole"], "locator.subrole", _MAX_ROLE) if raw.get("subrole") is not None else None
        ),
        identifier=(
            _text(raw["identifier"], "locator.identifier", _MAX_ID, strip=False)
            if raw.get("identifier") is not None
            else None
        ),
        name=_name(raw["name"], "locator.name") if raw.get("name") is not None else None,
        name_match=cast(NameMatch, name_match or "exact"),
        index=_integer(raw["index"], "locator.index", MAX_INDEX) if raw.get("index") is not None else None,
        path=parse_path(raw["path"]) if raw.get("path") is not None else None,
    )
    if spec.role is None and spec.identifier is None and spec.name is None and spec.path is None:
        raise _invalid("A locator needs at least one of role, identifier, name or path.")
    return spec


# --------------------------------------------------------------------------- matching
def _role_matches(wanted: str, ax_role: str, short: str) -> bool:
    folded = wanted.casefold()
    if folded.startswith("ax"):
        return ax_role.casefold() == folded
    return short == folded


def _name_matches(wanted: str, how: NameMatch, have: str | None) -> bool:
    if have is None:
        return False
    current = normalize_name(have)
    if how == "prefix":
        return current.startswith(wanted)
    if how == "contains":
        return wanted in current
    return current == wanted


def _step_matches(step: LocatorStep, el: Element) -> bool:
    if step.role is not None and not _role_matches(step.role, el.ax_role, el.role):
        return False
    if step.identifier is not None and el.identifier != step.identifier:
        return False
    return step.name is None or _name_matches(step.name, "exact", el.name)


def _ancestors(snap: Snapshot, el: Element) -> list[Element]:
    """``el``'s ancestors in ``snap``, outermost first."""
    chain: list[Element] = []
    parent = snap.element(el.parent_ref) if el.parent_ref else None
    while parent is not None and len(chain) < 256:
        chain.append(parent)
        parent = snap.element(parent.parent_ref) if parent.parent_ref else None
    chain.reverse()
    return chain


def _within(steps: tuple[LocatorStep, ...], ancestors: list[Element]) -> bool:
    """``steps`` match ``ancestors`` as a subsequence (greedy, outermost first)."""
    position = 0
    for step in steps:
        while position < len(ancestors) and not _step_matches(step, ancestors[position]):
            position += 1
        if position == len(ancestors):
            return False
        position += 1
    return True


def _element_matches(spec: LocatorSpec, snap: Snapshot, el: Element) -> bool:
    if spec.role is not None and not _role_matches(spec.role, el.ax_role, el.role):
        return False
    if spec.subrole is not None and (el.subrole or "").casefold() != spec.subrole.casefold():
        return False
    if spec.identifier is not None and el.identifier != spec.identifier:
        return False
    if spec.name is not None and not _name_matches(spec.name, spec.name_match, el.name):
        return False
    if spec.path is not None and el.locator.path != spec.path:
        return False
    return not spec.within or _within(spec.within, _ancestors(snap, el))


def match_elements(snap: Snapshot, spec: LocatorSpec) -> list[Element]:
    """The elements of ``snap`` that ``spec`` describes, in document order.

    ``spec.app`` and ``spec.window`` are not checked here: they choose which window to capture.
    With ``spec.index`` the result has at most one element."""
    found = [el for el in snap.all_elements if not is_vision_element(el) and _element_matches(spec, snap, el)]
    if spec.index is not None:
        return found[spec.index : spec.index + 1]
    return found


def is_unique(snap: Snapshot, count: int) -> bool | None:
    """``True`` for exactly one match in a complete capture; ``None`` for one match in a capture
    that was cut short by budgets (another match may lie beyond it); ``False`` otherwise."""
    if count != 1:
        return False
    return None if snap.truncated else True


# --------------------------------------------------------------------------- windows
def _usable(windows: Sequence[WindowInfo]) -> list[WindowInfo]:
    return [window for window in windows if not window.minimized]


def default_window(windows: Sequence[WindowInfo]) -> WindowInfo | None:
    """The window a capture without ``window_id`` reads: the focused one, else the main one,
    else the frontmost (``windows`` are one app's windows, front to back)."""
    usable = _usable(windows)
    for flag in ("focused", "main"):
        window = next((w for w in usable if getattr(w, flag)), None)
        if window is not None:
            return window
    return usable[0] if usable else None


def window_selector_for(windows: Sequence[WindowInfo], window: WindowInfo | None) -> WindowSelector | None:
    """How a locator should name ``window`` among its app's ``windows``: ``None`` when it is the
    window a capture reads anyway, else its title when no other window has it, else its index."""
    if window is None:
        return None
    usable = _usable(windows)
    front = default_window(usable)
    if front is None or front.window_id == window.window_id:
        return None
    position = next((i for i, w in enumerate(usable) if w.window_id == window.window_id), None)
    title = normalize_name(window.title) if window.title else ""
    if title and sum(1 for w in usable if normalize_name(w.title or "") == title) == 1:
        return WindowSelector(title=title)
    if position is None or position > MAX_WINDOW_INDEX:
        return None
    return WindowSelector(index=position)


def pick_window(windows: Sequence[WindowInfo], selector: WindowSelector, app: str) -> WindowInfo:
    """The window of ``windows`` (one app's, front to back) that ``selector`` names; several
    windows with a matching title give the frontmost. Raises ``WINDOW_NOT_FOUND``."""
    usable = _usable(windows)
    if selector.index is not None:
        if selector.index < len(usable):
            return usable[selector.index]
        raise AXPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"{app} has {len(usable)} open window(s); the locator asks for window index {selector.index}.",
            hint="Use list_windows to see the app's windows, front to back.",
        )
    wanted = normalize_name(selector.title or "")
    for window in usable:
        title = normalize_name(window.title or "")
        if title == wanted or (selector.match == "prefix" and title.startswith(wanted)):
            return window
    how = "starting with" if selector.match == "prefix" else "titled"
    raise AXPilotError(
        ErrorCode.WINDOW_NOT_FOUND,
        f"{app} has no window {how} {json.dumps(wanted, ensure_ascii=False)}.",
        hint="Use list_windows to see the app's windows, or leave out window to use its front window.",
    )


# --------------------------------------------------------------------------- candidates
def looks_generated(identifier: str) -> bool:
    """True for identifiers a toolkit made up rather than the developer chose: Interface
    Builder's ``_NS:123``, plain numbers, UUIDs and long hex strings. They change between builds."""
    return _GENERATED_ID.fullmatch(identifier.strip()) is not None


def _label(el: Element) -> str:
    return f"{el.role} {json.dumps(el.name, ensure_ascii=False)}" if el.name else el.role


def _scope_steps(ancestors: list[Element]) -> list[tuple[LocatorStep, Element]]:
    """Ancestors worth naming in ``within``, nearest first (at most three)."""
    steps: list[tuple[LocatorStep, Element]] = []
    for ancestor in reversed(ancestors):
        if not ancestor.locator.path:  # the window (or app) itself: the window selector covers it
            continue
        if ancestor.identifier and not looks_generated(ancestor.identifier):
            steps.append((LocatorStep(role=ancestor.ax_role, identifier=ancestor.identifier), ancestor))
        elif ancestor.ax_role in SCOPE_ROLES and (
            ancestor.name or ancestor.ax_role not in _NAMED_SCOPES_ONLY
        ):
            name = normalize_name(ancestor.name) if ancestor.name else None
            steps.append((LocatorStep(role=ancestor.ax_role, name=name), ancestor))
        if len(steps) == _MAX_SCOPE_STEPS:
            break
    return steps


def _stable_prefix(name: str) -> str | None:
    """The part of a name before its first digit (``"Download (3)"`` → ``"Download"``), when that
    is long enough to mean something."""
    found = _DIGIT.search(name)
    if found is None:
        return None
    prefix = name[: found.start()].rstrip(_PREFIX_TRAILER)
    return prefix if len(prefix) >= _MIN_PREFIX else None


class _Builder:
    """Collects candidates for one element, checking each against the snapshot."""

    def __init__(self, snap: Snapshot, el: Element) -> None:
        self.snap = snap
        self.el = el
        self.complete = not snap.truncated
        self.found: list[Candidate] = []
        self._seen: set[str] = set()

    def matches(self, spec: LocatorSpec) -> list[Element]:
        return match_elements(self.snap, spec)

    def offer(self, spec: LocatorSpec, strategy: Strategy, grade: Grade, reasons: Sequence[str]) -> bool:
        """Keep ``spec`` if it matches exactly the element; ``True`` if it does."""
        found = self.matches(spec)
        if len(found) != 1 or found[0].ref != self.el.ref:
            return False
        notes = list(reasons)
        if not self.complete:
            grade = _LOWER[grade]
            notes.append("not proven unique: the window has more elements than one capture reads")
        key = spec.to_json()
        if key not in self._seen:
            self._seen.add(key)
            self.found.append(
                Candidate(
                    spec=spec,
                    strategy=strategy,
                    grade=grade,
                    matches=1,
                    unique=True if self.complete else None,
                    reasons=tuple(notes),
                )
            )
        return True

    def scoped(
        self,
        spec: LocatorSpec,
        steps: list[tuple[LocatorStep, Element]],
        strategy: Strategy,
        grade: Grade,
        reasons: Sequence[str],
    ) -> LocatorSpec | None:
        """Try ``spec`` inside the nearest one, two and three scope steps; the first that is
        unique is kept and returned (``None`` if none is)."""
        for count in range(1, len(steps) + 1):
            chosen = steps[:count]
            scoped = replace(spec, within=tuple(step for step, _ in reversed(chosen)))
            where = ", ".join(f"in {_label(ancestor)}" for _, ancestor in chosen)
            if self.offer(scoped, strategy, grade, [*reasons, f"scoped {where}"]):
                return scoped
        return None


_ID_REASON = "AXIdentifier set by the app: survives language and layout changes"
_GENERATED_REASON = "the identifier looks generated by the toolkit: it may change when the app is rebuilt"
_NAME_REASON = "the name is UI text: it changes with the app's language and copy edits"
_PREFIX_REASON = "matches the start of the name: the rest contains a number that may change"
_NUMBER_REASON = (
    "the name contains a number: if it changes (a count, a percentage), the locator stops matching"
)
_ROLE_WITHIN_REASON = "only the role and its container: breaks if a similar element is added there"
_INDEX_REASON = "position among similar elements: breaks if one is added or removed"
_PATH_REASON = "position in the window's tree: breaks on any layout change"


def _worst(first: Grade, second: Grade) -> Grade:
    return first if _GRADE_RANK[first] >= _GRADE_RANK[second] else second


def candidates_for(
    snap: Snapshot,
    el: Element,
    *,
    app: str,
    window: WindowSelector | None = None,
    limit: int = 3,
) -> list[Candidate]:
    """Ranked locators for ``el``, each proven to match exactly ``el`` in ``snap`` (a ``full``
    snapshot of its window), best first: an ``AXIdentifier`` (``high``; ``low`` if it looks
    generated), the role and name, optionally inside its toolbar, sheet, list or another
    named container (``medium``), the role inside its containers, a position among similar
    elements or the position in the tree (``low``). A capture cut short by budgets lowers every
    grade by one and marks ``unique`` as unknown. Raises ``NOT_SUPPORTED`` for OCR text and
    ``INVALID_ARGUMENT`` for a bad ``limit``."""
    if limit < 1:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"limit must be at least 1, got {limit}.")
    if is_vision_element(el):
        raise AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            f"{el.ref} is OCR text, not an accessibility element, so no locator can name it.",
            hint='Use an element from snapshot(mode="interactive"), or the menu bar (select_menu).',
        )
    builder = _Builder(snap, el)
    base = LocatorSpec(app=app, window=window, role=el.ax_role)
    steps = _scope_steps(_ancestors(snap, el))
    name = normalize_name(el.name) if el.name else None
    identifier = el.identifier
    generated = identifier is not None and looks_generated(identifier)
    if identifier:
        grade: Grade = "low" if generated else "high"
        reasons = [_GENERATED_REASON if generated else _ID_REASON]
        by_id = replace(base, identifier=identifier)
        if not builder.offer(by_id, "identifier", grade, reasons):
            id_steps = [pair for pair in steps if pair[0].identifier is not None]
            if builder.scoped(by_id, id_steps, "identifier", grade, reasons) is None and name:
                named = replace(by_id, name=name)
                worse, more = _worst(grade, "medium"), [*reasons, _NAME_REASON]
                if not builder.offer(named, "identifier", worse, more):
                    builder.scoped(named, steps, "identifier", worse, more)
    if name:
        reasons = [_NAME_REASON, *([_NUMBER_REASON] if _DIGIT.search(name) else [])]
        by_name = replace(base, name=name)
        if not builder.offer(by_name, "name", "medium", reasons):
            builder.scoped(by_name, steps, "name_within", "medium", reasons)
        prefix = _stable_prefix(name) if el.ax_role in LABEL_ROLES else None
        if prefix is not None:
            by_prefix = replace(base, name=prefix, name_match="prefix")
            more = [_NAME_REASON, _PREFIX_REASON]
            if not builder.offer(by_prefix, "name", "medium", more):
                builder.scoped(by_prefix, steps, "name_within", "medium", more)
    elif not identifier and steps:
        builder.scoped(base, steps, "role_within", "low", [_ROLE_WITHIN_REASON])
    if not builder.found:
        fallback = replace(
            base,
            identifier=None if generated else identifier,
            name=name,
            within=tuple(step for step, _ in reversed(steps)),
        )
        position = next((i for i, other in enumerate(builder.matches(fallback)) if other.ref == el.ref), None)
        if position is not None and position <= MAX_INDEX:
            builder.offer(replace(fallback, index=position), "index", "low", [_INDEX_REASON])
    if el.locator.path:
        builder.offer(
            LocatorSpec(app=app, window=window, path=el.locator.path), "path", "low", [_PATH_REASON]
        )
    ranked = sorted(builder.found, key=lambda candidate: _GRADE_RANK[candidate.grade])
    return ranked[:limit]


def locator_warnings(snap: Snapshot, el: Element, recommended: Candidate | None) -> list[str]:
    """What a script author should know about locating ``el``: no usable identifier, a toolbar
    that can overflow, a menu, popover or sheet that must be open, a capture cut short."""
    warnings: list[str] = []
    if recommended is None:
        warnings.append(f"No locator names {el.ref} ({_label(el)}) on its own in this window.")
    if el.identifier is None or looks_generated(el.identifier):
        what = "sets no AXIdentifier" if el.identifier is None else "gives it only a generated identifier"
        warnings.append(
            f"{snap.app.name} {what} on {_label(el)}, so the locator relies on its name or position. "
            "If you build the app, give it an identifier: setAccessibilityIdentifier(_:) in AppKit, "
            ".accessibilityIdentifier(_:) in SwiftUI."
        )
    ancestors = _ancestors(snap, el)
    roles = {ancestor.ax_role for ancestor in ancestors}
    if "AXToolbar" in roles or el.ax_role == "AXToolbar":
        warnings.append(
            "The element is in a toolbar: in a narrow window it can move into the toolbar's overflow "
            "menu (»), where this locator no longer finds it."
        )
    for role, word in _TRANSIENT_ROLES.items():
        if role in roles:
            warnings.append(
                f"The element is inside a {word}: a script has to open the {word} before using it."
            )
    if snap.truncated:
        warnings.append(
            "This window has more elements than one capture reads, so uniqueness could not be proven."
        )
    return warnings


# --------------------------------------------------------------------------- export
_PY_WORD = re.compile(r"[^\W_]+")
_CAMEL = re.compile(r"([a-z])([A-Z])")


def _python_name(el: Element | None, spec: LocatorSpec) -> str:
    """A constant name for the locator in a script (``"Download"`` → ``DOWNLOAD``)."""
    source = (el.name if el is not None else None) or spec.name or spec.identifier or ""
    words = _PY_WORD.findall(_CAMEL.sub(r"\1 \2", source))[:4]
    name = "_".join(word.upper() for word in words)[:40].rstrip("_")
    if not name:
        return "ELEMENT"
    if not name[0].isalpha():
        name = f"ELEMENT_{name}"
    return name if name.isidentifier() and not keyword.iskeyword(name) else "ELEMENT"


def _unique_names(items: Sequence[tuple[LocatorSpec, Element | None]]) -> list[str]:
    """One constant name per item; repeats get ``_2``, ``_3``…"""
    names: list[str] = []
    seen: dict[str, int] = {}
    for spec, el in items:
        base = _python_name(el, spec)
        seen[base] = seen.get(base, 0) + 1
        names.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return names


def _python_action(el: Element | None, spec: LocatorSpec, name: str) -> str:
    """The SDK call a script most likely makes on the element. A secure field reads its text from
    an environment variable, so no password ends up in the script."""
    if el is not None and el.secure:
        return f'type_text(os.environ["{name}"])  # a secure field: needs [safety] allow_secure_fields'
    role = el.ax_role if el is not None else spec.role
    if role is not None and (role in TEXT_INPUT_ROLES or short_role(role) in ("field", "textarea")):
        return 'type_text("your text")'
    return "click()"


def export_locators(items: Sequence[tuple[LocatorSpec, Element | None]], fmt: ExportFormat) -> str:
    """Several locators for one script, in order (the steps of a flow): ``axpilot`` is a JSON object
    of named locators; ``axpilot-python`` a script with one constant per locator and one SDK call
    per step. Each item's element (optional) picks its name and action. Raises
    ``INVALID_ARGUMENT`` for an unknown format or no items."""
    if not items:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Nothing to export: no locators were given.")
    names = _unique_names(items)
    if fmt == "axpilot":
        named = {name: spec.to_dict() for name, (spec, _) in zip(names, items, strict=True)}
        return json.dumps(named, ensure_ascii=False, indent=2)
    if fmt == "axpilot-python":
        actions = [_python_action(el, spec, name) for name, (spec, el) in zip(names, items, strict=True)]
        lines = ["import os", ""] if any("os.environ" in action for action in actions) else []
        lines += ["from axpilot import Desktop", ""]
        lines += [f"{name} = {spec.to_dict()!r}" for name, (spec, _) in zip(names, items, strict=True)]
        lines += ["", "with Desktop.connect() as mac:"]
        lines += [
            f"    mac.find(locator={name}).{action}" for name, action in zip(names, actions, strict=True)
        ]
        return "\n".join(lines)
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown export format {fmt!r}.",
        hint="Use one of: " + ", ".join(EXPORT_FORMATS) + ".",
    )


def export_locator(spec: LocatorSpec, fmt: ExportFormat, *, element: Element | None = None) -> str:
    """``spec`` for a script: ``axpilot`` is the locator as JSON; ``axpilot-python`` is a short
    script with the AXPilot SDK (``element`` picks the variable name and the action). Raises
    ``INVALID_ARGUMENT`` for an unknown format."""
    if fmt == "axpilot":
        return spec.to_json()
    return export_locators([(spec, element)], fmt)
