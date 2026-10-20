"""Edge paths of the dropdown flow (:mod:`axpilot.server.dropdown`) on a small custom desktop:
verification that fails or cannot confirm, lists that stay open, options that are off screen or
covered, cleanup that cannot finish, and windows that vanish while they are read."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, Point, RawNode, RawTree
from axpilot.perception import dropdown as dd
from axpilot.server import dropdown
from axpilot.server.context import ServerContext
from axpilot.server.tools.input import Target, resolve_target
from axpilot.testing import make_test_context

pytestmark = pytest.mark.anyio


def option(handle: str, title: str, y: int, effects: Any, **extra: Any) -> dict[str, Any]:
    return {
        "id": handle,
        "role": "AXStaticText",
        "value": title,
        "frame": [300, y, 200, 30],
        "actions": ["AXPress"],
        "on_press": effects,
        **extra,
    }


def desktop() -> dict[str, Any]:
    """A web page with dropdowns that misbehave, and a native window with a stubborn pop-up."""
    page = [
        {
            "id": "stubborn",
            "role": "AXPopUpButton",
            "description": "Stubborn",
            "value": "One",
            "frame": [120, 120, 120, 24],
            "actions": ["AXPress"],
            "menu": [{"title": "One"}, {"title": "Two"}],
        },
        {
            "id": "silent",
            "role": "AXComboBox",
            "description": "Silent",
            "value": "",
            "frame": [300, 150, 200, 30],
            "actions": ["AXPress"],
            "on_press": {"show": "silent.list"},
        },
        {
            "id": "silent.list",
            "role": "AXList",
            "visible": False,
            "frame": [300, 182, 200, 60],
            "keys": {"⎋": {"hide": "silent.list"}},
            "children": [
                option("silent.a", "Apples", 182, {"hide": "silent.list"}),
                option("silent.b", "Pears", 212, {"hide": "silent.list"}),
            ],
        },
        {
            "id": "sticky",
            "role": "AXPopUpButton",
            "description": "Sticky",
            "value": "Red",
            "frame": [300, 260, 200, 30],
            "actions": ["AXPress"],
            "on_press": {"show": "sticky.list"},
        },
        {
            "id": "sticky.list",
            "role": "AXList",
            "visible": False,
            "frame": [300, 292, 200, 60],
            "keys": {"⎋": {"hide": "sticky.list"}},
            "children": [
                option("sticky.a", "Red", 292, {"set_value": {"id": "sticky", "value": "Red"}}),
                option("sticky.b", "Blue", 322, {"set_value": {"id": "sticky", "value": "Blue"}}),
            ],
        },
        {
            "id": "far",
            "role": "AXPopUpButton",
            "description": "Far",
            "value": "Near",
            "frame": [300, 380, 200, 30],
            "actions": ["AXPress"],
            "on_press": {"show": "far.list"},
        },
        {
            "id": "far.list",
            "role": "AXList",
            "visible": False,
            "frame": [300, 412, 200, 30],
            "children": [
                option(
                    "far.a",
                    "Near",
                    412,
                    [{"set_value": {"id": "far", "value": "Near"}}, {"hide": "far.list"}],
                ),
                {
                    "id": "far.b",
                    "role": "AXStaticText",
                    "value": "Away",
                    "frame": [300, 700, 200, 30],
                    "on_press": [{"set_value": {"id": "far", "value": "Away"}}, {"hide": "far.list"}],
                },
                option(
                    "far.c",
                    "Pressed",
                    730,
                    [{"set_value": {"id": "far", "value": "Pressed"}}, {"hide": "far.list"}],
                ),
            ],
        },
    ]
    return {
        "apps": [
            {"name": "Browser", "bundle_id": "com.google.Chrome", "pid": 700, "frontmost": True},
            {"name": "Other", "bundle_id": "com.example.other", "pid": 701},
        ],
        "windows": [
            {
                "window_id": 1,
                "app": "com.google.Chrome",
                "title": "Page",
                "frame": [100, 100, 600, 500],
                "tree": {
                    "role": "AXWindow",
                    "children": [{"role": "AXWebArea", "frame": [100, 100, 600, 500], "children": page}],
                },
            },
            {"window_id": 2, "app": "com.example.other", "title": "Other", "frame": [900, 100, 200, 200]},
        ],
    }


def native_desktop() -> dict[str, Any]:
    data = desktop()
    web_area = data["windows"][0]["tree"]["children"][0]
    data["windows"][0]["tree"]["children"] = web_area["children"]  # the same controls, native
    data["apps"][0]["bundle_id"] = "com.example.native"
    data["windows"][0]["app"] = "com.example.native"
    return data


def context(engine: FakeEngine | None = None) -> ServerContext:
    return make_test_context({"tools.profile": "standard"}, engine=engine or FakeEngine(desktop()))


async def target_of(ctx: ServerContext, name: str) -> Target:
    snap = await ctx.take_snapshot(app=None, mode="full")
    el = next(el for el in snap.all_elements if el.name == name)
    return await resolve_target(ctx, ref=el.ref, snapshot_id=snap.snapshot_id, x=None, y=None)


def fake(ctx: ServerContext) -> FakeEngine:
    assert isinstance(ctx.engine, FakeEngine)
    return ctx.engine


def visible(ctx: ServerContext, ref: str) -> bool:
    engine = fake(ctx)
    return engine.refresh_element(engine.resolve_ref(ref)).visible


# =========================================================================== verification
async def test_a_native_pop_up_that_ignores_the_choice_is_an_error() -> None:
    ctx = context(FakeEngine(native_desktop()))
    target = await target_of(ctx, "Stubborn")
    with pytest.raises(AXPilotError) as caught:
        await dropdown.choose_option(ctx, target, option="Two", index=None)
    assert caught.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert 'it shows "One"' in caught.value.message
    assert fake(ctx).menu_state() is None


async def test_a_web_picker_whose_choice_shows_nowhere_gets_a_warning() -> None:
    ctx = context()
    target = await target_of(ctx, "Silent")
    outcome = await dropdown.choose_option(ctx, target, option="Pears", index=None)
    assert outcome.strategy == "listbox"
    assert outcome.changed
    assert any("Could not confirm" in warning for warning in outcome.warnings)


async def test_a_list_that_stays_open_is_closed_with_a_warning() -> None:
    ctx = context()
    target = await target_of(ctx, "Sticky")
    outcome = await dropdown.choose_option(ctx, target, option="Blue", index=None)
    assert outcome.chosen == "Blue"
    assert any("stayed open" in warning for warning in outcome.warnings)
    assert not visible(ctx, "sticky.list")
    assert fake(ctx).refresh_element(fake(ctx).resolve_ref("sticky")).value == "Blue"


# =========================================================================== off screen and covered
async def test_an_option_outside_the_list_without_a_press_is_refused() -> None:
    ctx = context()
    target = await target_of(ctx, "Far")
    with pytest.raises(AXPilotError) as caught:
        await dropdown.choose_option(ctx, target, option="Away", index=None)
    assert caught.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert "not on screen" in caught.value.message
    assert visible(ctx, "far.list")  # no AXCancel, and Escape is not bound: it is left open (and logged)


async def test_an_option_outside_the_list_is_pressed_through_accessibility() -> None:
    ctx = context()
    target = await target_of(ctx, "Far")
    outcome = await dropdown.choose_option(ctx, target, option="Pressed", index=None)
    assert outcome.attempts[-1] == "pick:ax:AXPress"
    assert fake(ctx).refresh_element(fake(ctx).resolve_ref("far")).value == "Pressed"


class Covered(FakeEngine):
    """Another app's window covers the lists' options (an overlay macOS does not move away)."""

    def app_at_point(self, point: Point) -> AppInfo | None:
        if point.y > 180 and point.x > 300:
            return self.find_app("Other")
        return super().app_at_point(point)


async def test_an_option_covered_by_another_window_is_refused() -> None:
    ctx = context(Covered(desktop()))
    target = await target_of(ctx, "Silent")
    with pytest.raises(AXPilotError) as caught:
        await dropdown.choose_option(ctx, target, option="Apples", index=None)
    assert caught.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert "covered" in caught.value.message
    assert not visible(ctx, "silent.list")  # closed again with Escape


# =========================================================================== cleanup
async def test_closing_reports_when_the_app_is_not_in_front(caplog: pytest.LogCaptureFixture) -> None:
    ctx = context()
    target = await target_of(ctx, "Silent")
    fake(ctx).perform_action(fake(ctx).resolve_ref("silent"), "AXPress")
    tree = await ctx.capture_tree(app="Browser")
    listing = dd.find_node(tree.root, fake(ctx).resolve_ref("silent.list"))
    assert listing is not None
    found = dd.OptionList(listing[0], [], "page", in_web=True)
    fake(ctx).app_control("activate", "Other")
    warning = await dropdown._close(ctx, target, found)
    assert warning is not None
    assert "may still be open" in warning
    fake(ctx).app_control("activate", "Browser")
    assert await dropdown._close(ctx, target, found) is None
    assert not visible(ctx, "silent.list")


class Broken(FakeEngine):
    broken: frozenset[str] = frozenset()

    def element_valid(self, handle: Any) -> bool:
        if "element_valid" in self.broken:
            raise AXPilotError(ErrorCode.TIMEOUT, "no answer")
        return super().element_valid(handle)

    def frontmost_app(self) -> AppInfo | None:
        if "frontmost_app" in self.broken:
            raise AXPilotError(ErrorCode.TIMEOUT, "no answer")
        return super().frontmost_app()

    def capture_tree(self, **kwargs: Any) -> RawTree:
        if "capture_tree" in self.broken and kwargs.get("window_id") == 3:
            raise AXPilotError(ErrorCode.WINDOW_NOT_FOUND, "gone")
        return super().capture_tree(**kwargs)


async def test_cleanup_errors_are_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    engine = Broken(desktop())
    ctx = context(engine)
    target = await target_of(ctx, "Silent")
    found = dd.OptionList(RawNode(role="AXList", handle="silent.list"), [], "page")
    engine.broken = frozenset({"element_valid"})
    with caplog.at_level(logging.WARNING):
        assert await dropdown._close(ctx, target, found) is not None
        engine.broken = frozenset({"frontmost_app"})
        await dropdown._restore_text(ctx, target, "before")
    assert "Could not close" in caplog.text
    assert "Could not restore" in caplog.text


async def test_restoring_text_is_skipped_while_paused_or_in_the_background(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = context()
    target = await target_of(ctx, "Silent")
    events = len(fake(ctx).events)
    ctx.control.pause()
    with caplog.at_level(logging.WARNING):
        await dropdown._restore_text(ctx, target, "x")
        ctx.control.resume()
        fake(ctx).app_control("activate", "Other")
        await dropdown._restore_text(ctx, target, "x")
    assert "Left the filter text" in caplog.text
    assert not [e for e in fake(ctx).events[events:] if e[0] in ("press_key", "type_text")]
    fake(ctx).app_control("activate", "Browser")
    await dropdown._restore_text(ctx, target, "")
    assert [e[1]["combo"] for e in fake(ctx).events[events:] if e[0] == "press_key"] == ["cmd+a", "delete"]


# =========================================================================== odds and ends
async def test_a_target_without_an_element_is_refused() -> None:
    ctx = context()
    target = Target(point=Point(10, 10), app=None, window=None)
    with pytest.raises(AXPilotError) as caught:
        await dropdown.choose_option(ctx, target, option="x", index=None)
    assert caught.value.code is ErrorCode.ELEMENT_NOT_ACTIONABLE


async def test_app_kind_detection_failures_count_as_native(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = context(FakeEngine(native_desktop()))
    target = await target_of(ctx, "Stubborn")
    assert dropdown.in_web(ctx, target) is False

    def explode(*_: Any, **__: Any) -> str:
        raise OSError("unreadable bundle")

    monkeypatch.setattr(dropdown, "detect_app_kind", explode)
    assert dropdown.in_web(ctx, target) is False
    assert dropdown.in_web(ctx, Target(point=Point(0, 0), app=None, window=None)) is False


async def test_a_native_control_without_actions_is_opened_with_a_click() -> None:
    data = native_desktop()
    data["windows"][0]["tree"]["children"][0]["actions"] = []
    ctx = context(FakeEngine(data))
    target = await target_of(ctx, "Stubborn")
    attempt = await dropdown._open(ctx, target, web=False)
    assert attempt == "open:mouse"
    assert fake(ctx).menu_state() is not None


async def test_a_new_window_that_closes_while_read_is_skipped() -> None:
    data = desktop()
    data["windows"].append(
        {
            "window_id": 3,
            "app": "com.google.Chrome",
            "title": "Popup",
            "frame": [300, 150, 100, 100],
            "open": False,
        }
    )
    engine = Broken(data)
    ctx = context(engine)
    target = await target_of(ctx, "Silent")
    baseline = await dropdown._baseline(ctx, target)
    engine._open_window(3)
    engine.broken = frozenset({"capture_tree"})
    assert await dropdown._in_new_window(ctx, target, baseline) is None
    engine.broken = frozenset()
    assert await dropdown._in_new_window(ctx, target, baseline) is None
    assert await dropdown._window_ids(ctx, None) == frozenset()
