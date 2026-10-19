from __future__ import annotations

import math

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.coords import to_screen_point
from axpilot.models import Capture, Point, Rect


def make_capture(region: Rect, width: int, height: int) -> Capture:
    return Capture(data=b"", format="png", width=width, height=height, region=region)


def test_points_pass_through() -> None:
    assert to_screen_point(10.5, -20, "points", None) == Point(10.5, -20.0)


def test_points_ignore_a_capture() -> None:
    capture = make_capture(Rect(0, 0, 100, 100), 50, 50)
    assert to_screen_point(10, 20, "points", capture) == Point(10, 20)


def test_screenshot_requires_a_capture() -> None:
    with pytest.raises(AXPilotError) as info:
        to_screen_point(1, 1, "screenshot", None)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert info.value.hint is not None
    assert "screenshot_id" in info.value.hint


def test_downscaled_retina_capture_maps_back_to_points() -> None:
    # A 1440x900-point display captured at 2x and downscaled to 1280x800 pixels.
    capture = make_capture(Rect(0, 0, 1440, 900), 1280, 800)
    assert to_screen_point(640, 400, "screenshot", capture) == Point(720, 450)
    assert to_screen_point(0, 0, "screenshot", capture) == Point(0, 0)


def test_capture_with_negative_origin() -> None:
    # A secondary display left of the main one, captured at half size.
    capture = make_capture(Rect(-1920, -200, 1920, 1080), 960, 540)
    assert to_screen_point(480, 270, "screenshot", capture) == Point(-960, 340)


def test_window_capture_offset() -> None:
    capture = make_capture(Rect(100, 50, 800, 600), 800, 600)
    assert to_screen_point(10, 20, "screenshot", capture) == Point(110, 70)


def test_image_edges_are_inclusive() -> None:
    capture = make_capture(Rect(0, 0, 200, 100), 100, 50)
    assert to_screen_point(100, 50, "screenshot", capture) == Point(200, 100)


@pytest.mark.parametrize(("x", "y"), [(-1, 0), (0, -0.5), (101, 0), (0, 51)])
def test_pixels_outside_the_image_are_rejected(x: float, y: float) -> None:
    capture = make_capture(Rect(0, 0, 200, 100), 100, 50)
    with pytest.raises(AXPilotError) as info:
        to_screen_point(x, y, "screenshot", capture)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "100×50" in info.value.message


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("space", ["points", "screenshot"])
def test_non_finite_coordinates_are_rejected(bad: float, space: str) -> None:
    capture = make_capture(Rect(0, 0, 100, 100), 100, 100)
    with pytest.raises(AXPilotError):
        to_screen_point(bad, 1, space, capture)  # type: ignore[arg-type]
    with pytest.raises(AXPilotError):
        to_screen_point(1, bad, space, capture)  # type: ignore[arg-type]


def test_unknown_space_is_rejected() -> None:
    with pytest.raises(AXPilotError) as info:
        to_screen_point(1, 1, "pixels", None)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "screenshot" in (info.value.hint or "")
