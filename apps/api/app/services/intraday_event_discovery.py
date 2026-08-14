"""Event-conditioned alpha discovery before strategy construction.

The module answers a narrower question than a backtester: given a mechanically
defined event and information knowable at its decision timestamp, where (if
anywhere) does the fixed-horizon post-cost return distribution move?  It does
not create orders, stops, targets, campaigns, candidates, or broker records.

The protocol is intentionally staged:

* declare freezes events, features, horizons, costs, and chronological splits;
* discover may read discovery + validation only and freezes one score/veto model;
* confirm reads the final 20% once and cannot be repeated for that model.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from itertools import combinations
from json import dumps
from math import erfc, isfinite, sqrt
from statistics import fmean, pstdev
from typing import Any, Iterable, Sequence

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import (
    load_cost_model,
    load_dataset_candles,
    sector_map,
)
from app.services.intraday_news import (
    NEWS_FEATURE_NAMES,
    empty_news_features,
    load_news_feature_index,
)
from app.services.intraday_options import (
    OPTION_FEATURE_NAMES,
    empty_option_features,
    load_option_feature_index,
)
from app.services.intraday_research_integrity import (
    clustered_outcome_statistics,
    estimated_round_trip_cost_bps,
)
from app.services.intraday_session_calendar import (
    bar_close_timestamp,
    is_consecutive_session,
    ordered_regular_sessions,
    timeframe_minutes,
)
from app.services.research_splits import (
    NestedSplits,
    get_dataset_splits,
    record_split_access,
)

EVENT_DISCOVERY_VERSION = "intraday_event_conditioned_alpha_v2_explicit_ev"
BRANCH_GAP = "gap_absorption"
BRANCH_FAILED_AUCTION = "failed_auction"
BRANCH_ONE_MINUTE_VETO = "one_minute_veto"
BRANCH_ALPHA_CEILING = "alpha_ceiling"
BRANCHES = (
    BRANCH_GAP,
    BRANCH_FAILED_AUCTION,
    BRANCH_ONE_MINUTE_VETO,
    BRANCH_ALPHA_CEILING,
)
HORIZONS_MINUTES = (15, 30, 60, 120)
MIN_SCORE_EVENTS = 30
MIN_CONFIRMATION_EVENTS = 50
ALPHA_CEILING_DECISION_GRID_MINUTES = 60
EV_RIDGE_PENALTY = 8.0


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    role: str
    sign: int
    job: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "sign": self.sign,
            "job": self.job,
        }


COMMON_FEATURES = (
    FeatureSpec("relative_volume_surprise", "alpha", 1, "Separate abnormal participation from ordinary activity."),
    FeatureSpec("effort_result_ratio", "alpha", 1, "Measure aggressive-flow effort that produces unusually little price progress."),
    FeatureSpec("directional_flow_shift", "alpha", 1, "Measure whether signed flow is moving toward the hypothesized direction."),
    FeatureSpec("large_trade_share", "alpha", 1, "Distinguish broad noise from larger-print participation."),
    FeatureSpec("idiosyncratic_return", "regime", 1, "Remove contemporaneous broad-market and sector movement."),
    FeatureSpec("adverse_market_alignment", "veto", -1, "Reject events fighting an accelerating market move."),
    FeatureSpec("effective_spread_bps", "execution", -1, "Reject events whose quoted execution cost can consume the edge."),
)

FEATURE_CATALOG: dict[str, tuple[FeatureSpec, ...]] = {
    BRANCH_GAP: (
        FeatureSpec("gap_atr", "setup", 1, "Normalize the overnight move by the symbol's prior realized range."),
        FeatureSpec("idiosyncratic_gap_atr", "alpha", 1, "Remove market and sector overnight repricing from the stock gap."),
        FeatureSpec("gap_fill_fraction", "setup", 1, "Measure actual rejection of the opening gap."),
        FeatureSpec("flow_exhaustion", "alpha", 1, "Measure aggressive flow deceleration against the gap direction."),
        FeatureSpec("distance_from_prior_extreme_atr", "regime", 1, "Locate the response relative to prior-day value extremes."),
        *COMMON_FEATURES,
    ),
    BRANCH_FAILED_AUCTION: (
        FeatureSpec("excursion_atr", "setup", 1, "Normalize the excursion beyond the prior range."),
        FeatureSpec("progress_efficiency", "alpha", -1, "Detect large effort with little additional breakout progress."),
        FeatureSpec("reentry_depth_atr", "alpha", 1, "Measure rejection back inside the prior range when it is already knowable."),
        FeatureSpec("flow_exhaustion", "alpha", 1, "Measure signed-flow deceleration after the impulse."),
        FeatureSpec("minutes_from_open", "regime", -1, "Distinguish opening-auction failures from late-session moves."),
        *COMMON_FEATURES,
    ),
    BRANCH_ONE_MINUTE_VETO: (
        FeatureSpec("intrabar_directional_return", "veto", 1, "Reject a base event when the completed signal bar moved adversely."),
        FeatureSpec("intrabar_last_5m_directional_return", "veto", 1, "Reject late adverse acceleration inside the signal bar."),
        FeatureSpec("intrabar_directional_imbalance", "veto", 1, "Reject aggressive 1m flow opposing the base direction."),
        FeatureSpec("intrabar_imbalance_improvement", "veto", 1, "Reject deteriorating 1m signed flow."),
        FeatureSpec("intrabar_spread_bps", "veto", -1, "Reject an abnormally expensive 1m liquidity state."),
    ),
    BRANCH_ALPHA_CEILING: (
        FeatureSpec("bar_return_1", "alpha", 1, "Current completed-bar return."),
        FeatureSpec("bar_return_2", "alpha", 1, "Two-bar return ending at the decision bar."),
        FeatureSpec("bar_return_4", "alpha", 1, "Four-bar return ending at the decision bar."),
        FeatureSpec("realized_volatility_4", "regime", 1, "Four-bar realized volatility."),
        FeatureSpec("bar_range_bps", "regime", 1, "Completed-bar high-low range."),
        FeatureSpec("close_location", "alpha", 1, "Close location inside the completed bar."),
        FeatureSpec("distance_from_session_vwap", "alpha", 1, "Distance from knowable session VWAP."),
        FeatureSpec("opening_range_position", "alpha", 1, "Position inside or beyond the opening range."),
        FeatureSpec("gap_percent", "regime", 1, "Overnight gap known at the open."),
        FeatureSpec("relative_volume_surprise", "alpha", 1, "Same-slot relative volume."),
        FeatureSpec("signed_trade_imbalance", "alpha", 1, "Signed aggressive trade flow in the completed bar."),
        FeatureSpec("directional_flow_shift", "alpha", 1, "Change in signed imbalance from the prior bar."),
        FeatureSpec("large_trade_share", "alpha", 1, "Share of volume in larger prints."),
        FeatureSpec("idiosyncratic_return", "alpha", 1, "Return residual to contemporaneous market and sector movement."),
        FeatureSpec("market_return", "regime", 1, "Contemporaneous broad-market direction."),
        FeatureSpec("minutes_from_open", "regime", 1, "Intraday time slot."),
        FeatureSpec("effective_spread_bps", "execution", -1, "Conditional round-trip execution state."),
        FeatureSpec("news_last_15m", "alpha", 1, "Point-in-time article count in the last 15 minutes."),
        FeatureSpec("news_last_60m", "alpha", 1, "Point-in-time article count in the last 60 minutes."),
        FeatureSpec("news_last_24h", "regime", 1, "Point-in-time article count in the last 24 hours."),
        FeatureSpec("minutes_since_last_news", "regime", -1, "Minutes since the latest known article version."),
        FeatureSpec("first_news_today", "alpha", 1, "Whether this is the first known article for the symbol today."),
        FeatureSpec("news_frequency_surprise", "alpha", 1, "Recent article burst versus 24-hour baseline."),
        FeatureSpec("positive_news_score", "alpha", 1, "Lightweight positive keyword count in known recent news."),
        FeatureSpec("negative_news_score", "alpha", -1, "Lightweight negative keyword count in known recent news."),
        FeatureSpec("earnings_event", "regime", 1, "Known recent article mentions earnings/results."),
        FeatureSpec("guidance_event", "regime", 1, "Known recent article mentions guidance/outlook."),
        FeatureSpec("analyst_event", "regime", 1, "Known recent article mentions analyst/rating action."),
        FeatureSpec("ma_event", "regime", 1, "Known recent article mentions M&A."),
        FeatureSpec("regulatory_event", "regime", 1, "Known recent article mentions regulatory activity."),
        FeatureSpec("product_event", "regime", 1, "Known recent article mentions product/news launch."),
        FeatureSpec("management_event", "regime", 1, "Known recent article mentions management change."),
        FeatureSpec("legal_event", "regime", 1, "Known recent article mentions legal activity."),
        FeatureSpec("option_contracts", "regime", 1, "Available option-contract surface breadth known before decision."),
        FeatureSpec("option_atm_iv", "alpha", 1, "Near-the-money implied volatility from the latest known option surface."),
        FeatureSpec("option_put_call_iv_skew", "alpha", 1, "Near-the-money put IV minus call IV."),
        FeatureSpec("option_iv_term_slope", "regime", 1, "Far-expiry IV minus near-expiry IV."),
        FeatureSpec("option_call_volume", "alpha", 1, "Latest call option trade-size proxy in the known surface."),
        FeatureSpec("option_put_volume", "alpha", 1, "Latest put option trade-size proxy in the known surface."),
        FeatureSpec("option_put_call_volume_ratio", "alpha", 1, "Put option activity divided by call activity."),
        FeatureSpec("option_gamma_proxy", "alpha", 1, "Surface gamma exposure proxy using displayed size and latest trade size."),
        FeatureSpec("option_delta_abs_proxy", "regime", 1, "Absolute delta exposure proxy using displayed size and latest trade size."),
        FeatureSpec("option_near_atm_spread_bps", "execution", -1, "Near-ATM option spread state as a liquidity/attention proxy."),
        FeatureSpec("option_minutes_since_snapshot", "regime", -1, "Age of the option surface snapshot available at decision time."),
    ),
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "as_dict"):
        return _jsonable(value.as_dict())
    return value


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _mean(values: Iterable[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None and isfinite(float(value))]
    return fmean(usable) if usable else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if isfinite(float(value)))
    if not ordered:
        return None
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _rank_percentile(value: float | None, baseline: Sequence[float]) -> float | None:
    if value is None or not baseline:
        return None
    ordered = baseline
    below = bisect_left(ordered, value)
    equal = bisect_right(ordered, value) - below
    return (below + 0.5 * equal) / len(ordered)


def _direction_sign(direction: str) -> float:
    return 1.0 if direction == "long" else -1.0


def _bar_return(row: dict[str, Any]) -> float | None:
    open_price = _finite(row.get("open"))
    close = _finite(row.get("close"))
    if open_price is None or close is None or open_price <= 0:
        return None
    return close / open_price - 1


def _true_range_fraction(row: dict[str, Any], previous_close: float | None) -> float | None:
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    if high is None or low is None or previous_close is None or previous_close <= 0:
        return None
    return max(high - low, abs(high - previous_close), abs(low - previous_close)) / previous_close


def _flow_map(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    symbols: Sequence[str],
) -> dict[tuple[str, datetime], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, timestamp, trade_count, total_volume,
               signed_trade_imbalance, signed_trade_count_imbalance,
               large_trade_share, unclassified_share, effective_spread_bps
        FROM research_dataset_trade_flow_features
        WHERE dataset_id = %s AND timeframe = %s AND symbol = ANY(%s)
        ORDER BY symbol, timestamp
        """,
        (dataset_id, timeframe, list(symbols)),
    ).fetchall()
    return {
        (str(row["symbol"]).upper(), row["timestamp"]): dict(row)
        for row in rows
    }


def _one_minute_predictor_fingerprint(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    source: str,
    feed: str,
) -> dict[str, Any]:
    """Lock mutable 1m side channels without reading any forward return."""
    candles = conn.execute(
        """
        SELECT COUNT(*) AS rows, MIN(timestamp) AS first_at, MAX(timestamp) AS last_at,
               COALESCE(SUM(volume), 0) AS volume_sum,
               COALESCE(SUM(open + high + low + close), 0) AS ohlc_checksum
        FROM candles
        WHERE symbol = ANY(%s) AND timeframe = '1m' AND source = %s
          AND timestamp >= %s AND timestamp <= %s
        """,
        (list(symbols), source, start, end),
    ).fetchone()
    flow = conn.execute(
        """
        SELECT COUNT(*) AS rows, MIN(timestamp) AS first_at, MAX(timestamp) AS last_at,
               COALESCE(SUM(total_volume), 0) AS volume_sum,
               COALESCE(SUM(COALESCE(signed_trade_imbalance, 0)), 0) AS imbalance_checksum
        FROM intraday_trade_flow_features
        WHERE symbol = ANY(%s) AND timeframe = '1m' AND feed = %s
          AND timestamp >= %s AND timestamp <= %s
        """,
        (list(symbols), feed, start, end),
    ).fetchone()
    if int((candles or {}).get("rows") or 0) == 0 or int((flow or {}).get("rows") or 0) == 0:
        raise ValueError(
            "The declared 1m veto requires both 1m candles and 1m SIP trade-flow "
            "inside the frozen dataset window."
        )
    evidence = {
        "window_start": start,
        "window_end": end,
        "source": source,
        "feed": feed,
        "candles": dict(candles or {}),
        "trade_flow": dict(flow or {}),
    }
    return {
        "evidence": _jsonable(evidence),
        "fingerprint": sha256(dumps(evidence, sort_keys=True, default=str).encode()).hexdigest(),
        "return_blind": True,
        "outcome_fields_accessed": [],
    }


