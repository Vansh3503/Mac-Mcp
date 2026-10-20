"""Tests for the macOS engine (``axpilot.engine.macos``).

Two groups live here:

* **Helper tests** exercise :mod:`axpilot.engine.macos.helpers`, the platform-independent
  logic behind the engine (AX error mapping, AXValue parsing, menu matching, shortcut
  decoding, AX↔CGWindow matching, geometry, keystroke chunking, process ancestry, bundle
  discovery…). They run on every OS.
* **Smoke tests** drive the real :class:`~axpilot.engine.macos.MacEngine`. They are marked
  ``macos`` and skipped unless running on macOS with ``AXPILOT_MACOS_TESTS=1``. Grant
  Accessibility (and, for screenshots, Screen Recording) to the terminal that runs pytest.
  Tests that change state (the clipboard) also need ``AXPILOT_MACOS_TESTS_MUTATE=1``.

Run on a Mac::

    AXPILOT_MACOS_TESTS=1 uv run pytest tests/engine/test_macos_engine.py -q
"""

from __future__ import annotations

import concurrent.futures
import os
import plistlib
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import axpilot.engine.macos as macos_pkg
from axpilot.engine.fake import parse_shortcut
from axpilot.engine.macos import helpers as h
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import DisplayInfo, KeyCombo, Point, Rect

ON_MAC = sys.platform == "darwin" and os.environ.get("AXPILOT_MACOS_TESTS") == "1"
MUTATE = ON_MAC and os.environ.get("AXPILOT_MACOS_TESTS_MUTATE") == "1"

mac_only = pytest.mark.skipif(not ON_MAC, reason="needs macOS and AXPILOT_MACOS_TESTS=1")


def display(display_id: int, frame: Rect, *, main: bool = False, scale: float = 2.0) -> DisplayInfo:
    return DisplayInfo(display_id=display_id, frame=frame, work_area=frame, scale=scale, main=main)


MAIN = display(1, Rect(0, 0, 1440, 900), main=True)
LEFT = display(2, Rect(-1920, -180, 1920, 1080), scale=1.0)


def expect(code: ErrorCode, func: Any, *args: Any, **kwargs: Any) -> AXPilotError:
    with pytest.raises(AXPilotError) as info:
        func(*args, **kwargs)
    assert info.value.code == code, info.value
    return info.value


# =============================================================================== package


def test_package_imports_without_pyobjc() -> None:
    assert macos_pkg.__all__ == ["MacEngine"]
    with pytest.raises(AttributeError):
        _ = macos_pkg.NotAThing  # type: ignore[attr-defined]


@pytest.mark.skipif(sys.platform == "darwin", reason="PyObjC is installed on macOS")
def test_mac_engine_needs_pyobjc_elsewhere() -> None:
    with pytest.raises(ImportError):
        _ = macos_pkg.MacEngine


# ============================================================================ AX errors


@pytest.mark.parametrize(
    ("code", "operation", "expected"),
    [
        (h.AX_API_DISABLED, "read", ErrorCode.PERMISSION_ACCESSIBILITY),
        (h.AX_INVALID_UI_ELEMENT, "action", ErrorCode.STALE_REF),
        (h.AX_CANNOT_COMPLETE, "read", ErrorCode.TIMEOUT),
        (h.AX_ACTION_UNSUPPORTED, "action", ErrorCode.ELEMENT_NOT_ACTIONABLE),
        (h.AX_ATTRIBUTE_UNSUPPORTED, "write", ErrorCode.ELEMENT_NOT_ACTIONABLE),
        (h.AX_ATTRIBUTE_UNSUPPORTED, "read", ErrorCode.NOT_SUPPORTED),
        (h.AX_NOT_IMPLEMENTED, "action", ErrorCode.ELEMENT_NOT_ACTIONABLE),
        (h.AX_ILLEGAL_ARGUMENT, "write", ErrorCode.INVALID_ARGUMENT),
        (h.AX_FAILURE, "action", ErrorCode.ELEMENT_NOT_ACTIONABLE),
        (h.AX_FAILURE, "read", ErrorCode.INTERNAL),
        (-1, "read", ErrorCode.INTERNAL),
    ],
)
def test_classify_ax_error(code: int, operation: h.AXOperation, expected: ErrorCode) -> None:
    assert h.classify_ax_error(code, operation) == expected


def test_ax_error_names_and_timeouts() -> None:
    assert h.ax_error_name(h.AX_CANNOT_COMPLETE) == "kAXErrorCannotComplete (-25204)"
    assert h.ax_error_name(42) == "AXError 42"
    assert h.is_timeout(0.95, 1.0)
    assert not h.is_timeout(0.01, 1.0)
    assert not h.is_timeout(5.0, 0.0)


