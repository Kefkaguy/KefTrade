"""Institutional evidence controls for intraday research.

The signal horizon may be 15m/30m, but observations from many symbols on the
same trading day are not independent experiments.  This module turns raw
signal outcomes into session-clustered evidence, reports concentration and
power, and applies selection-aware confidence through the existing Deflated
Sharpe implementation.

It contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from hashlib import sha256
from math import sqrt
from random import Random
from statistics import fmean, pstdev
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from app.services.null_models import deflated_sharpe_ratio

INTRADAY_RESEARCH_INTEGRITY_VERSION = "intraday_research_integrity_v1"
NEW_YORK = ZoneInfo("America/New_York")

MINIMUM_SIGNALS = 50
MINIMUM_DISTINCT_SESSIONS = 20
MINIMUM_DAY_CLUSTERED_T = 3.0
MINIMUM_DEFLATED_SHARPE_CONFIDENCE = 0.95
MAX_SINGLE_SESSION_SHARE = 0.20
MAX_SINGLE_SYMBOL_SHARE = 0.50
DEFAULT_BOOTSTRAP_SAMPLES = 1_000


def exchange_session_date(value: Any) -> date:
    """Return the US-equity exchange date for a row, timestamp, or date."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("intraday timestamps must be timezone-aware")
        return value.astimezone(NEW_YORK).date()
    if isinstance(value, dict):
        feature = value.get("feature") or {}
        session = value.get("session_date") or feature.get("session_date")
        if isinstance(session, datetime):
            return session.date()
        if isinstance(session, date):
            return session
        candle = value.get("candle") or value
        return exchange_session_date(candle["timestamp"])
    raise TypeError(f"cannot derive an exchange session date from {type(value).__name__}")


def rows_after_session(
    rows: Iterable[dict[str, Any]],
    *,
    session_date_exclusive: date,
) -> list[dict[str, Any]]:
    """Exclude the entire last observed session, not just earlier timestamps."""
    return [row for row in rows if exchange_session_date(row) > session_date_exclusive]


def rows_with_session_context(
    rows: Sequence[dict[str, Any]],
    *,
    session_date_exclusive: date,
    context_bars: int,
) -> list[dict[str, Any]]:
    """Retain bounded warmup plus every row from untouched later sessions."""
    first_forward = next(
        (
            index
            for index, row in enumerate(rows)
            if exchange_session_date(row) > session_date_exclusive
        ),
        len(rows),
    )
    start = max(0, first_forward - max(0, context_bars))
    return list(rows[start:])


