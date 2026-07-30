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
from statistics import fmean
from typing import Any, Callable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.labs.intraday.cross_sectional_portfolio import spearman
from app.services.labs.intraday.dataset_snapshot import load_snapshot_intraday_features
from app.services.research_architecture import load_snapshot_candles
from app.services.intraday_research_integrity import (
    clustered_outcome_statistics,
    cost_model_readiness,
    dataset_research_readiness,
    estimated_round_trip_cost_bps,
    exchange_session_date,
)

FACTOR_DIAGNOSTICS_VERSION = "intraday_factor_diagnostics_v2"
DEFAULT_FACTOR_KEYS = (
    "first_to_last_half_hour_market_momentum",
    "overnight_gap_acceptance_absorption",
    "cross_sectional_same_slot_continuation",
    "vwap_execution_pressure",
    "liquidity_shock_reversal",
    "auction_imbalance_pressure",
)
MINIMUM_OBSERVATIONS = 50
MINIMUM_VALIDATION_T = 3.0


def _default_institutional_readiness() -> dict[str, Any]:
    return {
        "institutional_candle_ready": False,
        "institutional_execution_ready": False,
        "auction_imbalances": {"ready": False},
        "gates": {},
        "limitations": ["backend_research_data_readiness_not_supplied"],
    }


@dataclass(frozen=True)
class FactorSpec:
    key: str
    title: str
    hypothesis: str
    supported_timeframes: tuple[str, ...]
    builder: Callable[..., list[dict[str, Any]]]
    references: tuple[str, ...]
    requires_quotes: bool = False
    requires_auction_data: bool = False

    def frozen(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "hypothesis": self.hypothesis,
            "supported_timeframes": list(self.supported_timeframes),
            "requires_quotes": self.requires_quotes,
            "requires_auction_data": self.requires_auction_data,
            "references": list(self.references),
        }


def _session_date(row: dict[str, Any]) -> date:
    value = row.get("session_date")
    if isinstance(value, date):
        return value
    return exchange_session_date(row)


def factor_research_readiness(
    spec: FactorSpec,
    *,
    data_readiness: dict[str, Any],
    institutional_readiness: dict[str, Any],
) -> dict[str, Any]:
    """Return the dataset gates that apply to this specific factor."""
    gates = {
        "snapshot_candle_research_ready": bool(data_readiness.get("candle_research_ready")),
        "institutional_candle_ready": bool(
            institutional_readiness.get("institutional_candle_ready")
        ),
    }
    if spec.requires_quotes:
        gates["snapshot_frozen_quote_coverage"] = bool(
            data_readiness.get("execution_research_ready")
        )
        gates["institutional_frozen_sip_coverage"] = bool(
            (institutional_readiness.get("gates") or {}).get(
                "frozen_microstructure_80pct_coverage"
            )
        )
    if spec.requires_auction_data:
        gates["auction_imbalance_ready"] = bool(
            (institutional_readiness.get("auction_imbalances") or {}).get("ready")
        )
    return {
        "factor_key": spec.key,
        "requires_quotes": spec.requires_quotes,
        "requires_auction_data": spec.requires_auction_data,
        "gates": gates,
        "ready": all(gates.values()),
        "limitations": [label for label, passed in gates.items() if not passed],
    }


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
    if timeframe != "30m":
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


