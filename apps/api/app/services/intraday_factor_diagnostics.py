"""Continuous intraday factor research with locked forward confirmation.

Discovery reads only the first 80% of chronological sessions (50% discovery,
30% validation). The final 20% is deliberately not calculated or returned.
Confirmation requires a different, later immutable dataset and the frozen
factor list from a completed discovery run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from json import dumps
from math import erfc, sqrt
from statistics import fmean, pstdev
from typing import Any, Callable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.labs.intraday.cross_sectional_portfolio import spearman
from app.services.research_architecture import load_snapshot_candles

FACTOR_DIAGNOSTICS_VERSION = "intraday_factor_diagnostics_v1"
DEFAULT_FACTOR_KEYS = (
    "first_to_last_half_hour_market_momentum",
    "cross_sectional_same_slot_continuation",
    "liquidity_shock_reversal",
)
MINIMUM_OBSERVATIONS = 50
MINIMUM_VALIDATION_T = 2.0


@dataclass(frozen=True)
class FactorSpec:
    key: str
    title: str
    hypothesis: str
    supported_timeframes: tuple[str, ...]
    builder: Callable[..., list[dict[str, Any]]]
    references: tuple[str, ...]
    requires_quotes: bool = False

    def frozen(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "hypothesis": self.hypothesis,
            "supported_timeframes": list(self.supported_timeframes),
            "requires_quotes": self.requires_quotes,
            "references": list(self.references),
        }


def _session_date(row: dict[str, Any]) -> date:
    value = row.get("session_date")
    if isinstance(value, date):
        return value
    return row["timestamp"].date()


def first_to_last_half_hour_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **_: Any,
) -> list[dict[str, Any]]:
    """Opening half-hour return predicts the final half-hour return."""
    if timeframe != "30m":
        return []
    observations: list[dict[str, Any]] = []
    for symbol in ("SPY", "QQQ"):
        rows = sorted(candles_by_symbol.get(symbol, []), key=lambda row: row["timestamp"])
        sessions: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            sessions[_session_date(row)].append(row)
        previous_close: float | None = None
        for session_date in sorted(sessions):
            session = sorted(sessions[session_date], key=lambda row: row["timestamp"])
            if previous_close and previous_close > 0 and len(session) >= 2:
                first_close = float(session[0]["close"])
                last_open = float(session[-1]["open"])
                last_close = float(session[-1]["close"])
                if last_open > 0:
                    observations.append(
                        {
                            "factor_key": "first_to_last_half_hour_market_momentum",
                            "symbol": symbol,
                            "session_date": session_date,
                            "timestamp": session[-1]["timestamp"],
                            "score": (first_close - previous_close) / previous_close,
                            "target_return": (last_close - last_open) / last_open,
                        }
                    )
            if session:
                previous_close = float(session[-1]["close"])
    return observations


def cross_sectional_same_slot_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    lookback_sessions: int = 20,
    **_: Any,
) -> list[dict[str, Any]]:
    """Prior-session same-slot mean predicts the current slot cross-section."""
    if timeframe not in {"15m", "30m"}:
        return []
    candidates: list[dict[str, Any]] = []
    for symbol, raw_rows in candles_by_symbol.items():
        history: dict[tuple[int, int], list[float]] = defaultdict(list)
        for row in sorted(raw_rows, key=lambda item: item["timestamp"]):
            timestamp = row["timestamp"]
            slot = (timestamp.hour, timestamp.minute)
            open_price = float(row["open"])
            close = float(row["close"])
            if open_price <= 0:
                continue
            target = (close - open_price) / open_price
            prior = history[slot][-lookback_sessions:]
            if len(prior) >= 5:
                candidates.append(
                    {
                        "factor_key": "cross_sectional_same_slot_continuation",
                        "symbol": symbol,
                        "session_date": _session_date(row),
                        "timestamp": timestamp,
                        "score": fmean(prior),
                        "target_return": target,
                    }
                )
            history[slot].append(target)

    # A cross-sectional claim is only observable when at least four symbols
    # share the exact timestamp. Single-name rows are excluded, not converted
    # into time-series evidence for a different hypothesis.
    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        grouped[row["timestamp"]].append(row)
    return [
        row
        for rows in grouped.values()
        if len(rows) >= 4
        for row in rows
    ]


def liquidity_shock_reversal_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    microstructure_by_symbol: dict[str, dict[datetime, dict[str, Any]]] | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """Abnormal range/volume plus quote-flow exhaustion predicts reversal."""
    if not microstructure_by_symbol:
        return []
    market_by_time: dict[datetime, list[float]] = defaultdict(list)
    returns_by_symbol: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for symbol, rows in candles_by_symbol.items():
        values: list[tuple[dict[str, Any], float]] = []
        for row in sorted(rows, key=lambda item: item["timestamp"]):
            open_price = float(row["open"])
            if open_price <= 0:
                continue
            value = (float(row["close"]) - open_price) / open_price
            values.append((row, value))
            market_by_time[row["timestamp"]].append(value)
        returns_by_symbol[symbol] = values

    output: list[dict[str, Any]] = []
    for symbol, values in returns_by_symbol.items():
        volume_history: list[float] = []
        range_history: list[float] = []
        quote_map = microstructure_by_symbol.get(symbol, {})
        for index, (row, bar_return) in enumerate(values):
            volume = float(row.get("volume") or 0)
            open_price = float(row["open"])
            bar_range = (float(row["high"]) - float(row["low"])) / open_price
            quote = quote_map.get(row["timestamp"])
            if index >= 20 and quote:
                baseline_volume = fmean(volume_history[-20:])
                baseline_range = fmean(range_history[-20:])
                normalized_ofi = quote.get("normalized_order_flow_imbalance")
                if (
                    baseline_volume > 0
                    and baseline_range > 0
                    and volume >= 2 * baseline_volume
                    and bar_range >= 2 * baseline_range
                    and normalized_ofi is not None
                    and index + 1 < len(values)
                ):
                    market_return = fmean(market_by_time[row["timestamp"]])
                    residual = bar_return - market_return
                    # Exhaustion: price shock and terminal OFI disagree.
                    if residual * float(normalized_ofi) < 0:
                        next_row, next_return = values[index + 1]
                        if _session_date(next_row) == _session_date(row):
                            output.append(
                                {
                                    "factor_key": "liquidity_shock_reversal",
                                    "symbol": symbol,
                                    "session_date": _session_date(row),
                                    "timestamp": row["timestamp"],
                                    "score": -residual,
                                    "target_return": next_return,
                                }
                            )
            volume_history.append(volume)
            range_history.append(bar_range)
    return output


FACTOR_SPECS: dict[str, FactorSpec] = {
    "first_to_last_half_hour_market_momentum": FactorSpec(
        key="first_to_last_half_hour_market_momentum",
        title="First-to-Last Half-Hour Market Momentum",
        hypothesis=(
            "Urgent information incorporated in the first market half-hour persists into "
            "the closing half-hour as benchmark and closing-auction demand completes."
        ),
        supported_timeframes=("30m",),
        builder=first_to_last_half_hour_observations,
        references=("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866",),
    ),
    "cross_sectional_same_slot_continuation": FactorSpec(
        key="cross_sectional_same_slot_continuation",
        title="Cross-Sectional Same-Slot Continuation",
        hypothesis=(
            "Institutional execution schedules repeat at the same intraday slot, so names "
            "with persistently positive same-slot returns outperform negative-score peers."
        ),
        supported_timeframes=("15m", "30m"),
        builder=cross_sectional_same_slot_observations,
        references=("https://arxiv.org/abs/1005.3535",),
    ),
    "liquidity_shock_reversal": FactorSpec(
        key="liquidity_shock_reversal",
        title="Liquidity-Shock Reversal",
        hypothesis=(
            "An abnormal idiosyncratic range/volume shock reverses when terminal quote-flow "
            "imbalance opposes the price move, indicating exhaustion rather than information."
        ),
        supported_timeframes=("15m", "30m"),
        builder=liquidity_shock_reversal_observations,
        references=("https://arxiv.org/abs/1011.6402",),
        requires_quotes=True,
    ),
}


def chronological_boundaries(session_dates: Sequence[date]) -> dict[str, Any]:
    ordered = sorted(set(session_dates))
    if len(ordered) < 10:
        raise ValueError("At least 10 distinct sessions are required for chronological factor splits.")
    discovery_end_index = max(0, int(len(ordered) * 0.5) - 1)
    validation_end_index = max(discovery_end_index + 1, int(len(ordered) * 0.8) - 1)
    validation_end_index = min(validation_end_index, len(ordered) - 2)
    return {
        "discovery_start": ordered[0],
        "discovery_end": ordered[discovery_end_index],
        "validation_start": ordered[discovery_end_index + 1],
        "validation_end": ordered[validation_end_index],
        "confirmation_start": ordered[validation_end_index + 1],
        "confirmation_end": ordered[-1],
        "distinct_sessions": len(ordered),
    }


def factor_metrics(observations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in observations
        if row.get("score") is not None and row.get("target_return") is not None
    ]
    scores = [float(row["score"]) for row in usable]
    targets = [float(row["target_return"]) for row in usable]
    directional = [
        (1.0 if score > 0 else -1.0 if score < 0 else 0.0) * target
        for score, target in zip(scores, targets)
    ]
    by_day: dict[date, list[float]] = defaultdict(list)
    for row, value in zip(usable, directional):
        by_day[row["session_date"]].append(value)
    daily = [fmean(values) for _, values in sorted(by_day.items()) if values]
    daily_mean = fmean(daily) if daily else None
    daily_deviation = pstdev(daily) if len(daily) > 1 else 0.0
    t_stat = (
        daily_mean / (daily_deviation / sqrt(len(daily)))
        if daily_mean is not None and daily_deviation > 0
        else (999.0 if daily_mean is not None and daily_mean > 0 and len(daily) > 1 else None)
    )
    p_value = erfc(abs(t_stat) / sqrt(2)) if t_stat is not None else None

    grouped: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in usable:
        grouped[row["timestamp"]].append(row)
    group_ics: list[float] = []
    spreads: list[float] = []
    for rows in grouped.values():
        if len(rows) < 4:
            continue
        ic = spearman(
            [float(row["score"]) for row in rows],
            [float(row["target_return"]) for row in rows],
        )
        if ic is not None:
            group_ics.append(ic)
        ordered = sorted(rows, key=lambda row: float(row["score"]))
        tail = max(1, len(ordered) // 5)
        spreads.append(
            fmean(float(row["target_return"]) for row in ordered[-tail:])
            - fmean(float(row["target_return"]) for row in ordered[:tail])
        )

    return {
        "observations": len(usable),
        "distinct_sessions": len(by_day),
        "rank_ic": _round(spearman(scores, targets)),
        "mean_cross_sectional_rank_ic": _round(fmean(group_ics)) if group_ics else None,
        "rank_ic_periods": len(group_ics),
        "top_minus_bottom_spread_bps": _round(fmean(spreads) * 10_000) if spreads else None,
        "gross_directional_edge_bps": _round(fmean(directional) * 10_000) if directional else None,
        "day_clustered_t_statistic": _round(t_stat),
        "two_sided_normal_p_value": _round(p_value),
        "hit_rate": _round(sum(value > 0 for value in directional) / len(directional)) if directional else None,
        "measurable": len(usable) >= MINIMUM_OBSERVATIONS,
    }


def benjamini_hochberg(p_values: dict[str, float | None]) -> dict[str, float | None]:
    valid = sorted(
        ((key, float(value)) for key, value in p_values.items() if value is not None),
        key=lambda item: item[1],
    )
    adjusted: dict[str, float | None] = {key: None for key in p_values}
    running = 1.0
    count = len(valid)
    for reverse_index in range(count - 1, -1, -1):
        key, value = valid[reverse_index]
        rank = reverse_index + 1
        running = min(running, value * count / rank)
        adjusted[key] = _round(min(1.0, running))
    return adjusted


def evaluate_factor_discovery(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    factor_keys: Sequence[str],
    cost_model: dict[str, Any],
    microstructure_by_symbol: dict[str, dict[datetime, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    all_dates = [
        _session_date(row)
        for rows in candles_by_symbol.values()
        for row in rows
    ]
    boundaries = chronological_boundaries(all_dates)
    factor_results: dict[str, Any] = {}
    validation_p: dict[str, float | None] = {}
    for key in factor_keys:
        spec = FACTOR_SPECS[key]
        if timeframe not in spec.supported_timeframes:
            factor_results[key] = {"status": "unsupported_timeframe"}
            continue
        observations = spec.builder(
            candles_by_symbol,
            timeframe=timeframe,
            microstructure_by_symbol=microstructure_by_symbol,
        )
        if spec.requires_quotes and not microstructure_by_symbol:
            factor_results[key] = {
                "status": "blocked_missing_quote_data",
                "required_data": [
                    "bid/ask quotes",
                    "quote sizes",
                    "bar-level order-flow imbalance",
                ],
            }
            continue
        discovery = [
            row for row in observations
            if boundaries["discovery_start"] <= row["session_date"] <= boundaries["discovery_end"]
        ]
        validation = [
            row for row in observations
            if boundaries["validation_start"] <= row["session_date"] <= boundaries["validation_end"]
        ]
        discovery_metrics = factor_metrics(discovery)
        validation_metrics = factor_metrics(validation)
        validation_p[key] = validation_metrics["two_sided_normal_p_value"]
        cost_clearance = _cost_clearance(validation_metrics, cost_model)
        factor_results[key] = {
            "status": "measured" if validation_metrics["measurable"] else "insufficient_evidence",
            "spec": spec.frozen(),
            "discovery": discovery_metrics,
            "validation": validation_metrics,
            "cost_clearance": cost_clearance,
            "confirmation": {
                "status": "locked",
                "sessions_withheld": (
                    boundaries["confirmation_end"] - boundaries["confirmation_start"]
                ).days + 1,
                "detail": "No confirmation metric was calculated during discovery.",
            },
        }

    q_values = benjamini_hochberg(validation_p)
    selected: list[str] = []
    for key, result in factor_results.items():
        if result.get("status") != "measured":
            continue
        validation = result["validation"]
        result["validation"]["false_discovery_rate_q_value"] = q_values.get(key)
        rank_ic = validation["mean_cross_sectional_rank_ic"]
        if rank_ic is None:
            rank_ic = validation["rank_ic"]
        if (
            rank_ic is not None
            and rank_ic > 0
            and (validation["day_clustered_t_statistic"] or 0) >= MINIMUM_VALIDATION_T
            and result["cost_clearance"]["clears_stressed"]
            and q_values.get(key) is not None
            and float(q_values[key]) <= 0.1
        ):
            selected.append(key)
    return {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "mode": "discovery",
        "timeframe": timeframe,
        "split_boundaries": {key: str(value) for key, value in boundaries.items()},
        "cost_model": cost_model,
        "factors": factor_results,
        "selected_for_forward_confirmation": selected,
        "confirmation_data_accessed": False,
    }


def evaluate_forward_confirmation(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    factor_keys: Sequence[str],
    cost_model: dict[str, Any],
    microstructure_by_symbol: dict[str, dict[datetime, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    factors: dict[str, Any] = {}
    p_values: dict[str, float | None] = {}
    for key in factor_keys:
        spec = FACTOR_SPECS[key]
        if spec.requires_quotes and not microstructure_by_symbol:
            factors[key] = {"status": "blocked_missing_quote_data"}
            continue
        observations = spec.builder(
            candles_by_symbol,
            timeframe=timeframe,
            microstructure_by_symbol=microstructure_by_symbol,
        )
        metrics = factor_metrics(observations)
        p_values[key] = metrics["two_sided_normal_p_value"]
        factors[key] = {
            "status": "measured" if metrics["measurable"] else "insufficient_evidence",
            "confirmation": metrics,
            "cost_clearance": _cost_clearance(metrics, cost_model),
        }
    q_values = benjamini_hochberg(p_values)
    passed: list[str] = []
    for key, result in factors.items():
        if result.get("status") != "measured":
            continue
        result["confirmation"]["false_discovery_rate_q_value"] = q_values.get(key)
        metrics = result["confirmation"]
        rank_ic = metrics["mean_cross_sectional_rank_ic"]
        if rank_ic is None:
            rank_ic = metrics["rank_ic"]
        if (
            rank_ic is not None
            and rank_ic > 0
            and (metrics["day_clustered_t_statistic"] or 0) >= MINIMUM_VALIDATION_T
            and result["cost_clearance"]["clears_stressed"]
            and q_values.get(key) is not None
            and float(q_values[key]) <= 0.1
        ):
            passed.append(key)
    return {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "mode": "confirmation",
        "timeframe": timeframe,
        "cost_model": cost_model,
        "factors": factors,
        "passed_locked_confirmation": passed,
    }


def load_dataset_candles(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    symbols: Sequence[str] | None = None,
    max_symbols: int = 200,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    manifest = conn.execute(
        "SELECT * FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"No dataset manifest for dataset_id={dataset_id}.")
    available = [str(item).upper() for item in (manifest["assets"] or [])]
    selected = [item.upper() for item in symbols] if symbols else available[:max_symbols]
    # Keep benchmark ETFs even when they fall after the ordinary symbol cap.
    selected = list(dict.fromkeys([*selected, *[item for item in ("SPY", "QQQ") if item in available]]))
    candles = {
        symbol: load_snapshot_candles(conn, dataset_id, symbol, timeframe)
        for symbol in selected
    }
    return {symbol: rows for symbol, rows in candles.items() if rows}, dict(manifest)


def load_microstructure(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, dict[datetime, dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT *
        FROM intraday_microstructure_features
        WHERE symbol = ANY(%s) AND timeframe = %s
          AND (%s IS NULL OR timestamp >= %s)
          AND (%s IS NULL OR timestamp <= %s)
        ORDER BY symbol, timestamp
        """,
        (list(symbols), timeframe, start, start, end, end),
    ).fetchall()
    output: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        item = dict(row)
        output[str(item["symbol"])][item["timestamp"]] = item
    return dict(output)