def clustered_outcome_statistics(
    outcomes: Sequence[dict[str, Any]],
    *,
    effective_trials: int,
    require_symbol_diversification: bool = False,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
) -> dict[str, Any]:
    """Summarize direction-adjusted returns using trading days as clusters.

    Each outcome requires ``value`` and may carry ``session_date``,
    ``timestamp`` and ``symbol``.  Signals inside one session are averaged
    before inference so a market-wide shock cannot masquerade as dozens of
    independent observations.
    """
    usable: list[dict[str, Any]] = []
    for row in outcomes:
        value = row.get("value")
        if value is None:
            continue
        session = row.get("session_date")
        if session is None:
            timestamp = row.get("timestamp")
            if timestamp is None:
                continue
            session = exchange_session_date(timestamp)
        usable.append(
            {
                **row,
                "value": float(value),
                "session_date": exchange_session_date(session),
                "symbol": str(row.get("symbol") or "UNKNOWN").upper(),
            }
        )

    by_session: dict[date, list[float]] = defaultdict(list)
    session_counts: Counter[date] = Counter()
    symbol_counts: Counter[str] = Counter()
    for row in usable:
        by_session[row["session_date"]].append(row["value"])
        session_counts[row["session_date"]] += 1
        symbol_counts[row["symbol"]] += 1

    daily = [fmean(by_session[key]) for key in sorted(by_session)]
    daily_mean = fmean(daily) if daily else None
    daily_deviation = pstdev(daily) if len(daily) > 1 else 0.0
    clustered_t = (
        daily_mean / (daily_deviation / sqrt(len(daily)))
        if daily_mean is not None and daily_deviation > 0
        else (
            999.0
            if daily_mean is not None and daily_mean > 0 and len(daily) > 1
            else -999.0
            if daily_mean is not None and daily_mean < 0 and len(daily) > 1
            else 0.0
            if daily_mean == 0 and len(daily) > 1
            else None
        )
    )
    bootstrap = _moving_block_bootstrap(
        daily,
        samples=bootstrap_samples,
        seed=_stable_seed(usable, effective_trials),
    )
    deflated = deflated_sharpe_ratio(daily, trials=max(1, int(effective_trials)))
    if len(daily) >= 4 and daily_deviation == 0 and daily_mean is not None:
        deflated = {
            "deflated_sharpe": 1.0 if daily_mean > 0 else 0.0,
            "observed_sharpe": None,
            "trials": max(1, int(effective_trials)),
            "sample_size": len(daily),
            "reason": "deterministic session means",
        }

    count = len(usable)
    max_session_share = max(session_counts.values(), default=0) / max(1, count)
    max_symbol_share = max(symbol_counts.values(), default=0) / max(1, count)
    session_hhi = sum((value / max(1, count)) ** 2 for value in session_counts.values())
    symbol_hhi = sum((value / max(1, count)) ** 2 for value in symbol_counts.values())
    minimum_detectable = (
        (1.96 + 0.84) * daily_deviation / sqrt(len(daily)) * 10_000
        if len(daily) > 1 and daily_deviation > 0
        else None
    )
    concentration_ok = max_session_share <= MAX_SINGLE_SESSION_SHARE
    if require_symbol_diversification:
        concentration_ok = concentration_ok and max_symbol_share <= MAX_SINGLE_SYMBOL_SHARE

    gates = {
        "minimum_signals": count >= MINIMUM_SIGNALS,
        "minimum_distinct_sessions": len(daily) >= MINIMUM_DISTINCT_SESSIONS,
        "session_concentration": max_session_share <= MAX_SINGLE_SESSION_SHARE,
        "symbol_concentration": (
            max_symbol_share <= MAX_SINGLE_SYMBOL_SHARE
            if require_symbol_diversification
            else True
        ),
        "positive_block_bootstrap_lower_bound": (
            bootstrap["confidence_interval_95"][0] > 0
            if bootstrap["confidence_interval_95"] is not None
            else False
        ),
        "minimum_day_clustered_t": (
            clustered_t is not None and clustered_t >= MINIMUM_DAY_CLUSTERED_T
        ),
        "minimum_deflated_sharpe_confidence": (
            deflated.get("deflated_sharpe") is not None
            and float(deflated["deflated_sharpe"]) >= MINIMUM_DEFLATED_SHARPE_CONFIDENCE
        ),
    }
    return {
        "integrity_version": INTRADAY_RESEARCH_INTEGRITY_VERSION,
        "signals": count,
        "distinct_sessions": len(daily),
        "distinct_symbols": len(symbol_counts),
        "session_mean_returns": daily,
        "mean_return_bps": _round(daily_mean * 10_000 if daily_mean is not None else None),
        "day_clustered_t_statistic": _round(clustered_t),
        "block_bootstrap": bootstrap,
        "deflated_sharpe": deflated,
        "effective_trials": max(1, int(effective_trials)),
        "minimum_detectable_effect_bps_80pct_power": _round(minimum_detectable),
        "annualized_signal_frequency": (
            _round(count / len(daily) * 252) if daily else None
        ),
        "concentration": {
            "max_session_share": _round(max_session_share),
            "max_symbol_share": _round(max_symbol_share),
            "session_hhi": _round(session_hhi),
            "symbol_hhi": _round(symbol_hhi),
            "largest_session": str(session_counts.most_common(1)[0][0])
            if session_counts
            else None,
            "largest_symbol": symbol_counts.most_common(1)[0][0] if symbol_counts else None,
            "passes": concentration_ok,
        },
        "quality_gates": gates,
        "independent_evidence_ready": all(
            gates[key]
            for key in (
                "minimum_signals",
                "minimum_distinct_sessions",
                "session_concentration",
                "symbol_concentration",
            )
        ),
        "selection_adjusted_signal": all(gates.values()),
    }


def dataset_research_readiness(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    microstructure_by_symbol: dict[str, dict[datetime, dict[str, Any]]] | None = None,
    minimum_symbols: int = 20,
    minimum_sessions: int = 252,
) -> dict[str, Any]:
    """Measure whether a frozen snapshot can support institutional claims."""
    all_sessions = {
        exchange_session_date(row)
        for rows in candles_by_symbol.values()
        for row in rows
    }
    symbols = {symbol for symbol, rows in candles_by_symbol.items() if rows}
    candle_rows = sum(len(rows) for rows in candles_by_symbol.values())
    expected_quote_bars = candle_rows
    quote_bars = sum(len(rows) for rows in (microstructure_by_symbol or {}).values())
    quote_coverage = quote_bars / expected_quote_bars if expected_quote_bars else 0.0
    gates = {
        "minimum_symbols": len(symbols) >= minimum_symbols,
        "minimum_sessions": len(all_sessions) >= minimum_sessions,
        "candle_data_present": candle_rows > 0,
        "quote_coverage_for_execution": quote_coverage >= 0.80,
    }
    return {
        "symbols": len(symbols),
        "distinct_sessions": len(all_sessions),
        "candle_rows": candle_rows,
        "microstructure_bars": quote_bars,
        "microstructure_coverage": _round(quote_coverage),
        "gates": gates,
        "candle_research_ready": all(
            gates[key]
            for key in ("minimum_symbols", "minimum_sessions", "candle_data_present")
        ),
        "execution_research_ready": all(gates.values()),
        "limitations": [
            label
            for label, passed in gates.items()
            if not passed
        ],
    }


