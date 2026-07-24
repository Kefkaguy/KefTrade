"""Session-aware feature computation for the Intraday Lab (Phase 12, Step 1).

Mirrors the shape of `app.services.features` (load candles -> compute ->
upsert, full recompute rather than a delta) deliberately: that module already
proves the pattern is correct and idempotent for this codebase, and reusing
it means backfill and incremental computation are identical by construction
(see `sync_intraday_features`) rather than needing separate code paths to be
kept consistent.

Every computation here only ever looks at the current bar and bars strictly
before it (or, for `session_relative_volume`, prior sessions only) -- see the
per-function docstrings for exactly how each of the required no-look-ahead
properties is achieved.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd
import psycopg

from app.services.features import load_candles
from app.services.labs.intraday.session import assign_sessions, previous_valid_session, trading_schedule
from app.services.research_campaigns import is_equity_market_asset
from app.settings import settings

# A session-relative-volume baseline built from fewer than this many prior
# sessions is not trusted; the feature is left null rather than reporting a
# ratio against a thin/noisy sample.
RELATIVE_VOLUME_MINIMUM_PRIOR_SESSIONS = 3


def default_opening_range_minutes() -> int:
    return int(getattr(settings, "intraday_opening_range_minutes", 30) or 30)


def default_relative_volume_lookback_sessions() -> int:
    # Not one of the three settings named in the Step 1 requirements
    # (opening_range_minutes, minimum_distinct_sessions, intraday_cost_multiplier)
    # -- this is a computation-lookback parameter, not a research threshold,
    # so it is a plain module constant rather than a Settings field. Callers
    # may still override it per-call (see `compute_intraday_features`).
    return 20


def load_intraday_candles(conn: psycopg.Connection, symbol: str, timeframe: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Reuses the existing candle loader -- `intraday_features` is derived
    from the same `candles` table as `features`, just for 15m/30m bars."""
    return load_candles(conn, symbol, timeframe, limit=limit)


