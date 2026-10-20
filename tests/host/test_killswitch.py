"""The kill switch, the indicator (``axpilot.host.killswitch``) and the wiring of the macOS UI
(``axpilot.host.ui.start``) with stand-ins for its AppKit and Carbon modules."""

from __future__ import annotations

import sys
import types
from typing import Any, ClassVar, cast

import pytest

from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.host.killswitch import DEBOUNCE_S, LINGER_S, Indicator, KillSwitch
from axpilot.input.hotkeys import parse_hotkey
from axpilot.policy.control import ControlState

pytestmark = pytest.mark.anyio

HOTKEY = parse_hotkey("ctrl+option+cmd+.")


class Idle:
    def hid_idle_seconds(self) -> float:
        return 999.0


def control() -> ControlState:
    return ControlState(cast("Engine", Idle()), yield_to_user=False)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class View:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str | None, bool | None]] = []
        self.fail = fail

    def show(self, message: str, *, paused: bool) -> None:
        if self.fail:
            raise RuntimeError("no window server")
        self.calls.append(("show", message, paused))

    def hide(self) -> None:
        self.calls.append(("hide", None, None))


# --------------------------------------------------------------------------- kill switch
def test_presses_toggle_pause_and_bounces_count_once() -> None:
    state, clock, changes = control(), Clock(), []
    switch = KillSwitch(state, HOTKEY, clock=clock, on_change=changes.append)
    assert switch.pressed() is True
    assert state.paused
    assert state.pause_reason == "kill switch ⌃⌥⌘."
    clock.now += DEBOUNCE_S / 2  # key repeat or a bounce
    assert switch.pressed() is True
    assert state.paused
    clock.now += DEBOUNCE_S
    assert switch.pressed() is False
    assert not state.paused
    assert changes == [True, False]


async def test_a_press_stops_the_running_action() -> None:
    import anyio

    state = control()
    switch = KillSwitch(state, HOTKEY)

    async def typing() -> None:
        async with state.input_turn(label="type_text"):
            switch.pressed()
            await anyio.sleep(1)

    with pytest.raises(AXPilotError) as info:
        await typing()
    assert info.value.code is ErrorCode.PAUSED_BY_USER


def test_a_failing_report_or_toggle_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    state = control()

    def broken(paused: bool) -> None:
        raise RuntimeError("notification failed")

    assert KillSwitch(state, HOTKEY, on_change=broken).pressed() is True
    assert "Reporting the kill switch change failed" in caplog.text

    class Stuck(ControlState):
        def toggle_pause(self, reason: str = "kill switch") -> bool:
            raise RuntimeError("lock gone")

    stuck = Stuck(cast("Engine", Idle()))
    assert KillSwitch(stuck, HOTKEY).pressed() is False
    assert "could not change" in caplog.text


# --------------------------------------------------------------------------- indicator
async def test_the_indicator_follows_input_actions_and_lingers() -> None:
    state, clock, view = control(), Clock(), View()
    indicator = Indicator(view, HOTKEY.label, clock=clock)
    state.add_listener(indicator.on_event)
    indicator.tick()
    assert view.calls == []  # nothing to show yet
    async with state.input_turn(label="click"):
        indicator.tick()
        indicator.tick()  # no change, no redraw
    assert view.calls == [("show", "AXPilot is controlling this Mac · ⌃⌥⌘. stops it", False)]
    clock.now += LINGER_S / 2
    indicator.tick()
    assert len(view.calls) == 1  # still lingering
    clock.now += LINGER_S
    indicator.tick()
    assert view.calls[-1] == ("hide", None, None)


def test_the_indicator_shows_the_paused_state() -> None:
    state, view = control(), View()
    indicator = Indicator(view, HOTKEY.label)
    state.add_listener(indicator.on_event)
    state.pause()
    indicator.tick()
    assert view.calls == [("show", "AXPilot is paused · ⌃⌥⌘. resumes", True)]
    state.resume()
    indicator.tick()
    assert view.calls[-1] == ("hide", None, None)


def test_without_a_kill_switch_the_text_has_no_hotkey() -> None:
    indicator = Indicator(View(), None)
    assert indicator.message("controlling") == "AXPilot is controlling this Mac"
    assert indicator.message("paused") == "AXPilot is paused"


def test_a_failing_view_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    indicator = Indicator(View(fail=True), HOTKEY.label)
    indicator.on_event("turn_start", "click")
    indicator.tick()
    indicator.tick()  # the failure is logged once per change
    assert caplog.text.count("indicator could not be updated") == 1


