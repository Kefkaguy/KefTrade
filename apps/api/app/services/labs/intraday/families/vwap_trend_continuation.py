"""VWAP Trend Continuation v1 (Phase 12.3).

Hypothesis: the opposite of VWAP Reversion v1 -- when price is extended
from session VWAP *and* recent price action has been moving in the same
direction as that extension (a real trend, not a spike), the move
continues rather than reverts. VWAP Reversion v1's own code
(`labs/intraday/strategy.py`) is not imported or modified by this module;
this reuses the same `distance_from_session_vwap` field but requires an
additional momentum-confirmation check (via `recent_candles`) that VWAP
Reversion does not, and inverts the stop/target direction (target further
from VWAP, not back toward it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision

VWAP_TREND_CONTINUATION_ARCHITECTURE = "vwap_trend_continuation_v1"

VALID_DIRECTIONS = ("long", "short", "both")

DEFAULT_VWAP_TREND_CONTINUATION_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": VWAP_TREND_CONTINUATION_ARCHITECTURE,
    "entry_deviation_threshold": Decimal("0.006"),
    "momentum_confirmation_bars": 4,
    "minimum_session_relative_volume": Decimal("1.2"),
    "stop_multiple": Decimal("1.5"),
    "reward_risk_multiple": Decimal("1.2"),
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
    "risk_reward": Decimal("1.2"),
}


@dataclass
class VwapTrendContinuationState:
    current_session: date | None = None
    entries_taken: int = 0
    long_trend_taken: bool = False
    short_trend_taken: bool = False


class VwapTrendContinuationStrategy:
    """Satisfies `StrategyProtocol`. Trend continuation away from session VWAP."""

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid VWAP Trend Continuation direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = VwapTrendContinuationState()

    def reset(self) -> None:
        self.state = VwapTrendContinuationState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = VwapTrendContinuationState(current_session=session_date)

        session_vwap = feature.get("session_vwap")
        if session_vwap is None:
            return _avoid("Session VWAP unavailable for this bar.")

        momentum_bars = int(params.get("momentum_confirmation_bars") or 4)
        if len(recent_candles) <= momentum_bars:
            return _avoid("Insufficient recent-candle history for momentum confirmation.")

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

        vwap = Decimal(session_vwap)
        close = Decimal(candle["close"])
        deviation = close - vwap
        deviation_distance = abs(deviation)
        threshold = Decimal(str(params.get("entry_deviation_threshold", 0))) * vwap
        if threshold <= 0 or deviation_distance < threshold:
            return _avoid("Deviation from VWAP below the configured threshold.")

        # Momentum confirmation: price must have been moving in the SAME
        # direction as the current VWAP deviation over the recent window --
        # the property that distinguishes "trending away" from "extended
        # but stalling", which is exactly what VWAP Reversion v1 trades.
        momentum_reference = Decimal(recent_candles[-1 - momentum_bars]["close"])
        momentum = close - momentum_reference
        if (deviation > 0) != (momentum > 0):
            return _avoid("Recent price momentum does not confirm the VWAP extension direction.")

        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_trend_direction", False))
        stop_distance = deviation_distance * Decimal(str(params.get("stop_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.2)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_trend_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_trend_taken)

        # Extended above VWAP with confirming upward momentum -> long continuation.
        if long_eligible and deviation >= threshold:
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
                [f"Long VWAP trend continuation: close {close} is {deviation_distance} above VWAP {vwap}, momentum confirmed."],
                direction="long",
            )

        # Extended below VWAP with confirming downward momentum -> short continuation.
        if short_eligible and deviation <= -threshold:
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
                [f"Short VWAP trend continuation: close {close} is {deviation_distance} below VWAP {vwap}, momentum confirmed."],
                direction="short",
            )

        return _avoid("Deviation direction does not match an eligible side.")


VWAP_TREND_CONTINUATION_BLOCKS: dict[str, str] = {
    "trend": "vwap_trend_continuation_deviation",
    "momentum": "vwap_trend_continuation_momentum_confirmation",
    "volatility": "vwap_trend_continuation_deviation_distance",
    "volume": "vwap_trend_continuation_relative_volume",
    "entry": "vwap_trend_continuation",
    "exit": "vwap_trend_continuation_session_close_forced",
}

VWAP_TREND_CONTINUATION_THRESHOLDS = ("0.006", "0.010")
VWAP_TREND_CONTINUATION_DIRECTIONS = ("long", "short")


def generate_vwap_trend_continuation_candidates(*, max_candidates: int = 8) -> list[Any]:
    from itertools import product
    from hashlib import sha256

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    candidates = []
    seen: set[str] = set()
    for threshold, direction in product(VWAP_TREND_CONTINUATION_THRESHOLDS, VWAP_TREND_CONTINUATION_DIRECTIONS):
        params = {
            **DEFAULT_VWAP_TREND_CONTINUATION_PARAMETERS,
            "entry_deviation_threshold": threshold,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(VWAP_TREND_CONTINUATION_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"vwaptc_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_vwap_trend_continuation_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(VWAP_TREND_CONTINUATION_BLOCKS),
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
