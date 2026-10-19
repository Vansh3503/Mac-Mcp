"""Apple Shortcuts through the ``shortcuts`` command-line tool (macOS 12+).

* ``shortcuts list`` prints one shortcut name per line.
* ``shortcuts run <name> [--input-path <file>]`` runs one. The CLI takes input as a *file*, so
  ``input_text`` is written to a private temporary file (mode ``0600``) that is always deleted
  afterwards. The shortcut's output, if any, is written to stdout.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from collections.abc import Iterator

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import (
    DEFAULT_MAX_OUTPUT,
    EXIT_NOT_FOUND,
    CommandResult,
    CommandRunner,
    ensure_success,
    excerpt,
    output_records,
)

__all__ = ["MAX_NAME_CHARS", "MAX_TIMEOUT_S", "list_shortcuts", "run_shortcut", "shortcut_run_argv"]

_log = logging.getLogger(__name__)

MAX_NAME_CHARS = 255
MAX_TIMEOUT_S = 600.0
LIST_TIMEOUT_S = 30.0
_LIST_MAX_OUTPUT = 500_000
_NOT_FOUND_RE = re.compile(
    r"(?:couldn[’']t|could not|can[’']t|cannot|unable to) find (?:a |the )?shortcut"
    r"|no shortcut (?:named|with|called)",
    re.IGNORECASE,
)
_NOT_AVAILABLE_HINT = "The `shortcuts` command needs macOS 12 (Monterey) or later."


def _invalid(message: str, hint: str | None = None) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise _invalid(
            "The shortcut name is empty.", hint="Call `shortcuts` with action `list` to see names."
        )
    if len(name) > MAX_NAME_CHARS:
        raise _invalid(f"The shortcut name is too long (max {MAX_NAME_CHARS} characters).")
    if any(ch in name for ch in "\x00\n\r"):
        raise _invalid("The shortcut name cannot contain NUL or line-break characters.")
    if name.startswith("-"):
        raise _invalid(
            "Shortcut names starting with '-' are not supported (the CLI would read them as options).",
            hint="Rename the shortcut in the Shortcuts app.",
        )
    return name


def _validate_timeout(timeout_s: float) -> None:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
        raise _invalid("timeout_s must be a number of seconds.")
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise _invalid(f"timeout_s must be between 0 and {MAX_TIMEOUT_S:g} seconds, got {timeout_s!r}.")


def shortcut_run_argv(name: str, input_path: str | None = None) -> list[str]:
    """``shortcuts run <name> [--input-path <input_path>]``."""
    argv = ["shortcuts", "run", _validate_name(name)]
    if input_path is not None:
        argv += ["--input-path", input_path]
    return argv


def list_shortcuts(runner: CommandRunner, *, timeout_s: float = LIST_TIMEOUT_S) -> list[str]:
    """Names of the user's shortcuts, in the order the CLI prints them."""
    _validate_timeout(timeout_s)
    result = runner.run(["shortcuts", "list"], timeout_s=timeout_s, max_output=_LIST_MAX_OUTPUT)
    ensure_success(
        result,
        what="Listing shortcuts",
        timeout_s=timeout_s,
        hint="Retry; the Shortcuts service may be starting up.",
        missing_hint=_NOT_AVAILABLE_HINT,
    )
    return [line.strip() for line in output_records(result.stdout) if line.strip()]


@contextlib.contextmanager
def _input_file(text: str) -> Iterator[str]:
    """A private temporary file holding ``text`` (UTF-8), removed when the block exits."""
    fd, path = tempfile.mkstemp(prefix="axpilot-shortcut-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            _log.debug("shortcut input file %s was already removed", path)
        except OSError as exc:
            _log.warning("could not remove shortcut input file %s: %s", path, exc)


def run_shortcut(
    runner: CommandRunner,
    name: str,
    input_text: str | None = None,
    timeout_s: float = 60,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> CommandResult:
    """Run the shortcut ``name`` (optionally with text input) and return the successful result.

    The shortcut's output is ``result.stdout``. Raises ``INVALID_ARGUMENT`` for a bad or unknown
    name, ``TIMEOUT`` if it runs longer than ``timeout_s`` (at most 600 s), ``NOT_SUPPORTED``
    without the ``shortcuts`` CLI and ``COMMAND_FAILED`` if the shortcut fails.
    """
    _validate_name(name)
    _validate_timeout(timeout_s)
    if input_text is not None and not isinstance(input_text, str):
        raise _invalid("input_text must be text.")
    if input_text is None:
        return _run(runner, shortcut_run_argv(name), name, timeout_s, max_output)
    with _input_file(input_text) as path:
        return _run(runner, shortcut_run_argv(name, path), name, timeout_s, max_output)


def _run(
    runner: CommandRunner, argv: list[str], name: str, timeout_s: float, max_output: int
) -> CommandResult:
    result = runner.run(argv, timeout_s=timeout_s, max_output=max_output)
    failed = not result.ok and not result.timed_out and result.exit_code != EXIT_NOT_FOUND
    if failed and (_NOT_FOUND_RE.search(result.stderr) or _NOT_FOUND_RE.search(result.stdout)):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"There is no shortcut named {name!r}.",
            hint="Call `shortcuts` with action `list` to see the exact names.",
            details={"stderr": excerpt(result.stderr)},
        )
    if result.timed_out:
        hint = "The shortcut may be waiting for the user (e.g. an 'Ask for Input' action). Check the screen."
    else:
        hint = "Open the shortcut in the Shortcuts app to see which action failed."
    return ensure_success(
        result,
        what=f"Shortcut {name!r}",
        timeout_s=timeout_s,
        hint=hint,
        missing_hint=_NOT_AVAILABLE_HINT,
    )