# --------------------------------------------------------------------------- the UI wiring
class FakePump:
    fail = False

    def __init__(self) -> None:
        if FakePump.fail:
            raise RuntimeError("no window server")
        self.ticks: list[Any] = []

    def pump(self, seconds: float = 0.0) -> None:
        for tick in self.ticks:
            tick()


class FakeCarbon:
    fail = False
    instances: ClassVar[list[FakeCarbon]] = []

    def __init__(self, hotkey: Any, callback: Any) -> None:
        if FakeCarbon.fail:
            raise OSError("RegisterEventHotKey failed")
        self.hotkey, self.callback, self.closed = hotkey, callback, False
        FakeCarbon.instances.append(self)

    def close(self) -> None:
        self.closed = True


class FakeOverlay(View):
    fail = False

    def __init__(self) -> None:
        if FakeOverlay.fail:
            raise RuntimeError("no screens")
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, attr, value in (("app", "AppKitPump", FakePump), ("hotkey", "CarbonHotkey", FakeCarbon),
                              ("overlay", "Overlay", FakeOverlay)):  # fmt: skip
        module = types.ModuleType(f"axpilot.host.ui.{name}")
        setattr(module, attr, value)
        monkeypatch.setitem(sys.modules, f"axpilot.host.ui.{name}", module)
    FakePump.fail = FakeCarbon.fail = FakeOverlay.fail = False
    FakeCarbon.instances = []


async def test_start_wires_the_kill_switch_and_the_indicator(fake_appkit: None) -> None:
    from axpilot.host import ui

    state = control()
    running = ui.start(state, kill_switch="ctrl+option+cmd+.", indicator=True)
    assert running is not None
    (carbon,) = FakeCarbon.instances
    assert carbon.hotkey.label == "⌃⌥⌘."
    carbon.callback()  # the hotkey fires on the main thread
    assert state.paused
    running.pump(0.0)
    overlay = cast("FakeOverlay", running.indicator.view)  # type: ignore[union-attr]
    assert overlay.calls == [("show", "AXPilot is paused · ⌃⌥⌘. resumes", True)]
    running.close()
    assert carbon.closed
    assert overlay.closed


@pytest.mark.parametrize(
    "case",
    [
        # kill_switch, indicator, AppKit fails, hotkey fails, overlay fails -> what keeps running
        ("", False, False, False, False, None),  # both off: no AppKit at all
        ("ctrl+option+cmd+.", True, True, False, False, None),  # AppKit does not start
        ("ctrl+option+cmd+.", True, False, True, True, None),  # both parts fail
        ("ctrl+option+cmd+.", True, False, True, False, "indicator"),  # the hotkey is taken
        ("ctrl+option+cmd+.", True, False, False, True, "kill_switch"),  # no screens
        ("", True, False, False, False, "indicator"),
    ],
)
def test_start_degrades_part_by_part(fake_appkit: None, case: tuple[Any, ...]) -> None:
    from axpilot.host import ui

    kill_switch, indicator, FakePump.fail, FakeCarbon.fail, FakeOverlay.fail, expect = case
    running = ui.start(control(), kill_switch=kill_switch, indicator=indicator)
    if expect is None:
        assert running is None
        return
    assert running is not None
    assert (running.kill_switch is not None) is (expect == "kill_switch")
    assert (running.indicator is not None) is (expect == "indicator")
    if running.indicator is not None and not kill_switch:
        assert running.indicator.hotkey_label is None


def test_the_cli_starts_the_ui_only_for_the_real_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    from axpilot import cli
    from axpilot.engine.fake import FakeEngine
    from axpilot.testing import make_test_context

    calls: list[dict[str, Any]] = []
    fake_ui = types.ModuleType("axpilot.host.ui")
    fake_ui.start = lambda control, **options: calls.append(options) or "ui"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "axpilot.host.ui", fake_ui)
    monkeypatch.setattr("axpilot.host.ui", fake_ui, raising=False)

    fake_ctx = make_test_context()
    assert cli._start_ui(fake_ctx) is None  # not macOS
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli._start_ui(fake_ctx) is None  # the simulated desktop needs no kill switch
    real_ctx = make_test_context({"safety.kill_switch": "cmd+shift+escape", "safety.indicator": False})
    real_ctx.engine = cast("Engine", object.__new__(type("MacEngine", (), {})))
    assert not isinstance(real_ctx.engine, FakeEngine)
    assert cli._start_ui(real_ctx) == "ui"
    assert calls == [{"kill_switch": "cmd+shift+escape", "indicator": False}]
