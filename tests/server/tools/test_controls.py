"""``select_option`` (pop-ups, combo boxes, radio and tab groups, lists) and ``handle_dialog``
(sheets and dialog windows: inspect, accept, cancel, press, fill), against the fake desktop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools.controls import DESTRUCTIVE_BUTTON_RE, match_option
from axpilot.testing import connected_client, make_test_context, structured

pytestmark = pytest.mark.anyio


class Recorder:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.requests: list[ConfirmRequest] = []

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.requests.append(request)
        return self.answer


def make_ctx(*, engine: Any = None, confirm: Any = True, **overrides: Any) -> ServerContext:
    return make_test_context({"tools.profile": "standard", **overrides}, engine=engine, confirm=confirm)


@pytest.fixture
def ctx() -> ServerContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


async def call(client: Client, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args)
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


async def elements(client: Client, app: str, mode: str = "interactive") -> dict[str, dict[str, Any]]:
    await ok(client, "app", {"action": "activate", "name": app})
    snap = await ok(client, "snapshot", {"app": app, "mode": mode})
    return {f"{el['role']}:{el.get('name')}": el for el in snap["elements"]}


def engine_of(ctx: ServerContext) -> FakeEngine:
    assert isinstance(ctx.engine, FakeEngine)
    return ctx.engine


# =========================================================================== matching
@pytest.mark.parametrize(
    ("option", "index"),
    [("Center", 1), ("center", 1), ("cent", 1), ("Right", 3), ("Align Right…", 3)],
)
def test_match_option(option: str, index: int) -> None:
    assert match_option(["Align Left", "Center", "Justify", "Align Right"], option) == index


@pytest.mark.parametrize("option", ["Align", "Zebra"])
def test_match_option_errors(option: str) -> None:
    with pytest.raises(AXPilotError) as caught:
        match_option(["Align Left", "Center", "Align Right"], option)
    assert "Align Left" in caught.value.message


# =========================================================================== select_option
async def test_pop_ups_and_combo_boxes(client: Client) -> None:
    els = await elements(client, "TextEdit")
    style = els["popup:Paragraph Styles"]["ref"]
    data = await ok(client, "select_option", {"ref": style, "option": "heading"})
    assert data["strategy"] == "ax_value"
    after = await elements(client, "TextEdit")
    assert after["popup:Paragraph Styles"]["value"] == "Heading"
    _, error = await call(client, "select_option", {"ref": style, "option": "Nope"})
    assert error == "INVALID_ARGUMENT"
    _, error = await call(client, "select_option", {"ref": style, "index": 2})
    assert error == "INVALID_ARGUMENT"  # pop-ups list their items only while open
    size = after["combobox:Font Size"]["ref"]
    await ok(client, "select_option", {"ref": size, "option": "18"})
    assert (await elements(client, "TextEdit"))["combobox:Font Size"]["value"] == "18"


async def test_radio_groups_by_title_or_index(ctx: ServerContext, client: Client) -> None:
    els = await elements(client, "TextEdit")
    group = els["radiogroup:Text Alignment"]["ref"]
    data = await ok(client, "select_option", {"ref": group, "option": "center"})
    assert data["target"]["name"] == "Center"
    assert "checked" in (await elements(client, "TextEdit"))["radio:Center"]["states"]
    again = await ok(client, "select_option", {"ref": group, "option": "Center"})
    assert again["strategy"] is None  # already selected: nothing pressed
    await ok(client, "select_option", {"ref": group, "index": 4})
    assert "checked" in (await elements(client, "TextEdit"))["radio:Align Right"]["states"]
    for args in (
        {"ref": group, "index": 9},
        {"ref": group, "option": "Align"},  # ambiguous
        {"ref": group},
        {"ref": group, "option": "Center", "index": 1},
    ):
        _, error = await call(client, "select_option", args)
        assert error == "INVALID_ARGUMENT", args


async def test_an_option_element_itself_is_selected(client: Client) -> None:
    els = await elements(client, "Finder")
    as_list = els["radio:as List"]["ref"]
    await ok(client, "select_option", {"ref": as_list, "option": "ignored"})
    assert "checked" in (await elements(client, "Finder"))["radio:as List"]["states"]


async def test_list_rows(client: Client) -> None:
    els = await elements(client, "Notes")
    notes = next(el for key, el in els.items() if key.startswith("list:"))
    data = await ok(client, "select_option", {"ref": notes["ref"], "option": "trip"})
    assert data["target"]["name"].startswith("Trip ideas")
    assert (
        "selected"
        in next(el for key, el in (await elements(client, "Notes")).items() if key.startswith("row:Trip"))[
            "states"
        ]
    )


async def test_controls_without_options(client: Client) -> None:
    els = await elements(client, "Notes")
    _, error = await call(client, "select_option", {"ref": els["menubutton:Share"]["ref"], "option": "Mail"})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    _, error = await call(client, "select_option", {"ref": els["button:New Note"]["ref"], "option": "x"})
    assert error == "ELEMENT_NOT_ACTIONABLE"


async def test_select_option_follows_the_policy() -> None:
    ctx = make_ctx(**{"safety.deny_apps": ["com.apple.TextEdit"]})
    async with connected_client(ctx) as client:
        await ok(client, "app", {"action": "activate", "name": "Notes"})
        snap = await ok(client, "snapshot", {"app": "Notes"})
        ref = next(el["ref"] for el in snap["elements"] if el["role"] == "list")
        ctx.config.safety.deny_apps.append("com.apple.Notes")
        _, error = await call(client, "select_option", {"ref": ref, "option": "Trip"})
    assert error == "BLOCKED_BY_POLICY"


# =========================================================================== handle_dialog
async def test_inspect_a_sheet(client: Client) -> None:
    data = await ok(client, "handle_dialog", {"app": "Login Demo"})
    assert data["found"] is True
    assert data["kind"] == "sheet"
    assert "Sign in to Login Demo" in data["texts"]
    assert [b["name"] for b in data["buttons"]] == ["Cancel", "Sign In"]
    assert {f["role"] for f in data["fields"]} >= {"field", "secure", "checkbox"}
    none = await ok(client, "handle_dialog", {"app": "Notes"})
    assert none["found"] is False
    _, error = await call(client, "handle_dialog", {"app": "Notes", "action": "accept"})
    assert error == "ELEMENT_NOT_ACTIONABLE"


async def test_fill_and_accept(ctx: ServerContext, client: Client) -> None:
    await ok(client, "app", {"action": "activate", "name": "Login Demo"})
    data = await ok(
        client, "handle_dialog", {"app": "Login Demo", "action": "accept", "text": "ada@example.com"}
    )
    assert data["pressed"] == "Sign In"
    pressed = engine_of(ctx).events_of("press")
    assert pressed[-1]["name"] == "Sign In"
    assert any(e.get("value") == "ada@example.com" for e in engine_of(ctx).events_of("set_value"))


async def test_cancel_and_press(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "handle_dialog", {"app": "Login Demo", "action": "cancel"})
    assert data["pressed"] == "Cancel"
    assert (await ok(client, "handle_dialog", {"app": "Login Demo"}))["found"] is False


async def test_press_by_title(client: Client) -> None:
    _, error = await call(client, "handle_dialog", {"app": "Login Demo", "action": "press", "button": "Help"})
    assert error == "INVALID_ARGUMENT"
    data = await ok(client, "handle_dialog", {"app": "Login Demo", "action": "press", "button": "sign"})
    assert data["pressed"] == "Sign In"
    for args in ({"action": "press"}, {"button": "OK"}, {"text": "x"}):
        _, error = await call(client, "handle_dialog", {"app": "Login Demo", **args})
        assert error == "INVALID_ARGUMENT", args


def alert_desktop(tmp_path: Path, buttons: list[str], *, window_dialog: bool = False) -> FakeEngine:
    """One app showing an alert: a sheet, or (``window_dialog``) a dialog window."""
    contents = [
        {"role": "AXStaticText", "value": "Do you want to keep the changes?", "frame": [120, 120, 300, 20]},
        *(
            {
                "id": f"alert.{n}",
                "role": "AXButton",
                "title": title,
                "frame": [100 + 90 * n, 200, 80, 24],
                "actions": ["AXPress"],
                "on_press": {"close_window": True},
            }
            for n, title in enumerate(buttons)
        ),
    ]
    tree: dict[str, Any] = {"role": "AXWindow", "subrole": "AXStandardWindow", "children": []}
    if window_dialog:
        tree = {"role": "AXWindow", "subrole": "AXDialog", "children": contents}
    else:
        tree["children"] = [{"role": "AXSheet", "frame": [100, 100, 400, 140], "children": contents}]
    data = {
        "displays": [{"display_id": 1, "frame": [0, 0, 1440, 900], "work_area": [0, 25, 1440, 875],
                      "scale": 2.0, "main": True, "name": "Display"}],
        "apps": [{"name": "Editor", "bundle_id": "com.example.Editor", "pid": 500, "frontmost": True}],
        "windows": [{"window_id": 9, "app": "com.example.Editor", "title": "Draft",
                     "frame": [80, 60, 600, 400], "tree": tree}],
    }  # fmt: skip
    path = tmp_path / "alert.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return FakeEngine.from_fixture(path)


async def test_destructive_buttons_ask_first(tmp_path: Path) -> None:
    recorder = Recorder(answer=False)
    ctx = make_ctx(engine=alert_desktop(tmp_path, ["Delete", "Cancel", "Save"]), confirm=recorder)
    async with connected_client(ctx) as client:
        _, error = await call(client, "handle_dialog", {"action": "press", "button": "Delete"})
        assert error == "USER_DENIED"
        (request,) = recorder.requests
        assert '"Delete"' in request.summary
        assert request.detail == "Do you want to keep the changes?"
        data = await ok(client, "handle_dialog", {"action": "accept"})  # Save: no question
        assert data["pressed"] == "Save"
    assert len(recorder.requests) == 1


async def test_accept_picks_the_rightmost_button_without_a_known_title(tmp_path: Path) -> None:
    ctx = make_ctx(engine=alert_desktop(tmp_path, ["Cancel", "Keep", "Apply Changes"]))
    async with connected_client(ctx) as client:
        data = await ok(client, "handle_dialog", {"action": "accept"})
    assert data["pressed"] == "Apply Changes"


async def test_cancel_without_a_cancel_button_presses_escape(tmp_path: Path) -> None:
    engine = alert_desktop(tmp_path, ["OK"], window_dialog=True)
    ctx = make_ctx(engine=engine)
    async with connected_client(ctx) as client:
        info = await ok(client, "handle_dialog", {})
        assert info["kind"] == "dialog"
        data = await ok(client, "handle_dialog", {"action": "cancel"})
    assert data["pressed"] is None
    assert engine.events_of("press_key")[-1]["key"] == "escape"


@pytest.mark.parametrize(
    ("title", "destructive"),
    [("Delete", True), ("Don't Save", True), ("Move to Trash", True), ("Replace", True), ("Save", False),
     ("OK", False), ("Cancel", False), ("Remove Account", True)],
)  # fmt: skip
def test_destructive_titles(title: str, destructive: bool) -> None:
    assert bool(DESTRUCTIVE_BUTTON_RE.search(title)) is destructive