# ============================================================================== AXValues


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "<AXValue 0x60000318c0c0> {value = x:12.000000 y:-34.500000 type = kAXValueCGPointType}",
            (h.AXVALUE_CGPOINT, (12.0, -34.5)),
        ),
        ("{value = w:300.000000 h:22.000000 type = kAXValueCGSizeType}", (h.AXVALUE_CGSIZE, (300.0, 22.0))),
        (
            "{value = x:0.000000 y:25.000000 w:1440.000000 h:875.000000 type = kAXValueCGRectType}",
            (h.AXVALUE_CGRECT, (0.0, 25.0, 1440.0, 875.0)),
        ),
        ("{value = location:3 length:7 type = kAXValueCFRangeType}", (h.AXVALUE_CFRANGE, (3.0, 7.0))),
        ("{value = error:-25205 type = kAXValueAXErrorType}", (h.AXVALUE_AXERROR, (-25205.0,))),
        ("{value = x:1.5 y:2 type = kAXValueTypeCGPoint}", (h.AXVALUE_CGPOINT, (1.5, 2.0))),
    ],
)
def test_parse_axvalue_description(text: str, expected: tuple[int, tuple[float, ...]]) -> None:
    assert h.parse_axvalue_description(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "hello", "{value = x:1 type = kAXValueCGPointType}", "{value = x:1 y:2 type = kAXValueBogus}"],
)
def test_parse_axvalue_description_rejects_garbage(text: str) -> None:
    assert h.parse_axvalue_description(text) is None


# ============================================================================== geometry


def test_frame_from() -> None:
    assert h.frame_from((10, 20), (30, 40)) == Rect(10, 20, 30, 40)
    assert h.frame_from(None, (1, 1)) is None
    assert h.frame_from((float("nan"), 0), (1, 1)) is None


def test_cocoa_to_global_flips_with_the_main_display_height() -> None:
    # The menu bar of a 900-pt-high main display: Cocoa y=875..900 → global y=0..25.
    assert h.cocoa_to_global(0, 875, 1440, 25, 900) == Rect(0, 0, 1440, 25)
    # A display above the main one has negative global y.
    assert h.cocoa_to_global(0, 900, 1920, 1080, 900) == Rect(0, -1080, 1920, 1080)


def test_fit_size() -> None:
    assert h.fit_size(2880, 1800, 1280) == (1280, 800)
    assert h.fit_size(800, 600, 1280) == (800, 600)
    assert h.fit_size(0.2, 0.2, 10) == (1, 1)
    expect(ErrorCode.INVALID_ARGUMENT, h.fit_size, 10, 10, 0)


def test_display_lookup() -> None:
    displays = [MAIN, LEFT]
    assert h.display_for_rect(Rect(100, 100, 50, 50), displays) is MAIN
    assert h.display_for_rect(Rect(-10, 100, 40, 40), displays) is MAIN  # 30 pt on MAIN, 10 on LEFT
    assert h.display_for_rect(Rect(-30, 100, 40, 40), displays) is LEFT  # 10 pt on MAIN, 30 on LEFT
    assert h.display_for_point(Point(-5, 0), displays) is LEFT
    assert h.display_for_point(Point(5000, 5000), displays) is MAIN  # nearest
    assert h.display_for_point(Point(0, 0), []) is None
    assert h.point_on_any_display(Point(1440, 900), displays)
    assert not h.point_on_any_display(Point(1441, 0), [MAIN])


def test_normalized_box_to_rect_flips_vision_coordinates() -> None:
    region = Rect(100, 50, 200, 100)
    assert h.normalized_box_to_rect(0.0, 0.0, 1.0, 1.0, region) == region
    box = h.normalized_box_to_rect(0.25, 0.8, 0.5, 0.1, region)
    assert box.to_list() == [150, 60, 100, 10]


def test_visibility_and_clipping() -> None:
    viewport = Rect(0, 100, 500, 300)
    assert h.is_visible(Rect(10, 150, 50, 20), viewport)
    assert not h.is_visible(Rect(10, 900, 50, 20), viewport)
    assert h.is_visible(None, viewport)
    assert h.is_visible(Rect(10, 900, 0, 0), viewport)  # zero size: left to the snapshot layer
    assert h.is_visible(Rect(10, 900, 50, 20), None)
    assert h.child_clip("AXScrollArea", viewport, None) == viewport
    nested = h.child_clip("AXScrollArea", Rect(0, 0, 100, 150), viewport)
    assert nested == Rect(0, 100, 100, 50)
    assert h.child_clip("AXPopover", Rect(0, 0, 10, 10), viewport) is None
    assert h.child_clip("AXGroup", Rect(0, 0, 10, 10), viewport) == viewport
    # Closed menus (no on-screen frame) are hidden; open ones are not clipped by their parent.
    assert not h.node_visible("AXMenu", None, None)
    assert not h.node_visible("AXMenu", Rect(0, 0, 0, 0), None)
    assert h.node_visible("AXMenu", Rect(10, 10, 200, 300), None)
    assert h.node_visible("AXButton", None, viewport)


# ================================================================================= menus


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AXAscendingSortDirection", "ascending"),
        ("AXDescendingSortDirection", "descending"),
        ("AXUnknownSortDirection", None),
        (None, None),
    ],
)
def test_sort_direction(raw: Any, expected: str | None) -> None:
    assert h.sort_direction(raw) == expected


def test_normalize_menu_title() -> None:
    assert h.normalize_menu_title("Save As…") == h.normalize_menu_title("save  as...")
    assert h.normalize_menu_title("New\u00a0Window") == "new window"
    assert h.normalize_menu_title("Wi\u2011Fi") == h.normalize_menu_title("wi-fi")