def compute_intraday_features(
    candles: list[dict[str, Any]],
    *,
    opening_range_minutes: int | None = None,
    relative_volume_lookback_sessions: int | None = None,
) -> list[dict[str, Any]]:
    """Pure function: candle rows in, feature rows out. No DB access.

    Bars that do not fall within a regular trading session (premarket, bars
    on a non-trading day, or the closing edge of the session) are dropped
    entirely -- they never produce an `intraday_features` row. See
    `session.py`'s module docstring for why.
    """
    if not candles:
        return []

    opening_range_minutes = opening_range_minutes if opening_range_minutes is not None else default_opening_range_minutes()
    relative_volume_lookback_sessions = (
        relative_volume_lookback_sessions if relative_volume_lookback_sessions is not None else default_relative_volume_lookback_sessions()
    )

    df = pd.DataFrame(candles)
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = df[column].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)

    start_date = df["timestamp"].min().date()
    end_date = df["timestamp"].max().date()
    schedule = trading_schedule(start_date, end_date)

    sessions = assign_sessions(df["timestamp"], schedule)
    df = pd.concat([df, sessions], axis=1)
    df = df[df["session_date"].notna()].reset_index(drop=True)
    if df.empty:
        return []
    df["minutes_from_open"] = df["minutes_from_open"].astype(int)
    df["minutes_to_close"] = df["minutes_to_close"].astype(int)

    # --- session VWAP: cumulative sum reset at every session_date boundary.
    # groupby().cumsum() only ever accumulates rows up to and including the
    # current one within its own group, so no bar can see a later bar's price
    # or volume, and a new session_date starts its accumulation from zero.
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cumulative_pv = (typical_price * df["volume"]).groupby(df["session_date"]).cumsum()
    cumulative_volume = df["volume"].groupby(df["session_date"]).cumsum()
    df["session_vwap"] = cumulative_pv / cumulative_volume.replace(0, pd.NA)
    df["distance_from_session_vwap"] = (df["close"] - df["session_vwap"]) / df["session_vwap"]

    # --- opening range: expanding max/min while still inside the window,
    # then frozen once the window closes.
    # `cummax`/`cummin` treat NaN input as "ignore for later rows' running
    # value" but still emit NaN as the OUTPUT at the NaN row itself -- they do
    # NOT forward-fill. So an explicit `ffill` (grouped by session, same as
    # the cummax/cummin) is required to carry the settled window-close value
    # into every row after the window. The `ffill` only ever propagates a
    # value forward in time within the same session, so a post-window row
    # still only ever reflects bars up to and including the window's close --
    # never a later bar -- and a bar still inside the window keeps its own
    # true expanding max/min (the ffill has nothing earlier to overwrite it
    # with, since cummax/cummin already produced a real value there).
    in_window = df["minutes_from_open"] < opening_range_minutes
    or_high_candidate = df["high"].where(in_window)
    or_low_candidate = df["low"].where(in_window)
    or_high_running = or_high_candidate.groupby(df["session_date"]).cummax()
    or_low_running = or_low_candidate.groupby(df["session_date"]).cummin()
    df["opening_range_high"] = or_high_running.groupby(df["session_date"]).ffill()
    df["opening_range_low"] = or_low_running.groupby(df["session_date"]).ffill()
    or_span = df["opening_range_high"] - df["opening_range_low"]
    df["opening_range_position"] = (df["close"] - df["opening_range_low"]) / or_span.replace(0, pd.NA)

    # --- gap_percent: session_open vs. the previous VALID session's last
    # traded close in OUR data. A session-level constant, broadcast to every
    # bar in that session. Null when there is no previous valid session in
    # the calendar (start of history) or when we have no candle data at all
    # for that previous session (a data gap) -- never silently computed from
    # a missing/incorrect prior value.
    session_open_price = df.groupby("session_date")["open"].first()
    session_last_close = df.groupby("session_date")["close"].last()
    gap_by_session: dict[Any, float | None] = {}
    for session_date in session_open_price.index:
        previous_session = previous_valid_session(session_date, schedule)
        if previous_session is None or previous_session not in session_last_close.index:
            gap_by_session[session_date] = None
            continue
        previous_close = float(session_last_close.loc[previous_session])
        gap_by_session[session_date] = (float(session_open_price.loc[session_date]) - previous_close) / previous_close if previous_close else None
    df["gap_percent"] = df["session_date"].map(gap_by_session)

    # --- session_relative_volume: this bar's volume vs. the average volume
    # observed at the SAME minutes_from_open bucket over the prior N
    # sessions. Built by pivoting to (session_date x minutes_from_open),
    # shifting one row (one session) so the current session's own volume can
    # never enter its own baseline, THEN taking a rolling mean -- so the
    # baseline for a given session only ever draws on sessions strictly
    # before it. Requires >= RELATIVE_VOLUME_MINIMUM_PRIOR_SESSIONS actual
    # observations in that window or the result is null.
    pivot = df.pivot_table(index="session_date", columns="minutes_from_open", values="volume", aggfunc="last").sort_index()
    baseline = pivot.shift(1).rolling(window=relative_volume_lookback_sessions, min_periods=RELATIVE_VOLUME_MINIMUM_PRIOR_SESSIONS).mean()
    baseline_long = baseline.reset_index().melt(id_vars="session_date", var_name="minutes_from_open", value_name="_baseline_volume")
    df = df.merge(baseline_long, on=["session_date", "minutes_from_open"], how="left")
    df["session_relative_volume"] = df["volume"] / df["_baseline_volume"].replace(0, pd.NA)

    df["opening_range_minutes"] = opening_range_minutes
    df["relative_volume_lookback_sessions"] = relative_volume_lookback_sessions

    rows: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        rows.append(
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "timestamp": row["timestamp"],
                "session_date": row["session_date"],
                "minutes_from_open": int(row["minutes_from_open"]),
                "minutes_to_close": int(row["minutes_to_close"]),
                "session_vwap": _to_decimal(row.get("session_vwap")),
                "distance_from_session_vwap": _to_decimal(row.get("distance_from_session_vwap")),
                "opening_range_high": _to_decimal(row.get("opening_range_high")),
                "opening_range_low": _to_decimal(row.get("opening_range_low")),
                "opening_range_position": _to_decimal(row.get("opening_range_position")),
                "gap_percent": _to_decimal(row.get("gap_percent")),
                "session_relative_volume": _to_decimal(row.get("session_relative_volume")),
                "opening_range_minutes": int(row["opening_range_minutes"]),
                "relative_volume_lookback_sessions": int(row["relative_volume_lookback_sessions"]),
            }
        )
    return rows


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None
    return Decimal(str(round(float(value), 12)))


