"""Dates and times for date pickers: what an agent writes → one ISO 8601 form.

Date pickers (``AXDateTimeArea``, ``AXDateField``, ``AXTimeField``) hold an ``NSDate``. Agents
send text, so :func:`parse_date_input` accepts the forms people and models use (ISO dates, with
or without a time; a time alone for time fields) and returns an ISO 8601 string with the local
UTC offset, which engines turn back into a date (``datetime.fromisoformat``).
"""

from __future__ import annotations

import datetime as dt
import re

from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["parse_date_input"]

_TIME_ONLY = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_HINT = 'Use ISO 8601: "2026-10-20", "2026-10-20 14:30" or, for a time field, "14:30".'


def _local(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone() if moment.tzinfo is not None else moment.astimezone(None)


def _current(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return _local(dt.datetime.fromisoformat(value))
    except ValueError:
        return None


def parse_date_input(text: str, current: str | None = None) -> str:
    """``text`` as an ISO 8601 date-time with the local UTC offset.

    A date alone keeps the time of ``current`` (the picker's ISO value), else midnight; a time
    alone keeps the date of ``current``, else today. A time without a zone is local time.
    Raises ``INVALID_ARGUMENT`` for anything else."""
    raw = str(text).strip()
    now = _current(current)
    time_only = _TIME_ONLY.match(raw)
    try:
        if time_only:
            hour, minute, second = (int(part) if part else 0 for part in time_only.groups())
            base = now or dt.datetime.now().astimezone()
            moment = base.replace(hour=hour, minute=minute, second=second, microsecond=0)
        else:
            moment = dt.datetime.fromisoformat(raw.replace(" ", "T", 1) if " " in raw else raw)
            if len(raw) == 10:  # a date alone
                keep = now.timetz() if now is not None else dt.time()
                moment = dt.datetime.combine(moment.date(), keep.replace(tzinfo=None))
    except ValueError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"{raw!r} is not a date or time I can read.", hint=_HINT
        ) from exc
    return _local(moment).isoformat()
