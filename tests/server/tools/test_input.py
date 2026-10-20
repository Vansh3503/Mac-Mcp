"""Input tools: strategy ladders, policy, edge cases, the error matrix and fault injection."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import ErrorCode
from axpilot.models import AppAction, AppInfo, Rect, WindowInfo
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio


async def no_sleep(_: float) -> None:
    await anyio.lowlevel.checkpoint()


def fast(ctx: ServerContext) -> ServerContext:
    ctx.sleep = no_sleep
    return ctx


def demo_ctx(**overrides: Any) -> ServerContext:
    return fast(make_test_context({"tools.profile": "full", **overrides}))


@pytest.fixture
def ctx() -> ServerContext:
    return demo_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def engine_of(ctx: ServerContext) -> FakeEngine:
    engine = ctx.engine
    inner = getattr(engine, "inner", engine)
    assert isinstance(inner, FakeEngine)
    return inner


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args or {})
    return result, error_code_of(result)


async def elements_of(client: Client, app: str) -> list[dict[str, Any]]:
    return structured(await client.call_tool("snapshot", {"app": app}))["elements"]


async def refs_of(client: Client, app: str) -> dict[str, str]:
    """name → ref of the app's latest snapshot (the last element wins for duplicate names)."""
    result, error = await call(client, "snapshot", {"app": app})
    assert error is None, text_of(result)
    return {el["name"]: el["ref"] for el in structured(result)["elements"] if el["name"]}


def events(ctx: ServerContext, kind: str) -> list[dict[str, Any]]:
    return [payload for name, payload in engine_of(ctx).events if name == kind]


# =========================================================================== click
async def test_click_presses_through_accessibility_and_reports_the_change(
    ctx: ServerContext, client: Client
) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "click", {"ref": refs["New Note"]})
    assert error is None
    data = structured(result)
    assert data["strategy"] == "ax:AXPress"
    assert data["attempts"] == ["ax:AXPress"]
    assert data["diff"]["counts"]["added"] >= 1
    assert data["snapshot_id"].startswith("s_")
    assert "via ax:AXPress" in text_of(result)
    assert events(ctx, "perform_action")[-1]["action"] == "AXPress"
    assert not events(ctx, "mouse_click")  # no pointer movement


async def test_click_with_mouse_strategy_clicks_the_element_center(
    ctx: ServerContext, client: Client
) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "click", {"ref": refs["New Note"], "strategy": "mouse"})
    assert error is None
    assert structured(result)["strategy"] == "mouse"
    click = events(ctx, "mouse_click")[-1]
    x, y = click["point"]
    snap = structured(await client.call_tool("snapshot", {"app": "Notes"}))
    new_note = next(el for el in snap["elements"] if el["name"] == "New Note")
    fx, fy, fw, fh = new_note["frame"]
    assert fx <= x <= fx + fw
    assert fy <= y <= fy + fh


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"count": 2}, {"count": 2, "button": "left"}),
        ({"button": "right"}, {"count": 1, "button": "right"}),
        ({"modifiers": ["cmd"]}, {"count": 1, "modifiers": ["cmd"]}),
    ],
)
async def test_clicks_without_an_ax_equivalent_use_the_mouse(
    ctx: ServerContext, client: Client, args: dict[str, Any], expected: dict[str, Any]
) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "click", {"ref": refs["New Note"], **args})
    assert error is None
    assert structured(result)["strategy"] == "mouse"
    click = events(ctx, "mouse_click")[-1]
    for key, value in expected.items():
        assert click[key] == value


async def test_disabled_elements_are_not_clicked(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Finder")
    result, error = await call(client, "click", {"ref": refs["Forward"]})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "disabled" in text_of(result)
    assert not events(ctx, "perform_action")
    assert not events(ctx, "mouse_click")


async def test_ax_only_click_on_an_element_without_actions_fails_clearly(client: Client) -> None:
    result, _ = await call(client, "snapshot", {"app": "Notes", "mode": "full"})
    text = next(el for el in structured(result)["elements"] if el["role"] == "text" and not el["actions"])
    result, error = await call(client, "click", {"ref": text["ref"], "strategy": "ax"})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert result.meta is not None
    assert result.meta["axpilot/error"]["details"]["attempts"] == ["ax"]


async def test_a_covered_element_brings_its_app_forward_before_a_mouse_click(
    ctx: ServerContext, client: Client
) -> None:
    refs = await refs_of(client, "TextEdit")
    engine_of(ctx).app_control("activate", "Notes")  # Notes covers TextEdit now
    result, error = await call(client, "click", {"ref": refs["Bold"], "strategy": "mouse"})
    assert error is None, text_of(result)
    assert any(e.get("action") == "activate" for e in events(ctx, "app_control"))


class LateReorder(FakeEngine):
    """macOS 26: an activated app is frontmost at once, but the window server reorders its windows
    about 200 ms later; until then hit tests still see the covering app, or briefly no window."""

    stale: list[str | None]

    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        info = super().app_control(action, app)
        self.reordering = True
        return info

    def app_at_point(self, point: Any) -> AppInfo | None:
        if getattr(self, "reordering", False) and self.stale:
            name = self.stale.pop(0)
            return self.find_app(name) if name else None
        return super().app_at_point(point)


async def click_covered_bold(stale: list[str | None]) -> tuple[LateReorder, Any, str | None]:
    engine = LateReorder.demo()
    assert isinstance(engine, LateReorder)
    engine.stale = stale
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=engine))
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "TextEdit")
        FakeEngine.app_control(engine, "activate", "Notes")  # Notes covers TextEdit now
        result, error = await call(client, "click", {"ref": refs["Bold"], "strategy": "mouse"})
    return engine, result, error


