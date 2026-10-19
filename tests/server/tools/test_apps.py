"""App, window and open tools: success paths, policy, confirmations, the error matrix and
fault injection."""

from __future__ import annotations

import plistlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.apps import app_from_query
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio

NOTES_WINDOW = 401
SETTINGS_WINDOW = 801  # System Settings › Privacy & Security (deny-listed window)
ONEPASSWORD_WINDOW = 701
TERMINAL_WINDOW = 601


class Recorder:
    """A confirmer that records each question and answers with ``answer``."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self.answer


async def no_sleep(_: float) -> None:
    await anyio.lowlevel.checkpoint()


def make_ctx(
    profile: str = "full", *, engine: Any = None, confirm: Any = True, **overrides: Any
) -> ServerContext:
    ctx = make_test_context({"tools.profile": profile, **overrides}, engine=engine, confirm=confirm)
    ctx.sleep = no_sleep
    return ctx


@pytest.fixture
def ctx() -> ServerContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def engine_of(ctx: ServerContext) -> FakeEngine:
    engine = getattr(ctx.engine, "inner", ctx.engine)
    assert isinstance(engine, FakeEngine)
    return engine


async def call(client: Client, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args)
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


def running(ctx: ServerContext, name: str) -> bool:
    return any(app.name == name for app in engine_of(ctx).list_apps())


def window(ctx: ServerContext, window_id: int) -> Any:
    return next((w for w in engine_of(ctx).list_windows() if w.window_id == window_id), None)


# =========================================================================== app
async def test_launch_activate_hide_unhide_and_quit(ctx: ServerContext, client: Client) -> None:
    launched = await ok(client, "app", {"action": "launch", "name": "Calculator"})
    assert launched["app"]["name"] == "Calculator"
    assert launched["app"]["frontmost"] is True
    assert running(ctx, "Calculator")
    activated = await ok(client, "app", {"action": "activate", "name": "Notes"})
    assert activated["app"]["frontmost"] is True
    hidden = await ok(client, "app", {"action": "hide", "name": "Notes"})
    assert hidden["app"]["hidden"] is True
    shown = await ok(client, "app", {"action": "unhide", "name": "com.apple.Notes"})
    assert shown["app"]["hidden"] is False
    quit_result, error = await call(client, "app", {"action": "quit", "name": "Calculator"})
    assert error is None
    assert structured(quit_result)["app"] is None
    assert "asked Calculator to quit" in text_of(quit_result)
    assert not running(ctx, "Calculator")


@pytest.mark.parametrize(
    "name", ["com.apple.calculator", "/System/Applications/Calculator.app", "calculator"]
)
async def test_launch_by_bundle_id_path_or_name(ctx: ServerContext, client: Client, name: str) -> None:
    data = await ok(client, "app", {"action": "launch", "name": name})
    assert data["app"]["bundle_id"] == "com.apple.calculator"


async def test_force_quit_asks_the_user_after_the_policy_check() -> None:
    recorder = Recorder(answer=False)
    ctx = make_ctx(confirm=recorder)
    async with connected_client(ctx) as client:
        result, error = await call(client, "app", {"action": "force_quit", "name": "Notes"})
        assert error == "USER_DENIED", text_of(result)
        assert running(ctx, "Notes")
        (request,) = recorder.requests
        assert request.tool == "app"
        assert "Force quit Notes" in request.summary
        _, blocked = await call(client, "app", {"action": "force_quit", "name": "1Password"})
        assert blocked == "BLOCKED_BY_POLICY"
        assert len(recorder.requests) == 1  # a refused target is never put to the user
        await ok(client, "app", {"action": "quit", "name": "Notes"})  # quitting normally asks nothing
        assert len(recorder.requests) == 1


async def test_force_quit_runs_once_approved(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "app", {"action": "force_quit", "name": "Notes"})
    assert data["app"] is None
    assert not running(ctx, "Notes")
    assert engine_of(ctx).events_of("app_control")[-1]["action"] == "force_quit"


async def test_confirm_always_asks_once_per_call() -> None:
    recorder = Recorder()
    ctx = make_ctx(confirm=recorder, **{"safety.confirm": "always"})
    async with connected_client(ctx) as client:
        await ok(client, "app", {"action": "force_quit", "name": "Notes"})
        await ok(client, "app", {"action": "activate", "name": "Finder"})
    assert len(recorder.requests) == 2


@pytest.mark.parametrize(
    "args",
    [
        {"action": "activate", "name": "1Password"},
        {"action": "quit", "name": "com.1password.1password"},
        {"action": "launch", "name": "1Password"},
        {"action": "launch", "name": "/Applications/1Password.app"},
    ],
)
async def test_protected_apps_cannot_be_managed(
    ctx: ServerContext, client: Client, args: dict[str, Any]
) -> None:
    before = len(engine_of(ctx).events_of("app_control"))
    result, error = await call(client, "app", args)
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert "1Password" in text_of(result)
    assert len(engine_of(ctx).events_of("app_control")) == before


async def test_terminals_can_be_brought_forward_without_the_shell_tool() -> None:
    ctx = make_ctx("standard")
    async with connected_client(ctx) as client:
        data = await ok(client, "app", {"action": "activate", "name": "Terminal"})
        assert data["app"]["frontmost"] is True
        _, typed = await call(client, "type_text", {"text": "ls"})
    assert typed == "BLOCKED_BY_POLICY"  # but typing into it is still blocked


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"action": "activate", "name": "Calculator"}, ErrorCode.APP_NOT_FOUND),  # not running
        ({"action": "quit", "name": "No Such App"}, ErrorCode.APP_NOT_FOUND),
        ({"action": "launch", "name": "No Such App"}, ErrorCode.APP_NOT_FOUND),
        ({"action": "activate", "name": "   "}, ErrorCode.INVALID_ARGUMENT),
        ({"action": "explode", "name": "Notes"}, ErrorCode.INVALID_ARGUMENT),
        ({"action": "launch"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_app_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "app", args)
    assert error == code.value, text_of(result)


def test_launch_queries_are_guessed_for_the_policy(tmp_path: Path) -> None:
    assert app_from_query("/Applications/1Password.app").name == "1Password"
    bundle = fake_bundle(tmp_path / "Renamed.app", "com.example.real")
    assert app_from_query(str(bundle)).bundle_id == "com.example.real"
    (tmp_path / "Broken.app" / "Contents").mkdir(parents=True)
    (tmp_path / "Broken.app" / "Contents" / "Info.plist").write_text("not a plist", encoding="utf-8")
    assert app_from_query(str(tmp_path / "Broken.app")).bundle_id is None
    assert app_from_query("com.1password.1password").bundle_id == "com.1password.1password"
    assert app_from_query("Notes.app").name == "Notes"
    assert app_from_query("Notes").bundle_id is None


@pytest.mark.parametrize(
    ("fault", "code"), [(Fault.crash("bug in /Users/me"), "INTERNAL"), (Fault.hang(10), "TIMEOUT")]
)
async def test_engine_faults_in_app(fault: Fault, code: str) -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = make_ctx(engine=faulty)
    ctx.engine_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            faulty.set_fault("app_control", fault)
            result, error = await call(client, "app", {"action": "activate", "name": "Notes"})
            assert error == code, text_of(result)
            assert "/Users/me" not in text_of(result)
            faulty.clear_faults()
            await ok(client, "app", {"action": "activate", "name": "Notes"})
    finally:
        faulty.release()


# =========================================================================== window
async def test_window_move_resize_and_states(ctx: ServerContext, client: Client) -> None:
    moved = await ok(client, "window", {"action": "move", "window_id": NOTES_WINDOW, "x": 40, "y": 60})
    assert moved["window"]["frame"][:2] == [40, 60]
    resized = await ok(
        client, "window", {"action": "resize", "window_id": NOTES_WINDOW, "width": 900, "height": 700}
    )
    assert resized["window"]["frame"] == [40, 60, 900, 700]
    minimized = await ok(client, "window", {"action": "minimize", "window_id": NOTES_WINDOW})
    assert minimized["window"]["minimized"] is True
    restored = await ok(client, "window", {"action": "restore", "window_id": NOTES_WINDOW})
    assert restored["window"]["minimized"] is False
    for action in ("zoom", "fullscreen", "focus"):
        await ok(client, "window", {"action": action, "window_id": NOTES_WINDOW})
    actions = [event["action"] for event in engine_of(ctx).events_of("window_control")]
    assert actions == ["move", "resize", "minimize", "restore", "zoom", "fullscreen", "focus"]


async def test_window_by_ref_and_close(ctx: ServerContext, client: Client) -> None:
    snap = await ok(client, "snapshot", {"app": "TextEdit"})
    ref = snap["elements"][0]["ref"]
    window_id = snap["window"]["window_id"]
    result, error = await call(client, "window", {"action": "close", "ref": ref})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["closed"] is True
    assert data["window"] is None
    assert window(ctx, window_id) is None


@pytest.mark.parametrize("window_id", [SETTINGS_WINDOW, ONEPASSWORD_WINDOW])
async def test_protected_windows_cannot_be_managed(
    ctx: ServerContext, client: Client, window_id: int
) -> None:
    result, error = await call(client, "window", {"action": "minimize", "window_id": window_id})
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert not engine_of(ctx).events_of("window_control")


async def test_terminal_windows_can_be_moved_without_the_shell_tool() -> None:
    ctx = make_ctx("standard")
    async with connected_client(ctx) as client:
        await ok(client, "window", {"action": "move", "window_id": TERMINAL_WINDOW, "x": 10, "y": 40})


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"action": "move", "window_id": NOTES_WINDOW, "x": 10}, ErrorCode.INVALID_ARGUMENT),
        (
            {"action": "move", "window_id": NOTES_WINDOW, "x": 10, "y": 10, "width": 5},
            ErrorCode.INVALID_ARGUMENT,
        ),
        ({"action": "move", "window_id": NOTES_WINDOW, "x": -50000, "y": 10}, ErrorCode.INVALID_ARGUMENT),
        ({"action": "resize", "window_id": NOTES_WINDOW, "width": 100}, ErrorCode.INVALID_ARGUMENT),
        (
            {"action": "resize", "window_id": NOTES_WINDOW, "width": 0, "height": 10},
            ErrorCode.INVALID_ARGUMENT,
        ),
        (
            {"action": "resize", "window_id": NOTES_WINDOW, "width": 9, "height": 9, "x": 1},
            ErrorCode.INVALID_ARGUMENT,
        ),
        ({"action": "minimize", "window_id": NOTES_WINDOW, "width": 5}, ErrorCode.INVALID_ARGUMENT),
        ({"action": "minimize"}, ErrorCode.INVALID_ARGUMENT),
        ({"action": "minimize", "window_id": NOTES_WINDOW, "ref": "e1"}, ErrorCode.INVALID_ARGUMENT),
        ({"action": "minimize", "window_id": 999999}, ErrorCode.WINDOW_NOT_FOUND),
        ({"action": "minimize", "ref": "e99999"}, ErrorCode.STALE_REF),
        ({"action": "minimize", "ref": "s_zzzz:e1"}, ErrorCode.SNAPSHOT_NOT_FOUND),
        ({"action": "wiggle", "window_id": NOTES_WINDOW}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_window_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await ok(client, "snapshot", {"app": "Notes"})
    result, error = await call(client, "window", args)
    assert error == code.value, text_of(result)


async def test_engine_faults_in_window() -> None:
    faulty = FaultyEngine(FakeEngine.demo(), {"window_control": Fault.crash()})
    ctx = make_ctx(engine=faulty)
    async with connected_client(ctx) as client:
        _, error = await call(client, "window", {"action": "focus", "window_id": NOTES_WINDOW})
        assert error == "INTERNAL"
        faulty.clear_faults()
        await ok(client, "window", {"action": "focus", "window_id": NOTES_WINDOW})


# =========================================================================== open
@pytest.mark.parametrize(
    "target",
    ["https://example.com/a?b=c", "http://localhost:8000", "mailto:alex@example.com",
     "x-apple.systempreferences:com.apple.preference.sound"],
)  # fmt: skip
async def test_open_urls(ctx: ServerContext, client: Client, target: str) -> None:
    data = await ok(client, "open", {"target": target})
    assert data["kind"] == "url"
    assert engine_of(ctx).events_of("open")[-1]["target"] == target


async def test_app_url_schemes_need_scripting() -> None:
    ctx = make_ctx("standard")
    async with connected_client(ctx) as client:
        result, error = await call(client, "open", {"target": "shortcuts://run-shortcut?name=Wipe"})
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert "shortcuts:" in text_of(result)
    assert not engine_of(ctx).events_of("open")


async def test_app_url_schemes_open_with_scripting_and_a_warning(client: Client) -> None:
    data = await ok(client, "open", {"target": "zoommtg://zoom.us/join?confno=1"})
    assert any("app command" in w for w in data["warnings"])


async def test_open_files_folders_and_file_urls(ctx: ServerContext, client: Client, tmp_path: Path) -> None:
    document = tmp_path / "My Report.txt"
    document.write_text("hello", encoding="utf-8")
    assert (await ok(client, "open", {"target": str(document)}))["kind"] == "file"
    assert (await ok(client, "open", {"target": str(tmp_path)}))["kind"] == "folder"
    url = "file://" + str(document).replace(" ", "%20")
    data = await ok(client, "open", {"target": url})
    assert data["target"] == str(document)
    revealed = await ok(client, "open", {"target": str(document), "reveal": True})
    assert revealed["revealed"] is True
    event = engine_of(ctx).events_of("open")[-1]
    assert event["reveal"] is True
    assert event["with_app"] == "com.apple.finder"
    with_app = await ok(client, "open", {"target": str(document), "with_app": "TextEdit"})
    assert with_app["with_app"] == "TextEdit"


async def test_open_runs_no_code_without_the_shell_tool(tmp_path: Path) -> None:
    command = tmp_path / "setup.command"
    command.write_text("echo hi\n")
    binary = tmp_path / "tool"
    binary.write_text("#!/bin/sh\necho hi\n")
    binary.chmod(0o755)
    weblink = tmp_path / "Wipe.webloc"
    weblink.write_text("<plist/>")
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    ctx = make_ctx("standard")
    async with connected_client(ctx) as client:
        for args in (
            {"target": str(command)},
            {"target": "file://" + str(command)},
            {"target": str(binary)},
            {"target": str(weblink)},
            {"target": str(weblink), "with_app": "Safari"},
            {"target": str(notes), "with_app": "Terminal"},
            {"target": "https://example.com", "with_app": "com.apple.Terminal"},
        ):
            result, error = await call(client, "open", args)
            assert error == "BLOCKED_BY_POLICY", (args, text_of(result))
        assert not engine_of(ctx).events_of("open")
        await ok(client, "open", {"target": str(command), "with_app": "TextEdit"})  # shown, not run
        await ok(client, "open", {"target": str(binary), "reveal": True})
        await ok(client, "open", {"target": str(notes)})
    assert len(engine_of(ctx).events_of("open")) == 3
    shell_ctx = make_ctx("standard", **{"tools.enable": ["shell"]})
    async with connected_client(shell_ctx) as client:
        await ok(client, "open", {"target": str(command)})
        await ok(client, "open", {"target": str(notes), "with_app": "Terminal"})
    browser_ctx = make_ctx("standard", **{"tools.enable": ["applescript"]})
    async with connected_client(browser_ctx) as client:
        await ok(client, "open", {"target": str(weblink)})  # any scripting tool allows app URLs
        _, error = await call(client, "open", {"target": str(command)})
        assert error == "BLOCKED_BY_POLICY"  # running code needs the shell tool itself


def fake_bundle(folder: Path, bundle_id: str) -> Path:
    """An .app folder whose Info.plist names ``bundle_id``."""
    contents = folder / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as file:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleName": folder.stem}, file)
    return folder


async def test_open_cannot_reach_protected_apps(client: Client, tmp_path: Path) -> None:
    # a renamed copy of a password manager is still that password manager
    bundle = fake_bundle(tmp_path / "Totally Harmless.app", "com.1password.1password")
    _, error = await call(client, "open", {"target": str(bundle)})
    assert error == "BLOCKED_BY_POLICY"
    _, error = await call(client, "app", {"action": "launch", "name": str(bundle)})
    assert error == "BLOCKED_BY_POLICY"
    _, error = await call(client, "open", {"target": "https://example.com", "with_app": "1Password"})
    assert error == "BLOCKED_BY_POLICY"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"target": "   "}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "relative/file.txt"}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "/definitely/missing/file.txt"}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "file://otherhost/etc/hosts"}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "https://example.com", "reveal": True}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "/tmp", "reveal": True, "with_app": "TextEdit"}, ErrorCode.INVALID_ARGUMENT),
        ({"target": "https://example.com", "with_app": "No Such App"}, ErrorCode.APP_NOT_FOUND),
    ],
)
async def test_open_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "open", args)
    assert error == code.value, text_of(result)


async def test_engine_faults_in_open() -> None:
    faulty = FaultyEngine(
        FakeEngine.demo(), {"open_target": Fault.error(ErrorCode.COMMAND_FAILED, "open failed")}
    )
    ctx = make_ctx(engine=faulty)
    async with connected_client(ctx) as client:
        _, error = await call(client, "open", {"target": "https://example.com"})
    assert error == "COMMAND_FAILED"
