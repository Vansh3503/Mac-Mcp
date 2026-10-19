"""AppleScript and JavaScript for Automation (JXA) through ``osascript``.

Scripts are passed on **stdin** (``osascript -l AppleScript``), never as ``-e`` arguments, so
their contents do not show up in process listings and are not limited by ``ARG_MAX``.

Failures are mapped to stable error codes:

========================================================  =============================
osascript reports                                          code
========================================================  =============================
``Not authorized to send Apple events`` (-1743, -1744)    ``PERMISSION_AUTOMATION``
``… is not allowed assistive access`` (-25211, -1719)     ``PERMISSION_ACCESSIBILITY``
``User canceled.`` (-128)                                  ``USER_DENIED``
``Application isn't running.`` (-600)                      ``APP_NOT_FOUND``
``Can't get application "X".`` / ``-10814``                ``APP_NOT_FOUND``
anything else (non-zero exit)                              ``COMMAND_FAILED``
the deadline passed                                        ``TIMEOUT``
``osascript`` is missing (not macOS)                       ``NOT_SUPPORTED``
========================================================  =============================

Every mapped error carries ``details["error_number"]`` (when osascript printed one) and a stderr
excerpt, so callers such as :mod:`axpilot.system.browser` can refine the message further.

Untrusted text must be embedded with :func:`applescript_string` (AppleScript) or
:func:`jxa_string` (JavaScript); never build a literal by hand.
"""

from __future__ import annotations

import json
import re
from typing import Literal, cast

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import (
    DEFAULT_MAX_OUTPUT,
    EXIT_NOT_FOUND,
    CommandResult,
    CommandRunner,
    excerpt,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ScriptLanguage",
    "applescript_string",
    "jxa_string",
    "osascript_argv",
    "output_text",
    "parse_osascript_error",
    "run_applescript",
]

ScriptLanguage = Literal["applescript", "javascript"]

DEFAULT_TIMEOUT_S = 30.0

_LANGUAGE_FLAGS: dict[str, str] = {"applescript": "AppleScript", "javascript": "JavaScript"}
_LANGUAGE_NAMES: dict[str, str] = {"applescript": "AppleScript", "javascript": "JavaScript (JXA)"}

# Characters that must be escaped inside an AppleScript string literal. Backslash and double
# quote are the only ones that could end the literal; line breaks and tabs are escaped too so
# the generated source stays on one line and is immune to line-ending conversion.
_APPLESCRIPT_ESCAPES = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"})

_ERROR_RE = re.compile(
    r"^(?:\d+:\d+:\s*)?(?:(?:execution|syntax|script) error:\s*)?(?P<message>.*?)"
    r"\s*(?:\((?P<number>-?\d+)\))?\s*$",
    re.DOTALL,
)
_NOT_AUTHORIZED_RE = re.compile(r"not authori[sz]ed to send apple ?events", re.IGNORECASE)
_AUTHORIZED_TARGET_RE = re.compile(
    r"not authori[sz]ed to send apple ?events to (?P<app>.+?)\.?\s*(?:\(-?\d+\))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ASSISTIVE_RE = re.compile(r"not allowed assistive access", re.IGNORECASE)
_CANT_GET_APP_RE = re.compile(
    r"can[’']t get application (?:id )?\"(?P<app>[^\"]+)\"\.?\s*(?:\(-?\d+\))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_AUTOMATION_NUMBERS = frozenset({-1743, -1744})
_ASSISTIVE_NUMBERS = frozenset({-25211})
_USER_CANCELED = -128
_APP_NOT_RUNNING = -600
_APP_NOT_FOUND_NUMBERS = frozenset({-10814})


def applescript_string(s: str) -> str:
    """Return ``s`` as a double-quoted AppleScript string literal, safe to embed in source.

    Backslashes and double quotes are escaped, so no input can terminate the literal early
    and inject code; ``\\n``, ``\\r`` and ``\\t`` are written as escapes. NUL characters cannot
    be represented in AppleScript source and raise ``INVALID_ARGUMENT``.

    >>> applescript_string('say "hi"')
    '"say \\\\"hi\\\\""'
    """
    if not isinstance(s, str):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"Expected text for AppleScript, got {type(s).__name__}."
        )
    if "\x00" in s:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "AppleScript text cannot contain NUL characters.")
    return '"' + s.translate(_APPLESCRIPT_ESCAPES) + '"'


def jxa_string(s: str) -> str:
    """Return ``s`` as a JavaScript string literal (JSON, ASCII-only), safe to embed in JXA."""
    if not isinstance(s, str):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"Expected text for JavaScript, got {type(s).__name__}."
        )
    return json.dumps(s, ensure_ascii=True)


def _normalize_language(language: str) -> ScriptLanguage:
    key = language.strip().lower() if isinstance(language, str) else ""
    if key in ("jxa", "js"):
        key = "javascript"
    if key not in _LANGUAGE_FLAGS:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unknown script language {language!r}. Use 'applescript' or 'javascript'.",
        )
    return cast(ScriptLanguage, key)