async def test_a_mouse_click_waits_for_the_windows_to_reorder_after_activating() -> None:
    engine, result, error = await click_covered_bold(["Notes", "Notes", None, "Notes"])
    assert error is None, text_of(result)
    assert not engine.stale  # the passing "no window" mid-reorder did not end the wait
    assert engine.events_of("mouse_click")


async def test_a_window_that_stays_on_top_still_refuses_the_mouse_click() -> None:
    engine, result, error = await click_covered_bold(["Notes"] * 30)
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "covered by Notes" in text_of(result)
    assert not engine.events_of("mouse_click")


class SpaceSwitch(FakeEngine):
    """macOS 26: a full-screen app (Notes here) covers the target; after the target's app is
    activated the hit test names it at once, but the full-screen window stays on the current
    Space for a while, and a click sent then still reaches the full-screen app."""

    leaves_after: int = 0
    activated = False

    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        info = super().app_control(action, app)
        self.activated = True
        return info

    def list_windows(self, app: str | None = None) -> list[WindowInfo]:
        listed = not self.activated or self.leaves_after > 0
        if self.activated and self.leaves_after > 0:
            self.leaves_after -= 1
        screen = Rect(0, 0, 10_000, 10_000)
        return [
            dataclasses.replace(w, fullscreen=True, frame=screen) if w.app == "Notes" else w
            for w in super().list_windows(app)
            if w.app != "Notes" or listed
        ]


async def click_behind_a_full_screen_app(leaves_after: int) -> tuple[SpaceSwitch, Any, str | None]:
    engine = SpaceSwitch.demo()
    assert isinstance(engine, SpaceSwitch)
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=engine))
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "TextEdit")
        FakeEngine.app_control(engine, "activate", "Notes")  # full screen, in front of TextEdit
        engine.leaves_after = leaves_after
        result, error = await call(client, "click", {"ref": refs["Bold"], "strategy": "mouse"})
    return engine, result, error


async def test_a_mouse_click_waits_until_a_full_screen_app_has_left_the_space() -> None:
    engine, result, error = await click_behind_a_full_screen_app(leaves_after=3)
    assert error is None, text_of(result)
    assert engine.leaves_after == 0  # it kept checking while the full-screen window was there
    assert engine.events_of("mouse_click")


async def test_a_full_screen_app_that_stays_refuses_the_mouse_click() -> None:
    engine, result, error = await click_behind_a_full_screen_app(leaves_after=1_000)
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "covered by Notes" in text_of(result)
    assert not engine.events_of("mouse_click")


async def test_coordinate_clicks_are_hit_tested_against_the_policy(
    ctx: ServerContext, client: Client
) -> None:
    engine_of(ctx).app_control("activate", "1Password")
    result, error = await call(client, "click", {"x": 1950, "y": 840})  # inside the 1Password window
    assert error == "BLOCKED_BY_POLICY"
    assert "1Password" in text_of(result)
    assert not events(ctx, "mouse_click")


async def test_coordinate_clicks_cannot_reach_a_deny_listed_window(
    ctx: ServerContext, client: Client
) -> None:
    engine_of(ctx).app_control("activate", "System Settings")
    result, error = await call(client, "click", {"x": 3400, "y": 400})  # Privacy & Security
    assert error == "BLOCKED_BY_POLICY"
    assert "Privacy & Security" in text_of(result)


async def test_date_pickers_take_iso_dates_and_times(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Login Demo")
    data = structured(
        (await call(client, "set_value", {"ref": refs["Trial ends"], "value": "2026-12-24"}))[0]
    )
    assert data["message"].startswith('set to "2026-12-24T')
    stored = engine_of(ctx).events_of("set_value")[-1]["value"]
    assert stored.startswith("2026-12-24T")  # ISO 8601 with the local offset, as engines expect
    data = structured((await call(client, "set_value", {"ref": refs["Reminder time"], "value": "18:45"}))[0])
    assert "T18:45:00" in data["message"]
    result, error = await call(client, "set_value", {"ref": refs["Trial ends"], "value": "next tuesday"})
    assert error == "INVALID_ARGUMENT"
    assert "ISO 8601" in text_of(result)


async def test_column_headers_show_and_change_the_sort(client: Client) -> None:
    headers = {el["name"]: el for el in (await elements_of(client, "Finder")) if el["role"] == "button"}
    assert headers["Name"].get("value") == "ascending"
    assert headers["Date Modified"].get("value") is None
    _, error = await call(client, "click", {"ref": headers["Date Modified"]["ref"]})
    assert error is None
    after = {el["name"]: el for el in (await elements_of(client, "Finder")) if el["role"] == "button"}
    assert after["Date Modified"].get("value") == "descending"
    assert after["Name"].get("value") is None


async def test_color_wells_explain_how_to_change_them(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "TextEdit")
    result, error = await call(client, "set_value", {"ref": refs["Text Color"], "value": "#ff0000"})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "Colors panel" in text_of(result)
    assert not events(ctx, "set_value")


async def test_keys_and_menus_cannot_reach_a_deny_listed_window(ctx: ServerContext, client: Client) -> None:
    for tool, args in (
        ("press_keys", {"keys": "tab space", "app": "System Settings"}),
        ("select_menu", {"path": ["Edit", "Select All"], "app": "System Settings"}),
    ):
        result, error = await call(client, tool, args)
        assert error == "BLOCKED_BY_POLICY", (tool, text_of(result))
        assert "Privacy & Security" in text_of(result)
    assert not events(ctx, "press_key")
    assert not events(ctx, "menu_select")


async def test_keys_are_not_sent_when_the_app_does_not_come_to_the_front(
    ctx: ServerContext, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_of(ctx), "app_control", lambda *args, **kwargs: None)  # activation ignored
    result, error = await call(client, "press_keys", {"keys": "cmd+a", "app": "TextEdit"})
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)
    assert not events(ctx, "press_key")


