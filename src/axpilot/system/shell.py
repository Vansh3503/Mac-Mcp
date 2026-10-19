"""Run a shell command line (the opt-in ``shell`` tool).

This is the **only** place AXPilot starts a shell: ``[shell, "-lc", command]``, a login shell so
the user's ``PATH`` (Homebrew, ``~/.local/bin`` …) is available. Everything else runs argv lists.
The command's exit status is returned, not raised: a failing command is a normal result for the
model to read.
"""

from __future__ import annotations

from pathlib import Path

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import DEFAULT_MAX_OUTPUT, CommandResult, CommandRunner

__all__ = ["DEFAULT_SHELL", "MAX_COMMAND_CHARS", "MAX_SHELL_TIMEOUT_S", "run_shell", "shell_argv"]

DEFAULT_SHELL = "/bin/zsh"
MAX_SHELL_TIMEOUT_S = 120.0
"""Upper bound for ``timeout_s`` (the ``shell`` tool advertises at most 120 s)."""
MAX_COMMAND_CHARS = 100_000


def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def shell_argv(command: str, *, shell: str = DEFAULT_SHELL) -> list[str]:
    """Validate ``command`` and ``shell`` and return ``[shell, "-lc", command]``."""
    if not isinstance(command, str) or not command.strip():
        raise _invalid("The shell command is empty.")
    if len(command) > MAX_COMMAND_CHARS:
        raise _invalid(f"The shell command is too long ({len(command)} characters, max {MAX_COMMAND_CHARS}).")
    if "\x00" in command:
        raise _invalid("Shell commands cannot contain NUL characters.")
    if not isinstance(shell, str) or not shell.startswith("/") or "\x00" in shell:
        raise _invalid(
            f"The shell must be an absolute path, got {shell!r}.",
            hint="Set [scripting] shell to a path such as /bin/zsh or /bin/bash.",
        )
    return [shell, "-lc", command]


def run_shell(
    runner: CommandRunner,
    command: str,
    *,
    shell: str = DEFAULT_SHELL,
    cwd: str | None = None,
    timeout_s: float = 30,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> CommandResult:
    """Run ``command`` with ``shell -lc`` and return the result, whatever the exit status.

    ``cwd`` may start with ``~`` and must be an existing directory. ``timeout_s`` must be in
    ``(0, 120]``; on timeout the whole process group is killed and the partial output is
    returned with ``timed_out=True``. Stdin is ``/dev/null``, so commands that wait for input
    see end-of-file instead of hanging. Raises ``INVALID_ARGUMENT`` for bad arguments.
    """
    argv = shell_argv(command, shell=shell)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
        raise _invalid("timeout_s must be a number of seconds.")
    if not 0 < timeout_s <= MAX_SHELL_TIMEOUT_S:
        raise _invalid(f"timeout_s must be between 0 and {MAX_SHELL_TIMEOUT_S:g} seconds, got {timeout_s!r}.")
    workdir: str | None = None
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            raise _invalid("cwd must be a directory path.")
        path = Path(cwd).expanduser()
        if not path.is_dir():
            raise _invalid(f"The working directory does not exist: {path}")
        workdir = str(path)
    return runner.run(argv, timeout_s=timeout_s, cwd=workdir, max_output=max_output)
