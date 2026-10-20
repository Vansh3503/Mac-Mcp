"""Unit tests of :mod:`axpilot.server.elicit` with stand-ins for the SDK's request context."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from mcp_types import (
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    UrlElicitationCapability,
)

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server import elicit
from axpilot.server.elicit import CONFIRM_KEY, QuestionBook

REQUEST = ConfirmRequest(tool="shell", summary="Run `make` in a shell?", detail="make release")


class Raising:
    """A request context whose every property fails, as outside of a request."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("no request")


def context(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------- capabilities
@pytest.mark.parametrize(
    ("elicitation", "expected"),
    [
        (None, False),
        (ElicitationCapability(), True),  # a bare `elicitation: {}` means form support
        (ElicitationCapability(form=FormElicitationCapability()), True),
        (ElicitationCapability(url=UrlElicitationCapability()), False),  # URL-only
        (ElicitationCapability(form=FormElicitationCapability(), url=UrlElicitationCapability()), True),
    ],
)
def test_client_can_elicit(elicitation: Any, expected: bool) -> None:
    caps = SimpleNamespace(elicitation=elicitation)
    assert elicit.client_can_elicit(context(client_capabilities=caps)) is expected


def test_no_capabilities_or_no_request_means_no_elicitation() -> None:
    assert elicit.client_can_elicit(context(client_capabilities=None)) is False
    assert elicit.client_can_elicit(Raising()) is False
    assert elicit.uses_input_required(Raising()) is False
    assert elicit.pending_nonce(Raising()) is None
    assert elicit.pending_nonce(None) is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2026-07-28", True), ("2025-11-25", False), ("2025-06-18", False), (None, False), (20260728, False)],
)
def test_uses_input_required(version: str | None, expected: bool) -> None:
    # The same test as the SDK's own resolvers (`is_version_at_least` knows released versions).
    assert elicit.uses_input_required(context(protocol_version=version)) is expected


# --------------------------------------------------------------------------- the question
def test_message_shows_the_question_the_detail_and_how_to_answer() -> None:
    message = elicit.message_for(REQUEST)
    assert message.startswith("Run `make` in a shell?")
    assert "make release" in message
    assert 'Check "Allow"' in message


def test_a_long_detail_is_shortened_with_a_note() -> None:
    message = elicit.message_for(ConfirmRequest(tool="shell", summary="Run it?", detail="x" * 5000))
    assert "more characters not shown" in message
    assert len(message) < 1700


def test_the_question_result_carries_the_form_and_a_state() -> None:
    result = elicit.question_result(REQUEST, "n1")
    question = (result.input_requests or {})[CONFIRM_KEY]
    assert isinstance(question, ElicitRequest)
    assert isinstance(question.params, ElicitRequestFormParams)
    assert question.params.requested_schema["properties"]["allow"]["type"] == "boolean"
    state = json.loads(result.request_state or "")
    assert state == {"axpilot": 1, "nonce": "n1", "question": elicit.question_digest(REQUEST)}


def test_the_digest_changes_with_the_question() -> None:
    other = ConfirmRequest(tool="shell", summary="Run `make` in a shell?", detail="make clean")
    assert elicit.question_digest(REQUEST) != elicit.question_digest(other)


@pytest.mark.parametrize(
    "raw",
    [None, "", "not json", "[]", json.dumps({"axpilot": 2, "nonce": "n", "question": "q"}),
     json.dumps({"axpilot": 1, "nonce": 3, "question": "q"}), json.dumps({"axpilot": 1, "nonce": "n"})],
)  # fmt: skip
def test_states_that_are_not_ours_are_ignored(raw: str | None) -> None:
    ctx = context(request_state=raw, input_responses=None)
    assert elicit.pending_nonce(ctx) is None
    assert elicit.answer_from_retry(ctx, REQUEST, QuestionBook(), now=0.0) is None


# --------------------------------------------------------------------------- answers
def retry(book: QuestionBook, answer: ElicitResult | None, *, request: ConfirmRequest = REQUEST) -> Any:
    nonce = book.issue(0.0, 60.0)
    state = elicit.question_result(request, nonce).request_state
    responses = {CONFIRM_KEY: answer} if answer is not None else None
    return context(request_state=state, input_responses=responses)