async def test_screenshot_pixels_map_to_screen_points(ctx: ServerContext, client: Client) -> None:
    shot, _ = await call(client, "screenshot", {"max_edge": 720})
    shot_data = structured(shot)
    result, error = await call(
        client,
        "click",
        {"x": 360, "y": 225, "coordinate_space": "screenshot", "screenshot_id": shot_data["screenshot_id"],
         "report": "none"},
    )  # fmt: skip
    assert error is None, text_of(result)
    x, y = events(ctx, "mouse_click")[-1]["point"]
    assert abs(x - 720) < 2
    assert abs(y - 450) < 2


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e1", "x": 5, "y": 5}, ErrorCode.INVALID_ARGUMENT),
        ({"x": 5}, ErrorCode.INVALID_ARGUMENT),
        ({"x": -9000, "y": 5}, ErrorCode.INVALID_ARGUMENT),
        (
            {"x": 99999, "y": 99999, "coordinate_space": "screenshot", "screenshot_id": "c_zzzz"},
            ErrorCode.SNAPSHOT_NOT_FOUND,
        ),
        ({"ref": "e9999"}, ErrorCode.STALE_REF),
        ({"ref": "e1", "snapshot_id": "s_zzzz"}, ErrorCode.SNAPSHOT_NOT_FOUND),
        ({"ref": "e1", "count": 4}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e1", "button": "thumb"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_click_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await refs_of(client, "Notes")
    result, error = await call(client, "click", args)
    assert error == code.value, text_of(result)


@pytest.mark.parametrize("report", ["none", "snapshot"])
async def test_report_none_and_snapshot(client: Client, report: str) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "click", {"ref": refs["New Note"], "report": report})
    assert error is None
    data = structured(result)
    if report == "none":
        assert data["diff"] is None
        assert data["snapshot"] is None
    else:
        assert data["snapshot"]["snapshot_id"] == data["snapshot_id"]


