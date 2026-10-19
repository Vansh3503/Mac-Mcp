"""Coordinate spaces for pointer tools.

Tools accept coordinates in one of two spaces:

* ``"points"``: global screen points (the public default; see :mod:`axpilot.models`).
* ``"screenshot"``: pixels of a screenshot the model was shown. They are mapped back to
  screen points through that :class:`~axpilot.models.Capture`'s region and scale, so a
  vision model can click exactly what it saw, even on a downscaled Retina capture or a
  display with negative coordinates.
"""

from __future__ import annotations

import math
from typing import Literal

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import Capture, Point

__all__ = ["COORDINATE_SPACES", "CoordinateSpace", "to_screen_point"]

CoordinateSpace = Literal["points", "screenshot"]
COORDINATE_SPACES: tuple[CoordinateSpace, ...] = ("points", "screenshot")


def _require_finite(x: float, y: float) -> None:
    if not (math.isfinite(x) and math.isfinite(y)):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Coordinates must be finite numbers, got ({x}, {y}).",
        )


def _screenshot_to_points(x: float, y: float, capture: Capture) -> Point:
    if not (0 <= x <= capture.width and 0 <= y <= capture.height):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"({x:g}, {y:g}) is outside the {capture.width}×{capture.height} screenshot.",
            hint="Screenshot coordinates are pixels from the image's top-left corner.",
            details={"width": capture.width, "height": capture.height},
        )
    return capture.to_points(x, y)


def to_screen_point(
    x: float,
    y: float,
    space: CoordinateSpace,
    capture: Capture | None,
) -> Point:
    """Convert ``(x, y)`` given in ``space`` to global screen points.

    ``space="points"`` passes the values through. ``space="screenshot"`` maps image pixels
    of ``capture`` (which must be given) back to points; pixels outside the image are
    rejected. Raises ``INVALID_ARGUMENT`` for an unknown space, a missing capture, or
    non-finite coordinates.
    """
    _require_finite(x, y)
    if space == "points":
        return Point(float(x), float(y))
    if space == "screenshot":
        if capture is None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "coordinate_space='screenshot' needs the screenshot the coordinates refer to.",
                hint="Pass the `screenshot_id` returned by `screenshot`, or use coordinate_space='points'.",
            )
        return _screenshot_to_points(x, y, capture)
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown coordinate space {space!r}.",
        hint=f"Use one of: {', '.join(COORDINATE_SPACES)}.",
    )
