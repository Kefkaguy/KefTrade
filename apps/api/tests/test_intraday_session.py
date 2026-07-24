from datetime import date

import pandas as pd

from app.services.labs.intraday.session import assign_sessions, previous_valid_session, trading_schedule


def test_schedule_reflects_early_close_without_a_hardcoded_holiday_list() -> None:
    # 2026-11-27 is the day after Thanksgiving -- a real NYSE early close.
    # This must come from the calendar library, not a literal date check.
    schedule = trading_schedule(date(2026, 11, 20), date(2026, 12, 1))
    early_close_day = pd.Timestamp("2026-11-27")
    assert early_close_day in schedule.index
    row = schedule.loc[early_close_day]
    assert row["market_close"] == pd.Timestamp("2026-11-27 18:00:00", tz="UTC")  # 13:00 ET
    normal_day = pd.Timestamp("2026-11-25")
    assert schedule.loc[normal_day, "market_close"] == pd.Timestamp("2026-11-25 21:00:00", tz="UTC")  # 16:00 ET


def test_weekend_and_thanksgiving_holiday_are_excluded_from_the_schedule() -> None:
    schedule = trading_schedule(date(2026, 11, 20), date(2026, 12, 1))
    assert pd.Timestamp("2026-11-21") not in schedule.index  # Saturday
    assert pd.Timestamp("2026-11-22") not in schedule.index  # Sunday
    assert pd.Timestamp("2026-11-26") not in schedule.index  # Thanksgiving Day


def test_assign_sessions_excludes_premarket_and_the_close_boundary_itself() -> None:
    schedule = trading_schedule(date(2026, 11, 20), date(2026, 12, 1))
    timestamps = pd.to_datetime(
        [
            "2026-11-24 13:00:00+00:00",  # premarket, before 14:30 open
            "2026-11-24 14:30:00+00:00",  # session open
            "2026-11-27 18:00:00+00:00",  # exactly the early-close boundary: excluded (half-open interval)
        ],
        utc=True,
    )
    result = assign_sessions(pd.Series(timestamps), schedule)
    assert result.loc[0, "session_date"] is None
    assert result.loc[1, "session_date"] == date(2026, 11, 24)
    assert result.loc[1, "minutes_from_open"] == 0
    assert result.loc[2, "session_date"] is None


def test_assign_sessions_is_independent_of_input_order() -> None:
    """Regression guard: an earlier version of this function scrambled rows
    when given unsorted input because it restored order via `argsort()` on
    an already-sorted index rather than tagging original positions."""
    schedule = trading_schedule(date(2026, 11, 20), date(2026, 12, 1))
    timestamps = pd.to_datetime(
        [
            "2026-11-24 20:45:00+00:00",
            "2026-11-24 14:30:00+00:00",
            "2026-11-24 14:45:00+00:00",
        ],
        utc=True,
    )
    shuffled = timestamps.take([2, 0, 1])  # deliberately out of chronological order
    result = assign_sessions(pd.Series(shuffled), schedule)
    # Row 0 of the result must correspond to row 0 of the input (14:45, mfo=15)
    assert result.loc[0, "minutes_from_open"] == 15
    assert result.loc[1, "minutes_from_open"] == 375  # 20:45
    assert result.loc[2, "minutes_from_open"] == 0  # 14:30


def test_previous_valid_session_skips_weekend_and_holiday() -> None:
    schedule = trading_schedule(date(2026, 11, 20), date(2026, 12, 1))
    # Previous session before Friday 11-27 (early close) is Wednesday 11-25
    # (Thanksgiving on 11-26 and no weekend in between skipped correctly).
    assert previous_valid_session(date(2026, 11, 27), schedule) == date(2026, 11, 25)
    # Previous session before Monday 11-23 is Friday 11-20 (weekend skipped).
    assert previous_valid_session(date(2026, 11, 23), schedule) == date(2026, 11, 20)


def test_previous_valid_session_returns_none_at_the_start_of_the_schedule() -> None:
    schedule = trading_schedule(date(2026, 11, 20), date(2026, 12, 1))
    earliest = schedule.index[0].date()
    assert previous_valid_session(earliest, schedule) is None


def test_migration_uses_only_idempotent_schema_statements() -> None:
    """Regression guard for the exact bug class found twice earlier this
    session (migrations 023/040/043): the migrate job re-applies every
    migration file on every deploy, so a schema statement that isn't
    idempotent (e.g. a bare CREATE TABLE, or a DROP+ADD CONSTRAINT pair with
    a value that can go stale) breaks the next deploy. This migration must
    only ever use IF NOT EXISTS forms."""
    import pathlib

    migration = pathlib.Path(__file__).resolve().parents[3] / "database" / "migrations" / "044_intraday_features.sql"
    sql = migration.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS intraday_features" in sql
    assert "CREATE TABLE intraday_features" not in sql.replace("CREATE TABLE IF NOT EXISTS intraday_features", "")
    for index_name in ("intraday_features_symbol_timeframe_session_idx", "intraday_features_symbol_timeframe_idx"):
        assert f"CREATE INDEX IF NOT EXISTS {index_name}" in sql
