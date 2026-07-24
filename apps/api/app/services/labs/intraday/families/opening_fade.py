"""Opening Fade v1 (Phase 12.3).

Hypothesis: the opposite of Opening-Range Breakout -- a move beyond the
settled opening range that fails to hold is a fade back into the range, not
a continuation. Reuses the exact same opening-range fields ORB v1 uses
(`opening_range_high`/`opening_range_low`, settled after
`opening_range_minutes`) but inverts the direction mapping: extension above
the high is a SHORT setup here (ORB trades it long), extension below the
low is a LONG setup (ORB trades it short). ORB v1's own code
(`labs/intraday/strategy.py`) is not imported or modified by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision

OPENING_FADE_ARCHITECTURE = "opening_fade_v1"

VALID_DIRECTIONS = ("long", "short", "both")

DEFAULT_OPENING_FADE_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": OPENING_FADE_ARCHITECTURE,
    "opening_range_minutes": 30,
    "fade_buffer_atr": Decimal("0.1"),
    "minimum_session_relative_volume": Decimal("1.0"),
    "stop_atr_multiple": Decimal("1.0"),
    "reward_risk_multiple": Decimal("1.0"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_fade_direction": False,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.0"),
}


@dataclass
class OpeningFadeState:
    current_session: date | None = None
    entries_taken: int = 0
    long_fade_taken: bool = False
    short_fade_taken: bool = False


class OpeningFadeStrategy:
    """Satisfies `StrategyProtocol`. Fades a failed opening-range extension."""

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid Opening Fade direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = OpeningFadeState()

    def reset(self) -> None:
        self.state = OpeningFadeState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = OpeningFadeState(current_session=session_date)

        opening_range_minutes = int(feature.get("opening_range_minutes") or params.get("opening_range_minutes") or 30)
        minutes_from_open = feature.get("minutes_from_open")
        if minutes_from_open is None or minutes_from_open < opening_range_minutes:
            return _avoid("Opening range not yet complete.")

        opening_range_high = feature.get("opening_range_high")
        opening_range_low = feature.get("opening_range_low")
        if opening_range_high is None or opening_range_low is None:
            return _avoid("Opening range levels unavailable for this bar.")

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

        range_high = Decimal(opening_range_high)
        range_low = Decimal(opening_range_low)
        range_span = range_high - range_low
        if range_span <= 0:
            return _avoid("Opening range span is not positive.")

        buffer = range_span * Decimal(str(params.get("fade_buffer_atr", 0)))
        close = Decimal(candle["close"])
        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_fade_direction", False))
        stop_distance = range_span * Decimal(str(params.get("stop_atr_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.0)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_fade_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_fade_taken)

        # Extended above the high -> fade SHORT (opposite of ORB's long breakout).
        if short_eligible and close > (range_high + buffer):
            self.state.entries_taken += 1
            self.state.short_fade_taken = True
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Short opening fade: close {close} extended past high {range_high} + buffer {buffer}, fading back into range."],
                direction="short",
            )

        # Extended below the low -> fade LONG (opposite of ORB's short breakout).
        if long_eligible and close < (range_low - buffer):
            self.state.entries_taken += 1
            self.state.long_fade_taken = True
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Long opening fade: close {close} extended past low {range_low} - buffer {buffer}, fading back into range."],
                direction="long",
            )

        return _avoid("No opening-range extension beyond the configured buffer.")


OPENING_FADE_BLOCKS: dict[str, str] = {
    "trend": "opening_fade_session_context",
    "momentum": "opening_fade_relative_volume_confirmation",
    "volatility": "opening_fade_opening_range_span",
    "volume": "opening_fade_relative_volume",
    "entry": "opening_fade",
    "exit": "opening_fade_session_close_forced",
}

OPENING_FADE_BUFFER_LEVELS = ("0.05", "0.15")
OPENING_FADE_DIRECTIONS = ("long", "short")


def generate_opening_fade_candidates(*, max_candidates: int = 8) -> list[Any]:
    from itertools import product
    from hashlib import sha256

    from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

    candidates = []
    seen: set[str] = set()
    for buffer_level, direction in product(OPENING_FADE_BUFFER_LEVELS, OPENING_FADE_DIRECTIONS):
        params = {
            **DEFAULT_OPENING_FADE_PARAMETERS,
            "fade_buffer_atr": buffer_level,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(OPENING_FADE_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"ofade_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_opening_fade_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(OPENING_FADE_BLOCKS),
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
