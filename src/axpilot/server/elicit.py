"""Confirmations through the MCP client (elicitation), for direct mode.

When AXPilot runs inside the client's own process tree (``axpilot stdio --direct``, ``--engine
fake``, ``axpilot http``) and the client declared the form-elicitation capability, a confirmation
is asked through the client instead of the context's confirmer:

* **protocol 2025-11-25 and older:** a standalone ``elicitation/create`` request during the call
  (:func:`ask_during_call`);
* **protocol 2026-07-28 and newer** (stateless, "multi round-trip"): the call ends with an
  ``InputRequiredResult`` that carries the question (:func:`question_result`, raised as
  :class:`InputRequired` and returned by the guard). The client shows it and calls the tool again
  with the answer; the call runs again from the start, reaches the same question and finds the
  answer there (:func:`answer_from_retry`). Tools therefore ask before they change anything.

The SDK seals ``request_state`` (AES-GCM with a per-process key) and binds it to the tool name, a
digest of the arguments and a 10-minute expiry, so a client can neither read nor forge it, nor
change the arguments between the question and the answer. AXPilot's own state adds a nonce that
is accepted once (:class:`QuestionBook`: no replays, no answers older than the confirmation
timeout) and a digest of the question (the retry must lead to the very same question).

Only an explicit *accept* with ``allow: true`` approves. Decline, cancel, a missing or malformed
answer and an expired or replayed one are a "no" (``USER_DENIED``). A client that cannot be asked
after all (it answers the request with an error) falls back to the context's confirmer, which is
:class:`~axpilot.policy.confirm.DenyConfirmer` when nobody else can be asked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections.abc import Mapping
from typing import Any, Literal

from mcp.server.elicitation import render_elicitation_schema
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, Field, ValidationError

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import ConfirmRequest

__all__ = [
    "CONFIRM_KEY",
    "INPUT_REQUIRED_VERSION",
    "ConfirmAnswer",
    "InputRequired",
    "QuestionBook",
    "answer_from_retry",
    "ask_during_call",
    "client_can_elicit",
    "message_for",
    "pending_nonce",
    "question_digest",
    "question_result",
    "uses_input_required",
]

_log = logging.getLogger(__name__)

CONFIRM_KEY = "axpilot_confirm"
"""Key of AXPilot's question in ``InputRequiredResult.input_requests`` and of its answer."""
INPUT_REQUIRED_VERSION = "2026-07-28"
"""The first protocol version that elicits through ``InputRequiredResult``."""
_STATE_VERSION = 1
_DETAIL_MAX_CHARS = 1500
_BOOK_LIMIT = 256


class ConfirmAnswer(BaseModel):
    """The form a client shows for a confirmation: one checkbox."""

    allow: bool = Field(default=False, title="Allow", description="Check to let AXPilot do this.")


class InputRequired(Exception):
    """Ends the current round of a tool call with ``result`` (an ``InputRequiredResult``).

    Raised by :meth:`ServerContext.ask_user <axpilot.server.context.ServerContext.ask_user>` and
    turned into the call's result by the guard; tools must let it propagate."""

    def __init__(self, result: InputRequiredResult) -> None:
        super().__init__("the client must answer a question before the call can go on")
        self.result = result


class QuestionBook:
    """Nonces of the questions asked through ``InputRequiredResult``: each answer counts once,
    and only within ``ttl_s`` of the question. Thread-safe; at most 256 open questions are kept
    (the oldest is forgotten, so its answer is refused)."""

    def __init__(self, limit: int = _BOOK_LIMIT) -> None:
        self._limit = limit
        self._open: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, now: float, ttl_s: float) -> str:
        """A new nonce, valid until ``now + ttl_s``."""
        nonce = secrets.token_urlsafe(12)
        with self._lock:
            for key in [k for k, deadline in self._open.items() if deadline <= now]:
                del self._open[key]
            while len(self._open) >= self._limit:
                del self._open[next(iter(self._open))]
            self._open[nonce] = now + max(ttl_s, 0.0)
        return nonce

    def consume(self, nonce: str, now: float) -> Literal["ok", "expired", "unknown"]:
        """Use ``nonce`` up: ``ok`` once, ``expired`` after its deadline, ``unknown`` if it was
        never issued here or was already used."""
        with self._lock:
            deadline = self._open.pop(nonce, None)
        if deadline is None:
            return "unknown"
        return "ok" if deadline > now else "expired"


# --------------------------------------------------------------------------- the client
def client_can_elicit(mcp_context: Any) -> bool:
    """True if the client of ``mcp_context`` declared form elicitation (a bare
    ``elicitation: {}`` counts, as in the SDK; URL-only does not)."""
    try:
        capabilities = mcp_context.client_capabilities
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return False
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None:
        return False
    return getattr(elicitation, "form", None) is not None or getattr(elicitation, "url", None) is None


def uses_input_required(mcp_context: Any) -> bool:
    """True when the negotiated protocol elicits through ``InputRequiredResult``."""
    try:
        version = mcp_context.protocol_version
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return False
    return isinstance(version, str) and is_version_at_least(version, INPUT_REQUIRED_VERSION)


