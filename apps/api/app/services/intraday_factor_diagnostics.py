"""Continuous intraday factor research with locked forward confirmation.

Discovery reads only the first 80% of chronological sessions (50% discovery,
30% validation). The final 20% is deliberately not calculated or returned.
Confirmation requires a different, later immutable dataset and the frozen
factor list from a completed discovery run.

Every observation carries its own timing provenance -- which bar produced the
score, when that score became knowable, and which bars the target spans -- so
that `intraday_research_leakage` can prove mechanically that no factor reads
its own future.  Session positions resolve through `intraday_session_calendar`
rather than through list indices, because frozen snapshots contain
extended-hours bars.
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
from app.services.research_architecture import jsonable, load_snapshot_candles
from app.services.intraday_research_integrity import (
    clustered_outcome_statistics,
    cost_model_readiness,
    dataset_research_readiness,
    estimated_round_trip_cost_bps,
    exchange_session_date,
)
from app.services.intraday_research_power import (
    benchmark_session_context,
    power_and_stability_report,
)
from app.services.intraday_session_calendar import (
    bar_close_timestamp,
    bar_slot,
    closing_bar,
    extended_hours_audit,
    is_consecutive_session,
    opening_bar,
    ordered_regular_sessions,
    regular_session_rows,
)

FACTOR_DIAGNOSTICS_VERSION = "intraday_factor_diagnostics_v4_certified_instrument"
DEFAULT_FACTOR_KEYS = (
    "first_to_last_half_hour_market_momentum",
    "first_to_last_half_hour_market_reversal",
    "gap_up_acceptance_continuation",
    "gap_down_acceptance_continuation",
    "gap_up_absorption_reversal",
    "gap_down_absorption_reversal",
    "cross_sectional_same_slot_continuation",
    "cross_sectional_same_slot_reversal",
    "vwap_execution_pressure",
    "vwap_execution_pressure_fade",
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


# How a factor claims to earn money decides how it must be measured.  A
# rank-IC requirement is meaningful for a continuously scored cross-section
# and meaningless for a rare directional event whose score is a single signed
# magnitude, so the evidence gate dispatches on this rather than applying one
# universal rule.  See `factor_evidence_gate`.
FACTOR_TYPES = ("continuous", "directional_event", "cross_sectional")


@dataclass(frozen=True)
class FactorSpec:
    key: str
    title: str
    hypothesis: str
    supported_timeframes: tuple[str, ...]
    builder: Callable[..., list[dict[str, Any]]]
    references: tuple[str, ...]
    factor_type: str = "continuous"
    requires_quotes: bool = False
    requires_auction_data: bool = False

    def __post_init__(self) -> None:
        if self.factor_type not in FACTOR_TYPES:
            raise ValueError(
                f"{self.key}: factor_type must be one of {FACTOR_TYPES}, got {self.factor_type!r}"
            )

    def frozen(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "hypothesis": self.hypothesis,
            "factor_type": self.factor_type,
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


def _exchange_slot(timestamp: datetime) -> str:
    return bar_slot(timestamp)


def _observation(
    *,
    factor_key: str,
    symbol: str,
    session_date: date,
    score: float,
    target_return: float,
    signal_bar: dict[str, Any],
    entry_bar: dict[str, Any],
    exit_bar: dict[str, Any],
    timeframe: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build an observation that carries its own timing provenance.

    ``signal_bar`` is the last bar whose data enters the score, so the score
    is knowable at that bar's close.  ``entry_bar``/``exit_bar`` delimit the
    target.  These fields are what makes leakage mechanically checkable rather
    than a matter of reading the builder and trusting it.
    """
    return {
        "factor_key": factor_key,
        "symbol": symbol,
        "session_date": session_date,
        # Retained as the clustering and execution-cost key: costs are charged
        # at the slot where the position is actually opened.
        "timestamp": entry_bar["timestamp"],
        "score": score,
        "target_return": target_return,
        "signal_bar_timestamp": signal_bar["timestamp"],
        "decision_timestamp": bar_close_timestamp(
            signal_bar["timestamp"], timeframe=timeframe
        ),
        "entry_bar_timestamp": entry_bar["timestamp"],
        "exit_bar_timestamp": exit_bar["timestamp"],
        "horizon_bars": 1,
        **extra,
    }


