"""Observed intraday execution-cost research.

This module calibrates quote/fill cost scenarios. It does not change the
simulator's cost assumptions and has no path to broker order submission.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from math import ceil, floor
from statistics import fmean, median
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

EXECUTION_COST_VERSION = "intraday_execution_cost_v1"
MICROSTRUCTURE_VERSION = "quote_ofi_v1"
CONSERVATIVE_ROUND_TRIP_BPS = 30.0


def percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, probability)) * (len(ordered) - 1)
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def signed_fill_slippage_bps(fill: dict[str, Any], quote: dict[str, Any]) -> float | None:
    midpoint = float(quote["midpoint"])
    price = float(fill["price"])
    if midpoint <= 0 or price <= 0:
        return None
    side = str(fill["side"]).lower()
    if side == "buy":
        return (price - midpoint) / midpoint * 10_000
    if side == "sell":
        return (midpoint - price) / midpoint * 10_000
    return None


def match_fills_to_quotes(
    fills: Sequence[dict[str, Any]],
    quotes: Sequence[dict[str, Any]],
    *,
    maximum_age: timedelta = timedelta(minutes=5),
) -> list[dict[str, Any]]:
    """Match each fill to the most recent quote without looking forward."""
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for quote in quotes:
        by_symbol[str(quote["symbol"]).upper()].append(quote)
    for rows in by_symbol.values():
        rows.sort(key=lambda row: row["timestamp"])

    matched: list[dict[str, Any]] = []
    for fill in fills:
        symbol = str(fill["symbol"]).upper()
        candidates = by_symbol.get(symbol, [])
        if not candidates:
            continue
        times = [row["timestamp"] for row in candidates]
        fill_time = fill["transaction_at"]
        index = bisect_right(times, fill_time) - 1
        if index < 0:
            continue
        quote = candidates[index]
        age = fill_time - quote["timestamp"]
        if age < timedelta(0) or age > maximum_age:
            continue
        slippage = signed_fill_slippage_bps(fill, quote)
        if slippage is None:
            continue
        matched.append(
            {
                "fill": fill,
                "quote": quote,
                "quote_age_seconds": age.total_seconds(),
                "signed_slippage_bps": slippage,
            }
        )
    return matched


def calibrate_execution_costs(
    quotes: Sequence[dict[str, Any]],
    fills: Sequence[dict[str, Any]] = (),
    *,
    regulatory_bps: float = 0.1,
    conservative_round_trip_bps: float = CONSERVATIVE_ROUND_TRIP_BPS,
) -> dict[str, Any]:
    """Produce observed and stressed round-trip scenarios.

    A matched fill's midpoint slippage already includes spread paid, so it is
    never added to spread again. With no matched fills, quoted spread is the
    executable round-trip proxy (half-spread on entry plus half on exit).
    """
    valid_quotes = [
        quote
        for quote in quotes
        if float(quote.get("midpoint") or 0) > 0 and float(quote.get("spread_bps") or -1) >= 0
    ]
    matched = match_fills_to_quotes(fills, valid_quotes)
    spreads = [float(quote["spread_bps"]) for quote in valid_quotes]
    adverse_slippage = [max(0.0, float(row["signed_slippage_bps"])) for row in matched]

    median_spread = percentile(spreads, 0.5)
    p90_spread = percentile(spreads, 0.9)
    median_fill = percentile(adverse_slippage, 0.5)
    p90_fill = percentile(adverse_slippage, 0.9)

    if median_fill is not None:
        observed = 2 * median_fill + regulatory_bps
        stress_candidates = [2 * (p90_fill or median_fill) + regulatory_bps]
        if p90_spread is not None:
            stress_candidates.append(p90_spread + regulatory_bps)
        stressed = max(stress_candidates)
        basis = "matched fill slippage versus the last non-forward quote midpoint"
    elif median_spread is not None:
        observed = median_spread + regulatory_bps
        stressed = (p90_spread if p90_spread is not None else median_spread * 2) + regulatory_bps
        basis = "quoted spread proxy; no fill had a usable preceding quote"
    else:
        observed = None
        stressed = None
        basis = "unavailable; no valid quotes"

    def group_summary(group_quotes: Iterable[dict[str, Any]]) -> dict[str, Any]:
        values = [float(row["spread_bps"]) for row in group_quotes]
        return {
            "quotes": len(values),
            "median_spread_bps": _round(percentile(values, 0.5)),
            "p90_spread_bps": _round(percentile(values, 0.9)),
        }

    by_symbol_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_slot_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for quote in valid_quotes:
        by_symbol_rows[str(quote["symbol"]).upper()].append(quote)
        timestamp = quote["timestamp"].astimezone(UTC)
        by_slot_rows[f"{timestamp.hour:02d}:{timestamp.minute:02d}"].append(quote)

    timestamps = [quote["timestamp"] for quote in valid_quotes]
    return {
        "calculation_version": EXECUTION_COST_VERSION,
        "provider": str(valid_quotes[0]["provider"]) if valid_quotes else "unknown",
        "feed": str(valid_quotes[0]["feed"]) if valid_quotes else "unknown",
        "window_start": min(timestamps) if timestamps else None,
        "window_end": max(timestamps) if timestamps else None,
        "symbols": sorted(by_symbol_rows),
        "quote_observations": len(valid_quotes),
        "matched_fill_observations": len(matched),
        "regulatory_bps": round(float(regulatory_bps), 6),
        "median_spread_bps": _round(median_spread),
        "p90_spread_bps": _round(p90_spread),
        "median_signed_fill_slippage_bps": _round(median_fill),
        "p90_signed_fill_slippage_bps": _round(p90_fill),
        "observed_round_trip_bps": _round(observed),
        "stressed_round_trip_bps": _round(stressed),
        "conservative_round_trip_bps": round(float(conservative_round_trip_bps), 6),
        "by_symbol": {
            key: group_summary(rows) for key, rows in sorted(by_symbol_rows.items())
        },
        "by_time_slot": {
            key: group_summary(rows) for key, rows in sorted(by_slot_rows.items())
        },
        "methodology": {
            "observed_basis": basis,
            "quote_feed_limitation": (
                "The provider/feed label is retained. IEX observations are not treated as consolidated SIP quotes."
            ),
            "matched_quote_rule": "most recent quote at or before fill, maximum age five minutes",
            "double_counting_guard": "fill midpoint slippage and quoted spread are alternative bases, never summed",
            "cost_scenarios": ["observed", "stressed", "conservative_30bps"],
        },
    }


def calibrate_regular_session_bar_costs(
    bars: Sequence[dict[str, Any]],
    quotes: Sequence[dict[str, Any]] = (),
    fills: Sequence[dict[str, Any]] = (),
    *,
    regulatory_bps: float = 0.1,
    conservative_round_trip_bps: float = CONSERVATIVE_ROUND_TRIP_BPS,
) -> dict[str, Any]:
    """Calibrate costs from equally weighted regular-session bar summaries.

    Raw quote-event weighting is biased toward the most active symbol and
    minute, and extended-hours quotes are not representative of a strategy
    that enters during the regular session. Each symbol/bar therefore
    contributes exactly one median-spread observation. The stress scenario is
    the 90th percentile across those bar medians, not the 90th percentile of
    every transient quote update inside every bar.
    """
    valid = [
        row
        for row in bars
        if int(row.get("quote_count") or 0) > 0
        and row.get("median_spread_bps") is not None
        and float(row["median_spread_bps"]) >= 0
    ]
    matched = match_fills_to_quotes(fills, quotes)
    adverse_slippage = [max(0.0, float(row["signed_slippage_bps"])) for row in matched]
    bar_medians = [float(row["median_spread_bps"]) for row in valid]
    median_spread = percentile(bar_medians, 0.5)
    p90_spread = percentile(bar_medians, 0.9)
    median_fill = percentile(adverse_slippage, 0.5)
    p90_fill = percentile(adverse_slippage, 0.9)

    if median_fill is not None:
        observed = 2 * median_fill + regulatory_bps
        stressed = max(
            2 * (p90_fill if p90_fill is not None else median_fill) + regulatory_bps,
            (p90_spread if p90_spread is not None else 0) + regulatory_bps,
        )
        basis = "matched fill slippage versus preceding midpoint, regular-session bars for stress"
    elif median_spread is not None:
        observed = median_spread + regulatory_bps
        stressed = (p90_spread if p90_spread is not None else median_spread * 2) + regulatory_bps
        basis = "equally weighted regular-session symbol/bar median quoted spread"
    else:
        observed = None
        stressed = None
        basis = "unavailable; no regular-session quote bars joined to session features"

    by_symbol_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_slot_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        by_symbol_rows[str(row["symbol"]).upper()].append(row)
        timestamp = row["timestamp"].astimezone(UTC)
        by_slot_rows[f"{timestamp.hour:02d}:{timestamp.minute:02d}"].append(row)

    def summarize(group: Sequence[dict[str, Any]]) -> dict[str, Any]:
        values = [float(row["median_spread_bps"]) for row in group]
        return {
            "regular_session_bars": len(group),
            "quote_updates": sum(int(row.get("quote_count") or 0) for row in group),
            "median_bar_spread_bps": _round(percentile(values, 0.5)),
            "p90_bar_spread_bps": _round(percentile(values, 0.9)),
        }

    timestamps = [row["timestamp"] for row in valid]
    return {
        "calculation_version": EXECUTION_COST_VERSION,
        "provider": str(valid[0]["provider"]) if valid else "unknown",
        "feed": str(valid[0]["feed"]) if valid else "unknown",
        "window_start": min(timestamps) if timestamps else None,
        "window_end": max(timestamps) if timestamps else None,
        "symbols": sorted(by_symbol_rows),
        "quote_observations": sum(int(row.get("quote_count") or 0) for row in valid),
        "matched_fill_observations": len(matched),
        "regulatory_bps": round(float(regulatory_bps), 6),
        "median_spread_bps": _round(median_spread),
        "p90_spread_bps": _round(p90_spread),
        "median_signed_fill_slippage_bps": _round(median_fill),
        "p90_signed_fill_slippage_bps": _round(p90_fill),
        "observed_round_trip_bps": _round(observed),
        "stressed_round_trip_bps": _round(stressed),
        "conservative_round_trip_bps": round(float(conservative_round_trip_bps), 6),
        "by_symbol": {
            key: summarize(rows) for key, rows in sorted(by_symbol_rows.items())
        },
        "by_time_slot": {
            key: summarize(rows) for key, rows in sorted(by_slot_rows.items())
        },
        "methodology": {
            "observed_basis": basis,
            "regular_session_filter": (
                "microstructure bars inner-joined to session-aware intraday_features "
                "at the exact symbol/timeframe/timestamp"
            ),
            "event_weighting_guard": (
                "one observation per symbol/bar; raw quote-update frequency cannot dominate"
            ),
            "stress_definition": "90th percentile across regular-session symbol/bar median spreads",
            "bar_observations": len(valid),
            "quote_feed_limitation": (
                "The provider/feed label is retained. IEX observations are not treated as consolidated SIP quotes."
            ),
            "matched_quote_rule": "most recent quote at or before fill, maximum age five minutes",
            "double_counting_guard": "fill midpoint slippage and quoted spread are alternative bases, never summed",
            "cost_scenarios": ["observed", "stressed", "conservative_30bps"],
        },
    }


def persist_quote_snapshots(conn: psycopg.Connection, quotes: Sequence[dict[str, Any]]) -> int:
    if not quotes:
        return 0
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO intraday_quote_snapshots(
                symbol, provider, feed, timestamp, bid_price, ask_price,
                bid_size, ask_size, midpoint, spread_bps, raw_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(symbol, provider, feed, timestamp)
            DO UPDATE SET
                bid_price = EXCLUDED.bid_price,
                ask_price = EXCLUDED.ask_price,
                bid_size = EXCLUDED.bid_size,
                ask_size = EXCLUDED.ask_size,
                midpoint = EXCLUDED.midpoint,
                spread_bps = EXCLUDED.spread_bps,
                raw_payload = EXCLUDED.raw_payload
            """,
            [
                (
                    row["symbol"],
                    row["provider"],
                    row["feed"],
                    row["timestamp"],
                    row["bid_price"],
                    row["ask_price"],
                    row.get("bid_size"),
                    row.get("ask_size"),
                    row["midpoint"],
                    row["spread_bps"],
                    Jsonb(row.get("raw_payload") or {}),
                )
                for row in quotes
            ],
        )
    conn.commit()
    return len(quotes)


