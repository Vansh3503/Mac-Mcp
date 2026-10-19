from __future__ import annotations

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import (
    AutoConfirmer,
    Confirmer,
    ConfirmRequest,
    DenyConfirmer,
    ensure_confirmed,
)

REQUEST = ConfirmRequest(tool="shell", summary="Run `ls` in a shell?", detail="ls -la", client="Claude")


def test_request_defaults() -> None:
    request = ConfirmRequest(tool="app", summary="Force quit Notes?")
    assert request.detail is None
    assert request.client is None


def test_confirmers_satisfy_the_protocol() -> None:
    assert isinstance(AutoConfirmer(), Confirmer)
    assert isinstance(DenyConfirmer(), Confirmer)


@pytest.mark.anyio
async def test_auto_confirmer_allows_and_records() -> None:
    confirmer = AutoConfirmer()
    assert await confirmer.confirm(REQUEST) is True
    assert confirmer.requests == [REQUEST]


@pytest.mark.anyio
async def test_auto_confirmer_can_deny() -> None:
    confirmer = AutoConfirmer(allow=False)
    assert await confirmer.confirm(REQUEST) is False
    assert confirmer.requests == [REQUEST]


@pytest.mark.anyio
async def test_deny_confirmer_always_declines() -> None:
    assert await DenyConfirmer().confirm(REQUEST) is False


@pytest.mark.anyio
async def test_ensure_confirmed_passes_when_approved() -> None:
    await ensure_confirmed(AutoConfirmer(), REQUEST)


@pytest.mark.anyio
async def test_ensure_confirmed_raises_user_denied() -> None:
    with pytest.raises(AXPilotError) as info:
        await ensure_confirmed(AutoConfirmer(allow=False), REQUEST)
    assert info.value.code is ErrorCode.USER_DENIED
    assert "Run `ls` in a shell?" in info.value.message
    assert "declined" in (info.value.hint or "")
    assert info.value.details == {"tool": "shell"}


@pytest.mark.anyio
async def test_headless_denial_explains_how_to_approve() -> None:
    with pytest.raises(AXPilotError) as info:
        await ensure_confirmed(DenyConfirmer(), REQUEST)
    assert info.value.code is ErrorCode.USER_DENIED
    assert "--yes" in (info.value.hint or "")


@pytest.mark.anyio
async def test_custom_confirmer() -> None:
    class Recording:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def confirm(self, request: ConfirmRequest) -> bool:
            self.seen.append(request.tool)
            return request.tool != "shell"

    confirmer = Recording()
    assert isinstance(confirmer, Confirmer)
    await ensure_confirmed(confirmer, ConfirmRequest(tool="app", summary="Quit?"))
    with pytest.raises(AXPilotError):
        await ensure_confirmed(confirmer, REQUEST)
    assert confirmer.seen == ["app", "shell"]
