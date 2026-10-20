"""Generic passwords in the macOS login keychain, through ``/usr/bin/security``.

AXPilot keeps its HTTP bearer token here on macOS (:func:`axpilot.host.http.get_token`). The
``security`` command keeps PyObjC (and a ``pyobjc-framework-Security`` dependency) out of this
package and runs through :class:`~axpilot.system.runner.CommandRunner`, so the logic is tested on
Linux with a :class:`~axpilot.system.runner.FakeRunner`.

A secret never appears in a process's arguments, where other processes could see it: it is
written with ``security -i``, which reads its command from standard input, and read back with
``find-generic-password -w``, which prints it on standard output. Every write is verified by
reading the item back, because ``security -i`` may report success for a command that failed.
Error messages and details never contain the secret or the command's output.

Not verified on a real Mac yet: the exit codes and output of ``security`` on macOS 14 to 27, and
whether the login keychain is unlocked in every context the host runs in (started by the
launcher at login, over SSH).
"""

from __future__ import annotations

import re

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandResult, CommandRunner

__all__ = ["ITEM_NOT_FOUND", "SECURITY", "Keychain"]

SECURITY = "/usr/bin/security"
ITEM_NOT_FOUND = 44
"""Exit code of ``security find-generic-password`` when there is no such item (errSecItemNotFound)."""
KEYCHAIN_TIMEOUT_S = 10.0
_SAFE = re.compile(r"[A-Za-z0-9._-]{1,512}")
"""Names and secrets are passed to ``security -i``, which splits its input on spaces and quotes:
only these characters are accepted, so nothing can change the command."""


def _check(value: str, what: str) -> str:
    if not _SAFE.fullmatch(value):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"The keychain {what} may only contain letters, digits, '.', '_' and '-' (at most 512).",
        )
    return value


class Keychain:
    """The login keychain's generic passwords (see the module docstring)."""

    def __init__(self, runner: CommandRunner | None = None, *, timeout_s: float = KEYCHAIN_TIMEOUT_S) -> None:
        self.runner = runner or CommandRunner()
        self.timeout_s = timeout_s

    def read(self, service: str, account: str) -> str | None:
        """The password of the item ``service``/``account``, or ``None`` if there is none.

        Raises ``COMMAND_FAILED`` (or ``TIMEOUT``, ``NOT_SUPPORTED`` without ``security``) when
        the keychain cannot be read, e.g. while it is locked."""
        argv = [
            SECURITY,
            "find-generic-password",
            "-s",
            _check(service, "service"),
            "-a",
            _check(account, "account"),
            "-w",
        ]
        result = self.runner.run(argv, timeout_s=self.timeout_s)
        if result.exit_code == ITEM_NOT_FOUND and not result.timed_out:
            return None
        self._ensure(result, f"Reading {service} from the keychain")
        secret = result.stdout.strip()
        return secret or None

    def write(self, service: str, account: str, secret: str, *, label: str = "AXPilot") -> None:
        """Create or replace the item ``service``/``account`` with ``secret``, then read it back.

        Raises ``INVALID_ARGUMENT`` for characters ``security -i`` could misread, and
        ``COMMAND_FAILED`` when the item cannot be written or does not read back the same."""
        command = (
            f"add-generic-password -U -s {_check(service, 'service')} -a {_check(account, 'account')} "
            f"-l {_check(label, 'label')} -w {_check(secret, 'secret')}\n"
        )
        result = self.runner.run([SECURITY, "-i"], input_text=command, timeout_s=self.timeout_s)
        self._ensure(result, f"Storing {service} in the keychain")
        if self.read(service, account) != secret:
            raise AXPilotError(
                ErrorCode.COMMAND_FAILED,
                f"The keychain did not keep {service}.",
                hint="Unlock the login keychain (Keychain Access) and try again.",
                details={"command": "security"},
            )

    def delete(self, service: str, account: str) -> bool:
        """Remove the item; returns False if there was none."""
        argv = [
            SECURITY,
            "delete-generic-password",
            "-s",
            _check(service, "service"),
            "-a",
            _check(account, "account"),
        ]
        result = self.runner.run(argv, timeout_s=self.timeout_s)
        if result.exit_code == ITEM_NOT_FOUND and not result.timed_out:
            return False
        self._ensure(result, f"Removing {service} from the keychain")
        return True

    def _ensure(self, result: CommandResult, what: str) -> None:
        """Raise for a failed command. Its output is left out: it could contain the secret."""
        if result.ok:
            return
        if result.timed_out:
            raise AXPilotError(
                ErrorCode.TIMEOUT,
                f"{what} timed out after {self.timeout_s:g} s.",
                hint="The keychain may be waiting for a password prompt. Unlock it and try again.",
                details={"command": "security"},
            )
        if result.exit_code == 127:
            raise AXPilotError(
                ErrorCode.NOT_SUPPORTED,
                f"{what} needs /usr/bin/security, which is not available here.",
                hint="The keychain is macOS-only; AXPilot keeps the token in a private file elsewhere.",
                details={"command": "security"},
            )
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"{what} failed (exit {result.exit_code}).",
            hint="Unlock the login keychain (Keychain Access) and try again.",
            details={"command": "security", "exit_code": result.exit_code},
        )
