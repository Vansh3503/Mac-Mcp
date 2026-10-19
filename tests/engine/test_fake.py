from __future__ import annotations

import inspect
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from axpilot.engine import create_engine
from axpilot.engine.base import Engine
from axpilot.engine.fake import DEMO_FIXTURE, FakeEngine, parse_shortcut
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import KeyCombo, MenuNode, Point, RawNode, Rect
from axpilot.perception import build_snapshot, tree_to_dict


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine.demo()


def key(name: str, *modifiers: str) -> KeyCombo:
    return KeyCombo(name, frozenset(modifiers))


def expect(code: ErrorCode, func: Any, *args: Any, **kwargs: Any) -> AXPilotError:
    with pytest.raises(AXPilotError) as info:
        func(*args, **kwargs)
    assert info.value.code is code, info.value
    return info.value


def find(tree_root: RawNode, **attrs: Any) -> RawNode:
    for node in tree_root.walk():
        if all(getattr(node, name) == value for name, value in attrs.items()):
            return node
    raise AssertionError(f"no node with {attrs}")


def mini_desktop(**overrides: Any) -> dict[str, Any]:
    desktop: dict[str, Any] = {
        "os_version": "14.6",
        "apps": [{"name": "Widget", "bundle_id": "com.example.widget", "pid": 500, "frontmost": True}],
        "windows": [
            {
                "window_id": 1,
                "app": "com.example.widget",
                "title": "Controls",
                "frame": [100, 100, 400, 300],
                "tree": {
                    "role": "AXWindow",
                    "actions": ["AXRaise"],
                    "children": [
                        {
                            "id": "slider",
                            "role": "AXSlider",
                            "description": "Volume",
                            "value": 5,
                            "min": 0,
                            "max": 10,
                            "step": 2,
                            "frame": [110, 140, 200, 20],
                            "actions": ["AXIncrement", "AXDecrement"],
                        },
                        {
                            "id": "popup",
                            "role": "AXPopUpButton",
                            "description": "Size",
                            "value": "M",
                            "options": ["S", "M", "L"],
                            "frame": [110, 170, 100, 20],
                            "actions": ["AXPress"],
                        },
                        {
                            "id": "pane",
                            "role": "AXScrollArea",
                            "frame": [110, 200, 300, 150],
                            "children": [
                                {
                                    "id": "item",
                                    "role": "AXButton",
                                    "title": "Item",
                                    "frame": [120, 210, 60, 20],
                                    "actions": ["AXPress", "AXScrollToVisible"],
                                }
                            ],
                        },
                    ],
                },
            }
        ],
    }
    desktop.update(overrides)
    return desktop


# =========================================================================== interface & fixture
def test_every_abstract_engine_method_is_implemented() -> None:
    assert not inspect.isabstract(FakeEngine)
    for name in Engine.__abstractmethods__:
        assert getattr(FakeEngine, name) is not getattr(Engine, name), name
    assert FakeEngine.name == "fake"


def test_create_engine_builds_the_fake_engine(tmp_path: Path) -> None:
    assert isinstance(create_engine("fake"), FakeEngine)
    path = tmp_path / "mini.json"
    path.write_text(json.dumps(mini_desktop()), encoding="utf-8")
    custom = create_engine("fake", fixture=path)
    assert isinstance(custom, FakeEngine)
    assert custom.os_version() == "14.6"


def test_demo_fixture_has_the_documented_desktop(engine: FakeEngine) -> None:
    apps = {app.name: app for app in engine.list_apps(include_installed=True)}
    for name in ("Finder", "TextEdit", "Notes", "Login Demo", "Terminal", "1Password", "System Settings"):
        assert apps[name].pid > 0, name
    assert apps["Finder"].frontmost
    assert apps["Calculator"].pid == 0
    assert apps["Safari"].pid == 0
    titles = {w.title for w in engine.list_windows()}
    assert {"Downloads", "Untitled", "Notes", "Login Demo", "1Password", "Privacy & Security"} <= titles
    assert len(engine.list_displays()) == 2
    login = engine.capture_tree(app="Login Demo")
    secure = find(login.root, subrole="AXSecureTextField")
    assert secure.secure
    assert secure.value is None
    for app in ("TextEdit", "Notes"):
        menus = [m.title for m in engine.menu_bar(app).children]
        assert menus[:3] == ["Apple", app, "File"]
        assert {"Edit", "View", "Window", "Help"} <= set(menus)
    assert json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))["os_version"] == engine.os_version()


def test_from_fixture_errors(tmp_path: Path) -> None:
    expect(ErrorCode.INVALID_ARGUMENT, FakeEngine.from_fixture, tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    expect(ErrorCode.INVALID_ARGUMENT, FakeEngine.from_fixture, broken)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda d: d.update(colour="red"), "unknown key(s): colour"),
        (lambda d: d["windows"].append(dict(d["windows"][0])), "window_id must be a unique integer"),
        (lambda d: d["windows"][0].update(app="com.example.nope"), "must name an app"),
        (
            lambda d: d["windows"][0]["tree"]["children"][0].update(on_press={"focus": "ghost"}),
            "unknown element 'ghost'",
        ),
        (lambda d: d["windows"][0]["tree"]["children"][0].update(on_press={"teleport": 1}), "unknown effect"),
        (lambda d: d["windows"][0]["tree"]["children"][1].update(id="slider"), "used twice"),
        (lambda d: d["windows"][0]["tree"]["children"][1].update(id="n5"), "must not look like"),
        (
            lambda d: d["windows"][0]["tree"]["children"][0].update(on_press={"open_window": 99}),
            "unknown window",
        ),
        (lambda d: d.update(menus={"com.example.nope": []}), "unknown app"),
        (lambda d: d.update(displays=[]), "displays must be a non-empty list"),
    ],
)
def test_fixture_validation(change: Any, fragment: str) -> None:
    desktop = mini_desktop()
    change(desktop)
    error = expect(ErrorCode.INVALID_ARGUMENT, FakeEngine, desktop)
    assert fragment in error.message