async def test_kill_switch_and_active_user_stop_clicks(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Notes")
    engine_of(ctx).hid_idle = 0.1
    _, active = await call(client, "click", {"ref": refs["New Note"]})
    engine_of(ctx).hid_idle = 999
    ctx.control.pause("kill switch")
    _, paused = await call(client, "click", {"ref": refs["New Note"]})
    assert active == "USER_ACTIVE"
    assert paused == "PAUSED_BY_USER"
    assert not events(ctx, "perform_action")


@pytest.mark.parametrize(
    ("method", "fault", "code"),
    [
        ("perform_action", Fault.crash("boom /Users/me"), "INTERNAL"),
        ("perform_action", Fault.hang(10), "TIMEOUT"),
        ("perform_action", Fault.error(ErrorCode.STALE_REF, "gone"), "STALE_REF"),
    ],
)
async def test_engine_faults_during_a_click_release_the_input_lock(
    method: str, fault: Fault, code: str
) -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    ctx.engine_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            refs = await refs_of(client, "Notes")
            faulty.set_fault(method, fault)
            result, error = await call(client, "click", {"ref": refs["New Note"], "strategy": "ax"})
            assert error == code, text_of(result)
            assert "/Users/me" not in text_of(result)
            assert not ctx.control.busy
            faulty.clear_faults()
            _, again = await call(client, "click", {"ref": refs["New Note"]})
            assert again is None
    finally:
        faulty.release()


async def test_a_timed_out_ax_press_is_not_followed_by_a_mouse_click() -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    ctx.engine_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            refs = await refs_of(client, "Notes")
            faulty.set_fault("perform_action", Fault.hang(10))
            result, error = await call(client, "click", {"ref": refs["New Note"]})
        assert error == "TIMEOUT"
        assert "may still have happened" in text_of(result)
        assert faulty.calls["mouse_click"] == 0  # the press may land late: a click would press twice
        assert not ctx.control.busy
    finally:
        faulty.release()


async def test_a_failed_ax_press_falls_back_to_the_mouse_once() -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Notes")
        faulty.set_fault("perform_action", Fault.error(ErrorCode.ELEMENT_NOT_ACTIONABLE, "AX said no"))
        result, error = await call(client, "click", {"ref": refs["New Note"]})
    assert error is None
    data = structured(result)
    assert data["attempts"] == ["ax:AXPress", "mouse"]
    assert data["strategy"] == "mouse"
    assert faulty.calls["perform_action"] == 1  # never retried
    assert faulty.calls["mouse_click"] == 1


# =========================================================================== type_text
async def test_typing_into_a_native_field_sets_the_text_through_accessibility(
    ctx: ServerContext, client: Client
) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(
        client, "type_text", {"ref": refs["Note body"], "text": "Groceries", "clear": True}
    )
    assert error is None
    assert structured(result)["strategy"] == "ax_value"
    assert not events(ctx, "type_text")
    body = next(el for el in structured(await client.call_tool("snapshot", {"app": "Notes"}))["elements"]
                if el["name"] == "Note body")  # fmt: skip
    assert body["value"] == "Groceries"


async def test_typing_into_web_views_uses_keystrokes(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).app_control("launch", "Slack")
    refs = await refs_of(client, "Slack")
    result, error = await call(client, "type_text", {"ref": refs["Message #general"], "text": "hello team"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["strategy"] == "keystrokes"
    assert events(ctx, "type_text")[-1]["text"] == "hello team"


async def test_keystrokes_into_a_background_app_bring_it_to_the_front_first(
    ctx: ServerContext, client: Client
) -> None:
    # Keystrokes go to the frontmost app, whatever has accessibility focus elsewhere: typing into
    # Login Demo while Finder is in front must activate Login Demo before the first key.
    engine = engine_of(ctx)

    def front() -> str | None:
        app = engine.frontmost_app()
        return app.name if app is not None else None

    assert front() == "Finder"
    refs = await refs_of(client, "Login Demo")
    result, error = await call(client, "type_text", {"ref": refs["Email"], "text": "alex@example.com"})
    assert error is None, text_of(result)
    assert structured(result)["strategy"] == "keystrokes"
    assert front() == "Login Demo"
    kinds = [kind for kind, _ in engine.events]
    assert kinds.index("app_control") < kinds.index("type_text")
    assert events(ctx, "type_text")[-1]["target"] == "login.email"
    fields = structured(await client.call_tool("snapshot", {"app": "Login Demo"}))["elements"]
    assert (
        next(el for el in fields if el["name"] == "Email" and el["role"] == "field")["value"]
        == "alex@example.com"
    )


class IgnoresActivation(FakeEngine):
    """macOS may ignore an activation request from a process in the background."""

    def app_control(self, action: AppAction, app: str) -> AppInfo | None:
        if action == "activate":
            return None
        return super().app_control(action, app)


async def test_nothing_is_typed_when_the_target_app_stays_in_the_background() -> None:
    # The keystrokes would go to the app in front (Finder), not to the field the policy checked.
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=IgnoresActivation.demo()))
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Login Demo")
        result, error = await call(client, "type_text", {"ref": refs["Email"], "text": "alex@example.com"})
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)
    assert "Could not bring Login Demo to the front" in text_of(result)
    assert not events(ctx, "type_text")
    assert not events(ctx, "press_key")
    front = engine_of(ctx).frontmost_app()
    assert front is not None
    assert front.name == "Finder"


async def test_long_text_is_pasted_and_the_clipboard_is_restored(ctx: ServerContext, client: Client) -> None:
    engine = engine_of(ctx)
    engine.clipboard_set(text="keep me")
    engine.app_control("launch", "Slack")
    refs = await refs_of(client, "Slack")
    long_text = "lorem ipsum " * 40
    result, error = await call(client, "type_text", {"ref": refs["Message #general"], "text": long_text})
    assert error is None, text_of(result)
    assert structured(result)["strategy"] == "paste"
    assert engine.clipboard_get().text == "keep me"


async def test_the_clipboard_is_restored_even_when_pasting_fails() -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    faulty.inner.clipboard_set(text="keep me")
    faulty.inner.app_control("launch", "Slack")
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Slack")
        faulty.set_fault("press_key", Fault.crash())
        _, error = await call(client, "type_text", {"ref": refs["Message #general"], "text": "x" * 500})
    assert error == "INTERNAL"
    assert faulty.inner.clipboard_get().text == "keep me"


async def test_a_clipboard_that_cannot_be_restored_is_a_warning_not_silence() -> None:
    """On a Mac a restore can fail (it used to, for a copied file that was deleted since);
    the paste itself worked, so the result says so and warns about the clipboard."""
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=faulty))
    faulty.inner.app_control("launch", "Slack")
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Slack")
        faulty.set_fault("clipboard_restore", Fault.error(ErrorCode.INTERNAL, "macOS refused"))
        result, error = await call(client, "type_text", {"ref": refs["Message #general"], "text": "x" * 500})
        assert error is None, text_of(result)
        assert structured(result)["strategy"] == "paste"
        assert any("clipboard could not be restored" in w for w in structured(result)["warnings"])
        faulty.clear_faults()
        _, error = await call(client, "type_text", {"ref": refs["Message #general"], "text": "y" * 500})
    assert error is None  # the server and the input lock are fine


async def test_submit_presses_return(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "type_text", {"ref": refs["Search"], "text": "milk", "submit": True})
    assert error is None
    assert "pressed Return" in text_of(result)
    assert events(ctx, "press_key")[-1]["key"] == "return"