def upsert_intraday_features(conn: psycopg.Connection, feature_rows: list[dict[str, Any]]) -> int:
    affected = 0
    for row in feature_rows:
        result = conn.execute(
            """
            INSERT INTO intraday_features(
                symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
                session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low,
                opening_range_position, gap_percent, session_relative_volume,
                opening_range_minutes, relative_volume_lookback_sessions
            )
            VALUES (
                %(symbol)s, %(timeframe)s, %(timestamp)s, %(session_date)s, %(minutes_from_open)s, %(minutes_to_close)s,
                %(session_vwap)s, %(distance_from_session_vwap)s, %(opening_range_high)s, %(opening_range_low)s,
                %(opening_range_position)s, %(gap_percent)s, %(session_relative_volume)s,
                %(opening_range_minutes)s, %(relative_volume_lookback_sessions)s
            )
            ON CONFLICT(symbol, timeframe, timestamp)
            DO UPDATE SET
                session_date = EXCLUDED.session_date,
                minutes_from_open = EXCLUDED.minutes_from_open,
                minutes_to_close = EXCLUDED.minutes_to_close,
                session_vwap = EXCLUDED.session_vwap,
                distance_from_session_vwap = EXCLUDED.distance_from_session_vwap,
                opening_range_high = EXCLUDED.opening_range_high,
                opening_range_low = EXCLUDED.opening_range_low,
                opening_range_position = EXCLUDED.opening_range_position,
                gap_percent = EXCLUDED.gap_percent,
                session_relative_volume = EXCLUDED.session_relative_volume,
                opening_range_minutes = EXCLUDED.opening_range_minutes,
                relative_volume_lookback_sessions = EXCLUDED.relative_volume_lookback_sessions,
                updated_at = NOW()
            """,
            row,
        )
        affected += result.rowcount or 0
    return affected


def sync_intraday_features(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    *,
    candle_limit: int | None = None,
    opening_range_minutes: int | None = None,
    relative_volume_lookback_sessions: int | None = None,
) -> dict[str, Any]:
    """Full recompute + upsert for one (symbol, timeframe).

    Deliberately a full recompute from all available candles (bounded only by
    `candle_limit`, same as `sync_features`) rather than an incremental delta.
    This is what makes backfill and incremental sync produce identical
    results by construction: calling this twice, or calling it once on a
    partial history and again after more candles arrive, always recomputes
    every in-scope row from the same rules and upserts -- there is no
    separate "incremental" code path to drift out of sync with "backfill".
    """
    if timeframe not in ("15m", "30m"):
        raise ValueError(f"intraday features are only defined for 15m/30m timeframes, got {timeframe!r}")
    symbol_row = conn.execute("SELECT asset_class FROM symbols WHERE symbol = %s", (symbol,)).fetchone()
    if symbol_row and not is_equity_market_asset(symbol_row.get("asset_class")):
        return {"symbol": symbol, "timeframe": timeframe, "skipped": True, "reason": "not an equity/ETF asset_class; sessions do not apply"}

    candles = load_intraday_candles(conn, symbol, timeframe, limit=candle_limit)
    feature_rows = compute_intraday_features(
        candles,
        opening_range_minutes=opening_range_minutes,
        relative_volume_lookback_sessions=relative_volume_lookback_sessions,
    )
    upserted = upsert_intraday_features(conn, feature_rows)
    conn.commit()
    complete_rows = sum(1 for row in feature_rows if row["session_relative_volume"] is not None)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles_loaded": len(candles),
        "sessions": len({row["session_date"] for row in feature_rows}),
        "calculated": len(feature_rows),
        "usable": complete_rows,
        "upserted": upserted,
        "candle_limit": candle_limit,
    }


def backfill_intraday_features(
    conn: psycopg.Connection,
    symbols: list[str],
    timeframes: tuple[str, ...] = ("15m", "30m"),
    *,
    candle_limit: int | None = None,
) -> dict[str, Any]:
    """Backfill orchestrator: loops `sync_intraday_features` per (symbol, timeframe).

    Uses the exact same function as any future incremental/scheduled sync
    would -- see `sync_intraday_features`'s docstring for why that guarantees
    backfill and incremental computation agree.
    """
    results = []
    for symbol in symbols:
        for timeframe in timeframes:
            results.append(sync_intraday_features(conn, symbol, timeframe, candle_limit=candle_limit))
    processed = [row for row in results if not row.get("skipped")]
    skipped = [row for row in results if row.get("skipped")]
    return {
        "symbols": len(symbols),
        "timeframes": list(timeframes),
        "processed": len(processed),
        "skipped": len(skipped),
        "total_rows_upserted": sum(int(row.get("upserted") or 0) for row in processed),
        "results": results,
    }