def cost_model_readiness(
    cost_model: dict[str, Any],
    *,
    symbols: Sequence[str],
    minimum_quote_observations: int = 10_000,
    minimum_matched_fills: int = 30,
) -> dict[str, Any]:
    """Separate conservative research costs from production-ready TCA."""
    normalized = {str(symbol).upper() for symbol in symbols}
    calibrated_symbols = {
        str(symbol).upper() for symbol in (cost_model.get("by_symbol") or {})
    }
    coverage = len(normalized & calibrated_symbols) / max(1, len(normalized))
    quote_observations = int(cost_model.get("quote_observations") or 0)
    matched_fills = int(cost_model.get("matched_fill_observations") or 0)
    feed = str(cost_model.get("feed") or "unknown").lower()
    gates = {
        "observed_cost_available": cost_model.get("observed_round_trip_bps") is not None,
        "stressed_cost_available": cost_model.get("stressed_round_trip_bps") is not None,
        "minimum_quote_observations": quote_observations >= minimum_quote_observations,
        "minimum_symbol_coverage": coverage >= 0.80,
        "minimum_matched_fills": matched_fills >= minimum_matched_fills,
        "consolidated_or_full_feed": feed not in {"iex", "unknown"},
    }
    return {
        "quote_observations": quote_observations,
        "matched_fill_observations": matched_fills,
        "symbol_coverage": _round(coverage),
        "feed": feed,
        "gates": gates,
        "research_cost_available": (
            gates["stressed_cost_available"]
            or cost_model.get("conservative_round_trip_bps") is not None
        ),
        "production_cost_ready": all(gates.values()),
        "limitations": [label for label, passed in gates.items() if not passed],
    }


def estimated_round_trip_cost_bps(
    cost_model: dict[str, Any],
    *,
    symbol: str,
    timestamp: datetime,
    stressed: bool = True,
) -> float:
    """Use the most conservative available symbol/slot/global cost estimate."""
    symbol_row = (cost_model.get("by_symbol") or {}).get(symbol.upper()) or {}
    slot = timestamp.astimezone(ZoneInfo("UTC")).strftime("%H:%M")
    slot_row = (cost_model.get("by_time_slot") or {}).get(slot) or {}
    candidates = [
        symbol_row.get("p90_bar_spread_bps" if stressed else "median_bar_spread_bps"),
        slot_row.get("p90_bar_spread_bps" if stressed else "median_bar_spread_bps"),
        cost_model.get("stressed_round_trip_bps" if stressed else "observed_round_trip_bps"),
    ]
    usable = [float(value) for value in candidates if value is not None]
    if usable:
        return max(usable)
    fallback = cost_model.get("conservative_round_trip_bps")
    return float(fallback) if fallback is not None else 30.0


def _moving_block_bootstrap(
    daily: Sequence[float],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    if len(daily) < 4 or samples <= 0:
        return {
            "samples": 0,
            "block_length_sessions": None,
            "confidence_interval_95": None,
            "two_sided_p_value": None,
            "reason": "at least four distinct sessions are required",
        }
    block_length = max(1, min(5, round(len(daily) ** (1 / 3))))
    blocks = [
        [daily[(start + offset) % len(daily)] for offset in range(block_length)]
        for start in range(len(daily))
    ]
    rng = Random(seed)
    means: list[float] = []
    for _ in range(samples):
        sample: list[float] = []
        while len(sample) < len(daily):
            sample.extend(blocks[rng.randrange(len(blocks))])
        means.append(fmean(sample[: len(daily)]))
    ordered = sorted(means)
    lower = _percentile(ordered, 0.025)
    upper = _percentile(ordered, 0.975)
    non_positive = sum(value <= 0 for value in means)
    non_negative = sum(value >= 0 for value in means)
    p_value = min(1.0, 2 * min(non_positive + 1, non_negative + 1) / (samples + 1))
    return {
        "samples": samples,
        "block_length_sessions": block_length,
        "confidence_interval_95": [
            _round(lower * 10_000),
            _round(upper * 10_000),
        ],
        "two_sided_p_value": _round(p_value),
        "unit": "bps",
    }


def _stable_seed(outcomes: Sequence[dict[str, Any]], effective_trials: int) -> int:
    payload = "|".join(
        [
            str(max(1, effective_trials)),
            str(len(outcomes)),
            *[
                f"{row.get('session_date')}:{row.get('symbol')}:{row.get('value')}"
                for row in outcomes[:100]
            ],
        ]
    )
    return int.from_bytes(sha256(payload.encode()).digest()[:4], "big")


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None
