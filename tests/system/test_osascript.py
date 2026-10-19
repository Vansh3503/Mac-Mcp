from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.osascript import (
    applescript_string,
    jxa_string,
    osascript_argv,
    output_text,
    parse_osascript_error,
    run_applescript,
)
from axpilot.system.runner import FakeRunner

Lexer = Callable[[str], Any]

no_nul_text = st.text(alphabet=st.characters(exclude_characters="\x00"))

INJECTIONS = [
    '" & (do shell script "rm -rf ~") & "',
    '"\ndo shell script "id"\n--',
    '\\" & (do shell script "id") & \\"',
    "\\",
    '\\\\"',
    '"" -- comment',
    '(* block *) " end tell',
    "“curly” «chevron» ¬",
    "\r\n\t",
]


# ---------------------------------------------------------------------------------------------
# applescript_string: escaping is security-critical


def test_applescript_string_basics() -> None:
    assert applescript_string("") == '""'
    assert applescript_string("Safari") == '"Safari"'
    assert applescript_string('say "hi"') == '"say \\"hi\\""'
    assert applescript_string("a\\b") == '"a\\\\b"'
    assert applescript_string("l1\nl2\r\tx") == '"l1\\nl2\\r\\tx"'
    assert applescript_string("ünïcødé ✓") == '"ünïcødé ✓"'


def test_applescript_string_rejects_nul() -> None:
    with pytest.raises(AXPilotError) as info:
        applescript_string("a\x00b")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_applescript_string_rejects_non_text() -> None:
    with pytest.raises(AXPilotError):
        applescript_string(42)  # type: ignore[arg-type]


@given(text=no_nul_text)
def test_literal_round_trips_and_is_one_token(as_lexer: Lexer, text: str) -> None:
    literal = applescript_string(text)
    lexed = as_lexer(literal)
    assert lexed.code == "<S>"
    assert lexed.literals == [text]
    assert "\n" not in literal
    assert "\r" not in literal


@given(text=no_nul_text)
def test_untrusted_text_cannot_escape_a_template(as_lexer: Lexer, text: str) -> None:
    template = 'tell application "Safari" to open location {} -- done'
    source = template.format(applescript_string(text))
    lexed = as_lexer(source)
    assert lexed.code == "tell application <S> to open location <S> -- done"
    assert lexed.literals == ["Safari", text]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_known_injection_payloads_stay_inside_the_literal(as_lexer: Lexer, payload: str) -> None:
    source = f"set x to {applescript_string(payload)}\nreturn x"
    lexed = as_lexer(source)
    assert lexed.code == "set x to <S>\nreturn x"
    assert lexed.literals == [payload]


@given(no_nul_text)
def test_jxa_string_is_a_valid_ascii_js_literal(text: str) -> None:
    literal = jxa_string(text)
    assert literal.isascii()
    assert json.loads(literal) == text
    assert "\n" not in literal
    assert " " not in literal


# ---------------------------------------------------------------------------------------------
# run_applescript: argv, stdin and success


def test_runs_applescript_via_stdin(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="42\n")
    result = run_applescript(runner, "return 6 * 7", timeout_s=5)
    call = runner.last_call
    assert call.argv == ["osascript", "-l", "AppleScript"]
    assert call.input_text == "return 6 * 7"
    assert call.timeout_s == 5
    assert output_text(result) == "42"


def test_runs_javascript(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="ok\n")
    run_applescript(runner, "Application('Finder').name()", language="javascript")
    assert runner.last_call.argv == ["osascript", "-l", "JavaScript"]
    assert osascript_argv("javascript") == ["osascript", "-l", "JavaScript"]


def test_script_never_appears_in_argv(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="")
    run_applescript(runner, 'display dialog "secret token 123"')
    assert all("secret" not in arg for arg in runner.last_call.argv)


def test_output_text_strips_only_the_final_newline(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout="  a\n\n")
    assert output_text(run_applescript(runner, "x")) == "  a\n"


@pytest.mark.parametrize("source", ["", "   \n", "a\x00b"])
def test_rejects_empty_or_nul_scripts(runner: FakeRunner, source: str) -> None:
    with pytest.raises(AXPilotError) as info:
        run_applescript(runner, source)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert runner.calls == []


def test_rejects_unknown_language(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError) as info:
        run_applescript(runner, "x", language="python")  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------------------------
# Error mapping


def fail_with(runner: FakeRunner, stderr: str, exit_code: int = 1) -> AXPilotError:
    runner.register(["osascript"], stderr=stderr, exit_code=exit_code)
    with pytest.raises(AXPilotError) as info:
        run_applescript(runner, 'tell application id "com.apple.Safari" to get name')
    return info.value


@pytest.mark.parametrize(
    ("stderr", "app"),
    [
        (
            "0:41: execution error: Not authorized to send Apple events to System Events. (-1743)\n",
            "System Events",
        ),
        ("execution error: Not authorised to send Apple events to Safari. (-1743)", "Safari"),
        ("execution error: Error: Error: Not authorized to send Apple events to Finder. (-1743)", "Finder"),
        (
            "12:30: execution error: Not authorized to send Apple events to Google Chrome. (-1743)",
            "Google Chrome",
        ),
    ],
)
def test_maps_not_authorized_to_permission_automation(runner: FakeRunner, stderr: str, app: str) -> None:
    err = fail_with(runner, stderr)
    assert err.code is ErrorCode.PERMISSION_AUTOMATION
    assert app in err.message
    assert app in (err.hint or "")
    assert err.details["app"] == app
    assert err.details["error_number"] == -1743