def test_a_dumped_tree_loads_as_a_one_window_desktop(tmp_path: Path, engine: FakeEngine) -> None:
    dump = tree_to_dict(engine.capture_tree(app="Notes"))
    path = tmp_path / "notes.json"
    path.write_text(json.dumps(dump), encoding="utf-8")
    replay = FakeEngine.from_fixture(path)
    assert [a.name for a in replay.list_apps()] == ["Notes"]
    tree = replay.capture_tree()
    assert tree.window is not None
    assert tree.window.window_id == 401
    original = build_snapshot(engine.capture_tree(app="Notes"), snapshot_id="s_aaaa")
    replayed = build_snapshot(tree, snapshot_id="s_bbbb")
    assert [e.name for e in replayed.all_elements] == [e.name for e in original.all_elements]
    assert len(replay.list_displays()) == 1  # default display when the fixture has none


# =========================================================================== environment
def test_permissions_are_copies_and_can_be_flipped(engine: FakeEngine) -> None:
    status = engine.permissions()
    assert status.accessibility
    assert status.screen_recording
    assert status.automation == {"com.apple.Safari": True, "com.apple.Terminal": False}
    status.accessibility = False
    assert engine.permissions().accessibility
    engine.set_permissions(accessibility=False, screen_recording=False)
    assert not engine.permissions().accessibility
    assert not engine.permissions().screen_recording


def test_accessibility_is_required_for_tree_and_input(engine: FakeEngine) -> None:
    engine.set_permissions(accessibility=False)
    expect(ErrorCode.PERMISSION_ACCESSIBILITY, engine.capture_tree)
    expect(ErrorCode.PERMISSION_ACCESSIBILITY, engine.mouse_click, Point(10, 10))
    expect(ErrorCode.PERMISSION_ACCESSIBILITY, engine.menu_bar)
    assert engine.list_apps()  # listing apps needs no permission


def test_os_version_idle_and_secure_input_are_settable(engine: FakeEngine) -> None:
    assert engine.os_version() == "15.4"
    assert engine.hid_idle_seconds() == 999
    engine.hid_idle = 0.2
    assert engine.hid_idle_seconds() == 0.2
    assert engine.secure_input_active() is False
    engine.secure_input = True
    assert engine.secure_input_active() is True


# =========================================================================== apps, windows, displays
def test_list_apps_and_frontmost(engine: FakeEngine) -> None:
    running = engine.list_apps()
    assert all(app.pid > 0 for app in running)
    assert len(running) == 7
    everything = engine.list_apps(include_installed=True)
    installed_only = [app for app in everything if app.pid == 0]
    assert [app for app in everything if app.pid > 0] == running
    assert {"Calculator", "Safari", "Slack", "Canvas Paint", "IntelliJ IDEA", "Mail"} <= {
        app.name for app in installed_only
    }
    assert len(everything) == len(running) + len(installed_only)
    assert not any(app.frontmost for app in installed_only)
    front = engine.frontmost_app()
    assert front is not None
    assert (front.name, front.bundle_id, front.frontmost) == (
        "Finder",
        "com.apple.finder",
        True,
    )


@pytest.mark.parametrize("query", ["Notes", "notes", "com.apple.Notes", "812", "Notes.app", "note"])
def test_find_app_accepts_names_bundle_ids_pids_and_unique_prefixes(engine: FakeEngine, query: str) -> None:
    assert engine.find_app(query).name == "Notes"


def test_find_app_unknown_or_not_running(engine: FakeEngine) -> None:
    expect(ErrorCode.APP_NOT_FOUND, engine.find_app, "Photoshop")
    expect(ErrorCode.APP_NOT_FOUND, engine.find_app, "Calculator")


def test_windows_are_front_to_back_with_focus_flags(engine: FakeEngine) -> None:
    windows = engine.list_windows()
    assert [w.window_id for w in windows] == [201, 301, 401, 501, 601, 701, 801]
    assert [w.window_id for w in windows if w.focused] == [201]
    assert all(w.main for w in windows)
    assert [w.display_id for w in windows] == [1, 1, 1, 2, 2, 2, 2]
    assert [w.window_id for w in engine.list_windows("Terminal")] == [601]
    expect(ErrorCode.APP_NOT_FOUND, engine.list_windows, "Calculator")


def test_displays(engine: FakeEngine) -> None:
    main, external = engine.list_displays()
    assert main.main
    assert main.scale == 2.0
    assert main.frame == Rect(0, 0, 1440, 900)
    assert not external.main
    assert external.frame.y < 0
    assert external.work_area == Rect(1440, -155, 2560, 1415)


@pytest.mark.parametrize(
    ("point", "app"),
    [
        ((200, 200), "Finder"),
        ((1000, 700), "TextEdit"),
        ((50, 500), "Notes"),
        ((1700, 300), "Login Demo"),
        ((3200, 300), "System Settings"),
        ((5000, 5000), None),
    ],
)
def test_app_at_point_uses_the_topmost_window(
    engine: FakeEngine, point: tuple[int, int], app: str | None
) -> None:
    found = engine.app_at_point(Point(*point))
    assert (found.name if found else None) == app


# =========================================================================== accessibility tree
def test_capture_tree_defaults_to_the_frontmost_window(engine: FakeEngine) -> None:
    tree = engine.capture_tree()
    assert tree.app.name == "Finder"
    assert tree.window is not None
    assert tree.window.window_id == 201
    assert tree.root.role == "AXWindow"
    assert tree.root.title == "Downloads"
    assert tree.node_count == sum(1 for _ in tree.root.walk())
    assert not tree.truncated
    assert tree.menu_bar is None
    assert find(tree.root, description="list view").focused


def test_capture_tree_by_app_or_window(engine: FakeEngine) -> None:
    assert engine.capture_tree(app="Notes").window.window_id == 401  # type: ignore[union-attr]
    terminal = engine.capture_tree(window_id=601)
    assert terminal.app.name == "Terminal"
    assert find(terminal.root, role="AXTextArea").focused
    expect(ErrorCode.WINDOW_NOT_FOUND, engine.capture_tree, app="Notes", window_id=601)
    expect(ErrorCode.WINDOW_NOT_FOUND, engine.capture_tree, window_id=999)
    expect(ErrorCode.APP_NOT_FOUND, engine.capture_tree, app="Calculator")
    expect(ErrorCode.INVALID_ARGUMENT, engine.capture_tree, max_nodes=0)