def osascript_argv(language: ScriptLanguage = "applescript") -> list[str]:
    """The argv that runs a script read from stdin in ``language``."""
    return ["osascript", "-l", _LANGUAGE_FLAGS[_normalize_language(language)]]


def output_text(result: CommandResult) -> str:
    """The script's result text: stdout without the newline ``osascript`` appends."""
    return result.stdout.removesuffix("\n")


def parse_osascript_error(stderr: str) -> tuple[int | None, str]:
    """Split an osascript error line into ``(error_number, message)``.

    ``"0:41: execution error: Safari got an error: Can’t get window 5. (-1719)"`` →
    ``(-1719, "Safari got an error: Can’t get window 5.")``. The number is ``None`` if absent.
    """
    text = stderr.strip()
    match = _ERROR_RE.match(text)
    if not match:  # pragma: no cover - the pattern matches any string
        return None, text
    number = match.group("number")
    return (int(number) if number is not None else None), match.group("message").strip()


def run_applescript(
    runner: CommandRunner,
    source: str,
    *,
    language: ScriptLanguage = "applescript",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> CommandResult:
    """Run ``source`` with ``osascript`` and return the successful result.

    Use :func:`output_text` to get the script's return value as text. Raises
    ``PERMISSION_AUTOMATION``, ``PERMISSION_ACCESSIBILITY``, ``USER_DENIED``, ``APP_NOT_FOUND``,
    ``TIMEOUT``, ``NOT_SUPPORTED`` or ``COMMAND_FAILED`` as described in the module docstring,
    and ``INVALID_ARGUMENT`` for an empty script or an unknown language.
    """
    lang = _normalize_language(language)
    if not isinstance(source, str) or not source.strip():
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The script is empty.")
    if "\x00" in source:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Scripts cannot contain NUL characters.")
    result = runner.run(osascript_argv(lang), input_text=source, timeout_s=timeout_s, max_output=max_output)
    if result.ok:
        return result
    raise _failure(result, lang, timeout_s)


def _failure(result: CommandResult, language: ScriptLanguage, timeout_s: float) -> AXPilotError:
    """Translate a failed osascript run into the most specific :class:`AXPilotError`."""
    lang = _LANGUAGE_NAMES[language]
    if result.timed_out:
        return AXPilotError(
            ErrorCode.TIMEOUT,
            f"{lang} did not finish within {timeout_s:g} s and was stopped.",
            hint="The target app may be busy or showing a dialog. Check it, or retry with a longer timeout.",
            details={"command": "osascript"},
        )
    if result.exit_code == EXIT_NOT_FOUND:
        return AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            "`osascript` is not available on this system.",
            hint="AppleScript and JXA need macOS.",
            details={"command": "osascript", "stderr": excerpt(result.stderr)},
        )

    number, message = parse_osascript_error(result.stderr)
    message = message or f"osascript exited with code {result.exit_code}"
    details: dict[str, object] = {"exit_code": result.exit_code, "stderr": excerpt(result.stderr)}
    if number is not None:
        details["error_number"] = number

    if number in _AUTOMATION_NUMBERS or _NOT_AUTHORIZED_RE.search(result.stderr):
        target = _AUTHORIZED_TARGET_RE.search(result.stderr)
        app = target.group("app").strip() if target else None
        if app:
            details["app"] = app
        who = app or "the target app"
        return AXPilotError(
            ErrorCode.PERMISSION_AUTOMATION,
            f"macOS did not allow AXPilot to control {who} (Automation permission).",
            hint=(
                f"In System Settings → Privacy & Security → Automation, allow the app that runs "
                f"AXPilot to control {who}, then retry."
            ),
            details=details,
        )
    if number in _ASSISTIVE_NUMBERS or _ASSISTIVE_RE.search(result.stderr):
        return AXPilotError(
            ErrorCode.PERMISSION_ACCESSIBILITY,
            f"The script needs Accessibility (assistive access): {excerpt(message, 300)}",
            details=details,
        )
    if number == _USER_CANCELED:
        return AXPilotError(
            ErrorCode.USER_DENIED,
            "The user canceled the script (error -128).",
            hint="Ask the user before retrying.",
            details=details,
        )
    if number == _APP_NOT_RUNNING:
        return AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"The target application is not running: {excerpt(message, 300)}",
            hint="Launch the app first (the `app` tool with action `launch`), then retry.",
            details=details,
        )
    missing = _CANT_GET_APP_RE.search(result.stderr)
    if number in _APP_NOT_FOUND_NUMBERS or missing:
        if missing:
            details["app"] = missing.group("app")
        return AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"The application the script addresses was not found: {excerpt(message, 300)}",
            hint="Check the application name or bundle id, and that the app is installed.",
            details=details,
        )
    return AXPilotError(
        ErrorCode.COMMAND_FAILED,
        f"{lang} failed: {excerpt(message, 300)}",
        hint="Check the script and the target app's state. The error text above comes from osascript.",
        details=details,
    )