def test_maps_bare_1743_without_app_name(runner: FakeRunner) -> None:
    err = fail_with(runner, "execution error: Some unexpected wording. (-1743)")
    assert err.code is ErrorCode.PERMISSION_AUTOMATION
    assert "the target app" in err.message
    assert "app" not in err.details


@pytest.mark.parametrize(
    "stderr",
    [
        "execution error: osascript is not allowed assistive access. (-25211)",
        "execution error: System Events got an error: osascript is not allowed assistive access. (-1719)",
    ],
)
def test_maps_assistive_access_to_permission_accessibility(runner: FakeRunner, stderr: str) -> None:
    assert fail_with(runner, stderr).code is ErrorCode.PERMISSION_ACCESSIBILITY


def test_maps_user_canceled(runner: FakeRunner) -> None:
    err = fail_with(runner, "0:5: execution error: User canceled. (-128)")
    assert err.code is ErrorCode.USER_DENIED


def test_maps_application_not_running(runner: FakeRunner) -> None:
    err = fail_with(runner, "execution error: Safari got an error: Application isn’t running. (-600)")
    assert err.code is ErrorCode.APP_NOT_FOUND
    assert err.details["error_number"] == -600


def test_maps_missing_application(runner: FakeRunner) -> None:
    err = fail_with(runner, '0:30: syntax error: Can’t get application id "com.example.nope". (-2700)')
    assert err.code is ErrorCode.APP_NOT_FOUND
    assert err.details["app"] == "com.example.nope"


def test_other_errors_are_command_failed_with_details(runner: FakeRunner) -> None:
    err = fail_with(runner, "143:150: execution error: Safari got an error: Can’t get window 5. (-1719)")
    assert err.code is ErrorCode.COMMAND_FAILED
    assert "Can’t get window 5." in err.message
    assert err.details["error_number"] == -1719
    assert err.details["exit_code"] == 1
    assert "stderr" in err.details


def test_syntax_errors_are_command_failed(runner: FakeRunner) -> None:
    err = fail_with(runner, "0:4: syntax error: Expected end of line but found identifier. (-2741)")
    assert err.code is ErrorCode.COMMAND_FAILED
    assert err.message.startswith("AppleScript failed: Expected end of line")


def test_error_without_stderr(runner: FakeRunner) -> None:
    err = fail_with(runner, "", exit_code=3)
    assert err.code is ErrorCode.COMMAND_FAILED
    assert "exited with code 3" in err.message


def test_long_stderr_is_truncated_in_errors(runner: FakeRunner) -> None:
    err = fail_with(runner, "execution error: " + "x" * 5000 + " (-1)")
    assert len(err.message) < 400
    assert len(err.details["stderr"]) <= 500


def test_timeout_maps_to_timeout(runner: FakeRunner) -> None:
    runner.register(["osascript"], timed_out=True)
    with pytest.raises(AXPilotError) as info:
        run_applescript(runner, "delay 100", timeout_s=2)
    assert info.value.code is ErrorCode.TIMEOUT
    assert "2 s" in info.value.message


def test_missing_osascript_is_not_supported(runner: FakeRunner) -> None:
    # Nothing registered: FakeRunner behaves like a missing program (exit 127), as on Linux.
    with pytest.raises(AXPilotError) as info:
        run_applescript(runner, "return 1")
    assert info.value.code is ErrorCode.NOT_SUPPORTED


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("0:41: execution error: Boom. (-1743)\n", (-1743, "Boom.")),
        (
            "execution error: Error: ReferenceError: Can't find variable: foo (-2700)",
            (-2700, "Error: ReferenceError: Can't find variable: foo"),
        ),
        ("weird output", (None, "weird output")),
        ("", (None, "")),
    ],
)
def test_parse_osascript_error(stderr: str, expected: tuple[int | None, str]) -> None:
    assert parse_osascript_error(stderr) == expected


def test_the_test_lexer_itself(as_lexer: Lexer) -> None:
    lexed = as_lexer('a "b\\"c" d "\\n"')
    assert lexed.code == "a <S> d <S>"
    assert lexed.literals == ['b"c', "\n"]
    with pytest.raises(ValueError):
        as_lexer('"unterminated')
    with pytest.raises(ValueError):
        as_lexer('"dangling\\')


def test_jxa_string_rejects_non_text() -> None:
    with pytest.raises(AXPilotError):
        jxa_string(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("alias", ["jxa", "JS", " JavaScript "])
def test_language_aliases(runner: FakeRunner, alias: str) -> None:
    runner.register(["osascript"], stdout="")
    run_applescript(runner, "1", language=alias)  # type: ignore[arg-type]
    assert runner.last_call.argv == ["osascript", "-l", "JavaScript"]