def test_capture_tree_includes_the_menu_bar_on_request(engine: FakeEngine) -> None:
    bar = engine.capture_tree(app="Notes", include_menus=True).menu_bar
    assert bar is not None
    assert bar.role == "AXMenuBar"
    titles = [item.title for item in bar.children]
    assert titles == ["Apple", "Notes", "File", "Edit", "Format", "View", "Window", "Help"]
    file_menu = bar.children[2].children[0]
    assert file_menu.role == "AXMenu"
    assert file_menu.children[0].title == "New Note"


def test_capture_budgets_truncate_and_report_child_counts(engine: FakeEngine) -> None:
    small = engine.capture_tree(app="Notes", max_nodes=10)
    assert small.node_count == 10
    assert small.truncated
    assert any(node.child_count for node in small.root.walk())
    shallow = engine.capture_tree(app="Notes", max_depth=1)
    assert shallow.truncated
    assert all(not child.children for child in shallow.root.children)


def test_captured_trees_are_snapshots_of_state(engine: FakeEngine) -> None:
    before = engine.capture_tree(app="Login Demo")
    engine.perform_action(engine.resolve_ref("login.remember"), "AXPress")
    after = engine.capture_tree(app="Login Demo")
    assert find(before.root, title="Remember me").value == 0
    assert find(after.root, title="Remember me").value == 1


def test_app_without_windows_captures_the_application_element(engine: FakeEngine) -> None:
    engine.window_control(601, "close")
    tree = engine.capture_tree(app="Terminal")
    assert tree.window is None
    assert tree.root.role == "AXApplication"
    assert tree.root.title == "Terminal"
    assert engine.element_valid(tree.root.handle)
    assert engine.refresh_element(tree.root.handle).role == "AXApplication"
    assert engine.element_attributes(tree.root.handle) == {"AXRole": "AXApplication", "AXTitle": "Terminal"}


def test_element_validity_refresh_and_attributes(engine: FakeEngine) -> None:
    handle = engine.resolve_ref("notes.body")
    assert engine.element_valid(handle)
    assert not engine.element_valid("nope")
    assert not engine.element_valid(42)
    refreshed = engine.refresh_element(handle)
    assert refreshed.role == "AXTextArea"
    assert refreshed.children == []
    assert refreshed.focused
    attributes = engine.element_attributes(handle)
    assert attributes["AXRole"] == "AXTextArea"
    assert attributes["AXDescription"] == "Note body"
    assert attributes["AXPosition"] == [502, 77]
    assert attributes["AXFocused"] is True
    json.dumps(attributes)
    secret = engine.element_attributes(engine.resolve_ref("login.password"))
    assert "AXValue" not in secret
    assert secret["AXSubrole"] == "AXSecureTextField"
    engine.window_control(401, "close")
    assert not engine.element_valid(handle)
    expect(ErrorCode.STALE_REF, engine.refresh_element, handle)
    expect(ErrorCode.STALE_REF, engine.element_attributes, handle)


# =========================================================================== AX actions
def test_press_applies_fixture_effects_and_is_recorded(engine: FakeEngine) -> None:
    handle = engine.resolve_ref("login.submit")
    assert not engine.refresh_element(engine.resolve_ref("login.error")).visible
    engine.perform_action(handle, "AXPress")
    assert engine.refresh_element(engine.resolve_ref("login.error")).visible
    assert engine.events[-2] == ("perform_action", {"handle": handle, "action": "AXPress", "name": "Sign In"})
    assert engine.events[-1] == ("press", {"handle": handle, "name": "Sign In"})


def test_press_on_controls_has_builtin_behaviour(engine: FakeEngine) -> None:
    bold = engine.resolve_ref("textedit.bold")
    engine.perform_action(bold, "AXPress")
    assert engine.refresh_element(bold).value == 1
    engine.perform_action(bold, "AXPress")
    assert engine.refresh_element(bold).value == 0
    tree = engine.capture_tree(app="TextEdit")
    center = find(tree.root, description="Center")
    engine.perform_action(center.handle, "AXPress")
    radios = [n for n in engine.capture_tree(app="TextEdit").root.walk() if n.role == "AXRadioButton"]
    assert [r.value for r in radios] == [0, 1, 0, 0]
    disclosure = engine.resolve_ref("finder.invoices.disclosure")
    engine.perform_action(disclosure, "AXPress")
    assert engine.refresh_element(disclosure).expanded is True
    invoices = [n for n in engine.capture_tree().root.walk() if n.role == "AXRow" and n.expanded is not None]
    assert [row.expanded for row in invoices] == [True]


def test_perform_action_errors(engine: FakeEngine) -> None:
    forward = find(engine.capture_tree().root, description="Forward")
    error = expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.perform_action, forward.handle, "AXPress")
    assert "disabled" in error.message
    body = engine.resolve_ref("notes.body")
    error = expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.perform_action, body, "AXPress")
    assert error.hint
    assert "Supported actions" in error.hint
    expect(ErrorCode.STALE_REF, engine.perform_action, "ghost", "AXPress")


def test_raise_action_brings_a_window_forward(engine: FakeEngine) -> None:
    terminal_root = engine.capture_tree(window_id=601).root
    engine.perform_action(terminal_root.handle, "AXRaise")
    assert engine.list_windows()[0].window_id == 601
    assert engine.frontmost_app().name == "Terminal"  # type: ignore[union-attr]


def test_increment_decrement_and_slider_values() -> None:
    engine = FakeEngine(mini_desktop())
    slider = engine.resolve_ref("slider")
    engine.perform_action(slider, "AXIncrement")
    assert engine.refresh_element(slider).value == 7
    engine.perform_action(slider, "AXIncrement")
    engine.perform_action(slider, "AXIncrement")
    assert engine.refresh_element(slider).value == 10  # clamped to max
    engine.set_value(slider, "2.5")
    assert engine.refresh_element(slider).value == 2.5
    engine.set_value(slider, -4)
    assert engine.refresh_element(slider).value == 0
    expect(ErrorCode.INVALID_ARGUMENT, engine.set_value, slider, "loud")


def test_set_value_on_text_fields_and_secure_fields(engine: FakeEngine) -> None:
    email = engine.resolve_ref("login.email")
    engine.set_value(email, "ada@example.com")
    assert engine.refresh_element(email).value == "ada@example.com"
    password = engine.resolve_ref("login.password")
    assert engine.secure_value(password) == "correct horse battery staple"
    engine.set_value(password, "s3cret")
    assert engine.refresh_element(password).value is None
    assert engine.secure_value(password) == "s3cret"
    assert engine.events[-1] == ("set_value", {"handle": password, "value": "<secure>"})