def test_match_menu_title() -> None:
    titles = ["New Window", "New Tab", "", "Open…", "Open Recent", "Close Window"]
    assert h.match_menu_title(titles, "new tab", "File") == 1
    assert h.match_menu_title(titles, "Open...", "File") == 3
    assert h.match_menu_title(titles, "Close", "File") == 5
    ambiguous = expect(ErrorCode.MENU_NOT_FOUND, h.match_menu_title, titles, "New", "File")
    assert "ambiguous" in ambiguous.message
    missing = expect(ErrorCode.MENU_NOT_FOUND, h.match_menu_title, titles, "Print", "File")
    assert missing.hint is not None
    assert "Open Recent" in missing.hint
    expect(ErrorCode.MENU_NOT_FOUND, h.match_menu_title, titles, "", "File")
    expect(ErrorCode.MENU_NOT_FOUND, h.match_menu_title, [], "x", "File")


def test_decode_menu_modifiers() -> None:
    assert h.decode_menu_modifiers(0) == {"cmd"}
    assert h.decode_menu_modifiers(None) == {"cmd"}
    assert h.decode_menu_modifiers(1) == {"cmd", "shift"}
    assert h.decode_menu_modifiers(2 | 4) == {"cmd", "option", "ctrl"}
    assert h.decode_menu_modifiers(8 | 4) == {"ctrl"}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("S", 0), "⌘S"),
        (("S", 1), "⇧⌘S"),
        (("a", 12), "⌃A"),
        ((",", 0), "⌘,"),
        (("?", 0), "⌘?"),
        (("\r", 0), "⌘↩"),
        (("\uf704", 0), "⌘F1"),
        (("\uf702", 2), "⌥⌘←"),
        ((None, 0, 0x0B), "⌘↩"),
        ((None, 1, 0x1B), "⇧⌘⎋"),
        ((None, 0, None, 0x7B), "⌘←"),
        ((None, 0), None),
        (("", 0, 0, None), None),
        ((None, 0, None, 0x00), None),  # 'a' is layout-dependent, not a named key
    ],
)
def test_menu_shortcut(args: tuple[Any, ...], expected: str | None) -> None:
    assert h.menu_shortcut(*args) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((" ", 0), "⌘Space"),  # M3: whitespace command characters are real shortcuts
        (("\r", 0), "⌘↩"),
        (("\t", 4), "⌃⌘⇥"),
        (("\n", 1), "⇧⌘↩"),
        (("S", 1), "⇧⌘S"),
        (("Save", 0), "⌘S"),  # only the first character counts
        ((" ", 0, None, 0x00), "⌘Space"),  # a stale virtual key never overrides the character
        ((None, 0, None, 0x31), "⌘Space"),  # no character: the virtual key (49, space) is used
        (("", 0, None, 0x24), "⌘↩"),  # an empty character is no character
        ((None, None), None),
        ((42, 0), None),  # not a string
        (("S", "1"), "⌘S"),  # non-integer modifiers count as none
        (("S", True), "⌘S"),  # a bool is not a modifier mask
        ((None, 0, 0x0B), "⌘↩"),
        ((None, 0, "0x0B"), None),
    ],
)
def test_menu_item_shortcut_from_raw_attributes(raw: tuple[Any, ...], expected: str | None) -> None:
    assert h.menu_item_shortcut(*raw) == expected


class Activation:
    """Records the activation ladder; the app comes to the front after ``front_after`` checks."""

    def __init__(self, *, front_after: int | None, raises: bool = False) -> None:
        self.front_after = front_after
        self.raises = raises
        self.steps: list[str] = []
        self.checks = 0

    def request(self) -> None:
        self.steps.append("request")

    def ax_raise(self) -> None:
        self.steps.append("ax_raise")

    def is_front(self) -> bool:
        self.checks += 1
        return self.front_after is not None and self.checks > self.front_after

    def wait(self, predicate: Any, timeout_s: float) -> bool:
        self.steps.append(f"wait {timeout_s:g}")
        return any(predicate() for _ in range(3))


def test_activation_sets_ax_frontmost_in_the_first_attempt() -> None:
    """M2: on macOS 14+ activateWithOptions from the background is usually ignored; waiting a
    full timeout before trying AXFrontmost made every activation take over 1.5 s."""
    ladder = Activation(front_after=1)
    front = h.bring_to_front(
        request=ladder.request,
        ax_raise=ladder.ax_raise,
        is_front=ladder.is_front,
        wait=ladder.wait,
        timeout_s=1.5,
    )
    assert front
    assert ladder.steps == ["request", "ax_raise", "wait 1.5"]  # one wait, after both requests


def test_activation_that_is_immediate_skips_accessibility() -> None:
    ladder = Activation(front_after=0)
    front = h.bring_to_front(
        request=ladder.request,
        ax_raise=ladder.ax_raise,
        is_front=ladder.is_front,
        wait=ladder.wait,
        timeout_s=1.5,
    )
    assert front
    assert ladder.steps == ["request"]


def test_activation_without_accessibility_and_an_app_that_never_comes_forward() -> None:
    ladder = Activation(front_after=None)
    front = h.bring_to_front(
        request=ladder.request, ax_raise=None, is_front=ladder.is_front, wait=ladder.wait, timeout_s=1.5
    )
    assert not front
    assert ladder.steps == ["request", "wait 1.5"]


