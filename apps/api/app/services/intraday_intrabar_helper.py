"""1m microscope diagnostics for higher-timeframe intraday setups.

This module deliberately does not create a new 1m strategy family.  It asks a
more controlled question: for a declared 15m/30m event, what did 1m candles and
signed trade flow show inside the signal bar before the higher-timeframe entry?

The output is diagnostic evidence for designing a later predeclared combined
hypothesis.  It is not a promotion, confirmation, broker action, or UI action.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import FACTOR_SPECS
from app.services.research_architecture import jsonable, load_snapshot_candles

INTRABAR_HELPER_VERSION = "intraday_intrabar_helper_v1_1m_microscope"
PARENT_SECONDS = {"15m": 15 * 60, "30m": 30 * 60}
INTRABAR_SECONDS = {"1m": 60}


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, places: int = 6) -> float | None:
    return round(value, places) if value is not None else None


def _direction(score: Any) -> str:
    value = _float(score) or 0.0
    return "long" if value >= 0 else "short"


def _signed_for_direction(value: float | None, direction: str) -> float | None:
    if value is None:
        return None
    return value if direction == "long" else -value


def _weighted_average(rows: Sequence[dict[str, Any]], field: str) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        value = _float(row.get(field))
        volume = _float(row.get("total_volume")) or 0.0
        if value is None or volume <= 0:
            continue
        numerator += value * volume
        denominator += volume
    return numerator / denominator if denominator > 0 else None


def _simple_average(rows: Sequence[dict[str, Any]], field: str) -> float | None:
    values = [_float(row.get(field)) for row in rows]
    usable = [value for value in values if value is not None]
    return fmean(usable) if usable else None


def _load_manifest(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, assets, window_start, window_end, dataset_kind
        FROM research_dataset_manifests
        WHERE id = %s
        """,
        (dataset_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"dataset {dataset_id} does not exist")
    return dict(row)


def _dataset_symbols(manifest: dict[str, Any]) -> list[str]:
    assets = manifest.get("assets") or []
    return sorted(str(item).upper() for item in assets)


