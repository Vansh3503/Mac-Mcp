"""Asking the user before risky actions.

The server builds a :class:`ConfirmRequest` when :meth:`Policy.requires_confirmation
<axpilot.policy.rules.Policy.requires_confirmation>` says so, and awaits a
:class:`Confirmer`. The host app supplies a native dialog; :class:`AutoConfirmer` serves
tests and ``--yes``; :class:`DenyConfirmer` is the headless default when nobody can be asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["AutoConfirmer", "ConfirmRequest", "Confirmer", "DenyConfirmer", "ensure_confirmed"]


@dataclass(slots=True)
class ConfirmRequest:
    """What the user is asked to approve."""

    tool: str
    summary: str
    """One line shown as the question, e.g. ``"Run `rm -rf build` in a shell?"``."""
    detail: str | None = None
    """Longer text (the full command or script)."""
    client: str | None = None
    """The client asking, e.g. ``"Claude"``."""


@runtime_checkable
class Confirmer(Protocol):
    """Anything that can ask the user to approve a :class:`ConfirmRequest`."""

    async def confirm(self, request: ConfirmRequest) -> bool:
        """Return True if the user approved. Must not raise for a plain "no"."""
        ...


class AutoConfirmer:
    """Answers every request with ``allow`` without asking. Used by tests and ``--yes``.

    Every request is kept in :attr:`requests` so tests can assert on what was asked.
    """

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self.allow


class DenyConfirmer:
    """Declines everything: the safe default when no confirmation UI is available."""

    denial_hint = (
        "AXPilot could not ask the user (no confirmation UI is available here). The user can "
        'approve actions in advance with `--yes`, or set [safety] confirm = "never".'
    )

    async def confirm(self, request: ConfirmRequest) -> bool:
        return False


async def ensure_confirmed(confirmer: Confirmer, request: ConfirmRequest) -> None:
    """Ask ``confirmer`` and raise ``USER_DENIED`` unless the user approves."""
    if await confirmer.confirm(request):
        return
    hint = getattr(confirmer, "denial_hint", None) or (
        "The user declined. Do not retry the same action; ask them what to do instead."
    )
    raise AXPilotError(
        ErrorCode.USER_DENIED,
        f"The user did not approve: {request.summary}",
        hint=hint,
        details={"tool": request.tool},
    )