def test_set_value_on_popups_checkboxes_and_read_only_elements(engine: FakeEngine) -> None:
    font = engine.resolve_ref("textedit.font")
    engine.set_value(font, "menlo")
    assert engine.refresh_element(font).value == "Menlo"
    error = expect(ErrorCode.INVALID_ARGUMENT, engine.set_value, font, "Comic Sans")
    assert error.hint
    assert "Helvetica" in error.hint
    size = engine.resolve_ref("textedit.size")
    engine.set_value(size, "15")  # combo boxes accept free text
    assert engine.refresh_element(size).value == "15"
    remember = engine.resolve_ref("login.remember")
    engine.set_value(remember, True)
    assert engine.refresh_element(remember).value == 1
    engine.set_value(remember, "off")
    assert engine.refresh_element(remember).value == 0
    expect(ErrorCode.INVALID_ARGUMENT, engine.set_value, remember, "maybe")
    expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.set_value, engine.resolve_ref("login.submit"), "x")
    username = find(engine.capture_tree(app="1Password").root, description="username")
    expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.set_value, username.handle, "mallory")


def test_focus_and_insert_text(engine: FakeEngine) -> None:
    search = engine.resolve_ref("notes.search")
    engine.focus(search)
    assert engine.refresh_element(search).focused
    assert not engine.refresh_element(engine.resolve_ref("notes.body")).focused
    assert engine.insert_text(search, "groc") is True
    assert engine.insert_text(search, "eries") is True
    assert engine.refresh_element(search).value == "groceries"
    assert engine.insert_text(engine.resolve_ref("notes.new-note"), "x") is False
    assert engine.events_of("insert_text")[-1] == {"handle": search, "secure": False, "text": "eries"}
    password = engine.resolve_ref("login.password")
    assert engine.insert_text(password, "!")
    assert engine.secure_value(password) == "correct horse battery staple!"
    assert engine.events_of("insert_text")[-1] == {"handle": password, "secure": True, "length": 1}
    disabled = find(engine.capture_tree().root, description="Forward").handle
    expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.focus, disabled)


def test_scroll_to_visible() -> None:
    engine = FakeEngine(mini_desktop())
    assert engine.scroll_to_visible(engine.resolve_ref("item")) is True
    assert engine.scroll_to_visible(engine.resolve_ref("popup")) is False
    assert engine.events[-1] == ("scroll_to_visible", {"handle": "item"})


# =========================================================================== synthetic input
def test_mouse_move_and_cursor_position(engine: FakeEngine) -> None:
    assert engine.cursor_position() == Point(720, 450)
    engine.mouse_move(Point(10, 20), duration_s=0.1)
    assert engine.cursor_position() == Point(10, 20)
    assert engine.events[-1] == ("mouse_move", {"point": [10, 20], "duration_s": 0.1})


def test_click_on_a_background_window_activates_it_and_hits_the_deepest_control(engine: FakeEngine) -> None:
    engine.mouse_click(Point(344, 261))  # the "Trip ideas" row, but Finder's window is on top there
    assert engine.frontmost_app().name == "Finder"  # type: ignore[union-attr]
    engine.mouse_click(Point(60, 500))  # an uncovered part of the Notes window
    assert engine.frontmost_app().name == "Notes"  # type: ignore[union-attr]
    assert engine.list_windows()[0].window_id == 401
    engine.mouse_click(Point(344, 261))  # now Notes is in front: select "Trip ideas"
    click = engine.events_of("mouse_click")[-1]
    assert click["target"] == "notes.row.trip"
    assert click["window_id"] == 401
    assert engine.refresh_element("notes.row.trip").selected
    assert not engine.refresh_element("notes.row.shopping").selected


def test_click_modifiers_extend_row_selection(engine: FakeEngine) -> None:
    engine.app_control("activate", "Notes")
    engine.mouse_click(Point(344, 261), modifiers=frozenset({"cmd"}))
    assert engine.refresh_element("notes.row.trip").selected
    assert engine.refresh_element("notes.row.shopping").selected


def test_click_focuses_text_fields_and_presses_buttons(engine: FakeEngine) -> None:
    engine.app_control("activate", "Notes")
    engine.mouse_click(Point(1340, 52))
    assert engine.refresh_element("notes.search").focused
    engine.mouse_click(Point(484, 52))  # New Note
    assert engine.refresh_element("notes.row.new").visible
    assert engine.refresh_element("notes.body").focused
    assert ("press", {"handle": "notes.new-note", "name": "New Note"}) in engine.events


def test_right_click_and_desktop_clicks_only_record(engine: FakeEngine) -> None:
    engine.app_control("activate", "Notes")
    engine.mouse_click(Point(484, 52), button="right")
    assert not engine.refresh_element("notes.row.new").visible
    engine.mouse_click(Point(5000, 5000))
    assert engine.events_of("mouse_click")[-1]["target"] is None
    expect(ErrorCode.INVALID_ARGUMENT, engine.mouse_click, Point(1, 1), count=0)
    expect(ErrorCode.INVALID_ARGUMENT, engine.mouse_click, Point(1, 1), button="thumb")  # type: ignore[arg-type]


def test_triple_click_selects_all_so_typing_replaces(engine: FakeEngine) -> None:
    engine.app_control("activate", "TextEdit")
    engine.mouse_click(Point(662, 469), count=3)
    engine.type_text("Fresh start")
    assert engine.refresh_element("textedit.body").value == "Fresh start"


def test_mouse_drag_and_scroll(engine: FakeEngine) -> None:
    engine.mouse_drag(Point(10, 10), Point(300, 200), modifiers=frozenset({"option"}))
    assert engine.cursor_position() == Point(300, 200)
    assert engine.events[-1][1]["modifiers"] == ["option"]
    engine.scroll(Point(60, 300), 0, 3)
    engine.scroll(None, 0, 2)
    area = engine.events[-1][1]["target"]
    assert area is not None
    assert engine.refresh_element(area).role == "AXScrollArea"
    assert engine.scroll_offsets[area] == (0.0, 5.0)
    expect(ErrorCode.INVALID_ARGUMENT, engine.mouse_drag, Point(0, 0), Point(1, 1), button="thumb")  # type: ignore[arg-type]


