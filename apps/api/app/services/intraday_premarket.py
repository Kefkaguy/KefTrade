"""Premarket price discovery from the extended-hours bars already ingested.

Regular-session research deliberately discards everything before 09:30, and
that exclusion is correct for measuring a session.  But the discarded bars are
not noise: they are where an overnight repricing is actually negotiated.  Two
gaps of identical size are different events if one was discovered across four
hours of premarket trading on heavy volume and the other appeared at the
opening print with nothing behind it.

Nothing here requires new ingestion.  The SIP feed already returns 04:00-09:30
bars and they are sitting unused in the candle table.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, time
from statistics import fmean
from typing import Any, Sequence

import psycopg

PREMARKET_VERSION = "intraday_premarket_v1"

PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
# A premarket baseline built from fewer than this many prior sessions is not
# trusted; the relative measure is left null rather than compared to noise.
MINIMUM_BASELINE_SESSIONS = 5
BASELINE_SESSIONS = 20


def _round(value: float | None) -> float | None:
    return round(float(value), 8) if value is not None else None


def premarket_features(
    sessions: Sequence[dict[str, Any]],
    *,
    timeframe: str,
    source: str,
    baseline_sessions: int = BASELINE_SESSIONS,
) -> list[dict[str, Any]]:
    """Build one premarket row per session from ordered session summaries.

    ``sessions`` carries, per symbol-session: premarket bar count, volume,
    first/last premarket price, high, low, and the regular-session open and
    close.  The relative-volume baseline uses only strictly prior sessions, so
    a session is never measured against itself.
    """
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sessions:
        by_symbol[str(row["symbol"]).upper()].append(row)

    output: list[dict[str, Any]] = []
    for symbol, rows in by_symbol.items():
        ordered = sorted(rows, key=lambda item: item["session_date"])
        history: list[float] = []
        previous_close: float | None = None
        previous_session: date | None = None
        for row in ordered:
            bars = int(row.get("premarket_bars") or 0)
            volume = float(row.get("premarket_volume") or 0)
            if bars > 0:
                baseline = history[-baseline_sessions:]
                relative = (
                    volume / fmean(baseline)
                    if len(baseline) >= MINIMUM_BASELINE_SESSIONS and fmean(baseline) > 0
                    else None
                )
                first = row.get("first_premarket_price")
                last = row.get("last_premarket_price")
                high = row.get("premarket_high")
                low = row.get("premarket_low")
                session_open = row.get("regular_open")
                premarket_return = (
                    (float(last) - float(first)) / float(first)
                    if first and last and float(first) > 0
                    else None
                )
                premarket_range = (
                    (float(high) - float(low)) / float(low)
                    if high and low and float(low) > 0
                    else None
                )
                # The gap is only an overnight gap if the previous session is
                # adjacent; a point-in-time universe leaves holes.
                adjacent = (
                    previous_session is not None
                    and 0 < (row["session_date"] - previous_session).days <= 5
                )
                premarket_gap = (
                    (float(last) - previous_close) / previous_close
                    if adjacent and previous_close and last and previous_close > 0
                    else None
                )
                opening_gap = (
                    (float(session_open) - previous_close) / previous_close
                    if adjacent and previous_close and session_open and previous_close > 0
                    else None
                )
                # What share of the eventual opening gap premarket had already
                # priced. Above 1 means premarket overshot and the open pulled
                # back toward the prior close.
                discovered = (
                    premarket_gap / opening_gap
                    if premarket_gap is not None and opening_gap not in (None, 0)
                    else None
                )
                output.append(
                    {
                        "symbol": symbol,
                        "session_date": row["session_date"],
                        "timeframe": timeframe,
                        "source": source,
                        "premarket_bars": bars,
                        "premarket_volume": volume,
                        "premarket_relative_volume": _round(relative),
                        "premarket_return": _round(premarket_return),
                        "premarket_range": _round(premarket_range),
                        "premarket_high": high,
                        "premarket_low": low,
                        "last_premarket_price": last,
                        "prior_regular_close": previous_close,
                        "premarket_gap": _round(premarket_gap),
                        "opening_gap": _round(opening_gap),
                        "gap_discovered_premarket": _round(discovered),
                        "calculation_version": PREMARKET_VERSION,
                    }
                )
                history.append(volume)
            if row.get("regular_close") is not None:
                previous_close = float(row["regular_close"])
                previous_session = row["session_date"]
    return sorted(output, key=lambda row: (row["symbol"], row["session_date"]))


def load_session_summaries(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    source: str,
) -> list[dict[str, Any]]:
    """Premarket and regular-session summaries per symbol-session."""
    rows = conn.execute(
        """
        WITH marked AS (
            SELECT symbol,
                   (timestamp AT TIME ZONE 'America/New_York')::date AS session_date,
                   (timestamp AT TIME ZONE 'America/New_York')::time AS session_time,
                   timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = ANY(%s) AND timeframe = %s AND source = %s
        )
        SELECT symbol, session_date,
               COUNT(*) FILTER (
                   WHERE session_time >= %s AND session_time < %s
               ) AS premarket_bars,
               COALESCE(SUM(volume) FILTER (
                   WHERE session_time >= %s AND session_time < %s
               ), 0) AS premarket_volume,
               MAX(high) FILTER (WHERE session_time >= %s AND session_time < %s)
                   AS premarket_high,
               MIN(low) FILTER (WHERE session_time >= %s AND session_time < %s)
                   AS premarket_low,
               (ARRAY_AGG(open ORDER BY timestamp ASC) FILTER (
                   WHERE session_time >= %s AND session_time < %s
               ))[1] AS first_premarket_price,
               (ARRAY_AGG(close ORDER BY timestamp DESC) FILTER (
                   WHERE session_time >= %s AND session_time < %s
               ))[1] AS last_premarket_price,
               (ARRAY_AGG(open ORDER BY timestamp ASC) FILTER (
                   WHERE session_time >= %s AND session_time < TIME '16:00'
               ))[1] AS regular_open,
               (ARRAY_AGG(close ORDER BY timestamp DESC) FILTER (
                   WHERE session_time >= %s AND session_time < TIME '16:00'
               ))[1] AS regular_close
        FROM marked
        GROUP BY symbol, session_date
        ORDER BY symbol, session_date
        """,
        (
            [item.upper() for item in symbols],
            timeframe,
            source,
            *([PREMARKET_OPEN, REGULAR_OPEN] * 6),
            REGULAR_OPEN,
            REGULAR_OPEN,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def persist_premarket_features(
    conn: psycopg.Connection,
    rows: Sequence[dict[str, Any]],
) -> int:
    affected = 0
    for row in rows:
        result = conn.execute(
            """
            INSERT INTO intraday_premarket_features(
                symbol, session_date, timeframe, source, premarket_bars,
                premarket_volume, premarket_relative_volume, premarket_return,
                premarket_range, premarket_high, premarket_low,
                last_premarket_price, prior_regular_close, premarket_gap,
                opening_gap, gap_discovered_premarket, calculation_version
            )
            VALUES (%(symbol)s, %(session_date)s, %(timeframe)s, %(source)s,
                    %(premarket_bars)s, %(premarket_volume)s,
                    %(premarket_relative_volume)s, %(premarket_return)s,
                    %(premarket_range)s, %(premarket_high)s, %(premarket_low)s,
                    %(last_premarket_price)s, %(prior_regular_close)s,
                    %(premarket_gap)s, %(opening_gap)s,
                    %(gap_discovered_premarket)s, %(calculation_version)s)
            ON CONFLICT (symbol, session_date, timeframe, source) DO UPDATE SET
                premarket_bars = EXCLUDED.premarket_bars,
                premarket_volume = EXCLUDED.premarket_volume,
                premarket_relative_volume = EXCLUDED.premarket_relative_volume,
                premarket_return = EXCLUDED.premarket_return,
                premarket_range = EXCLUDED.premarket_range,
                premarket_high = EXCLUDED.premarket_high,
                premarket_low = EXCLUDED.premarket_low,
                last_premarket_price = EXCLUDED.last_premarket_price,
                prior_regular_close = EXCLUDED.prior_regular_close,
                premarket_gap = EXCLUDED.premarket_gap,
                opening_gap = EXCLUDED.opening_gap,
                gap_discovered_premarket = EXCLUDED.gap_discovered_premarket,
                calculation_version = EXCLUDED.calculation_version
            """,
            {key: row.get(key) for key in (
                "symbol", "session_date", "timeframe", "source", "premarket_bars",
                "premarket_volume", "premarket_relative_volume", "premarket_return",
                "premarket_range", "premarket_high", "premarket_low",
                "last_premarket_price", "prior_regular_close", "premarket_gap",
                "opening_gap", "gap_discovered_premarket", "calculation_version",
            )},
        )
        affected += result.rowcount or 0
    conn.commit()
    return affected


def build_premarket_dataset(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str = "30m",
    source: str = "alpaca_sip",
) -> dict[str, Any]:
    summaries = load_session_summaries(
        conn, symbols=symbols, timeframe=timeframe, source=source
    )
    rows = premarket_features(summaries, timeframe=timeframe, source=source)
    written = persist_premarket_features(conn, rows)
    with_volume = [row for row in rows if row["premarket_volume"] > 0]
    return {
        "premarket_version": PREMARKET_VERSION,
        "timeframe": timeframe,
        "source": source,
        "symbols_requested": len(list(symbols)),
        "symbol_sessions_scanned": len(summaries),
        "premarket_sessions": len(rows),
        "sessions_with_premarket_volume": len(with_volume),
        "rows_written": written,
        "coverage": (
            round(len(rows) / len(summaries), 6) if summaries else None
        ),
    }
