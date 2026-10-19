"""Observe tools: success paths, the error matrix and fault injection (QUALITY.md B2)."""

from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp import Client
from PIL import Image

from axpilot.engine.fake import FakeEngine
from axpilot.errors import ErrorCode
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio


@pytest.fixture
def ctx() -> ServerContext:
    return make_test_context({"tools.profile": "full"})


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


async def call(client: Client, tool: str, args: dict[str, Any] | None = None) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args or {})
    return result, error_code_of(result)


def images(result: Any) -> list[Any]:
    return [block for block in result.content if getattr(block, "type", "") == "image"]


# =========================================================================== snapshot
async def test_snapshot_defaults_to_the_frontmost_app(client: Client) -> None:
    result, error = await call(client, "snapshot")
    assert error is None
    data = structured(result)
    assert data["app"]["name"] == "Finder"
    assert data["snapshot_id"].startswith("s_")
    assert text_of(result).startswith("app Finder (com.apple.finder, pid 412)")


async def test_snapshot_lists_interactive_elements_with_refs_and_centers(client: Client) -> None:
    result, error = await call(client, "snapshot", {"app": "Notes"})
    assert error is None
    data = structured(result)
    refs = [el["ref"] for el in data["elements"]]
    assert refs[:3] == ["e1", "e2", "e3"]
    assert len(refs) == len(set(refs))
    new_note = next(el for el in data["elements"] if el["name"] == "New Note")
    assert new_note["role"] == "button"
    assert f"{new_note['ref']}" in text_of(result)
    assert data["focused_ref"] is not None


async def test_snapshot_paging_with_offset_and_max_elements(client: Client) -> None:
    first, _ = await call(client, "snapshot", {"app": "Notes", "max_elements": 5})
    second, _ = await call(client, "snapshot", {"app": "Notes", "max_elements": 5, "offset": 5})
    page1, page2 = structured(first), structured(second)
    assert [el["ref"] for el in page1["elements"]] == ["e1", "e2", "e3", "e4", "e5"]
    assert page2["elements"][0]["ref"] == "e6"
    assert page1["next_offset"] == 5
    assert "offset=5" in text_of(first)


@pytest.mark.parametrize("mode", ["interactive", "full", "text"])
async def test_ax_modes_are_reported_as_requested(client: Client, mode: str) -> None:
    result, error = await call(client, "snapshot", {"app": "Notes", "mode": mode})
    assert error is None
    data = structured(result)
    assert data["mode"] == mode
    assert not any(el["ref"].startswith("o") for el in data["elements"])


async def test_auto_mode_stays_ax_only_for_apps_with_good_accessibility(client: Client) -> None:
    result, _ = await call(client, "snapshot", {"app": "Notes"})
    data = structured(result)
    assert data["ax_quality"] == "good"
    assert data["app_kind"] == "native"
    assert not any(el["ref"].startswith("o") for el in data["elements"])


async def test_vision_mode_adds_ocr_elements_that_inspect_can_resolve(client: Client) -> None:
    result, _ = await call(client, "snapshot", {"app": "Notes", "mode": "vision"})
    data = structured(result)
    ocr_refs = [el["ref"] for el in data["elements"] if el["ref"].startswith("o")]
    assert ocr_refs
    assert ocr_refs[0] == "o1"
    ax_refs = [el["ref"] for el in data["elements"] if el["ref"].startswith("e")]
    assert data["elements"][0]["ref"] == ax_refs[0]  # AX elements come first
    inspect, error = await call(client, "inspect", {"ref": ocr_refs[0], "snapshot_id": data["snapshot_id"]})
    assert error is None, text_of(inspect)


async def test_auto_mode_falls_back_to_vision_for_canvas_apps(ctx: ServerContext, client: Client) -> None:
    ctx.engine.app_control("launch", "Canvas Paint")
    result, error = await call(client, "snapshot", {"app": "Canvas Paint"})
    assert error is None
    data = structured(result)
    assert data["ax_quality"] == "poor"
    assert any(el["ref"].startswith("o") for el in data["elements"])