def _assert_one_minute_fingerprint(
    conn: psycopg.Connection,
    *,
    declaration: dict[str, Any],
) -> None:
    specification = declaration["specification"]
    expected = specification.get("one_minute_predictor_fingerprint")
    if expected is None:
        return
    current = _one_minute_predictor_fingerprint(
        conn,
        symbols=list(declaration["symbols"]),
        start=datetime.fromisoformat(expected["evidence"]["window_start"]),
        end=datetime.fromisoformat(expected["evidence"]["window_end"]),
        source=str(specification["source"]),
        feed=str(specification["feed"]),
    )
    if current["fingerprint"] != expected["fingerprint"]:
        raise ValueError(
            "Frozen 1m predictor side channel changed after declaration. "
            "Create a new declaration; do not mix the changed 1m data into this study."
        )


def _attach_flow(
    candles: dict[str, list[dict[str, Any]]],
    flow: dict[tuple[str, datetime], dict[str, Any]],
) -> None:
    for symbol, rows in candles.items():
        for row in rows:
            side = flow.get((symbol.upper(), row["timestamp"])) or {}
            for key in (
                "trade_count",
                "total_volume",
                "signed_trade_imbalance",
                "signed_trade_count_imbalance",
                "large_trade_share",
                "unclassified_share",
                "effective_spread_bps",
            ):
                row[f"flow_{key}"] = side.get(key)


def _context_maps(
    candles: dict[str, list[dict[str, Any]]],
    sectors: dict[str, str],
) -> dict[str, Any]:
    returns: dict[tuple[str, datetime], float] = {}
    by_time_sector: dict[tuple[datetime, str], list[float]] = defaultdict(list)
    for symbol, rows in candles.items():
        for row in rows:
            value = _bar_return(row)
            if value is None:
                continue
            returns[(symbol, row["timestamp"])] = value
            sector = sectors.get(symbol)
            if sector:
                by_time_sector[(row["timestamp"], sector)].append(value)
    sector_returns = {
        key: fmean(values) for key, values in by_time_sector.items() if values
    }
    market = {
        timestamp: value
        for (symbol, timestamp), value in returns.items()
        if symbol == "SPY"
    }
    overnight: dict[tuple[str, date], float] = {}
    by_date_sector: dict[tuple[date, str], list[float]] = defaultdict(list)
    for symbol, rows in candles.items():
        previous_close: float | None = None
        previous_session: date | None = None
        for session_date, session in ordered_regular_sessions(rows, timeframe=str(rows[0]["timeframe"])):
            if not session:
                continue
            opening = _finite(session[0].get("open"))
            if (
                opening is not None
                and previous_close is not None
                and previous_close > 0
                and is_consecutive_session(previous_session, session_date)
            ):
                gap = opening / previous_close - 1
                overnight[(symbol, session_date)] = gap
                sector = sectors.get(symbol)
                if sector:
                    by_date_sector[(session_date, sector)].append(gap)
            previous_close = _finite(session[-1].get("close"))
            previous_session = session_date
    overnight_sector = {
        key: fmean(values) for key, values in by_date_sector.items() if values
    }
    overnight_market = {
        session_date: value
        for (symbol, session_date), value in overnight.items()
        if symbol == "SPY"
    }
    return {
        "returns": returns,
        "sector": sector_returns,
        "market": market,
        "overnight": overnight,
        "overnight_sector": overnight_sector,
        "overnight_market": overnight_market,
    }


def _prior_atr(rows: Sequence[dict[str, Any]], index: int, lookback: int = 20) -> float | None:
    values: list[float] = []
    start = max(1, index - lookback)
    for cursor in range(start, index):
        previous = _finite(rows[cursor - 1].get("close"))
        value = _true_range_fraction(rows[cursor], previous)
        if value is not None:
            values.append(value)
    return fmean(values) if values else None


def _base_features(
    *,
    symbol: str,
    row: dict[str, Any],
    previous_row: dict[str, Any] | None,
    direction: str,
    contexts: dict[str, Any],
    sectors: dict[str, str],
) -> dict[str, Any]:
    sign = _direction_sign(direction)
    bar_ret = _bar_return(row) or 0.0
    imbalance = _finite(row.get("flow_signed_trade_imbalance"))
    previous_imbalance = _finite((previous_row or {}).get("flow_signed_trade_imbalance"))
    relative_volume = _finite(row.get("session_relative_volume"))
    market_return = contexts["market"].get(row["timestamp"], 0.0)
    sector_return = contexts["sector"].get(
        (row["timestamp"], sectors.get(symbol, "")),
        market_return,
    )
    flow_effort = abs(imbalance or 0.0) * max(relative_volume or 0.0, 0.0)
    effort_result = min(1000.0, flow_effort / max(abs(bar_ret), 0.00001))
    directional_shift = (
        sign * (imbalance - previous_imbalance)
        if imbalance is not None and previous_imbalance is not None
        else None
    )
    return {
        "relative_volume_surprise": relative_volume,
        "effort_result_ratio": effort_result,
        "directional_flow_shift": directional_shift,
        "large_trade_share": _finite(row.get("flow_large_trade_share")),
        "effective_spread_bps": _finite(row.get("flow_effective_spread_bps")),
        "market_return": market_return,
        "sector_return": sector_return,
        "idiosyncratic_return": sign * (bar_ret - 0.5 * market_return - 0.5 * sector_return),
        "adverse_market_alignment": sign * market_return < 0,
        "signed_imbalance": imbalance,
        "bar_return": bar_ret,
        "minutes_from_open": _finite(row.get("minutes_from_open")),
        "signal_bar_timestamp": row["timestamp"],
    }


def _outcomes(
    session: Sequence[dict[str, Any]],
    *,
    decision_index: int,
    direction: str,
    timeframe: str,
    cost_bps: float,
    horizons: Sequence[int],
) -> dict[str, Any]:
    entry_index = decision_index + 1
    if entry_index >= len(session):
        return {}
    entry = _finite(session[entry_index].get("open"))
    if entry is None or entry <= 0:
        return {}
    frame_minutes = timeframe_minutes(timeframe)
    sign = _direction_sign(direction)
    result: dict[str, Any] = {}
    for horizon in horizons:
        if horizon % frame_minutes:
            result[f"{horizon}m"] = {"available": False, "reason": "horizon_not_on_parent_bar_grid"}
            continue
        bars = horizon // frame_minutes
        exit_index = entry_index + bars - 1
        if exit_index >= len(session):
            result[f"{horizon}m"] = {"available": False, "reason": "session_end_before_horizon"}
            continue
        path = session[entry_index : exit_index + 1]
        exit_price = _finite(path[-1].get("close"))
        highs = [_finite(row.get("high")) for row in path]
        lows = [_finite(row.get("low")) for row in path]
        if exit_price is None or any(value is None for value in highs + lows):
            result[f"{horizon}m"] = {"available": False, "reason": "missing_ohlc"}
            continue
        gross = sign * (exit_price / entry - 1)
        path_returns = [
            value
            for row in path
            if (value := _bar_return(row)) is not None
        ]
        future_spreads = [
            value
            for row in path
            if (value := _finite(row.get("flow_effective_spread_bps"))) is not None
        ]
        decision_spread = _finite(session[decision_index].get("flow_effective_spread_bps"))
        if direction == "long":
            mfe = max(float(value) for value in highs) / entry - 1
            mae = max(0.0, 1 - min(float(value) for value in lows) / entry)
        else:
            mfe = 1 - min(float(value) for value in lows) / entry
            mae = max(0.0, max(float(value) for value in highs) / entry - 1)
        result[f"{horizon}m"] = {
            "available": True,
            "entry_price": entry,
            "exit_price": exit_price,
            "gross_return": gross,
            "gross_return_bps": gross * 10_000,
            "net_return": gross - cost_bps / 10_000,
            "net_return_bps": gross * 10_000 - cost_bps,
            "mfe_bps": max(0.0, mfe) * 10_000,
            "mae_bps": mae * 10_000,
            "future_realized_volatility_bps": (
                pstdev(path_returns) * 10_000 if len(path_returns) > 1 else 0.0
            ),
            "future_mean_spread_bps": _mean(future_spreads),
            "liquidity_deterioration_bps": (
                (_mean(future_spreads) or 0.0) - decision_spread
                if future_spreads and decision_spread is not None
                else None
            ),
            "bars": bars,
        }
    eod_path = session[entry_index:]
    if eod_path:
        eod_exit = _finite(eod_path[-1].get("close"))
        eod_highs = [_finite(row.get("high")) for row in eod_path]
        eod_lows = [_finite(row.get("low")) for row in eod_path]
        if eod_exit is not None and not any(value is None for value in eod_highs + eod_lows):
            gross = sign * (eod_exit / entry - 1)
            if direction == "long":
                mfe = max(float(value) for value in eod_highs) / entry - 1
                mae = max(0.0, 1 - min(float(value) for value in eod_lows) / entry)
            else:
                mfe = 1 - min(float(value) for value in eod_lows) / entry
                mae = max(0.0, max(float(value) for value in eod_highs) / entry - 1)
            path_returns = [
                value for row in eod_path if (value := _bar_return(row)) is not None
            ]
            future_spreads = [
                value
                for row in eod_path
                if (value := _finite(row.get("flow_effective_spread_bps"))) is not None
            ]
            decision_spread = _finite(session[decision_index].get("flow_effective_spread_bps"))
            result["eod"] = {
                "available": True,
                "entry_price": entry,
                "exit_price": eod_exit,
                "gross_return": gross,
                "gross_return_bps": gross * 10_000,
                "net_return": gross - cost_bps / 10_000,
                "net_return_bps": gross * 10_000 - cost_bps,
                "mfe_bps": max(0.0, mfe) * 10_000,
                "mae_bps": mae * 10_000,
                "future_realized_volatility_bps": (
                    pstdev(path_returns) * 10_000 if len(path_returns) > 1 else 0.0
                ),
                "future_mean_spread_bps": _mean(future_spreads),
                "liquidity_deterioration_bps": (
                    (_mean(future_spreads) or 0.0) - decision_spread
                    if future_spreads and decision_spread is not None
                    else None
                ),
                "bars": len(eod_path),
            }
        else:
            result["eod"] = {"available": False, "reason": "missing_ohlc"}
    return result


def _event(
    *,
    event_key: str,
    branch: str,
    stage: str,
    symbol: str,
    session_date: date,
    decision_row: dict[str, Any],
    decision_index: int,
    session: Sequence[dict[str, Any]],
    direction: str,
    timeframe: str,
    features: dict[str, Any],
    labels: dict[str, Any],
    cost_model: dict[str, Any],
    horizons: Sequence[int],
) -> dict[str, Any]:
    decision_timestamp = bar_close_timestamp(decision_row["timestamp"], timeframe=timeframe)
    cost_bps = estimated_round_trip_cost_bps(
        cost_model,
        symbol=symbol,
        timestamp=decision_timestamp,
    )
    return {
        "event_key": event_key,
        "branch": branch,
        "stage": stage,
        "symbol": symbol,
        "session_date": session_date,
        "decision_timestamp": decision_timestamp,
        "direction": direction,
        "features": features,
        "labels": labels,
        "cost_bps": cost_bps,
        "outcomes": _outcomes(
            session,
            decision_index=decision_index,
            direction=direction,
            timeframe=timeframe,
            cost_bps=cost_bps,
            horizons=horizons,
        ),
    }


def _detect_gap_absorption(
    candles: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    contexts: dict[str, Any],
    sectors: dict[str, str],
    cost_model: dict[str, Any],
    horizons: Sequence[int],
    decision_start: datetime | None = None,
    decision_end: datetime | None = None,
) -> list[dict[str, Any]]:
    if timeframe != "30m":
        return []
    events: list[dict[str, Any]] = []
    for symbol, rows in candles.items():
        previous_close: float | None = None
        previous_high: float | None = None
        previous_low: float | None = None
        previous_session: date | None = None
        regular_history: list[dict[str, Any]] = []
        for session_date, session in ordered_regular_sessions(rows, timeframe=timeframe):
            if not session:
                continue
            last = session[-1]
            adjacent = is_consecutive_session(previous_session, session_date)
            if adjacent and previous_close and len(session) >= 4:
                opening = session[0]
                decision = session[1]
                session_open = _finite(opening.get("open"))
                decision_close = _finite(decision.get("close"))
                relative_volume = _finite(decision.get("session_relative_volume"))
                if session_open and decision_close is not None:
                    gap = session_open / previous_close - 1
                    fill = (
                        (session_open - decision_close) / (session_open - previous_close)
                        if session_open != previous_close
                        else 0.0
                    )
                    knowable_at = bar_close_timestamp(decision["timestamp"], timeframe=timeframe)
                    if (
                        abs(gap) >= 0.003
                        and (relative_volume or 0) >= 1.5
                        and fill >= 0.5
                        and (decision_start is None or knowable_at >= decision_start)
                        and (decision_end is None or knowable_at < decision_end)
                    ):
                        direction = "long" if gap < 0 else "short"
                        atr = _prior_atr(regular_history, len(regular_history)) or 0.005
                        market_gap = contexts["overnight_market"].get(session_date, 0.0)
                        sector_gap = contexts["overnight_sector"].get(
                            (session_date, sectors.get(symbol, "")),
                            market_gap,
                        )
                        base = _base_features(
                            symbol=symbol,
                            row=decision,
                            previous_row=opening,
                            direction=direction,
                            contexts=contexts,
                            sectors=sectors,
                        )
                        gap_sign = 1.0 if gap > 0 else -1.0
                        current_imbalance = _finite(decision.get("flow_signed_trade_imbalance"))
                        opening_imbalance = _finite(opening.get("flow_signed_trade_imbalance"))
                        flow_exhaustion = (
                            -gap_sign * (current_imbalance - opening_imbalance)
                            if current_imbalance is not None and opening_imbalance is not None
                            else None
                        )
                        prior_extreme = previous_low if direction == "long" else previous_high
                        distance = (
                            _direction_sign(direction) * (decision_close - prior_extreme) / previous_close / atr
                            if prior_extreme is not None and atr > 0
                            else None
                        )
                        features = {
                            **base,
                            "gap_return": gap,
                            "gap_atr": abs(gap) / atr,
                            "idiosyncratic_gap_atr": abs(gap - 0.5 * market_gap - 0.5 * sector_gap) / atr,
                            "gap_fill_fraction": fill,
                            "flow_exhaustion": flow_exhaustion,
                            "distance_from_prior_extreme_atr": distance,
                            "prior_session_high": previous_high,
                            "prior_session_low": previous_low,
                        }
                        events.append(
                            _event(
                                event_key="30m_gap_absorption",
                                branch=BRANCH_GAP,
                                stage="absorbed_gap",
                                symbol=symbol,
                                session_date=session_date,
                                decision_row=decision,
                                decision_index=1,
                                session=session,
                                direction=direction,
                                timeframe=timeframe,
                                features=features,
                                labels={"gap_direction": "down" if gap < 0 else "up"},
                                cost_model=cost_model,
                                horizons=horizons,
                            )
                        )
            previous_close = _finite(last.get("close"))
            previous_high = max((_finite(row.get("high")) or 0) for row in session)
            previous_low = min((_finite(row.get("low")) or float("inf")) for row in session)
            previous_session = session_date
            regular_history.extend(session)
    return events