def test_typing_goes_to_the_focused_field_of_the_frontmost_app(engine: FakeEngine) -> None:
    engine.type_text("ignored")  # Finder's focused element is not a text field
    assert engine.events[-1] == ("type_text", {"target": None, "text": "ignored"})
    engine.app_control("activate", "Login Demo")
    engine.type_text("ada@example.com\n")
    assert engine.refresh_element("login.email").value == "ada@example.com"  # newline stripped
    engine.press_key(key("tab"))
    engine.type_text("hunter2")
    assert engine.secure_value("login.password") == "hunter2"  # tab selected the old contents
    assert engine.events[-1] == ("type_text", {"target": "login.password", "secure": True, "length": 7})


def test_secure_input_blocks_synthetic_keys(engine: FakeEngine) -> None:
    engine.secure_input = True
    expect(ErrorCode.SECURE_INPUT_ACTIVE, engine.type_text, "x")
    expect(ErrorCode.SECURE_INPUT_ACTIVE, engine.press_key, key("a"))


def test_editing_keys(engine: FakeEngine) -> None:
    engine.app_control("activate", "Terminal")
    engine.type_text("ls")
    engine.press_key(key("delete"))
    assert engine.refresh_element("terminal.shell").value.endswith("% l")  # type: ignore[union-attr]
    engine.press_key(key("return"))
    engine.press_key(key("space"))
    assert engine.refresh_element("terminal.shell").value.endswith("% l\n ")  # type: ignore[union-attr]
    engine.press_key(key("a", "cmd"))
    engine.press_key(key("delete"))
    assert engine.refresh_element("terminal.shell").value == ""
    assert engine.events_of("press_key")[0] == {"key": "delete", "modifiers": [], "combo": "delete"}


def test_clipboard_shortcuts_copy_and_paste(engine: FakeEngine) -> None:
    engine.app_control("activate", "Notes")
    engine.press_key(key("a", "cmd"))
    engine.press_key(key("c", "cmd"))
    assert engine.clipboard_get().text == "Shopping list\nMilk, eggs, bread, coffee beans, olive oil"
    engine.focus("notes.search")
    engine.press_key(key("v", "cmd"))
    assert engine.refresh_element("notes.search").value.startswith("Shopping list")  # type: ignore[union-attr]
    engine.focus("notes.body")
    engine.press_key(key("a", "cmd"))
    engine.press_key(key("x", "cmd"))
    assert engine.refresh_element("notes.body").value == ""


def test_menu_shortcuts_trigger_menu_items(engine: FakeEngine) -> None:
    engine.app_control("activate", "Notes")
    engine.press_key(key("n", "cmd"))
    assert engine.refresh_element("notes.row.new").visible
    assert engine.events[-1] == (
        "menu_select",
        {"app": "com.apple.Notes", "path": ["File", "New Note"], "via": "shortcut"},
    )
    engine.press_key(key("f", "cmd", "option"))
    assert engine.refresh_element("notes.search").focused


def test_parse_shortcut_glyphs() -> None:
    assert parse_shortcut("⇧⌘S") == key("s", "shift", "cmd")
    assert parse_shortcut("⌃⌘F") == key("f", "ctrl", "cmd")
    assert parse_shortcut("⌥⌘⎋") == key("escape", "option", "cmd")
    assert parse_shortcut("⌘,") == key(",", "cmd")
    assert parse_shortcut("⌘⌫") == key("delete", "cmd")
    assert parse_shortcut("⌃⌘␣") == key("space", "ctrl", "cmd")
    assert parse_shortcut(None) is None


# =========================================================================== app & window control
def test_launching_an_app_opens_its_windows(engine: FakeEngine) -> None:
    info = engine.app_control("launch", "Calculator")
    assert info is not None
    assert info.pid > 0
    assert info.frontmost
    assert engine.list_windows()[0].window_id == 901
    assert engine.capture_tree().root.title == "Calculator"
    again = engine.app_control("launch", "/System/Applications/Calculator.app")
    assert again is not None
    assert again.pid == info.pid
    expect(ErrorCode.APP_NOT_FOUND, engine.app_control, "launch", "Photoshop")


def test_activate_hide_unhide(engine: FakeEngine) -> None:
    engine.app_control("activate", "Terminal")
    assert engine.frontmost_app().name == "Terminal"  # type: ignore[union-attr]
    assert engine.list_windows()[0].window_id == 601
    assert engine.list_windows()[0].focused
    hidden = engine.app_control("hide", "Terminal")
    assert hidden is not None
    assert hidden.hidden
    assert 601 not in [w.window_id for w in engine.list_windows()]
    assert engine.frontmost_app().name != "Terminal"  # type: ignore[union-attr]
    under = engine.app_at_point(Point(2400, 300))
    assert under is None or under.name != "Terminal"
    engine.app_control("unhide", "Terminal")
    assert 601 in [w.window_id for w in engine.list_windows()]
    expect(ErrorCode.INVALID_ARGUMENT, engine.app_control, "explode", "Terminal")  # type: ignore[arg-type]


@pytest.mark.parametrize("action", ["quit", "force_quit"])
def test_quitting_closes_windows_and_invalidates_handles(engine: FakeEngine, action: str) -> None:
    body = engine.resolve_ref("textedit.body")
    assert engine.app_control(action, "TextEdit") is None  # type: ignore[arg-type]
    assert not engine.element_valid(body)
    assert "TextEdit" not in [a.name for a in engine.list_apps()]
    expect(ErrorCode.APP_NOT_FOUND, engine.app_control, "activate", "TextEdit")
    relaunched = engine.app_control("launch", "TextEdit")
    assert relaunched is not None
    assert engine.resolve_ref("textedit.body") == "textedit.body#2"
    assert engine.events_of("app_control")[0] == {"action": action, "app": "com.apple.TextEdit"}