def _load_parent_candles(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        symbol: load_snapshot_candles(
            conn,
            dataset_id=dataset_id,
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        for symbol in symbols
    }


def _load_intrabar_candles(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    source: str,
    start: datetime,
    end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT symbol, timeframe, timestamp, open, high, low, close, volume, source
        FROM candles
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND source = %s
          AND timestamp >= %s
          AND timestamp < %s
        ORDER BY symbol, timestamp
        """,
        ([item.upper() for item in symbols], timeframe, source, start, end),
    ).fetchall()
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        record = dict(row)
        output[str(record["symbol"]).upper()].append(record)
    return output


def _load_intrabar_trade_flow(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    feed: str,
    start: datetime,
    end: datetime,
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT symbol, timestamp, trade_count, total_volume, classified_volume,
               signed_trade_imbalance, signed_trade_count_imbalance,
               large_trade_share, unclassified_share, effective_spread_bps,
               effective_trade_count
        FROM intraday_trade_flow_features
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND feed = %s
          AND timestamp >= %s
          AND timestamp < %s
          AND calculation_version LIKE 'intraday_trade_flow_v2%%'
        ORDER BY symbol, timestamp
        """,
        ([item.upper() for item in symbols], timeframe, feed, start, end),
    ).fetchall()
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        record = dict(row)
        output[str(record["symbol"]).upper()].append(record)
    return output


def _intrabar_available_window(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    source: str,
    feed: str,
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the overlapping 1m candle/trade-flow window available to diagnose.

    Dataset 82 contains many years of 30m candles, while the 1m microscope data
    is intentionally much narrower.  Without this bound, diagnostics spend a
    long time recomputing higher-timeframe events that cannot possibly receive
    1m annotations.
    """
    symbol_list = [item.upper() for item in symbols]
    candle = conn.execute(
        """
        SELECT MIN(timestamp) AS first_bar, MAX(timestamp) AS last_bar
        FROM candles
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND source = %s
          AND (%s::timestamptz IS NULL OR timestamp >= %s)
          AND (%s::timestamptz IS NULL OR timestamp < %s)
        """,
        (
            symbol_list,
            timeframe,
            source,
            requested_start,
            requested_start,
            requested_end,
            requested_end,
        ),
    ).fetchone()
    flow = conn.execute(
        """
        SELECT MIN(timestamp) AS first_bar, MAX(timestamp) AS last_bar
        FROM intraday_trade_flow_features
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND feed = %s
          AND calculation_version LIKE 'intraday_trade_flow_v2%%'
          AND (%s::timestamptz IS NULL OR timestamp >= %s)
          AND (%s::timestamptz IS NULL OR timestamp < %s)
        """,
        (
            symbol_list,
            timeframe,
            feed,
            requested_start,
            requested_start,
            requested_end,
            requested_end,
        ),
    ).fetchone()
    candle_first = candle["first_bar"] if candle else None
    candle_last = candle["last_bar"] if candle else None
    flow_first = flow["first_bar"] if flow else None
    flow_last = flow["last_bar"] if flow else None
    if not candle_first or not candle_last:
        raise ValueError("no 1m candles found for the requested diagnostic window")
    if not flow_first or not flow_last:
        raise ValueError("no 1m trade-flow found for the requested diagnostic window")

    bar_delta = timedelta(seconds=INTRABAR_SECONDS[timeframe])
    start = max(value for value in (candle_first, flow_first, requested_start) if value)
    end = min(
        value
        for value in (
            candle_last + bar_delta,
            flow_last + bar_delta,
            requested_end,
        )
        if value
    )
    if end <= start:
        raise ValueError("1m candle/trade-flow windows do not overlap")
    return start, end


def intrabar_data_coverage(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    timeframe: str = "1m",
    source: str = "alpaca_sip",
    feed: str = "sip",
) -> dict[str, Any]:
    """Measure whether the 1m microscope data exists before running diagnostics."""
    candle = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT symbol) AS symbols,
               COUNT(DISTINCT (timestamp AT TIME ZONE 'America/New_York')::date) AS sessions,
               MIN(timestamp) AS first_bar,
               MAX(timestamp) AS last_bar
        FROM candles
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND source = %s
          AND timestamp >= %s
          AND timestamp < %s
        """,
        ([item.upper() for item in symbols], timeframe, source, start, end),
    ).fetchone()
    flow = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(DISTINCT symbol) AS symbols,
               COUNT(DISTINCT (timestamp AT TIME ZONE 'America/New_York')::date) AS sessions,
               MIN(timestamp) AS first_bar,
               MAX(timestamp) AS last_bar
        FROM intraday_trade_flow_features
        WHERE symbol = ANY(%s)
          AND timeframe = %s
          AND feed = %s
          AND timestamp >= %s
          AND timestamp < %s
          AND calculation_version LIKE 'intraday_trade_flow_v2%%'
        """,
        ([item.upper() for item in symbols], timeframe, feed, start, end),
    ).fetchone()
    return {
        "calculation_version": INTRABAR_HELPER_VERSION,
        "timeframe": timeframe,
        "source": source,
        "feed": feed,
        "window_start": start,
        "window_end": end,
        "symbols_requested": len(set(item.upper() for item in symbols)),
        "candles": dict(candle or {}),
        "trade_flow": dict(flow or {}),
    }


def _event_window(
    observation: dict[str, Any],
    *,
    parent_timeframe: str,
) -> tuple[datetime, datetime]:
    start = observation["signal_bar_timestamp"]
    end = observation["decision_timestamp"]
    if end <= start:
        end = start + timedelta(seconds=PARENT_SECONDS[parent_timeframe])
    return start, end


def _confirmation_flags(
    *,
    direction: str,
    bars: Sequence[dict[str, Any]],
    flow_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    expected = direction
    if not bars:
        return {
            "intrabar_price_confirms_direction": None,
            "last_5m_price_confirms_direction": None,
            "higher_lows_or_lower_highs": None,
            "new_extreme_rejected": None,
            "vwap_reclaimed": None,
            "imbalance_confirms_direction": None,
            "imbalance_improving": None,
            "spread_acceptable": None,
        }

    first_open = _float(bars[0].get("open"))
    last_close = _float(bars[-1].get("close"))
    last_5 = list(bars[-5:])
    last_5_open = _float(last_5[0].get("open")) if last_5 else None
    return_bps = (
        (last_close / first_open - 1) * 10_000
        if first_open and first_open > 0 and last_close is not None
        else None
    )
    last_5_return_bps = (
        (last_close / last_5_open - 1) * 10_000
        if last_5_open and last_5_open > 0 and last_close is not None
        else None
    )

    lows = [_float(row.get("low")) for row in bars]
    highs = [_float(row.get("high")) for row in bars]
    usable_lows = [value for value in lows if value is not None]
    usable_highs = [value for value in highs if value is not None]
    first_half = list(bars[: max(1, len(bars) // 2)])
    second_half = list(bars[max(1, len(bars) // 2):])
    first_half_low = min((_float(row.get("low")) for row in first_half), default=None)
    second_half_low = min((_float(row.get("low")) for row in second_half), default=None)
    first_half_high = max((_float(row.get("high")) for row in first_half), default=None)
    second_half_high = max((_float(row.get("high")) for row in second_half), default=None)

    session_vwap = None
    total_notional = 0.0
    total_volume = 0.0
    for row in bars:
        close = _float(row.get("close"))
        volume = _float(row.get("volume")) or 0.0
        if close is None or volume <= 0:
            continue
        total_notional += close * volume
        total_volume += volume
    if total_volume > 0:
        session_vwap = total_notional / total_volume

    first_flow = list(flow_rows[: max(1, len(flow_rows) // 2)])
    second_flow = list(flow_rows[max(1, len(flow_rows) // 2):])
    first_imbalance = _weighted_average(first_flow, "signed_trade_imbalance")
    second_imbalance = _weighted_average(second_flow, "signed_trade_imbalance")
    last_5_imbalance = _weighted_average(list(flow_rows[-5:]), "signed_trade_imbalance")
    median_spread_proxy = _simple_average(flow_rows, "effective_spread_bps")

    signed_return = _signed_for_direction(return_bps, expected)
    signed_last_5_return = _signed_for_direction(last_5_return_bps, expected)
    signed_last_5_imbalance = _signed_for_direction(last_5_imbalance, expected)
    signed_delta_imbalance = _signed_for_direction(
        second_imbalance - first_imbalance
        if first_imbalance is not None and second_imbalance is not None
        else None,
        expected,
    )

    if expected == "long":
        structure = (
            second_half_low is not None
            and first_half_low is not None
            and second_half_low >= first_half_low
        )
        rejected = (
            bool(usable_lows)
            and last_close is not None
            and min(usable_lows) > 0
            and (last_close / min(usable_lows) - 1) * 10_000 >= 5
        )
        vwap_reclaimed = (
            session_vwap is not None
            and last_close is not None
            and last_close >= session_vwap
        )
    else:
        structure = (
            second_half_high is not None
            and first_half_high is not None
            and second_half_high <= first_half_high
        )
        rejected = (
            bool(usable_highs)
            and last_close is not None
            and max(usable_highs) > 0
            and (max(usable_highs) / last_close - 1) * 10_000 >= 5
        )
        vwap_reclaimed = (
            session_vwap is not None
            and last_close is not None
            and last_close <= session_vwap
        )

    return {
        "intrabar_price_confirms_direction": signed_return is not None and signed_return > 0,
        "last_5m_price_confirms_direction": (
            signed_last_5_return is not None and signed_last_5_return > 0
        ),
        "higher_lows_or_lower_highs": structure,
        "new_extreme_rejected": rejected,
        "vwap_reclaimed": vwap_reclaimed,
        "imbalance_confirms_direction": (
            signed_last_5_imbalance is not None and signed_last_5_imbalance > 0
        ),
        "imbalance_improving": (
            signed_delta_imbalance is not None and signed_delta_imbalance > 0
        ),
        # This is intentionally permissive.  It is a diagnostic flag, not a
        # trading gate, and effective_spread_bps may be absent when flow was
        # built with tick-rule-only trades.
        "spread_acceptable": (
            median_spread_proxy is None or median_spread_proxy <= 5.0
        ),
    }


def _event_diagnostics(
    observation: dict[str, Any],
    *,
    parent_timeframe: str,
    intrabar_timeframe: str,
    bars: Sequence[dict[str, Any]],
    flow_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    direction = _direction(observation.get("score"))
    expected_bars = PARENT_SECONDS[parent_timeframe] // INTRABAR_SECONDS[intrabar_timeframe]
    flags = _confirmation_flags(direction=direction, bars=bars, flow_rows=flow_rows)
    first_open = _float(bars[0].get("open")) if bars else None
    last_close = _float(bars[-1].get("close")) if bars else None
    intrabar_return_bps = (
        (last_close / first_open - 1) * 10_000
        if first_open and first_open > 0 and last_close is not None
        else None
    )
    total_volume = sum((_float(row.get("volume")) or 0.0) for row in bars)
    flow_volume = sum((_float(row.get("total_volume")) or 0.0) for row in flow_rows)
    first_imbalance = _weighted_average(
        flow_rows[: max(1, len(flow_rows) // 2)], "signed_trade_imbalance"
    )
    second_imbalance = _weighted_average(
        flow_rows[max(1, len(flow_rows) // 2):], "signed_trade_imbalance"
    )
    last_5_imbalance = _weighted_average(flow_rows[-5:], "signed_trade_imbalance")
    confirmations = [
        key for key, value in flags.items()
        if value is True and key != "spread_acceptable"
    ]
    session_date = observation["session_date"]
    return {
        "factor_key": observation["factor_key"],
        "symbol": observation["symbol"],
        "session_date": (
            session_date.isoformat()
            if isinstance(session_date, date)
            else str(session_date)
        ),
        "direction": direction,
        "signal_bar_timestamp": observation["signal_bar_timestamp"],
        "decision_timestamp": observation["decision_timestamp"],
        "entry_bar_timestamp": observation["entry_bar_timestamp"],
        "target_return_bps": _round((_float(observation.get("target_return")) or 0.0) * 10_000),
        "intrabar_bars": len(bars),
        "expected_intrabar_bars": expected_bars,
        "coverage_complete": len(bars) >= expected_bars,
        "trade_flow_bars": len(flow_rows),
        "trade_flow_coverage_complete": len(flow_rows) >= expected_bars,
        "intrabar_return_bps": _round(intrabar_return_bps),
        "directional_intrabar_return_bps": _round(
            _signed_for_direction(intrabar_return_bps, direction)
        ),
        "intrabar_volume": _round(total_volume, 2),
        "trade_flow_volume": _round(flow_volume, 2),
        "first_half_signed_imbalance": _round(first_imbalance),
        "second_half_signed_imbalance": _round(second_imbalance),
        "last_5m_signed_imbalance": _round(last_5_imbalance),
        "directional_imbalance_delta": _round(
            _signed_for_direction(
                second_imbalance - first_imbalance
                if first_imbalance is not None and second_imbalance is not None
                else None,
                direction,
            )
        ),
        "flags": flags,
        "confirmation_score": len(confirmations),
        "confirmations": confirmations,
    }


def _summarize_factor(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {
            "events": 0,
            "coverage_complete_events": 0,
            "trade_flow_complete_events": 0,
            "mean_target_return_bps": None,
            "mean_target_return_bps_when_confirmed": None,
            "mean_target_return_bps_when_rejected": None,
        }
    confirmed = [
        row for row in events
        if row["coverage_complete"]
        and row["confirmation_score"] >= 3
        and row["flags"].get("spread_acceptable") is not False
    ]
    rejected = [
        row for row in events
        if row["coverage_complete"]
        and row not in confirmed
    ]

    def mean_target(rows: Sequence[dict[str, Any]]) -> float | None:
        values = [_float(row.get("target_return_bps")) for row in rows]
        usable = [value for value in values if value is not None]
        return _round(fmean(usable)) if usable else None

    flag_counts: dict[str, int] = defaultdict(int)
    for row in events:
        for key, value in (row.get("flags") or {}).items():
            if value is True:
                flag_counts[key] += 1
    return {
        "events": len(events),
        "coverage_complete_events": sum(1 for row in events if row["coverage_complete"]),
        "trade_flow_complete_events": sum(
            1 for row in events if row["trade_flow_coverage_complete"]
        ),
        "mean_target_return_bps": mean_target(events),
        "confirmed_events_score_ge_3": len(confirmed),
        "mean_target_return_bps_when_confirmed": mean_target(confirmed),
        "rejected_or_incomplete_events": len(events) - len(confirmed),
        "mean_target_return_bps_when_rejected": mean_target(rejected),
        "flag_true_counts": dict(sorted(flag_counts.items())),
    }


def run_intrabar_diagnostics(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    parent_timeframe: str,
    factor_keys: Sequence[str],
    intrabar_timeframe: str = "1m",
    source: str = "alpaca_sip",
    feed: str = "sip",
    start: datetime | None = None,
    end: datetime | None = None,
    parent_lookback_days: int = 10,
    max_events_per_factor: int | None = 500,
    persist: bool = True,
) -> dict[str, Any]:
    """Attach 1m microscope features to higher-timeframe factor events."""
    if parent_timeframe not in PARENT_SECONDS:
        raise ValueError("parent_timeframe must be 15m or 30m")
    if intrabar_timeframe != "1m":
        raise ValueError("intrabar_timeframe must be 1m")
    unknown = [key for key in factor_keys if key not in FACTOR_SPECS]
    if unknown:
        raise ValueError(f"unknown factor key(s): {', '.join(unknown)}")
    unsupported = [
        key for key in factor_keys
        if parent_timeframe not in FACTOR_SPECS[key].supported_timeframes
    ]
    if unsupported:
        raise ValueError(
            f"factor(s) not supported on {parent_timeframe}: {', '.join(unsupported)}"
        )

    manifest = _load_manifest(conn, dataset_id)
    symbols = _dataset_symbols(manifest)
    intrabar_start, intrabar_end = _intrabar_available_window(
        conn,
        symbols=symbols,
        timeframe=intrabar_timeframe,
        source=source,
        feed=feed,
        requested_start=start,
        requested_end=end,
    )
    parent_start = intrabar_start - timedelta(days=max(parent_lookback_days, 1))
    parent_end = intrabar_end
    candles = _load_parent_candles(
        conn,
        dataset_id=dataset_id,
        symbols=symbols,
        timeframe=parent_timeframe,
        start=parent_start,
        end=parent_end,
    )
    event_rows: dict[str, list[dict[str, Any]]] = {}
    all_events: list[dict[str, Any]] = []
    for key in factor_keys:
        rows = []
        for row in FACTOR_SPECS[key].builder(candles, timeframe=parent_timeframe):
            event_start, event_end = _event_window(
                row,
                parent_timeframe=parent_timeframe,
            )
            if event_start >= intrabar_start and event_end <= intrabar_end:
                rows.append(row)
        rows = sorted(
            rows,
            key=lambda row: (
                row["signal_bar_timestamp"],
                row["symbol"],
                row.get("entry_bar_timestamp"),
            ),
        )
        if max_events_per_factor is not None:
            rows = rows[-max_events_per_factor:]
        event_rows[key] = rows
        all_events.extend(rows)

    if all_events:
        diagnostic_start = min(
            _event_window(row, parent_timeframe=parent_timeframe)[0]
            for row in all_events
        )
        diagnostic_end = max(
            _event_window(row, parent_timeframe=parent_timeframe)[1]
            for row in all_events
        )
    else:
        diagnostic_start = intrabar_start
        diagnostic_end = intrabar_end

    intrabar_candles = _load_intrabar_candles(
        conn,
        symbols=symbols,
        timeframe=intrabar_timeframe,
        source=source,
        start=diagnostic_start,
        end=diagnostic_end,
    )
    intrabar_flow = _load_intrabar_trade_flow(
        conn,
        symbols=symbols,
        timeframe=intrabar_timeframe,
        feed=feed,
        start=diagnostic_start,
        end=diagnostic_end,
    )

    factors: dict[str, Any] = {}
    for key, observations in event_rows.items():
        diagnostics = []
        for observation in observations:
            event_start, event_end = _event_window(
                observation,
                parent_timeframe=parent_timeframe,
            )
            symbol = str(observation["symbol"]).upper()
            bars = [
                row for row in intrabar_candles.get(symbol, [])
                if event_start <= row["timestamp"] < event_end
            ]
            flow_rows = [
                row for row in intrabar_flow.get(symbol, [])
                if event_start <= row["timestamp"] < event_end
            ]
            diagnostics.append(
                _event_diagnostics(
                    observation,
                    parent_timeframe=parent_timeframe,
                    intrabar_timeframe=intrabar_timeframe,
                    bars=bars,
                    flow_rows=flow_rows,
                )
            )
        factors[key] = {
            "spec": FACTOR_SPECS[key].frozen(),
            "events_scored": len(diagnostics),
            "summary": _summarize_factor(diagnostics),
            "recent_events": diagnostics[-20:],
        }

    coverage = intrabar_data_coverage(
        conn,
        symbols=symbols,
        start=diagnostic_start,
        end=diagnostic_end,
        timeframe=intrabar_timeframe,
        source=source,
        feed=feed,
    )
    report = {
        "calculation_version": INTRABAR_HELPER_VERSION,
        "dataset_id": dataset_id,
        "parent_timeframe": parent_timeframe,
        "intrabar_timeframe": intrabar_timeframe,
        "source": source,
        "feed": feed,
        "factor_keys": list(factor_keys),
        "window": {
            "start": diagnostic_start,
            "end": diagnostic_end,
            "available_intrabar_start": intrabar_start,
            "available_intrabar_end": intrabar_end,
            "parent_scan_start": parent_start,
            "parent_scan_end": parent_end,
            "parent_lookback_days": parent_lookback_days,
        },
        "symbols": symbols,
        "coverage": coverage,
        "factors": factors,
        "protocol_note": (
            "Diagnostic only. These 1m features may be used to design a later "
            "predeclared combined hypothesis; this report is not locked "
            "confirmation and does not promote any strategy."
        ),
    }
    if persist:
        row = conn.execute(
            """
            INSERT INTO intraday_intrabar_diagnostic_runs(
                parent_dataset_id, parent_timeframe, intrabar_timeframe,
                provider, feed, factor_keys, results, calculation_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                dataset_id,
                parent_timeframe,
                intrabar_timeframe,
                "alpaca",
                feed,
                Jsonb(jsonable(list(factor_keys))),
                Jsonb(jsonable(report)),
                INTRABAR_HELPER_VERSION,
            ),
        ).fetchone()
        conn.commit()
        report["diagnostic_run_id"] = int(row["id"])
    return report
