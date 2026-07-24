"""Intraday Lab strategy families (Phase 12).

Opening-Range Breakout v1 (Step 2B) and VWAP Reversion v1 (this pass) live
here. Everything reusable --
next-bar-open execution, position sizing, fees, slippage, stop/target
resolution, maximum holding behavior, and the structural session-close exit
enforced via `ExecutionConstraints.flat_by_session_close` -- stays owned by
the generic simulator (Step 2A). This module only ever returns a
`StrategyDecision`; it never scans for exits itself, and it never infers a
session change from a UTC date -- session identity comes entirely from
`feature["session_date"]`, the same calendar-derived value the intraday
dataset loader already validated.

There is no dedicated ATR/volatility field in `intraday_features` for 15m/30m
bars yet (only `session_vwap`, `distance_from_session_vwap`, the opening-range
fields, `gap_percent`, and `session_relative_volume`). Rather than adding new
volatility computation inside the strategy layer -- which the architecture
review's approved conclusion said should stay out of the simulator/strategy
boundary -- this reuses the settled opening-range span
(`opening_range_high - opening_range_low`) as the volatility unit for the
`breakout_buffer_atr`/`stop_atr_multiple` parameters. Both names are kept
from the requested parameter list; the unit they scale is this range span.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.labs.intraday.dataset import minimum_entry_lookahead_minutes
from app.services.strategy import ExecutionConstraints, StrategyDecision

OPENING_RANGE_BREAKOUT_ARCHITECTURE = "opening_range_breakout_v1"

VALID_DIRECTIONS = ("long", "short", "both")

# Deliberately small, explicit default parameter set -- not a search space.
# `generate_orb_candidates` (campaign.py) varies only a couple of these for
# the pilot; this is what a single ORB instance runs with by default.
DEFAULT_ORB_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": OPENING_RANGE_BREAKOUT_ARCHITECTURE,
    "opening_range_minutes": 30,
    "breakout_buffer_atr": Decimal("0.1"),
    "minimum_session_relative_volume": Decimal("1.0"),
    "stop_atr_multiple": Decimal("1.0"),
    "reward_risk_multiple": Decimal("1.5"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_breakout_direction": False,
    # Generic simulator parameters every strategy must supply (BASE_PARAMETERS
    # equivalents) -- risk_reward is only a gate fallback; the decision always
    # sets its own risk_reward, so this value is never actually used.
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    # NOT 1.0: run_backtest's walk-forward split only skips entirely when
    # len(rows) < 80 (true for every unit-test fixture in this module, which
    # is why this bug was invisible there). On real production datasets
    # (thousands of rows), a ratio of 1.0 makes split_index == len(rows) - 1,
    # leaving a 1-bar validation window and i = max(start_index, 50) landing
    # past the end of that window -- the loop body never executes and every
    # job silently produces zero trades. Confirmed via the Step 2B pilot:
    # all 80 jobs came back with number_of_trades == 0 until this was fixed
    # to match the existing swing convention (BASE_PARAMETERS).
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.5"),
}


@dataclass
class OpeningRangeBreakoutState:
    """Typed, ORB-owned state. The simulator never inspects this."""

    current_session: date | None = None
    entries_taken: int = 0
    long_breakout_taken: bool = False
    short_breakout_taken: bool = False


class OpeningRangeBreakoutStrategy:
    """Satisfies `StrategyProtocol` from `app.services.strategy`.

    One instance is meant for one (symbol, timeframe, parameter combination,
    backtest run): `make_strategy_definition` constructs a fresh instance per
    candidate evaluation, and `run_backtest` additionally calls `reset()`
    before every run regardless -- state can never straddle a run, symbol,
    campaign job, or rerun even if a caller reused one instance.
    """

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid ORB direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = OpeningRangeBreakoutState()

    def reset(self) -> None:
        self.state = OpeningRangeBreakoutState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        # Session identity comes only from the already-calendar-resolved
        # `session_date` column -- never from a UTC date derived from the
        # candle timestamp, which would be wrong across session boundaries
        # that don't align to UTC midnight.
        if session_date != self.state.current_session:
            self.state = OpeningRangeBreakoutState(current_session=session_date)

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

        buffer = range_span * Decimal(str(params.get("breakout_buffer_atr", 0)))
        close = Decimal(candle["close"])
        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_breakout_direction", False))
        stop_distance = range_span * Decimal(str(params.get("stop_atr_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.5)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_breakout_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_breakout_taken)

        if long_eligible and close > (range_high + buffer):
            self.state.entries_taken += 1
            self.state.long_breakout_taken = True
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Long opening-range breakout: close {close} > high {range_high} + buffer {buffer}."],
                direction="long",
            )

        if short_eligible and close < (range_low - buffer):
            self.state.entries_taken += 1
            self.state.short_breakout_taken = True
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Short opening-range breakout: close {close} < low {range_low} - buffer {buffer}."],
                direction="short",
            )

        return _avoid("No breakout beyond the configured buffer.")


def _avoid(reason: str) -> StrategyDecision:
    return StrategyDecision("avoid", None, None, None, None, [reason])


VWAP_REVERSION_ARCHITECTURE = "vwap_reversion_v1"

# entry_deviation_threshold defaults chosen from the real distribution of
# |distance_from_session_vwap| across the research-core symbols: median
# ~0.33%, p75 ~0.6%, p90 ~1.0% (both 15m and 30m). 0.006 (~p75) is the
# default -- meaningfully extended, not just noise, without being so rare
# the strategy rarely fires.
DEFAULT_VWAP_REVERSION_PARAMETERS: dict[str, Any] = {
    "strategy_architecture": VWAP_REVERSION_ARCHITECTURE,
    "entry_deviation_threshold": Decimal("0.006"),
    "minimum_session_relative_volume": Decimal("1.0"),
    "stop_multiple": Decimal("1.5"),
    "reward_risk_multiple": Decimal("1.0"),
    "maximum_entries_per_session": 1,
    "minimum_minutes_before_close_for_entry": 0,
    "direction": "both",
    "allow_repeat_reversion_direction": False,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    # See the ORB walk_forward_train_ratio comment above -- 1.0 silently
    # zeroes every real job once len(rows) >= 80. Fixed to 0.7 from the
    # start here.
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
    "risk_reward": Decimal("1.0"),
}


@dataclass
class VwapReversionState:
    """Typed, VWAP-Reversion-owned state. The simulator never inspects this."""

    current_session: date | None = None
    entries_taken: int = 0
    long_reversion_taken: bool = False
    short_reversion_taken: bool = False


class VwapReversionStrategy:
    """Satisfies `StrategyProtocol`. Mean-reversion around the session VWAP.

    Entry: the bar's close is extended `entry_deviation_threshold` or more
    away from `session_vwap` (long when extended below, short when extended
    above). No dedicated ATR field exists for 15m/30m yet (same situation
    ORB v1 documented), so the volatility unit here is the bar's own
    distance from VWAP at signal time (`abs(close - session_vwap)`) rather
    than a manufactured indicator -- a dataset-native measure specific to
    this family, distinct from ORB's opening-range-span unit.
    """

    def __init__(self, params: dict[str, Any], *, timeframe: str):
        direction = str(params.get("direction", "both"))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Invalid VWAP Reversion direction {direction!r}; must be one of {VALID_DIRECTIONS}.")
        self.params = params
        self.timeframe = timeframe
        self.execution_constraints = ExecutionConstraints(flat_by_session_close=True)
        self.state = VwapReversionState()

    def reset(self) -> None:
        self.state = VwapReversionState()

    def __call__(
        self,
        candle: dict[str, Any],
        feature: dict[str, Any],
        recent_candles: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> StrategyDecision:
        session_date = feature.get("session_date")
        if session_date != self.state.current_session:
            self.state = VwapReversionState(current_session=session_date)

        session_vwap = feature.get("session_vwap")
        if session_vwap is None:
            return _avoid("Session VWAP unavailable for this bar.")

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

        direction_setting = str(params.get("direction", "both"))
        allow_repeat = bool(params.get("allow_repeat_reversion_direction", False))
        stop_distance = deviation_distance * Decimal(str(params.get("stop_multiple", 1)))
        reward_risk = Decimal(str(params.get("reward_risk_multiple", 1.0)))

        long_eligible = direction_setting in ("long", "both") and (allow_repeat or not self.state.long_reversion_taken)
        short_eligible = direction_setting in ("short", "both") and (allow_repeat or not self.state.short_reversion_taken)

        # Price below VWAP by at least the threshold -> bet on reversion up.
        if long_eligible and deviation <= -threshold:
            self.state.entries_taken += 1
            self.state.long_reversion_taken = True
            stop_loss = close - stop_distance
            take_profit = close + (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Long VWAP reversion: close {close} is {deviation_distance} below VWAP {vwap} (threshold {threshold})."],
                direction="long",
            )

        # Price above VWAP by at least the threshold -> bet on reversion down.
        if short_eligible and deviation >= threshold:
            self.state.entries_taken += 1
            self.state.short_reversion_taken = True
            stop_loss = close + stop_distance
            take_profit = close - (stop_distance * reward_risk)
            return StrategyDecision(
                "setup",
                (close, close),
                stop_loss,
                take_profit,
                reward_risk,
                [f"Short VWAP reversion: close {close} is {deviation_distance} above VWAP {vwap} (threshold {threshold})."],
                direction="short",
            )

        return _avoid("Deviation direction does not match an eligible side.")


# One registry, not a growing if/elif chain: `make_strategy_definition`
# (strategy_discovery.py) and `is_intraday_lab_candidate` (campaign.py) both
# key off this dict so adding a third intraday family never requires another
# branch in either place -- only another entry here.
INTRADAY_STRATEGY_FACTORIES: dict[str, type] = {
    OPENING_RANGE_BREAKOUT_ARCHITECTURE: OpeningRangeBreakoutStrategy,
    VWAP_REVERSION_ARCHITECTURE: VwapReversionStrategy,
}
