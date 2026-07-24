"""EMA Trend Continuation v1 (Phase 12.3).

Hypothesis: a fast/slow EMA crossover on the intraday bar series, confirmed
by price holding on the correct side of the fast EMA, identifies a
continuing intraday trend. `intraday_features` has no EMA field for 15m/30m
bars, so this reuses the existing swing EMA helper
(`app.services.strategy.calculate_ema_from_candles`) against the
already-provided, already-no-lookahead `recent_candles` window -- no new
indicator computation added to the feature layer, no simulator change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision, calculate_ema_from_candles

EMA_TREND_CONTINUATION_ARCHITECTURE = "ema_trend_continuation_v1"

VALID_DIRECTIONS = ("long", "short", "both")

DEFAULT_EMA_TREND_CONTINUATION_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": EMA_TREND_CONTINUATION_ARCHITECTURE,
    "ema_fast_period": 9,
    "ema_slow_period": 21,
    "minimum_session_relative_volume": Decimal("1.0"),
    "stop_multiple": Decimal("1.5"),
    "reward_risk_multiple": Decimal("1.5"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_trend_direction": False,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.5"),
}


@dataclass
class EmaTrendContinuationState:
    current_session: date | None = None
    entries_taken: int = 0
    long_trend_taken: bool = False
    short_trend_taken: bool = False


class EmaTrendContinuationStrategy:
    """Satisfies `StrategyProtocol`. EMA crossover trend continuation."""

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid EMA Trend Continuation direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = EmaTrendContinuationState()

    def reset(self) -> None:
        self.state = EmaTrendContinuationState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = EmaTrendContinuationState(current_session=session_date)

        fast_period = int(params.get("ema_fast_period") or 9)
        slow_period = int(params.get("ema_slow_period") or 21)
        ema_fast = calculate_ema_from_candles(recent_candles, fast_period)
        ema_slow = calculate_ema_from_candles(recent_candles, slow_period)
        if ema_fast is None or ema_slow is None:
            return _avoid("Insufficient recent-candle history for the EMA periods.")

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

        close = Decimal(candle["close"])
        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_trend_direction", False))
        # Distance between the fast and slow EMA is the volatility/trend-
        # strength unit here -- no dedicated ATR field exists for 15m/30m,
        # same situation every other family in this library documents, and
        # a different, family-specific unit than ORB's range-span or VWAP
        # Reversion/Continuation's VWAP-distance.
        trend_strength = abs(ema_fast - ema_slow)
        stop_distance = trend_strength * Decimal(str(params.get("stop_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.5)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_trend_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_trend_taken)

        if long_eligible and ema_fast > ema_slow and close > ema_fast and stop_distance > 0:
            self.state.entries_taken += 1
            self.state.long_trend_taken = True
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Long EMA trend continuation: EMA{fast_period} {ema_fast} > EMA{slow_period} {ema_slow}, price confirming above the fast EMA."],
                direction="long",
            )

        if short_eligible and ema_fast < ema_slow and close < ema_fast and stop_distance > 0:
            self.state.entries_taken += 1
            self.state.short_trend_taken = True
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Short EMA trend continuation: EMA{fast_period} {ema_fast} < EMA{slow_period} {ema_slow}, price confirming below the fast EMA."],
                direction="short",
            )

        return _avoid("No qualifying EMA trend alignment.")


EMA_TREND_CONTINUATION_BLOCKS: dict[str, str] = {
    "trend": "ema_trend_continuation_crossover",
    "momentum": "ema_trend_continuation_relative_volume_confirmation",
    "volatility": "ema_trend_continuation_ema_spread",
    "volume": "ema_trend_continuation_relative_volume",
    "entry": "ema_trend_continuation",
    "exit": "ema_trend_continuation_session_close_forced",
}

EMA_TREND_CONTINUATION_PERIOD_SETS = (("9", "21"), ("12", "26"))
EMA_TREND_CONTINUATION_DIRECTIONS = ("long", "short")


def generate_ema_trend_continuation_candidates(*, max_candidates: int = 8) -> list[Any]:
    from itertools import product
    from hashlib import sha256

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    candidates = []
    seen: set[str] = set()
    for (fast, slow), direction in product(EMA_TREND_CONTINUATION_PERIOD_SETS, EMA_TREND_CONTINUATION_DIRECTIONS):
        params = {
            **DEFAULT_EMA_TREND_CONTINUATION_PARAMETERS,
            "ema_fast_period": int(fast),
            "ema_slow_period": int(slow),
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(EMA_TREND_CONTINUATION_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"emac_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_ema_trend_continuation_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(EMA_TREND_CONTINUATION_BLOCKS),
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