def _jsonable_factor_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable_factor_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_factor_payload(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    return jsonable(value)


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
    """Opening half-hour return predicts the final half-hour return.

    The score is the published first half-hour return -- the 09:30 bar's close
    against the previous session's closing price, so it includes the overnight
    move.  The target is the executable version of the published closing
    half-hour return: entered at the open of the last regular bar rather than
    at the preceding bar's close, which is not a price anyone can trade.
    """
    if timeframe != "30m":
        return []
    observations: list[dict[str, Any]] = []
    for symbol in ("SPY", "QQQ"):
        previous_close: float | None = None
        previous_session: date | None = None
        for session_date, session in ordered_regular_sessions(
            candles_by_symbol.get(symbol, []),
            timeframe=timeframe,
        ):
            first_bar = opening_bar(session, timeframe=timeframe)
            last_bar = closing_bar(session, timeframe=timeframe)
            # A session whose bar complement matches neither the full-day nor
            # the early-close calendar has no identifiable closing half hour.
            if first_bar is None or last_bar is None:
                previous_close, previous_session = None, None
                continue
            adjacent = is_consecutive_session(previous_session, session_date)
            if first_bar["timestamp"] != last_bar["timestamp"]:
                last_open = float(last_bar["open"])
                if adjacent and previous_close and previous_close > 0 and last_open > 0:
                    observations.append(
                        _observation(
                            factor_key="first_to_last_half_hour_market_momentum",
                            symbol=symbol,
                            session_date=session_date,
                            score=(float(first_bar["close"]) - previous_close) / previous_close,
                            target_return=(float(last_bar["close"]) - last_open) / last_open,
                            signal_bar=first_bar,
                            entry_bar=last_bar,
                            exit_bar=last_bar,
                            timeframe=timeframe,
                            signal_polarity="continuation",
                        )
                    )
            previous_close = float(last_bar["close"])
            previous_session = session_date
    return observations


def first_to_last_half_hour_reversal_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Opening half-hour return predicts the opposite final-half-hour return."""
    return [
        {
            **row,
            "factor_key": "first_to_last_half_hour_market_reversal",
            "score": -float(row["score"]),
            "signal_polarity": "reversal",
        }
        for row in first_to_last_half_hour_observations(
            candles_by_symbol,
            timeframe=timeframe,
            **kwargs,
        )
    ]


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
        # Keyed on the exchange-local slot: a UTC key silently merges 10:00 ET
        # and 09:00 ET observations across a daylight-saving boundary.
        history: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
        for row in regular_session_rows(raw_rows, timeframe=timeframe):
            slot = _exchange_slot(row["timestamp"])
            open_price = float(row["open"])
            close = float(row["close"])
            if open_price <= 0:
                continue
            target = (close - open_price) / open_price
            prior = history[slot][-lookback_sessions:]
            if len(prior) >= 5:
                candidates.append(
                    _observation(
                        factor_key="cross_sectional_same_slot_continuation",
                        symbol=symbol,
                        session_date=_session_date(row),
                        score=fmean(value for _, value in prior),
                        target_return=target,
                        # The score is complete at the close of the most recent
                        # prior same-slot bar, a full session before entry.
                        signal_bar=prior[-1][0],
                        entry_bar=row,
                        exit_bar=row,
                        timeframe=timeframe,
                        signal_polarity="continuation",
                    )
                )
            history[slot].append((row, target))

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


def cross_sectional_same_slot_reversal_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Persistent same-slot leaders predict underperformance at the next slot."""
    return [
        {
            **row,
            "factor_key": "cross_sectional_same_slot_reversal",
            "score": -float(row["score"]),
            "signal_polarity": "reversal",
        }
        for row in cross_sectional_same_slot_observations(
            candles_by_symbol,
            timeframe=timeframe,
            **kwargs,
        )
    ]


def overnight_gap_acceptance_absorption_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    horizon_bars: int = 1,
    **_: Any,
) -> list[dict[str, Any]]:
    """Opening participation distinguishes accepted gaps from absorbed gaps.

    The gap is measured from the previous session's regular closing price to
    the 09:30 open, and acceptance is judged on the bar after the open.  Both
    positions come from the calendar: on a day carrying a premarket bar, list
    indices measure the 09:00 open against the previous 16:30 print and judge
    acceptance on the opening bar itself.

    ``horizon_bars`` holds the position for that many bars, entered at the open
    of the bar after the decision.  The position is never carried past the
    session close: an event whose horizon would run past the last regular bar
    is dropped rather than silently shortened, because a shortened horizon is a
    different hypothesis from the one that was declared.
    """
    if timeframe != "30m":
        return []
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be at least 1")
    decision_index = 1
    entry_index = decision_index + 1
    output: list[dict[str, Any]] = []
    for symbol, rows in candles_by_symbol.items():
        previous_close: float | None = None
        previous_session: date | None = None
        for session_date, session in ordered_regular_sessions(rows, timeframe=timeframe):
            first_bar = opening_bar(session, timeframe=timeframe)
            last_bar = closing_bar(session, timeframe=timeframe)
            if first_bar is None or session[0]["timestamp"] != first_bar["timestamp"]:
                previous_close = float(last_bar["close"]) if last_bar else None
                previous_session = session_date if last_bar else None
                continue
            exit_index = entry_index + horizon_bars - 1
            # Point-in-time membership leaves holes where a symbol was out of
            # the universe. A close-to-open move across such a hole spans
            # months, not a night, and is not the event being hypothesised.
            adjacent = is_consecutive_session(previous_session, session_date)
            if adjacent and previous_close and previous_close > 0 and len(session) > exit_index:
                session_open = float(first_bar["open"])
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
                    entry_bar = session[entry_index]
                    exit_bar = session[exit_index]
                    entry_price = float(entry_bar["open"])
                    horizon_return = (
                        (float(exit_bar["close"]) - entry_price) / entry_price
                        if entry_price > 0
                        else None
                    )
                    flow_state = (
                        "acceptance"
                        if gap_fill <= 0.25
                        else "absorption"
                        if gap_fill >= 0.50
                        else None
                    )
                    if horizon_return is not None and flow_state is not None:
                        output.append(
                            _observation(
                                factor_key="overnight_gap_acceptance_absorption",
                                symbol=symbol,
                                session_date=session_date,
                                score=gap if flow_state == "acceptance" else -gap,
                                target_return=horizon_return,
                                signal_bar=decision,
                                entry_bar=entry_bar,
                                exit_bar=exit_bar,
                                timeframe=timeframe,
                                horizon_bars=horizon_bars,
                                flow_state=flow_state,
                                gap_return=gap,
                                gap_fill_fraction=gap_fill,
                                gap_direction="up" if gap > 0 else "down",
                            )
                        )
            if last_bar is not None:
                previous_close = float(last_bar["close"])
                previous_session = session_date
            else:
                previous_close, previous_session = None, None
    return output