def test_menu_shortcuts_parse_back_like_the_fake_engine() -> None:
    assert parse_shortcut(h.menu_shortcut("S", 1)) == KeyCombo("s", frozenset({"cmd", "shift"}))
    assert parse_shortcut(h.menu_shortcut("\r", 0)) == KeyCombo("return", frozenset({"cmd"}))


# =============================================================================== windows


def geo(x: float, y: float, w: float, h_: float, title: str | None = None) -> h.WindowGeometry:
    return h.WindowGeometry(Rect(x, y, w, h_), title)


def test_match_windows_by_frame() -> None:
    ax = [geo(0, 25, 800, 600, "A"), geo(100, 100, 400, 300, "B")]
    cg = [geo(100.5, 100, 400, 300), geo(0, 25, 800, 600)]
    assert h.match_windows(ax, cg) == {0: 1, 1: 0}


def test_match_windows_prefers_equal_titles_and_z_order() -> None:
    ax = [geo(0, 0, 500, 500, "Doc 2"), geo(0, 0, 500, 500, "Doc 1")]
    cg_with_titles = [geo(0, 0, 500, 500, "Doc 1"), geo(0, 0, 500, 500, "Doc 2")]
    assert h.match_windows(ax, cg_with_titles) == {0: 1, 1: 0}
    cg_without_titles = [geo(0, 0, 500, 500), geo(0, 0, 500, 500)]
    assert h.match_windows(ax, cg_without_titles) == {0: 0, 1: 1}


def test_match_windows_title_fallback_and_rejections() -> None:
    assert h.match_windows([geo(0, 0, 500, 500, "Moved")], [geo(300, 300, 500, 500, "moved")]) == {0: 0}
    assert h.match_windows([geo(0, 0, 500, 500, "A")], [geo(300, 300, 500, 500, "B")]) == {}
    assert h.match_windows([h.WindowGeometry(None, None)], [geo(0, 0, 1, 1)]) == {}
    # One CG window can only be used once.
    assert h.match_windows([geo(0, 0, 10, 10), geo(0, 0, 10, 10)], [geo(0, 0, 10, 10)]) == {0: 0}


# ================================================================================ typing


def test_utf16_length_and_clusters() -> None:
    assert h.utf16_length("abc") == 3
    assert h.utf16_length("😀") == 2
    family = "👨\u200d👩\u200d👧"
    assert h.text_clusters(f"a{family}b") == ["a", family, "b"]
    assert h.text_clusters("e\u0301x") == ["e\u0301", "x"]
    assert h.text_clusters("🇫🇷🇩🇪") == ["🇫🇷", "🇩🇪"]
    assert h.text_clusters("👍🏽!") == ["👍🏽", "!"]


def test_type_segments_chunks_text_and_maps_special_keys() -> None:
    segments = h.type_segments("a" * 45)
    assert [s for s, _ in segments] == ["text", "text", "text"]
    assert [len(t) for _, t in segments] == [20, 20, 5]
    assert h.type_segments("hi\r\nyou\tthere\r") == [
        ("text", "hi"),
        ("key", "return"),
        ("text", "you"),
        ("key", "tab"),
        ("text", "there"),
        ("key", "return"),
    ]
    assert h.type_segments("") == []


def test_type_segments_never_split_characters() -> None:
    text = "x" * 19 + "😀" + "y"
    segments = h.type_segments(text)
    assert all(h.utf16_length(t) <= h.MAX_EVENT_UTF16 for _, t in segments)
    assert "".join(t for _, t in segments) == text
    assert segments[0] == ("text", "x" * 19)
    assert h.type_segments("ab😀", max_units=1) == [("text", "a"), ("text", "b"), ("text", "😀")]


def test_type_segments_rejects_control_characters() -> None:
    error = expect(ErrorCode.INVALID_ARGUMENT, h.type_segments, "bell\x07")
    assert "U+0007" in error.message


# ================================================================================ motion


def test_scroll_steps_and_wheel_signs() -> None:
    assert h.scroll_steps(3) == [3]
    assert h.scroll_steps(25) == [10, 10, 5]
    assert h.scroll_steps(-12) == [-10, -2]
    assert h.scroll_steps(0.2) == [1]
    assert h.scroll_steps(0) == []
    assert h.scroll_steps(2.5) == [3]
    expect(ErrorCode.INVALID_ARGUMENT, h.scroll_steps, float("inf"))
    expect(ErrorCode.INVALID_ARGUMENT, h.scroll_steps, h.MAX_SCROLL_LINES + 1)
    # Positive dy reveals content below → negative wheel values; same for dx.
    assert h.wheel_deltas(0, 3) == ([-3], [0])
    assert h.wheel_deltas(-2, 0) == ([0], [2])
    assert h.wheel_deltas(15, -5) == ([5, 0], [-10, -5])


def test_motion_helpers() -> None:
    assert h.motion_steps(0) == 2
    assert h.motion_steps(0.5) == 30
    assert h.motion_steps(1000) == 600
    assert h.motion_steps(float("nan")) == 2
    path = h.interpolate(Point(0, 0), Point(10, 20), 2)
    assert path == [Point(5, 10), Point(10, 20)]
    assert h.interpolate(Point(0, 0), Point(1, 1), 0) == [Point(1, 1)]