def _detect_failed_auctions(
    candles: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    contexts: dict[str, Any],
    sectors: dict[str, str],
    cost_model: dict[str, Any],
    horizons: Sequence[int],
    decision_start: datetime | None = None,
    decision_end: datetime | None = None,
) -> list[dict[str, Any]]:
    if timeframe != "15m":
        return []
    events: list[dict[str, Any]] = []
    for symbol, rows in candles.items():
        for session_date, session in ordered_regular_sessions(rows, timeframe=timeframe):
            index = 4
            while index < len(session) - 2:
                prior = session[index - 4 : index]
                current = session[index]
                atr = _prior_atr(session, index) or 0.003
                prior_high = max(float(row["high"]) for row in prior)
                prior_low = min(float(row["low"]) for row in prior)
                close = float(current["close"])
                high = float(current["high"])
                low = float(current["low"])
                upper = high > prior_high * (1 + 0.05 * atr) and close > prior_high
                lower = low < prior_low * (1 - 0.05 * atr) and close < prior_low
                relative_volume = _finite(current.get("session_relative_volume")) or 0.0
                if not (upper or lower) or relative_volume < 1.2:
                    index += 1
                    continue
                breakout_side = "up" if upper else "down"
                direction = "short" if upper else "long"
                level = prior_high if upper else prior_low
                excursion = (high - level) / close if upper else (level - low) / close
                failed_index: int | None = None
                for candidate_index in range(index + 1, min(len(session), index + 3)):
                    candidate_close = float(session[candidate_index]["close"])
                    if (upper and candidate_close <= prior_high) or (lower and candidate_close >= prior_low):
                        failed_index = candidate_index
                        break
                base = _base_features(
                    symbol=symbol,
                    row=current,
                    previous_row=session[index - 1],
                    direction=direction,
                    contexts=contexts,
                    sectors=sectors,
                )
                imbalance = _finite(current.get("flow_signed_trade_imbalance"))
                previous_imbalance = _finite(session[index - 1].get("flow_signed_trade_imbalance"))
                flow_exhaustion = (
                    _direction_sign(direction) * (imbalance - previous_imbalance)
                    if imbalance is not None and previous_imbalance is not None
                    else None
                )
                impulse = abs(_bar_return(current) or 0.0)
                features = {
                    **base,
                    "excursion_atr": excursion / atr,
                    "progress_efficiency": impulse / max(relative_volume, 0.01),
                    "reentry_depth_atr": None,
                    "flow_exhaustion": flow_exhaustion,
                }
                probe_knowable_at = bar_close_timestamp(current["timestamp"], timeframe=timeframe)
                probe_in_window = (
                    (decision_start is None or probe_knowable_at >= decision_start)
                    and (decision_end is None or probe_knowable_at < decision_end)
                )
                if probe_in_window:
                    events.append(_event(
                        event_key="15m_failed_auction_probe",
                        branch=BRANCH_FAILED_AUCTION,
                        stage="breakout_probe",
                        symbol=symbol,
                        session_date=session_date,
                        decision_row=current,
                        decision_index=index,
                        session=session,
                        direction=direction,
                        timeframe=timeframe,
                        features=features,
                        labels={
                            "breakout_side": breakout_side,
                            "failure_within_2_bars": failed_index is not None,
                        },
                        cost_model=cost_model,
                        horizons=horizons,
                    ))
                if failed_index is not None and failed_index + 1 < len(session):
                    failure = session[failed_index]
                    failure_close = float(failure["close"])
                    reentry = (
                        (prior_high - failure_close) / failure_close
                        if upper
                        else (failure_close - prior_low) / failure_close
                    )
                    confirmed_base = _base_features(
                        symbol=symbol,
                        row=failure,
                        previous_row=current,
                        direction=direction,
                        contexts=contexts,
                        sectors=sectors,
                    )
                    failure_imbalance = _finite(failure.get("flow_signed_trade_imbalance"))
                    confirmed_features = {
                        **features,
                        **confirmed_base,
                        "reentry_depth_atr": reentry / atr,
                        "flow_exhaustion": (
                            _direction_sign(direction) * (failure_imbalance - imbalance)
                            if failure_imbalance is not None and imbalance is not None
                            else flow_exhaustion
                        ),
                    }
                    failure_knowable_at = bar_close_timestamp(failure["timestamp"], timeframe=timeframe)
                    failure_in_window = (
                        (decision_start is None or failure_knowable_at >= decision_start)
                        and (decision_end is None or failure_knowable_at < decision_end)
                    )
                    if failure_in_window:
                        events.append(_event(
                            event_key="15m_failed_auction_confirmed",
                            branch=BRANCH_FAILED_AUCTION,
                            stage="range_reentry",
                            symbol=symbol,
                            session_date=session_date,
                            decision_row=failure,
                            decision_index=failed_index,
                            session=session,
                            direction=direction,
                            timeframe=timeframe,
                            features=confirmed_features,
                            labels={"breakout_side": breakout_side, "failure_within_2_bars": True},
                            cost_model=cost_model,
                            horizons=horizons,
                        ))
                    index = failed_index + 1
                else:
                    index += 2
    return events


