"""Confirmations through the MCP client (elicitation) in direct mode, on both protocol flows:
a request during the call (2025-11-25, ``mode="legacy"``) and ``InputRequiredResult`` with a
retry (2026-07-28, the in-process default). Falls back to the confirmer when the client can't be
asked; never elicits with ``--yes``, in host mode, or when ``[safety] elicitation`` is off."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitRequestFormParams, ElicitResult, InputRequiredResult, InputResponses

from axpilot.policy.confirm import AutoConfirmer, DenyConfirmer
from axpilot.server.app import build_server
from axpilot.server.context import ServerContext
from axpilot.server.elicit import CONFIRM_KEY
from axpilot.server.guard import INPUT_REQUIRED
from axpilot.server.results import error_code_of, text_of
from axpilot.system.runner import FakeRunner
from axpilot.testing import make_test_context, structured

pytestmark = pytest.mark.anyio

FORCE_QUIT = {"action": "force_quit", "name": "TextEdit"}
Callback = Callable[[Any, Any], Awaitable[ElicitResult]]


class Asker:
    """An elicitation callback that records the questions and gives a fixed answer."""

    def __init__(self, action: str = "accept", content: dict[str, Any] | None = None) -> None:
        self.action = action
        self.content = {"allow": True} if content is None and action == "accept" else content
        self.messages: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        assert isinstance(params, ElicitRequestFormParams)
        assert params.requested_schema["properties"]["allow"]["type"] == "boolean"
        self.messages.append(params.message)
        return ElicitResult(action=self.action, content=self.content)  # type: ignore[arg-type]


def make_ctx(confirm: Any = None, **overrides: Any) -> ServerContext:
    recorder = confirm if confirm is not None else AutoConfirmer(allow=True)
    return make_test_context({"tools.profile": "standard", **overrides}, confirm=recorder)


async def call(
    ctx: ServerContext, tool: str, args: dict[str, Any], callback: Callback | None, mode: str = "auto"
) -> tuple[Any, str | None]:
    async with Client(build_server(ctx), mode=mode, elicitation_callback=callback) as client:  # type: ignore[arg-type]
        result = await client.call_tool(tool, args)
    return result, error_code_of(result)


def audit_codes(ctx: ServerContext) -> list[str | None]:
    assert ctx.audit.path is not None
    lines = ctx.audit.path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line).get("error_code") for line in lines]


MODES = pytest.mark.parametrize("mode", ["auto", "legacy"])


# --------------------------------------------------------------------------- answers
@MODES
async def test_an_approval_in_the_client_runs_the_action(mode: str) -> None:
    confirmer = AutoConfirmer(allow=False)
    ctx = make_ctx(confirmer)
    asker = Asker()
    result, error = await call(ctx, "app", FORCE_QUIT, asker, mode)
    assert error is None, text_of(result)
    assert structured(result)["action"] == "force_quit"
    (message,) = asker.messages
    assert "Force quit TextEdit?" in message
    assert 'Check "Allow"' in message
    assert confirmer.requests == []  # the client asked; the confirmer was not used
    assert [e["action"] for e in ctx.engine.events_of("app_control")] == ["force_quit"]  # type: ignore[attr-defined]


@MODES
@pytest.mark.parametrize(
    ("action", "content"),
    [
        ("decline", None),
        ("cancel", None),
        ("accept", {"allow": False}),
        ("accept", {"allow": "maybe"}),
        ("accept", {}),
    ],
)
async def test_anything_but_allow_is_a_no(mode: str, action: str, content: dict[str, Any] | None) -> None:
    confirmer = AutoConfirmer(allow=True)
    ctx = make_ctx(confirmer)
    result, error = await call(ctx, "app", FORCE_QUIT, Asker(action, content), mode)
    assert error == "USER_DENIED"
    assert "declined in the client" in text_of(result)
    assert confirmer.requests == []
    assert ctx.engine.events_of("app_control") == []  # type: ignore[attr-defined]


async def test_the_retry_flow_is_audited_as_two_rounds() -> None:
    ctx = make_ctx()
    _, error = await call(ctx, "app", FORCE_QUIT, Asker())
    assert error is None
    assert audit_codes(ctx) == [INPUT_REQUIRED, None]


async def test_a_confirming_tool_runs_its_command_once_after_the_answer() -> None:
    runner = FakeRunner()
    runner.register(["/bin/zsh", "-lc"], stdout="done\n")
    ctx = make_test_context({"tools.profile": "standard", "tools.enable": ["scripting"]}, runner=runner)
    asker = Asker()
    result, error = await call(ctx, "shell", {"command": "echo done"}, asker)
    assert error is None, text_of(result)
    assert "echo done" in asker.messages[0]  # the exact command is shown
    assert len(runner.calls_for(["/bin/zsh"])) == 1  # asked first, ran once


# --------------------------------------------------------------------------- fallbacks
@MODES
async def test_a_client_without_elicitation_uses_the_confirmer(mode: str) -> None:
    confirmer = AutoConfirmer(allow=True)
    ctx = make_ctx(confirmer)
    _, error = await call(ctx, "app", FORCE_QUIT, None, mode)
    assert error is None
    assert len(confirmer.requests) == 1


async def test_a_client_that_fails_the_request_falls_back_to_the_confirmer() -> None:
    async def broken(context: Any, params: Any) -> ElicitResult:
        raise RuntimeError("no UI here")

    confirmer = DenyConfirmer()
    ctx = make_ctx(confirmer)
    result, error = await call(ctx, "app", FORCE_QUIT, broken, "legacy")
    assert error == "USER_DENIED"
    assert "--yes" in text_of(result)  # the DenyConfirmer's hint: nobody could be asked


@pytest.mark.parametrize("setup", ["yes", "config_off", "host"])
async def test_no_elicitation_with_yes_when_disabled_or_in_host_mode(setup: str) -> None:
    confirmer = AutoConfirmer(allow=True)
    if setup == "config_off":
        ctx = make_ctx(confirmer, **{"safety.elicitation": False})
    else:
        ctx = make_ctx(confirmer)
        if setup == "yes":
            ctx.elicitation = False
        else:
            ctx.mode = "host"
    asker = Asker()
    _, error = await call(ctx, "app", FORCE_QUIT, asker)
    assert error is None
    assert asker.messages == []
    assert len(confirmer.requests) == 1


def test_the_cli_turns_elicitation_off_with_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from axpilot import cli

    for name in ("AXPILOT_CONFIG_DIR", "AXPILOT_HOME", "AXPILOT_LOG_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    assert cli._context(cli.Engine.fake, None, yes=True, log=False).elicitation is False
    assert cli._context(cli.Engine.fake, None, yes=False, log=False).elicitation is True


# --------------------------------------------------------------------------- no self-approval
@MODES
async def test_no_tool_may_act_while_the_client_shows_the_question(mode: str) -> None:
    ctx = make_ctx()
    seen: list[str | None] = []

    async with Client(build_server(ctx), mode=mode) as other:  # a parallel call, e.g. a click on "Allow"

        async def answer(context: Any, params: Any) -> ElicitResult:
            result = await other.call_tool("press_keys", {"keys": "return"})
            seen.append(error_code_of(result))
            return ElicitResult(action="accept", content={"allow": True})

        _, error = await call(ctx, "app", FORCE_QUIT, answer, mode)
        assert error is None
        assert seen == ["NEEDS_USER"]
        result = await other.call_tool("press_keys", {"keys": "return"})  # answered: input works again
        assert error_code_of(result) is None, text_of(result)


async def test_a_question_left_unanswered_blocks_input_only_until_the_timeout() -> None:
    ctx = make_ctx()
    now = [1000.0]
    ctx.clock = lambda: now[0]
    ctx.control._clock = lambda: now[0]  # type: ignore[attr-defined]
    async with Client(build_server(ctx), elicitation_callback=Asker()) as client:
        first = await client.session.call_tool("app", FORCE_QUIT, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)  # the client walks away without answering
        result = await client.call_tool("press_keys", {"keys": "return"})
        assert error_code_of(result) == "NEEDS_USER"
        now[0] += ctx.confirm_timeout_s + 1
        result = await client.call_tool("press_keys", {"keys": "return"})
        assert error_code_of(result) is None, text_of(result)


# --------------------------------------------------------------------------- tampering
async def first_round(client: Client) -> InputRequiredResult:
    first = await client.session.call_tool("app", FORCE_QUIT, allow_input_required=True)
    assert isinstance(first, InputRequiredResult)
    assert set(first.input_requests or {}) == {CONFIRM_KEY}
    return first


def approve() -> InputResponses:
    return {CONFIRM_KEY: ElicitResult(action="accept", content={"allow": True})}


async def test_an_answer_is_used_once() -> None:
    runner = FakeRunner()
    runner.register(["/bin/zsh", "-lc"], stdout="ok\n")
    ctx = make_test_context({"tools.profile": "standard", "tools.enable": ["scripting"]}, runner=runner)
    args = {"command": "make release"}
    async with Client(build_server(ctx), elicitation_callback=Asker()) as client:
        first = await client.session.call_tool("shell", args, allow_input_required=True)
        assert isinstance(first, InputRequiredResult)
        done = await client.session.call_tool(
            "shell", args, input_responses=approve(), request_state=first.request_state
        )
        assert error_code_of(done) is None, text_of(done)
        again = await client.session.call_tool(
            "shell", args, input_responses=approve(), request_state=first.request_state
        )
    assert error_code_of(again) == "USER_DENIED"
    assert "already used" in text_of(again)
    assert len(runner.calls_for(["/bin/zsh"])) == 1


async def test_a_late_answer_is_refused() -> None:
    ctx = make_ctx()
    now = [50.0]
    ctx.clock = lambda: now[0]
    async with Client(build_server(ctx), elicitation_callback=Asker()) as client:
        first = await first_round(client)
        now[0] += ctx.confirm_timeout_s + 1
        late = await client.session.call_tool(
            "app", FORCE_QUIT, input_responses=approve(), request_state=first.request_state
        )
    assert error_code_of(late) == "USER_DENIED"
    assert "too late" in text_of(late)


async def test_a_retry_without_the_answer_is_a_no() -> None:
    ctx = make_ctx()
    async with Client(build_server(ctx), elicitation_callback=Asker()) as client:
        first = await first_round(client)
        result = await client.session.call_tool("app", FORCE_QUIT, request_state=first.request_state)
    assert error_code_of(result) == "USER_DENIED"


async def test_an_approval_cannot_be_moved_to_other_arguments() -> None:
    ctx = make_ctx()
    async with Client(build_server(ctx), elicitation_callback=Asker()) as client:
        first = await first_round(client)
        with pytest.raises(MCPError, match="requestState"):
            await client.session.call_tool(
                "app",
                {"action": "force_quit", "name": "Notes"},
                input_responses=approve(),
                request_state=first.request_state,
            )
    assert ctx.engine.events_of("app_control") == []  # type: ignore[attr-defined]


async def test_a_forged_state_is_rejected() -> None:
    ctx = make_ctx()
    forged = json.dumps({"axpilot": 1, "nonce": "x", "question": "y"})
    async with Client(build_server(ctx), elicitation_callback=Asker()) as client:
        with pytest.raises(MCPError, match="requestState"):
            await client.session.call_tool("app", FORCE_QUIT, input_responses=approve(), request_state=forged)
    assert ctx.engine.events_of("app_control") == []  # type: ignore[attr-defined]


async def test_a_question_that_times_out_during_the_call_is_a_no() -> None:
    ctx = make_ctx(AutoConfirmer(allow=True))
    ctx.confirm_timeout_s = 0.05

    async def never(context: Any, params: Any) -> ElicitResult:
        await anyio.sleep(5)
        raise AssertionError("not reached")

    result, error = await call(ctx, "app", FORCE_QUIT, never, "legacy")
    assert error == "USER_DENIED"
    assert "Nobody answered" in text_of(result)