def _overnight_gap_variant_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    factor_key: str,
    gap_direction: str,
    flow_state: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Select one predeclared gap side/state without changing its economic sign."""
    return [
        {
            **row,
            "factor_key": factor_key,
            "signal_polarity": (
                "continuation" if flow_state == "acceptance" else "reversal"
            ),
        }
        for row in overnight_gap_acceptance_absorption_observations(
            candles_by_symbol,
            timeframe=timeframe,
            **kwargs,
        )
        if row["gap_direction"] == gap_direction
        and row["flow_state"] == flow_state
    ]


def gap_up_acceptance_continuation_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return _overnight_gap_variant_observations(
        candles_by_symbol,
        timeframe=timeframe,
        factor_key="gap_up_acceptance_continuation",
        gap_direction="up",
        flow_state="acceptance",
        **kwargs,
    )


def gap_down_acceptance_continuation_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return _overnight_gap_variant_observations(
        candles_by_symbol,
        timeframe=timeframe,
        factor_key="gap_down_acceptance_continuation",
        gap_direction="down",
        flow_state="acceptance",
        **kwargs,
    )


def gap_up_absorption_reversal_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return _overnight_gap_variant_observations(
        candles_by_symbol,
        timeframe=timeframe,
        factor_key="gap_up_absorption_reversal",
        gap_direction="up",
        flow_state="absorption",
        **kwargs,
    )


def gap_down_absorption_reversal_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return _overnight_gap_variant_observations(
        candles_by_symbol,
        timeframe=timeframe,
        factor_key="gap_down_absorption_reversal",
        gap_direction="down",
        flow_state="absorption",
        **kwargs,
    )


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
        ordered = regular_session_rows(rows, timeframe=timeframe)
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
                _observation(
                    factor_key="vwap_execution_pressure",
                    symbol=symbol,
                    session_date=_session_date(current),
                    score=displacement * float(relative_volume),
                    target_return=(float(following["close"]) - next_open) / next_open,
                    signal_bar=current,
                    entry_bar=following,
                    exit_bar=following,
                    timeframe=timeframe,
                    signal_polarity="continuation",
                )
            )
    return output


def vwap_execution_pressure_fade_observations(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    timeframe: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Abnormal displacement from VWAP predicts a one-bar fade."""
    return [
        {
            **row,
            "factor_key": "vwap_execution_pressure_fade",
            "score": -float(row["score"]),
            "signal_polarity": "reversal",
        }
        for row in vwap_execution_pressure_observations(
            candles_by_symbol,
            timeframe=timeframe,
            **kwargs,
        )
    ]


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
        for row in regular_session_rows(rows, timeframe=timeframe):
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
                                _observation(
                                    factor_key="liquidity_shock_reversal",
                                    symbol=symbol,
                                    session_date=_session_date(row),
                                    score=-residual,
                                    target_return=next_return,
                                    signal_bar=row,
                                    entry_bar=next_row,
                                    exit_bar=next_row,
                                    timeframe=timeframe,
                                    signal_polarity="reversal",
                                )
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
        factor_type="continuous",
        references=("https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866",),
    ),
    "first_to_last_half_hour_market_reversal": FactorSpec(
        key="first_to_last_half_hour_market_reversal",
        title="First-to-Last Half-Hour Market Reversal",
        hypothesis=(
            "Opening information pressure is absorbed during the session, so the final "
            "half-hour moves against the return through the first half-hour."
        ),
        supported_timeframes=("30m",),
        builder=first_to_last_half_hour_reversal_observations,
        factor_type="continuous",
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
        factor_type="directional_event",
        references=(),
    ),
    "gap_up_acceptance_continuation": FactorSpec(
        key="gap_up_acceptance_continuation",
        title="Gap-Up Acceptance Continuation",
        hypothesis=(
            "Urgent positive overnight repricing continues when elevated opening "
            "participation accepts a gap up rather than filling it."
        ),
        supported_timeframes=("30m",),
        builder=gap_up_acceptance_continuation_observations,
        factor_type="directional_event",
        references=(),
    ),
    "gap_down_acceptance_continuation": FactorSpec(
        key="gap_down_acceptance_continuation",
        title="Gap-Down Acceptance Continuation",
        hypothesis=(
            "Urgent negative overnight repricing continues when elevated opening "
            "participation accepts a gap down rather than filling it."
        ),
        supported_timeframes=("30m",),
        builder=gap_down_acceptance_continuation_observations,
        factor_type="directional_event",
        references=(),
    ),
    "gap_up_absorption_reversal": FactorSpec(
        key="gap_up_absorption_reversal",
        title="Gap-Up Absorption Reversal",
        hypothesis=(
            "A gap up reverses when elevated opening participation fills at least half "
            "the gap, revealing that regular-session sellers absorbed overnight demand."
        ),
        supported_timeframes=("30m",),
        builder=gap_up_absorption_reversal_observations,
        factor_type="directional_event",
        references=(),
    ),
    "gap_down_absorption_reversal": FactorSpec(
        key="gap_down_absorption_reversal",
        title="Gap-Down Absorption Reversal",
        hypothesis=(
            "A gap down reverses when elevated opening participation fills at least half "
            "the gap, revealing that regular-session buyers absorbed overnight supply."
        ),
        supported_timeframes=("30m",),
        builder=gap_down_absorption_reversal_observations,
        factor_type="directional_event",
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
        factor_type="cross_sectional",
        references=("https://arxiv.org/abs/1005.3535",),
    ),
    "cross_sectional_same_slot_reversal": FactorSpec(
        key="cross_sectional_same_slot_reversal",
        title="Cross-Sectional Same-Slot Reversal",
        hypothesis=(
            "Repeated same-slot returns measure temporary execution pressure, so prior "
            "same-slot leaders underperform laggards when that scheduled flow is absorbed."
        ),
        supported_timeframes=("30m",),
        builder=cross_sectional_same_slot_reversal_observations,
        factor_type="cross_sectional",
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
        factor_type="continuous",
        references=(),
    ),
    "vwap_execution_pressure_fade": FactorSpec(
        key="vwap_execution_pressure_fade",
        title="VWAP Execution Pressure Fade",
        hypothesis=(
            "Abnormal participation that displaces price from session VWAP exhausts, "
            "causing the next execution interval to move back toward VWAP."
        ),
        supported_timeframes=("30m",),
        builder=vwap_execution_pressure_fade_observations,
        factor_type="continuous",
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
        factor_type="directional_event",
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
        factor_type="directional_event",
        references=("https://nasdaqtrader.com/Trader.aspx?id=OpenClose",),
        requires_quotes=True,
        requires_auction_data=True,
    ),
}


