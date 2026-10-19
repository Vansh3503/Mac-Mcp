"""The automation SDK against the simulated desktop: every method, typed results and handles,
errors rebuilt from the tool results, the sync wrapper (threads, lifecycle) and the host
connection."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import anyio
import pytest

import axpilot
from axpilot import AsyncDesktop, Desktop
from axpilot.engine.fake import DEMO_FIXTURE, FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import AutoConfirmer, ConfirmRequest
from axpilot.sdk import ActionResult, AsyncElementHandle, ElementHandle, ReadResult, SnapshotResult
from axpilot.testing import make_test_context
from axpilot.testing.host import running_host


def events(mac: Desktop | AsyncDesktop, kind: str) -> list[dict[str, Any]]:
    engine = mac.context.engine
    assert isinstance(engine, FakeEngine)
    return engine.events_of(kind)


@pytest.fixture
def mac() -> Any:
    with Desktop.fake() as desktop:
        yield desktop


# =========================================================================== package
def test_lazy_exports() -> None:
    assert axpilot.Desktop is Desktop
    assert axpilot.AsyncDesktop is AsyncDesktop
    with pytest.raises(AttributeError):
        _ = axpilot.Nothing  # type: ignore[attr-defined]


# =========================================================================== observe
def test_snapshot_and_handles(mac: Desktop) -> None:
    snap = mac.snapshot("Notes")
    assert isinstance(snap, SnapshotResult)
    assert (snap.app, snap.window) == ("Notes", "Notes")
    assert snap.snapshot_id.startswith("s_")
    assert len(snap) == len(snap.elements) > 10
    assert snap.focused is not None
    assert snap.focused.focused
    button = snap.find("new note")
    assert isinstance(button, ElementHandle)
    assert button.element is not None
    assert button.element.role == "button"
    assert snap.find("New Note", exact=True) is not None
    assert snap.find("New", exact=True) is None
    assert snap.find(role="search") is not None
    rows = snap.find_all(role="row")
    assert len(rows) >= 4
    assert snap.element(rows[0].ref).ref == rows[0].ref
    with pytest.raises(AXPilotError) as caught:
        snap.element("e9999")
    assert caught.value.code is ErrorCode.STALE_REF
    assert [el.ref for el in snap][:3] == [el.ref for el in snap.elements][:3]


def test_observe_methods(mac: Desktop) -> None:
    status = mac.status()
    assert status["engine"] == "fake"
    assert status.text
    assert "Notes" in {app["name"] for app in mac.list_apps()}
    assert mac.list_windows("Notes")[0]["title"] == "Notes"
    assert len(mac.list_displays()) == 2
    assert "windows" in mac.explore("Notes").data
    shot = mac.screenshot()
    assert (shot.width, shot.height) == (1280, 800)
    assert shot.image[:2] in (b"\x89P", b"\xff\xd8")  # PNG or JPEG
    snap = mac.snapshot("Notes")
    inspected = mac.inspect(snap.find_all("New Note")[0].ref, snapshot_id=snap.snapshot_id)
    assert inspected["element"]["name"] == "New Note"
    waited = mac.wait_for("window", "Notes", timeout_s=1)
    assert waited["satisfied"] is True
    assert mac.ocr("Notes").data


def test_screenshot_save(mac: Desktop, tmp_path: Path) -> None:
    shot = mac.screenshot("Notes")
    saved = shot.save(tmp_path / "notes.img")
    assert saved.read_bytes() == shot.image


def test_find_and_read(mac: Desktop) -> None:
    found = mac.find("Share", app="Notes")
    assert found.found
    assert isinstance(found.first, ElementHandle)
    assert found.matches[0].ref == found.first.ref
    clicked = found.click()  # acts on the best match
    assert isinstance(clicked, ActionResult)
    nothing = mac.find("Zebra crossing", app="Notes")
    assert not nothing.found
    with pytest.raises(AXPilotError):
        _ = nothing.first
    read = mac.read("Notes")
    assert isinstance(read, ReadResult)
    assert read.complete
    assert read.rows == read.lines
    assert read.content.count("\n") == len(read.lines) - 1
    assert read.pages >= 1


# =========================================================================== input
def test_input_methods(mac: Desktop) -> None:
    mac.app("Notes").activate()
    snap = mac.snapshot("Notes")
    new_note = snap.find("New Note")
    assert new_note is not None
    result = new_note.click()
    assert result.strategy is not None
    assert result.message.startswith("clicked")
    assert result.snapshot_id
    typed = mac.type_text("Groceries")
    assert "typed" in typed.message
    search = mac.snapshot("Notes").find(role="search")
    assert search is not None
    search.type_text("milk", clear=True)
    assert mac.snapshot("Notes").find(role="search").element.value == "milk"  # type: ignore[union-attr]
    assert mac.press_keys(["cmd+a", "delete"]).message
    assert mac.press_keys("escape", repeat=2).message
    assert mac.menu("File", "New Note").message
    assert "File" in {menu["title"] for menu in mac.menus("Notes")["menus"]}
    assert mac.scroll(direction="down", app="Notes").data
    assert mac.move_mouse(x=500, y=500).message
    assert mac.drag(from_point=(500, 500), to_point=(600, 500)).message
    assert events(mac, "mouse_drag")


def test_values_and_forms() -> None:
    with Desktop.fake() as mac:
        mac.app("Login Demo").activate()
        snap = mac.snapshot("Login Demo")
        email, remember = snap.find("Email", role="field"), snap.find("Remember me")
        assert email is not None
        assert remember is not None
        result = mac.fill_form(
            {email.ref: "ada@example.com", remember.ref: True}, snapshot_id=snap.snapshot_id
        )
        assert result.message
        after = mac.snapshot("Login Demo")
        assert after.find("Email", role="field").element.value == "ada@example.com"  # type: ignore[union-attr]
        assert after.find("Remember me").element.checked  # type: ignore[union-attr]
        remember_again = after.find("Remember me")
        assert remember_again is not None
        assert remember_again.set_value(False).message
        assert not mac.snapshot("Login Demo").find("Remember me").element.checked  # type: ignore[union-attr]
        password = after.find("Password", role="secure")
        assert password is not None
        with pytest.raises(AXPilotError) as caught:
            password.type_text("hunter2")
        assert caught.value.code is ErrorCode.BLOCKED_BY_POLICY


def test_options_and_dialogs(mac: Desktop) -> None:
    mac.app("TextEdit").activate()
    group = mac.snapshot("TextEdit").find("Text Alignment", role="radiogroup")
    assert group is not None
    assert mac.select_option(group.ref, "Center").message.startswith("selected")
    dialog = mac.handle_dialog(app="Login Demo")
    assert dialog["found"] is True
    assert [b["name"] for b in dialog["buttons"]] == ["Cancel", "Sign In"]
    assert mac.handle_dialog("cancel", app="Login Demo")["pressed"] == "Cancel"


# =========================================================================== apps and system
def test_apps_and_system(mac: Desktop) -> None:
    calculator = mac.app("Calculator").launch()
    assert calculator.windows()
    assert calculator.snapshot().app == "Calculator"
    calculator.hide()
    assert calculator.quit()["action"] == "quit"
    mac.set_clipboard("from the SDK")
    assert mac.get_clipboard() == "from the SDK"
    mac.notify("Done", "All good")
    assert events(mac, "notify")[-1]["title"] == "Done"
    assert mac.open_target("https://example.com").data
    window_id = mac.list_windows("Notes")[0]["window_id"]
    assert mac.window("move", window_id=window_id, x=10, y=40).data["window"]["frame"][:2] == [10, 40]
    assert mac.call("list_displays")["displays"]


def test_scripting_goes_through_the_same_runner() -> None:
    with Desktop.fake() as mac:
        runner = mac.context.runner
        runner.register(["/bin/zsh", "-lc"], stdout="hi\n")
        runner.register(["osascript"], stdout="42\n")
        assert mac.shell("echo hi")["stdout"] == "hi\n"
        assert mac.applescript("return 42") == "42"


# =========================================================================== errors and policy
def test_errors_are_rebuilt(mac: Desktop) -> None:
    with pytest.raises(AXPilotError) as caught:
        mac.snapshot("NoSuchApp")
    assert caught.value.code is ErrorCode.APP_NOT_FOUND
    assert caught.value.hint
    with pytest.raises(AXPilotError) as caught:
        mac.app("1Password").activate()
    assert caught.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert caught.value.details["rule"] == "deny_apps"
    with pytest.raises(AXPilotError) as caught:
        mac.call("no_such_tool")
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError):
        mac.menu()


def test_confirmations_and_profiles() -> None:
    asked: list[ConfirmRequest] = []

    class Recorder:
        async def confirm(self, request: ConfirmRequest) -> bool:
            asked.append(request)
            return False

    with Desktop.fake(confirm=Recorder()) as mac:
        with pytest.raises(AXPilotError) as caught:
            mac.app_action("force_quit", "Notes")
        assert caught.value.code is ErrorCode.USER_DENIED
    assert [request.tool for request in asked] == ["app"]
    with Desktop.fake(profile="minimal") as mac, pytest.raises(AXPilotError) as caught:
        mac.menu("File", "New Note")
    assert caught.value.code is ErrorCode.TOOL_DISABLED
    with Desktop.fake(config={"safety.deny_apps": ["com.apple.Notes"]}) as mac, pytest.raises(AXPilotError):
        mac.snapshot("Notes")


def test_audit_records_name_the_sdk(mac: Desktop) -> None:
    mac.status()
    records = mac.context.audit.tail(5)
    assert records[-1]["tool"] == "status"
    assert str(records[-1]["client"]).startswith("AXPilot SDK")


def test_unknown_error_codes_become_internal() -> None:
    err = AXPilotError.from_dict({"code": "SOMETHING_NEW", "message": "x", "hint": "y", "details": {"a": 1}})
    assert err.code is ErrorCode.INTERNAL
    assert err.details == {"a": 1, "code": "SOMETHING_NEW"}
    assert AXPilotError.from_dict({"code": "TIMEOUT"}).message == "TIMEOUT"


# =========================================================================== lifecycle
def test_lazy_open_and_idempotent_close() -> None:
    mac = Desktop.fake()
    assert mac.status()["engine"] == "fake"  # opens on first use
    engine = mac.context.engine
    mac.close()
    mac.close()
    assert mac.context is None
    assert engine is not None


def test_closing_stops_the_thread() -> None:
    before = threading.active_count()
    with Desktop.fake() as mac:
        mac.status()
        assert threading.active_count() > before
    assert threading.active_count() <= before + 1  # worker threads may linger briefly; the loop is gone


def test_calls_from_several_threads() -> None:
    with Desktop.fake() as mac:
        results: list[str] = []
        errors: list[BaseException] = []

        def work(app: str) -> None:
            try:
                for _ in range(3):
                    results.append(mac.snapshot(app).app)
            except BaseException as exc:  # collected for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(app,)) for app in ("Notes", "Finder", "Calculator")]
        mac.app("Calculator").launch()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    assert errors == []
    assert sorted(set(results)) == ["Calculator", "Finder", "Notes"]
    assert len(results) == 9


def test_an_error_while_opening_leaves_nothing_running(tmp_path: Path) -> None:
    before = threading.active_count()
    with pytest.raises(AXPilotError) as caught:
        Desktop.fake(profile="godmode").open()
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert threading.active_count() <= before + 1


def test_fixtures(tmp_path: Path) -> None:

    data = json.loads(Path(DEMO_FIXTURE).read_text(encoding="utf-8"))
    data["os_version"] = "15.9"
    path = tmp_path / "desk.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with Desktop.fake(path) as mac:
        assert mac.status()["os_version"] == "15.9"


def test_direct_needs_a_mac_unless_given_an_engine() -> None:

    if sys.platform != "darwin":
        with pytest.raises(AXPilotError) as caught:
            Desktop.direct()
        assert caught.value.code is ErrorCode.UNSUPPORTED_OS
    with Desktop.direct(make_test_context().config, FakeEngine.demo(), confirm=True) as mac:
        assert mac.status()["engine"] == "fake"


# =========================================================================== async
@pytest.mark.anyio
async def test_async_desktop() -> None:
    async with AsyncDesktop.fake() as mac:
        snap = await mac.snapshot("Notes")
        button = snap.find("New Note")
        assert isinstance(button, AsyncElementHandle)
        result = await button.click()
        assert result.message.startswith("clicked")
        notes = await mac.app("Notes").activate()
        assert (await notes.snapshot()).app == "Notes"
        found = await mac.find("Share", app="Notes")
        assert isinstance(found.first, AsyncElementHandle)
        assert (await found.first.inspect())["element"]["name"] == "Share"
        with pytest.raises(AXPilotError) as caught:
            await mac.snapshot("NoSuchApp")
        assert caught.value.code is ErrorCode.APP_NOT_FOUND


@pytest.mark.anyio
async def test_connect_through_the_host(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    ctx = make_test_context({"tools.profile": "standard"})
    async with running_host(ctx, path, pairing_confirmer=AutoConfirmer()):
        async with AsyncDesktop.connect(path, profile="minimal") as mac:
            status = await mac.status()
            assert status["mode"] == "host"
            assert status["profile"] == "minimal"
            assert str(status["client"]).startswith("AXPilot SDK")
            snap = await mac.snapshot("Notes")
            assert snap.find("New Note") is not None
            assert mac.context is None
        with pytest.raises(AXPilotError) as caught:
            async with AsyncDesktop.connect(path, profile="godmode"):
                pass
        assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_connect_without_a_host(sock_dir: Path) -> None:
    with pytest.raises(AXPilotError) as caught:
        Desktop.connect(sock_dir / "missing.sock").open()
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE


def test_sync_connect_through_the_host(sock_dir: Path) -> None:
    path = sock_dir / "axpilot.sock"
    ready = threading.Event()
    stop = threading.Event()

    def host() -> None:
        async def main() -> None:
            async with running_host(make_test_context(), path, pairing_confirmer=AutoConfirmer()):
                ready.set()
                await anyio.to_thread.run_sync(stop.wait)

        anyio.run(main)

    thread = threading.Thread(target=host)
    thread.start()
    try:
        assert ready.wait(10)
        with Desktop.connect(path) as mac:
            assert mac.status()["mode"] == "host"
            assert mac.snapshot("Notes").find("New Note") is not None
    finally:
        stop.set()
        thread.join(10)
