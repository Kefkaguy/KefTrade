from datetime import UTC, date, datetime, timedelta

from app.services.intraday_session_calendar import (
    NEW_YORK,
    bar_close_timestamp,
    bar_slot,
    closing_bar,
    early_close_session_slots,
    extended_hours_audit,
    is_regular_session_bar,
    opening_bar,
    ordered_regular_sessions,
    regular_session_rows,
    regular_session_slots,
    session_shape,
    sessions_by_date,
)


def bar(day: date, slot: str, *, symbol: str = "SPY", close: float = 100.0) -> dict:
    hour, minute = (int(part) for part in slot.split(":"))
    return {
        "symbol": symbol,
        "timeframe": "30m",
        "timestamp": datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK
        ).astimezone(UTC),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
    }


def full_session(day: date, *, symbol: str = "SPY") -> list[dict]:
    return [bar(day, slot, symbol=symbol) for slot in regular_session_slots("30m")]


def test_regular_session_slots_cover_the_trading_day_only():
    slots = regular_session_slots("30m")

    assert slots[0] == "09:30"
    assert slots[-1] == "15:30"
    assert len(slots) == 13
    assert len(regular_session_slots("15m")) == 26
    assert early_close_session_slots("30m")[-1] == "12:30"


def test_extended_hours_bars_are_not_regular_session_bars():
    day = date(2025, 3, 3)

    assert is_regular_session_bar(bar(day, "09:30")["timestamp"], timeframe="30m")
    assert not is_regular_session_bar(bar(day, "09:00")["timestamp"], timeframe="30m")
    assert not is_regular_session_bar(bar(day, "16:00")["timestamp"], timeframe="30m")


def test_bar_slot_follows_the_exchange_clock_across_daylight_saving():
    winter = datetime(2025, 1, 15, 14, 30, tzinfo=UTC)
    summer = datetime(2025, 7, 15, 13, 30, tzinfo=UTC)

    assert bar_slot(winter) == "09:30"
    assert bar_slot(summer) == "09:30"


def test_opening_and_closing_bars_ignore_premarket_and_post_close():
    day = date(2025, 3, 3)
    rows = [
        bar(day, "08:30", close=1.0),
        *full_session(day),
        bar(day, "16:00", close=999.0),
    ]

    session = regular_session_rows(rows, timeframe="30m")

    assert bar_slot(opening_bar(session, timeframe="30m")["timestamp"]) == "09:30"
    assert bar_slot(closing_bar(session, timeframe="30m")["timestamp"]) == "15:30"
    assert len(session) == 13


def test_closing_bar_refuses_a_session_of_unknown_shape():
    day = date(2025, 3, 3)
    truncated = [bar(day, slot) for slot in ("09:30", "10:00", "10:30")]

    assert session_shape(truncated, timeframe="30m") == "incomplete"
    assert closing_bar(truncated, timeframe="30m") is None
    assert closing_bar(truncated, timeframe="30m", require_known_shape=False) is not None


def test_early_close_session_is_a_known_shape():
    day = date(2025, 11, 28)
    rows = [bar(day, slot) for slot in early_close_session_slots("30m")]

    assert session_shape(rows, timeframe="30m") == "early_close"
    assert bar_slot(closing_bar(rows, timeframe="30m")["timestamp"]) == "12:30"


def test_bar_close_timestamp_is_when_the_information_completes():
    timestamp = bar(date(2025, 3, 3), "09:30")["timestamp"]

    assert bar_close_timestamp(timestamp, timeframe="30m") == timestamp + timedelta(
        minutes=30
    )


def test_sessions_group_by_exchange_date_in_order():
    days = [date(2025, 3, 3), date(2025, 3, 4)]
    rows = [row for day in days for row in full_session(day)]

    grouped = sessions_by_date(rows, timeframe="30m")
    ordered = ordered_regular_sessions(rows, timeframe="30m")

    assert sorted(grouped) == days
    assert [day for day, _ in ordered] == days
    assert all(len(session) == 13 for _, session in ordered)


def test_extended_hours_audit_counts_contaminated_sessions():
    clean_day = date(2025, 3, 3)
    dirty_day = date(2025, 3, 4)
    rows = [
        *full_session(clean_day),
        bar(dirty_day, "09:00"),
        *full_session(dirty_day),
        bar(dirty_day, "16:30"),
    ]

    audit = extended_hours_audit({"SPY": rows}, timeframe="30m")

    assert audit["extended_hours_rows"] == 2
    assert audit["extended_hours_slots"] == {"09:00": 1, "16:30": 1}
    assert audit["symbol_sessions_touched_by_extended_hours"] == 1
    assert audit["session_shapes"] == {"full": 2}
    assert audit["complete_session_share"] == 1.0