async def test_snapshot_with_an_annotated_screenshot_returns_an_image(client: Client) -> None:
    result, error = await call(
        client, "snapshot", {"app": "Notes", "include_screenshot": True, "annotate": True}
    )
    assert error is None
    (image,) = images(result)
    decoded = Image.open(io.BytesIO(base64.b64decode(image.data)))
    assert decoded.width > 0
    assert decoded.height > 0


async def test_snapshot_without_screen_recording_degrades_with_a_warning() -> None:
    ctx = make_test_context()
    ctx.engine.set_permissions(screen_recording=False)  # type: ignore[attr-defined]
    async with connected_client(ctx) as client:
        result, error = await call(client, "snapshot", {"app": "Notes", "include_screenshot": True})
    assert error is None
    assert images(result) == []
    warnings = structured(result).get("warnings") or []
    assert warnings, "a skipped screenshot must be surfaced as a warning (QUALITY.md A8)"
    assert any("screen" in warning.lower() for warning in warnings)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"app": "Nopes"}, ErrorCode.APP_NOT_FOUND),
        ({"window_id": 99999}, ErrorCode.WINDOW_NOT_FOUND),
        ({"app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
        ({"app": "com.1password.1password"}, ErrorCode.BLOCKED_BY_POLICY),
        ({"app": "System Settings"}, ErrorCode.BLOCKED_BY_POLICY),  # Privacy & Security pane
        ({"app": "Notes", "max_elements": 0}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "Notes", "offset": -1}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "Notes", "mode": "telepathy"}, ErrorCode.INVALID_ARGUMENT),
        ({"snapshot_id": "s_zzzz"}, None),  # accepted as a hint; unknown ids are not fatal
    ],
)
async def test_snapshot_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode | None) -> None:
    result, error = await call(client, "snapshot", args)
    if code is None:
        assert error in (None, "SNAPSHOT_NOT_FOUND")
    else:
        assert error == code.value, text_of(result)
        assert "Hint:" in text_of(result)


async def test_snapshot_policy_errors_name_the_app_and_rule(client: Client) -> None:
    result, _ = await call(client, "snapshot", {"app": "1Password"})
    text = text_of(result)
    assert "1Password" in text
    assert "deny_apps" in text


async def test_snapshot_without_accessibility_permission() -> None:
    ctx = make_test_context()
    ctx.engine.set_permissions(accessibility=False)  # type: ignore[attr-defined]
    async with connected_client(ctx) as client:
        result, error = await call(client, "snapshot", {"app": "Notes"})
        status, status_error = await call(client, "status")
    assert error == "PERMISSION_ACCESSIBILITY"
    assert "System Settings" in text_of(result)
    assert status_error is None  # status still works and explains what is missing
    assert "MISSING" in text_of(status)


# =========================================================================== screenshot
async def test_screenshot_of_the_main_display_maps_pixels_to_points(client: Client) -> None:
    result, error = await call(client, "screenshot", {"max_edge": 640})
    assert error is None
    data = structured(result)
    (image,) = images(result)
    decoded = Image.open(io.BytesIO(base64.b64decode(image.data)))
    assert max(decoded.size) <= 640
    assert data["screenshot_id"].startswith("c_")
    assert "screenshot_id" in text_of(result)


async def test_screenshot_of_a_rect_and_png_format(client: Client) -> None:
    result, error = await call(client, "screenshot", {"rect": [0, 0, 100, 100], "format": "png"})
    assert error is None
    (image,) = images(result)
    assert image.mime_type == "image/png"