def test_media_key_event_data() -> None:
    assert h.media_key_event_data(0, True) == (0xA00, 0x0A00)
    assert h.media_key_event_data(7, False) == (0xB00, (7 << 16) | 0xB00)


# ======================================================================== process lineage

PS_OUTPUT = """\
    1     0 /sbin/launchd
  501     1 /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal
  600   501 /usr/bin/login
  601   600 -zsh
  700   601 /opt/homebrew/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python
  garbage line
  800     1 /Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Helper
  801   800 /bin/zsh
"""


def test_parse_ps_table_and_ancestry() -> None:
    table = h.parse_ps_table(PS_OUTPUT)
    assert table[601] == (600, "-zsh")
    assert 0 not in table
    chain = h.ancestry(700, table)
    assert [pid for pid, _ in chain] == [700, 601, 600, 501, 1]
    assert h.ancestry(12345, table) == []
    cyclic = {1: (2, "a"), 2: (1, "b")}
    assert [pid for pid, _ in h.ancestry(1, cyclic)] == [1, 2]


def test_app_bundle_and_responsible_app() -> None:
    assert h.app_bundle_of("/Applications/Safari.app/Contents/MacOS/Safari") == "/Applications/Safari.app"
    nested = "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/x"
    assert h.app_bundle_of(nested) == "/Applications/Visual Studio Code.app"
    assert (
        h.app_bundle_of("/Library/Frameworks/Python.framework/Resources/Python.app/Contents/MacOS/Python")
        is None
    )
    assert h.app_bundle_of("/usr/bin/python3") is None
    table = h.parse_ps_table(PS_OUTPUT)
    assert h.responsible_app_name(exe for _, exe in h.ancestry(700, table)) == "Terminal"
    assert h.responsible_app_name(exe for _, exe in h.ancestry(801, table)) == "Visual Studio Code"
    assert h.responsible_app_name(["/bin/zsh", "/sbin/launchd"]) is None


# ========================================================================= installed apps