def overnight_gap_acceptance_absorption_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **_: Any,
) -> list[dict[str, Any]]:
    """Opening participation distinguishes accepted gaps from absorbed gaps."""
    if timeframe != "30m":
        return []
    decision_index = 1
    output: list[dict[str, Any]] = []
    for symbol, rows in candles_by_symbol.items():
        sessions: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for row in sorted(rows, key=lambda item: item["timestamp"]):
            sessions[_session_date(row)].append(row)
        previous_close: float | None = None
        for session_date in sorted(sessions):
            session = sorted(sessions[session_date], key=lambda item: item["timestamp"])
            if previous_close and previous_close > 0 and len(session) > decision_index + 1:
                session_open = float(session[0]["open"])
                decision = session[decision_index]
                decision_close = float(decision["close"])
                gap = (session_open - previous_close) / previous_close
                relative_volume = decision.get("session_relative_volume")
                if (
                    abs(gap) >= 0.003
                    and relative_volume is not None
                    and float(relative_volume) >= 1.5
                    and session_open > 0
                ):
                    gap_fill = (
                        (session_open - decision_close) / (session_open - previous_close)
                        if session_open != previous_close
                        else 0.0
                    )
                    next_bar = session[decision_index + 1]
                    next_open = float(next_bar["open"])
                    next_return = (
                        (float(next_bar["close"]) - next_open) / next_open
                        if next_open > 0
                        else None
                    )
                    if next_return is not None and gap_fill <= 0.25:
                        output.append(
                            {
                                "factor_key": "overnight_gap_acceptance_absorption",
                                "symbol": symbol,
                                "session_date": session_date,
                                "timestamp": decision["timestamp"],
                                "score": gap,
                                "target_return": next_return,
                                "flow_state": "acceptance",
                            }
                        )
                    elif next_return is not None and gap_fill >= 0.50:
                        output.append(
                            {
                                "factor_key": "overnight_gap_acceptance_absorption",
                                "symbol": symbol,
                                "session_date": session_date,
                                "timestamp": decision["timestamp"],
                                "score": -gap,
                                "target_return": next_return,
                                "flow_state": "absorption",
                            }
                        )
            if session:
                previous_close = float(session[-1]["close"])
    return output


def vwap_execution_pressure_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **_: Any,
) -> list[dict[str, Any]]:
    """Abnormal participation away from VWAP predicts one-bar continuation."""
    if timeframe != "30m":
        return []
    output: list[dict[str, Any]] = []
    for symbol, rows in candles_by_symbol.items():
        ordered = sorted(rows, key=lambda item: item["timestamp"])
        for current, following in zip(ordered, ordered[1:]):
            if _session_date(current) != _session_date(following):
                continue
            vwap = current.get("session_vwap")
            relative_volume = current.get("session_relative_volume")
            if vwap is None or relative_volume is None or float(vwap) <= 0:
                continue
            close = float(current["close"])
            displacement = (close - float(vwap)) / float(vwap)
            if abs(displacement) < 0.001 or float(relative_volume) < 1.5:
                continue
            next_open = float(following["open"])
            if next_open <= 0:
                continue
            output.append(
                {
                    "factor_key": "vwap_execution_pressure",
                    "symbol": symbol,
                    "session_date": _session_date(current),
                    "timestamp": current["timestamp"],
                    "score": displacement * float(relative_volume),
                    "target_return": (float(following["close"]) - next_open) / next_open,
                }
            )
    return output


