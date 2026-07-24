"""Phase 12.5 Step 2: pre-entry scalar feature computation.

Computed as a post-hoc lookup at trade-persistence time (see
`app.services.research_campaigns.persist_intraday_job_trades`), not inside
`backtester.py`. This keeps the simulator itself completely unmodified in
this step while still recording the seven features named in the Phase 12.5
architecture proposal (section 2.3): prior returns, ATR-relative movement,
VWAP distance, trend slope, volume acceleration, session progress, and
remaining session time.

No-look-ahead guarantee: every candle-derived feature below is computed from
bars strictly *before* the entry bar's own timestamp -- never the entry bar
itself, and never anything after it. This is deliberately more conservative
than the strategy's own `recent_candles` window (which includes the signal
bar), since these values are recorded as evidence about what was knowable
*before* the trade began, not what the strategy's decide() function saw.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

# Fixed, documented lookback window for every candle-derived pre-entry
# feature below. A single constant, asserted by a regression test, so it can
# never silently drift between features or across a future edit.
PRE_ENTRY_FEATURE_LOOKBACK_BARS = 10


def compute_pre_entry_scalar_features(
    pre_entry_candles: list[dict[str, Any]],
    *,
    vwap_distance: Decimal | None,
    minutes_from_open: int | None,
    minutes_to_close: int | None,
) -> dict[str, float | None]:
    """Pure function: candles strictly before the entry bar in, seven scalar
    features out. `pre_entry_candles` must be sorted ascending by timestamp
    and must not include the entry bar itself."""

    closes = [Decimal(str(candle["close"])) for candle in pre_entry_candles]
    highs = [Decimal(str(candle["high"])) for candle in pre_entry_candles]
    lows = [Decimal(str(candle["low"])) for candle in pre_entry_candles]
    volumes = [Decimal(str(candle["volume"])) for candle in pre_entry_candles]

    return_1 = _return(closes, lookback=1)
    return_5 = _return(closes, lookback=5)
    atr_relative_move = _atr_relative_move(closes, highs, lows)
    trend_slope = _trend_slope(closes)
    volume_acceleration = _volume_acceleration(volumes)
    session_progress = (
        float(minutes_from_open) / float(minutes_from_open + minutes_to_close)
        if minutes_from_open is not None and minutes_to_close is not None and (minutes_from_open + minutes_to_close) > 0
        else None
    )

    return {
        "pre_entry_return_1": return_1,
        "pre_entry_return_5": return_5,
        "pre_entry_atr_relative_move": atr_relative_move,
        "pre_entry_vwap_distance": float(vwap_distance) if vwap_distance is not None else None,
        "pre_entry_trend_slope": trend_slope,
        "pre_entry_volume_acceleration": volume_acceleration,
        "pre_entry_session_progress": session_progress,
    }


def _return(closes: list[Decimal], *, lookback: int) -> float | None:
    if len(closes) < lookback + 1:
        return None
    recent = closes[-1]
    past = closes[-1 - lookback]
    if past == 0:
        return None
    return float((recent - past) / past)


def _true_range(high: Decimal, low: Decimal, previous_close: Decimal) -> Decimal:
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def _atr_relative_move(closes: list[Decimal], highs: list[Decimal], lows: list[Decimal]) -> float | None:
    if len(closes) < PRE_ENTRY_FEATURE_LOOKBACK_BARS + 1:
        return None
    window = range(len(closes) - PRE_ENTRY_FEATURE_LOOKBACK_BARS, len(closes))
    true_ranges = [_true_range(highs[i], lows[i], closes[i - 1]) for i in window]
    atr = sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges))
    if atr == 0:
        return None
    move = closes[-1] - closes[-1 - PRE_ENTRY_FEATURE_LOOKBACK_BARS]
    return float(move / atr)


def _trend_slope(closes: list[Decimal]) -> float | None:
    if len(closes) < PRE_ENTRY_FEATURE_LOOKBACK_BARS:
        return None
    window = closes[-PRE_ENTRY_FEATURE_LOOKBACK_BARS:]
    values = [float(value) for value in window]
    n = len(values)
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    denominator = sum((index - mean_x) ** 2 for index in range(n))
    if denominator == 0 or mean_y == 0:
        return None
    slope = numerator / denominator
    return slope / mean_y


def _volume_acceleration(volumes: list[Decimal]) -> float | None:
    if len(volumes) < PRE_ENTRY_FEATURE_LOOKBACK_BARS + 1:
        return None
    trailing_window = volumes[-1 - PRE_ENTRY_FEATURE_LOOKBACK_BARS : -1]
    trailing_average = sum(trailing_window, Decimal("0")) / Decimal(len(trailing_window))
    if trailing_average == 0:
        return None
    return float(volumes[-1] / trailing_average)


def compute_pre_entry_features_for_trades(
    conn: psycopg.Connection,
    *,
    symbol: str,
    timeframe: str,
    trades: list[dict[str, Any]],
    dataset_id: int | None = None,
) -> list[dict[str, float | None]]:
    """DB-touching wrapper: loads one bounded candle window per (symbol,
    timeframe) covering every trade's entry time, then computes each trade's
    pre-entry features from the slice strictly before its own entry bar.
    One query pair per job (not per trade) for efficiency.

    Reads from the frozen dataset snapshot when `dataset_id` is given
    (mirrors `load_intraday_backtest_dataset`'s own dataset_id handling),
    otherwise from the live tables.
    """

    if not trades:
        return []

    entry_times = [trade["entry_time"] for trade in trades]
    earliest = min(entry_times)
    latest = max(entry_times)

    if dataset_id is not None:
        candle_rows = conn.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM research_dataset_candles
            WHERE dataset_id = %s AND symbol = %s AND timeframe = %s AND timestamp <= %s
            ORDER BY timestamp ASC
            """,
            (dataset_id, symbol, timeframe, latest),
        ).fetchall()
        feature_rows = conn.execute(
            """
            SELECT timestamp, distance_from_session_vwap
            FROM research_dataset_intraday_features
            WHERE dataset_id = %s AND symbol = %s AND timeframe = %s AND timestamp BETWEEN %s AND %s
            """,
            (dataset_id, symbol, timeframe, earliest, latest),
        ).fetchall()
    else:
        candle_rows = conn.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s AND timeframe = %s AND timestamp <= %s
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe, latest),
        ).fetchall()
        feature_rows = conn.execute(
            """
            SELECT timestamp, distance_from_session_vwap
            FROM intraday_features
            WHERE symbol = %s AND timeframe = %s AND timestamp BETWEEN %s AND %s
            """,
            (symbol, timeframe, earliest, latest),
        ).fetchall()

    candles = [dict(row) for row in candle_rows]
    vwap_distance_by_time = {row["timestamp"]: row["distance_from_session_vwap"] for row in feature_rows}
    timestamps = [candle["timestamp"] for candle in candles]

    results = []
    for trade in trades:
        entry_time = trade["entry_time"]
        # Bars strictly before the entry bar's own timestamp -- the entry bar
        # itself is excluded (see module docstring).
        cutoff = 0
        for index, timestamp in enumerate(timestamps):
            if timestamp >= entry_time:
                break
            cutoff = index + 1
        pre_entry_candles = candles[max(0, cutoff - PRE_ENTRY_FEATURE_LOOKBACK_BARS - 1) : cutoff]
        results.append(
            compute_pre_entry_scalar_features(
                pre_entry_candles,
                vwap_distance=vwap_distance_by_time.get(entry_time),
                minutes_from_open=trade.get("entry_minutes_from_open"),
                minutes_to_close=trade.get("entry_minutes_to_close"),
            )
        )
    return results
