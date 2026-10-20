"""``select_option`` on every kind of dropdown, against the fake desktop.

Kinds: native pop-up buttons (menu as their child, and at application level), menu buttons
(child and application-level menus), a combo box whose list opens in a window of its own
(Java), a web ``<select>``, a custom web combo box that only reacts to real mouse clicks (React
Select), a web autocomplete that opens with the Down arrow and filters as you type, an ARIA menu
button, and an Electron app's picker. For each: success, an option that is already chosen, an
unknown option (the menu or list is closed again), a disabled option, a disabled control, a stale
ref, the policy, the user at the controls, the kill switch, and engine faults (the input lock is
released and the menu is closed). The manual way (open with ``perform_action``, snapshot, click
the item) is tested too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import connected_client, make_test_context, structured

pytestmark = pytest.mark.anyio


@dataclass(frozen=True)
class Kind:
    """One dropdown of the fake desktop and what choosing in it does."""

    id: str
    app: str
    key: str
    """``role:name`` of the control in an interactive snapshot."""
    option: str
    chosen: str
    """The exact title the option matches."""
    control: str
    """Fixture id of the element whose value shows the choice (or the menu button itself)."""
    current: str | None
    """The option shown before anything is chosen (``None``: menu buttons show none)."""
    strategy: str
    launch: bool = False
    disabled: str | None = None
    """An option that is disabled, if the dropdown has one."""
    listing: str | None = None
    """Fixture id of a web list that must be hidden again after a failure."""


KINDS = [
    Kind(
        "popup", "TextEdit", "popup:Paragraph Styles", "heading", "Heading", "textedit.style", "Body", "menu"
    ),
    Kind(
        "popup-app-menu",
        "TextEdit",
        "popup:Line and Paragraph Spacing",
        "1.5",
        "1.5",
        "textedit.spacing",
        "1.0",
        "menu",
    ),
    Kind(
        "menubutton",
        "Finder",
        "menubutton:Action",
        "new folder",
        "New Folder",
        "finder.action",
        None,
        "menu",
        disabled="New Folder with Selection",
    ),
    Kind(
        "menubutton-app-menu",
        "Notes",
        "menubutton:Share",
        "Mess",
        "Messages",
        "notes.share",
        None,
        "menu",
        disabled="Copy Link",
    ),
    Kind(
        "java-menu-window",
        "IntelliJ IDEA",
        "combobox:Run/Debug Configurations",
        "all tests",
        "All Tests",
        "idea.runconfig",
        "Main",
        "menu_window",
        launch=True,
    ),
    Kind(
        "web-select",
        "Google Chrome",
        "popup:Country",
        "japan",
        "Japan",
        "store.country",
        "Canada",
        "menu",
        launch=True,
    ),
    Kind(
        "web-combobox",
        "Google Chrome",
        "combobox:Shipping method",
        "express",
        "Express (2 days)",
        "store.shipping.value",
        None,
        "listbox",
        launch=True,
        disabled="Overnight (sold out)",
        listing="store.shipping.list",
    ),
    Kind(
        "web-autocomplete",
        "Google Chrome",
        "combobox:Pickup store",
        "London",
        "London – Soho",
        "store.pickup",
        None,
        "listbox",
        launch=True,
        listing="store.pickup.list",
    ),
    Kind(
        "web-aria-menu",
        "Google Chrome",
        "popup:Sort reviews",
        "newest",
        "Newest",
        "store.sort",
        "Most helpful",
        "menu",
        launch=True,
        listing="store.sort.menu",
    ),
    Kind(
        "electron",
        "Slack",
        "popup:Notifications",
        "mentions",
        "Mentions only",
        "slack.notify",
        "All new messages",
        "listbox",
        launch=True,
        listing="slack.notify.list",
    ),
]
KIND_IDS = [kind.id for kind in KINDS]
WITH_DISABLED = [kind for kind in KINDS if kind.disabled]


def make_ctx(engine: Any = None, **overrides: Any) -> ServerContext:
    return make_test_context({"tools.profile": "standard", **overrides}, engine=engine)


@pytest.fixture
def ctx() -> ServerContext:
    return make_ctx()


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


def fake(ctx: ServerContext) -> FakeEngine:
    engine = ctx.engine
    assert isinstance(engine, FakeEngine)
    return engine


async def call(client: Client, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args)
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


async def control_ref(client: Client, kind: Kind) -> str:
    await ok(client, "app", {"action": "launch" if kind.launch else "activate", "name": kind.app})
    snap = await ok(client, "snapshot", {"app": kind.app})
    for el in snap["elements"]:
        if f"{el['role']}:{el.get('name')}" == kind.key:
            return el["ref"]
    raise AssertionError(f"{kind.key} not in the snapshot of {kind.app}")


def shown(ctx: ServerContext, kind: Kind) -> Any:
    engine = fake(ctx)
    return engine.refresh_element(engine.resolve_ref(kind.control)).value


def assert_closed(ctx: ServerContext, kind: Kind) -> None:
    """Nothing the dropdown opened is left open."""
    engine = fake(ctx)
    assert engine.menu_state() is None
    if kind.listing is not None:
        assert engine.refresh_element(engine.resolve_ref(kind.listing)).visible is False
    if kind.id == "java-menu-window":
        assert [w.window_id for w in engine.list_windows("com.jetbrains.intellij")] == [1301]


# =========================================================================== success
@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_select_option_chooses_in_every_kind_of_dropdown(
    ctx: ServerContext, client: Client, kind: Kind
) -> None:
    ref = await control_ref(client, kind)
    data = await ok(client, "select_option", {"ref": ref, "option": kind.option})
    assert data["strategy"] == kind.strategy
    assert data["attempts"][0].startswith("open:")
    assert data["attempts"][-1].startswith("pick:")
    assert kind.chosen in data["message"]
    if not kind.id.startswith("menubutton"):
        assert shown(ctx, kind) == kind.chosen
    assert_closed(ctx, kind)
    assert not ctx.control.busy


async def test_a_menu_button_runs_the_chosen_command(ctx: ServerContext, client: Client) -> None:
    ref = await control_ref(client, KINDS[2])
    data = await ok(client, "select_option", {"ref": ref, "option": "New Folder"})
    assert "untitled folder" in str(data["diff"])
    chosen = fake(ctx).events_of("menu_choose")
    assert [event["title"] for event in chosen] == ["New Folder"]


async def test_a_background_window_comes_forward_before_its_menu_opens(
    ctx: ServerContext, client: Client
) -> None:
    """Finder (macOS 26) checks its Action menu's items against the key window: opened in a
    background window, New Folder was disabled. The window is focused first, as a click would."""
    ref = await control_ref(client, KINDS[2])
    fake(ctx).app_control("activate", "Notes")  # Finder in the background
    fake(ctx).events.clear()
    await ok(client, "select_option", {"ref": ref, "option": "New Folder"})
    kinds = [
        kind
        for kind, details in fake(ctx).events
        if kind in ("window_control", "perform_action", "menu_choose")
    ]
    assert kinds[0] == "window_control"
    assert fake(ctx).events_of("window_control")[0]["action"] == "focus"


@pytest.mark.parametrize("kind", [k for k in KINDS if k.strategy == "menu"], ids=lambda k: k.id)
async def test_index_chooses_by_position(ctx: ServerContext, client: Client, kind: Kind) -> None:
    ref = await control_ref(client, kind)
    data = await ok(client, "select_option", {"ref": ref, "index": 1})
    assert data["strategy"] == kind.strategy
    assert_closed(ctx, kind)
    _, error = await call(client, "select_option", {"ref": ref, "index": 99})
    assert error == "INVALID_ARGUMENT"
    assert_closed(ctx, kind)


async def test_web_pickers_get_real_clicks_not_accessibility_actions(
    ctx: ServerContext, client: Client
) -> None:
    kind = next(k for k in KINDS if k.id == "web-combobox")
    ref = await control_ref(client, kind)
    data = await ok(client, "select_option", {"ref": ref, "option": "standard"})
    assert data["attempts"] == ["open:mouse", "pick:mouse"]
    assert not fake(ctx).events_of("ignored")  # the page script never saw an AX action
    assert shown(ctx, kind) == "Standard (5–7 days)"


async def test_a_web_autocomplete_opens_with_the_keyboard_and_filters(
    ctx: ServerContext, client: Client
) -> None:
    kind = next(k for k in KINDS if k.id == "web-autocomplete")
    ref = await control_ref(client, kind)
    data = await ok(client, "select_option", {"ref": ref, "option": "Oslo"})  # not listed until typed
    assert data["attempts"] == ["open:mouse", "open:keys:down", "filter:keys", "pick:mouse"]
    assert shown(ctx, kind) == "Oslo – Sentrum"
    assert any(event.get("text") == "Oslo" for event in fake(ctx).events_of("type_text"))
    assert_closed(ctx, kind)


async def test_pop_ups_set_through_set_value_and_fill_form_use_the_menu(
    ctx: ServerContext, client: Client
) -> None:
    style = await control_ref(client, KINDS[0])
    data = await ok(client, "set_value", {"ref": style, "value": "Title"})
    assert data["strategy"] == "menu"
    assert shown(ctx, KINDS[0]) == "Title"
    country = await control_ref(client, next(k for k in KINDS if k.id == "web-select"))
    await ok(client, "fill_form", {"fields": [{"ref": country, "value": "Portugal"}]})
    engine = fake(ctx)
    assert engine.refresh_element(engine.resolve_ref("store.country")).value == "Portugal"
    _, error = await call(client, "set_value", {"ref": country, "value": "Atlantis"})
    assert error == "INVALID_ARGUMENT"
    assert engine.menu_state() is None


# =========================================================================== already chosen
@pytest.mark.parametrize("kind", [k for k in KINDS if k.current is not None], ids=lambda k: k.id)
async def test_choosing_the_current_option_presses_nothing(
    ctx: ServerContext, client: Client, kind: Kind
) -> None:
    ref = await control_ref(client, kind)
    before = len(fake(ctx).events_of("menu_open"))
    data = await ok(client, "select_option", {"ref": ref, "option": kind.current or ""})
    if kind.id in ("popup", "popup-app-menu", "web-select", "web-aria-menu", "electron"):
        assert data["strategy"] is None  # a pop-up that already shows it is not even opened
        assert len(fake(ctx).events_of("menu_open")) == before
    assert shown(ctx, kind) == kind.current
    assert_closed(ctx, kind)


# =========================================================================== errors
@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_an_unknown_option_lists_the_options_and_closes_the_dropdown(
    ctx: ServerContext, client: Client, kind: Kind
) -> None:
    ref = await control_ref(client, kind)
    before = shown(ctx, kind)
    result, error = await call(client, "select_option", {"ref": ref, "option": "Zebra crossing"})
    assert error == "INVALID_ARGUMENT", text_of(result)
    assert kind.chosen in text_of(result)  # the options are listed
    assert shown(ctx, kind) == before
    assert_closed(ctx, kind)
    assert not ctx.control.busy


@pytest.mark.parametrize("kind", WITH_DISABLED, ids=lambda k: k.id)
async def test_a_disabled_option_is_refused_and_the_dropdown_closed(
    ctx: ServerContext, client: Client, kind: Kind
) -> None:
    assert kind.disabled is not None
    ref = await control_ref(client, kind)
    result, error = await call(client, "select_option", {"ref": ref, "option": kind.disabled})
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)
    assert "disabled" in text_of(result)
    assert not fake(ctx).events_of("menu_choose")
    assert_closed(ctx, kind)


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_a_disabled_control_is_not_opened(ctx: ServerContext, client: Client, kind: Kind) -> None:
    ref = await control_ref(client, kind)
    engine = fake(ctx)
    engine.set_enabled(kind.control if kind.control != "store.shipping.value" else "store.shipping", False)
    await ok(client, "snapshot", {"app": kind.app})
    events = len(engine.events)
    result, error = await call(client, "select_option", {"ref": ref, "option": kind.option})
    assert error == "ELEMENT_NOT_ACTIONABLE", text_of(result)
    assert "disabled" in text_of(result)
    assert not [e for e in engine.events[events:] if e[0] in ("press", "mouse_click", "menu_open")]


async def test_a_control_that_opens_nothing_is_reported(ctx: ServerContext, client: Client) -> None:
    await ok(client, "app", {"action": "activate", "name": "Finder"})
    snap = await ok(client, "snapshot", {"app": "Finder"})
    group = next(el["ref"] for el in snap["elements"] if el.get("name") == "Group")
    result, error = await call(client, "select_option", {"ref": group, "option": "Name"})
    assert error == "ELEMENT_NOT_ACTIONABLE"
    assert "showed no menu" in text_of(result)
    assert not ctx.control.busy


async def test_a_stale_ref_is_refused(ctx: ServerContext, client: Client) -> None:
    kind = next(k for k in KINDS if k.id == "web-select")
    ref = await control_ref(client, kind)
    windows = fake(ctx).list_windows("Google Chrome")
    await ok(client, "window", {"action": "close", "window_id": windows[0].window_id})
    _, error = await call(client, "select_option", {"ref": ref, "option": "Japan"})
    assert error == "STALE_REF"
    assert not fake(ctx).events_of("menu_open")


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_the_policy_blocks_before_anything_opens(kind: Kind) -> None:
    ctx = make_ctx()
    async with connected_client(ctx) as client:
        ref = await control_ref(client, kind)
        bundle = fake(ctx).find_app(kind.app).bundle_id
        assert bundle is not None
        ctx.config.safety.deny_apps.append(bundle)
        _, error = await call(client, "select_option", {"ref": ref, "option": kind.option})
    assert error == "BLOCKED_BY_POLICY"
    assert fake(ctx).menu_state() is None
    assert not fake(ctx).events_of("menu_choose")


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_nothing_happens_while_the_user_is_active(
    ctx: ServerContext, client: Client, kind: Kind
) -> None:
    ref = await control_ref(client, kind)
    engine = fake(ctx)
    engine.hid_idle = 0.0
    events = len(engine.events)
    _, error = await call(client, "select_option", {"ref": ref, "option": kind.option})
    assert error == "USER_ACTIVE"
    assert engine.events[events:] == []


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
async def test_nothing_happens_while_paused(ctx: ServerContext, client: Client, kind: Kind) -> None:
    ref = await control_ref(client, kind)
    ctx.control.pause()
    events = len(fake(ctx).events)
    _, error = await call(client, "select_option", {"ref": ref, "option": kind.option})
    assert error == "PAUSED_BY_USER"
    assert fake(ctx).events[events:] == []


# =========================================================================== faults while open
class BreakOnPick(FakeEngine):
    """Opens dropdowns normally, then fails when an option is pressed or clicked."""

    failure: BaseException = RuntimeError("boom /Users/me/secret")
    pause: ServerContext | None = None

    lists = ("store.shipping.list", "store.pickup.list", "slack.notify.list", "store.sort.menu")

    def _armed(self) -> bool:
        listing = [self._lookup(ref) for ref in self.lists]
        return self.menu_state() is not None or any(node is not None and node.visible for node in listing)

    def perform_action(self, handle: Any, action: str) -> None:
        if action == "AXPress" and self._armed():
            raise self.failure
        super().perform_action(handle, action)

    def mouse_click(
        self, point: Any, button: Any = "left", count: int = 1, modifiers: Any = frozenset()
    ) -> None:
        if self._armed():
            raise self.failure
        super().mouse_click(point, button, count, modifiers)

    def capture_tree(self, **kwargs: Any) -> Any:
        if self.pause is not None and self._armed():
            self.pause.control.pause()
        return super().capture_tree(**kwargs)


def break_on_pick() -> BreakOnPick:
    engine = BreakOnPick.demo()
    assert isinstance(engine, BreakOnPick)
    return engine


FAULTS = [
    pytest.param(RuntimeError("boom /Users/me/secret"), "INTERNAL", id="crash"),
    pytest.param(AXPilotError(ErrorCode.STALE_REF, "the item went away"), "STALE_REF", id="stale"),
    pytest.param(AXPilotError(ErrorCode.TIMEOUT, "the app did not answer"), "TIMEOUT", id="timeout"),
]


@pytest.mark.parametrize("kind", [k for k in KINDS if k.id != "java-menu-window"], ids=lambda k: k.id)
@pytest.mark.parametrize(("failure", "code"), FAULTS)
async def test_a_fault_while_open_closes_the_dropdown_and_releases_the_lock(
    kind: Kind, failure: BaseException, code: str
) -> None:
    engine = break_on_pick()
    engine.failure = failure
    ctx = make_ctx(engine)
    async with connected_client(ctx) as client:
        ref = await control_ref(client, kind)
        result, error = await call(client, "select_option", {"ref": ref, "option": kind.option})
        assert error == code, text_of(result)
        assert "/Users/me" not in text_of(result)
        assert not ctx.control.busy
        assert_closed(ctx, kind)
        engine.failure = RuntimeError("unused")
        assert (await ok(client, "status", {}))["engine"]


@pytest.mark.parametrize("kind", [k for k in KINDS if k.id != "java-menu-window"], ids=lambda k: k.id)
async def test_the_kill_switch_while_open_stops_and_closes_the_dropdown(kind: Kind) -> None:
    engine = break_on_pick()
    ctx = make_ctx(engine)
    engine.pause = ctx
    async with connected_client(ctx) as client:
        ref = await control_ref(client, kind)
        result, error = await call(client, "select_option", {"ref": ref, "option": kind.option})
    assert error == "PAUSED_BY_USER", text_of(result)
    assert not engine.events_of("menu_choose")
    assert not ctx.control.busy
    assert engine.menu_state() is None
    if kind.listing is not None:
        # web lists close with Escape, which AXPilot does not send while paused: they stay open
        assert engine.refresh_element(engine.resolve_ref(kind.listing)).visible is True


# =========================================================================== the manual way
MANUAL = [
    ("Finder", "Action", "New Folder", "New Folder with Selection", "AXShowMenu"),
    ("Notes", "Share", "Messages", "Copy Link", "AXPress"),
]


@pytest.mark.parametrize("case", MANUAL, ids=lambda case: case[0])
async def test_open_snapshot_and_click_the_item(
    ctx: ServerContext, client: Client, case: tuple[str, str, str, str, str]
) -> None:
    app, button, item, disabled, action = case
    await ok(client, "app", {"action": "activate", "name": app})
    snap = await ok(client, "snapshot", {"app": app})
    ref = next(el["ref"] for el in snap["elements"] if el.get("name") == button)
    await ok(client, "perform_action", {"ref": ref, "action": action})
    opened = await ok(client, "snapshot", {"app": app})
    items = {el["name"]: el for el in opened["elements"] if el["role"] == "menuitem"}
    assert item in items
    assert "disabled" in items[disabled]["states"]
    await ok(client, "click", {"ref": items[item]["ref"]})
    assert [event["title"] for event in fake(ctx).events_of("menu_choose")] == [item]
    assert fake(ctx).menu_state() is None
    after = await ok(client, "snapshot", {"app": app})
    assert not [el for el in after["elements"] if el["role"] == "menuitem"]


async def test_escape_or_a_click_outside_closes_an_open_menu(ctx: ServerContext, client: Client) -> None:
    await ok(client, "app", {"action": "activate", "name": "Notes"})
    snap = await ok(client, "snapshot", {"app": "Notes"})
    ref = next(el["ref"] for el in snap["elements"] if el.get("name") == "Format")
    await ok(client, "perform_action", {"ref": ref, "action": "AXPress"})
    assert fake(ctx).menu_state() is not None
    await ok(client, "press_keys", {"keys": "escape"})
    assert fake(ctx).menu_state() is None
    await ok(client, "perform_action", {"ref": ref, "action": "AXShowMenu"})
    await ok(client, "click", {"x": 1300, "y": 600})
    assert fake(ctx).menu_state() is None
    assert not fake(ctx).events_of("menu_choose")