# --------------------------------------------------------------------------- the question
def message_for(request: ConfirmRequest) -> str:
    """The text the client shows: the question, the exact command or script, and how to answer."""
    parts = [request.summary]
    if request.detail:
        detail = request.detail
        if len(detail) > _DETAIL_MAX_CHARS:
            hidden = len(detail) - (_DETAIL_MAX_CHARS - 1)
            detail = f"{detail[: _DETAIL_MAX_CHARS - 1]}… ({hidden} more characters not shown)"
        parts.append(detail)
    parts.append(
        'AXPilot asks before doing this. Check "Allow" and accept to go ahead; anything else declines.'
    )
    return "\n\n".join(parts)


def question_digest(request: ConfirmRequest) -> str:
    """A digest of what the user is asked (tool, question and detail)."""
    payload = json.dumps([request.tool, request.summary, request.detail], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def question_result(request: ConfirmRequest, nonce: str) -> InputRequiredResult:
    """The ``InputRequiredResult`` that asks ``request`` (protocol 2026-07-28 and newer)."""
    question = ElicitRequest(
        params=ElicitRequestFormParams(
            message=message_for(request), requested_schema=render_elicitation_schema(ConfirmAnswer)
        )
    )
    state = {"axpilot": _STATE_VERSION, "nonce": nonce, "question": question_digest(request)}
    return InputRequiredResult(
        input_requests={CONFIRM_KEY: question}, request_state=json.dumps(state, separators=(",", ":"))
    )


def _our_state(mcp_context: Any) -> Mapping[str, Any] | None:
    try:
        raw = mcp_context.request_state
    except (AttributeError, LookupError, RuntimeError, ValueError):
        return None
    if not isinstance(raw, str):
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(state, dict) or state.get("axpilot") != _STATE_VERSION:
        return None
    if not isinstance(state.get("nonce"), str) or not isinstance(state.get("question"), str):
        return None
    return state


def pending_nonce(mcp_context: Any) -> str | None:
    """The nonce of AXPilot's question that this call answers, if it is such a retry."""
    state = _our_state(mcp_context) if mcp_context is not None else None
    return state["nonce"] if state is not None else None


def _denied(message: str, request: ConfirmRequest, hint: str, **details: Any) -> AXPilotError:
    return AXPilotError(
        ErrorCode.USER_DENIED,
        f"{message}: {request.summary}",
        hint=hint,
        details={"tool": request.tool, "via": "elicitation", **details},
    )


def _approved(answer: Any) -> bool:
    if not isinstance(answer, ElicitResult) or answer.action != "accept":
        return False
    try:
        return ConfirmAnswer.model_validate(answer.content or {}).allow
    except ValidationError:
        return False


def answer_from_retry(
    mcp_context: Any, request: ConfirmRequest, book: QuestionBook, now: float
) -> bool | None:
    """The user's answer to ``request`` carried by this retry.

    Returns ``None`` when the call carries no answer from AXPilot's question (the first round),
    ``True`` when the user approved. Raises ``USER_DENIED`` for every other case: declined or
    cancelled, no answer, an expired or already used answer, or an answer to a different
    question (the situation changed between the two rounds)."""
    state = _our_state(mcp_context)
    if state is None:
        return None
    status = book.consume(state["nonce"], now)
    if status == "expired":
        raise _denied(
            "The approval came too late",
            request,
            "Ask the user again in the chat before retrying.",
            expired=True,
        )
    if status == "unknown":
        raise _denied(
            "This approval was already used or is not known to this server",
            request,
            "Each approval runs one call. Call the tool again to ask the user once more.",
        )
    if state["question"] != question_digest(request):
        raise _denied(
            "The question changed between asking and answering",
            request,
            "The screen or the request changed. Call the tool again to ask the user about it.",
        )
    try:
        responses = mcp_context.input_responses or {}
    except (AttributeError, LookupError, RuntimeError, ValueError):
        responses = {}
    if _approved(responses.get(CONFIRM_KEY)):
        return True
    raise _denied(
        "The user did not approve",
        request,
        "The user declined in the client. Do not retry the same action; ask them what to do instead.",
    )


async def ask_during_call(mcp_context: Any, request: ConfirmRequest) -> bool | None:
    """Ask with a standalone ``elicitation/create`` request (protocol 2025-11-25 and older).

    Returns the answer (``True`` only for accept with ``allow: true``), or ``None`` when the
    client could not be asked after all (it answered the request with an error), so the caller
    can fall back to its confirmer."""
    try:
        result = await mcp_context.elicit(message_for(request), ConfirmAnswer)
    except ValueError:  # accepted, but with content that does not match the form
        return False
    except Exception as exc:  # the client failed the request: fall back to the confirmer (A8: logged)
        _log.warning(
            "The client could not show the confirmation (%s); asking another way.", type(exc).__name__
        )
        return None
    if result.action != "accept":
        return False
    return bool(getattr(result.data, "allow", False))