def load_cost_model(conn: psycopg.Connection, calibration_id: int | None) -> dict[str, Any]:
    if calibration_id is None:
        return {
            "calibration_id": None,
            "observed_round_trip_bps": 30.0,
            "stressed_round_trip_bps": 30.0,
            "conservative_round_trip_bps": 30.0,
            "basis": "No observed calibration selected; all scenarios use the conservative 30bps baseline.",
        }
    row = conn.execute(
        "SELECT * FROM intraday_execution_cost_calibrations WHERE id = %s",
        (calibration_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No execution-cost calibration id={calibration_id}.")
    return {
        "calibration_id": calibration_id,
        "observed_round_trip_bps": _float_or_none(row["observed_round_trip_bps"]),
        "stressed_round_trip_bps": _float_or_none(row["stressed_round_trip_bps"]),
        "conservative_round_trip_bps": float(row["conservative_round_trip_bps"]),
        "quote_observations": int(row["quote_observations"]),
        "matched_fill_observations": int(row["matched_fill_observations"]),
        "basis": row["methodology"],
    }


def frozen_spec_hash(
    *,
    factor_keys: Sequence[str],
    timeframe: str,
    cost_model: dict[str, Any],
) -> str:
    payload = {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "timeframe": timeframe,
        "factor_specs": [FACTOR_SPECS[key].frozen() for key in factor_keys],
        "cost_model": cost_model,
    }
    return sha256(dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def persist_factor_run(
    conn: psycopg.Connection,
    *,
    mode: str,
    dataset_id: int,
    source_run_id: int | None,
    timeframe: str,
    factor_keys: Sequence[str],
    symbols: Sequence[str],
    result: dict[str, Any],
    spec_hash: str,
) -> int:
    row = conn.execute(
        """
        INSERT INTO intraday_factor_diagnostic_runs(
            mode, status, dataset_id, source_run_id, timeframe, factor_keys,
            symbols, split_boundaries, cost_model, results, frozen_spec_hash,
            protocol_version, completed_at
        )
        VALUES (%s, 'completed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING id
        """,
        (
            mode,
            dataset_id,
            source_run_id,
            timeframe,
            Jsonb(list(factor_keys)),
            Jsonb(list(symbols)),
            Jsonb(result.get("split_boundaries") or {}),
            Jsonb(result["cost_model"]),
            Jsonb(result),
            spec_hash,
            FACTOR_DIAGNOSTICS_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def _cost_clearance(metrics: dict[str, Any], cost_model: dict[str, Any]) -> dict[str, Any]:
    edge = metrics.get("gross_directional_edge_bps")

    def clears(key: str) -> bool:
        value = cost_model.get(key)
        return bool(edge is not None and value is not None and edge > float(value))

    return {
        "gross_directional_edge_bps": edge,
        "clears_observed": clears("observed_round_trip_bps"),
        "clears_stressed": clears("stressed_round_trip_bps"),
        "clears_conservative_30bps": clears("conservative_round_trip_bps"),
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None
