"""Geometry helpers of the platform-independent models."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from axpilot.models import Point, Rect

coords = st.floats(min_value=-5000, max_value=5000, allow_nan=False)
sizes = st.floats(min_value=0, max_value=3000, allow_nan=False)
rects = st.builds(Rect, coords, coords, sizes, sizes)


def test_union_spans_both_rectangles() -> None:
    assert Rect(0, 0, 10, 10).union(Rect(20, 5, 5, 30)) == Rect(0, 0, 25, 35)
    assert Rect(5, 5, 1, 1).union(Rect(5, 5, 1, 1)) == Rect(5, 5, 1, 1)


@given(rects, rects)
def test_union_contains_both_and_is_symmetric(a: Rect, b: Rect) -> None:
    union = a.union(b)
    assert union == b.union(a)
    for rect in (a, b):
        assert union.x <= rect.x
        assert union.y <= rect.y
        assert union.x + union.w >= rect.x + rect.w - 1e-6
        assert union.y + union.h >= rect.y + rect.h - 1e-6


def test_intersection_and_contains() -> None:
    a, b = Rect(0, 0, 10, 10), Rect(5, 5, 10, 10)
    assert a.intersection(b) == Rect(5, 5, 5, 5)
    assert a.intersection(Rect(10, 0, 5, 5)) is None
    assert a.contains(Point(0, 0))
    assert not a.contains(Point(10, 5))
