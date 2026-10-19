"""The macOS dialog confirmer: escaping, answers, and failures that count as "no"."""

from __future__ import annotations

import pytest

from axpilot.host.dialogs import DialogConfirmer, dialog_script
from axpilot.policy.confirm import ConfirmRequest
from axpilot.system.runner import FakeRunner

pytestmark = pytest.mark.anyio

REQUEST = ConfirmRequest(tool="app", summary="Force quit Notes?", detail="pid 812", client="Claude")


def test_the_script_embeds_the_request_as_string_literals() -> None:
    evil = ConfirmRequest(
        tool="shell", summary='Run: rm -rf ~ " & do shell script "say pwned', detail="line\nbreak \\ back"
    )
    script = dialog_script(evil, 120)
    assert script.startswith('display dialog "')
    assert '\\" & do shell script \\"say pwned' in script  # quotes stay escaped inside the literal
    assert "giving up after 120" in script
    assert 'default button "Deny" cancel button "Deny"' in script


def test_long_requests_are_shortened() -> None:
    script = dialog_script(ConfirmRequest(tool="x", summary="a" * 5000), 30)
    assert len(script) < 1500
    assert "…" in script


@pytest.mark.parametrize(
    ("registration", "expected"),
    [
        ({"stdout": "button returned:Allow, gave up:false\n"}, True),
        ({"stdout": "button returned:Allow, gave up:true\n"}, False),
        ({"exit_code": 1, "stderr": "execution error: User canceled. (-128)"}, False),
        ({"exit_code": 127}, False),
        ({"timed_out": True}, False),
    ],
)
async def test_answers(
    registration: dict[str, object], expected: bool, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner()
    runner.register(["osascript"], **registration)  # type: ignore[arg-type]
    confirmer = DialogConfirmer(runner, timeout_s=30)
    assert await confirmer.confirm(REQUEST) is expected
    call = runner.calls[-1]
    assert call.argv == ["osascript", "-"]
    assert "Force quit Notes?" in (call.input_text or "")
    assert "Asked by: Claude" in (call.input_text or "")
    if registration.get("exit_code") == 127:
        assert "dialog failed" in caplog.text


def test_the_timeout_never_exceeds_the_limit() -> None:
    assert DialogConfirmer(FakeRunner(), timeout_s=10_000).timeout_s == 120