async def test_password_fields_are_off_limits_by_default(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Login Demo")
    result, error = await call(client, "type_text", {"ref": refs["Password"], "text": "hunter2"})
    assert error == "BLOCKED_BY_POLICY"
    assert "allow_secure_fields" in text_of(result)
    assert not events(ctx, "set_value")
    assert not events(ctx, "type_text")


async def test_keystrokes_without_a_ref_check_the_focused_field() -> None:
    """Typing "wherever the focus is" must not get around the secure-field rule."""
    ctx = demo_ctx(**{"tools.enable": ["computer"]})
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Login Demo")
        _, password = ctx.snapshots.resolve(refs["Password"], None)
        engine_of(ctx).app_control("activate", "Login Demo")
        engine_of(ctx).focus(password.handle)
        focused = structured(await client.call_tool("snapshot", {"app": "Login Demo"}))
        assert focused["focused_ref"] == refs["Password"]
        for tool, args in (
            ("type_text", {"text": "hunter2"}),
            ("press_keys", {"keys": "h u n t e r 2"}),
            ("computer", {"action": "type", "text": "hunter2"}),
            ("computer", {"action": "key", "text": "shift+h"}),
        ):
            result, error = await call(client, tool, args)
            assert error == "BLOCKED_BY_POLICY", (tool, args, text_of(result))
        assert not events(ctx, "type_text")
        _, error = await call(client, "press_keys", {"keys": "tab"})  # moving on is fine
        assert error is None
    allowed = demo_ctx(**{"safety.allow_secure_fields": True})
    async with connected_client(allowed) as client:
        refs = await refs_of(client, "Login Demo")
        _, password = allowed.snapshots.resolve(refs["Password"], None)
        engine_of(allowed).app_control("activate", "Login Demo")
        engine_of(allowed).focus(password.handle)
        _, error = await call(client, "type_text", {"text": "hunter2"})
        assert error is None


async def test_password_fields_can_be_allowed_and_the_text_never_reaches_the_audit_log() -> None:
    ctx = demo_ctx(**{"safety.allow_secure_fields": True})
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Login Demo")
        _, error = await call(
            client, "type_text", {"ref": refs["Password"], "text": "hunter2", "clear": True}
        )
    assert error is None
    log = json.dumps(ctx.audit.tail(50))
    assert "hunter2" not in log
    assert "<redacted 7 chars>" in log


async def test_secure_keyboard_entry_blocks_keystrokes_but_not_accessibility(
    ctx: ServerContext, client: Client
) -> None:
    engine = engine_of(ctx)
    refs = await refs_of(client, "Notes")
    engine.secure_input = True
    native, native_error = await call(client, "type_text", {"ref": refs["Note body"], "text": "ok"})
    keys, keys_error = await call(client, "type_text", {"text": "x", "strategy": "keystrokes"})
    assert native_error is None, text_of(native)
    assert keys_error == "SECURE_INPUT_ACTIVE"
    assert "password" in text_of(keys).lower()


async def test_typing_into_a_terminal_needs_the_shell_tool() -> None:
    blocked = demo_ctx(**{"tools.profile": "standard"})
    engine_of(blocked).app_control("activate", "Terminal")
    async with connected_client(blocked) as client:
        result, error = await call(client, "type_text", {"text": "rm -rf ~"})
    assert error == "BLOCKED_BY_POLICY"
    assert "Terminal" in text_of(result)
    assert not [name for name, _ in engine_of(blocked).events if name in {"type_text", "press_key"}]
    allowed = demo_ctx(**{"tools.profile": "standard", "tools.enable": ["shell"]})
    engine_of(allowed).app_control("activate", "Terminal")
    async with connected_client(allowed) as other:
        _, ok_error = await call(other, "type_text", {"text": "ls"})
    assert ok_error is None


async def test_typing_into_something_that_is_not_a_text_field_warns(client: Client) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "type_text", {"ref": refs["New Note"], "text": "x"})
    assert error is None
    assert any("not a text field" in w for w in structured(result)["warnings"])


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"text": ""}, ErrorCode.INVALID_ARGUMENT),
        ({"text": "x" * 20_001}, ErrorCode.INVALID_ARGUMENT),
        ({"text": "x", "ref": "e9999"}, ErrorCode.STALE_REF),
        ({"text": "x", "strategy": "telepathy"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_type_text_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await refs_of(client, "Notes")
    result, error = await call(client, "type_text", args)
    assert error == code.value, text_of(result)


async def test_clearing_a_field_with_empty_text(client: Client) -> None:
    refs = await refs_of(client, "Notes")
    result, error = await call(client, "type_text", {"ref": refs["Note body"], "text": "", "clear": True})
    assert error is None
    assert "cleared" in text_of(result)


# =========================================================================== press_keys
async def test_press_keys_sequences_and_repeats(ctx: ServerContext, client: Client) -> None:
    result, error = await call(client, "press_keys", {"keys": "cmd+a delete", "repeat": 2, "app": "Notes"})
    assert error is None, text_of(result)
    pressed = [(e["key"], tuple(e["modifiers"])) for e in events(ctx, "press_key")]
    assert pressed == [("a", ("cmd",)), ("delete", ()), ("a", ("cmd",)), ("delete", ())]
    assert "×2" in text_of(result)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"keys": "cmd+banana"}, ErrorCode.INVALID_ARGUMENT),
        ({"keys": ""}, ErrorCode.INVALID_ARGUMENT),
        ({"keys": "tab", "repeat": 51}, ErrorCode.INVALID_ARGUMENT),
        ({"keys": "tab", "app": "Nopes"}, ErrorCode.APP_NOT_FOUND),
        ({"keys": "cmd+q", "app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
    ],
)
async def test_press_keys_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "press_keys", args)
    assert error == code.value, text_of(result)


