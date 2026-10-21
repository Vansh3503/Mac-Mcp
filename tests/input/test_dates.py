"""Date-picker input: the forms agents send become one ISO 8601 form with the local offset."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.dates import date_part_digits, date_part_of, has_time, parse_date_input


@pytest.fixture(autouse=True)
def berlin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    ("text", "current", "expected"),
    [
        ("2026-10-20", None, "2026-10-20T00:00:00+02:00"),
        ("2026-10-20", "2026-01-05T09:15:00+01:00", "2026-10-20T09:15:00+02:00"),  # keeps the time
        ("2026-12-24 18:00", None, "2026-12-24T18:00:00+01:00"),
        ("2026-10-20T14:30", None, "2026-10-20T14:30:00+02:00"),
        ("2026-10-20T14:30:00+00:00", None, "2026-10-20T16:30:00+02:00"),  # converted to local time
        ("14:30", "2026-03-01T08:00:00+01:00", "2026-03-01T14:30:00+01:00"),  # keeps the date
        ("9:05:30", "2026-03-01T08:00:00+01:00", "2026-03-01T09:05:30+01:00"),
        ("  2026-10-20  ", "not a date", "2026-10-20T00:00:00+02:00"),
    ],
)
def test_parse_date_input(text: str, current: str | None, expected: str) -> None:
    assert parse_date_input(text, current) == expected


def test_a_time_alone_without_a_current_value_is_today() -> None:
    assert parse_date_input("07:45").endswith("T07:45:00+02:00") or parse_date_input("07:45").endswith(
        "T07:45:00+01:00"
    )


@pytest.mark.parametrize("text", ["next tuesday", "25:00", "2026-13-01", "20/10/2026", ""])
def test_unreadable_dates_are_refused(text: str) -> None:
    with pytest.raises(AXPilotError) as caught:
        parse_date_input(text)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert "ISO 8601" in (caught.value.hint or "")


@pytest.mark.parametrize(
    ("labels", "part"),
    [
        (("Month", "mm"), "month"),
        (("Day",), "day"),
        ((None, "yyyy"), "year"),
        (("Tag", "tt"), "day"),  # German: tt.mm.jjjj
        ((None, "jjjj"), "year"),
        ((None, "jj"), "day"),  # French: jj/mm/aaaa
        ((None, "aaaa"), "year"),
        (("Año",), "year"),
        (("Hours",), None),
        (("mm/dd/yyyy",), None),  # names several parts
        ((None, None), None),
    ],
)
def test_date_part_of(labels: tuple[str | None, ...], part: str | None) -> None:
    assert date_part_of(*labels) == part


def test_date_part_digits_are_zero_padded() -> None:
    iso = parse_date_input("2026-03-05")
    assert [date_part_digits(iso, part) for part in ("month", "day", "year")] == ["03", "05", "2026"]


@pytest.mark.parametrize(
    ("text", "timed"),
    [("2026-10-20", False), ("2026-10-20 14:30", True), ("2026-10-20T14:30", True), ("14:30", True)],
)
def test_has_time(text: str, timed: bool) -> None:
    assert has_time(text) is timed