def test_window_move_and_resize_shift_their_contents(engine: FakeEngine) -> None:
    before = engine.refresh_element("finder.search").frame
    moved = engine.window_control(201, "move", Rect(220, 190, 0, 0))
    assert moved is not None
    assert moved.frame == Rect(220, 190, 900, 520)
    after = engine.refresh_element("finder.search").frame
    assert before is not None
    assert after == Rect(before.x + 100, before.y + 100, before.w, before.h)
    resized = engine.window_control(201, "resize", Rect(0, 0, 700, 400))
    assert resized is not None
    assert resized.frame == Rect(220, 190, 700, 400)
    expect(ErrorCode.INVALID_ARGUMENT, engine.window_control, 201, "move")
    expect(ErrorCode.INVALID_ARGUMENT, engine.window_control, 201, "resize", Rect(0, 0, 0, 10))
    expect(ErrorCode.WINDOW_NOT_FOUND, engine.window_control, 999, "focus")
    expect(ErrorCode.INVALID_ARGUMENT, engine.window_control, 201, "spin")  # type: ignore[arg-type]


def test_minimize_restore_zoom_fullscreen_focus_close(engine: FakeEngine) -> None:
    minimized = engine.window_control(301, "minimize")
    assert minimized is not None
    assert minimized.minimized
    assert engine.app_at_point(Point(1050, 700)).name == "Notes"  # type: ignore[union-attr]
    focused = engine.window_control(301, "focus")
    assert focused is not None
    assert not focused.minimized
    assert focused.focused
    zoomed = engine.window_control(301, "zoom")
    assert zoomed is not None
    assert zoomed.frame == Rect(0, 25, 1440, 875)
    unzoomed = engine.window_control(301, "zoom")
    assert unzoomed is not None
    assert unzoomed.frame == Rect(260, 140, 820, 600)
    full = engine.window_control(301, "fullscreen")
    assert full is not None
    assert full.fullscreen
    assert full.frame == Rect(0, 0, 1440, 900)
    restored = engine.window_control(301, "restore")
    assert restored is not None
    assert not restored.fullscreen
    assert restored.frame == Rect(260, 140, 820, 600)
    assert engine.window_control(301, "close") is None
    assert 301 not in [w.window_id for w in engine.list_windows()]
    assert engine.events_of("window_control")[0] == {"window_id": 301, "action": "minimize", "rect": None}


def test_close_button_effect_closes_its_window(engine: FakeEngine) -> None:
    close = find(engine.capture_tree(window_id=601).root, subrole="AXCloseButton")
    engine.perform_action(close.handle, "AXPress")
    assert 601 not in [w.window_id for w in engine.list_windows()]


def test_open_target(engine: FakeEngine) -> None:
    engine.open_target("~/Downloads/Quarterly Report.pdf")
    assert engine.events[-1] == (
        "open",
        {"target": "~/Downloads/Quarterly Report.pdf", "with_app": None, "reveal": False},
    )
    engine.open_target("https://github.com", with_app="Safari")
    assert engine.frontmost_app().name == "Safari"  # type: ignore[union-attr]
    assert engine.list_windows()[0].title == "Start Page"
    engine.open_target("~/Downloads", reveal=True)
    assert engine.frontmost_app().name == "Finder"  # type: ignore[union-attr]
    expect(ErrorCode.INVALID_ARGUMENT, engine.open_target, "  ")
    expect(ErrorCode.APP_NOT_FOUND, engine.open_target, "x.txt", with_app="Nonexistent Editor")


# =========================================================================== menus
def test_menu_bar_depth_and_handles(engine: FakeEngine) -> None:
    top = engine.menu_bar("Notes", depth=1)
    assert top.title == "Notes"
    assert all(not menu.children for menu in top.children)
    two = engine.menu_bar("Notes")
    edit = next(menu for menu in two.children if menu.title == "Edit")
    assert [item.title for item in edit.children][:3] == ["Undo", "Redo", "Cut"]  # separators dropped
    assert all(not item.children for item in edit.children)
    three = engine.menu_bar("Notes", depth=3)
    find_menu = next(
        item for item in next(m for m in three.children if m.title == "Edit").children if item.title == "Find"
    )
    assert find_menu.children[0].shortcut == "⌥⌘F"
    assert engine.element_valid(find_menu.handle)


def test_apps_without_fixture_menus_get_standard_menus(engine: FakeEngine) -> None:
    titles = [menu.title for menu in engine.menu_bar("Terminal").children]
    assert titles == ["Apple", "Terminal", "File", "Edit", "View", "Window", "Help"]
    assert engine.menu_bar().children[1].title == "Finder"  # frontmost app by default


def test_menu_select_matching_rules(engine: FakeEngine) -> None:
    assert engine.menu_select("TextEdit", ["file", "save..."]) == ["File", "Save…"]
    assert engine.menu_select("TextEdit", ["Edit", "Sel"]) == ["Edit", "Select All"]
    assert engine.menu_select("TextEdit", ["Format", "Font", "Show Fonts"]) == [
        "Format",
        "Font",
        "Show Fonts",
    ]
    error = expect(ErrorCode.MENU_NOT_FOUND, engine.menu_select, "TextEdit", ["File", "Re"])
    assert "ambiguous" in error.message
    assert "Rename…" in error.message
    error = expect(ErrorCode.MENU_NOT_FOUND, engine.menu_select, "TextEdit", ["File", "Teleport"])
    assert error.hint
    assert "Duplicate" in error.hint
    expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.menu_select, "Notes", ["Edit", "Undo"])
    error = expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.menu_select, "Notes", ["Edit", "Find"])
    assert error.hint
    assert "Find Next" in error.hint
    expect(ErrorCode.INVALID_ARGUMENT, engine.menu_select, "Notes", [])
    expect(ErrorCode.APP_NOT_FOUND, engine.menu_select, "Calculator", ["File"])


def test_menu_items_with_effects_and_toggles(engine: FakeEngine) -> None:
    engine.menu_select("TextEdit", ["File", "New"])
    assert engine.list_windows()[0].title == "Untitled 2"
    assert engine.capture_tree(app="TextEdit").window.window_id == 302  # type: ignore[union-attr]
    assert engine.refresh_element("textedit2.body").focused  # a new window takes focus

    def ruler() -> MenuNode:
        menus = {menu.title: menu for menu in engine.menu_bar("TextEdit", depth=3).children}
        text_menu = next(item for item in menus["Format"].children if item.title == "Text")
        return next(item for item in text_menu.children if item.title == "Show Ruler")

    assert ruler().checked
    engine.menu_select("TextEdit", ["Format", "Text", "Show Ruler"])
    assert not ruler().checked
    assert engine.events_of("menu_select")[-1] == {
        "app": "com.apple.TextEdit",
        "path": ["Format", "Text", "Show Ruler"],
        "via": "menu",
    }