async def test_press_keys_refuses_while_secure_input_is_on(ctx: ServerContext, client: Client) -> None:
    engine_of(ctx).secure_input = True
    _, error = await call(client, "press_keys", {"keys": "cmd+v", "app": "Notes"})
    assert error == "SECURE_INPUT_ACTIVE"


# =========================================================================== select_menu
async def test_listing_menus(client: Client) -> None:
    result, error = await call(client, "select_menu", {"app": "TextEdit", "depth": 2})
    assert error is None
    menus = structured(result)["menus"]
    titles = [menu["title"] for menu in menus]
    assert "Format" in titles
    assert any(menu["children"] for menu in menus)


async def test_choosing_a_nested_menu_item(ctx: ServerContext, client: Client) -> None:
    result, error = await call(client, "select_menu", {"app": "TextEdit", "path": ["format", "font", "bold"]})
    assert error is None, text_of(result)
    assert structured(result)["selected"] == ["Format", "Font", "Bold"]
    assert events(ctx, "menu_select")


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"app": "TextEdit", "path": ["Format", "Nonexistent"]}, ErrorCode.MENU_NOT_FOUND),
        ({"app": "TextEdit", "path": ["Edit", "Redo"]}, ErrorCode.ELEMENT_NOT_ACTIONABLE),
        ({"app": "TextEdit", "path": ["Format", " "]}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "1Password", "path": ["File"]}, ErrorCode.BLOCKED_BY_POLICY),
        ({"app": "Nopes", "path": ["File"]}, ErrorCode.APP_NOT_FOUND),
    ],
)
async def test_select_menu_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "select_menu", args)
    assert error == code.value, text_of(result)


async def test_menu_not_found_lists_what_is_available(client: Client) -> None:
    result, _ = await call(client, "select_menu", {"app": "TextEdit", "path": ["Format", "Nonexistent"]})
    assert "Font" in text_of(result)


async def test_menu_bar_extras(ctx: ServerContext, client: Client) -> None:
    listed, error = await call(client, "select_menu", {"extras": True})  # Control Center by default
    assert error is None, text_of(listed)
    data = structured(listed)
    assert data["app"] == "Control Center"
    assert [menu["title"] for menu in data["menus"]] == ["Wi‑Fi", "Battery", "Clock"]
    chosen, error = await call(client, "select_menu", {"extras": True, "path": ["wi-fi", "cafe guest"]})
    assert error == "MENU_NOT_FOUND"  # "é" is a different letter: nothing is guessed
    chosen, error = await call(client, "select_menu", {"extras": True, "path": ["wi-fi", "Café"]})
    assert error is None, text_of(chosen)
    assert structured(chosen)["selected"] == ["Wi‑Fi", "Café Guest"]
    opened, error = await call(client, "select_menu", {"extras": True, "path": ["Battery"]})
    assert error is None, text_of(opened)  # a status item alone is clicked (its menu or panel opens)
    assert events(ctx, "menu_select")[-1] == {
        "app": "com.apple.controlcenter",
        "path": ["Battery"],
        "via": "extras",
    }


async def test_menu_bar_extras_follow_the_policy_and_need_status_items(client: Client) -> None:
    for args in (
        {"extras": True, "app": "1Password"},
        {"extras": True, "app": "1Password", "path": ["1Password", "Lock"]},
    ):
        result, error = await call(client, "select_menu", args)
        assert error == "BLOCKED_BY_POLICY", text_of(result)
    result, error = await call(client, "select_menu", {"extras": True, "app": "Notes"})
    assert error == "MENU_NOT_FOUND"
    assert "Control Center" in text_of(result)


# =========================================================================== set_value
async def test_checkboxes_are_set_idempotently(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "TextEdit")
    on, error = await call(client, "set_value", {"ref": refs["Bold"], "value": "true"})
    assert error is None
    assert structured(on)["message"] == "turned on"
    presses = len(events(ctx, "perform_action"))
    again, _ = await call(client, "set_value", {"ref": refs["Bold"], "value": "on"})
    assert structured(again)["message"] == "already on"
    assert len(events(ctx, "perform_action")) == presses  # nothing pressed the second time
    off, _ = await call(client, "set_value", {"ref": refs["Bold"], "value": "false"})
    assert structured(off)["message"] == "turned off"


async def test_radio_buttons_cannot_be_turned_off_directly(client: Client) -> None:
    refs = await refs_of(client, "TextEdit")
    result, error = await call(client, "set_value", {"ref": refs["Align Left"], "value": "false"})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "another option" in text_of(result)
    _, chosen = await call(client, "set_value", {"ref": refs["Center"], "value": "true"})
    assert chosen is None


async def test_pop_up_buttons_choose_an_option(client: Client) -> None:
    refs = await refs_of(client, "TextEdit")
    result, error = await call(client, "set_value", {"ref": refs["Paragraph Styles"], "value": "Title"})
    assert error is None, text_of(result)
    missing, missing_error = await call(
        client, "set_value", {"ref": refs["Paragraph Styles"], "value": "Poster"}
    )
    assert missing_error in ("ELEMENT_NOT_ACTIONABLE", "INVALID_ARGUMENT"), text_of(missing)