def load_execution_evidence(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = [symbol.upper() for symbol in symbols]
    quotes = [
        dict(row)
        for row in conn.execute(
            """
            SELECT symbol, provider, feed, timestamp, bid_price, ask_price,
                   bid_size, ask_size, midpoint, spread_bps
            FROM intraday_quote_snapshots
            WHERE symbol = ANY(%s) AND timestamp BETWEEN %s AND %s
            ORDER BY symbol, timestamp
            """,
            (normalized, start, end),
        ).fetchall()
    ]
    fills = [
        dict(row)
        for row in conn.execute(
            """
            SELECT symbol, side, quantity, price, transaction_at
            FROM broker_fills
            WHERE symbol = ANY(%s) AND transaction_at BETWEEN %s AND %s
            ORDER BY symbol, transaction_at
            """,
            (normalized, start, end),
        ).fetchall()
    ]
    return quotes, fills


def load_regular_session_cost_bars(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Only quote bars that correspond to a real regular-session feature row."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT micro.symbol, micro.timeframe, micro.timestamp,
                   micro.provider, micro.feed, micro.quote_count,
                   micro.median_spread_bps, micro.p90_spread_bps,
                   micro.mean_depth, micro.order_flow_imbalance,
                   micro.normalized_order_flow_imbalance
            FROM intraday_microstructure_features micro
            JOIN intraday_features feature
              ON feature.symbol = micro.symbol
             AND feature.timeframe = micro.timeframe
             AND feature.timestamp = micro.timestamp
            WHERE micro.symbol = ANY(%s)
              AND micro.timeframe = %s
              AND micro.timestamp BETWEEN %s AND %s
              AND feature.minutes_from_open >= 0
              AND feature.minutes_to_close >= 0
            ORDER BY micro.symbol, micro.timestamp
            """,
            ([symbol.upper() for symbol in symbols], timeframe, start, end),
        ).fetchall()
    ]


def persist_cost_calibration(conn: psycopg.Connection, result: dict[str, Any]) -> int:
    row = conn.execute(
        """
        INSERT INTO intraday_execution_cost_calibrations(
            provider, feed, window_start, window_end, symbols,
            quote_observations, matched_fill_observations, regulatory_bps,
            median_spread_bps, p90_spread_bps,
            median_signed_fill_slippage_bps, p90_signed_fill_slippage_bps,
            observed_round_trip_bps, stressed_round_trip_bps,
            conservative_round_trip_bps, by_symbol, by_time_slot,
            methodology, calculation_version
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING id
        """,
        (
            result["provider"],
            result["feed"],
            result["window_start"],
            result["window_end"],
            Jsonb(result["symbols"]),
            result["quote_observations"],
            result["matched_fill_observations"],
            result["regulatory_bps"],
            result["median_spread_bps"],
            result["p90_spread_bps"],
            result["median_signed_fill_slippage_bps"],
            result["p90_signed_fill_slippage_bps"],
            result["observed_round_trip_bps"],
            result["stressed_round_trip_bps"],
            result["conservative_round_trip_bps"],
            Jsonb(result["by_symbol"]),
            Jsonb(result["by_time_slot"]),
            Jsonb(result["methodology"]),
            result["calculation_version"],
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def aggregate_microstructure_bars(
    quotes: Sequence[dict[str, Any]],
    *,
    timeframe: str,
) -> list[dict[str, Any]]:
    """Aggregate quote spread/depth and Cont-style OFI into 15m/30m bars."""
    if timeframe not in {"15m", "30m"}:
        raise ValueError("Microstructure aggregation supports 15m or 30m.")
    minutes = int(timeframe[:-1])
    grouped: dict[tuple[str, datetime, str, str], list[dict[str, Any]]] = defaultdict(list)
    for quote in sorted(quotes, key=lambda row: (row["symbol"], row["timestamp"])):
        timestamp = quote["timestamp"].astimezone(UTC)
        bucket_minute = (timestamp.minute // minutes) * minutes
        bucket = timestamp.replace(minute=bucket_minute, second=0, microsecond=0)
        key = (str(quote["symbol"]).upper(), bucket, str(quote["provider"]), str(quote["feed"]))
        grouped[key].append(quote)

    output: list[dict[str, Any]] = []
    for (symbol, timestamp, provider, feed), rows in sorted(grouped.items()):
        spreads = [float(row["spread_bps"]) for row in rows]
        depths = [
            (float(row.get("bid_size") or 0) + float(row.get("ask_size") or 0)) / 2
            for row in rows
        ]
        ofi = 0.0
        for previous, current in zip(rows, rows[1:]):
            pb0, pb1 = float(previous["bid_price"]), float(current["bid_price"])
            pa0, pa1 = float(previous["ask_price"]), float(current["ask_price"])
            qb0, qb1 = float(previous.get("bid_size") or 0), float(current.get("bid_size") or 0)
            qa0, qa1 = float(previous.get("ask_size") or 0), float(current.get("ask_size") or 0)
            ofi += (qb1 if pb1 >= pb0 else 0) - (qb0 if pb1 <= pb0 else 0)
            ofi -= (qa1 if pa1 <= pa0 else 0) - (qa0 if pa1 >= pa0 else 0)
        mean_depth = fmean(depths) if depths else 0.0
        output.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "provider": provider,
                "feed": feed,
                "quote_count": len(rows),
                "median_spread_bps": _round(median(spreads)),
                "p90_spread_bps": _round(percentile(spreads, 0.9)),
                "mean_depth": _round(mean_depth),
                "order_flow_imbalance": _round(ofi),
                "normalized_order_flow_imbalance": _round(ofi / mean_depth) if mean_depth > 0 else None,
                "calculation_version": MICROSTRUCTURE_VERSION,
            }
        )
    return output


def persist_microstructure_bars(conn: psycopg.Connection, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO intraday_microstructure_features(
                symbol, timeframe, timestamp, provider, feed, quote_count,
                median_spread_bps, p90_spread_bps, mean_depth,
                order_flow_imbalance, normalized_order_flow_imbalance,
                calculation_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(symbol, timeframe, timestamp, provider, feed)
            DO UPDATE SET
                quote_count = EXCLUDED.quote_count,
                median_spread_bps = EXCLUDED.median_spread_bps,
                p90_spread_bps = EXCLUDED.p90_spread_bps,
                mean_depth = EXCLUDED.mean_depth,
                order_flow_imbalance = EXCLUDED.order_flow_imbalance,
                normalized_order_flow_imbalance = EXCLUDED.normalized_order_flow_imbalance,
                calculation_version = EXCLUDED.calculation_version
            """,
            [
                (
                    row["symbol"], row["timeframe"], row["timestamp"], row["provider"], row["feed"],
                    row["quote_count"], row["median_spread_bps"], row["p90_spread_bps"],
                    row["mean_depth"], row["order_flow_imbalance"],
                    row["normalized_order_flow_imbalance"], row["calculation_version"],
                )
                for row in rows
            ],
        )
    conn.commit()
    return len(rows)


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None