def auction_imbalance_pressure_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    auction_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    **_: Any,
) -> list[dict[str, Any]]:
    """Auction alpha requires event-time prices; candles are never substituted."""
    # The eventual target must be midpoint-at-message to auction execution,
    # which cannot be reconstructed from a 15m/30m OHLC bar.  Persisted
    # imbalance events are therefore a readiness input, not permission to
    # manufacture a target from candles.
    return []


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
    "overnight_gap_acceptance_absorption": FactorSpec(
        key="overnight_gap_acceptance_absorption",
        title="Overnight Gap Acceptance / Absorption",
        hypothesis=(
            "Urgent overnight repricing continues when elevated opening participation "
            "accepts the gap and reverses when that flow is absorbed."
        ),
        supported_timeframes=("30m",),
        builder=overnight_gap_acceptance_absorption_observations,
        references=(),
    ),
    "cross_sectional_same_slot_continuation": FactorSpec(
        key="cross_sectional_same_slot_continuation",
        title="Cross-Sectional Same-Slot Continuation",
        hypothesis=(
            "Institutional execution schedules repeat at the same intraday slot, so names "
            "with persistently positive same-slot returns outperform negative-score peers."
        ),
        supported_timeframes=("30m",),
        builder=cross_sectional_same_slot_observations,
        references=("https://arxiv.org/abs/1005.3535",),
    ),
    "vwap_execution_pressure": FactorSpec(
        key="vwap_execution_pressure",
        title="VWAP Execution Pressure",
        hypothesis=(
            "Abnormal scheduled participation that moves price away from session VWAP "
            "persists over the next execution interval."
        ),
        supported_timeframes=("30m",),
        builder=vwap_execution_pressure_observations,
        references=(),
    ),
    "liquidity_shock_reversal": FactorSpec(
        key="liquidity_shock_reversal",
        title="Liquidity-Shock Reversal",
        hypothesis=(
            "An abnormal idiosyncratic range/volume shock reverses when terminal quote-flow "
            "imbalance opposes the price move, indicating exhaustion rather than information."
        ),
        supported_timeframes=("30m",),
        builder=liquidity_shock_reversal_observations,
        references=("https://arxiv.org/abs/1011.6402",),
        requires_quotes=True,
    ),
    "auction_imbalance_pressure": FactorSpec(
        key="auction_imbalance_pressure",
        title="Opening / Closing Auction Imbalance Pressure",
        hypothesis=(
            "Published auction imbalance pressure moves the executable midpoint toward "
            "the auction clearing price when contra liquidity cannot absorb forced flow."
        ),
        supported_timeframes=("30m",),
        builder=auction_imbalance_pressure_observations,
        references=("https://nasdaqtrader.com/Trader.aspx?id=OpenClose",),
        requires_quotes=True,
        requires_auction_data=True,
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


def factor_metrics(
    observations: Sequence[dict[str, Any]],
    *,
    effective_trials: int = 1,
    cost_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    evidence = clustered_outcome_statistics(
        [
            {
                "value": value,
                "session_date": row["session_date"],
                "timestamp": row["timestamp"],
                "symbol": row["symbol"],
            }
            for row, value in zip(usable, directional)
        ],
        effective_trials=effective_trials,
        require_symbol_diversification=len({str(row["symbol"]) for row in usable}) > 1,
    )
    stressed_costs = (
        [
            estimated_round_trip_cost_bps(
                cost_model,
                symbol=str(row["symbol"]),
                timestamp=row["timestamp"],
                stressed=True,
            )
            for row in usable
        ]
        if cost_model is not None
        else []
    )
    net_directional = [
        value - cost / 10_000
        for value, cost in zip(directional, stressed_costs)
    ]
    net_evidence = (
        clustered_outcome_statistics(
            [
                {
                    "value": value,
                    "session_date": row["session_date"],
                    "timestamp": row["timestamp"],
                    "symbol": row["symbol"],
                }
                for row, value in zip(usable, net_directional)
            ],
            effective_trials=effective_trials,
            require_symbol_diversification=len({str(row["symbol"]) for row in usable}) > 1,
        )
        if net_directional
        else None
    )

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
        "distinct_sessions": evidence["distinct_sessions"],
        "distinct_symbols": evidence["distinct_symbols"],
        "rank_ic": _round(spearman(scores, targets)),
        "mean_cross_sectional_rank_ic": _round(fmean(group_ics)) if group_ics else None,
        "rank_ic_periods": len(group_ics),
        "top_minus_bottom_spread_bps": _round(fmean(spreads) * 10_000) if spreads else None,
        "gross_directional_edge_bps": _round(fmean(directional) * 10_000) if directional else None,
        "net_stressed_edge_bps": (
            _round(fmean(net_directional) * 10_000) if net_directional else None
        ),
        "median_stressed_cost_bps": (
            _round(sorted(stressed_costs)[len(stressed_costs) // 2])
            if stressed_costs
            else None
        ),
        "day_clustered_t_statistic": evidence["day_clustered_t_statistic"],
        "two_sided_normal_p_value": evidence["block_bootstrap"]["two_sided_p_value"],
        "hit_rate": _round(sum(value > 0 for value in directional) / len(directional)) if directional else None,
        "measurable": (
            len(usable) >= MINIMUM_OBSERVATIONS
            and evidence["independent_evidence_ready"]
        ),
        "evidence_quality": evidence,
        "net_evidence_quality": net_evidence,
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
    auction_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    institutional_data_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_dates = [
        _session_date(row)
        for rows in candles_by_symbol.values()
        for row in rows
    ]
    boundaries = chronological_boundaries(all_dates)
    data_readiness = dataset_research_readiness(
        candles_by_symbol,
        microstructure_by_symbol=microstructure_by_symbol,
    )
    cost_readiness = cost_model_readiness(cost_model, symbols=list(candles_by_symbol))
    institutional_readiness = (
        institutional_data_readiness or _default_institutional_readiness()
    )
    effective_trials = max(1, len(factor_keys))
    factor_results: dict[str, Any] = {}
    validation_p: dict[str, float | None] = {}
    for key in factor_keys:
        spec = FACTOR_SPECS[key]
        if timeframe not in spec.supported_timeframes:
            factor_results[key] = {"status": "unsupported_timeframe"}
            continue
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
        if spec.requires_auction_data and not auction_by_symbol:
            factor_results[key] = {
                "status": "blocked_missing_auction_data",
                "required_data": [
                    "event-time opening/closing imbalance messages",
                    "reference and clearing prices",
                    "paired and imbalance quantities",
                    "midpoint at message time",
                ],
            }
            continue
        readiness = factor_research_readiness(
            spec,
            data_readiness=data_readiness,
            institutional_readiness=institutional_readiness,
        )
        observations = spec.builder(
            candles_by_symbol,
            timeframe=timeframe,
            microstructure_by_symbol=microstructure_by_symbol,
            auction_by_symbol=auction_by_symbol,
        )
        discovery = [
            row for row in observations
            if boundaries["discovery_start"] <= row["session_date"] <= boundaries["discovery_end"]
        ]
        validation = [
            row for row in observations
            if boundaries["validation_start"] <= row["session_date"] <= boundaries["validation_end"]
        ]
        discovery_metrics = factor_metrics(
            discovery,
            effective_trials=effective_trials,
            cost_model=cost_model,
        )
        validation_metrics = factor_metrics(
            validation,
            effective_trials=effective_trials,
            cost_model=cost_model,
        )
        validation_p[key] = validation_metrics["two_sided_normal_p_value"]
        cost_clearance = _cost_clearance(validation_metrics, cost_model)
        factor_results[key] = {
            "status": "measured" if validation_metrics["measurable"] else "insufficient_evidence",
            "spec": spec.frozen(),
            "discovery": discovery_metrics,
            "validation": validation_metrics,
            "cost_clearance": cost_clearance,
            "factor_research_readiness": readiness,
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
            and validation["evidence_quality"]["selection_adjusted_signal"]
            and validation["net_evidence_quality"]["selection_adjusted_signal"]
            and data_readiness["candle_research_ready"]
            and cost_readiness["research_cost_available"]
            and result["factor_research_readiness"]["ready"]
        ):
            selected.append(key)
    return {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "mode": "discovery",
        "timeframe": timeframe,
        "split_boundaries": {key: str(value) for key, value in boundaries.items()},
        "cost_model": cost_model,
        "data_readiness": data_readiness,
        "institutional_data_readiness": institutional_readiness,
        "cost_readiness": cost_readiness,
        "effective_trials": effective_trials,
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
    auction_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
    institutional_data_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data_readiness = dataset_research_readiness(
        candles_by_symbol,
        microstructure_by_symbol=microstructure_by_symbol,
    )
    cost_readiness = cost_model_readiness(cost_model, symbols=list(candles_by_symbol))
    institutional_readiness = (
        institutional_data_readiness or _default_institutional_readiness()
    )
    effective_trials = max(1, len(factor_keys))
    factors: dict[str, Any] = {}
    p_values: dict[str, float | None] = {}
    for key in factor_keys:
        spec = FACTOR_SPECS[key]
        if spec.requires_quotes and not microstructure_by_symbol:
            factors[key] = {"status": "blocked_missing_quote_data"}
            continue
        if spec.requires_auction_data and not auction_by_symbol:
            factors[key] = {"status": "blocked_missing_auction_data"}
            continue
        readiness = factor_research_readiness(
            spec,
            data_readiness=data_readiness,
            institutional_readiness=institutional_readiness,
        )
        observations = spec.builder(
            candles_by_symbol,
            timeframe=timeframe,
            microstructure_by_symbol=microstructure_by_symbol,
            auction_by_symbol=auction_by_symbol,
        )
        metrics = factor_metrics(
            observations,
            effective_trials=effective_trials,
            cost_model=cost_model,
        )
        p_values[key] = metrics["two_sided_normal_p_value"]
        factors[key] = {
            "status": "measured" if metrics["measurable"] else "insufficient_evidence",
            "confirmation": metrics,
            "cost_clearance": _cost_clearance(metrics, cost_model),
            "factor_research_readiness": readiness,
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
            and metrics["evidence_quality"]["selection_adjusted_signal"]
            and metrics["net_evidence_quality"]["selection_adjusted_signal"]
            and data_readiness["candle_research_ready"]
            and cost_readiness["research_cost_available"]
            and result["factor_research_readiness"]["ready"]
        ):
            passed.append(key)
    return {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "mode": "confirmation",
        "timeframe": timeframe,
        "cost_model": cost_model,
        "data_readiness": data_readiness,
        "institutional_data_readiness": institutional_readiness,
        "cost_readiness": cost_readiness,
        "effective_trials": effective_trials,
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
    candles: dict[str, list[dict[str, Any]]] = {}
    for symbol in selected:
        rows = load_snapshot_candles(conn, dataset_id, symbol, timeframe)
        features = {
            row["timestamp"]: row
            for row in load_snapshot_intraday_features(conn, dataset_id, symbol, timeframe)
        }
        candles[symbol] = [
            {
                **row,
                **{
                    key: value
                    for key, value in (features.get(row["timestamp"]) or {}).items()
                    if key not in {"symbol", "timeframe", "timestamp"}
                },
            }
            for row in rows
        ]
    return {symbol: rows for symbol, rows in candles.items() if rows}, dict(manifest)


def load_microstructure(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime | None,
    end: datetime | None,
    dataset_id: int | None = None,
) -> dict[str, dict[datetime, dict[str, Any]]]:
    if dataset_id is None:
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
    else:
        rows = conn.execute(
            """
            SELECT symbol, timeframe, timestamp,
                   microstructure_provider AS provider,
                   microstructure_feed AS feed,
                   quote_count, median_spread_bps, p90_spread_bps,
                   mean_depth, order_flow_imbalance,
                   normalized_order_flow_imbalance
            FROM research_dataset_intraday_features
            WHERE dataset_id = %s
              AND symbol = ANY(%s) AND timeframe = %s
              AND quote_count > 0
              AND (%s IS NULL OR timestamp >= %s)
              AND (%s IS NULL OR timestamp <= %s)
            ORDER BY symbol, timestamp
            """,
            (
                dataset_id,
                list(symbols),
                timeframe,
                start,
                start,
                end,
                end,
            ),
        ).fetchall()
    output: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        item = dict(row)
        output[str(item["symbol"])][item["timestamp"]] = item
    return dict(output)


def load_auction_imbalances(
    conn: psycopg.Connection,
    *,
    symbols: Sequence[str],
    start: datetime | None,
    end: datetime | None,
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT *
        FROM intraday_auction_imbalances
        WHERE symbol = ANY(%s)
          AND (%s IS NULL OR timestamp >= %s)
          AND (%s IS NULL OR timestamp <= %s)
        ORDER BY symbol, timestamp
        """,
        (list(symbols), start, start, end, end),
    ).fetchall()
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        output[str(item["symbol"]).upper()].append(item)
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
        "provider": row["provider"],
        "feed": row["feed"],
        "by_symbol": dict(row["by_symbol"] or {}),
        "by_time_slot": dict(row["by_time_slot"] or {}),
        "window_start": row["window_start"],
        "window_end": row["window_end"],
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
    for key in factor_keys:
        factor_result = (result.get("factors") or {}).get(key) or {}
        conn.execute(
            """
            INSERT INTO intraday_research_trials(
                trial_fingerprint, trial_type, phase, architecture,
                family_name, candidate_id, timeframe, dataset_id,
                horizon_bars, effective_trials, parameters, symbols,
                split_policy, cost_model, outcome, calculation_version
            )
            VALUES (%s, 'factor', %s, %s, %s, %s, %s, %s, NULL, %s,
                    %s, %s, %s, %s, %s, %s)
            """,
            (
                sha256(f"{spec_hash}|{key}".encode()).hexdigest(),
                "confirmation" if mode == "confirmation" else "validation",
                key,
                FACTOR_SPECS[key].title,
                key,
                timeframe,
                dataset_id,
                int(result.get("effective_trials") or max(1, len(factor_keys))),
                Jsonb(FACTOR_SPECS[key].frozen()),
                Jsonb(list(symbols)),
                Jsonb(result.get("split_boundaries") or {}),
                Jsonb(result.get("cost_model") or {}),
                Jsonb(factor_result),
                FACTOR_DIAGNOSTICS_VERSION,
            ),
        )
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
    net_stressed = metrics.get("net_stressed_edge_bps")

    def clears(key: str) -> bool:
        value = cost_model.get(key)
        return bool(edge is not None and value is not None and edge > float(value))

    return {
        "gross_directional_edge_bps": edge,
        "net_stressed_edge_bps": net_stressed,
        "clears_observed": clears("observed_round_trip_bps"),
        "clears_stressed": (
            bool(net_stressed is not None and float(net_stressed) > 0)
            if net_stressed is not None
            else clears("stressed_round_trip_bps")
        ),
        "clears_conservative_30bps": clears("conservative_round_trip_bps"),
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None