async def test_text_fields_combo_boxes_and_secure_fields(client: Client) -> None:
    refs = await refs_of(client, "Login Demo")
    _, email_error = await call(client, "set_value", {"ref": refs["Email"], "value": "ada@example.com"})
    _, secret_error = await call(client, "set_value", {"ref": refs["Password"], "value": "x"})
    assert email_error is None
    assert secret_error == "BLOCKED_BY_POLICY"
    refs = await refs_of(client, "TextEdit")
    size, size_error = await call(client, "set_value", {"ref": refs["Font Size"], "value": "14"})
    assert size_error is None
    assert "14" in text_of(size)


@pytest.mark.parametrize(
    ("value", "code"),
    [("maybe", ErrorCode.INVALID_ARGUMENT), ("", ErrorCode.INVALID_ARGUMENT)],
)
async def test_set_value_rejects_values_of_the_wrong_kind(
    client: Client, value: str, code: ErrorCode
) -> None:
    refs = await refs_of(client, "TextEdit")
    result, error = await call(client, "set_value", {"ref": refs["Bold"], "value": value})
    assert error == code.value, text_of(result)


async def test_set_value_needs_an_accessibility_element(client: Client) -> None:
    snap, _ = await call(client, "snapshot", {"app": "Notes", "mode": "vision"})
    ocr_ref = next(el["ref"] for el in structured(snap)["elements"] if el["ref"].startswith("o"))
    result, error = await call(client, "set_value", {"ref": ocr_ref, "value": "x"})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "OCR" in text_of(result)


# ----------------------------------------------------------- sliders, steppers, disclosure
CONTROLS = {
    "os_version": "15.4",
    "apps": [{"name": "Controls", "bundle_id": "com.example.Controls", "pid": 4242, "frontmost": True}],
    "windows": [
        {
            "window_id": 4201,
            "app": "com.example.Controls",
            "title": "Controls",
            "frame": [100, 100, 600, 400],
            "tree": {
                "role": "AXWindow",
                "title": "Controls",
                "frame": [100, 100, 600, 400],
                "children": [
                    {"role": "AXSlider", "description": "Volume", "value": 50, "min": 0, "max": 100,
                     "frame": [120, 140, 200, 20], "actions": ["AXIncrement", "AXDecrement"]},
                    {"role": "AXIncrementor", "description": "Copies", "value": 1, "min": 1, "max": 9,
                     "step": 1, "settable": False, "frame": [120, 180, 40, 22],
                     "actions": ["AXIncrement", "AXDecrement"]},
                    {"role": "AXCheckBox", "subrole": "AXSwitch", "description": "Wi-Fi", "value": 1,
                     "frame": [120, 220, 40, 22], "actions": ["AXPress"]},
                    {"role": "AXCheckBox", "description": "Locked", "value": 0, "enabled": False,
                     "frame": [120, 260, 80, 22], "actions": ["AXPress"]},
                ],
            },
        }
    ],
}  # fmt: skip


@pytest.fixture
async def controls() -> AsyncIterator[tuple[ServerContext, Client, dict[str, str]]]:
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=FakeEngine.from_dict(CONTROLS)))
    async with connected_client(ctx) as client:
        yield ctx, client, await refs_of(client, "Controls")


async def test_sliders_are_clamped_to_their_range(
    controls: tuple[ServerContext, Client, dict[str, str]],
) -> None:
    _, client, refs = controls
    result, error = await call(client, "set_value", {"ref": refs["Volume"], "value": "150"})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["message"] == "set to 100"
    assert any("outside the range" in w for w in data["warnings"])
    _, bad = await call(client, "set_value", {"ref": refs["Volume"], "value": "loud"})
    assert bad == "INVALID_ARGUMENT"


class IgnoresSliderValues(FakeEngine):
    """Safari (macOS 26): a range input accepts an AXValue without moving; AXIncrement works."""

    def set_value(self, handle: Any, value: str | float | bool) -> None:
        if self.refresh_element(handle).role == "AXSlider":
            return
        super().set_value(handle, value)


async def test_a_slider_that_ignores_its_value_is_stepped_instead() -> None:
    engine = IgnoresSliderValues.from_dict(CONTROLS)
    ctx = fast(make_test_context({"tools.profile": "full"}, engine=engine))
    async with connected_client(ctx) as client:
        refs = await refs_of(client, "Controls")
        result, error = await call(client, "set_value", {"ref": refs["Volume"], "value": "53"})
        assert error is None, text_of(result)
        assert structured(result)["strategy"] == "ax:step"  # not "ax_value", which changed nothing
        snapshot, _ = await call(client, "snapshot", {"app": "Controls"})
    volume = next(el for el in structured(snapshot)["elements"] if el["name"] == "Volume")
    assert volume["value"] == "53"


async def test_read_only_steppers_are_stepped_to_the_value(
    controls: tuple[ServerContext, Client, dict[str, str]],
) -> None:
    ctx, client, refs = controls
    result, error = await call(client, "set_value", {"ref": refs["Copies"], "value": "4"})
    assert error is None, text_of(result)
    assert structured(result)["strategy"] == "ax:step"
    assert len([e for e in events(ctx, "perform_action") if e["action"] == "AXIncrement"]) == 3


async def test_switches_and_disabled_controls(controls: tuple[ServerContext, Client, dict[str, str]]) -> None:
    _, client, refs = controls
    already, _ = await call(client, "set_value", {"ref": refs["Wi-Fi"], "value": "true"})
    assert structured(already)["message"] == "already on"
    locked, locked_error = await call(client, "set_value", {"ref": refs["Locked"], "value": "true"})
    assert locked_error == "ELEMENT_NOT_ACTIONABLE"
    assert "disabled" in text_of(locked)


