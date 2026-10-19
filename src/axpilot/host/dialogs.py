"""Asking the user on the Mac with a standard dialog (``osascript`` ``display dialog``).

:class:`DialogConfirmer` is the confirmer of the host and of in-process servers on macOS until
the native menu-bar UI exists: it shows the question in a dialog with *Deny* (the default and
the cancel button) and *Allow*, and gives up after the confirmation timeout. Anything but an
explicit *Allow* (a click on *Deny*, Escape, the timeout, or a failure to show the dialog) is a
"no". Request text is escaped into AppleScript string literals, so it cannot change the script.
"""

from __future__ import annotations

import logging

import anyio

from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import CONFIRM_TIMEOUT_MAX_S
from axpilot.system.osascript import applescript_string
from axpilot.system.runner import CommandRunner

__all__ = ["DialogConfirmer", "dialog_script"]

_log = logging.getLogger(__name__)

ALLOW = "Allow"
DENY = "Deny"
_TEXT_MAX = 900


def dialog_script(request: ConfirmRequest, timeout_s: float) -> str:
    """The AppleScript that asks ``request`` (its strings escaped as literals)."""
    lines = [request.summary]
    if request.client:
        lines.append(f"Asked by: {request.client}")
    if request.detail:
        lines.append(request.detail)
    text = "\n\n".join(lines)
    if len(text) > _TEXT_MAX:
        text = text[: _TEXT_MAX - 1] + "…"
    return (
        f"display dialog {applescript_string(text)} with title {applescript_string('AXPilot')} "
        f"buttons {{{applescript_string(DENY)}, {applescript_string(ALLOW)}}} "
        f"default button {applescript_string(DENY)} cancel button {applescript_string(DENY)} "
        f"with icon caution giving up after {max(1, int(timeout_s))}"
    )


class DialogConfirmer:
    """Asks with a macOS dialog (see the module docstring)."""

    denial_hint = "The user declined in AXPilot's dialog (or did not answer). Ask them what to do instead."

    def __init__(
        self, runner: CommandRunner | None = None, *, timeout_s: float = CONFIRM_TIMEOUT_MAX_S
    ) -> None:
        self.runner = runner or CommandRunner()
        self.timeout_s = min(timeout_s, CONFIRM_TIMEOUT_MAX_S)
        self._lock = anyio.Lock()

    async def confirm(self, request: ConfirmRequest) -> bool:
        script = dialog_script(request, self.timeout_s)

        def ask() -> bool:
            result = self.runner.run(["osascript", "-"], input_text=script, timeout_s=self.timeout_s + 5)
            if not result.ok:
                if result.exit_code != 1:  # 1 = the user pressed Deny/Escape (error -128)
                    _log.warning(
                        "The confirmation dialog failed (exit %s): %s", result.exit_code, result.stderr[:300]
                    )
                return False
            return f"button returned:{ALLOW}" in result.stdout and "gave up:true" not in result.stdout

        async with self._lock:  # one dialog at a time
            return await anyio.to_thread.run_sync(ask, abandon_on_cancel=True)