def _intrabar_rows(
    conn: psycopg.Connection,
    events: Sequence[dict[str, Any]],
    *,
    source: str,
    feed: str,
    parent_timeframe: str,
) -> dict[int, dict[str, Any]]:
    """Aggregate only the completed 1m signal bars needed by base events."""
    if not events:
        return {}
    parent_minutes = timeframe_minutes(parent_timeframe)
    result: dict[int, dict[str, Any]] = {}
    for batch_start in range(0, len(events), 200):
        batch = events[batch_start : batch_start + 200]
        indices = list(range(batch_start, batch_start + len(batch)))
        symbols = [str(event["symbol"]) for event in batch]
        ends = [event["decision_timestamp"] for event in batch]
        starts = [value - timedelta(minutes=parent_minutes) for value in ends]
        candle_rows = conn.execute(
            """
            WITH requested AS (
                SELECT * FROM unnest(%s::int[], %s::text[], %s::timestamptz[], %s::timestamptz[])
                    AS r(event_index, symbol, start_at, end_at)
            )
            SELECT r.event_index, c.timestamp, c.open, c.high, c.low, c.close, c.volume
            FROM requested r
            JOIN candles c ON c.symbol = r.symbol AND c.timeframe = '1m'
                AND c.source = %s AND c.timestamp >= r.start_at AND c.timestamp < r.end_at
            ORDER BY r.event_index, c.timestamp
            """,
            (indices, symbols, starts, ends, source),
        ).fetchall()
        flow_rows = conn.execute(
            """
            WITH requested AS (
                SELECT * FROM unnest(%s::int[], %s::text[], %s::timestamptz[], %s::timestamptz[])
                    AS r(event_index, symbol, start_at, end_at)
            )
            SELECT r.event_index, f.timestamp, f.total_volume,
                   f.signed_trade_imbalance, f.effective_spread_bps
            FROM requested r
            JOIN intraday_trade_flow_features f
              ON f.symbol = r.symbol AND f.timeframe = '1m' AND f.feed = %s
             AND f.timestamp >= r.start_at AND f.timestamp < r.end_at
            ORDER BY r.event_index, f.timestamp
            """,
            (indices, symbols, starts, ends, feed),
        ).fetchall()
        by_candle: dict[int, list[dict[str, Any]]] = defaultdict(list)
        by_flow: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in candle_rows:
            by_candle[int(row["event_index"])].append(dict(row))
        for row in flow_rows:
            by_flow[int(row["event_index"])].append(dict(row))
        for event_index in indices:
            bars = by_candle.get(event_index, [])
            flows = by_flow.get(event_index, [])
            if not bars:
                continue
            event = events[event_index]
            sign = _direction_sign(str(event["direction"]))
            first_open = _finite(bars[0].get("open"))
            last_close = _finite(bars[-1].get("close"))
            last_5 = bars[-5:]
            last_5_open = _finite(last_5[0].get("open")) if last_5 else None
            last_5_close = _finite(last_5[-1].get("close")) if last_5 else None
            half = max(1, len(flows) // 2)
            first_imbalance = _mean(_finite(row.get("signed_trade_imbalance")) for row in flows[:half])
            second_imbalance = _mean(_finite(row.get("signed_trade_imbalance")) for row in flows[half:])
            result[event_index] = {
                "intrabar_coverage": len(bars),
                "intrabar_directional_return": (
                    sign * (last_close / first_open - 1)
                    if first_open and last_close is not None
                    else None
                ),
                "intrabar_last_5m_directional_return": (
                    sign * (last_5_close / last_5_open - 1)
                    if last_5_open and last_5_close is not None
                    else None
                ),
                "intrabar_directional_imbalance": (
                    sign * second_imbalance if second_imbalance is not None else None
                ),
                "intrabar_imbalance_improvement": (
                    sign * (second_imbalance - first_imbalance)
                    if first_imbalance is not None and second_imbalance is not None
                    else None
                ),
                "intrabar_spread_bps": _mean(
                    _finite(row.get("effective_spread_bps")) for row in flows
                ),
            }
    return result


def _window_return(rows: Sequence[dict[str, Any]], start: int, end: int) -> float | None:
    if start < 0 or end >= len(rows) or start > end:
        return None
    opening = _finite(rows[start].get("open"))
    closing = _finite(rows[end].get("close"))
    if opening is None or closing is None or opening <= 0:
        return None
    return closing / opening - 1


def _detect_alpha_ceiling_panel(
    candles: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    contexts: dict[str, Any],
    sectors: dict[str, str],
    cost_model: dict[str, Any],
    horizons: Sequence[int],
    news_index: Any | None = None,
    option_index: Any | None = None,
    decision_start: datetime | None = None,
    decision_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build a broad, non-overlapping-enough information-set panel.

    Decisions are sampled on a frozen hourly grid.  Every predictor comes
    from the completed decision bar or earlier.  Direction is deliberately
    left as ``long`` in the raw outcome; the frozen EV model compares the
    long and short decompositions and chooses a direction later.
    """
    events: list[dict[str, Any]] = []
    for symbol, rows in candles.items():
        for session_date, session in ordered_regular_sessions(rows, timeframe=timeframe):
            for index, current in enumerate(session):
                if index < 3 or index + 1 >= len(session):
                    continue
                minutes_from_open = int(_finite(current.get("minutes_from_open")) or 0)
                if minutes_from_open % ALPHA_CEILING_DECISION_GRID_MINUTES:
                    continue
                knowable_at = bar_close_timestamp(current["timestamp"], timeframe=timeframe)
                if decision_start is not None and knowable_at < decision_start:
                    continue
                if decision_end is not None and knowable_at >= decision_end:
                    continue
                open_price = _finite(current.get("open"))
                high = _finite(current.get("high"))
                low = _finite(current.get("low"))
                close = _finite(current.get("close"))
                if not open_price or high is None or low is None or close is None:
                    continue
                bar_returns = [
                    value
                    for row in session[index - 3 : index + 1]
                    if (value := _bar_return(row)) is not None
                ]
                imbalance = _finite(current.get("flow_signed_trade_imbalance"))
                prior_imbalance = _finite(session[index - 1].get("flow_signed_trade_imbalance"))
                common = _base_features(
                    symbol=symbol,
                    row=current,
                    previous_row=session[index - 1],
                    direction="long",
                    contexts=contexts,
                    sectors=sectors,
                )
                features = {
                    "bar_return_1": _bar_return(current),
                    "bar_return_2": _window_return(session, index - 1, index),
                    "bar_return_4": _window_return(session, index - 3, index),
                    "realized_volatility_4": pstdev(bar_returns) if len(bar_returns) > 1 else 0.0,
                    "bar_range_bps": (high - low) / open_price * 10_000,
                    "close_location": (close - low) / (high - low) if high > low else 0.5,
                    "distance_from_session_vwap": _finite(current.get("distance_from_session_vwap")),
                    "opening_range_position": _finite(current.get("opening_range_position")),
                    "gap_percent": _finite(current.get("gap_percent")),
                    "relative_volume_surprise": _finite(current.get("session_relative_volume")),
                    "signed_trade_imbalance": imbalance,
                    "directional_flow_shift": (
                        imbalance - prior_imbalance
                        if imbalance is not None and prior_imbalance is not None
                        else None
                    ),
                    "large_trade_share": _finite(current.get("flow_large_trade_share")),
                    "idiosyncratic_return": common["idiosyncratic_return"],
                    "market_return": common["market_return"],
                    "minutes_from_open": minutes_from_open,
                    "effective_spread_bps": _finite(current.get("flow_effective_spread_bps")),
                }
                features.update(
                    news_index.features_at(symbol, knowable_at)
                    if news_index is not None
                    else empty_news_features()
                )
                features.update(
                    option_index.features_at(symbol, knowable_at, underlying_price=close)
                    if option_index is not None
                    else empty_option_features()
                )
                events.append(
                    _event(
                        event_key=f"{timeframe}_alpha_ceiling_panel",
                        branch=BRANCH_ALPHA_CEILING,
                        stage="hourly_information_set",
                        symbol=symbol,
                        session_date=session_date,
                        decision_row=current,
                        decision_index=index,
                        session=session,
                        direction="long",
                        timeframe=timeframe,
                        features=features,
                        labels={
                            "dynamic_direction": True,
                            "decision_grid_minutes": ALPHA_CEILING_DECISION_GRID_MINUTES,
                        },
                        cost_model=cost_model,
                        horizons=horizons,
                    )
                )
    return events


def detect_events(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    branches: Sequence[str],
    symbols: Sequence[str],
    cost_model: dict[str, Any],
    horizons: Sequence[int],
    source: str,
    feed: str,
    include_news_features: bool = False,
    include_options_features: bool = False,
    options_feed: str = "opra",
    decision_start: datetime | None = None,
    decision_end: datetime | None = None,
) -> list[dict[str, Any]]:
    candles, _ = load_dataset_candles(
        conn,
        dataset_id=dataset_id,
        timeframe=timeframe,
        symbols=symbols,
        max_symbols=max(1, len(symbols)),
        start=decision_start - timedelta(days=40) if decision_start is not None else None,
        end=decision_end,
    )
    selected = sorted(candles)
    _attach_flow(
        candles,
        _flow_map(
            conn,
            dataset_id=dataset_id,
            timeframe=timeframe,
            symbols=selected,
        ),
    )
    sectors = sector_map(conn, selected)
    contexts = _context_maps(candles, sectors)
    news_index = None
    option_index = None
    all_timestamps = [
        row["timestamp"]
        for rows in candles.values()
        for row in rows
        if row.get("timestamp") is not None
    ]
    if include_news_features and selected:
        if all_timestamps:
            news_index = load_news_feature_index(
                conn,
                symbols=selected,
                start=min(all_timestamps),
                end=max(all_timestamps) + timedelta(days=1),
            )
    if include_options_features and selected and all_timestamps:
        option_index = load_option_feature_index(
            conn,
            symbols=selected,
            start=min(all_timestamps),
            end=max(all_timestamps) + timedelta(days=1),
            feed=options_feed,
        )
    events: list[dict[str, Any]] = []
    if BRANCH_GAP in branches or BRANCH_ONE_MINUTE_VETO in branches:
        base = _detect_gap_absorption(
            candles,
            timeframe=timeframe,
            contexts=contexts,
            sectors=sectors,
            cost_model=cost_model,
            horizons=horizons,
            decision_start=decision_start,
            decision_end=decision_end,
        )
        if BRANCH_GAP in branches:
            events.extend(base)
        if BRANCH_ONE_MINUTE_VETO in branches:
            intrabar = _intrabar_rows(
                conn,
                base,
                source=source,
                feed=feed,
                parent_timeframe=timeframe,
            )
            for index, base_event in enumerate(base):
                features = intrabar.get(index)
                if not features:
                    continue
                events.append(
                    {
                        **base_event,
                        "event_key": "30m_gap_absorption_1m_veto",
                        "branch": BRANCH_ONE_MINUTE_VETO,
                        "stage": "pre_entry_veto",
                        "features": {**base_event["features"], **features},
                        "labels": {**base_event["labels"], "base_event_key": "30m_gap_absorption"},
                    }
                )
    if BRANCH_FAILED_AUCTION in branches:
        events.extend(
            _detect_failed_auctions(
                candles,
                timeframe=timeframe,
                contexts=contexts,
                sectors=sectors,
                cost_model=cost_model,
                horizons=horizons,
                decision_start=decision_start,
                decision_end=decision_end,
            )
        )
    if BRANCH_ALPHA_CEILING in branches:
        events.extend(
            _detect_alpha_ceiling_panel(
                candles,
                timeframe=timeframe,
                contexts=contexts,
                sectors=sectors,
                cost_model=cost_model,
                horizons=horizons,
                news_index=news_index,
                option_index=option_index,
                decision_start=decision_start,
                decision_end=decision_end,
            )
        )
    return sorted(events, key=lambda row: (row["decision_timestamp"], row["event_key"], row["symbol"]))


def _normalization_model(
    events: Sequence[dict[str, Any]],
    feature_names: Sequence[str],
) -> dict[str, Any]:
    model: dict[str, Any] = {}
    for name in feature_names:
        global_values = [
            value
            for event in events
            if (value := _finite(event["features"].get(name))) is not None
        ]
        by_symbol_slot: dict[str, list[float]] = defaultdict(list)
        for event in events:
            value = _finite(event["features"].get(name))
            if value is None:
                continue
            slot = event["decision_timestamp"].strftime("%H:%M")
            by_symbol_slot[f"{event['symbol']}|{slot}"].append(value)
        global_values.sort()
        conditional = {
            key: sorted(values) for key, values in by_symbol_slot.items() if len(values) >= 10
        }
        model[name] = {
            "global": global_values,
            "conditional": {
                key: values for key, values in conditional.items()
            },
            "global_stats": {
                "mean": _mean(global_values),
                "deviation": pstdev(global_values) if len(global_values) > 1 else 0.0,
            },
            "conditional_stats": {
                key: {
                    "mean": _mean(values),
                    "deviation": pstdev(values) if len(values) > 1 else 0.0,
                }
                for key, values in conditional.items()
            },
        }
    return model


def _apply_normalization(
    events: Sequence[dict[str, Any]],
    model: dict[str, Any],
) -> None:
    for event in events:
        slot = event["decision_timestamp"].strftime("%H:%M")
        key = f"{event['symbol']}|{slot}"
        for name, spec in model.items():
            value = _finite(event["features"].get(name))
            conditional = (spec.get("conditional") or {}).get(key)
            baseline = conditional or spec.get("global") or []
            percentile = _rank_percentile(value, baseline)
            stats = (
                (spec.get("conditional_stats") or {}).get(key)
                if conditional
                else spec.get("global_stats")
            ) or {}
            mean = _finite(stats.get("mean"))
            deviation = _finite(stats.get("deviation")) or 0.0
            if not stats:
                mean = _mean(baseline)
                deviation = pstdev(baseline) if len(baseline) > 1 else 0.0
            event["features"][f"{name}_percentile"] = percentile
            event["features"][f"{name}_z"] = (
                (value - mean) / deviation
                if value is not None and mean is not None and deviation > 0
                else None
            )


def _primary_horizon(event_key: str) -> str:
    return "30m" if event_key.startswith("15m_") else "60m"


def _outcome_value(event: dict[str, Any], horizon: str, field: str = "model_net_return") -> float | None:
    row = (event.get("outcomes") or {}).get(horizon) or {}
    if not row.get("available"):
        return None
    value = _finite(row.get(field))
    if value is None and field == "model_net_return":
        value = _finite(row.get("net_return"))
    if value is None and field == "gross_return":
        net = _finite(row.get("net_return"))
        if net is not None:
            value = net + float(event.get("cost_bps") or 0.0) / 10_000
    return value


def _phase_summary(
    events: Sequence[dict[str, Any]],
    *,
    horizon: str,
    effective_trials: int,
) -> dict[str, Any]:
    outcomes = [
        {
            "value": value,
            "session_date": event["session_date"],
            "timestamp": event["decision_timestamp"],
            "symbol": event["symbol"],
        }
        for event in events
        if (value := _outcome_value(event, horizon)) is not None
    ]
    if not outcomes:
        return {"signals": 0, "mean_net_bps": None, "reason": "no_horizon_coverage"}
    stats = clustered_outcome_statistics(
        outcomes,
        effective_trials=max(1, effective_trials),
        require_symbol_diversification=True,
        bootstrap_samples=500,
    )
    stats["mean_net_bps"] = _mean(row["value"] for row in outcomes) * 10_000
    return _jsonable(stats)


def _stability_report(
    events: Sequence[dict[str, Any]],
    *,
    horizon: str,
) -> dict[str, Any]:
    """Show whether expectancy is broad or one period/liquidity state in disguise."""
    usable = [row for row in events if _outcome_value(row, horizon) is not None]
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_liquidity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
    spreads = [
        value for row in usable
        if (value := _finite(row["features"].get("effective_spread_bps"))) is not None
    ]
    low_spread = _percentile(spreads, 1 / 3)
    high_spread = _percentile(spreads, 2 / 3)
    for row in usable:
        by_year[str(row["session_date"].year)].append(row)
        spread = _finite(row["features"].get("effective_spread_bps"))
        liquidity = (
            "unknown" if spread is None or low_spread is None or high_spread is None
            else "liquid" if spread <= low_spread
            else "expensive" if spread >= high_spread
            else "middle"
        )
        by_liquidity[liquidity].append(row)
        market = _finite(row["features"].get("market_return")) or 0.0
        regime = "market_up" if market > 0.001 else "market_down" if market < -0.001 else "market_flat"
        by_regime[regime].append(row)

    def summarize(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return [
            {
                "bucket": key,
                "events": len(rows),
                "mean_net_bps": (_mean(_outcome_value(row, horizon) for row in rows) or 0.0) * 10_000,
                "positive": (_mean(_outcome_value(row, horizon) for row in rows) or 0.0) > 0,
            }
            for key, rows in sorted(groups.items())
        ]

    return {
        "by_year": summarize(by_year),
        "by_liquidity": summarize(by_liquidity),
        "by_market_regime": summarize(by_regime),
    }


def _deciles(events: Sequence[dict[str, Any]], horizon: str) -> list[dict[str, Any]]:
    usable = [
        event for event in events
        if _finite(event["features"].get("_alpha_score")) is not None
        and _outcome_value(event, horizon) is not None
    ]
    ordered = sorted(usable, key=lambda row: float(row["features"]["_alpha_score"]))
    output: list[dict[str, Any]] = []
    for decile in range(10):
        start = len(ordered) * decile // 10
        end = len(ordered) * (decile + 1) // 10
        rows = ordered[start:end]
        output.append(
            {
                "decile": decile + 1,
                "events": len(rows),
                "mean_net_bps": (
                    _mean(_outcome_value(row, horizon) for row in rows) * 10_000
                    if rows else None
                ),
            }
        )
    return output


def _normal_p_value(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    deviation = pstdev(values)
    if deviation == 0:
        return 0.0 if fmean(values) != 0 else 1.0
    t_value = fmean(values) / (deviation / sqrt(len(values)))
    return erfc(abs(t_value) / sqrt(2))


def _bh_q_values(rows: list[dict[str, Any]]) -> None:
    indexed = sorted(
        [(index, float(row["p_value"])) for index, row in enumerate(rows) if row.get("p_value") is not None],
        key=lambda item: item[1],
    )
    running = 1.0
    for reverse_rank, (index, p_value) in enumerate(reversed(indexed), 1):
        rank = len(indexed) - reverse_rank + 1
        running = min(running, p_value * len(indexed) / rank)
        rows[index]["q_value"] = running


def _feature_diagnostics(
    events: Sequence[dict[str, Any]],
    features: Sequence[FeatureSpec],
    horizon: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for spec in features:
        usable = [
            event for event in events
            if _finite(event["features"].get(f"{spec.name}_percentile")) is not None
            and _outcome_value(event, horizon) is not None
        ]
        ordered = sorted(usable, key=lambda row: float(row["features"][f"{spec.name}_percentile"]))
        width = max(1, len(ordered) // 5)
        low = ordered[:width]
        high = ordered[-width:]
        signed_difference = spec.sign * (
            (_mean(_outcome_value(row, horizon) for row in high) or 0.0)
            - (_mean(_outcome_value(row, horizon) for row in low) or 0.0)
        )
        daily: dict[date, list[float]] = defaultdict(list)
        for row in high:
            value = _outcome_value(row, horizon)
            if value is not None:
                daily[row["session_date"]].append(spec.sign * value)
        for row in low:
            value = _outcome_value(row, horizon)
            if value is not None:
                daily[row["session_date"]].append(-spec.sign * value)
        output.append(
            {
                "feature": spec.name,
                "role": spec.role,
                "job": spec.job,
                "events": len(usable),
                "signed_top_minus_bottom_bps": signed_difference * 10_000,
                "p_value": _normal_p_value([fmean(values) for values in daily.values()]),
                "q_value": None,
            }
        )
    _bh_q_values(output)
    return output


def _veto_report(
    discovery: Sequence[dict[str, Any]],
    validation: Sequence[dict[str, Any]],
    specs: Sequence[FeatureSpec],
    horizon: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reports: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for spec in specs:
        if spec.role not in {"veto", "execution"}:
            continue
        field = f"{spec.name}_percentile"
        threshold = 0.15 if spec.sign > 0 else 0.85

        def flagged(event: dict[str, Any]) -> bool:
            value = _finite(event["features"].get(field))
            if value is None:
                return False
            return value <= threshold if spec.sign > 0 else value >= threshold

        def phase(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
            usable = [row for row in rows if _outcome_value(row, horizon) is not None]
            removed = [row for row in usable if flagged(row)]
            kept = [row for row in usable if not flagged(row)]
            base_mean = _mean(_outcome_value(row, horizon) for row in usable)
            kept_mean = _mean(_outcome_value(row, horizon) for row in kept)
            base_loss = _mean(
                1.0 if (_outcome_value(row, horizon) or 0.0) <= 0 else 0.0 for row in usable
            )
            removed_loss = _mean(
                1.0 if (_outcome_value(row, horizon) or 0.0) <= 0 else 0.0 for row in removed
            )
            def adverse(row: dict[str, Any]) -> bool:
                outcome = (row.get("outcomes") or {}).get(horizon) or {}
                mae = _finite(outcome.get("mae_bps")) or 0.0
                return mae > max(20.0, 2 * float(row.get("cost_bps") or 0.0))

            base_adverse = _mean(1.0 if adverse(row) else 0.0 for row in usable)
            removed_adverse = _mean(1.0 if adverse(row) else 0.0 for row in removed)
            return {
                "events": len(usable),
                "removed": len(removed),
                "removed_share": len(removed) / len(usable) if usable else 0.0,
                "base_mean_net_bps": base_mean * 10_000 if base_mean is not None else None,
                "kept_mean_net_bps": kept_mean * 10_000 if kept_mean is not None else None,
                "improvement_bps": (
                    (kept_mean - base_mean) * 10_000
                    if kept_mean is not None and base_mean is not None else None
                ),
                "base_loss_rate": base_loss,
                "removed_loss_rate": removed_loss,
                "base_large_mae_rate": base_adverse,
                "removed_large_mae_rate": removed_adverse,
            }

        discovery_report = phase(discovery)
        validation_report = phase(validation)
        qualifies = bool(
            discovery_report["events"] >= MIN_SCORE_EVENTS
            and 0.02 <= discovery_report["removed_share"] <= 0.30
            and (discovery_report["improvement_bps"] or 0) > 0
            and (validation_report["improvement_bps"] or 0) >= 0
            and (discovery_report["removed_loss_rate"] or 0)
                >= 1.10 * (discovery_report["base_loss_rate"] or 1)
        )
        report = {
            "feature": spec.name,
            "job": spec.job,
            "threshold_percentile": threshold,
            "veto_when": "below" if spec.sign > 0 else "above",
            "discovery": discovery_report,
            "validation": validation_report,
            "selected": qualifies,
        }
        reports.append(report)
        if qualifies:
            selected.append(
                {
                    "feature": spec.name,
                    "threshold_percentile": threshold,
                    "veto_when": report["veto_when"],
                    "job": spec.job,
                }
            )
    return reports, selected


def _is_vetoed(event: dict[str, Any], vetoes: Sequence[dict[str, Any]]) -> bool:
    for veto in vetoes:
        value = _finite(event["features"].get(f"{veto['feature']}_percentile"))
        if value is None:
            continue
        if veto["veto_when"] == "above" and value >= veto["threshold_percentile"]:
            return True
        if veto["veto_when"] == "below" and value <= veto["threshold_percentile"]:
            return True
    return False


def _probability_backtest_overfit(
    events: Sequence[dict[str, Any]],
    features: Sequence[FeatureSpec],
    horizon: str,
) -> dict[str, Any]:
    alpha = [spec for spec in features if spec.role in {"alpha", "setup", "regime"}]
    candidates = [spec.name for spec in alpha] + ["composite"]
    usable = [row for row in events if _outcome_value(row, horizon) is not None]
    sessions = sorted({row["session_date"] for row in usable})
    if len(candidates) < 2 or len(sessions) < 8:
        return {"estimated": False, "reason": "requires_two_scores_and_eight_sessions"}
    blocks = [set(sessions[len(sessions) * i // 8 : len(sessions) * (i + 1) // 8]) for i in range(8)]

    def candidate_return(rows: Sequence[dict[str, Any]], name: str) -> float:
        scored: list[tuple[float, float]] = []
        for row in rows:
            outcome = _outcome_value(row, horizon)
            if outcome is None:
                continue
            if name == "composite":
                score = _finite(row["features"].get("_alpha_score"))
            else:
                spec = next(item for item in alpha if item.name == name)
                percentile = _finite(row["features"].get(f"{name}_percentile"))
                score = spec.sign * (2 * percentile - 1) if percentile is not None else None
            if score is not None:
                scored.append((score, outcome))
        if not scored:
            return float("-inf")
        threshold = _percentile([score for score, _ in scored], 0.8)
        selected = [outcome for score, outcome in scored if threshold is not None and score >= threshold]
        return _mean(selected) if selected else float("-inf")

    below_median = 0
    trials = 0
    for train_blocks in combinations(range(8), 4):
        train_sessions = set().union(*(blocks[index] for index in train_blocks))
        train = [row for row in usable if row["session_date"] in train_sessions]
        test = [row for row in usable if row["session_date"] not in train_sessions]
        train_scores = {name: candidate_return(train, name) for name in candidates}
        winner = max(candidates, key=lambda name: train_scores[name])
        test_scores = sorted((candidate_return(test, name), name) for name in candidates)
        rank = next(index for index, (_, name) in enumerate(test_scores) if name == winner)
        percentile_rank = rank / max(1, len(test_scores) - 1)
        below_median += int(percentile_rank < 0.5)
        trials += 1
    return {
        "estimated": True,
        "cscv_splits": trials,
        "candidate_scores": candidates,
        "probability_of_backtest_overfit": below_median / trials if trials else None,
        "interpretation": "Fraction of in-sample winners ranking below the OOS median.",
    }


def _design_matrix(
    events: Sequence[dict[str, Any]],
    features: Sequence[FeatureSpec] | Sequence[dict[str, Any]],
) -> np.ndarray:
    names = [spec.name if isinstance(spec, FeatureSpec) else str(spec["name"]) for spec in features]
    matrix = np.zeros((len(events), len(names) + 1), dtype=np.float64)
    matrix[:, 0] = 1.0
    for column, name in enumerate(names, 1):
        matrix[:, column] = [
            2.0 * value - 1.0
            if (value := _finite(row["features"].get(f"{name}_percentile"))) is not None
            else 0.0
            for row in events
        ]
    return matrix


def _ridge_coefficients(matrix: np.ndarray, target: np.ndarray, penalty: float) -> list[float]:
    if not len(target):
        return []
    regularizer = np.eye(matrix.shape[1], dtype=np.float64) * penalty
    regularizer[0, 0] = 0.0
    coefficients = np.linalg.pinv(matrix.T @ matrix + regularizer) @ matrix.T @ target
    return [float(value) for value in coefficients]


def _logistic_coefficients(
    matrix: np.ndarray,
    target: np.ndarray,
    penalty: float,
    iterations: int = 30,
) -> list[float]:
    if not len(target):
        return []
    coefficients = np.zeros(matrix.shape[1], dtype=np.float64)
    base_rate = min(0.999, max(0.001, float(target.mean())))
    coefficients[0] = np.log(base_rate / (1.0 - base_rate))
    penalty_vector = np.full(matrix.shape[1], penalty, dtype=np.float64)
    penalty_vector[0] = 0.0
    for _ in range(iterations):
        linear = np.clip(matrix @ coefficients, -30.0, 30.0)
        probability = 1.0 / (1.0 + np.exp(-linear))
        weights = np.maximum(probability * (1.0 - probability), 1e-6)
        gradient = matrix.T @ (target - probability) - penalty_vector * coefficients
        hessian = matrix.T @ (matrix * weights[:, None]) + np.diag(penalty_vector)
        step = np.linalg.pinv(hessian) @ gradient
        coefficients += step
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return [float(value) for value in coefficients]


def _linear_prediction(matrix: np.ndarray, coefficients: Sequence[float]) -> np.ndarray:
    return matrix @ np.asarray(coefficients, dtype=np.float64)


def _logistic_prediction(matrix: np.ndarray, coefficients: Sequence[float]) -> np.ndarray:
    linear = np.clip(_linear_prediction(matrix, coefficients), -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-linear))


def _fit_explicit_ev_model(
    discovery: Sequence[dict[str, Any]],
    features: Sequence[FeatureSpec],
    horizon: str,
    *,
    dynamic_direction: bool,
) -> dict[str, Any] | None:
    usable = [row for row in discovery if _outcome_value(row, horizon, "gross_return") is not None]
    if len(usable) < max(30, len(features) * 3):
        return None
    matrix = _design_matrix(usable, features)
    gross_bps = np.asarray(
        [_outcome_value(row, horizon, "gross_return") * 10_000 for row in usable],
        dtype=np.float64,
    )
    won = (gross_bps > 0).astype(np.float64)
    gains = gross_bps[gross_bps > 0]
    losses = -gross_bps[gross_bps <= 0]
    win_coefficients = _logistic_coefficients(matrix, won, EV_RIDGE_PENALTY)
    gain_coefficients = _ridge_coefficients(
        matrix[gross_bps > 0], gains, EV_RIDGE_PENALTY
    ) if len(gains) else [0.0] * matrix.shape[1]
    loss_coefficients = _ridge_coefficients(
        matrix[gross_bps <= 0], losses, EV_RIDGE_PENALTY
    ) if len(losses) else [0.0] * matrix.shape[1]
    magnitude_cap = max(1.0, float(np.quantile(np.abs(gross_bps), 0.99)))
    return {
        "model_version": "explicit_conditional_ev_ridge_logit_v1",
        "horizon": horizon,
        "feature_names": [spec.name for spec in features],
        "ridge_penalty": EV_RIDGE_PENALTY,
        "training_events": len(usable),
        "dynamic_long_short_direction": dynamic_direction,
        "win_definition": "direction_adjusted_gross_return_gt_zero_before_cost",
        "win_probability": {"link": "logit", "coefficients": win_coefficients},
        "conditional_gain_bps": {"link": "ridge_identity", "coefficients": gain_coefficients},
        "conditional_loss_bps": {"link": "ridge_identity", "coefficients": loss_coefficients},
        "magnitude_cap_bps": magnitude_cap,
        "cost_rule": "per_symbol_decision_timestamp_frozen_stressed_round_trip_cost_bps",
        "objective": "P(W|X)*E[G|W,X]-P(L|X)*E[|L||L,X]-C(X)",
    }


def _apply_explicit_ev_model(
    events: Sequence[dict[str, Any]],
    features: Sequence[FeatureSpec] | Sequence[dict[str, Any]],
    model: dict[str, Any],
) -> None:
    if not events:
        return
    matrix = _design_matrix(events, features)
    probability = _logistic_prediction(matrix, model["win_probability"]["coefficients"])
    cap = float(model["magnitude_cap_bps"])
    gain = np.clip(
        _linear_prediction(matrix, model["conditional_gain_bps"]["coefficients"]),
        0.0,
        cap,
    )
    loss = np.clip(
        _linear_prediction(matrix, model["conditional_loss_bps"]["coefficients"]),
        0.0,
        cap,
    )
    dynamic = bool(model.get("dynamic_long_short_direction"))
    horizon = str(model["horizon"])
    for index, row in enumerate(events):
        cost = float(row.get("cost_bps") or 0.0)
        long_gross_ev = float(probability[index] * gain[index] - (1.0 - probability[index]) * loss[index])
        short_gross_ev = -long_gross_ev
        direction = "short" if dynamic and short_gross_ev > long_gross_ev else row["direction"]
        predicted_gross = short_gross_ev if direction == "short" else long_gross_ev
        flipped = dynamic and direction == "short"
        selected_probability = 1.0 - float(probability[index]) if flipped else float(probability[index])
        selected_gain = float(loss[index]) if flipped else float(gain[index])
        selected_loss = float(gain[index]) if flipped else float(loss[index])
        predicted_ev = predicted_gross - cost
        row["features"].update(
            {
                "_p_win": selected_probability,
                "_expected_gain_bps": selected_gain,
                "_expected_loss_bps": selected_loss,
                "_predicted_cost_bps": cost,
                "_predicted_gross_bps": predicted_gross,
                "_predicted_ev_bps": predicted_ev,
                "_predicted_direction": direction,
                "_alpha_score": predicted_ev,
            }
        )
        outcome = (row.get("outcomes") or {}).get(horizon) or {}
        raw_gross = _finite(outcome.get("gross_return_bps"))
        if raw_gross is None:
            raw_gross_return = _outcome_value(row, horizon, "gross_return")
            raw_gross = raw_gross_return * 10_000 if raw_gross_return is not None else None
        if outcome.get("available") and raw_gross is not None:
            direction_sign = -1.0 if dynamic and direction == "short" else 1.0
            outcome["model_gross_return_bps"] = direction_sign * raw_gross
            outcome["model_net_return_bps"] = direction_sign * raw_gross - cost
            outcome["model_net_return"] = outcome["model_net_return_bps"] / 10_000


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    if float(left_array.std()) == 0.0 or float(right_array.std()) == 0.0:
        return None
    return float(np.corrcoef(left_array, right_array)[0, 1])


def _ev_diagnostics(events: Sequence[dict[str, Any]], horizon: str) -> dict[str, Any]:
    usable = [
        row for row in events
        if _finite(row["features"].get("_predicted_ev_bps")) is not None
        and _outcome_value(row, horizon, "gross_return") is not None
    ]
    if not usable:
        return {"events": 0}
    predicted_ev = [float(row["features"]["_predicted_ev_bps"]) for row in usable]
    predicted_probability = [float(row["features"]["_p_win"]) for row in usable]
    actual_gross = [
        float(((row.get("outcomes") or {}).get(horizon) or {}).get("model_gross_return_bps"))
        for row in usable
    ]
    actual_win = [1.0 if value > 0 else 0.0 for value in actual_gross]
    actual_net = [
        float(((row.get("outcomes") or {}).get(horizon) or {}).get("model_net_return_bps"))
        for row in usable
    ]
    winners = [value for value in actual_gross if value > 0]
    losers = [-value for value in actual_gross if value <= 0]
    p_win = _mean(actual_win) or 0.0
    gain = _mean(winners) or 0.0
    loss = _mean(losers) or 0.0
    cost = _mean(float(row.get("cost_bps") or 0.0) for row in usable) or 0.0
    return {
        "events": len(usable),
        "brier_score": _mean((p - y) ** 2 for p, y in zip(predicted_probability, actual_win)),
        "direction_accuracy": _mean(
            1.0 if (p >= 0.5) == bool(y) else 0.0
            for p, y in zip(predicted_probability, actual_win)
        ),
        "predicted_ev_to_realized_net_correlation": _pearson(predicted_ev, actual_net),
        "mean_predicted_ev_bps": _mean(predicted_ev),
        "mean_realized_net_bps": _mean(actual_net),
        "empirical_decomposition": {
            "p_win": p_win,
            "mean_gain_bps_given_win": gain,
            "p_loss": 1.0 - p_win,
            "mean_absolute_loss_bps_given_loss": loss,
            "mean_cost_bps": cost,
            "reconstructed_ev_bps": p_win * gain - (1.0 - p_win) * loss - cost,
        },
    }


def _cross_sectional_values(
    events: Sequence[dict[str, Any]],
    horizon: str,
) -> tuple[dict[int, float], dict[int, float]]:
    by_timestamp: dict[datetime, list[tuple[int, float]]] = defaultdict(list)
    for row in events:
        gross = _outcome_value(row, horizon, "gross_return")
        if gross is not None:
            by_timestamp[row["decision_timestamp"]].append((id(row), gross * 10_000))
    residuals: dict[int, float] = {}
    ranks: dict[int, float] = {}
    for values in by_timestamp.values():
        center = _mean(value for _, value in values) or 0.0
        ordered = sorted(value for _, value in values)
        for row_id, value in values:
            residuals[row_id] = value - center
            ranks[row_id] = _rank_percentile(value, ordered) or 0.5
    return residuals, ranks


def _fit_auxiliary_target_models(
    discovery: Sequence[dict[str, Any]],
    validation: Sequence[dict[str, Any]],
    features: Sequence[FeatureSpec],
    horizon: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Measure predictability beyond the trading-return objective.

    These models never select trades.  They answer whether the frozen X set
    predicts residual return/rank, tail movement, path risk, volatility, or
    liquidity.  This is the actual information-ceiling diagnostic.
    """
    discovery_residual, discovery_rank = _cross_sectional_values(discovery, horizon)
    validation_residual, validation_rank = _cross_sectional_values(validation, horizon)

    def bounded(rows: list[dict[str, Any]], limit: int = 50_000) -> list[dict[str, Any]]:
        if len(rows) <= limit:
            return rows
        stride = max(1, len(rows) // limit)
        return rows[::stride][:limit]

    def target(row: dict[str, Any], name: str, *, residual: dict[int, float], rank: dict[int, float]) -> float | None:
        outcome = (row.get("outcomes") or {}).get(horizon) or {}
        if not outcome.get("available"):
            return None
        if name == "future_residual_return_bps":
            return residual.get(id(row))
        if name == "cross_sectional_return_rank":
            return rank.get(id(row))
        if name == "continuation_signed_return_bps":
            current = _finite(row["features"].get("bar_return_1"))
            gross = _finite(outcome.get("gross_return_bps"))
            if current is None or gross is None or current == 0:
                return None
            return (1.0 if current > 0 else -1.0) * gross
        return _finite(outcome.get(name))

    target_names = (
        "future_residual_return_bps",
        "cross_sectional_return_rank",
        "continuation_signed_return_bps",
        "mfe_bps",
        "mae_bps",
        "future_realized_volatility_bps",
        "liquidity_deterioration_bps",
    )
    models: dict[str, Any] = {}
    reports: dict[str, Any] = {}
    for name in target_names:
        train = bounded([
            row for row in discovery
            if target(row, name, residual=discovery_residual, rank=discovery_rank) is not None
        ])
        test = bounded([
            row for row in validation
            if target(row, name, residual=validation_residual, rank=validation_rank) is not None
        ])
        if len(train) < max(30, len(features) * 3):
            reports[name] = {"estimated": False, "reason": "insufficient_discovery_rows"}
            continue
        coefficients = _ridge_coefficients(
            _design_matrix(train, features),
            np.asarray(
                [target(row, name, residual=discovery_residual, rank=discovery_rank) for row in train],
                dtype=np.float64,
            ),
            EV_RIDGE_PENALTY,
        )
        models[name] = {"link": "ridge_identity", "coefficients": coefficients}
        predicted = (
            [float(value) for value in _linear_prediction(_design_matrix(test, features), coefficients)]
            if test else []
        )
        actual = [
            float(target(row, name, residual=validation_residual, rank=validation_rank))
            for row in test
        ]
        threshold = _percentile(predicted, 0.80)
        top = [value for prediction, value in zip(predicted, actual) if threshold is not None and prediction >= threshold]
        reports[name] = {
            "estimated": True,
            "discovery_events": len(train),
            "validation_events": len(test),
            "validation_prediction_correlation": _pearson(predicted, actual),
            "validation_base_mean": _mean(actual),
            "validation_top_predicted_quintile_mean": _mean(top),
        }

    gross_discovery = [
        abs(value * 10_000)
        for row in discovery
        if (value := _outcome_value(row, horizon, "gross_return")) is not None
    ]
    large_move_threshold = _percentile(gross_discovery, 0.80)
    if large_move_threshold is not None:
        train = bounded([
            row for row in discovery if _outcome_value(row, horizon, "gross_return") is not None
        ])
        test = bounded([
            row for row in validation if _outcome_value(row, horizon, "gross_return") is not None
        ])
        labels = np.asarray(
            [
                1.0 if abs(_outcome_value(row, horizon, "gross_return") * 10_000) >= large_move_threshold else 0.0
                for row in train
            ],
            dtype=np.float64,
        )
        coefficients = _logistic_coefficients(_design_matrix(train, features), labels, EV_RIDGE_PENALTY)
        models["large_move_probability"] = {
            "link": "logit",
            "threshold_bps": large_move_threshold,
            "coefficients": coefficients,
        }
        probability = (
            [float(value) for value in _logistic_prediction(_design_matrix(test, features), coefficients)]
            if test else []
        )
        actual = [
            1.0 if abs(_outcome_value(row, horizon, "gross_return") * 10_000) >= large_move_threshold else 0.0
            for row in test
        ]
        cutoff = _percentile(probability, 0.80)
        top = [value for prediction, value in zip(probability, actual) if cutoff is not None and prediction >= cutoff]
        reports["large_move_probability"] = {
            "estimated": True,
            "large_move_threshold_bps": large_move_threshold,
            "validation_events": len(test),
            "validation_brier_score": _mean((p - y) ** 2 for p, y in zip(probability, actual)),
            "validation_base_rate": _mean(actual),
            "validation_top_predicted_quintile_rate": _mean(top),
        }
    return models, reports


def _mid_tier(summary: dict[str, Any], diagnostics: dict[str, Any]) -> str:
    mean_net = float(summary.get("mean_net_bps") or 0.0)
    signals = int(summary.get("signals") or 0)
    sessions = int(summary.get("distinct_sessions") or 0)
    symbols = int(summary.get("distinct_symbols") or 0)
    clustered_t = float(summary.get("day_clustered_t_statistic") or 0.0)
    if mean_net <= 0:
        return "failed_or_negative"
    if (
        signals >= 100
        and sessions >= 40
        and symbols >= 8
        and clustered_t >= 1.0
        and float(diagnostics.get("direction_accuracy") or 0.0) >= 0.50
    ):
        return "mid_portfolio_candidate"
    return "weak_positive_watchlist"


def _concise_alpha_ceiling(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    horizons: dict[str, Any] = {}
    for horizon, row in (report.get("horizons") or {}).items():
        validation = row.get("validation_selected") or {}
        diagnostics = row.get("validation_ev_diagnostics") or {}
        predictable_targets = {}
        for name, target in (row.get("predictability_targets") or {}).items():
            correlation = target.get("validation_prediction_correlation")
            base_rate = target.get("validation_base_rate")
            top_rate = target.get("validation_top_predicted_quintile_rate")
            if correlation is not None or (base_rate is not None and top_rate is not None):
                predictable_targets[name] = {
                    "correlation": correlation,
                    "base_rate": base_rate,
                    "top_quintile_rate": top_rate,
                }
        horizons[horizon] = {
            "portfolio_tier": row.get("portfolio_tier"),
            "signals": validation.get("signals"),
            "sessions": validation.get("distinct_sessions"),
            "symbols": validation.get("distinct_symbols"),
            "mean_net_bps": validation.get("mean_net_bps"),
            "day_clustered_t": validation.get("day_clustered_t_statistic"),
            "direction_accuracy": diagnostics.get("direction_accuracy"),
            "predicted_vs_realized_net_correlation": diagnostics.get(
                "predicted_ev_to_realized_net_correlation"
            ),
            "predictability_targets": predictable_targets,
        }
    return {
        "objective": report.get("objective"),
        "discovery_selected_horizon": report.get("discovery_selected_horizon"),
        "confirmation_untouched": report.get("confirmation_untouched"),
        "horizons": horizons,
    }


def _build_models(
    events: Sequence[dict[str, Any]],
    *,
    branches: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any], int]:
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_key[event["event_key"]].append(event)
    models: dict[str, Any] = {}
    reports: dict[str, Any] = {}
    effective_trials = 0
    for event_key, rows in by_key.items():
        branch = rows[0]["branch"]
        specs = list(FEATURE_CATALOG[branch])
        discovery = [row for row in rows if row["phase"] == "discovery"]
        validation = [row for row in rows if row["phase"] == "validation"]
        eligible = [
            spec for spec in specs
            if sum(_finite(row["features"].get(spec.name)) is not None for row in discovery)
                >= max(5, int(0.60 * len(discovery)))
        ]
        normalization = _normalization_model(discovery, [spec.name for spec in eligible])
        _apply_normalization(rows, normalization)
        alpha = [
            spec for spec in eligible
            if spec.role in {"setup", "alpha", "regime", "execution"}
        ]
        pass_through = branch == BRANCH_ONE_MINUTE_VETO
        dynamic_direction = branch == BRANCH_ALPHA_CEILING
        horizon_models: dict[str, Any] = {}
        horizon_reports: dict[str, Any] = {}
        candidate_horizons = sorted(
            {
                horizon
                for row in discovery
                for horizon, outcome in (row.get("outcomes") or {}).items()
                if outcome.get("available")
            },
            key=lambda value: 10_000 if value == "eod" else int(value.removesuffix("m")),
        )
        if pass_through:
            horizon = _primary_horizon(event_key)
            threshold = 1.0
            for row in rows:
                row["features"]["_alpha_score"] = 1.0
                row["features"]["_predicted_ev_bps"] = None
        else:
            for candidate_horizon in candidate_horizons:
                ev_model = _fit_explicit_ev_model(
                    discovery,
                    alpha,
                    candidate_horizon,
                    dynamic_direction=dynamic_direction,
                )
                if ev_model is None:
                    continue
                _apply_explicit_ev_model(rows, alpha, ev_model)
                candidate_threshold = max(
                    0.0,
                    _percentile(
                        [
                            value for row in discovery
                            if (value := _finite(row["features"].get("_predicted_ev_bps"))) is not None
                        ],
                        0.80,
                    ) or 0.0,
                )
                for row in rows:
                    score = _finite(row["features"].get("_predicted_ev_bps"))
                    row["features"]["_selected"] = bool(
                        score is not None and score >= candidate_threshold
                    )
                selected_discovery_for_horizon = [
                    row for row in discovery if row["features"].get("_selected")
                ]
                selected_validation_for_horizon = [
                    row for row in validation if row["features"].get("_selected")
                ]
                discovery_summary = _phase_summary(
                    selected_discovery_for_horizon,
                    horizon=candidate_horizon,
                    effective_trials=max(1, len(alpha) * max(1, len(candidate_horizons))),
                )
                validation_summary = _phase_summary(
                    selected_validation_for_horizon,
                    horizon=candidate_horizon,
                    effective_trials=max(1, len(alpha) * max(1, len(candidate_horizons))),
                )
                validation_diagnostics = _ev_diagnostics(validation, candidate_horizon)
                auxiliary_models, auxiliary_report = _fit_auxiliary_target_models(
                    discovery,
                    validation,
                    alpha,
                    candidate_horizon,
                )
                ev_model["selection_percentile"] = 0.80
                ev_model["selection_threshold_bps"] = candidate_threshold
                ev_model["auxiliary_target_models"] = auxiliary_models
                horizon_models[candidate_horizon] = ev_model
                horizon_reports[candidate_horizon] = {
                    "discovery_selected": discovery_summary,
                    "validation_selected": validation_summary,
                    "discovery_ev_diagnostics": _ev_diagnostics(discovery, candidate_horizon),
                    "validation_ev_diagnostics": validation_diagnostics,
                    "predictability_targets": auxiliary_report,
                    "portfolio_tier": _mid_tier(validation_summary, validation_diagnostics),
                }
            if not horizon_models:
                horizon = _primary_horizon(event_key)
                threshold = None
                for row in rows:
                    row["features"]["_alpha_score"] = None
                    row["features"]["_predicted_ev_bps"] = None
            else:
                preferred = _primary_horizon(event_key)
                if branch == BRANCH_ALPHA_CEILING:
                    horizon = max(
                        horizon_models,
                        key=lambda key: (
                            float(
                                horizon_reports[key]["discovery_selected"].get("mean_net_bps")
                                or float("-inf")
                            ),
                            int(horizon_reports[key]["discovery_selected"].get("signals") or 0),
                        ),
                    )
                else:
                    horizon = preferred if preferred in horizon_models else next(iter(horizon_models))
                chosen_ev_model = horizon_models[horizon]
                threshold = float(chosen_ev_model["selection_threshold_bps"])
                _apply_explicit_ev_model(rows, alpha, chosen_ev_model)
        veto_reports, vetoes = _veto_report(discovery, validation, eligible, horizon)
        for row in rows:
            row["features"]["_vetoed"] = _is_vetoed(row, vetoes)
            score_value = _finite(row["features"].get("_alpha_score"))
            row["features"]["_selected"] = bool(
                threshold is not None
                and score_value is not None
                and score_value >= threshold
                and not row["features"]["_vetoed"]
            )
        selected_discovery = [row for row in discovery if row["features"]["_selected"]]
        selected_validation = [row for row in validation if row["features"]["_selected"]]
        trials = max(1, len(eligible) * max(1, len(horizon_models)) + len(veto_reports) + 1)
        effective_trials += trials
        feature_report = _feature_diagnostics(discovery, eligible, horizon)
        reports[event_key] = {
            "branch": branch,
            "stage": rows[0]["stage"],
            "primary_horizon": horizon,
            "event_counts": {"discovery": len(discovery), "validation": len(validation)},
            "horizon_coverage": {
                key: sum(bool((row.get("outcomes") or {}).get(key, {}).get("available")) for row in rows)
                for key in ("15m", "30m", "60m", "120m", "eod")
            },
            "unconditional": {
                "discovery": _phase_summary(discovery, horizon=horizon, effective_trials=trials),
                "validation": _phase_summary(validation, horizon=horizon, effective_trials=trials),
            },
            "feature_diagnostics": feature_report,
            "score_deciles": {
                "discovery": _deciles(discovery, horizon),
                "validation": _deciles(validation, horizon),
            },
            "veto_diagnostics": veto_reports,
            "selected": {
                "discovery": _phase_summary(selected_discovery, horizon=horizon, effective_trials=trials),
                "validation": _phase_summary(selected_validation, horizon=horizon, effective_trials=trials),
            },
            "selected_stability": {
                "discovery": _stability_report(selected_discovery, horizon=horizon),
                "validation": _stability_report(selected_validation, horizon=horizon),
            },
            "pbo": (
                {
                    "estimated": False,
                    "reason": (
                        "broad alpha-ceiling uses explicit effective-trial accounting and untouched validation; "
                        "the legacy signed-feature CSCV ranking is not meaningful for an unconstrained EV model"
                    ),
                }
                if branch == BRANCH_ALPHA_CEILING
                else _probability_backtest_overfit(discovery + validation, eligible, horizon)
            ),
            "alpha_ceiling": {
                "objective": "P(W|X)*E[G|W,X]-P(L|X)*E[|L||L,X]-C(X)",
                "horizons": horizon_reports,
                "discovery_selected_horizon": horizon,
                "confirmation_untouched": True,
            },
            "interpretation": _mid_tier(
                _phase_summary(selected_validation, horizon=horizon, effective_trials=trials),
                _ev_diagnostics(validation, horizon),
            ),
        }
        models[event_key] = {
            "event_key": event_key,
            "branch": branch,
            "stage": rows[0]["stage"],
            "primary_horizon": horizon,
            "normalization": normalization,
            "alpha_features": [spec.as_dict() for spec in alpha],
            "score_rule": (
                "base_event_pass_through_veto_only"
                if pass_through
                else "explicit_conditional_ev_ridge_logit_fitted_on_discovery_only"
            ),
            "base_event_pass_through": pass_through,
            "selection_percentile": 0.80,
            "selection_threshold": threshold,
            "conditional_ev_model": horizon_models.get(horizon),
            "horizon_models": horizon_models,
            "dynamic_long_short_direction": dynamic_direction,
            "vetoes": vetoes,
            "architecture": {
                "setup": [spec.name for spec in eligible if spec.role == "setup"],
                "alpha": [spec.name for spec in eligible if spec.role == "alpha"],
                "regime": [spec.name for spec in eligible if spec.role == "regime"],
                "veto": [item["feature"] for item in vetoes],
                "execution": [spec.name for spec in eligible if spec.role == "execution"],
            },
            "effective_trials": trials,
        }
    return models, reports, max(1, effective_trials)


def _apply_frozen_models(events: Sequence[dict[str, Any]], models: dict[str, Any]) -> None:
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_key[event["event_key"]].append(event)
    for event_key, rows in by_key.items():
        model = models.get(event_key)
        if not model:
            continue
        _apply_normalization(rows, model["normalization"])
        conditional_ev_model = model.get("conditional_ev_model")
        if conditional_ev_model:
            _apply_explicit_ev_model(rows, model["alpha_features"], conditional_ev_model)
        for row in rows:
            score = (
                1.0
                if model.get("base_event_pass_through")
                else _finite(row["features"].get("_predicted_ev_bps"))
            )
            row["features"]["_alpha_score"] = score
            row["features"]["_vetoed"] = _is_vetoed(row, model["vetoes"])
            row["features"]["_selected"] = bool(
                score is not None
                and model["selection_threshold"] is not None
                and score >= model["selection_threshold"]
                and not row["features"]["_vetoed"]
            )


def _validate_branches(timeframe: str, branches: Sequence[str]) -> list[str]:
    unique = list(dict.fromkeys(branches))
    unknown = sorted(set(unique) - set(BRANCHES))
    if unknown:
        raise ValueError(f"Unknown event branches: {unknown}")
    allowed = (
        {BRANCH_GAP, BRANCH_ONE_MINUTE_VETO, BRANCH_ALPHA_CEILING}
        if timeframe == "30m"
        else {BRANCH_FAILED_AUCTION, BRANCH_ALPHA_CEILING}
    )
    invalid = sorted(set(unique) - allowed)
    if invalid:
        raise ValueError(f"{timeframe} does not support branches {invalid}; allowed={sorted(allowed)}")
    return unique


def declare_event_study(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    branches: Sequence[str],
    symbols: Sequence[str] | None,
    cost_calibration_id: int | None,
    feed: str,
    purpose: str,
    include_news_features: bool = False,
    include_options_features: bool = False,
    options_feed: str = "opra",
) -> dict[str, Any]:
    branches = _validate_branches(timeframe, branches)
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no immutable nested split boundaries.")
    manifest = conn.execute(
        "SELECT assets, integrity, dataset_kind, window_start, window_end "
        "FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest or manifest["dataset_kind"] != "intraday":
        raise ValueError(f"Dataset {dataset_id} is not an intraday snapshot.")
    available = [str(item).upper() for item in (manifest["assets"] or [])]
    selected = list(dict.fromkeys(str(item).upper() for item in (symbols or available)))
    missing = sorted(set(selected) - set(available))
    if missing:
        raise ValueError(f"Dataset {dataset_id} does not contain symbols: {missing}")
    integrity = manifest.get("integrity") or {}
    source = str(integrity.get("pinned_source") or ("alpaca_sip" if feed == "sip" else "alpaca_iex"))
    cost_model = load_cost_model(conn, cost_calibration_id)
    if cost_calibration_id is None:
        raise ValueError("Event-conditioned discovery requires an explicit execution-cost calibration id.")
    catalog = {
        branch: [spec.as_dict() for spec in FEATURE_CATALOG[branch]] for branch in branches
    }
    specification = {
        "purpose": purpose,
        "research_object": "conditional_expectancy_not_strategy",
        "event_definitions": {
            BRANCH_GAP: "30m opening gap >=30bps, elevated second-bar relative volume, and >=50% gap rejection",
            BRANCH_FAILED_AUCTION: "15m excursion beyond prior four-bar range followed by optional re-entry within two bars",
            BRANCH_ONE_MINUTE_VETO: "1m state inside the completed 30m signal bar; veto only, never standalone direction",
            BRANCH_ALPHA_CEILING: (
                "All-symbol hourly decision grid using only completed-bar predictors; "
                "the development-fitted EV model compares long and short expectancy"
            ),
        },
        "horizons_minutes": list(HORIZONS_MINUTES),
        "outcomes": [
            "gross_return",
            "net_return_after_stressed_cost",
            "MFE",
            "MAE",
            "future_realized_volatility",
            "future_liquidity_deterioration",
            "EOD_return",
        ],
        "split_policy": "50pct_discovery_30pct_validation_20pct_one_shot_confirmation",
        "score_policy": (
            "explicit conditional EV: P(win|X)*E(gain|win,X)-"
            "P(loss|X)*E(abs(loss)|loss,X)-conditional_cost; top development quintile"
        ),
        "alpha_ceiling_targets": [
            "future_gross_and_net_return",
            "future_direction_probability",
            "conditional_gain_and_loss_magnitude",
            "future_MFE_and_MAE",
            "future_realized_volatility",
            "future_liquidity_deterioration",
        ],
        "news_side_channel": {
            "enabled": bool(include_news_features),
            "provider": "alpaca_news",
            "known_at_policy": "use article updated_at as the earliest usable version timestamp",
            "research_question": "price_flow_baseline_vs_price_flow_plus_point_in_time_news",
            "features": list(NEWS_FEATURE_NAMES) if include_news_features else [],
        },
        "options_side_channel": {
            "enabled": bool(include_options_features),
            "provider": "alpaca_options",
            "feed": options_feed,
            "known_at_policy": "use option-chain observed_at as the earliest usable surface timestamp",
            "research_question": "price_flow_news_baseline_vs_price_flow_news_plus_point_in_time_options",
            "limitation": (
                "Option-chain snapshots are point-in-time from collection time; "
                "they are not historical option-surface reconstruction for old decisions."
            ),
            "features": list(OPTION_FEATURE_NAMES) if include_options_features else [],
        },
        "mid_tier_policy": (
            "validation net positive, >=100 signals, >=40 sessions, >=8 symbols, "
            "day-clustered t>=1, direction accuracy>=50%; elite gates unchanged"
        ),
        "veto_policy": "development_mining_validation_check_max_30pct_removed",
        "multiple_testing": ["Benjamini-Hochberg", "Deflated Sharpe", "CSCV PBO diagnostic"],
        "broker_authorization": False,
        "source": source,
        "feed": feed,
    }
    if BRANCH_ONE_MINUTE_VETO in branches:
        specification["one_minute_predictor_fingerprint"] = _one_minute_predictor_fingerprint(
            conn,
            symbols=selected,
            start=manifest["window_start"],
            end=manifest["window_end"],
            source=source,
            feed=feed,
        )
    hash_payload = {
        "dataset_id": dataset_id,
        "timeframe": timeframe,
        "branches": branches,
        "symbols": selected,
        "features": catalog,
        "splits": splits.as_dict(),
        "cost_model": cost_model,
        "specification": specification,
        "include_news_features": bool(include_news_features),
        "include_options_features": bool(include_options_features),
        "options_feed": options_feed,
        "protocol_version": EVENT_DISCOVERY_VERSION,
    }
    spec_hash = sha256(dumps(hash_payload, sort_keys=True, default=str).encode()).hexdigest()
    existing = conn.execute(
        "SELECT id, created_at FROM intraday_event_study_declarations WHERE specification_hash = %s",
        (spec_hash,),
    ).fetchone()
    if existing:
        return {
            "declaration_id": int(existing["id"]),
            "already_declared": True,
            "specification_hash": spec_hash,
            "next_allowed_phase": "development_discovery",
            "news_features": bool(include_news_features),
            "options_features": bool(include_options_features),
            "options_feed": options_feed if include_options_features else None,
        }
    row = conn.execute(
        """
        INSERT INTO intraday_event_study_declarations(
            dataset_id, timeframe, branches, symbols, horizons_minutes,
            feature_catalog, split_boundaries, cost_model, specification,
            specification_hash, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            dataset_id,
            timeframe,
            Jsonb(branches),
            Jsonb(selected),
            Jsonb(list(HORIZONS_MINUTES)),
            Jsonb(catalog),
            Jsonb(splits.as_dict()),
            Jsonb(_jsonable(cost_model)),
            Jsonb(specification),
            spec_hash,
            EVENT_DISCOVERY_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {
        "declaration_id": int(row["id"]),
        "already_declared": False,
        "dataset_id": dataset_id,
        "timeframe": timeframe,
        "branches": branches,
        "symbols": len(selected),
        "horizons_minutes": list(HORIZONS_MINUTES),
        "additional_horizons": ["eod"],
        "specification_hash": spec_hash,
        "next_allowed_phase": "development_discovery",
        "created_at": row["created_at"],
        "news_features": bool(include_news_features),
        "options_features": bool(include_options_features),
        "options_feed": options_feed if include_options_features else None,
    }


def _load_declaration(conn: psycopg.Connection, declaration_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM intraday_event_study_declarations WHERE id = %s",
        (declaration_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No event-study declaration id={declaration_id}.")
    return dict(row)


def _splits_from_json(value: dict[str, Any]) -> NestedSplits:
    return NestedSplits(
        discovery_start=datetime.fromisoformat(value["discovery_start"]),
        discovery_end=datetime.fromisoformat(value["discovery_end"]),
        validation_start=datetime.fromisoformat(value["validation_start"]),
        validation_end=datetime.fromisoformat(value["validation_end"]),
        confirmation_start=datetime.fromisoformat(value["confirmation_start"]),
        confirmation_end=datetime.fromisoformat(value["confirmation_end"]),
        split_version=value["split_version"],
    )


def run_event_discovery(
    conn: psycopg.Connection,
    *,
    declaration_id: int,
    max_events: int | None = None,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT id, results, event_count, created_at FROM intraday_event_study_runs WHERE declaration_id = %s",
        (declaration_id,),
    ).fetchone()
    if existing:
        return {
            "run_id": int(existing["id"]),
            "already_completed": True,
            "event_count": int(existing["event_count"]),
            "summary": existing["results"],
        }
    declaration = _load_declaration(conn, declaration_id)
    specification = declaration["specification"]
    splits = _splits_from_json(declaration["split_boundaries"])
    _assert_one_minute_fingerprint(conn, declaration=declaration)
    print(
        f"event discovery: loading dataset={declaration['dataset_id']} timeframe={declaration['timeframe']} "
        f"branches={','.join(declaration['branches'])}",
        flush=True,
    )
    events = detect_events(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        timeframe=str(declaration["timeframe"]),
        branches=list(declaration["branches"]),
        symbols=list(declaration["symbols"]),
        cost_model=dict(declaration["cost_model"]),
        horizons=list(declaration["horizons_minutes"]),
        source=str(specification["source"]),
        feed=str(specification["feed"]),
        include_news_features=bool((specification.get("news_side_channel") or {}).get("enabled")),
        include_options_features=bool((specification.get("options_side_channel") or {}).get("enabled")),
        options_feed=str((specification.get("options_side_channel") or {}).get("feed") or "opra"),
        decision_end=splits.confirmation_start,
    )
    development = []
    for event in events:
        phase = splits.phase_for(event["decision_timestamp"])
        if phase in {"discovery", "validation"}:
            event["phase"] = phase
            development.append(event)
    if max_events:
        development = development[:max_events]
    print(f"event discovery: {len(development)} development events detected; scoring", flush=True)
    models, reports, effective_trials = _build_models(
        development,
        branches=list(declaration["branches"]),
    )
    results = {
        "research_object": "explicit_conditional_ev_and_alpha_ceiling_not_strategy",
        "dataset_id": int(declaration["dataset_id"]),
        "declaration_id": declaration_id,
        "timeframe": declaration["timeframe"],
        "branches": declaration["branches"],
        "news_features": bool((specification.get("news_side_channel") or {}).get("enabled")),
        "options_features": bool((specification.get("options_side_channel") or {}).get("enabled")),
        "options_feed": str((specification.get("options_side_channel") or {}).get("feed") or "opra")
        if bool((specification.get("options_side_channel") or {}).get("enabled"))
        else None,
        "splits_accessed": ["discovery", "validation"],
        "confirmation_accessed": False,
        "events": len(development),
        "effective_trials": effective_trials,
        "event_studies": reports,
        "mid_candidate_count": sum(
            1
            for report in reports.values()
            for horizon in (report.get("alpha_ceiling") or {}).get("horizons", {}).values()
            if horizon.get("portfolio_tier") == "mid_portfolio_candidate"
        ),
        "promotion_authorized": False,
        "next_allowed_phase": "freeze_complete_then_one_shot_confirmation",
        "protocol_version": EVENT_DISCOVERY_VERSION,
    }
    row = conn.execute(
        """
        INSERT INTO intraday_event_study_runs(
            declaration_id, dataset_id, results, frozen_model,
            effective_trials, event_count, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            declaration_id,
            declaration["dataset_id"],
            Jsonb(_jsonable(results)),
            Jsonb(_jsonable(models)),
            effective_trials,
            len(development),
            EVENT_DISCOVERY_VERSION,
        ),
    ).fetchone()
    run_id = int(row["id"])
    persisted_development = [
        event
        for event in development
        if event["branch"] != BRANCH_ALPHA_CEILING or event["features"].get("_selected")
    ]
    for batch_start in range(0, len(persisted_development), 500):
        batch = persisted_development[batch_start : batch_start + 500]
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO intraday_event_study_events(
                    run_id, event_key, branch, stage, symbol, session_date,
                    decision_timestamp, direction, phase, features, outcomes,
                    labels, cost_bps
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                [
                    (
                        run_id,
                        event["event_key"],
                        event["branch"],
                        event["stage"],
                        event["symbol"],
                        event["session_date"],
                        event["decision_timestamp"],
                        event["direction"],
                        event["phase"],
                        Jsonb(_jsonable(event["features"])),
                        Jsonb(_jsonable(event["outcomes"])),
                        Jsonb(_jsonable(event["labels"])),
                        event["cost_bps"],
                    )
                    for event in batch
                ],
            )
    for phase in ("discovery", "validation"):
        record_split_access(
            conn,
            dataset_id=int(declaration["dataset_id"]),
            phase=phase,
            decision_type="event_conditioned_model_development",
            candidate_id=f"event-study:{run_id}",
            detail={"declaration_id": declaration_id, "branches": declaration["branches"]},
        )
    conn.commit()
    return {
        "run_id": run_id,
        "declaration_id": declaration_id,
        "events": len(development),
        "effective_trials": effective_trials,
        "event_studies": {
            key: {
                "events": report["event_counts"],
                "primary_horizon": report["primary_horizon"],
                "interpretation": report["interpretation"],
                "selected_validation": report["selected"]["validation"],
                "alpha_ceiling": _concise_alpha_ceiling(report.get("alpha_ceiling")),
                "selected_vetoes": [row["feature"] for row in report["veto_diagnostics"] if row["selected"]],
            }
            for key, report in reports.items()
        },
        "confirmation_accessed": False,
        "raw_event_persistence": (
            "all_named_events; alpha_ceiling_selected_events_only_to_bound_database_growth"
        ),
        "next_command": f"confirm --run-id {run_id}",
        "created_at": row["created_at"],
    }


def _confirmation_gates(summary: dict[str, Any]) -> dict[str, bool]:
    bootstrap = summary.get("block_bootstrap") or {}
    lower = (bootstrap.get("confidence_interval_95") or [None])[0]
    deflated = summary.get("deflated_sharpe") or {}
    return {
        "minimum_50_events": int(summary.get("signals") or 0) >= MIN_CONFIRMATION_EVENTS,
        "positive_net_expectancy": (summary.get("mean_net_bps") or 0) > 0,
        "day_clustered_t_at_least_3": (summary.get("day_clustered_t_statistic") or 0) >= 3.0,
        "positive_block_bootstrap_lower_bound": lower is not None and float(lower) > 0,
        "deflated_sharpe_confidence_95pct": (
            deflated.get("deflated_sharpe") is not None
            and float(deflated["deflated_sharpe"]) >= 0.95
        ),
        "independent_evidence_ready": bool(summary.get("independent_evidence_ready")),
    }


def run_event_confirmation(
    conn: psycopg.Connection,
    *,
    run_id: int,
) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM intraday_event_study_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not run:
        raise ValueError(f"No event-study run id={run_id}.")
    existing = conn.execute(
        "SELECT * FROM intraday_event_confirmation_runs WHERE discovery_run_id = %s",
        (run_id,),
    ).fetchone()
    if existing:
        raise ValueError(
            f"Confirmation for event-study run {run_id} is already spent as confirmation id={existing['id']}."
        )
    declaration = _load_declaration(conn, int(run["declaration_id"]))
    specification = declaration["specification"]
    splits = _splits_from_json(declaration["split_boundaries"])
    _assert_one_minute_fingerprint(conn, declaration=declaration)
    print(f"event confirmation: one-shot read of run={run_id} final 20pct", flush=True)
    events = detect_events(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        timeframe=str(declaration["timeframe"]),
        branches=list(declaration["branches"]),
        symbols=list(declaration["symbols"]),
        cost_model=dict(declaration["cost_model"]),
        horizons=list(declaration["horizons_minutes"]),
        source=str(specification["source"]),
        feed=str(specification["feed"]),
        include_news_features=bool((specification.get("news_side_channel") or {}).get("enabled")),
        include_options_features=bool((specification.get("options_side_channel") or {}).get("enabled")),
        options_feed=str((specification.get("options_side_channel") or {}).get("feed") or "opra"),
        decision_start=splits.confirmation_start,
        decision_end=splits.confirmation_end + timedelta(days=1),
    )
    confirmation = [
        event for event in events
        if splits.phase_for(event["decision_timestamp"]) == "confirmation"
    ]
    models = dict(run["frozen_model"])
    _apply_frozen_models(confirmation, models)
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in confirmation:
        by_key[event["event_key"]].append(event)
    results: dict[str, Any] = {}
    all_passed = bool(models)
    for event_key, model in models.items():
        rows = by_key.get(event_key, [])
        selected = [row for row in rows if row["features"].get("_selected")]
        summary = _phase_summary(
            selected,
            horizon=model["primary_horizon"],
            effective_trials=int(run["effective_trials"]),
        )
        gates = _confirmation_gates(summary)
        passed = bool(gates) and all(gates.values())
        all_passed = all_passed and passed
        results[event_key] = {
            "base_events": len(rows),
            "selected_events": len(selected),
            "primary_horizon": model["primary_horizon"],
            "summary": summary,
            "gates": gates,
            "passed": passed,
        }
    model_hash = sha256(
        dumps(
            {
                "run_id": run_id,
                "declaration_hash": declaration["specification_hash"],
                "model": models,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    payload = {
        "run_id": run_id,
        "declaration_id": int(run["declaration_id"]),
        "dataset_id": int(run["dataset_id"]),
        "phase": "confirmation",
        "one_shot": True,
        "news_features": bool((specification.get("news_side_channel") or {}).get("enabled")),
        "options_features": bool((specification.get("options_side_channel") or {}).get("enabled")),
        "options_feed": str((specification.get("options_side_channel") or {}).get("feed") or "opra")
        if bool((specification.get("options_side_channel") or {}).get("enabled"))
        else None,
        "event_studies": results,
        "passed_locked_confirmation": all_passed,
        "strategy_created": False,
        "broker_authorized": False,
        "next_phase": "strategy_engineering" if all_passed else "stop_or_new_predeclared_hypothesis",
        "protocol_version": EVENT_DISCOVERY_VERSION,
    }
    stored = conn.execute(
        """
        INSERT INTO intraday_event_confirmation_runs(
            discovery_run_id, declaration_id, model_hash, results,
            passed, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            run_id,
            run["declaration_id"],
            model_hash,
            Jsonb(_jsonable(payload)),
            all_passed,
            EVENT_DISCOVERY_VERSION,
        ),
    ).fetchone()
    record_split_access(
        conn,
        dataset_id=int(run["dataset_id"]),
        phase="confirmation",
        decision_type="event_conditioned_one_shot_confirmation",
        candidate_id=f"event-study:{run_id}",
        detail={"model_hash": model_hash},
    )
    conn.commit()
    return {"confirmation_id": int(stored["id"]), **payload, "created_at": stored["created_at"]}


def event_study_report(
    conn: psycopg.Connection,
    *,
    run_id: int,
) -> dict[str, Any]:
    run = conn.execute(
        "SELECT * FROM intraday_event_study_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not run:
        raise ValueError(f"No event-study run id={run_id}.")
    confirmation = conn.execute(
        "SELECT id, passed, results, created_at FROM intraday_event_confirmation_runs WHERE discovery_run_id = %s",
        (run_id,),
    ).fetchone()
    studies = (run["results"] or {}).get("event_studies") or {}
    return {
        "run_id": run_id,
        "declaration_id": int(run["declaration_id"]),
        "dataset_id": int(run["dataset_id"]),
        "event_count": int(run["event_count"]),
        "effective_trials": int(run["effective_trials"]),
        "studies": {
            key: {
                "branch": value.get("branch"),
                "stage": value.get("stage"),
                "primary_horizon": value.get("primary_horizon"),
                "events": value.get("event_counts"),
                "validation_interpretation": value.get("interpretation"),
                "validation_selected": (value.get("selected") or {}).get("validation"),
                "alpha_ceiling": _concise_alpha_ceiling(value.get("alpha_ceiling")),
                "selected_vetoes": [
                    item.get("feature") for item in value.get("veto_diagnostics", []) if item.get("selected")
                ],
                "pbo": value.get("pbo"),
            }
            for key, value in studies.items()
        },
        "confirmation": (
            {
                "id": int(confirmation["id"]),
                "passed": bool(confirmation["passed"]),
                "results": confirmation["results"],
                "created_at": confirmation["created_at"],
            }
            if confirmation else {"status": "untouched"}
        ),
        "broker_authorized": False,
    }


def feature_catalog() -> dict[str, Any]:
    return {
        "protocol_version": EVENT_DISCOVERY_VERSION,
        "branches": {
            branch: [spec.as_dict() for spec in specs]
            for branch, specs in FEATURE_CATALOG.items()
        },
        "horizons_minutes": list(HORIZONS_MINUTES),
        "ev_objective": "P(W|X)*E[G|W,X]-P(L|X)*E[|L||L,X]-C(X)",
        "mid_tier_is_not_elite": True,
        "workflow": ["declare", "discover", "report", "confirm_once"],
        "broker_authorized": False,
    }