async def test_screenshots_leave_deny_listed_apps_out(ctx: ServerContext, client: Client) -> None:
    await call(client, "screenshot", {})
    engine = ctx.engine
    assert isinstance(engine, FakeEngine)
    shots = [payload for name, payload in engine.events if name == "screenshot"]
    assert shots
    excluded = set(shots[-1].get("exclude_bundle_ids") or ())
    assert "com.1password.1password" in excluded


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"window_id": 99999}, ErrorCode.WINDOW_NOT_FOUND),
        ({"display_id": 12345}, ErrorCode.DISPLAY_NOT_FOUND),
        ({"rect": [0, 0, -5, 100]}, ErrorCode.INVALID_ARGUMENT),
        ({"rect": [0, 0, 0, 0]}, ErrorCode.INVALID_ARGUMENT),
        ({"app": "1Password"}, ErrorCode.BLOCKED_BY_POLICY),
        ({"format": "gif"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_screenshot_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "screenshot", args)
    assert error == code.value, text_of(result)


async def test_screenshot_without_permission() -> None:
    ctx = make_test_context()
    ctx.engine.set_permissions(screen_recording=False)  # type: ignore[attr-defined]
    async with connected_client(ctx) as client:
        result, error = await call(client, "screenshot")
        _ocr, ocr_error = await call(client, "ocr")
    assert error == "PERMISSION_SCREEN_RECORDING"
    assert "Screen Recording" in text_of(result)
    assert ocr_error == "PERMISSION_SCREEN_RECORDING"


# =========================================================================== inspect
async def test_inspect_shows_attributes_of_a_ref(client: Client) -> None:
    snap, _ = await call(client, "snapshot", {"app": "Notes"})
    result, error = await call(
        client, "inspect", {"ref": "e1", "snapshot_id": structured(snap)["snapshot_id"]}
    )
    assert error is None
    assert "AXRole" in text_of(result)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"ref": "e1", "snapshot_id": "s_zzzz"}, ErrorCode.SNAPSHOT_NOT_FOUND),
        ({"ref": "zz"}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e9999"}, ErrorCode.STALE_REF),
        ({}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_inspect_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await call(client, "snapshot", {"app": "Notes"})
    result, error = await call(client, "inspect", args)
    assert error == code.value, text_of(result)


async def test_inspect_before_any_snapshot_explains_what_to_do() -> None:
    ctx = make_test_context()
    async with connected_client(ctx) as client:
        result, error = await call(client, "inspect", {"ref": "e1"})
    assert error in ("SNAPSHOT_NOT_FOUND", "STALE_REF")
    assert "snapshot" in text_of(result).lower()


# =========================================================================== ocr / lists
async def test_ocr_reads_text_with_frames(client: Client) -> None:
    result, error = await call(client, "ocr", {"app": "Notes"})
    assert error is None
    data = structured(result)
    assert data["lines"]
    assert all("frame" in line or "center" in line for line in data["lines"])


async def test_list_apps_marks_policy_blocked_apps(client: Client) -> None:
    result, error = await call(client, "list_apps", {"include_installed": True})
    assert error is None
    text = text_of(result)
    assert "blocked by policy" in text
    assert "installed" in text


async def test_list_windows_and_displays(client: Client) -> None:
    windows, error = await call(client, "list_windows", {"app": "Notes"})
    assert error is None
    assert "Notes" in text_of(windows)
    _missing, missing_error = await call(client, "list_windows", {"app": "Nopes"})
    assert missing_error == "APP_NOT_FOUND"
    displays, displays_error = await call(client, "list_displays")
    assert displays_error is None
    assert len(structured(displays)["displays"]) == 2


# =========================================================================== wait_for
@pytest.mark.parametrize(
    "args",
    [
        {"condition": "element", "query": "New Note", "app": "Notes"},
        {"condition": "app_running", "query": "Notes"},
        {"condition": "window", "query": "Notes"},
        {"condition": "duration", "timeout_ms": 20},
    ],
)
async def test_wait_for_conditions_that_already_hold_return_at_once(
    client: Client, args: dict[str, Any]
) -> None:
    result, error = await call(client, "wait_for", {"timeout_ms": 500, **args})
    assert error is None, text_of(result)


async def test_wait_for_idle_needs_a_quiet_period(client: Client) -> None:
    result, error = await call(client, "wait_for", {"condition": "idle", "app": "Notes", "timeout_ms": 2000})
    assert error is None
    assert "unchanged" in text_of(result)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (
            {"condition": "element", "query": "Does not exist", "app": "Notes", "timeout_ms": 150},
            ErrorCode.TIMEOUT,
        ),
        (
            {"condition": "element_gone", "query": "New Note", "app": "Notes", "timeout_ms": 150},
            ErrorCode.TIMEOUT,
        ),
        ({"condition": "app_running", "query": "Calculator", "timeout_ms": 150}, ErrorCode.TIMEOUT),
        ({"condition": "element", "timeout_ms": 150}, ErrorCode.INVALID_ARGUMENT),
        ({"condition": "duration", "timeout_ms": 70000}, ErrorCode.INVALID_ARGUMENT),
        ({"condition": "teleport"}, ErrorCode.INVALID_ARGUMENT),
    ],
)
async def test_wait_for_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    result, error = await call(client, "wait_for", args)
    assert error == code.value, text_of(result)
    if code is ErrorCode.TIMEOUT:
        assert "snapshot" in text_of(result).lower()