def test_an_approval_is_accepted_once() -> None:
    book = QuestionBook()
    ctx = retry(book, ElicitResult(action="accept", content={"allow": True}))
    assert elicit.answer_from_retry(ctx, REQUEST, book, now=1.0) is True
    with pytest.raises(AXPilotError, match="already used") as info:
        elicit.answer_from_retry(ctx, REQUEST, book, now=1.0)
    assert info.value.code is ErrorCode.USER_DENIED


def test_an_answer_to_a_different_question_is_refused() -> None:
    book = QuestionBook()
    other = ConfirmRequest(tool="shell", summary="Run `make` in a shell?", detail="make clean")
    ctx = retry(book, ElicitResult(action="accept", content={"allow": True}), request=other)
    with pytest.raises(AXPilotError, match="question changed"):
        elicit.answer_from_retry(ctx, REQUEST, book, now=1.0)


def test_an_expired_answer_is_refused() -> None:
    book = QuestionBook()
    ctx = retry(book, ElicitResult(action="accept", content={"allow": True}))
    with pytest.raises(AXPilotError, match="too late") as info:
        elicit.answer_from_retry(ctx, REQUEST, book, now=61.0)
    assert info.value.details["expired"] is True


@pytest.mark.parametrize(
    "answer",
    [None, ElicitResult(action="decline"), ElicitResult(action="cancel"),
     ElicitResult(action="accept", content={"allow": False}), ElicitResult(action="accept"),
     ElicitResult(action="accept", content={"allow": "perhaps"})],
)  # fmt: skip
def test_anything_but_allow_is_refused(answer: ElicitResult | None) -> None:
    book = QuestionBook()
    with pytest.raises(AXPilotError, match="did not approve"):
        elicit.answer_from_retry(retry(book, answer), REQUEST, book, now=1.0)


def test_responses_that_cannot_be_read_are_no_answer() -> None:
    book = QuestionBook()
    state = elicit.question_result(REQUEST, book.issue(0.0, 60.0)).request_state

    class Partial:
        request_state = state

        @property
        def input_responses(self) -> Any:
            raise RuntimeError("gone")

    with pytest.raises(AXPilotError, match="did not approve"):
        elicit.answer_from_retry(Partial(), REQUEST, book, now=1.0)


# --------------------------------------------------------------------------- the book
def test_the_book_forgets_expired_and_the_oldest_questions() -> None:
    book = QuestionBook(limit=2)
    first = book.issue(0.0, 10.0)
    second = book.issue(0.0, 10.0)
    third = book.issue(0.0, 10.0)  # over the limit: the oldest is forgotten
    assert book.consume(first, 1.0) == "unknown"
    assert book.consume(second, 1.0) == "ok"
    late = book.issue(20.0, 1.0)  # issuing also drops the expired `third`
    assert book.consume(third, 20.0) == "unknown"
    assert book.consume(late, 25.0) == "expired"
    assert book.consume("never-issued", 0.0) == "unknown"


# --------------------------------------------------------------------------- during the call
@pytest.mark.anyio
async def test_ask_during_call_maps_the_outcomes() -> None:
    class Ctx:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome
            self.messages: list[str] = []

        async def elicit(self, message: str, schema: Any) -> Any:
            self.messages.append(message)
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

    accepted = SimpleNamespace(action="accept", data=elicit.ConfirmAnswer(allow=True))
    unchecked = SimpleNamespace(action="accept", data=elicit.ConfirmAnswer(allow=False))
    assert await elicit.ask_during_call(Ctx(accepted), REQUEST) is True
    assert await elicit.ask_during_call(Ctx(unchecked), REQUEST) is False
    assert await elicit.ask_during_call(Ctx(SimpleNamespace(action="decline")), REQUEST) is False
    assert await elicit.ask_during_call(Ctx(ValueError("bad content")), REQUEST) is False
    assert await elicit.ask_during_call(Ctx(RuntimeError("no UI")), REQUEST) is None