# =========================================================================== perform_action / fill_form
async def test_perform_action(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Notes")
    _, error = await call(client, "perform_action", {"ref": refs["New Note"], "action": "AXPress"})
    unsupported, unsupported_error = await call(
        client, "perform_action", {"ref": refs["New Note"], "action": "AXShowMenu"}
    )
    _, invalid = await call(client, "perform_action", {"ref": refs["New Note"], "action": "rm -rf"})
    assert error is None
    assert unsupported_error == "ELEMENT_NOT_ACTIONABLE"
    assert "AXPress" in text_of(unsupported)
    assert invalid == "INVALID_ARGUMENT"


async def test_fill_form_fills_fields_in_order(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Login Demo")
    result, error = await call(
        client,
        "fill_form",
        {
            "fields": [
                {"ref": refs["Email"], "value": "ada@example.com"},
                {"ref": refs["Remember me"], "value": "true"},
            ]
        },
    )
    assert error is None, text_of(result)
    assert "filled 2 fields" in text_of(result)


async def test_fill_form_checks_every_ref_before_changing_anything(
    ctx: ServerContext, client: Client
) -> None:
    refs = await refs_of(client, "Login Demo")
    before = len(engine_of(ctx).events)
    result, error = await call(
        client,
        "fill_form",
        {"fields": [{"ref": refs["Email"], "value": "a"}, {"ref": "e9999", "value": "b"}]},
    )
    assert error == "STALE_REF", text_of(result)
    assert [e for e in engine_of(ctx).events[before:] if e[0] in ("set_value", "perform_action")] == []
    _, secure = await call(
        client,
        "fill_form",
        {"fields": [{"ref": refs["Email"], "value": "a"}, {"ref": refs["Password"], "value": "b"}]},
    )
    assert secure == "BLOCKED_BY_POLICY"
    assert [e for e in engine_of(ctx).events[before:] if e[0] == "set_value"] == []


async def test_fill_form_reports_progress_when_a_field_fails(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "TextEdit")
    result, error = await call(
        client,
        "fill_form",
        {"fields": [{"ref": refs["Bold"], "value": "true"}, {"ref": refs["Italic"], "value": "maybe"}]},
    )
    assert error == "INVALID_ARGUMENT"
    assert result.meta is not None
    details = result.meta["axpilot/error"]["details"]
    assert details["completed"] == [refs["Bold"]]
    assert details["failed"] == refs["Italic"]
    assert "Filled 1 of 2" in text_of(result)


# =========================================================================== drag / move_mouse
async def test_drag_between_points_and_policy_on_both_ends(ctx: ServerContext, client: Client) -> None:
    _, error = await call(
        client, "drag", {"from_x": 300, "from_y": 300, "to_x": 500, "to_y": 400, "duration_ms": 0}
    )
    assert error is None
    assert events(ctx, "mouse_drag")
    engine_of(ctx).app_control("activate", "1Password")
    result, blocked = await call(client, "drag", {"from_x": 300, "from_y": 300, "to_x": 1950, "to_y": 840})
    assert blocked == "BLOCKED_BY_POLICY", text_of(result)


async def test_fill_form_types_into_web_text_fields(ctx: ServerContext, client: Client) -> None:
    """Safari (macOS 26) took an AXValue change of a form field without changing the page:
    fill_form said "filled" and the form was sent empty. Browsers get keystrokes, like type_text."""
    engine_of(ctx).app_control("launch", "Google Chrome")
    refs = await refs_of(client, "Google Chrome")
    fields = [{"ref": refs["Address and search bar"], "value": "example.com"}]
    result, error = await call(client, "fill_form", {"fields": fields})
    assert error is None, text_of(result)
    assert structured(result)["warnings"] == []
    assert [e["text"] for e in events(ctx, "type_text")] == ["example.com"]
    assert not [e for e in events(ctx, "set_value") if e.get("value") == "example.com"]


async def test_a_dragged_list_row_is_grabbed_by_its_text(ctx: ServerContext, client: Client) -> None:
    """Finder (macOS 26) drags a file only from its name or icon: the row's center (another
    column) started a selection rectangle instead, and the file stayed put."""
    engine_of(ctx).app_control("launch", "com.apple.mail")
    result, error = await call(client, "snapshot", {"app": "Mail", "mode": "full"})
    assert error is None, text_of(result)
    elements = structured(result)["elements"]
    row = next(el for el in elements if el["role"] == "row" and el["name"].startswith("Alice Chen"))
    assert row["center"] == [432.5, 91.0]  # the middle column (subject)
    _, error = await call(
        client, "drag", {"from_ref": row["ref"], "to_x": 300, "to_y": 300, "duration_ms": 0}
    )
    assert error is None
    assert events(ctx, "mouse_drag")[-1]["start"] == [277.5, 91.0]  # the sender, the first text


async def test_move_mouse_hovers_without_clicking(ctx: ServerContext, client: Client) -> None:
    refs = await refs_of(client, "Notes")
    _, error = await call(client, "move_mouse", {"ref": refs["New Note"]})
    assert error is None
    assert events(ctx, "mouse_move")
    assert not events(ctx, "mouse_click")
