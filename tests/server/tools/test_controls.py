"""``select_option`` (pop-ups, combo boxes, radio and tab groups, lists) and ``handle_dialog``
(sheets and dialog windows: inspect, accept, cancel, press, fill), against the fake desktop."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.confirm import ConfirmRequest
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.server.tools import controls
from axpilot.server.tools.controls import DESTRUCTIVE_BUTTON_RE, match_option
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

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
    [("Center", 1), ("center", 1), ("cent", 1), ("Align Right…", 3)],
)
def test_match_option(option: str, index: int) -> None:
    assert match_option(["Align Left", "Center", "Justify", "Align Right"], option) == index


@pytest.mark.parametrize("option", ["Align", "Zebra"])
def test_match_option_errors(option: str) -> None:
    with pytest.raises(AXPilotError) as caught:
        match_option(["Align Left", "Center", "Align Right"], option)
    assert "Align Left" in caught.value.message


def test_a_word_inside_an_option_title_is_refused_with_the_title() -> None:
    with pytest.raises(AXPilotError) as caught:
        match_option(["Align Left", "Center", "Align Right"], "Right")
    assert caught.value.message.endswith('it is part of "Align Right".')


# =========================================================================== select_option
async def test_pop_ups_and_combo_boxes(client: Client) -> None:
    els = await elements(client, "TextEdit")
    style = els["popup:Paragraph Styles"]["ref"]
    data = await ok(client, "select_option", {"ref": style, "option": "heading"})
    assert data["strategy"] == "menu"  # opened, and the item chosen from the menu
    after = await elements(client, "TextEdit")
    assert after["popup:Paragraph Styles"]["value"] == "Heading"
    _, error = await call(client, "select_option", {"ref": style, "option": "Nope"})
    assert error == "INVALID_ARGUMENT"
    await ok(client, "select_option", {"ref": style, "index": 2})  # the open menu lists them
    after = await elements(client, "TextEdit")
    assert after["popup:Paragraph Styles"]["value"] == "Title"
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
    finder = await elements(client, "Finder")
    _, error = await call(
        client, "select_option", {"ref": finder["menubutton:Group"]["ref"], "option": "Name"}
    )
    assert error == "ELEMENT_NOT_ACTIONABLE"  # pressing it showed no menu
    els = await elements(client, "Notes")
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


FAULTS = [
    (Fault.crash("boom /Users/me"), "INTERNAL"),
    (Fault.hang(10), "TIMEOUT"),
    (Fault.error(ErrorCode.STALE_REF, "gone"), "STALE_REF"),
]


@pytest.mark.parametrize(("fault", "code"), FAULTS)
async def test_engine_faults_during_select_option_release_the_input_lock(fault: Fault, code: str) -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = make_ctx(engine=faulty)
    # Only the hang gets a short deadline, on a copy: a server keeps the context it was built with.
    faulted = dataclasses.replace(ctx, engine_timeout_s=0.1) if code == "TIMEOUT" else ctx
    try:
        async with connected_client(ctx) as client:  # setup with normal deadlines
            group = (await elements(client, "TextEdit"))["radiogroup:Text Alignment"]["ref"]
        async with connected_client(faulted) as client:
            faulty.set_fault("perform_action", fault)
            result, error = await call(client, "select_option", {"ref": group, "option": "Center"})
            assert error == code, text_of(result)
            assert "/Users/me" not in text_of(result)
            assert not ctx.control.busy
            faulty.clear_faults()
        async with connected_client(ctx) as client:  # normal deadlines
            await ok(client, "select_option", {"ref": group, "option": "Center"})
    finally:
        faulty.release()


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


def entered(engine: FakeEngine) -> list[str]:
    """Text put into fields, set through accessibility or typed as keystrokes (apps of an unknown
    kind, like these fixtures, get keystrokes first)."""
    return [e["value"] for e in engine.events_of("set_value")] + [
        e["text"] for e in engine.events_of("type_text")
    ]


async def test_fill_and_accept(ctx: ServerContext, client: Client) -> None:
    await ok(client, "app", {"action": "activate", "name": "Login Demo"})
    data = await ok(
        client, "handle_dialog", {"app": "Login Demo", "action": "accept", "text": "ada@example.com"}
    )
    assert data["pressed"] == "Sign In"
    pressed = engine_of(ctx).events_of("press")
    assert pressed[-1]["name"] == "Sign In"
    assert "ada@example.com" in entered(engine_of(ctx))


async def test_cancel_and_press(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "handle_dialog", {"app": "Login Demo", "action": "cancel"})
    assert data["pressed"] == "Cancel"
    assert (await ok(client, "handle_dialog", {"app": "Login Demo"}))["found"] is False


@pytest.mark.parametrize(("method", "action"), [("perform_action", "accept"), ("capture_tree", "inspect")])
@pytest.mark.parametrize(("fault", "code"), FAULTS)
async def test_engine_faults_during_handle_dialog_release_the_input_lock(
    method: str, action: str, fault: Fault, code: str
) -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = make_ctx(engine=faulty)
    # Only the hang gets a short deadline, on a copy: a server keeps the context it was built with.
    faulted = dataclasses.replace(ctx, engine_timeout_s=0.1) if code == "TIMEOUT" else ctx
    try:
        async with connected_client(faulted) as client:
            faulty.set_fault(method, fault)
            result, error = await call(client, "handle_dialog", {"app": "Login Demo", "action": action})
            assert error == code, text_of(result)
            assert "/Users/me" not in text_of(result)
            assert not ctx.control.busy
            faulty.clear_faults()
        async with connected_client(ctx) as client:  # normal deadlines
            assert (await ok(client, "handle_dialog", {"app": "Login Demo"}))["found"] is True
    finally:
        faulty.release()


async def test_handle_dialog_follows_the_policy() -> None:
    ctx = make_ctx(**{"safety.deny_apps": ["com.example.LoginDemo"]})
    async with connected_client(ctx) as client:
        for action in ("inspect", "accept", "cancel"):
            _, error = await call(client, "handle_dialog", {"app": "Login Demo", "action": action})
            assert error == "BLOCKED_BY_POLICY", action
    assert not engine_of(ctx).events_of("press")


async def open_save_panel(client: Client) -> None:
    await ok(client, "app", {"action": "activate", "name": "TextEdit"})
    await ok(client, "select_menu", {"app": "TextEdit", "path": ["File", "Save…"]})


async def test_a_save_panel_goes_to_a_folder_then_saves(ctx: ServerContext, client: Client) -> None:
    await open_save_panel(client)
    data = await ok(
        client,
        "handle_dialog",
        {"app": "TextEdit", "action": "accept", "path": "~/Desktop", "text": "Report"},
    )
    assert data["pressed"] == "Save"
    removed = {item["role"] for item in data["diff"]["removed"]}
    assert "sheet" in removed
    assert "window" not in removed  # the diff compares like with like (both snapshots full)
    engine = engine_of(ctx)
    keys = [e["combo"] for e in engine.events_of("press_key")]
    assert keys[:2] == ["shift+cmd+g", "return"]
    values = {e.get("handle"): e.get("value") for e in engine.events_of("set_value")}
    assert values["textedit.goto.path"] == "~/Desktop"
    assert values["textedit.save.name"] == "Report"
    assert (await ok(client, "handle_dialog", {"app": "TextEdit"}))["found"] is False  # saved and closed


async def test_a_path_needs_an_open_or_save_panel(ctx: ServerContext, client: Client) -> None:
    await ok(client, "app", {"action": "activate", "name": "Login Demo"})
    result, error = await call(
        client, "handle_dialog", {"app": "Login Demo", "action": "accept", "path": "/tmp"}
    )
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "Go to the folder" in text_of(result)
    assert not engine_of(ctx).events_of("press")  # nothing was accepted


async def test_a_refused_path_closes_the_field_and_saves_nothing(
    ctx: ServerContext, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controls, "GO_TO_WAIT_S", 0.2)
    await open_save_panel(client)
    engine = engine_of(ctx)
    engine._behaviour["textedit.goto.go"]["on_press"] = []  # the panel keeps the field: no such folder
    result, error = await call(
        client, "handle_dialog", {"app": "TextEdit", "action": "accept", "path": "~/Nowhere"}
    )
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)
    assert "did not accept" in text_of(result)
    assert engine.events_of("press_key")[-1]["key"] == "escape"
    assert not [e for e in engine.events_of("press") if e.get("name") == "Save"]


@pytest.mark.parametrize(
    "args",
    [
        {"action": "inspect", "path": "/tmp"},
        {"action": "cancel", "path": "/tmp"},
        {"action": "accept", "path": "Documents"},
        {"action": "accept", "path": "/tmp\n/etc"},
    ],
)
async def test_path_arguments_are_checked(client: Client, args: dict[str, Any]) -> None:
    _, error = await call(client, "handle_dialog", {"app": "TextEdit", **args})
    assert error == "INVALID_ARGUMENT"


async def test_press_by_title(client: Client) -> None:
    _, error = await call(client, "handle_dialog", {"app": "Login Demo", "action": "press", "button": "Help"})
    assert error == "INVALID_ARGUMENT"
    data = await ok(client, "handle_dialog", {"app": "Login Demo", "action": "press", "button": "sign"})
    assert data["pressed"] == "Sign In"
    for args in ({"action": "press"}, {"button": "OK"}, {"text": "x"}):
        _, error = await call(client, "handle_dialog", {"app": "Login Demo", **args})
        assert error == "INVALID_ARGUMENT", args


def alert_desktop(
    tmp_path: Path,
    buttons: list[str],
    *,
    window_dialog: bool = False,
    field: bool = False,
    search: bool = False,
) -> FakeEngine:
    """One app showing an alert: a sheet, or (``window_dialog``) a dialog window, optionally
    with a text field (``field``) and a search field before it (``search``, like a save panel)."""
    text_field = {
        "id": "alert.field",
        "role": "AXTextField",
        "description": "Name",
        "value": "draft",
        "frame": [120, 150, 300, 24],
    }
    contents = [
        {"role": "AXStaticText", "value": "Do you want to keep the changes?", "frame": [120, 120, 300, 20]},
        *(
            [
                {
                    "id": "alert.search",
                    "role": "AXTextField",
                    "subrole": "AXSearchField",
                    "value": "",
                    "frame": [120, 90, 200, 22],
                }
            ]
            if search
            else []
        ),
        *([text_field] if field else []),
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


async def test_a_destructive_press_asks_before_filling_the_text_field(tmp_path: Path) -> None:
    # Nothing may change before the user answers: a "no" leaves the field as it was, and an answer
    # given through the MCP client runs the call again from the start.
    recorder = Recorder(answer=False)
    engine = alert_desktop(tmp_path, ["Delete", "Cancel"], field=True)
    ctx = make_ctx(engine=engine, confirm=recorder)
    async with connected_client(ctx) as client:
        _, error = await call(client, "handle_dialog", {"action": "press", "button": "Delete", "text": "x"})
        assert error == "USER_DENIED"
        assert engine.events_of("set_value") == []
        assert engine.events_of("type_text") == []
        recorder.answer = True
        data = await ok(client, "handle_dialog", {"action": "press", "button": "Delete", "text": "final"})
    assert data["pressed"] == "Delete"
    assert entered(engine) == ["final"]


async def test_text_goes_to_the_name_field_not_the_search_field(tmp_path: Path) -> None:
    """macOS 26's save panel lists its search field before "Save As:": the text went into the
    search field and the document was saved as "Untitled"."""
    engine = alert_desktop(tmp_path, ["Cancel", "Save"], field=True, search=True)
    name, search = engine.resolve_ref("alert.field"), engine.resolve_ref("alert.search")
    ctx = make_ctx(engine=engine)
    async with connected_client(ctx) as client:
        await ok(client, "handle_dialog", {"action": "accept", "text": "report"})
    assert entered(engine) == ["report"]
    typed_into = {
        e.get("handle") or e.get("target")
        for e in engine.events_of("set_value") + engine.events_of("type_text")
    }
    assert name in typed_into
    assert search not in typed_into


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
