"""Session Momentum v1 (Phase 12.3).

Hypothesis: a strong recent-bar price move, confirmed by relative volume,
tends to continue rather than mean-revert. Uses only the raw `recent_candles`
window every strategy already receives (no new indicator field, no
simulator/dataset change) to measure the lookback return, plus
`session_relative_volume` for confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision

SESSION_MOMENTUM_ARCHITECTURE = "session_momentum_v1"

VALID_DIRECTIONS = ("long", "short", "both")

DEFAULT_SESSION_MOMENTUM_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": SESSION_MOMENTUM_ARCHITECTURE,
    "momentum_lookback_bars": 6,
    "momentum_threshold": Decimal("0.004"),
    "minimum_session_relative_volume": Decimal("1.2"),
    "stop_multiple": Decimal("1.5"),
    "reward_risk_multiple": Decimal("1.2"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_momentum_direction": False,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.2"),
}


@dataclass
class SessionMomentumState:
    current_session: date | None = None
    entries_taken: int = 0
    long_momentum_taken: bool = False
    short_momentum_taken: bool = False


class SessionMomentumStrategy:
    """Satisfies `StrategyProtocol`. Trend continuation on recent momentum."""

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid Session Momentum direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = SessionMomentumState()

    def reset(self) -> None:
        self.state = SessionMomentumState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = SessionMomentumState(current_session=session_date)

        lookback = int(params.get("momentum_lookback_bars") or 6)
        # recent_candles includes the current bar as its last element (see
        # backtester.py's recent_candles slicing) -- never a future bar.
        if len(recent_candles) <= lookback:
            return _avoid("Insufficient recent-candle history for the momentum lookback.")

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
        lookback_close = Decimal(recent_candles[-1 - lookback]["close"])
        if lookback_close <= 0:
            return _avoid("Invalid lookback reference price.")
        momentum_return = (close - lookback_close) / lookback_close
        momentum_magnitude = abs(momentum_return)
        threshold = Decimal(str(params.get("momentum_threshold", 0)))
        if momentum_magnitude < threshold:
            return _avoid("Momentum below the configured threshold.")

        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_momentum_direction", False))
        stop_distance = momentum_magnitude * close * Decimal(str(params.get("stop_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.2)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_momentum_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_momentum_taken)

        if long_eligible and momentum_return > threshold:
            self.state.entries_taken += 1
            self.state.long_momentum_taken = True
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Long session momentum: {momentum_return * 100:.2f}% over {lookback} bars."],
                direction="long",
            )

        if short_eligible and momentum_return < -threshold:
            self.state.entries_taken += 1
            self.state.short_momentum_taken = True
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Short session momentum: {momentum_return * 100:.2f}% over {lookback} bars."],
                direction="short",
            )

        return _avoid("Momentum direction does not match an eligible side.")


SESSION_MOMENTUM_BLOCKS: dict[str, str] = {
    "trend": "session_momentum_recent_return",
    "momentum": "session_momentum_relative_volume_confirmation",
    "volatility": "session_momentum_return_magnitude",
    "volume": "session_momentum_relative_volume",
    "entry": "session_momentum",
    "exit": "session_momentum_session_close_forced",
}

SESSION_MOMENTUM_THRESHOLDS = ("0.004", "0.008")
SESSION_MOMENTUM_DIRECTIONS = ("long", "short")


def generate_session_momentum_candidates(*, max_candidates: int = 8) -> list[Any]:
    from itertools import product
    from hashlib import sha256

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    candidates = []
    seen: set[str] = set()
    for threshold, direction in product(SESSION_MOMENTUM_THRESHOLDS, SESSION_MOMENTUM_DIRECTIONS):
        params = {
            **DEFAULT_SESSION_MOMENTUM_PARAMETERS,
            "momentum_threshold": threshold,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(SESSION_MOMENTUM_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"sessmom_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_session_momentum_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(SESSION_MOMENTUM_BLOCKS),
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