def test_standard_menu_commands(engine: FakeEngine) -> None:
    engine.menu_select("Terminal", ["Window", "Minimize"])
    assert next(w for w in engine.list_windows() if w.window_id == 601).minimized
    engine.menu_select("TextEdit", ["File", "Close"])
    assert 301 not in [w.window_id for w in engine.list_windows()]
    engine.menu_select("Notes", ["Notes", "Hide Notes"])
    assert engine.find_app("Notes").hidden
    engine.menu_select("Finder", ["Finder", "Show All"])
    assert not engine.find_app("Notes").hidden
    engine.menu_select("Terminal", ["Terminal", "Quit Terminal"])
    assert "Terminal" not in [a.name for a in engine.list_apps()]


def test_menu_handles_from_the_captured_menu_bar_can_be_pressed(engine: FakeEngine) -> None:
    bar = engine.capture_tree(app="Notes", include_menus=True).menu_bar
    assert bar is not None
    new_note = bar.children[2].children[0].children[0]
    assert new_note.title == "New Note"
    assert engine.element_valid(new_note.handle)
    engine.perform_action(new_note.handle, "AXPress")
    assert engine.refresh_element("notes.row.new").visible
    expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, engine.perform_action, bar.children[2].handle, "AXPress")


# =========================================================================== capture & vision
def decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_screenshot_of_the_main_display(engine: FakeEngine) -> None:
    capture = engine.screenshot()
    assert (capture.width, capture.height, capture.format) == (1280, 800, "jpeg")
    assert capture.region == Rect(0, 0, 1440, 900)
    assert capture.data[:2] == b"\xff\xd8"
    image = decode(capture.data)
    assert image.size == (1280, 800)
    assert image.format == "JPEG"
    assert capture.to_points(640, 400) == Point(720, 450)
    assert engine.events[-1][0] == "screenshot"


def test_screenshot_regions_windows_displays_and_formats(engine: FakeEngine) -> None:
    rect = engine.screenshot(rect=Rect(100, 100, 100, 50), fmt="png")
    assert (rect.width, rect.height) == (200, 100)  # Retina: 2 px per point, not upscaled further
    assert decode(rect.data).format == "PNG"
    window = engine.screenshot(window_id=601, max_edge=370)
    assert window.region == Rect(2300, 200, 740, 480)
    assert (window.width, window.height) == (370, 240)
    external = engine.screenshot(display_id=2, quality=30)
    assert (external.width, external.height) == (1280, 720)
    expect(ErrorCode.DISPLAY_NOT_FOUND, engine.screenshot, display_id=9)
    expect(ErrorCode.WINDOW_NOT_FOUND, engine.screenshot, window_id=9)
    expect(ErrorCode.INVALID_ARGUMENT, engine.screenshot, fmt="gif")  # type: ignore[arg-type]
    expect(ErrorCode.INVALID_ARGUMENT, engine.screenshot, quality=0)
    expect(ErrorCode.INVALID_ARGUMENT, engine.screenshot, rect=Rect(0, 0, 0, 10))


def test_screenshot_needs_permission_and_honours_exclusions(engine: FakeEngine) -> None:
    region = Rect(500, 560, 20, 20)  # inside Finder's window, clear of controls
    with_finder = decode(engine.screenshot(rect=region, fmt="png").data).convert("RGB")
    without = decode(engine.screenshot(rect=region, fmt="png", exclude_bundle_ids=["com.apple.finder"]).data)
    assert with_finder.getpixel((5, 5)) != without.convert("RGB").getpixel((5, 5))
    assert engine.events[-1][1]["exclude_bundle_ids"] == ["com.apple.finder"]
    engine.set_permissions(screen_recording=False)
    expect(ErrorCode.PERMISSION_SCREEN_RECORDING, engine.screenshot)


def test_ocr_reads_visible_static_text_in_the_region(engine: FakeEngine) -> None:
    lines = engine.ocr(engine.screenshot())
    texts = [line.text for line in lines]
    assert "5 items, 212.4 GB available" in texts
    assert "Notes" in texts  # the folder list is not covered by Finder
    assert "Shopping list" not in texts  # hidden behind the Finder window
    assert "Old ideas" not in texts  # scrolled below the Notes window
    assert all(0.0 < line.confidence <= 1.0 for line in lines)
    ys = [line.frame.y for line in lines]
    assert ys == sorted(ys)
    region_only = engine.ocr(engine.screenshot(rect=Rect(120, 588, 900, 22)))
    assert [line.text for line in region_only] == ["5 items, 212.4 GB available"]


# =========================================================================== clipboard, notifications, misc
def test_clipboard_round_trip(engine: FakeEngine) -> None:
    content = engine.clipboard_get()
    assert content.text == "Quarterly numbers: revenue up 12%"
    assert content.types == ["public.utf8-plain-text"]
    engine.clipboard_set(text="hello", files=["/tmp/a.txt"])
    content = engine.clipboard_get()
    assert (content.text, content.files) == ("hello", ["/tmp/a.txt"])
    assert content.types == ["public.utf8-plain-text", "public.file-url"]
    engine.clipboard_set(image_png=b"\x89PNG....")
    content = engine.clipboard_get()
    assert content.text is None
    assert content.types == ["public.png"]
    assert engine.events_of("clipboard_set")[-1] == {"text": None, "image_png": 8, "files": []}


def test_notify_close_and_event_helpers(engine: FakeEngine) -> None:
    engine.notify("Done", "Exported 3 files", subtitle="AXPilot")
    assert engine.events[-1] == (
        "notify",
        {"title": "Done", "message": "Exported 3 files", "subtitle": "AXPilot"},
    )
    engine.close()
    engine.close()
    assert engine.events_of("notify") == [
        {"title": "Done", "message": "Exported 3 files", "subtitle": "AXPilot"}
    ]
    assert engine.resolve_ref("login.email") == "login.email"  # by fixture id or AXIdentifier
    expect(ErrorCode.STALE_REF, engine.resolve_ref, "nothing-here")


def test_mutating_calls_are_logged_in_order(engine: FakeEngine) -> None:
    engine.app_control("activate", "Notes")
    engine.mouse_move(Point(1, 1))
    engine.type_text("x")
    engine.press_key(key("escape"))
    assert [kind for kind, _ in engine.events] == ["app_control", "mouse_move", "type_text", "press_key"]


