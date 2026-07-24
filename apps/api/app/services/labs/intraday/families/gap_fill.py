"""Gap Fill v1 (Phase 12.3).

Hypothesis: an overnight gap tends to partially retrace toward the prior
session's close early in the new session. Direction is determined by the
gap itself (gap up -> fade short, gap down -> fade long), not chosen by the
strategy; the `direction` parameter instead restricts *which side* of gaps
this instance trades (matches the convention every other Intraday Lab
family uses for its `direction` parameter).

Uses only `gap_percent` (already a per-session constant broadcast to every
bar by `compute_intraday_features`) plus the same generic entry-cutoff and
session-close machinery every other family uses. No simulator or dataset
changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision

GAP_FILL_ARCHITECTURE = "gap_fill_v1"

VALID_DIRECTIONS = ("long", "short", "both")

# Gap-fill trades are a session-open phenomenon, not an all-day one: a gap
# still "open" three hours into the session isn't the same trade thesis.
# entry_window_minutes bounds how early in the session an entry may occur.
DEFAULT_GAP_FILL_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": GAP_FILL_ARCHITECTURE,
    "entry_window_minutes": 60,
    "minimum_gap_threshold": Decimal("0.005"),
    "minimum_session_relative_volume": Decimal("1.0"),
    "stop_multiple": Decimal("1.5"),
    "reward_risk_multiple": Decimal("1.0"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_gap_direction": False,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.0"),
}


@dataclass
class GapFillState:
    current_session: date | None = None
    entries_taken: int = 0
    long_fill_taken: bool = False
    short_fill_taken: bool = False


class GapFillStrategy:
    """Satisfies `StrategyProtocol`. Fades the session's opening gap."""

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid Gap Fill direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = GapFillState()

    def reset(self) -> None:
        self.state = GapFillState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = GapFillState(current_session=session_date)

        minutes_from_open = feature.get("minutes_from_open")
        entry_window_minutes = int(params.get("entry_window_minutes") or 60)
        if minutes_from_open is None or minutes_from_open > entry_window_minutes:
            return _avoid("Outside the gap-fill entry window.")

        gap_percent = feature.get("gap_percent")
        if gap_percent is None:
            return _avoid("Gap percent unavailable for this session.")

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

        gap = Decimal(gap_percent)
        gap_magnitude = abs(gap)
        threshold = Decimal(str(params.get("minimum_gap_threshold", 0)))
        if gap_magnitude < threshold:
            return _avoid("Gap below the configured threshold.")

        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_gap_direction", False))
        close = Decimal(candle["close"])
        stop_distance = gap_magnitude * close * Decimal(str(params.get("stop_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.0)))

        # Gap up -> fade short. Only eligible when the direction filter allows shorts.
        if gap > threshold and direction_setting in ("short", "both") and (allow_repeat or not self.state.short_fill_taken):
            self.state.entries_taken += 1
            self.state.short_fill_taken = True
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Short gap fill: {gap * 100:.2f}% gap up, fading toward the prior close."],
                direction="short",
            )

        # Gap down -> fade long.
        if gap < -threshold and direction_setting in ("long", "both") and (allow_repeat or not self.state.long_fill_taken):
            self.state.entries_taken += 1
            self.state.long_fill_taken = True
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Long gap fill: {gap * 100:.2f}% gap down, fading toward the prior close."],
                direction="long",
            )

        return _avoid("Gap direction does not match an eligible side.")


GAP_FILL_BLOCKS: dict[str, str] = {
    "trend": "gap_fill_session_context",
    "momentum": "gap_fill_relative_volume_confirmation",
    "volatility": "gap_fill_gap_magnitude",
    "volume": "gap_fill_relative_volume",
    "entry": "gap_fill",
    "exit": "gap_fill_session_close_forced",
}

GAP_FILL_THRESHOLDS = ("0.005", "0.010")
GAP_FILL_DIRECTIONS = ("long", "short")


def generate_gap_fill_candidates(*, max_candidates: int = 8) -> list[Any]:
    from itertools import product
    from hashlib import sha256

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    candidates = []
    seen: set[str] = set()
    for threshold, direction in product(GAP_FILL_THRESHOLDS, GAP_FILL_DIRECTIONS):
        params = {
            **DEFAULT_GAP_FILL_PARAMETERS,
            "minimum_gap_threshold": threshold,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(GAP_FILL_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"gapfill_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_gap_fill_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(GAP_FILL_BLOCKS),
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
