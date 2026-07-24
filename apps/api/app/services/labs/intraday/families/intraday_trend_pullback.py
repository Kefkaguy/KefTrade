"""Intraday Trend Pullback v1 (Phase 12.3).

Hypothesis: within an established intraday trend (price meaningfully above
or below session VWAP), a short-term pullback that does not break the trend
context is a continuation entry, not a reversal. Distinct from VWAP
Reversion (which fades extension from VWAP) and Session Momentum (which
requires the move itself to already be extended) -- this requires an
established trend context *and* a recent retracement *within* it, using
`distance_from_session_vwap` for trend context and the raw `recent_candles`
window for the pullback measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision

INTRADAY_TREND_PULLBACK_ARCHITECTURE = "intraday_trend_pullback_v1"

VALID_DIRECTIONS = ("long", "short", "both")

DEFAULT_INTRADAY_TREND_PULLBACK_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": INTRADAY_TREND_PULLBACK_ARCHITECTURE,
    "trend_min_distance_from_vwap": Decimal("0.004"),
    "pullback_lookback_bars": 5,
    "pullback_depth_threshold": Decimal("0.002"),
    "minimum_session_relative_volume": Decimal("1.0"),
    "stop_multiple": Decimal("1.5"),
    "reward_risk_multiple": Decimal("1.2"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_pullback_direction": False,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.2"),
}


@dataclass
class IntradayTrendPullbackState:
    current_session: date | None = None
    entries_taken: int = 0
    long_pullback_taken: bool = False
    short_pullback_taken: bool = False


class IntradayTrendPullbackStrategy:
    """Satisfies `StrategyProtocol`. Buys dips in an uptrend, sells rallies in a downtrend."""

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid Intraday Trend Pullback direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = IntradayTrendPullbackState()

    def reset(self) -> None:
        self.state = IntradayTrendPullbackState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = IntradayTrendPullbackState(current_session=session_date)

        distance = feature.get("distance_from_session_vwap")
        if distance is None:
            return _avoid("VWAP distance unavailable for this bar.")

        lookback = int(params.get("pullback_lookback_bars") or 5)
        if len(recent_candles) <= lookback:
            return _avoid("Insufficient recent-candle history for the pullback lookback.")

        if self.state.entries_taken >= int(params.get("maximum_entries_per_session", 1)):
            return _avoid("Maximum entries for this session already reached.")

        required_minutes_to_close = max(
            minimum_entry_lookahead_minutes(self.timeframe, entry_offset_bars=1, minimum_holding_bars=1),
            int(params.get("minimum_minutes_before_close_for_entry") or 0),
        )
        minutes_to_close = feature.get("minutes_to_close")
        if minutes_to_close is None or minutes_to_close < required_minutes_to_close:
            return _avoid("Too close to session close for a safe next-bar-open entry.")

        minimum_relative_volume = params.get("minimum_session_relative_volume")
        if minimum_relative_volume is not None:
            relative_volume = feature.get("session_relative_volume")
            if relative_volume is None or Decimal(relative_volume) < Decimal(str(minimum_relative_volume)):
                return _avoid("Relative-volume confirmation failed.")

        trend_distance = Decimal(distance)
        trend_threshold = Decimal(str(params.get("trend_min_distance_from_vwap", 0)))
        close = Decimal(candle["close"])
        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_pullback_direction", False))
        pullback_threshold = Decimal(str(params.get("pullback_depth_threshold", 0)))
        # Excludes the current bar -- the lookback window is strictly prior bars.
        lookback_window = recent_candles[-1 - lookback : -1]
        stop_distance_multiple = Decimal(str(params.get("stop_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.2)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_pullback_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_pullback_taken)

        if long_eligible and trend_distance >= trend_threshold:
            recent_high = max(Decimal(row["high"]) for row in lookback_window)
            if recent_high > 0:
                pullback_depth = (recent_high - close) / recent_high
                if pullback_depth >= pullback_threshold:
                    self.state.entries_taken += 1
                    self.state.long_pullback_taken = True
                    stop_distance = pullback_depth * close * stop_distance_multiple
                    stop_loss = close - stop_distance
                    take_profit = close + (stop_distance * reward_risk)
                    return StrategyDecision(
                        "setup",
                        (close, close),
                        stop_loss,
                        take_profit,
                        reward_risk,
                        [f"Long trend pullback: {pullback_depth * 100:.2f}% pullback within an uptrend ({trend_distance * 100:.2f}% above VWAP)."],
                        direction="long",
                    )

        if short_eligible and trend_distance <= -trend_threshold:
            recent_low = min(Decimal(row["low"]) for row in lookback_window)
            if recent_low > 0:
                rally_depth = (close - recent_low) / recent_low
                if rally_depth >= pullback_threshold:
                    self.state.entries_taken += 1
                    self.state.short_pullback_taken = True
                    stop_distance = rally_depth * close * stop_distance_multiple
                    stop_loss = close + stop_distance
                    take_profit = close - (stop_distance * reward_risk)
                    return StrategyDecision(
                        "setup",
                        (close, close),
                        stop_loss,
                        take_profit,
                        reward_risk,
                        [f"Short trend pullback: {rally_depth * 100:.2f}% rally within a downtrend ({trend_distance * 100:.2f}% below VWAP)."],
                        direction="short",
                    )

        return _avoid("No qualifying trend-context pullback.")


INTRADAY_TREND_PULLBACK_BLOCKS: dict[str, str] = {
    "trend": "intraday_pullback_vwap_trend_context",
    "momentum": "intraday_pullback_relative_volume_confirmation",
    "volatility": "intraday_pullback_depth",
    "volume": "intraday_pullback_relative_volume",
    "entry": "intraday_trend_pullback",
    "exit": "intraday_pullback_session_close_forced",
}

INTRADAY_TREND_PULLBACK_DEPTHS = ("0.002", "0.004")
INTRADAY_TREND_PULLBACK_DIRECTIONS = ("long", "short")


def generate_intraday_trend_pullback_candidates(*, max_candidates: int = 8) -> list[Any]:
    from itertools import product
    from hashlib import sha256

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    candidates = []
    seen: set[str] = set()
    for depth, direction in product(INTRADAY_TREND_PULLBACK_DEPTHS, INTRADAY_TREND_PULLBACK_DIRECTIONS):
        params = {
            **DEFAULT_INTRADAY_TREND_PULLBACK_PARAMETERS,
            "pullback_depth_threshold": depth,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(INTRADAY_TREND_PULLBACK_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"itpb_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_intraday_trend_pullback_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(INTRADAY_TREND_PULLBACK_BLOCKS),
                parameters=params,
                complexity=4,
                canonical_key=canonical_key,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def _avoid(reason: str) -> StrategyDecision:
    return StrategyDecision("avoid", None, None, None, None, [reason])
