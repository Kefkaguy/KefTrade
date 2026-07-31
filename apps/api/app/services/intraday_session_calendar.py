"""Regular-session bar calendar for 15m/30m intraday research.

Frozen snapshots contain extended-hours candles.  ``session[0]`` is therefore
not the opening half hour and ``session[-1]`` is not the closing half hour: on
any day carrying a premarket or post-close bar those positions silently
measure a 09:00 bar against a 16:30 bar.  Every factor definition that names a
session position must resolve it through this module instead of relying on the
ordering of whatever rows the snapshot happens to hold.

The module contains no research verdict, no campaign code, and no UI code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

SESSION_CALENDAR_VERSION = "intraday_session_calendar_v1"
NEW_YORK = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
TIMEFRAME_MINUTES = {"15m": 15, "30m": 30}


def timeframe_minutes(timeframe: str) -> int:
    try:
        return TIMEFRAME_MINUTES[timeframe]
    except KeyError as error:
        raise ValueError(f"unsupported intraday timeframe {timeframe!r}") from error


def _slots_between(start: time, end: time, minutes: int) -> tuple[str, ...]:
    anchor = datetime(2001, 1, 1, start.hour, start.minute)
    limit = datetime(2001, 1, 1, end.hour, end.minute)
    slots: list[str] = []
    while anchor < limit:
        slots.append(anchor.strftime("%H:%M"))
        anchor += timedelta(minutes=minutes)
    return tuple(slots)


def regular_session_slots(timeframe: str) -> tuple[str, ...]:
    """Exchange-local start times of every regular-hours bar on a full day."""
    return _slots_between(REGULAR_OPEN, REGULAR_CLOSE, timeframe_minutes(timeframe))


def early_close_session_slots(timeframe: str) -> tuple[str, ...]:
    """Exchange-local start times on a 13:00 ET early-close day."""
    return _slots_between(REGULAR_OPEN, EARLY_CLOSE, timeframe_minutes(timeframe))


def exchange_time(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        raise ValueError("intraday timestamps must be timezone-aware")
    return timestamp.astimezone(NEW_YORK)


def bar_slot(timestamp: datetime) -> str:
    """Exchange-local ``HH:MM`` start of the bar."""
    return exchange_time(timestamp).strftime("%H:%M")


def session_date(timestamp: datetime) -> date:
    return exchange_time(timestamp).date()


def is_regular_session_bar(timestamp: datetime, *, timeframe: str) -> bool:
    return bar_slot(timestamp) in set(regular_session_slots(timeframe))


def bar_close_timestamp(timestamp: datetime, *, timeframe: str) -> datetime:
    """The instant a bar's information becomes complete."""
    return timestamp + timedelta(minutes=timeframe_minutes(timeframe))


def _row_timestamp(row: dict[str, Any]) -> datetime:
    timestamp = row.get("timestamp")
    if not isinstance(timestamp, datetime):
        raise TypeError("intraday rows require a datetime 'timestamp'")
    return timestamp


def regular_session_rows(
    rows: Iterable[dict[str, Any]],
    *,
    timeframe: str,
) -> list[dict[str, Any]]:
    """Drop premarket and post-close bars, preserving chronological order."""
    allowed = set(regular_session_slots(timeframe))
    return sorted(
        (row for row in rows if bar_slot(_row_timestamp(row)) in allowed),
        key=_row_timestamp,
    )


def regular_sessions_by_symbol(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
) -> dict[str, list[dict[str, Any]]]:
    return {
        symbol: regular_session_rows(rows, timeframe=timeframe)
        for symbol, rows in candles_by_symbol.items()
    }


def sessions_by_date(
    rows: Iterable[dict[str, Any]],
    *,
    timeframe: str,
    regular_only: bool = True,
) -> dict[date, list[dict[str, Any]]]:
    """Group bars by exchange session date in chronological order."""
    selected = (
        regular_session_rows(rows, timeframe=timeframe)
        if regular_only
        else sorted(rows, key=_row_timestamp)
    )
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        grouped[session_date(_row_timestamp(row))].append(row)
    return dict(grouped)


def session_shape(session_rows: Sequence[dict[str, Any]], *, timeframe: str) -> str:
    """Classify a regular-hours session as full, early-close, or incomplete."""
    slots = [bar_slot(_row_timestamp(row)) for row in session_rows]
    if slots == list(regular_session_slots(timeframe)):
        return "full"
    if slots == list(early_close_session_slots(timeframe)):
        return "early_close"
    return "incomplete"


