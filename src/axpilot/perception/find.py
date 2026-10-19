"""Fuzzy element search over a snapshot (the ``find`` tool)."""

from __future__ import annotations

import difflib
import re

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.perception.model import Element, Snapshot
from axpilot.perception.roles import short_role

# Score tiers. For a given field, each tier's best score stays below the next tier's worst, so a
# better kind of match wins; the difflib ratio (weight _RATIO_WEIGHT) breaks ties within a tier.
_EXACT = 0.91
_PREFIX = 0.71
_SUBSTRING = 0.51
_TOKENS = 0.31
_TOKENS_SPAN = 0.10
_FUZZY = 0.11
_FUZZY_SPAN = 0.10
_FUZZY_MIN_RATIO = 0.7
_RATIO_WEIGHT = 0.09

# Matching the name is worth slightly more than matching other fields.
_FIELD_PENALTY: dict[str, float] = {"name": 0.0, "identifier": 0.01, "value": 0.02, "description": 0.03}

_WORD = re.compile(r"\w+")


def normalize(text: str) -> str:
    """Case-fold, unify ellipses and collapse whitespace."""
    return " ".join(text.replace("…", "...").casefold().split())


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text)


def _tier(query: str, candidate: str) -> float:
    if candidate == query:
        return _EXACT
    if candidate.startswith(query):
        return _PREFIX
    if query in candidate:
        return _SUBSTRING
    query_tokens = _tokens(query)
    candidate_tokens = _tokens(candidate)
    if query_tokens and candidate_tokens:
        hits = sum(1 for qt in query_tokens if any(ct.startswith(qt) for ct in candidate_tokens))
        if hits:
            return _TOKENS + _TOKENS_SPAN * hits / len(query_tokens)
    return 0.0


def score_text(query: str, candidate: str | None) -> float:
    """Similarity of a normalized ``query`` to ``candidate`` in ``[0, 1]`` (0 = no match)."""
    if not candidate:
        return 0.0
    normalized = normalize(candidate)
    if not normalized:
        return 0.0
    ratio = difflib.SequenceMatcher(None, query, normalized).ratio()
    tier = _tier(query, normalized)
    if tier == 0.0:
        if ratio < _FUZZY_MIN_RATIO:
            return 0.0
        tier = _FUZZY + _FUZZY_SPAN * ratio
    return tier + _RATIO_WEIGHT * ratio


def _fields(el: Element) -> dict[str, str | None]:
    description = el.extra.get("description")
    return {
        "name": el.name,
        "identifier": el.identifier,
        "value": el.value,
        "description": description if isinstance(description, str) else None,
    }


def _element_score(el: Element, query: str) -> float:
    best = 0.0
    for field, text in _fields(el).items():
        score = score_text(query, text)
        if score > 0.0:
            best = max(best, score - _FIELD_PENALTY[field])
    return best


def role_matches(el: Element, role: str) -> bool:
    """True if ``role`` names the element's short role, AX role or subrole (case-insensitive)."""
    wanted = role.strip()
    folded = wanted.casefold()
    if not folded:
        return True
    if el.role == folded or el.ax_role.casefold() == folded:
        return True
    if el.subrole and el.subrole.casefold() == folded:
        return True
    if el.ax_role.casefold() == "ax" + folded:
        return True
    return folded.startswith("ax") and short_role(wanted) == el.role


def find_elements(
    snap: Snapshot,
    *,
    text: str | None = None,
    role: str | None = None,
    identifier: str | None = None,
    limit: int = 20,
) -> list[tuple[Element, float]]:
    """Search ``snap.all_elements``; returns ``(element, score)`` pairs, best first.

    ``text`` is matched case-insensitively against name, identifier, value and description
    (exact > prefix > substring > word-prefix overlap > typo-tolerant similarity). ``role``
    accepts short roles (``button``) or AX roles (``AXButton``); ``identifier`` must match the
    ``AXIdentifier`` exactly (case-insensitive). Filters combine with AND; with no ``text``
    every element passing the filters scores 1.0 and pre-order is kept. Ties prefer
    interactive elements, then document order.
    """
    if limit < 1:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"limit must be at least 1, got {limit}.")
    query = normalize(text) if text else ""
    role = role.strip() if role else None
    wanted_id = identifier.strip().casefold() if identifier and identifier.strip() else None
    if not (query or role or wanted_id):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            "find needs at least one of text, role or identifier.",
            hint='Example: find(text="Save") or find(role="button").',
        )
    scored: list[tuple[Element, float]] = []
    for el in snap.all_elements:
        if role and not role_matches(el, role):
            continue
        if wanted_id and (el.identifier or "").casefold() != wanted_id:
            continue
        score = _element_score(el, query) if query else 1.0
        if score > 0.0:
            scored.append((el, round(min(score, 1.0), 4)))
    scored.sort(key=lambda pair: (-pair[1], not pair[0].interactive))
    return scored[:limit]
