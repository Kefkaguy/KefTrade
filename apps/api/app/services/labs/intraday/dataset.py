"""Backtest-ready dataset assembly for the Intraday Lab (Phase 12, Step 2A).

Joins `candles` to `intraday_features` (never the swing `features` table --
see `app.services.research_campaigns.load_campaign_dataset` for that, left
untouched) and produces the `session_end_index` array the simulator needs to
structurally enforce flat-by-session-close (see `backtester.find_exit_index`).

This module never silently backtests a partially joined dataset: every honesty
check below raises `IntradayDatasetError` with the actual counts rather than
returning a dataset that looks complete but isn't.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app.services.backtester import build_market_arrays, combine_candles_features
from app.services.features import load_candles
from app.settings import settings

SUPPORTED_INTRADAY_TIMEFRAMES = ("15m", "30m")

# Bar duration in minutes for each supported intraday timeframe -- used only
# for the entry-cutoff calculation below, never for session-boundary
# resolution (that comes entirely from `intraday_features.session_date`,
# itself calendar-derived -- see `app.services.labs.intraday.session`).
INTRADAY_BAR_DURATION_MINUTES: dict[str, int] = {"15m": 15, "30m": 30}

# A join that drops more than half the candle rows signals a real problem
# (e.g. intraday_features was never backfilled for most of this range) --
# not the normal, expected premarket-orphan exclusion rate (~10-15% in
# production data). Deliberately conservative so it only fires on a genuine
# coverage gap.
MINIMUM_CANDLE_FEATURE_JOIN_RATIO = 0.5

# Opening-range fields should be non-null for essentially every joined row --
# the very first in-window bar of a session already has a real (self-)
# max/min value (see `compute_intraday_features`), so a large null fraction
# means real data is missing, not an expected edge case.
MINIMUM_OPENING_RANGE_COVERAGE = 0.95


class IntradayDatasetError(ValueError):
    """Raised instead of silently returning a partial/insufficient dataset."""


def load_intraday_features(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if limit is not None:
        rows = conn.execute(
            """
            SELECT *
            FROM (
                SELECT *
                FROM intraday_features
                WHERE symbol = %s AND timeframe = %s
                ORDER BY timestamp DESC
                LIMIT %s
            ) recent
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return list(rows)
    rows = conn.execute(
        """
        SELECT *
        FROM intraday_features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    return list(rows)


def build_session_end_index(rows: list[dict[str, Any]]) -> list[int]:
    """For each row, the index of the last row sharing its session_date.

    Requires `rows` sorted ascending by timestamp with every row's
    `feature["session_date"]` set (true for any dataset produced by
    `build_intraday_backtest_dataset`, which excludes orphan rows via the
    candle/intraday_features join and validates session_date is never null).
    Session_date groups are contiguous in a properly joined, orphan-free,
    ascending-by-timestamp dataset, so a single backward pass suffices.
    """
    n = len(rows)
    session_end_index = [0] * n
    if n == 0:
        return session_end_index
    last_index = n - 1
    current_session = rows[-1]["feature"]["session_date"]
    for i in range(n - 1, -1, -1):
        session_date = rows[i]["feature"]["session_date"]
        if session_date != current_session:
            last_index = i
            current_session = session_date
        session_end_index[i] = last_index
    return session_end_index


def build_intraday_backtest_dataset(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    *,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    """Pure function: candle rows + intraday_features rows in, backtest-ready dataset out."""
    if timeframe not in SUPPORTED_INTRADAY_TIMEFRAMES:
        raise IntradayDatasetError(
            f"Unsupported intraday timeframe {timeframe!r} for {symbol}. "
            f"Supported: {SUPPORTED_INTRADAY_TIMEFRAMES}."
        )
    if not candles:
        raise IntradayDatasetError(f"No candles available for {symbol} {timeframe}.")
    if not features:
        raise IntradayDatasetError(
            f"No intraday_features rows available for {symbol} {timeframe}. "
            "Run the intraday features backfill before backtesting this dataset."
        )

    rows = combine_candles_features(candles, features)
    if not rows:
        raise IntradayDatasetError(
            f"Joining candles to intraday_features for {symbol} {timeframe} produced zero rows "
            "(no timestamp overlap)."
        )

    join_ratio = len(rows) / len(candles)
    if join_ratio < MINIMUM_CANDLE_FEATURE_JOIN_RATIO:
        raise IntradayDatasetError(
            f"Only {len(rows)}/{len(candles)} ({join_ratio:.0%}) candles for {symbol} {timeframe} "
            f"joined to an intraday_features row -- below the minimum expected coverage "
            f"({MINIMUM_CANDLE_FEATURE_JOIN_RATIO:.0%}). Refusing to silently backtest a "
            "partially joined dataset; check whether the intraday features backfill covers "
            "this candle range."
        )

    missing_session_date = [row for row in rows if row["feature"].get("session_date") is None]
    if missing_session_date:
        raise IntradayDatasetError(
            f"{len(missing_session_date)}/{len(rows)} joined rows for {symbol} {timeframe} are "
            "missing session_date metadata; intraday_features should never contain a row "
            "without one."
        )

    distinct_sessions = {row["feature"]["session_date"] for row in rows}
    minimum_sessions = int(settings.intraday_minimum_distinct_sessions)
    if len(distinct_sessions) < minimum_sessions:
        raise IntradayDatasetError(
            f"Only {len(distinct_sessions)} distinct sessions available for {symbol} {timeframe}, "
            f"below the configured minimum ({minimum_sessions}, INTRADAY_MINIMUM_DISTINCT_SESSIONS)."
        )

    opening_range_missing = sum(
        1 for row in rows if row["feature"].get("opening_range_high") is None or row["feature"].get("opening_range_low") is None
    )
    opening_range_coverage = 1 - (opening_range_missing / len(rows))
    if opening_range_coverage < MINIMUM_OPENING_RANGE_COVERAGE:
        raise IntradayDatasetError(
            f"Opening-range coverage for {symbol} {timeframe} is only {opening_range_coverage:.1%} "
            f"({opening_range_missing}/{len(rows)} rows missing opening_range_high/low), below the "
            f"minimum ({MINIMUM_OPENING_RANGE_COVERAGE:.0%}). This indicates real gaps in the "
            "underlying candle history, not the expected zero-null case."
        )

    session_end_index = build_session_end_index(rows)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "features": features,
        "rows": rows,
        "market_arrays": build_market_arrays(rows),
        "session_end_index": session_end_index,
        "coverage": {
            "candle_join_ratio": join_ratio,
            "distinct_sessions": len(distinct_sessions),
            "opening_range_coverage": opening_range_coverage,
        },
    }


def load_intraday_backtest_dataset(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    *,
    candle_limit: int | None = None,
    feature_limit: int | None = None,
    dataset_id: int | None = None,
) -> dict[str, Any]:
    if timeframe not in SUPPORTED_INTRADAY_TIMEFRAMES:
        raise IntradayDatasetError(
            f"Unsupported intraday timeframe {timeframe!r} for {symbol}. "
            f"Supported: {SUPPORTED_INTRADAY_TIMEFRAMES}."
        )
    if dataset_id is not None:
        # Phase 12.5: read from the frozen, content-hashed snapshot instead of
        # the live tables, mirroring how load_frozen_campaign_dataset already
        # does this for swing research. Never falls back to live data on a
        # miss -- an empty frozen dataset is a real error, not silently
        # "upgraded" to live candles.
        from app.services.labs.intraday.dataset_snapshot import load_snapshot_candles, load_snapshot_intraday_features

        candles = load_snapshot_candles(conn, dataset_id, symbol, timeframe)
        features = load_snapshot_intraday_features(conn, dataset_id, symbol, timeframe)
        return build_intraday_backtest_dataset(candles, features, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe, limit=candle_limit)
    features = load_intraday_features(conn, symbol, timeframe, limit=feature_limit)
    return build_intraday_backtest_dataset(candles, features, symbol=symbol, timeframe=timeframe)


def minimum_entry_lookahead_minutes(
    timeframe: str,
    *,
    entry_offset_bars: int = 1,
    minimum_holding_bars: int = 1,
) -> int:
    """Minutes of session time an entry signal must still have ahead of it.

    Generic, reusable by any intraday strategy family (not ORB-specific):
    covers the next-bar-open execution delay plus at least one bar of actual
    holding time before the structural flat-by-session-close cap could force
    an immediate exit.
    """
    if timeframe not in INTRADAY_BAR_DURATION_MINUTES:
        raise IntradayDatasetError(
            f"Unsupported intraday timeframe {timeframe!r}. Supported: {SUPPORTED_INTRADAY_TIMEFRAMES}."
        )
    bar_duration = INTRADAY_BAR_DURATION_MINUTES[timeframe]
    return (entry_offset_bars + minimum_holding_bars) * bar_duration


def entry_is_within_session_cutoff(
    feature: dict[str, Any],
    *,
    timeframe: str,
    entry_offset_bars: int = 1,
    minimum_holding_bars: int = 1,
) -> bool:
    """Whether a signal at this bar leaves enough session time to actually trade.

    Reads `minutes_to_close` from the (already session-aware)
    `intraday_features` row -- no calendar lookup needed here, that
    resolution already happened upstream.
    """
    minutes_to_close = feature.get("minutes_to_close")
    if minutes_to_close is None:
        return False
    required = minimum_entry_lookahead_minutes(
        timeframe,
        entry_offset_bars=entry_offset_bars,
        minimum_holding_bars=minimum_holding_bars,
    )
    return minutes_to_close >= required
