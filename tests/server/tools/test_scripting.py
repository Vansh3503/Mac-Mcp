"""Scripting tools: shell, applescript, shortcuts and defaults. FakeRunner results, the
confirmation text (the exact command), policy checks on script targets and preference domains,
the error matrix, and fault injection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from axpilot.errors import ErrorCode
from axpilot.models import AppInfo
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.scripting import CONFIRM_DETAIL_CHARS, script_targets
from axpilot.system.runner import CommandResult, FakeCall, FakeRunner
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio

ZSH = ["/bin/zsh", "-lc"]


class Recorder:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self.answer


def make_ctx(*, confirm: Any = True, engine: Any = None, **overrides: Any) -> ServerContext:
    return make_test_context({"tools.profile": "full", **overrides}, engine=engine, confirm=confirm)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def ctx(recorder: Recorder) -> ServerContext:
    return make_ctx(confirm=recorder)


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def runner_of(ctx: ServerContext) -> FakeRunner:
    assert isinstance(ctx.runner, FakeRunner)
    return ctx.runner


async def call(client: Client, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args)
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


# =========================================================================== shell
async def test_shell_runs_with_zsh_in_the_home_folder(
    ctx: ServerContext, client: Client, recorder: Recorder
) -> None:
    runner_of(ctx).register(ZSH, stdout="hello\n", stderr="warn\n", exit_code=0)
    data = await ok(client, "shell", {"command": "echo hello"})
    call_ = runner_of(ctx).last_call
    assert call_.argv == [*ZSH, "echo hello"]
    assert call_.cwd == str(Path.home())
    assert call_.timeout_s == 30
    assert data["exit_code"] == 0
    assert data["stdout"] == "hello\n"
    assert data["stderr"] == "warn\n"
    (request,) = recorder.requests
    assert request.tool == "shell"
    assert request.summary == "Run this shell command?"
    assert request.detail is not None
    assert request.detail.startswith("echo hello")
    assert "Stopped after 30 s" in request.detail


async def test_a_failing_command_is_a_normal_result(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(ZSH, stderr="no such file\n", exit_code=2)
    result, error = await call(client, "shell", {"command": "ls /nope"})
    assert error is None
    data = structured(result)
    assert data["exit_code"] == 2
    assert "exit 2" in text_of(result)
    assert "no such file" in text_of(result)


async def test_a_timeout_returns_the_partial_output(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(ZSH, stdout="partial", timed_out=True)
    data = await ok(client, "shell", {"command": "sleep 100", "timeout_s": 5})
    assert data["timed_out"] is True
    assert data["exit_code"] == -1
    assert runner_of(ctx).last_call.timeout_s == 5


async def test_output_is_cut_to_the_configured_size() -> None:
    ctx = make_ctx(**{"scripting.output_max_chars": 500})
    runner_of(ctx).register(ZSH, stdout="x" * 5000)
    async with connected_client(ctx) as client:
        data = await ok(client, "shell", {"command": "yes"})
    assert data["truncated"] is True
    assert len(data["stdout"]) < 700
    assert runner_of(ctx).last_call.max_output == 500


async def test_shell_cwd_and_limits(
    ctx: ServerContext, client: Client, tmp_path: Path, recorder: Recorder
) -> None:
    runner_of(ctx).register(ZSH)
    data = await ok(client, "shell", {"command": "pwd", "cwd": str(tmp_path)})
    assert data["cwd"] == str(tmp_path)
    for args in (
        {"command": "pwd", "cwd": str(tmp_path / "missing")},
        {"command": "pwd", "timeout_s": 31},
        {"command": "   "},
        {"command": "a\x00b"},
    ):
        _, error = await call(client, "shell", args)
        assert error == "INVALID_ARGUMENT", args
    _, error = await call(client, "shell", {"command": "pwd", "timeout_s": 500})
    assert error == "INVALID_ARGUMENT"  # above the schema's 120 s
    assert len(recorder.requests) == 1  # invalid calls never reach the user


async def test_the_timeout_limit_follows_the_config() -> None:
    ctx = make_ctx(**{"scripting.shell_timeout_s": 90})
    runner_of(ctx).register(ZSH)
    async with connected_client(ctx) as client:
        await ok(client, "shell", {"command": "make", "timeout_s": 90})
        assert runner_of(ctx).last_call.timeout_s == 90
        await ok(client, "shell", {"command": "make"})
        assert runner_of(ctx).last_call.timeout_s == 90


async def test_the_configured_shell_is_used() -> None:
    ctx = make_ctx(**{"scripting.shell": "/bin/bash"})
    runner_of(ctx).register(["/bin/bash", "-lc"], stdout="ok")
    async with connected_client(ctx) as client:
        await ok(client, "shell", {"command": "echo ok"})
    assert runner_of(ctx).last_call.argv[0] == "/bin/bash"


async def test_a_declined_command_does_not_run() -> None:
    ctx = make_ctx(confirm=False)
    runner_of(ctx).register(ZSH)
    async with connected_client(ctx) as client:
        _, error = await call(client, "shell", {"command": "rm -rf build"})
    assert error == "USER_DENIED"
    assert runner_of(ctx).calls == []


async def test_long_commands_are_shortened_in_the_question(
    ctx: ServerContext, client: Client, recorder: Recorder
) -> None:
    runner_of(ctx).register(ZSH)
    await ok(client, "shell", {"command": "echo " + "a" * 5000})
    detail = recorder.requests[0].detail or ""
    assert len(detail) < CONFIRM_DETAIL_CHARS + 200
    assert "more characters not shown" in detail


async def test_confirm_never_runs_without_asking() -> None:
    recorder = Recorder()
    ctx = make_ctx(confirm=recorder, **{"safety.confirm": "never"})
    runner_of(ctx).register(ZSH)
    async with connected_client(ctx) as client:
        await ok(client, "shell", {"command": "true"})
    assert recorder.requests == []


async def test_confirm_always_asks_once() -> None:
    recorder = Recorder()
    ctx = make_ctx(confirm=recorder, **{"safety.confirm": "always"})
    runner_of(ctx).register(ZSH)
    async with connected_client(ctx) as client:
        await ok(client, "shell", {"command": "true"})
    assert len(recorder.requests) == 1


async def test_harmless_actions_ask_only_when_confirm_is_always() -> None:
    for mode, expected in (("destructive", []), ("always", ["shortcuts", "defaults"])):
        recorder = Recorder()
        ctx = make_ctx(confirm=recorder, **{"safety.confirm": mode})
        runner_of(ctx).register(["shortcuts", "list"], stdout="A\n")
        runner_of(ctx).register(["defaults", "read"], stdout="1\n")
        async with connected_client(ctx) as client:
            await ok(client, "shortcuts", {})
            await ok(client, "defaults", {"domain": "com.apple.dock", "key": "autohide"})
        assert [r.tool for r in recorder.requests] == expected, mode
        if expected:
            assert recorder.requests[1].summary == "Read the preference autohide of com.apple.dock?"


async def test_scripting_is_off_in_the_standard_profile() -> None:
    ctx = make_test_context()
    async with connected_client(ctx) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("shell", {"command": "true"})
    assert not names & {"shell", "applescript", "shortcuts", "defaults"}
    assert error_code_of(result) == "TOOL_DISABLED"


# =========================================================================== applescript
async def test_applescript_runs_the_script_on_stdin(
    ctx: ServerContext, client: Client, recorder: Recorder
) -> None:
    runner_of(ctx).register(["osascript"], stdout="3\n")
    script = 'tell application "Notes" to count notes'
    data = await ok(client, "applescript", {"script": script})
    call_ = runner_of(ctx).last_call
    assert call_.argv == ["osascript", "-l", "AppleScript"]
    assert call_.input_text == script
    assert data == {**data, "result": "3", "language": "applescript", "apps": ["Notes"]}
    (request,) = recorder.requests
    assert request.summary == "Run this AppleScript script (it addresses Notes)?"
    assert request.detail == script


async def test_jxa(ctx: ServerContext, client: Client, recorder: Recorder) -> None:
    runner_of(ctx).register(["osascript"], stdout="[]\n")
    script = "Application('Calendar').calendars.name()"
    data = await ok(client, "applescript", {"script": script, "language": "javascript"})
    assert runner_of(ctx).last_call.argv == ["osascript", "-l", "JavaScript"]
    assert data["apps"] == ["Calendar"]
    assert "JavaScript (JXA)" in recorder.requests[0].summary


@pytest.mark.parametrize(
    "script",
    [
        'tell application "1Password" to activate',
        'tell application id "com.1password.1password" to quit',
        'tell app "1password" to activate',
        'set x to "tell application" & "x"\ntell application "1Password"\nend tell',
    ],
)
async def test_scripts_naming_protected_apps_are_refused(
    ctx: ServerContext, client: Client, recorder: Recorder, script: str
) -> None:
    runner_of(ctx).register(["osascript"], stdout="")
    _, error = await call(client, "applescript", {"script": script})
    assert error == "BLOCKED_BY_POLICY"
    assert recorder.requests == []
    assert runner_of(ctx).calls == []


async def test_jxa_naming_a_protected_app_is_refused(ctx: ServerContext, client: Client) -> None:
    _, error = await call(
        client,
        "applescript",
        {"script": 'Application("1Password").activate()', "language": "javascript"},
    )
    assert error == "BLOCKED_BY_POLICY"


async def test_apps_that_are_not_installed_match_deny_entries_by_name() -> None:
    ctx = make_ctx(**{"safety.deny_apps": ["Keychain Access"]})
    async with connected_client(ctx) as client:
        _, error = await call(
            client, "applescript", {"script": 'tell application "Keychain Access" to activate'}
        )
    assert error == "BLOCKED_BY_POLICY"


async def test_scripting_a_terminal_needs_the_shell_tool() -> None:
    ctx = make_ctx(**{"tools.disable": ["shell"]})
    runner_of(ctx).register(["osascript"], stdout="")
    async with connected_client(ctx) as client:
        _, error = await call(
            client, "applescript", {"script": 'tell application "Terminal" to do script "ls"'}
        )
        assert error == "BLOCKED_BY_POLICY"
    ctx = make_ctx()  # with shell enabled, scripting Terminal is no worse than the shell itself
    runner_of(ctx).register(["osascript"], stdout="")
    async with connected_client(ctx) as client:
        await ok(client, "applescript", {"script": 'tell application "Terminal" to do script "ls"'})


async def test_system_events_scripts_are_checked_against_the_frontmost_app() -> None:
    ctx = make_ctx()
    runner_of(ctx).register(["osascript"], stdout="")
    script = 'tell application "System Events" to keystroke "x"'
    async with connected_client(ctx) as client:
        await ok(client, "app", {"action": "activate", "name": "Notes"})
        await ok(client, "applescript", {"script": script})
        ctx.config.safety.deny_apps.append("com.apple.Notes")
        _, error = await call(client, "applescript", {"script": script})
    assert error == "BLOCKED_BY_POLICY"


async def test_allow_apps_by_bundle_id_still_allows_scripts_by_name() -> None:
    ctx = make_ctx(**{"safety.allow_apps": ["com.apple.Notes"]})
    runner_of(ctx).register(["osascript"], stdout="")
    async with connected_client(ctx) as client:
        await ok(client, "applescript", {"script": 'tell application "Notes" to count notes'})
        _, error = await call(client, "applescript", {"script": 'tell application "Mail" to count messages'})
    assert error == "BLOCKED_BY_POLICY"


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            "0:10: execution error: Not authorized to send Apple events to Notes. (-1743)",
            "PERMISSION_AUTOMATION",
        ),
        ("0:10: execution error: Notes got an error: User canceled. (-128)", "USER_DENIED"),
        ('0:10: execution error: Can’t get application "Nope". (-2700)', "APP_NOT_FOUND"),
        ("0:5: syntax error: Expected end of line. (-2741)", "COMMAND_FAILED"),
    ],
)
async def test_applescript_errors(ctx: ServerContext, client: Client, stderr: str, code: str) -> None:
    runner_of(ctx).register(["osascript"], stderr=stderr, exit_code=1)
    result, error = await call(client, "applescript", {"script": "return 1"})
    assert error == code, text_of(result)


async def test_applescript_timeout(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(["osascript"], timed_out=True)
    _, error = await call(client, "applescript", {"script": "delay 100", "timeout_s": 2})
    assert error == "TIMEOUT"
    assert runner_of(ctx).last_call.timeout_s == 2


async def test_applescript_on_linux_is_not_supported(ctx: ServerContext, client: Client) -> None:
    _, error = await call(client, "applescript", {"script": "return 1"})  # nothing registered: exit 127
    assert error == "NOT_SUPPORTED"


@pytest.mark.parametrize(
    ("script", "language", "expected"),
    [
        ('tell application "Notes"\nend tell', "applescript", [("Notes", None)]),
        ('tell application id "com.apple.Notes"', "applescript", [("com.apple.Notes", "com.apple.Notes")]),
        ('tell app "Mail" to quit\ntell application "Mail" to activate', "applescript", [("Mail", None)]),
        ('tell application "Say \\"hi\\"" to quit', "applescript", [('Say "hi"', None)]),
        ("Application('Mail').activate(); Application(\"com.apple.Notes\")", "javascript",
         [("Mail", None), ("com.apple.Notes", "com.apple.Notes")]),
        ("return 1", "applescript", []),
    ],
)  # fmt: skip
def test_script_targets(script: str, language: Any, expected: list[tuple[str, str | None]]) -> None:
    assert [(app.name, app.bundle_id) for app in script_targets(script, language)] == expected


# =========================================================================== shortcuts
async def test_shortcuts_list_is_not_confirmed(
    ctx: ServerContext, client: Client, recorder: Recorder
) -> None:
    runner_of(ctx).register(["shortcuts", "list"], stdout="Morning Routine\nResize Image\nNight Mode\n")
    data = await ok(client, "shortcuts", {})
    assert data["shortcuts"] == ["Morning Routine", "Resize Image", "Night Mode"]
    filtered = await ok(client, "shortcuts", {"query": "MODE", "limit": 5})
    assert filtered["shortcuts"] == ["Night Mode"]
    limited = await ok(client, "shortcuts", {"limit": 1})
    assert limited["count"] == 3
    assert limited["shortcuts"] == ["Morning Routine"]
    assert recorder.requests == []


async def test_shortcuts_run_asks_and_passes_input(
    ctx: ServerContext, client: Client, recorder: Recorder
) -> None:
    seen: list[str] = []

    def answer(call_: FakeCall) -> CommandResult:
        path = call_.argv[call_.argv.index("--input-path") + 1]
        seen.append(Path(path).read_text(encoding="utf-8"))
        return CommandResult(call_.argv, 0, "resized\n", "")

    runner_of(ctx).register(["shortcuts", "run"], handler=answer)
    data = await ok(client, "shortcuts", {"action": "run", "name": "Resize Image", "input": "photo.png"})
    assert data["output"] == "resized"
    assert seen == ["photo.png"]
    (request,) = recorder.requests
    assert request.summary == "Run the shortcut 'Resize Image'?"
    assert request.detail == "Input: photo.png"


async def test_shortcuts_errors(ctx: ServerContext, client: Client, recorder: Recorder) -> None:
    runner_of(ctx).register(["shortcuts", "run"], stderr="Error: Couldn’t find shortcut", exit_code=1)
    _, error = await call(client, "shortcuts", {"action": "run", "name": "Nope"})
    assert error == "INVALID_ARGUMENT"
    for args in (
        {"action": "run"},
        {"action": "run", "name": "x", "query": "y"},
        {"action": "list", "name": "x"},
        {"action": "run", "name": "-flag"},
    ):
        _, error = await call(client, "shortcuts", args)
        assert error == "INVALID_ARGUMENT", args
    runner_of(ctx).register(["shortcuts", "run"], timed_out=True)
    _, error = await call(client, "shortcuts", {"action": "run", "name": "Ask"})
    assert error == "TIMEOUT"


# =========================================================================== defaults
async def test_defaults_read(ctx: ServerContext, client: Client, recorder: Recorder) -> None:
    runner_of(ctx).register(["defaults", "read"], stdout="1\n")
    data = await ok(client, "defaults", {"domain": "com.apple.dock", "key": "autohide"})
    assert runner_of(ctx).last_call.argv == ["defaults", "read", "com.apple.dock", "autohide"]
    assert data["value"] == "1"
    whole = await ok(client, "defaults", {"domain": "-g"})
    assert runner_of(ctx).last_call.argv == ["defaults", "read", "NSGlobalDomain"]
    assert whole["key"] is None
    assert recorder.requests == []


async def test_defaults_write_asks_with_the_exact_command(
    ctx: ServerContext, client: Client, recorder: Recorder
) -> None:
    runner_of(ctx).register(["defaults", "write"])
    data = await ok(
        client, "defaults", {"action": "write", "domain": "com.apple.dock", "key": "autohide", "value": True}
    )
    assert runner_of(ctx).last_call.argv == [
        "defaults",
        "write",
        "com.apple.dock",
        "autohide",
        "-bool",
        "true",
    ]
    assert data["command"] == "defaults write com.apple.dock autohide -bool true"
    assert data["type"] == "bool"
    (request,) = recorder.requests
    assert request.detail == "defaults write com.apple.dock autohide -bool true"
    await ok(
        client,
        "defaults",
        {"action": "write", "domain": "com.example", "key": "n", "value": "5", "type": "int"},
    )
    assert runner_of(ctx).last_call.argv[-2:] == ["-int", "5"]
    await ok(
        client,
        "defaults",
        {"action": "write", "domain": "com.example", "key": "s", "value": "it's; rm -rf ~"},
    )
    assert runner_of(ctx).last_call.argv[-2:] == ["-string", "it's; rm -rf ~"]  # an argv item, no shell


async def test_defaults_refuses_protected_domains(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(["defaults"], stdout="secret")
    for args in (
        {"domain": "com.1password.1password"},
        {"domain": "~/Library/Preferences/com.1password.1password.plist"},
        {"action": "write", "domain": "com.apple.keychainaccess", "key": "k", "value": 1},
    ):
        _, error = await call(client, "defaults", args)
        assert error == "BLOCKED_BY_POLICY", args
    assert runner_of(ctx).calls == []


async def test_defaults_errors(ctx: ServerContext, client: Client, recorder: Recorder) -> None:
    runner_of(ctx).register(
        ["defaults", "read"], stderr="The domain/default pair of (x, y) does not exist", exit_code=1
    )
    result, error = await call(client, "defaults", {"domain": "x.y.z", "key": "y"})
    assert error == "COMMAND_FAILED"
    assert result.meta["axpilot/error"]["details"]["reason"] == "not_found"
    for args in (
        {"action": "write", "domain": "com.example", "key": "k"},
        {"action": "write", "domain": "com.example", "value": 1},
        {"domain": "com.example", "value": 1},
        {"domain": "-flag"},
        {"action": "write", "domain": "com.example", "key": "k", "value": "abc", "type": "int"},
    ):
        _, error = await call(client, "defaults", args)
        assert error == "INVALID_ARGUMENT", args
    assert recorder.requests == []


# =========================================================================== faults and audit
async def test_engine_faults_surface_as_errors() -> None:
    engine = FaultyEngine(
        __import__("axpilot.engine.fake", fromlist=["FakeEngine"]).FakeEngine.demo(),
        {"list_apps": Fault.error(ErrorCode.TIMEOUT)},
    )
    ctx = make_ctx(engine=engine)
    async with connected_client(ctx) as client:
        _, error = await call(client, "applescript", {"script": 'tell application "Notes" to quit'})
        assert error == "TIMEOUT"
        _, error = await call(client, "defaults", {"domain": "com.apple.dock"})
        assert error == "TIMEOUT"


async def test_a_crashing_runner_is_internal_and_logged(ctx: ServerContext, client: Client) -> None:
    def boom(call_: FakeCall) -> CommandResult:
        raise RuntimeError("runner exploded")

    runner_of(ctx).register(ZSH, handler=boom)
    result, error = await call(client, "shell", {"command": "true"})
    assert error == "INTERNAL"
    assert "runner exploded" not in text_of(result)


async def test_commands_are_redacted_in_the_audit_log(ctx: ServerContext, client: Client) -> None:
    runner_of(ctx).register(ZSH)
    await ok(client, "shell", {"command": "export TOKEN=hunter2"})
    records = ctx.audit.tail(10)
    assert [record["tool"] for record in records] == ["shell"]
    assert "hunter2" not in str(records)


def test_stand_ins_name_the_app() -> None:
    (app,) = script_targets('tell application id "com.apple.Notes" to quit', "applescript")
    assert app == AppInfo(name="com.apple.Notes", pid=0, bundle_id="com.apple.Notes")