def opening_bar(
    session_rows: Sequence[dict[str, Any]],
    *,
    timeframe: str,
) -> dict[str, Any] | None:
    """The 09:30 ET bar, or None when the session does not contain one."""
    opening_slot = regular_session_slots(timeframe)[0]
    for row in session_rows:
        if bar_slot(_row_timestamp(row)) == opening_slot:
            return row
    return None


def closing_bar(
    session_rows: Sequence[dict[str, Any]],
    *,
    timeframe: str,
    require_known_shape: bool = True,
) -> dict[str, Any] | None:
    """The final regular-hours bar of a full or early-close session.

    ``require_known_shape`` refuses to name a closing bar for a session whose
    bar complement matches neither calendar shape, because on a truncated or
    partially ingested day the last stored bar is not the closing half hour.
    """
    if not session_rows:
        return None
    shape = session_shape(session_rows, timeframe=timeframe)
    if require_known_shape and shape == "incomplete":
        return None
    return max(session_rows, key=_row_timestamp)


def ordered_regular_sessions(
    rows: Iterable[dict[str, Any]],
    *,
    timeframe: str,
) -> list[tuple[date, list[dict[str, Any]]]]:
    grouped = sessions_by_date(rows, timeframe=timeframe)
    return [(key, grouped[key]) for key in sorted(grouped)]


# A Friday-to-Monday gap is three calendar days; a long weekend with a holiday
# is four.  Anything wider means the two sessions are not adjacent in the data
# -- a symbol that left and rejoined a point-in-time universe, or a hole in the
# history -- and the move between them is not an overnight gap.
MAX_CONSECUTIVE_SESSION_DAYS = 5


def is_consecutive_session(
    previous: date | None,
    current: date,
    *,
    max_calendar_days: int = MAX_CONSECUTIVE_SESSION_DAYS,
) -> bool:
    """Are these two sessions adjacent enough to span a single overnight?

    Point-in-time membership means a symbol's rows stop when it leaves the
    universe and resume when it rejoins.  Measuring a close-to-open move across
    that hole produces a gap of months, which is not the quantity any overnight
    hypothesis is about.
    """
    if previous is None:
        return False
    delta = (current - previous).days
    return 0 < delta <= max_calendar_days


def extended_hours_audit(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    timeframe: str,
) -> dict[str, Any]:
    """Measure how much of a snapshot sits outside regular trading hours.

    Reported rather than silently repaired: a snapshot whose sessions are
    mostly incomplete cannot support a closing-half-hour claim at all.
    """
    allowed = set(regular_session_slots(timeframe))
    total = 0
    extended = 0
    naive_timestamps = 0
    misaligned = 0
    duplicates = 0
    extended_slots: dict[str, int] = defaultdict(int)
    contaminated_sessions: set[tuple[str, date]] = set()
    shapes: dict[str, int] = defaultdict(int)
    minutes = timeframe_minutes(timeframe)
    for symbol, rows in candles_by_symbol.items():
        seen: set[datetime] = set()
        for row in rows:
            timestamp = _row_timestamp(row)
            total += 1
            if timestamp.tzinfo is None:
                naive_timestamps += 1
                continue
            if timestamp in seen:
                duplicates += 1
            seen.add(timestamp)
            local = exchange_time(timestamp)
            # A 30m bar must start on a 30m boundary of the exchange clock; a
            # bar at 09:47 is a normalization failure, not market structure.
            if local.second or local.microsecond or (local.minute % minutes):
                misaligned += 1
            slot = bar_slot(timestamp)
            if slot not in allowed:
                extended += 1
                extended_slots[slot] += 1
                contaminated_sessions.add((symbol, session_date(timestamp)))
        for session_day, session_rows in sessions_by_date(rows, timeframe=timeframe).items():
            shapes[session_shape(session_rows, timeframe=timeframe)] += 1

    session_total = sum(shapes.values())
    return {
        "calendar_version": SESSION_CALENDAR_VERSION,
        "timeframe": timeframe,
        "candle_rows": total,
        "extended_hours_rows": extended,
        "extended_hours_share": _round(extended / total) if total else None,
        "extended_hours_slots": dict(sorted(extended_slots.items())),
        "symbol_sessions_touched_by_extended_hours": len(contaminated_sessions),
        "symbol_sessions": session_total,
        "session_shapes": dict(shapes),
        "duplicate_symbol_timestamp_rows": duplicates,
        "naive_timestamps": naive_timestamps,
        "misaligned_bar_starts": misaligned,
        "timestamps_normalized": naive_timestamps == 0 and misaligned == 0,
        "expected_full_session_bars": len(allowed),
        "complete_session_share": (
            _round((shapes.get("full", 0) + shapes.get("early_close", 0)) / session_total)
            if session_total
            else None
        ),
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None