def test_engine_calls_are_thread_safe(engine: FakeEngine) -> None:
    import threading

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                engine.capture_tree(app="Notes")
                engine.mouse_move(Point(5, 5))
                engine.list_windows()
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(engine.events_of("mouse_move")) == 300


# =========================================================================== fixture effects & defaults
def effects_desktop() -> dict[str, Any]:
    def press(ident: str, effect: Any, y: int) -> dict[str, Any]:
        return {
            "id": ident,
            "role": "AXButton",
            "title": ident,
            "frame": [20, y, 80, 20],
            "actions": ["AXPress"],
            "on_press": effect,
        }

    return {
        "displays": [
            {"display_id": 7, "frame": [0, 0, 1000, 800], "scale": 1.0},
            {"display_id": 8, "frame": [1000, 0, 1000, 800], "scale": 1.0},
        ],
        "apps": [
            {"name": "Widget", "bundle_id": "com.example.widget", "running": True, "frontmost": True},
            {"name": "Helper", "bundle_id": "com.example.helper", "running": False},
        ],
        "windows": [
            {
                "window_id": 1,
                "app": "Widget",
                "title": "Main",
                "frame": [0, 0, 600, 500],
                "tree": {
                    "role": "AXWindow",
                    "children": [
                        press("poke", {"focus": "helper.field"}, 10),
                        press("panel", {"open_window": 3}, 40),
                        press("copy", {"clipboard": "copied!"}, 70),
                        press("check", {"set_value": {"id": "box", "value": 1}}, 100),
                        press("home", [{"launch": "Helper"}, {"activate": "Widget"}], 130),
                        {
                            "id": "box",
                            "role": "AXCheckBox",
                            "title": "Box",
                            "value": 0,
                            "frame": [20, 160, 20, 20],
                        },
                    ],
                },
            },
            {
                "window_id": 2,
                "app": "com.example.helper",
                "title": "Helper",
                "frame": [1100, 50, 400, 300],
                "tree": {
                    "role": "AXWindow",
                    "children": [
                        {
                            "id": "helper.field",
                            "role": "AXTextField",
                            "frame": [1110, 90, 200, 24],
                            "focused": True,
                        }
                    ],
                },
            },
            {
                "window_id": 3,
                "app": "com.example.helper",
                "title": "Panel",
                "frame": [1150, 100, 200, 200],
                "open": False,
            },
        ],
        "menus": {"Widget": [{"title": "Widget", "children": [{"title": "Quit Widget", "shortcut": "⌘Q"}]}]},
        "clipboard": {"text": "hi", "files": ["/tmp/x"]},
    }


def test_fixture_defaults_fill_in_pids_main_display_and_apple_menu() -> None:
    engine = FakeEngine(effects_desktop())
    widget = engine.find_app("Widget")
    assert widget.pid > 1000
    displays = engine.list_displays()
    assert [d.main for d in displays] == [True, False]
    assert displays[0].work_area == displays[0].frame
    assert [m.title for m in engine.menu_bar().children] == ["Apple", "Widget"]
    assert engine.clipboard_get().files == ["/tmp/x"]
    assert [w.window_id for w in engine.list_windows()] == [1]  # Helper is not running


def test_effects_that_target_closed_windows_are_skipped_and_logged() -> None:
    engine = FakeEngine(effects_desktop())
    engine.perform_action("poke", "AXPress")
    assert engine.events[-1] == ("effect_skipped", {"effect": "focus", "ref": "helper.field"})


def test_open_window_effect_launches_the_owning_app() -> None:
    engine = FakeEngine(effects_desktop())
    engine.perform_action("panel", "AXPress")
    assert engine.find_app("Helper").pid > 0
    assert [w.window_id for w in engine.list_windows()] == [3, 2, 1]
    assert engine.frontmost_app().name == "Helper"  # type: ignore[union-attr]
    assert engine.list_windows()[0].display_id == 8


def test_clipboard_set_value_launch_and_activate_effects() -> None:
    engine = FakeEngine(effects_desktop())
    engine.perform_action("copy", "AXPress")
    assert engine.clipboard_get().text == "copied!"
    engine.perform_action("check", "AXPress")
    assert engine.refresh_element("box").value == 1
    engine.perform_action("home", "AXPress")
    assert engine.find_app("Helper").pid > 0
    assert engine.frontmost_app().name == "Widget"  # type: ignore[union-attr]
    engine.app_control("activate", "Helper")
    engine.type_text("typed")
    assert engine.refresh_element("helper.field").value == "typed"  # the new window took focus


def test_remaining_window_menu_commands(engine: FakeEngine) -> None:
    engine.menu_select("TextEdit", ["Window", "Zoom"])
    assert next(w for w in engine.list_windows() if w.window_id == 301).frame == Rect(0, 25, 1440, 875)
    engine.menu_select("TextEdit", ["View", "Enter Full Screen"])
    full = next(w for w in engine.list_windows() if w.window_id == 301)
    assert full.fullscreen
    assert full.frame == Rect(0, 0, 1440, 900)
    engine.window_control(301, "zoom")  # ignored while full screen
    assert next(w for w in engine.list_windows() if w.window_id == 301).fullscreen
    engine.menu_select("TextEdit", ["Window", "Bring All to Front"])
    assert engine.frontmost_app().name == "TextEdit"  # type: ignore[union-attr]
    engine.menu_select("TextEdit", ["TextEdit", "Hide Others"])
    assert [w.app for w in engine.list_windows()] == ["TextEdit"]


def test_set_value_on_radio_buttons_selects_within_the_group(engine: FakeEngine) -> None:
    radios = [n for n in engine.capture_tree(app="TextEdit").root.walk() if n.role == "AXRadioButton"]
    engine.set_value(radios[3].handle, True)
    values = [n.value for n in engine.capture_tree(app="TextEdit").root.walk() if n.role == "AXRadioButton"]
    assert values == [0, 0, 0, 1]
    engine.set_value(radios[3].handle, False)
    assert engine.refresh_element(radios[3].handle).value == 0


def test_tree_dumps_need_an_app() -> None:
    error = expect(ErrorCode.INVALID_ARGUMENT, FakeEngine.from_dict, {"root": {"role": "AXWindow"}})
    assert "needs an 'app'" in error.message