# --------------------------------------------------------------------------
# Holding-horizon variants
# --------------------------------------------------------------------------
# A holding horizon is part of the hypothesis, not a knob to be tried after
# the fact.  Each horizon is therefore its own registered factor with its own
# key, so it lands in the ledger as its own trial and cannot be swapped in
# after a one-bar result disappoints.
GAP_HORIZON_BARS = (2, 4)
GAP_VARIANTS = {
    "gap_up_acceptance_continuation": ("up", "acceptance"),
    "gap_down_acceptance_continuation": ("down", "acceptance"),
    "gap_up_absorption_reversal": ("up", "absorption"),
    "gap_down_absorption_reversal": ("down", "absorption"),
}


def _gap_horizon_builder(
    *,
    factor_key: str,
    gap_direction: str,
    flow_state: str,
    horizon_bars: int,
) -> Callable[..., list[dict[str, Any]]]:
    def builder(
        candles_by_symbol: dict[str, list[dict[str, Any]]],
        *,
        timeframe: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        kwargs.pop("horizon_bars", None)
        return _overnight_gap_variant_observations(
            candles_by_symbol,
            timeframe=timeframe,
            factor_key=factor_key,
            gap_direction=gap_direction,
            flow_state=flow_state,
            horizon_bars=horizon_bars,
            **kwargs,
        )

    builder.__name__ = f"{factor_key}_observations"
    builder.__doc__ = (
        f"{gap_direction.title()} gap {flow_state}, held {horizon_bars} bars from the "
        "open of the bar after the decision to that bar's close."
    )
    return builder


for _base_key, (_direction, _state) in GAP_VARIANTS.items():
    for _horizon in GAP_HORIZON_BARS:
        _key = f"{_base_key}_{_horizon}bar"
        _base_spec = FACTOR_SPECS[_base_key]
        FACTOR_SPECS[_key] = FactorSpec(
            key=_key,
            title=f"{_base_spec.title} ({_horizon}-bar hold)",
            hypothesis=(
                f"{_base_spec.hypothesis} Measured over a {_horizon}-bar hold, exited at "
                "that bar's close and never carried past the session close."
            ),
            supported_timeframes=_base_spec.supported_timeframes,
            builder=_gap_horizon_builder(
                factor_key=_key,
                gap_direction=_direction,
                flow_state=_state,
                horizon_bars=_horizon,
            ),
            factor_type="directional_event",
            references=_base_spec.references,
        )
del _base_key, _direction, _state, _horizon, _key, _base_spec


def chronological_boundaries(
    session_dates: Sequence[date],
    *,
    embargo_sessions: int = 1,
) -> dict[str, Any]:
    """Chronological 50/30/20 split with an embargo between the splits.

    ``embargo_sessions`` sessions are dropped either side of each boundary and
    belong to no split.  Without it, a position opened near the end of
    discovery can still be open into the first validation session, so the two
    samples share an outcome and validation is no longer out of sample.  One
    session covers every horizon that is closed at the session close; a
    hypothesis that carries risk overnight needs a larger embargo.
    """
    ordered = sorted(set(session_dates))
    embargo = max(0, int(embargo_sessions))
    minimum = 10 + 2 * embargo
    if len(ordered) < minimum:
        raise ValueError(
            f"At least {minimum} distinct sessions are required for chronological "
            f"factor splits with a {embargo}-session embargo."
        )
    discovery_end_index = max(0, int(len(ordered) * 0.5) - 1)
    validation_start_index = discovery_end_index + 1 + embargo
    validation_end_index = max(validation_start_index, int(len(ordered) * 0.8) - 1)
    validation_end_index = min(validation_end_index, len(ordered) - 2 - embargo)
    confirmation_start_index = validation_end_index + 1 + embargo
    if not (
        discovery_end_index
        < validation_start_index
        <= validation_end_index
        < confirmation_start_index
        <= len(ordered) - 1
    ):
        raise ValueError(
            "The session history is too short to carve discovery, validation and "
            f"confirmation splits separated by a {embargo}-session embargo."
        )
    return {
        "discovery_start": ordered[0],
        "discovery_end": ordered[discovery_end_index],
        "validation_start": ordered[validation_start_index],
        "validation_end": ordered[validation_end_index],
        "confirmation_start": ordered[confirmation_start_index],
        "confirmation_end": ordered[-1],
        "distinct_sessions": len(ordered),
        "embargo_sessions": embargo,
        "embargoed_sessions": [
            str(item)
            for item in (
                ordered[discovery_end_index + 1 : validation_start_index]
                + ordered[validation_end_index + 1 : confirmation_start_index]
            )
        ],
    }


def interpret_factor_failure(
    result: dict[str, Any],
    *,
    metrics_key: str = "validation",
) -> dict[str, Any]:
    """Say what a failure means, so the same null is not re-tested forever.

    The distinction that matters is whether the sample could have detected the
    effect. An interpretable null retires the hypothesis; an underpowered null
    retires nothing, because nothing was measured.
    """
    if result.get("status") != "measured":
        return {
            "verdict": "not_measured",
            "action": "repair_data_or_gather_more",
            "detail": result.get("status"),
            "retire_hypothesis": False,
        }
    gate = result.get("evidence_gate") or {}
    if gate.get("passed"):
        return {
            "verdict": "survivor",
            "action": "proceed_to_locked_confirmation",
            "retire_hypothesis": False,
        }

    failed = set(gate.get("failed") or [])
    power = ((result.get("power_and_stability") or {}).get("power") or {})
    interpretable = bool(power.get("null_result_is_interpretable"))
    readiness = result.get("factor_research_readiness") or {}

    if not readiness.get("ready", True):
        return {
            "verdict": "data_not_ready",
            "action": "repair_data_before_confirmation",
            "failed_gates": sorted(failed),
            "limitations": list(readiness.get("limitations") or []),
            "retire_hypothesis": False,
        }
    if not interpretable:
        return {
            "verdict": "underpowered_null",
            "action": "gather_more_data",
            "detail": (
                "The sample could not have detected the claimed effect, so this "
                "reading is neither a rejection nor support."
            ),
            "sessions_required_for_80pct_power": power.get("sessions_required_for_80pct_power"),
            "observed_sessions": power.get("observed_sessions"),
            "failed_gates": sorted(failed),
            "retire_hypothesis": False,
        }
    if failed & {"clears_stressed_costs", "positive_net_return", "positive_event_conditioned_net_return", "executable_long_short_spread"}:
        return {
            "verdict": "fails_on_cost",
            "action": "retire_or_redesign_holding_horizon",
            "failed_gates": sorted(failed),
            "retire_hypothesis": True,
        }
    if failed & {"stable_subperiods", "stable_rank_performance"}:
        return {
            "verdict": "unstable",
            "action": "retire_or_declare_a_genuinely_new_regime_hypothesis",
            "failed_gates": sorted(failed),
            "retire_hypothesis": True,
        }
    return {
        "verdict": "interpretable_null",
        "action": "retire_hypothesis",
        "detail": (
            "The sample was powered to detect the claimed effect and did not "
            "find it."
        ),
        "failed_gates": sorted(failed),
        "retire_hypothesis": True,
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

    median_cost = (
        sorted(stressed_costs)[len(stressed_costs) // 2] if stressed_costs else None
    )
    spread_bps = _round(fmean(spreads) * 10_000) if spreads else None
    quarterly_ic = _quarterly_rank_ic(usable)

    return {
        "observations": len(usable),
        "distinct_sessions": evidence["distinct_sessions"],
        "distinct_symbols": evidence["distinct_symbols"],
        "rank_ic": _round(spearman(scores, targets)),
        "mean_cross_sectional_rank_ic": _round(fmean(group_ics)) if group_ics else None,
        "rank_ic_periods": len(group_ics),
        "quarterly_rank_ic": quarterly_ic,
        "rank_ic_stability": _rank_ic_stability(quarterly_ic),
        "top_minus_bottom_spread_bps": spread_bps,
        # A long-short spread is only executable if it survives paying the
        # round trip on both legs.
        "net_top_minus_bottom_spread_bps": (
            _round(spread_bps - 2 * median_cost)
            if spread_bps is not None and median_cost is not None
            else None
        ),
        "gross_directional_edge_bps": _round(fmean(directional) * 10_000) if directional else None,
        "net_stressed_edge_bps": (
            _round(fmean(net_directional) * 10_000) if net_directional else None
        ),
        "median_stressed_cost_bps": _round(median_cost),
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


def _quarterly_rank_ic(observations: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    """Rank IC within each calendar quarter, for continuous-factor stability."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        session = row["session_date"]
        grouped[f"{session.year}Q{(session.month - 1) // 3 + 1}"].append(row)
    output: dict[str, float | None] = {}
    for label in sorted(grouped):
        rows = grouped[label]
        if len(rows) < 20:
            continue
        output[label] = _round(
            spearman(
                [float(row["score"]) for row in rows],
                [float(row["target_return"]) for row in rows],
            )
        )
    return output


def _rank_ic_stability(quarterly: dict[str, float | None]) -> dict[str, Any]:
    values = [value for value in quarterly.values() if value is not None]
    positive = sum(1 for value in values if value > 0)
    return {
        "scored_quarters": len(values),
        "positive_quarters": positive,
        "positive_share": _round(positive / len(values)) if values else None,
        "stable": bool(values) and positive / len(values) >= 2 / 3,
    }


def factor_evidence_gate(
    spec: FactorSpec,
    metrics: dict[str, Any],
    *,
    cost_clearance: dict[str, Any],
    q_value: float | None,
    power_report: dict[str, Any] | None,
    dataset_ready: bool,
    cost_ready: bool,
) -> dict[str, Any]:
    """Apply the evidence rules that match how this factor claims to earn.

    A universal positive-rank-IC requirement is the wrong instrument for a
    directional event: its score is one signed magnitude conditioned on a rare
    state, and its rank correlation against a one-bar return is noise.  Each
    factor type is therefore measured on what it actually asserts, while the
    integrity requirements that apply to any claim -- clustering, selection
    adjustment, cost clearance, false-discovery control -- apply to all.
    """
    evidence = metrics.get("evidence_quality") or {}
    net_evidence = metrics.get("net_evidence_quality") or {}
    net_bootstrap = (net_evidence.get("block_bootstrap") or {}).get(
        "confidence_interval_95"
    )
    net_edge = metrics.get("net_stressed_edge_bps")
    clustered_t = metrics.get("day_clustered_t_statistic") or 0.0

    universal = {
        "dataset_research_ready": dataset_ready,
        "research_cost_available": cost_ready,
        "independent_clustered_evidence": bool(evidence.get("independent_evidence_ready")),
        "minimum_day_clustered_t": clustered_t >= MINIMUM_VALIDATION_T,
        "clears_stressed_costs": bool(cost_clearance.get("clears_stressed")),
        "false_discovery_rate_controlled": q_value is not None and float(q_value) <= 0.1,
        "selection_adjusted_gross": bool(evidence.get("selection_adjusted_signal")),
        "selection_adjusted_net": bool(net_evidence.get("selection_adjusted_signal")),
    }

    rank_ic = metrics.get("rank_ic")
    cross_sectional_ic = metrics.get("mean_cross_sectional_rank_ic")
    subperiods = ((power_report or {}).get("subperiods") or {})
    quarterly_stable = bool(
        (subperiods.get("quarterly_stability") or {}).get("stable")
    )

    if spec.factor_type == "continuous":
        specific = {
            "positive_information_coefficient": rank_ic is not None and rank_ic > 0,
            "stable_rank_performance": bool(
                (metrics.get("rank_ic_stability") or {}).get("stable")
            ),
            "positive_net_return": net_edge is not None and float(net_edge) > 0,
        }
    elif spec.factor_type == "directional_event":
        specific = {
            "positive_event_conditioned_net_return": (
                net_edge is not None and float(net_edge) > 0
            ),
            "positive_net_bootstrap_lower_bound": bool(
                net_bootstrap is not None and net_bootstrap[0] > 0
            ),
            "stable_subperiods": quarterly_stable,
        }
    elif spec.factor_type == "cross_sectional":
        net_spread = metrics.get("net_top_minus_bottom_spread_bps")
        specific = {
            "positive_cross_sectional_information_coefficient": (
                cross_sectional_ic is not None and cross_sectional_ic > 0
            ),
            "executable_long_short_spread": (
                net_spread is not None and float(net_spread) > 0
            ),
            "positive_net_return": net_edge is not None and float(net_edge) > 0,
        }
    else:  # pragma: no cover - FactorSpec validates the set
        raise ValueError(f"unknown factor_type {spec.factor_type!r}")

    gates = {**universal, **specific}
    return {
        "factor_type": spec.factor_type,
        "universal_gates": universal,
        "factor_type_gates": specific,
        "gates": gates,
        "passed": all(gates.values()),
        "failed": [label for label, ok in gates.items() if not ok],
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
    effective_trials: int | None = None,
    trial_ledger: dict[str, Any] | None = None,
    sector_by_symbol: dict[str, str] | None = None,
    certification: dict[str, Any] | None = None,
    embargo_sessions: int = 1,
    observations_by_factor: dict[str, list[dict[str, Any]]] | None = None,
    session_dates: Sequence[date] | None = None,
    symbols: Sequence[str] | None = None,
    data_readiness: dict[str, Any] | None = None,
    calendar_audit: dict[str, Any] | None = None,
    benchmark_context: dict[date, dict[str, Any]] | None = None,
    required_event_counts: dict[str, int] | None = None,
    required_sessions: int | None = None,
) -> dict[str, Any]:
    # A universe-scale snapshot does not fit in memory as candle dicts, so the
    # caller may stream it: build each factor's observations symbol by symbol
    # and pass them in with the aggregate facts computed alongside.  Every
    # per-symbol factor gives an identical answer either way, because its
    # observations never depend on another symbol's bars.
    streamed = observations_by_factor is not None
    all_dates = list(session_dates) if session_dates is not None else [
        _session_date(row)
        for rows in candles_by_symbol.values()
        for row in rows
    ]
    boundaries = chronological_boundaries(all_dates, embargo_sessions=embargo_sessions)
    if data_readiness is None:
        data_readiness = dataset_research_readiness(
            candles_by_symbol,
            microstructure_by_symbol=microstructure_by_symbol,
        )
    if calendar_audit is None:
        calendar_audit = extended_hours_audit(candles_by_symbol, timeframe=timeframe)
    universe = list(symbols) if symbols is not None else list(candles_by_symbol)
    cost_readiness = cost_model_readiness(cost_model, symbols=universe)
    institutional_readiness = (
        institutional_data_readiness or _default_institutional_readiness()
    )
    # The correction is charged against every test ever run against this data,
    # not just the ones in today's argument list.  The caller supplies that
    # count from the append-only ledger; falling back to the run size is a
    # floor, never a substitute.
    effective_trials = max(1, int(effective_trials or len(factor_keys)))
    if benchmark_context is None:
        benchmark_context = benchmark_session_context(
            candles_by_symbol, timeframe=timeframe
        )
    factor_results: dict[str, Any] = {}
    validation_p: dict[str, float | None] = {}
    for key in factor_keys:
        spec = FACTOR_SPECS[key]
        if streamed and spec.factor_type == "cross_sectional":
            raise ValueError(
                f"{key} is cross-sectional: its score compares symbols at the same "
                "instant, so it cannot be built one symbol at a time. Run it with "
                "the full candle set."
            )
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
        observations = (
            list(observations_by_factor.get(key) or [])
            if streamed
            else spec.builder(
                candles_by_symbol,
                timeframe=timeframe,
                microstructure_by_symbol=microstructure_by_symbol,
                auction_by_symbol=auction_by_symbol,
            )
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
        power_report = power_and_stability_report(
            validation,
            evidence_quality=validation_metrics["evidence_quality"],
            net_evidence_quality=validation_metrics["net_evidence_quality"],
            benchmark_context=benchmark_context,
            sector_by_symbol=sector_by_symbol,
            discovery_metrics=discovery_metrics,
            validation_metrics=validation_metrics,
            trials_recorded=(trial_ledger or {}).get("effective_trials"),
            required_event_count=(required_event_counts or {}).get(key),
            required_sessions=required_sessions,
        )
        factor_results[key] = {
            "status": "measured" if validation_metrics["measurable"] else "insufficient_evidence",
            "spec": spec.frozen(),
            "discovery": discovery_metrics,
            "validation": validation_metrics,
            "cost_clearance": cost_clearance,
            "power_and_stability": power_report,
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
    evidence_survivors: list[str] = []
    selected: list[str] = []
    survivors_blocked_by_readiness: dict[str, list[str]] = {}
    for key, result in factor_results.items():
        if result.get("status") != "measured":
            continue
        result["validation"]["false_discovery_rate_q_value"] = q_values.get(key)
        gate = factor_evidence_gate(
            FACTOR_SPECS[key],
            result["validation"],
            cost_clearance=result["cost_clearance"],
            q_value=q_values.get(key),
            power_report=result["power_and_stability"],
            dataset_ready=bool(data_readiness["candle_research_ready"]),
            cost_ready=bool(cost_readiness["research_cost_available"]),
        )
        result["evidence_gate"] = gate
        result["evidence_gate_passed"] = gate["passed"]
        result["interpretation"] = interpret_factor_failure(result)
        if gate["passed"]:
            evidence_survivors.append(key)
            if result["factor_research_readiness"]["ready"]:
                selected.append(key)
            else:
                survivors_blocked_by_readiness[key] = list(
                    result["factor_research_readiness"]["limitations"]
                )
    return {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "mode": "discovery",
        "timeframe": timeframe,
        "split_boundaries": {key: str(value) for key, value in boundaries.items()},
        "cost_model": cost_model,
        "data_readiness": data_readiness,
        "session_calendar_audit": calendar_audit,
        "institutional_data_readiness": institutional_readiness,
        "cost_readiness": cost_readiness,
        "effective_trials": effective_trials,
        "trial_ledger": trial_ledger,
        "instrument_certification": _certification_summary(certification),
        "factors": factor_results,
        "evidence_survivors": evidence_survivors,
        "selected_for_forward_confirmation": selected,
        "survivors_blocked_by_readiness": survivors_blocked_by_readiness,
        "interpretation": {
            key: (result.get("interpretation") or {}).get("verdict")
            for key, result in factor_results.items()
        },
        "hypotheses_to_retire": sorted(
            key
            for key, result in factor_results.items()
            if (result.get("interpretation") or {}).get("retire_hypothesis")
        ),
        "confirmation_data_accessed": False,
    }


def _certification_summary(certification: dict[str, Any] | None) -> dict[str, Any]:
    """Carry the instrument verdict alongside every result it produced."""
    if not certification:
        return {
            "certified": False,
            "status": "not_supplied",
            "detail": (
                "No instrument certification accompanied this run; its results "
                "cannot distinguish absence of alpha from a broken measurement."
            ),
        }
    return {
        "certified": bool(certification.get("certified")),
        "status": "supplied",
        "controls_version": certification.get("controls_version"),
        "checks": certification.get("checks"),
        "certification_id": certification.get("certification_id"),
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
    effective_trials: int | None = None,
    trial_ledger: dict[str, Any] | None = None,
    sector_by_symbol: dict[str, str] | None = None,
    certification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data_readiness = dataset_research_readiness(
        candles_by_symbol,
        microstructure_by_symbol=microstructure_by_symbol,
    )
    calendar_audit = extended_hours_audit(candles_by_symbol, timeframe=timeframe)
    cost_readiness = cost_model_readiness(cost_model, symbols=list(candles_by_symbol))
    institutional_readiness = (
        institutional_data_readiness or _default_institutional_readiness()
    )
    effective_trials = max(1, int(effective_trials or len(factor_keys)))
    benchmark_context = benchmark_session_context(
        candles_by_symbol, timeframe=timeframe
    )
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
            "spec": spec.frozen(),
            "confirmation": metrics,
            "cost_clearance": _cost_clearance(metrics, cost_model),
            "power_and_stability": power_and_stability_report(
                observations,
                evidence_quality=metrics["evidence_quality"],
                net_evidence_quality=metrics["net_evidence_quality"],
                benchmark_context=benchmark_context,
                sector_by_symbol=sector_by_symbol,
                validation_metrics=metrics,
                trials_recorded=(trial_ledger or {}).get("effective_trials"),
            ),
            "factor_research_readiness": readiness,
        }
    q_values = benjamini_hochberg(p_values)
    passed: list[str] = []
    for key, result in factors.items():
        if result.get("status") != "measured":
            continue
        result["confirmation"]["false_discovery_rate_q_value"] = q_values.get(key)
        gate = factor_evidence_gate(
            FACTOR_SPECS[key],
            result["confirmation"],
            cost_clearance=result["cost_clearance"],
            q_value=q_values.get(key),
            power_report=result["power_and_stability"],
            dataset_ready=bool(data_readiness["candle_research_ready"]),
            cost_ready=bool(cost_readiness["research_cost_available"]),
        )
        result["evidence_gate"] = gate
        result["evidence_gate_passed"] = gate["passed"]
        result["interpretation"] = interpret_factor_failure(
            result, metrics_key="confirmation"
        )
        if gate["passed"] and result["factor_research_readiness"]["ready"]:
            passed.append(key)
    return {
        "protocol_version": FACTOR_DIAGNOSTICS_VERSION,
        "mode": "confirmation",
        "timeframe": timeframe,
        "cost_model": cost_model,
        "data_readiness": data_readiness,
        "session_calendar_audit": calendar_audit,
        "institutional_data_readiness": institutional_readiness,
        "cost_readiness": cost_readiness,
        "effective_trials": effective_trials,
        "trial_ledger": trial_ledger,
        "instrument_certification": _certification_summary(certification),
        "factors": factors,
        "passed_locked_confirmation": passed,
        # Confirmation is one shot: anything measured here that did not pass is
        # a permanently retired version, not a candidate for another attempt.
        "failed_locked_confirmation": sorted(
            key
            for key, result in factors.items()
            if result.get("status") == "measured" and not result.get("evidence_gate_passed")
        ),
        "interpretation": {
            key: (result.get("interpretation") or {}).get("verdict")
            for key, result in factors.items()
        },
    }


def load_dataset_candles(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    symbols: Sequence[str] | None = None,
    max_symbols: int = 200,
    include_benchmarks: bool = True,
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
    # A batched caller must switch this off: injecting them into every batch
    # would count their observations once per batch.
    if include_benchmarks:
        selected = list(
            dict.fromkeys([*selected, *[item for item in ("SPY", "QQQ") if item in available]])
        )
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


def persist_certification(
    conn: psycopg.Connection,
    *,
    dataset_id: int | None,
    timeframe: str,
    factor_keys: Sequence[str],
    controls: dict[str, Any],
    leakage: dict[str, Any],
    calendar: dict[str, Any],
) -> dict[str, Any]:
    """Store the instrument evidence a run's results must be read against."""
    controls_passed = bool(controls.get("certified"))
    leakage_passed = bool(leakage.get("passed"))
    # A snapshot whose sessions are mostly incomplete cannot support a
    # closing-half-hour claim, and two feeds under two source labels put two
    # prices on the same bar, so both are part of calendar integrity.
    calendar_passed = bool(
        (calendar.get("complete_session_share") or 0) >= 0.95
        and calendar.get("duplicate_symbol_timestamp_rows", 0) == 0
        and calendar.get("timestamps_normalized", True)
    )
    row = conn.execute(
        """
        INSERT INTO intraday_research_certifications(
            dataset_id, timeframe, certified, controls_passed, leakage_passed,
            calendar_passed, controls, leakage, calendar, published_replication,
            factor_keys, protocol_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
        """,
        (
            dataset_id,
            timeframe,
            controls_passed and leakage_passed and calendar_passed,
            controls_passed,
            leakage_passed,
            calendar_passed,
            Jsonb(_jsonable_factor_payload(controls)),
            Jsonb(_jsonable_factor_payload(leakage)),
            Jsonb(_jsonable_factor_payload(calendar)),
            Jsonb(_jsonable_factor_payload(controls.get("published_replication") or {})),
            Jsonb(_jsonable_factor_payload(list(factor_keys))),
            FACTOR_DIAGNOSTICS_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {
        "certification_id": int(row["id"]),
        "created_at": row["created_at"],
        "certified": controls_passed and leakage_passed and calendar_passed,
        "controls_passed": controls_passed,
        "leakage_passed": leakage_passed,
        "calendar_passed": calendar_passed,
        "controls_version": controls.get("controls_version"),
        "checks": {
            **(controls.get("checks") or {}),
            "no_factor_reads_its_future": leakage_passed,
            "snapshot_sessions_complete": calendar_passed,
        },
    }


def load_certification(
    conn: psycopg.Connection,
    *,
    certification_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM intraday_research_certifications WHERE id = %s",
        (certification_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No instrument certification id={certification_id}.")
    item = dict(row)
    return {
        "certification_id": int(item["id"]),
        "certified": bool(item["certified"]),
        "controls_passed": bool(item["controls_passed"]),
        "leakage_passed": bool(item["leakage_passed"]),
        "calendar_passed": bool(item["calendar_passed"]),
        "controls_version": (item.get("controls") or {}).get("controls_version"),
        "checks": (item.get("controls") or {}).get("checks"),
        "created_at": item["created_at"],
        "dataset_id": item["dataset_id"],
    }


def sector_map(conn: psycopg.Connection, symbols: Sequence[str]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT symbol, sector FROM symbols WHERE symbol = ANY(%s) AND sector IS NOT NULL",
        ([str(symbol).upper() for symbol in symbols],),
    ).fetchall()
    return {str(row["symbol"]).upper(): str(row["sector"]) for row in rows}


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
    certification_id: int | None = None,
    declaration_id: int | None = None,
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
                Jsonb(_jsonable_factor_payload(FACTOR_SPECS[key].frozen())),
                Jsonb(_jsonable_factor_payload(list(symbols))),
                Jsonb(_jsonable_factor_payload(result.get("split_boundaries") or {})),
                Jsonb(_jsonable_factor_payload(result.get("cost_model") or {})),
                Jsonb(_jsonable_factor_payload(factor_result)),
                FACTOR_DIAGNOSTICS_VERSION,
            ),
        )
    row = conn.execute(
        """
        INSERT INTO intraday_factor_diagnostic_runs(
            mode, status, dataset_id, source_run_id, certification_id,
            declaration_id, timeframe, factor_keys,
            symbols, split_boundaries, cost_model, results, frozen_spec_hash,
            protocol_version, completed_at
        )
        VALUES (%s, 'completed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING id
        """,
        (
            mode,
            dataset_id,
            source_run_id,
            certification_id,
            declaration_id,
            timeframe,
            Jsonb(_jsonable_factor_payload(list(factor_keys))),
            Jsonb(_jsonable_factor_payload(list(symbols))),
            Jsonb(_jsonable_factor_payload(result.get("split_boundaries") or {})),
            Jsonb(_jsonable_factor_payload(result["cost_model"])),
            Jsonb(_jsonable_factor_payload(result)),
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