def make_bundle(
    root: Path, name: str, info: dict[str, Any] | None, *, frameworks: tuple[str, ...] = ()
) -> Path:
    bundle = root / f"{name}.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    if info is not None:
        (contents / "Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
    for framework in frameworks:
        (contents / "Frameworks" / framework).mkdir(parents=True)
    return bundle


def test_scan_and_read_bundles(tmp_path: Path) -> None:
    apps = tmp_path / "Applications"
    make_bundle(apps, "Notes", {"CFBundleIdentifier": "com.apple.Notes", "CFBundleName": "Notes"})
    make_bundle(apps / "Utilities", "Terminal", {"CFBundleIdentifier": "com.apple.Terminal"})
    broken = make_bundle(apps, "Broken", None)
    (broken / "Contents" / "Info.plist").write_text("not a plist")
    (apps / "readme.txt").write_text("x")
    found = h.scan_app_bundles([apps, tmp_path / "missing"])
    assert [p.name for p in found] == ["Broken.app", "Notes.app", "Terminal.app"]
    infos = {i.name: i for i in map(h.read_bundle_info, found)}
    assert infos["Notes"].bundle_id == "com.apple.Notes"
    assert infos["Broken"].bundle_id is None
    assert infos["Notes"].matches_name("notes.app")
    assert not infos["Notes"].matches_name("")


def test_chromium_kind(tmp_path: Path) -> None:
    electron = make_bundle(tmp_path, "Slack", {}, frameworks=("Electron Framework.framework",))
    cef = make_bundle(tmp_path, "Spotify", {}, frameworks=("Chromium Embedded Framework.framework",))
    chrome_like = make_bundle(tmp_path, "Browser", {})
    (
        chrome_like
        / "Contents/Frameworks/Browser Framework.framework/Versions/1.0/Helpers/Browser Helper (Renderer).app"
    ).mkdir(parents=True)
    plain = make_bundle(tmp_path, "Plain", {})
    assert h.chromium_kind(str(electron), "com.tinyspeck.slackmacgap") == "electron"
    assert h.chromium_kind(str(cef), "com.spotify.client") == "chromium"
    assert h.chromium_kind(str(chrome_like), "com.example.browser") == "chromium"
    assert h.chromium_kind(None, "com.google.Chrome.canary") == "chromium"
    assert h.chromium_kind(str(plain), "com.apple.Notes") is None
    assert h.chromium_kind(None, None) is None


# ================================================================================== misc


def test_target_classification() -> None:
    assert h.is_auth_agent("SecurityAgent", None)
    assert h.is_auth_agent(None, "com.apple.SecurityAgent")
    assert not h.is_auth_agent("Safari", "com.apple.Safari")
    assert h.looks_like_url("https://example.com")
    assert h.looks_like_url("x-apple.systempreferences:com.apple.preference.security")
    assert not h.looks_like_url("/tmp/a:b")
    assert not h.looks_like_url("notes.txt")
    assert h.looks_like_bundle_id("com.apple.Safari")
    assert not h.looks_like_bundle_id("Safari.app")
    assert not h.looks_like_bundle_id("Visual Studio Code")
    assert h.looks_like_app_path("/Applications/Safari.app")
    assert h.looks_like_app_path("~/Applications/X.app")
    assert not h.looks_like_app_path("Safari")


def test_notification_script_escapes_everything() -> None:
    script = h.notification_script('Say "hi"', "line 1\nline 2 \\ end", "sub")
    assert script == (
        'display notification "line 1\\nline 2 \\\\ end" with title "Say \\"hi\\"" subtitle "sub"'
    )
    assert h.notification_script("", "only message") == 'display notification "only message"'
    expect(ErrorCode.INVALID_ARGUMENT, h.notification_script, " ", "")
    long = h.notification_script("t" * 500, "m")
    assert "t" * (h.NOTIFY_TITLE_MAX - 1) + "…" in long


def test_values_and_toggles() -> None:
    assert h.is_png(h.PNG_SIGNATURE + b"rest")
    assert not h.is_png(b"GIF89a")
    assert h.parse_toggle("On") is True
    assert h.parse_toggle(0) is False
    expect(ErrorCode.INVALID_ARGUMENT, h.parse_toggle, "maybe")
    assert h.coerce_value(0.5, "0.75", "AXSlider") == 0.75
    assert h.coerce_value(3, "4", "AXIncrementor") == 4
    assert h.coerce_value(None, 7, "AXSlider") == 7.0
    assert h.coerce_value(True, "off", "AXCheckBox") is False
    assert h.coerce_value("old", 2.0, "AXTextField") == "2"
    assert h.coerce_value(None, True, "AXTextField") == "true"
    expect(ErrorCode.INVALID_ARGUMENT, h.coerce_value, 0.5, "loud", "AXSlider")
    expect(ErrorCode.INVALID_ARGUMENT, h.coerce_value, 0.5, "nan", "AXSlider")
    assert h.truncate_text("abcdef", 4) == "abc…"
    assert h.truncate_text("abc", 4) == "abc"
    assert h.truncate_text("abc", 0) == ""


# ======================================================================= keyboard layout


def test_build_char_map_prefers_main_keys_and_fewer_modifiers() -> None:
    table = {
        (0x00, 0): "q",  # AZERTY 'q' on the ANSI 'a' key
        (0x00, h.UC_SHIFT_STATE): "Q",
        (0x1C, 0): "!",
        (0x1C, h.UC_SHIFT_STATE): "8",
        (0x43, 0): "*",  # keypad *
        (0x1C, h.UC_OPTION_STATE): "*",  # option+8 on the main keyboard
        (0x31, 0): " ",
        (0x24, 0): "\r",
    }
    mapping = h.build_char_map(
        lambda code, state: table.get((code, state), ""), [0x00, 0x1C, 0x24, 0x31, 0x43]
    )
    assert mapping["q"] == (0x00, False, False)
    assert mapping["Q"] == (0x00, True, False)
    assert mapping["8"] == (0x1C, True, False)
    assert mapping["*"] == (0x1C, False, True)  # main keyboard beats the keypad
    assert " " not in mapping
    assert "\r" not in mapping


def test_us_ansi_fallback_layout() -> None:
    layout = h.KeyboardLayout("com.apple.keylayout.US", h.us_ansi_char_map(), fallback=True)
    assert layout.lookup("a") == (0x00, False, False)
    assert layout.lookup("A") == (0x00, True, False)
    assert layout.lookup("?") == (0x2C, True, False)
    assert layout.lookup("€") is None
    lowercase_only = h.KeyboardLayout(None, {"é": (0x13, False, False)})
    assert lowercase_only.lookup("É") == (0x13, True, False)


# ============================================================================== HID idle


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.last_hid = 990.0  # the user last touched the machine 10 s ago

    def idle(self) -> float:
        return self.now - self.last_hid


def test_idle_estimator_ignores_our_own_events() -> None:
    clock = FakeClock()
    estimator = h.IdleEstimator(clock.idle, lambda: clock.now)
    assert estimator.idle_seconds() == pytest.approx(10.0)
    estimator.before_post()
    clock.last_hid = clock.now  # our synthetic event resets the HID idle counter
    estimator.after_post()
    clock.now += 0.5
    assert estimator.idle_seconds() == pytest.approx(10.5)  # still the user's idle time
    clock.now += 1.0
    clock.last_hid = clock.now - 0.2  # now the user moved the mouse
    assert estimator.idle_seconds() == pytest.approx(0.2)
    estimator.before_post()  # notices the user's activity
    clock.last_hid = clock.now
    estimator.after_post()
    clock.now += 0.3
    assert estimator.idle_seconds() == pytest.approx(0.5)


def test_idle_estimator_when_posts_do_not_reset_hid_idle() -> None:
    clock = FakeClock()
    estimator = h.IdleEstimator(clock.idle, lambda: clock.now)
    estimator.before_post()
    estimator.after_post()
    clock.now += 2.0
    assert estimator.idle_seconds() == pytest.approx(12.0)


# =========================================================================== screenshots


def test_capture_plan_display_window_and_rect() -> None:
    displays = [MAIN, LEFT]
    plan = h.capture_plan(displays, max_edge=1280)
    assert plan.display is MAIN
    assert plan.region == MAIN.frame
    assert (plan.width, plan.height) == (1280, 800)
    assert plan.source_rect is None
    left = h.capture_plan(displays, display_id=2, max_edge=4000)
    assert left.region == LEFT.frame
    assert (left.width, left.height) == (1920, 1080)
    window = h.capture_plan(displays, window_frame=Rect(-500, 0, 400, 300), max_edge=4000)
    assert window.display is LEFT
    assert (window.width, window.height) == (400, 300)
    rect = h.capture_plan(displays, rect=Rect(1400, 850, 100, 100), max_edge=4000)
    assert rect.region == Rect(1400, 850, 40, 50)
    assert rect.source_rect == Rect(1400, 850, 40, 50)
    assert (rect.width, rect.height) == (80, 100)
    on_left = h.capture_plan(displays, rect=Rect(-1000, -100, 200, 100), max_edge=4000)
    assert on_left.source_rect == Rect(920, 80, 200, 100)


def test_capture_plan_errors() -> None:
    expect(ErrorCode.DISPLAY_NOT_FOUND, h.capture_plan, [MAIN], display_id=99)
    expect(ErrorCode.DISPLAY_NOT_FOUND, h.capture_plan, [])
    expect(ErrorCode.INVALID_ARGUMENT, h.capture_plan, [MAIN], rect=Rect(0, 0, 0, 10))
    expect(ErrorCode.INVALID_ARGUMENT, h.capture_plan, [MAIN], rect=Rect(5000, 5000, 10, 10))


def test_resolve_languages() -> None:
    supported = ["en-US", "fr-FR", "zh-Hans", "de-DE"]
    assert h.resolve_languages(["en", "FR-fr", "zh_Hans", "en-US"], supported) == [
        "en-US",
        "fr-FR",
        "zh-Hans",
    ]
    error = expect(ErrorCode.INVALID_ARGUMENT, h.resolve_languages, ["xx", "de"], supported)
    assert "xx" in error.message
    assert error.hint is not None
    assert "de-DE" in error.hint


# =========================================================================== hit testing


@dataclass(frozen=True)
class Win:
    pid: int
    owner: str
    frame: Rect
    layer: int = 0
    alpha: float = 1.0


def test_hit_test() -> None:
    displays = [MAIN]
    overlay = Win(10, "ScreenTool", Rect(0, 0, 1440, 900), layer=25)
    menu_bar = Win(20, "Window Server", Rect(0, 0, 1440, 25), layer=24)
    popover = Win(30, "1Password", Rect(1000, 25, 300, 400), layer=101)
    invisible = Win(40, "Ghost", Rect(0, 0, 1440, 900), alpha=0.0)
    mine = Win(os.getpid(), "AXPilot", Rect(0, 0, 1440, 900), layer=0)
    window = Win(50, "Notes", Rect(0, 25, 1200, 800))
    stack = [overlay, menu_bar, popover, invisible, mine, window]
    kwargs: dict[str, Any] = {"own_pid": os.getpid(), "displays": displays}
    assert h.hit_test(stack, Point(500, 10), **kwargs) == "frontmost"
    assert h.hit_test(stack, Point(1100, 100), **kwargs) is popover
    assert h.hit_test(stack, Point(100, 100), **kwargs) is window
    assert h.hit_test(stack, Point(1300, 880), **kwargs) is None


# ======================================================================== macOS smoke tests


@pytest.fixture(scope="module")
def engine() -> Iterator[Any]:
    from axpilot.engine import create_engine

    eng = create_engine("macos")
    yield eng
    eng.close()


@pytest.fixture(scope="module")
def accessibility(engine: Any) -> None:
    if not engine.permissions().accessibility:
        pytest.skip("Accessibility is not granted to the app running pytest")


@pytest.fixture(scope="module")
def screen_recording(engine: Any) -> None:
    if not engine.permissions().screen_recording:
        pytest.skip("Screen Recording is not granted to the app running pytest")


@pytest.fixture(scope="module")
def finder(engine: Any) -> Any:
    return engine.find_app("com.apple.finder")


@pytest.mark.macos
@mac_only
class TestMacEngine:
    def test_is_the_mac_engine(self, engine: Any) -> None:
        assert engine.name == "macos"

    def test_permissions_and_version(self, engine: Any) -> None:
        status = engine.permissions()
        assert isinstance(status.accessibility, bool)
        assert isinstance(status.screen_recording, bool)
        assert all(isinstance(v, bool) for v in status.automation.values())
        major = int(engine.os_version().split(".")[0])
        assert major >= 14

    def test_list_apps_and_frontmost(self, engine: Any, finder: Any) -> None:
        apps = engine.list_apps()
        assert any(a.bundle_id == "com.apple.finder" for a in apps)
        assert all(a.pid > 0 for a in apps)
        assert sum(a.frontmost for a in apps) <= 1
        assert finder.name
        front = engine.frontmost_app()
        assert front is None or front.pid > 0
        installed = engine.list_apps(include_installed=True)
        assert any(a.pid == 0 for a in installed)

    def test_find_app_errors(self, engine: Any) -> None:
        expect(ErrorCode.APP_NOT_FOUND, engine.find_app, "no such app 7f3a")

    def test_list_displays(self, engine: Any) -> None:
        displays = engine.list_displays()
        assert displays
        assert displays[0].main
        assert (displays[0].frame.x, displays[0].frame.y) == (0, 0)
        for d in displays:
            assert d.scale >= 1.0
            assert d.frame.intersection(d.work_area) is not None

    def test_list_windows(self, engine: Any) -> None:
        windows = engine.list_windows()
        ids = [w.window_id for w in windows]
        assert len(ids) == len(set(ids))
        for w in windows:
            assert w.pid > 0
            assert w.frame.w > 0
        assert sum(w.focused for w in windows) <= 1

    def test_cursor_idle_and_secure_input(self, engine: Any) -> None:
        position = engine.cursor_position()
        assert isinstance(position, Point)
        assert engine.hid_idle_seconds() >= 0
        assert isinstance(engine.secure_input_active(), bool)

    def test_app_at_point(self, engine: Any) -> None:
        app = engine.app_at_point(Point(10, 5))  # the menu bar → the frontmost app
        assert app is None or app.pid > 0

    def test_keyboard_layout_map(self) -> None:
        from axpilot.engine.macos.carbon import LayoutProvider

        layout = LayoutProvider().current()  # pytest runs tests on the main thread
        assert not layout.fallback, "the real layout could not be read"
        assert layout.lookup("a") is not None
        if layout.source_id == "com.apple.keylayout.US":
            assert layout.lookup("a") == (0x00, False, False)
            assert layout.lookup("?") == (0x2C, True, False)

    def test_capture_tree_finder(self, engine: Any, accessibility: None, finder: Any) -> None:
        from axpilot.perception import build_snapshot

        tree = engine.capture_tree(app="com.apple.finder", include_menus=True)
        assert tree.app.pid == finder.pid
        assert tree.root.role in ("AXWindow", "AXApplication")
        assert tree.node_count >= 1
        assert tree.menu_bar is not None
        assert tree.menu_bar.role == "AXMenuBar"
        assert not any(n.secure and n.value is not None for n in tree.root.walk())
        snapshot = build_snapshot(tree, snapshot_id="s_test")
        assert snapshot.app.pid == finder.pid
        assert engine.element_valid(tree.root.handle)
        again = engine.refresh_element(tree.root.handle)
        assert again.role == tree.root.role
        assert again.children == []
        attributes = engine.element_attributes(tree.root.handle)
        assert attributes["AXRole"] == tree.root.role
        assert "AXActionNames" in attributes
        assert not engine.element_valid("not an element")

    def test_capture_tree_budget(self, engine: Any, accessibility: None) -> None:
        tree = engine.capture_tree(app="com.apple.finder", max_nodes=3)
        assert tree.node_count <= 3
        expect(ErrorCode.INVALID_ARGUMENT, engine.capture_tree, max_nodes=0)

    def test_menu_bar_finder(self, engine: Any, accessibility: None) -> None:
        bar = engine.menu_bar("com.apple.finder", depth=2)
        titles = [m.title for m in bar.children]
        assert len(titles) > 3  # Apple menu, Finder, File, Edit… (titles are localized)
        assert all(titles)
        shortcuts = [i.shortcut for m in bar.children for i in m.children if i.shortcut]
        assert shortcuts, "Finder menus have keyboard shortcuts"
        assert all(parse_shortcut(s) is not None for s in shortcuts)
        expect(ErrorCode.MENU_NOT_FOUND, engine.menu_select, "com.apple.finder", ["No Such Menu 7f3a"])

    def test_screenshot_and_ocr(self, engine: Any, screen_recording: None) -> None:
        capture = engine.screenshot(max_edge=800, fmt="png")
        assert capture.data.startswith(h.PNG_SIGNATURE)
        assert max(capture.width, capture.height) <= 800
        assert capture.region == engine.list_displays()[0].frame
        jpeg = engine.screenshot(rect=Rect(0, 0, 400, 200), max_edge=4000, fmt="jpeg", quality=60)
        assert jpeg.data[:2] == b"\xff\xd8"
        assert jpeg.region == Rect(0, 0, 400, 200)
        lines = engine.ocr(capture)
        assert all(line.frame.w >= 0 for line in lines)

    @pytest.mark.skipif(not MUTATE, reason="changes the clipboard; set AXPILOT_MACOS_TESTS_MUTATE=1")
    def test_clipboard_roundtrip(self, engine: Any) -> None:
        saved = engine.clipboard_get()
        try:
            engine.clipboard_set(text="AXPilot clipboard test ✓")
            assert engine.clipboard_get().text == "AXPilot clipboard test ✓"
        finally:
            engine.clipboard_set(text=saved.text, image_png=saved.image_png, files=saved.files or None)


@pytest.mark.macos
@mac_only
def test_the_run_loop_pump_lets_workers_read_the_keyboard_layout() -> None:
    """M1: a worker asks the main thread (AppHelper.callAfter) for the layout; that only works
    while something turns the main run loop."""
    from axpilot.engine.macos.carbon import LayoutProvider
    from axpilot.engine.macos.runloop import pump
    from axpilot.runloop import main_thread_pump, wait_pumping

    assert main_thread_pump() is pump
    pump(0.0)  # never blocks
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        started = time.monotonic()
        layout = wait_pumping(pool.submit(LayoutProvider().current), pump)
    assert not layout.fallback, "a worker thread fell back to US-ANSI although the main thread pumped"
    assert time.monotonic() - started < 0.5, "the worker waited for the main thread's timeout"
