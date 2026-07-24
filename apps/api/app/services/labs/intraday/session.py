"""Exchange trading-session calendar for the Intraday Lab.

Market timezone and calendar source (explicit, per the Step 1 requirements):
  - Exchange calendar: NYSE (`pandas_market_calendars`, calendar code "XNYS").
    This is the calendar for every symbol currently in the research core
    (US equities/ETFs); it is not used for crypto.
  - The calendar library resolves session open/close times to UTC directly
    (it understands `America/New_York` and DST internally), so every
    timestamp this module returns is UTC-aware and directly comparable to
    the UTC timestamps already stored in `candles`/`features`.
  - Regular-hours treatment: a session is exactly
    `[market_open, market_close)` per the exchange calendar for that date.
    There is no separate "regular vs extended" distinction to make here
    because Alpaca's IEX feed (the provider this repo uses,
    `providers/alpaca.py`) does not return extended-hours bars for IEX in the
    first place -- regular hours are the only bars that exist upstream.
  - Early closes (half days, e.g. the day after Thanksgiving) are handled
    automatically by the calendar library, not by a hardcoded holiday list --
    `schedule()` returns the correct shortened `market_close` for those dates.
  - Sessions are looked up by matching each bar's UTC timestamp against the
    calendar's actual `[market_open, market_close)` window for that date, not
    by truncating the UTC timestamp to a calendar date. This matters because
    a UTC calendar-date truncation would misclassify sessions incorrectly
    around midnight UTC boundaries during standard time, and would have no
    way to know about early closes at all.
  - Premarket data: excluded, not stored separately. A bar whose timestamp
    does not fall inside any session's `[market_open, market_close)` window
    (as returned by `assign_sessions`) is dropped by the caller
    (`features.py`) before it ever reaches `intraday_features`. This is
    consistent with what the data provider already supplies (IEX has no
    premarket bars), and documented here in case a future provider does
    supply extended-hours data: those bars still would not get a row until
    this module is deliberately extended to handle them.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

CALENDAR_NAME = "XNYS"

# Padding applied before the requested start date so that `previous_valid_session`
# can find a real prior session even when the request starts exactly at the
# beginning of a symbol's available history.
SCHEDULE_LOOKBACK_PADDING_DAYS = 15


@lru_cache(maxsize=1)
def get_calendar():
    return mcal.get_calendar(CALENDAR_NAME)


def trading_schedule(start_date: date, end_date: date, *, padding_days: int = SCHEDULE_LOOKBACK_PADDING_DAYS) -> pd.DataFrame:
    """UTC-aware `[market_open, market_close)` for every trading session in range.

    Padded backward so callers can resolve the "previous valid session" for
    dates at the start of the requested window without a second calendar
    call. Indexed by the exchange calendar's own session date (a `Timestamp`
    at midnight in the calendar's terms) -- never derived from a UTC bar
    timestamp.
    """
    calendar = get_calendar()
    schedule = calendar.schedule(start_date=start_date - timedelta(days=padding_days), end_date=end_date)
    return schedule[["market_open", "market_close"]]


def assign_sessions(timestamps: pd.Series, schedule: pd.DataFrame) -> pd.DataFrame:
    """Match each bar-open timestamp to its exchange session, if any.

    For every input timestamp, finds the most recent session whose
    `market_open` is at or before it, then verifies the timestamp is still
    strictly before that session's `market_close`. A bar that does not fall
    inside any session's window (premarket, weekends, holidays, post-close)
    gets `NaT`/`NaN` in every output column -- the caller drops those rows
    (see the premarket-exclusion note in this module's docstring).

    Returns a DataFrame aligned to `timestamps`' original order with columns:
    `session_date`, `market_open`, `market_close`, `minutes_from_open`,
    `minutes_to_close`.
    """
    ts = pd.Series(pd.to_datetime(timestamps, utc=True)).reset_index(drop=True)
    # `order` holds, in ascending-time order, the ORIGINAL position (0..N-1)
    # each sorted row came from -- e.g. order[0] is the original index of the
    # earliest timestamp. merge_asof requires sorted input, but the caller's
    # input order must be preserved in the return value, so every row is
    # tagged with its original position and restored via that tag afterward
    # (not via a second, unrelated sort) to avoid silently permuting results.
    order = ts.sort_values().index.to_numpy()
    sorted_ts = ts.loc[order].reset_index(drop=True)

    sessions = schedule.reset_index().rename(columns={schedule.index.name or "index": "session_date"})
    sessions = sessions.sort_values("market_open").reset_index(drop=True)

    merged = pd.merge_asof(
        sorted_ts.to_frame(name="timestamp"),
        sessions,
        left_on="timestamp",
        right_on="market_open",
        direction="backward",
    )
    within_session = merged["timestamp"] < merged["market_close"]
    merged.loc[~within_session, ["session_date", "market_open", "market_close"]] = pd.NaT

    merged["minutes_from_open"] = (merged["timestamp"] - merged["market_open"]).dt.total_seconds() / 60
    merged["minutes_to_close"] = (merged["market_close"] - merged["timestamp"]).dt.total_seconds() / 60
    merged.loc[~within_session, ["minutes_from_open", "minutes_to_close"]] = pd.NA

    # session_date must be a plain date (the exchange session date), not a
    # tz-aware midnight Timestamp, so it matches the SQL DATE column.
    merged["session_date"] = merged["session_date"].apply(lambda value: value.date() if pd.notna(value) else None)

    # Restore the caller's original row order: row i of `merged` is the
    # timestamp that originally lived at position order[i], so tagging with
    # `order` and sorting that tag back to 0..N-1 is the exact inverse of the
    # `ts.loc[order]` reindex above.
    merged.index = order
    result = merged.sort_index()[
        ["session_date", "market_open", "market_close", "minutes_from_open", "minutes_to_close"]
    ]
    return result


def previous_valid_session(session_date: date, schedule: pd.DataFrame) -> date | None:
    """The exchange calendar's session immediately before `session_date`.

    Skips weekends and holidays automatically because `schedule` only ever
    contains real trading sessions -- there is no "yesterday" arithmetic here.
    Returns `None` if `session_date` is the earliest session in `schedule`
    (the caller should request enough `padding_days` to avoid this in
    practice; `None` propagating to a null `gap_percent` is the documented,
    safe fallback either way).
    """
    session_dates = [index.date() for index in schedule.index]
    try:
        position = session_dates.index(session_date)
    except ValueError:
        return None
    if position == 0:
        return None
    return session_dates[position - 1]