# =========================================================================== status
async def test_status_reports_version_permissions_profile_and_tools(client: Client) -> None:
    result, error = await call(client, "status")
    assert error is None
    data = structured(result)
    text = text_of(result)
    assert "axpilot" in text
    assert "permissions" in text
    assert data["profile"] == "full"
    assert "snapshot" in data["enabled_tools"]


async def test_status_shows_the_kill_switch(ctx: ServerContext, client: Client) -> None:
    ctx.control.pause("kill switch")
    result, error = await call(client, "status")
    assert error is None
    assert "paused" in text_of(result).lower()


# =========================================================================== fault injection
OBSERVE_CALLS: list[tuple[str, dict[str, Any], str]] = [
    ("snapshot", {"app": "Notes"}, "capture_tree"),
    ("screenshot", {}, "screenshot"),
    ("ocr", {"app": "Notes"}, "ocr"),
    ("list_apps", {}, "list_apps"),
    ("list_windows", {}, "list_windows"),
    ("list_displays", {}, "list_displays"),
]


@pytest.mark.parametrize(("tool", "args", "method"), OBSERVE_CALLS)
async def test_engine_crashes_become_internal_and_the_server_survives(
    tool: str, args: dict[str, Any], method: str
) -> None:
    faulty = FaultyEngine(FakeEngine.demo(), {method: Fault.crash("secret detail /Users/x")})
    ctx = make_test_context({"tools.profile": "full"}, engine=faulty)
    async with connected_client(ctx) as client:
        result, error = await call(client, tool, args)
        faulty.clear_faults()
        _again, again_error = await call(client, tool, args)
    assert error == "INTERNAL"
    assert "secret detail" not in text_of(result)
    assert again_error is None
    assert ctx.audit.tail(2)[0]["error_code"] == "INTERNAL"


@pytest.mark.parametrize(("tool", "args", "method"), OBSERVE_CALLS[:3])
async def test_hung_engine_calls_time_out(tool: str, args: dict[str, Any], method: str) -> None:
    faulty = FaultyEngine(FakeEngine.demo(), {method: Fault.hang(10)})
    ctx = make_test_context({"tools.profile": "full"}, engine=faulty)
    ctx.engine_timeout_s = 0.1
    ctx.capture_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            result, error = await call(client, tool, args)
    finally:
        faulty.release()
    assert error == "TIMEOUT", text_of(result)


@pytest.mark.parametrize(("tool", "args", "method"), OBSERVE_CALLS[:2])
async def test_engine_errors_keep_their_codes(tool: str, args: dict[str, Any], method: str) -> None:
    fault = Fault.error(ErrorCode.PERMISSION_ACCESSIBILITY, "AX is disabled", hint="Grant it.")
    faulty = FaultyEngine(FakeEngine.demo(), {method: fault})
    ctx = make_test_context({"tools.profile": "full"}, engine=faulty)
    async with connected_client(ctx) as client:
        result, error = await call(client, tool, args)
    assert error == "PERMISSION_ACCESSIBILITY"
    assert "Grant it." in text_of(result)
