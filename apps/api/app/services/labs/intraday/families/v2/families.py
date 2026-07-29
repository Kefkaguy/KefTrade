"""Strategy Engine V2 families and participant-flow research families.

Each family is a genuinely different market hypothesis, not a reparameterized
EMA/RSI rule. Every one declares what it expects the market to do, what must
be true to trade it, what would falsify it, and how success will be judged --
all written before any campaign runs.

Five families need an absolute price target (session VWAP, the prior
session's close, the opposite opening-range boundary) and therefore opt into
the Phase 13.4 `honor_absolute_take_profit` execution semantics. The rest use
R-multiple targets and stay on the baseline semantics.

Where a hypothesis is directionally symmetric the family supports both sides;
where it is not (gap continuation, seasonality windows) it is long-only. No
V2 family is externally executable -- `execution_capability` is
`simulation_only` for all ten until each has passed the unchanged elite gate
and been explicitly reviewed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.labs.intraday.families.v2.base import (
    EntryPlan,
    HypothesisSpec,
    V2Strategy,
    register_v2_family,
)

_INTRADAY_DNA_COMMON = {
    "strategy_version": "v2",
    "execution_capability": "simulation_only",
    "holding_horizon_class": "intraday_hours",
    "timeframe_class": "intraday_15m_30m",
    # The original candle-pattern families failed the full 15m/30m raw-signal
    # audit. They remain registered for reproducibility, but the registry
    # archives them and this DNA records the evidence honestly.
    "evidence_confidence": "tested_negative_archived",
}


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ===========================================================================
# 1. Opening Range Breakout v2
# ===========================================================================

class OpeningRangeBreakoutV2(V2Strategy):
    architecture = "opening_range_breakout_v2"
    feature_groups = ("session", "opening_range", "relative_volume", "vwap", "volatility")
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Opening Range Breakout v2",
        market_behavior=(
            "The first N minutes of a session establish a reference range. A decisive close "
            "beyond that range, on genuinely elevated participation, marks the start of a "
            "directional session rather than noise."
        ),
        hypothesis=(
            "Closes beyond a completed opening range, confirmed by same-time-of-day relative "
            "volume and a minimum range quality, continue in the breakout direction more often "
            "than they revert."
        ),
        required_conditions=(
            "Opening range of the configured width is complete.",
            "Bar closes beyond the range boundary by at least the configured ATR buffer.",
            "Relative volume at or above the configured minimum.",
            "Range width within the configured ATR band (not a dead range, not a gap-blown one).",
            "Optional: close on the same side of session VWAP as the breakout.",
        ),
        invalidation_conditions=(
            "Price closes back inside the opening range after breaking out (failed breakout).",
            "Range-bound regime with repeated boundary rejections.",
            "Volume at or below the same-time-of-day norm.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        window = int(params.get("opening_range_minutes", 30))
        prefix = f"or{window}"
        if not v2.get(f"{prefix}_complete"):
            return f"Opening range ({window}m) not yet complete."

        high, low = v2.get(f"{prefix}_high"), v2.get(f"{prefix}_low")
        atr = v2.get("atr")
        if high is None or low is None or atr is None or atr <= 0:
            return "Opening range levels or ATR unavailable."

        width_atr = v2.get(f"{prefix}_width_atr")
        min_width = float(params.get("minimum_range_width_atr", 0.3))
        max_width = float(params.get("maximum_range_width_atr", 5.0))
        if width_atr is None or not (min_width <= width_atr <= max_width):
            return f"Opening range width {width_atr} outside the tradable band."

        relative_volume = v2.get("same_time_of_day_relative_volume") or v2.get("rolling_relative_volume")
        minimum_volume = float(params.get("minimum_relative_volume", 1.2))
        if relative_volume is None or relative_volume < minimum_volume:
            return "Relative-volume confirmation failed."

        if v2.get(f"{prefix}_failed_breakout_up") or v2.get(f"{prefix}_failed_breakout_down"):
            return "Breakout already failed back inside the range this session."

        close = float(candle["close"])
        buffer_atr = float(params.get("breakout_buffer_atr", 0.1))
        buffer_price = buffer_atr * atr
        require_vwap = bool(params.get("require_vwap_alignment", False))
        vwap = v2.get("session_vwap")

        if close > high + buffer_price:
            if require_vwap and (vwap is None or close < vwap):
                return "Upside breakout without VWAP alignment."
            return EntryPlan("long", _d(low), f"Close {close:.2f} broke {window}m range high {high:.2f} on {relative_volume:.2f}x volume.")
        if close < low - buffer_price:
            if require_vwap and (vwap is None or close > vwap):
                return "Downside breakout without VWAP alignment."
            return EntryPlan("short", _d(high), f"Close {close:.2f} broke {window}m range low {low:.2f} on {relative_volume:.2f}x volume.")
        return "Close is inside the opening range."


register_v2_family(
    OpeningRangeBreakoutV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "opening_range_breakout_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "range_breakout",
        "confirmation_structure": ["relative_volume", "closing_confirmation", "range_quality"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "roughly_daily",
        "trend_dependency": "benefits_from_trend",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "first_two_hours",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["range_bound"],
        "feature_dependencies": ["or30_complete", "or30_high", "or30_low", "same_time_of_day_relative_volume", "atr"],
    },
    parameter_grid={
        "opening_range_minutes": (15, 30),
        "breakout_buffer_atr": (Decimal("0.05"), Decimal("0.15")),
        "minimum_relative_volume": (Decimal("1.2"), Decimal("1.5")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "opening_range_breakout", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 2. Opening Range Fade v2
# ===========================================================================

class OpeningRangeFadeV2(V2Strategy):
    architecture = "opening_range_fade_v2"
    feature_groups = ("session", "opening_range", "relative_volume", "vwap", "volatility")
    uses_absolute_targets = True
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Opening Range Fade v2",
        market_behavior=(
            "A breakout that immediately fails -- price pushes beyond the opening range and "
            "then closes back inside it -- signals absorbed supply or demand rather than a "
            "genuine directional session."
        ),
        hypothesis=(
            "After a failed opening-range breakout, price reverts toward session VWAP or the "
            "opposite range boundary more often than it re-breaks in the original direction."
        ),
        required_conditions=(
            "Opening range complete and price traded beyond it earlier this session.",
            "Current bar closes back inside the range.",
            "Volatility not in an extreme-expansion state.",
            "Relative volume below the configured runaway-trend ceiling.",
        ),
        invalidation_conditions=(
            "Price re-breaks and holds beyond the range boundary.",
            "Strong trending regime with expanding volatility.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        window = int(params.get("opening_range_minutes", 30))
        prefix = f"or{window}"
        if not v2.get(f"{prefix}_complete"):
            return f"Opening range ({window}m) not yet complete."

        high, low = v2.get(f"{prefix}_high"), v2.get(f"{prefix}_low")
        if high is None or low is None:
            return "Opening range levels unavailable."

        relative_volume = v2.get("rolling_relative_volume")
        ceiling = float(params.get("maximum_relative_volume", 2.5))
        if relative_volume is not None and relative_volume > ceiling:
            return "Volume too heavy -- looks like a real trend, not a failed break."

        if bool(params.get("avoid_volatility_expansion", True)) and v2.get("volatility_expansion"):
            return "Volatility expanding; fading is invalidated."

        target_mode = str(params.get("fade_target", "vwap"))
        vwap = v2.get("session_vwap")

        # A failed upside break fades short; a failed downside break fades long.
        if v2.get(f"{prefix}_failed_breakout_up"):
            target = _d(vwap) if (target_mode == "vwap" and vwap is not None) else _d(low)
            close = float(candle["close"])
            if float(target) >= close:
                return "Fade target is not below price; no downside left to capture."
            return EntryPlan("short", _d(high), f"Failed upside break of {window}m range; fading toward {target_mode}.", take_profit=target)
        if v2.get(f"{prefix}_failed_breakout_down"):
            target = _d(vwap) if (target_mode == "vwap" and vwap is not None) else _d(high)
            close = float(candle["close"])
            if float(target) <= close:
                return "Fade target is not above price; no upside left to capture."
            return EntryPlan("long", _d(low), f"Failed downside break of {window}m range; fading toward {target_mode}.", take_profit=target)
        return "No failed opening-range breakout this session."


register_v2_family(
    OpeningRangeFadeV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "opening_range_fade_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "range_fade",
        "confirmation_structure": ["closing_confirmation", "volatility_expansion"],
        "exit_structure": ["vwap_target", "opposite_range_boundary_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "requires_range",
        "volatility_dependency": "requires_normal_or_low",
        "volume_dependency": "agnostic",
        "session_dependency": "first_two_hours",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "mean_reversion",
        "required_regime": ["range_bound", "normal_volatility"],
        "invalidation_regime": ["trending_up", "trending_down", "high_volatility"],
        "feature_dependencies": ["or30_failed_breakout_up", "or30_failed_breakout_down", "session_vwap"],
    },
    parameter_grid={
        "opening_range_minutes": (15, 30),
        "fade_target": ("vwap", "opposite_boundary"),
        "maximum_relative_volume": (Decimal("2.0"), Decimal("2.5")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "failed_opening_range_breakout", "exit": "absolute_target_or_session_close"},
)


# ===========================================================================
# 3. VWAP Bounce
# ===========================================================================

class VwapBounceV2(V2Strategy):
    architecture = "vwap_bounce_v2"
    feature_groups = ("session", "vwap", "relative_volume", "volatility", "market_structure")
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="VWAP Bounce v2",
        market_behavior=(
            "In a directional session, session VWAP acts as a reference that trend participants "
            "defend. Price returning to VWAP and being rejected from it marks continuation, not "
            "reversal."
        ),
        hypothesis=(
            "When structure confirms a trend, a touch of session VWAP followed by rejection in "
            "the trend direction, on confirming volume, continues the trend."
        ),
        required_conditions=(
            "Market structure confirms a trend in the traded direction.",
            "Price approached within an ATR-normalized tolerance of session VWAP.",
            "Current bar rejects VWAP back in the trend direction.",
            "Volume confirmation at or above the configured minimum.",
        ),
        invalidation_conditions=(
            "Price closes decisively through VWAP against the trend.",
            "Structure flips (a break of the opposing swing).",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        vwap = v2.get("session_vwap")
        atr = v2.get("atr")
        if vwap is None or atr is None or atr <= 0:
            return "Session VWAP or ATR unavailable."

        tolerance_atr = float(params.get("vwap_tolerance_atr", 0.5))
        low, high, close = float(candle["low"]), float(candle["high"]), float(candle["close"])
        approached_from_above = (low - vwap) <= tolerance_atr * atr and low >= vwap - tolerance_atr * atr
        approached_from_below = (vwap - high) <= tolerance_atr * atr and high <= vwap + tolerance_atr * atr

        relative_volume = v2.get("rolling_relative_volume")
        minimum_volume = float(params.get("minimum_relative_volume", 1.0))
        if relative_volume is None or relative_volume < minimum_volume:
            return "Volume confirmation failed."

        structure = v2.get("structure_state")
        require_structure = bool(params.get("require_structure_confirmation", True))

        if close > vwap and approached_from_above:
            if require_structure and structure not in ("uptrend", "mixed"):
                return f"Structure {structure!r} does not confirm an uptrend."
            swing_low = v2.get("last_confirmed_swing_low")
            stop = _d(min(low, swing_low) if swing_low is not None else low)
            return EntryPlan("long", stop, f"Rejected VWAP {vwap:.2f} from above in a {structure} structure.")
        if close < vwap and approached_from_below:
            if require_structure and structure not in ("downtrend", "mixed"):
                return f"Structure {structure!r} does not confirm a downtrend."
            swing_high = v2.get("last_confirmed_swing_high")
            stop = _d(max(high, swing_high) if swing_high is not None else high)
            return EntryPlan("short", stop, f"Rejected VWAP {vwap:.2f} from below in a {structure} structure.")
        return "No VWAP approach-and-rejection on this bar."


register_v2_family(
    VwapBounceV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "vwap_bounce_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "vwap_pullback",
        "confirmation_structure": ["structure_state", "relative_volume", "vwap_alignment"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "roughly_daily",
        "trend_dependency": "requires_trend",
        "volatility_dependency": "agnostic",
        "volume_dependency": "requires_confirmation",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "requires_confirmed_structure",
        "behavior_class": "momentum",
        "required_regime": ["trending_up", "trending_down"],
        "invalidation_regime": ["range_bound"],
        "feature_dependencies": ["session_vwap", "atr", "structure_state", "last_confirmed_swing_low"],
    },
    parameter_grid={
        "vwap_tolerance_atr": (Decimal("0.3"), Decimal("0.6")),
        "minimum_relative_volume": (Decimal("1.0"), Decimal("1.3")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "vwap_rejection_with_structure", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 4. VWAP Mean Reversion v2
# ===========================================================================

class VwapMeanReversionV2(V2Strategy):
    architecture = "vwap_mean_reversion_v2"
    feature_groups = ("session", "vwap", "volatility", "relative_volume")
    uses_absolute_targets = True
    supports_short = True
    hypothesis = HypothesisSpec(
        title="VWAP Mean Reversion v2",
        market_behavior=(
            "Price stretched a long way from session VWAP, with momentum already fading, is "
            "over-extended relative to the session's own volume-weighted consensus."
        ),
        hypothesis=(
            "When ATR-normalized deviation from session VWAP exceeds a threshold and the "
            "extension is no longer accelerating, price reverts toward VWAP."
        ),
        required_conditions=(
            "ATR-normalized VWAP deviation beyond the configured threshold.",
            "Extension no longer accelerating (declining bar momentum).",
            "Outside the opening drive window, where extension is normal.",
            "Volatility not in an extreme-expansion state.",
        ),
        invalidation_conditions=(
            "Deviation keeps expanding with rising volume (a real trend day).",
            "Volatility expansion regime.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        deviation_atr = v2.get("vwap_deviation_atr")
        vwap = v2.get("session_vwap")
        if deviation_atr is None or vwap is None:
            return "VWAP deviation unavailable."

        minutes_from_open = v2.get("minutes_from_open")
        skip_minutes = int(params.get("skip_first_minutes", 60))
        if minutes_from_open is None or minutes_from_open < skip_minutes:
            return "Inside the opening drive window; extension is expected there."

        if bool(params.get("avoid_volatility_expansion", True)) and v2.get("volatility_expansion"):
            return "Volatility expanding; reversion is invalidated."

        threshold = float(params.get("deviation_threshold_atr", 2.0))
        if abs(deviation_atr) < threshold:
            return f"Deviation {deviation_atr:.2f} ATR below the {threshold} ATR threshold."

        # Exhaustion proxy from bar data only: the extension bar is no longer
        # the strongest push in its own direction.
        bar_return = v2.get("bar_return")
        if bar_return is not None:
            if deviation_atr > 0 and bar_return > float(params.get("maximum_continuation_return", 0.002)):
                return "Extension still accelerating upward; not yet exhausted."
            if deviation_atr < 0 and bar_return < -float(params.get("maximum_continuation_return", 0.002)):
                return "Extension still accelerating downward; not yet exhausted."

        close = float(candle["close"])
        atr = v2.get("atr") or 0.0
        stop_atr = float(params.get("stop_atr_multiple", 1.0))
        if deviation_atr > 0:
            return EntryPlan("short", _d(close + stop_atr * atr), f"Extended {deviation_atr:.2f} ATR above VWAP; reverting to {vwap:.2f}.", take_profit=_d(vwap))
        return EntryPlan("long", _d(close - stop_atr * atr), f"Extended {deviation_atr:.2f} ATR below VWAP; reverting to {vwap:.2f}.", take_profit=_d(vwap))


register_v2_family(
    VwapMeanReversionV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "vwap_mean_reversion_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "vwap_extension_fade",
        "confirmation_structure": ["declining_momentum", "session_window"],
        "exit_structure": ["vwap_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "requires_range",
        "volatility_dependency": "requires_normal_or_low",
        "volume_dependency": "agnostic",
        "session_dependency": "avoids_open",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "mean_reversion",
        "required_regime": ["range_bound", "normal_volatility"],
        "invalidation_regime": ["trending_up", "trending_down", "high_volatility"],
        "feature_dependencies": ["vwap_deviation_atr", "session_vwap", "volatility_expansion", "bar_return"],
    },
    parameter_grid={
        "deviation_threshold_atr": (Decimal("1.5"), Decimal("2.5")),
        "skip_first_minutes": (60, 90),
        "direction": ("long", "short"),
    },
    blocks={"entry": "vwap_extension_exhaustion", "exit": "vwap_target_or_session_close"},
)


# ===========================================================================
# 5. Gap Continuation v2
# ===========================================================================

class GapContinuationV2(V2Strategy):
    architecture = "gap_continuation_v2"
    feature_groups = ("session", "gap", "opening_range", "relative_volume", "volatility")
    uses_absolute_targets = False
    supports_short = False  # Long-first: the short leg is a separate hypothesis.
    hypothesis = HypothesisSpec(
        title="Gap Continuation v2",
        market_behavior=(
            "A large overnight gap that the market does not immediately fade represents genuine "
            "repricing on news, not a liquidity artifact."
        ),
        hypothesis=(
            "An ATR-normalized gap up that holds above the session open and pushes through the "
            "opening range on supporting volume continues in the gap direction."
        ),
        required_conditions=(
            "Overnight gap at or beyond the configured ATR-normalized size.",
            "Price holding above the session open (gap not fading).",
            "Relative volume at or above the configured minimum.",
            "Price extended through the opening range in the gap direction.",
        ),
        invalidation_conditions=(
            "Gap fill fraction exceeds the configured ceiling (the gap is closing).",
            "Price closes back below the session open.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran. Long-only by design.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        gap_atr = v2.get("gap_atr")
        direction = v2.get("gap_direction")
        session_open = v2.get("session_open")
        if gap_atr is None or session_open is None:
            return "Gap size or session open unavailable."
        if direction != "up":
            return "No qualifying gap up (long-only family)."

        minimum_gap_atr = float(params.get("minimum_gap_atr", 0.5))
        if gap_atr < minimum_gap_atr:
            return f"Gap {gap_atr:.2f} ATR below the {minimum_gap_atr} ATR minimum."

        fill_fraction = v2.get("gap_fill_fraction")
        maximum_fill = float(params.get("maximum_gap_fill_fraction", 0.5))
        if fill_fraction is not None and fill_fraction > maximum_fill:
            return f"Gap already {fill_fraction:.0%} filled; continuation invalidated."

        close = float(candle["close"])
        if close <= session_open:
            return "Price is not holding above the session open."

        relative_volume = v2.get("same_time_of_day_relative_volume") or v2.get("rolling_relative_volume")
        minimum_volume = float(params.get("minimum_relative_volume", 1.2))
        if relative_volume is None or relative_volume < minimum_volume:
            return "Relative-volume support failed."

        window = int(params.get("opening_range_minutes", 30))
        or_high = v2.get(f"or{window}_high")
        if not v2.get(f"or{window}_complete") or or_high is None:
            return f"Opening range ({window}m) not yet complete."
        if close <= or_high:
            return "Price has not extended through the opening range."

        return EntryPlan("long", _d(session_open), f"Gap up {gap_atr:.2f} ATR holding above open and through the {window}m range.")


register_v2_family(
    GapContinuationV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "gap_continuation_v2",
        "direction_support": ["long"],
        "entry_structure": "gap_open_continuation",
        "confirmation_structure": ["relative_volume", "closing_confirmation", "range_quality"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "benefits_from_trend",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "first_two_hours",
        "gap_dependency": "requires_gap",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["range_bound"],
        "feature_dependencies": ["gap_atr", "gap_direction", "gap_fill_fraction", "session_open", "or30_high"],
    },
    parameter_grid={
        "minimum_gap_atr": (Decimal("0.5"), Decimal("1.0")),
        "minimum_relative_volume": (Decimal("1.2"), Decimal("1.5")),
        "maximum_gap_fill_fraction": (Decimal("0.35"), Decimal("0.5")),
    },
    blocks={"entry": "gap_continuation", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 6. Gap Fill v2
# ===========================================================================

class GapFillV2(V2Strategy):
    architecture = "gap_fill_v2"
    feature_groups = ("session", "gap", "relative_volume", "volatility")
    uses_absolute_targets = True
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Gap Fill v2",
        market_behavior=(
            "A gap that opens without follow-through -- no volume expansion, no extension beyond "
            "the open -- reflects a thin overnight repricing that the regular session reverses."
        ),
        hypothesis=(
            "An unconfirmed gap (no continuation, ordinary volume) retraces toward the prior "
            "session close.",
        ),
        required_conditions=(
            "Gap at or beyond the configured ATR-normalized minimum.",
            "No continuation confirmation (price not extending in the gap direction).",
            "Relative volume below the configured continuation ceiling.",
            "Outside the opening minutes, so the fade is not fighting the opening auction.",
        ),
        invalidation_conditions=(
            "Volume expands and price extends in the gap direction.",
            "Gap already filled (nothing left to capture).",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        gap_atr = v2.get("gap_atr")
        direction = v2.get("gap_direction")
        prior_close = v2.get("prior_session_close")
        session_open = v2.get("session_open")
        if gap_atr is None or prior_close is None or session_open is None or direction not in ("up", "down"):
            return "Gap context unavailable."

        minimum_gap_atr = float(params.get("minimum_gap_atr", 0.5))
        if abs(gap_atr) < minimum_gap_atr:
            return f"Gap {abs(gap_atr):.2f} ATR below the {minimum_gap_atr} ATR minimum."

        minutes_from_open = v2.get("minutes_from_open")
        skip_minutes = int(params.get("skip_first_minutes", 30))
        if minutes_from_open is None or minutes_from_open < skip_minutes:
            return "Inside the opening auction window."

        if v2.get("gap_continuation"):
            return "Gap is showing continuation; fade invalidated."

        relative_volume = v2.get("rolling_relative_volume")
        ceiling = float(params.get("maximum_relative_volume", 2.0))
        if relative_volume is not None and relative_volume > ceiling:
            return "Volume too heavy; looks like real continuation."

        fill_fraction = v2.get("gap_fill_fraction")
        if fill_fraction is not None and fill_fraction >= 1.0:
            return "Gap already filled."

        # A "partial fill" target is expressed as the trade's ACTUAL single
        # target -- the engine has no multi-leg exits and none are invented.
        fill_target = float(params.get("fill_target_fraction", 1.0))
        close = float(candle["close"])
        target_price = session_open + (prior_close - session_open) * fill_target
        atr = v2.get("atr") or 0.0
        stop_atr = float(params.get("stop_atr_multiple", 1.0))

        if direction == "up":
            if target_price >= close:
                return "Fill target is not below price."
            return EntryPlan("short", _d(close + stop_atr * atr), f"Unconfirmed gap up; fading {fill_target:.0%} toward prior close {prior_close:.2f}.", take_profit=_d(target_price))
        if target_price <= close:
            return "Fill target is not above price."
        return EntryPlan("long", _d(close - stop_atr * atr), f"Unconfirmed gap down; fading {fill_target:.0%} toward prior close {prior_close:.2f}.", take_profit=_d(target_price))


register_v2_family(
    GapFillV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "gap_fill_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "gap_open_fade",
        "confirmation_structure": ["session_window", "declining_momentum"],
        "exit_structure": ["prior_close_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "requires_range",
        "volatility_dependency": "requires_normal_or_low",
        "volume_dependency": "agnostic",
        "session_dependency": "avoids_open",
        "gap_dependency": "requires_gap",
        "market_structure_dependency": "agnostic",
        "behavior_class": "mean_reversion",
        "required_regime": ["range_bound", "normal_volatility"],
        "invalidation_regime": ["trending_up", "trending_down"],
        "feature_dependencies": ["gap_atr", "prior_session_close", "gap_continuation", "gap_fill_fraction"],
    },
    parameter_grid={
        "minimum_gap_atr": (Decimal("0.5"), Decimal("1.0")),
        "fill_target_fraction": (Decimal("0.5"), Decimal("1.0")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "unconfirmed_gap_fade", "exit": "prior_close_target_or_session_close"},
)


# ===========================================================================
# 7. Relative-Volume Momentum v2
# ===========================================================================

class RelativeVolumeMomentumV2(V2Strategy):
    architecture = "relative_volume_momentum_v2"
    feature_groups = ("session", "relative_volume", "volatility", "market_structure")
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Relative-Volume Momentum v2",
        market_behavior=(
            "Volume far above the same-time-of-day norm marks the arrival of informed or forced "
            "flow. Price expansion on that flow is informative in a way that price expansion on "
            "ordinary volume is not."
        ),
        hypothesis=(
            "Abnormal same-time-of-day relative volume, together with a price expansion bar and "
            "confirming structure, continues briefly in the expansion direction."
        ),
        required_conditions=(
            "Same-time-of-day relative volume classified abnormal, with a reliable sample.",
            "Range expansion beyond the configured ratio.",
            "Structure confirms the expansion direction.",
        ),
        invalidation_conditions=(
            "Volume normalizes without price follow-through.",
            "Structure opposes the expansion direction.",
            "The same-time-of-day sample is too small to be reliable.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        if bool(params.get("require_reliable_sample", True)) and not v2.get("same_time_of_day_volume_reliable"):
            return "Same-time-of-day volume sample too small to be reliable."

        relative_volume = v2.get("same_time_of_day_relative_volume")
        threshold = float(params.get("minimum_relative_volume", 2.0))
        if relative_volume is None or relative_volume < threshold:
            return f"Relative volume {relative_volume} below the {threshold}x threshold."

        expansion = v2.get("range_expansion_ratio")
        minimum_expansion = float(params.get("minimum_range_expansion", 1.5))
        if expansion is None or expansion < minimum_expansion:
            return f"Range expansion {expansion} below the {minimum_expansion}x minimum."

        bar_return = v2.get("bar_return")
        if bar_return is None or bar_return == 0:
            return "No directional price expansion on this bar."

        structure = v2.get("structure_state")
        require_structure = bool(params.get("require_structure_confirmation", True))
        low, high = float(candle["low"]), float(candle["high"])

        if bar_return > 0:
            if require_structure and structure not in ("uptrend", "mixed"):
                return f"Structure {structure!r} does not confirm upside expansion."
            return EntryPlan("long", _d(low), f"Abnormal volume {relative_volume:.2f}x with {expansion:.2f}x range expansion, upside.")
        if require_structure and structure not in ("downtrend", "mixed"):
            return f"Structure {structure!r} does not confirm downside expansion."
        return EntryPlan("short", _d(high), f"Abnormal volume {relative_volume:.2f}x with {expansion:.2f}x range expansion, downside.")


register_v2_family(
    RelativeVolumeMomentumV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "relative_volume_momentum_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "volume_surge_continuation",
        "confirmation_structure": ["relative_volume", "structure_state", "volatility_expansion"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_minutes",
        "expected_frequency_class": "multiple_per_session",
        "trend_dependency": "agnostic",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "uses_structure_context",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["low_volatility"],
        "feature_dependencies": ["same_time_of_day_relative_volume", "range_expansion_ratio", "structure_state"],
    },
    parameter_grid={
        "minimum_relative_volume": (Decimal("2.0"), Decimal("3.0")),
        "minimum_range_expansion": (Decimal("1.5"), Decimal("2.0")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "abnormal_volume_expansion", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 8. Volatility Squeeze Breakout v2
# ===========================================================================

class VolatilitySqueezeBreakoutV2(V2Strategy):
    architecture = "volatility_squeeze_breakout_v2"
    feature_groups = ("session", "volatility", "relative_volume", "market_structure")
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Volatility Squeeze Breakout v2",
        market_behavior=(
            "Volatility is mean-reverting in the sense that compression precedes expansion. A "
            "period where Bollinger bands contract inside the Keltner channel marks coiled "
            "positioning."
        ),
        hypothesis=(
            "After a confirmed compression, the first genuine volatility expansion with volume "
            "confirmation resolves directionally and continues briefly."
        ),
        required_conditions=(
            "Compression confirmed on the window ENDING BEFORE this bar (an expansion bar destroys its own compression reading).",
            "Current bar shows confirmed volatility expansion.",
            "Volume confirmation at or above the configured minimum.",
            "A directional bar establishing the breakout side.",
        ),
        invalidation_conditions=(
            "Expansion reverses within the configured false-breakout window.",
            "Volume does not confirm the expansion.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        # Compression is read from the window ENDING BEFORE this bar. A large
        # expansion bar inflates ATR and the Bollinger stdev, so measuring
        # compression on the same bar would destroy the very reading the
        # hypothesis depends on -- "was it coiled before it moved?".
        compression = v2.get("prior_atr_compression_ratio")
        if compression is None:
            return "Insufficient history for a prior-window compression measurement."

        maximum_compression = float(params.get("maximum_compression_ratio", 0.85))
        expansion_ratio = v2.get("range_expansion_ratio")
        minimum_expansion = float(params.get("minimum_range_expansion", 1.5))

        was_compressed = compression <= maximum_compression or v2.get("prior_squeeze_state") == "in_squeeze"
        if not was_compressed:
            return f"No compression before this bar (prior ratio {compression:.2f})."
        if expansion_ratio is None or expansion_ratio < minimum_expansion:
            return f"No confirmed expansion (ratio {expansion_ratio})."

        relative_volume = v2.get("rolling_relative_volume")
        minimum_volume = float(params.get("minimum_relative_volume", 1.2))
        if relative_volume is None or relative_volume < minimum_volume:
            return "Volume confirmation failed."

        bar_return = v2.get("bar_return")
        if bar_return is None or bar_return == 0:
            return "Expansion has no directional resolution."

        low, high = float(candle["low"]), float(candle["high"])
        if bar_return > 0:
            return EntryPlan("long", _d(low), f"Squeeze released upward: prior compression {compression:.2f}, expansion {expansion_ratio:.2f}x.")
        return EntryPlan("short", _d(high), f"Squeeze released downward: prior compression {compression:.2f}, expansion {expansion_ratio:.2f}x.")


register_v2_family(
    VolatilitySqueezeBreakoutV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "volatility_squeeze_breakout_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "compression_breakout",
        "confirmation_structure": ["volatility_expansion", "relative_volume", "closing_confirmation"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "agnostic",
        "volatility_dependency": "requires_compression_then_expansion",
        "volume_dependency": "requires_confirmation",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["high_volatility"],
        "feature_dependencies": ["prior_atr_compression_ratio", "prior_squeeze_state", "range_expansion_ratio"],
    },
    parameter_grid={
        "maximum_compression_ratio": (Decimal("0.8"), Decimal("0.9")),
        "minimum_range_expansion": (Decimal("1.5"), Decimal("2.0")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "squeeze_release", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 9. Intraday Seasonality v2
# ===========================================================================

class IntradaySeasonalityV2(V2Strategy):
    architecture = "intraday_seasonality_v2"
    feature_groups = ("session", "relative_volume", "volatility")
    uses_absolute_targets = False
    supports_short = False  # Long-first; the short leg is a separate hypothesis.
    hypothesis = HypothesisSpec(
        title="Intraday Seasonality v2",
        market_behavior=(
            "Order flow is not uniform across the session. The open carries overnight "
            "repricing, midday thins out, and the close carries systematic rebalancing. Those "
            "windows can carry persistent, symbol-specific drift."
        ),
        hypothesis=(
            "For a given symbol and session window, historical same-time-of-day drift persists "
            "well enough that trading in its direction is profitable after costs."
        ),
        required_conditions=(
            "Current bar falls inside the configured session window.",
            "Same-time-of-day sample size at or above the reliability minimum.",
            "Historical same-time-of-day mean return in the traded direction beyond the threshold.",
        ),
        invalidation_conditions=(
            "Same-time-of-day sample too small to be reliable.",
            "Historical drift below the threshold or of the wrong sign.",
            "Drift measured on the validation window disagrees with the training window.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": (
                "Declared before any Phase 13 campaign ran. Seasonality is the family most "
                "exposed to overfitting, so its evidence is judged on the walk-forward "
                "validation window only, exactly like every other family."
            ),
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        window = str(params.get("session_window", "opening_hour"))
        in_window = {
            "opening_hour": bool(v2.get("is_opening_hour")),
            "lunch": bool(v2.get("is_lunch_period")),
            "power_hour": bool(v2.get("is_power_hour")),
        }.get(window)
        if in_window is None:
            return f"Unknown session window {window!r}."
        if not in_window:
            return f"Outside the {window} window."

        if not v2.get("same_time_of_day_volume_reliable"):
            return "Same-time-of-day sample too small to be reliable."

        mean_return = v2.get("same_time_of_day_mean_return")
        if mean_return is None:
            return "No same-time-of-day return statistics available."

        threshold = float(params.get("minimum_seasonal_drift", 0.0005))
        if mean_return < threshold:
            return f"Historical drift {mean_return:.5f} below the {threshold} long threshold."

        atr = v2.get("atr")
        if atr is None or atr <= 0:
            return "ATR unavailable for stop placement."
        close = float(candle["close"])
        stop_atr = float(params.get("stop_atr_multiple", 1.0))
        return EntryPlan("long", _d(close - stop_atr * atr), f"{window} historical drift {mean_return:.4%} over {v2.get('same_time_of_day_sample_count')} prior sessions.")


register_v2_family(
    IntradaySeasonalityV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "intraday_seasonality_v2",
        "direction_support": ["long"],
        "entry_structure": "time_of_day_entry",
        "confirmation_structure": ["session_window"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "roughly_daily",
        "trend_dependency": "agnostic",
        "volatility_dependency": "agnostic",
        "volume_dependency": "agnostic",
        "session_dependency": "open_only",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "hybrid",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": ["same_time_of_day_mean_return", "same_time_of_day_volume_reliable", "is_opening_hour"],
    },
    parameter_grid={
        "session_window": ("opening_hour", "power_hour"),
        "minimum_seasonal_drift": (Decimal("0.0005"), Decimal("0.0015")),
    },
    blocks={"entry": "session_window_drift", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 10. Market-Structure Breakout / Reversal v2
# ===========================================================================

class MarketStructureBreakV2(V2Strategy):
    architecture = "market_structure_break_v2"
    feature_groups = ("session", "market_structure", "relative_volume", "volatility")
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Market-Structure Breakout / Reversal v2",
        market_behavior=(
            "Confirmed swing highs and lows are where resting orders cluster. A decisive break "
            "of structure signals continuation; a break that immediately fails signals a sweep "
            "of that resting liquidity and reverses."
        ),
        hypothesis=(
            "A confirmed break of structure with volume continues, and a failed break (price "
            "trades through a swing and closes back on the original side) reverses. Which of "
            "the two a candidate trades is an explicit parameter, not an after-the-fact choice."
        ),
        required_conditions=(
            "At least one confirmed swing high and one confirmed swing low.",
            "Break mode: close beyond the swing level with volume confirmation.",
            "Sweep mode: bar traded through the swing level and closed back inside.",
        ),
        invalidation_conditions=(
            "No confirmed swing structure available.",
            "Volume does not confirm a structure break.",
            "Structure state is undetermined.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        swing_high = v2.get("last_confirmed_swing_high")
        swing_low = v2.get("last_confirmed_swing_low")
        if swing_high is None or swing_low is None:
            return "No confirmed swing structure yet."

        mode = str(params.get("structure_mode", "break"))
        low, high, close = float(candle["low"]), float(candle["high"]), float(candle["close"])

        if mode == "sweep":
            # Failed break / liquidity-sweep proxy -- explicitly a bar-data
            # proxy, not an order-book observation.
            if v2.get("failed_structure_break_up"):
                return EntryPlan("short", _d(high), f"Swept swing high {swing_high:.2f} and closed back below.")
            if v2.get("failed_structure_break_down"):
                return EntryPlan("long", _d(low), f"Swept swing low {swing_low:.2f} and closed back above.")
            return "No failed structure break on this bar."

        relative_volume = v2.get("rolling_relative_volume")
        minimum_volume = float(params.get("minimum_relative_volume", 1.2))
        if relative_volume is None or relative_volume < minimum_volume:
            return "Volume does not confirm the structure break."

        if v2.get("structure_break_up"):
            return EntryPlan("long", _d(swing_low), f"Broke structure above {swing_high:.2f} on {relative_volume:.2f}x volume.")
        if v2.get("structure_break_down"):
            return EntryPlan("short", _d(swing_high), f"Broke structure below {swing_low:.2f} on {relative_volume:.2f}x volume.")
        return "No structure break on this bar."


register_v2_family(
    MarketStructureBreakV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "market_structure_break_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "structure_break",
        "confirmation_structure": ["structure_state", "relative_volume", "closing_confirmation"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "expected_frequency_class": "roughly_daily",
        "trend_dependency": "benefits_from_trend",
        "volatility_dependency": "agnostic",
        "volume_dependency": "requires_confirmation",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "requires_confirmed_structure",
        "behavior_class": "hybrid",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": ["last_confirmed_swing_high", "last_confirmed_swing_low", "structure_break_up", "failed_structure_break_up"],
    },
    parameter_grid={
        "structure_mode": ("break", "sweep"),
        "minimum_relative_volume": (Decimal("1.2"), Decimal("1.5")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "market_structure_break_or_sweep", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 11. Cross-Sectional Momentum v2
# ===========================================================================

class CrossSectionalMomentumV2(V2Strategy):
    architecture = "cross_sectional_momentum_v2"
    feature_groups = ("session",)
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Cross-Sectional Momentum v2",
        market_behavior=(
            "Every other family in this registry judges one symbol against its own history. "
            "Cross-sectional momentum (Jegadeesh & Titman, 1993) judges a symbol against its "
            "peers at the same moment: relative strength within a universe is a more replicated "
            "market anomaly than any single-symbol technical pattern tested so far."
        ),
        hypothesis=(
            "A symbol ranking in the extreme percentiles of trailing relative strength within its "
            "own campaign universe continues to relatively out- or under-perform briefly."
        ),
        required_conditions=(
            "At least 3 peer symbols have a computable trailing return at this bar (ranking "
            "requires real breadth, not two symbols compared to each other).",
            "This symbol's percentile rank is at or beyond the configured extreme threshold.",
        ),
        invalidation_conditions=(
            "Rank reverts back toward the middle of the distribution.",
            "Too few peers have computable trailing returns to rank meaningfully.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        percentile = feature.get("cross_sectional_momentum_percentile")
        if percentile is None:
            return "Cross-sectional ranking unavailable at this bar (fewer than 3 peers with a computable trailing return)."

        upper = float(params.get("upper_percentile", 0.8))
        lower = float(params.get("lower_percentile", 0.2))
        low, high = float(candle["low"]), float(candle["high"])

        if percentile >= upper:
            return EntryPlan("long", _d(low), f"Cross-sectional percentile {percentile:.2f} at or above the {upper} strength threshold.")
        if percentile <= lower:
            return EntryPlan("short", _d(high), f"Cross-sectional percentile {percentile:.2f} at or below the {lower} weakness threshold.")
        return f"Cross-sectional percentile {percentile:.2f} is not extreme (needs >= {upper} or <= {lower})."


register_v2_family(
    CrossSectionalMomentumV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "cross_sectional_momentum_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "cross_sectional_rank_extreme",
        "confirmation_structure": ["none"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_hours",
        "expected_frequency_class": "multiple_per_session",
        "trend_dependency": "benefits_from_trend",
        "volatility_dependency": "agnostic",
        "volume_dependency": "agnostic",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": ["cross_sectional_momentum_percentile"],
    },
    parameter_grid={
        # Tightened from an earlier (0.7/0.8, 0.2/0.3) grid that traded the
        # top/bottom ~30% of the universe and produced 500+ pooled trades at
        # PF 0.07-0.19 -- research on transaction-cost drag and overtrading
        # (see Phase 13.10 PF research) points at that volume itself being a
        # likely cause: costs compound faster than a weak raw edge can beat
        # them. This grid trades only the single most extreme symbol (~1 of
        # 10) instead of the top/bottom third.
        "upper_percentile": (Decimal("0.9"), Decimal("0.95")),
        "lower_percentile": (Decimal("0.05"), Decimal("0.1")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "cross_sectional_rank_extreme", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# 12. Cross-Sectional Reversal v2
# ===========================================================================

class CrossSectionalReversalV2(V2Strategy):
    architecture = "cross_sectional_reversal_v2"
    feature_groups = ("session",)
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Cross-Sectional Reversal v2",
        market_behavior=(
            "Classic cross-sectional momentum (Jegadeesh & Titman, 1993) is validated on "
            "months-long formation and holding periods. At short horizons the documented effect "
            "is the OPPOSITE -- short-term reversal (Jegadeesh, 1990): a symbol that has recently "
            "outperformed its peers tends to give some of that back, and a recent relative laggard "
            "tends to bounce, before any longer-horizon momentum has a chance to establish."
        ),
        hypothesis=(
            "A symbol at the extreme weak end of its trailing cross-sectional rank bounces toward "
            "its peers; a symbol at the extreme strong end pulls back toward its peers -- the "
            "mirror image of CrossSectionalMomentumV2's entry logic, sharing the identical ranking "
            "computation so the two hypotheses are tested on exactly the same evidence."
        ),
        required_conditions=(
            "At least 3 peer symbols have a computable trailing return at this bar.",
            "This symbol's percentile rank is at or beyond the configured extreme threshold.",
        ),
        invalidation_conditions=(
            "The extreme rank persists or extends rather than reverting.",
            "Too few peers have computable trailing returns to rank meaningfully.",
        ),
        success_criteria={
            "minimum_trades": 30,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 2,
            "notes": "Declared before any Phase 13 campaign ran.",
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        percentile = feature.get("cross_sectional_momentum_percentile")
        if percentile is None:
            return "Cross-sectional ranking unavailable at this bar (fewer than 3 peers with a computable trailing return)."

        upper = float(params.get("upper_percentile", 0.8))
        lower = float(params.get("lower_percentile", 0.2))
        low, high = float(candle["low"]), float(candle["high"])

        # Inverted from CrossSectionalMomentumV2: weakness is bought (bet on
        # a bounce), strength is faded (bet on a pullback).
        if percentile <= lower:
            return EntryPlan("long", _d(low), f"Cross-sectional percentile {percentile:.2f} at or below the {lower} weakness threshold; betting on a short-term bounce.")
        if percentile >= upper:
            return EntryPlan("short", _d(high), f"Cross-sectional percentile {percentile:.2f} at or above the {upper} strength threshold; fading for a short-term pullback.")
        return f"Cross-sectional percentile {percentile:.2f} is not extreme (needs >= {upper} or <= {lower})."


register_v2_family(
    CrossSectionalReversalV2,
    dna={
        **_INTRADAY_DNA_COMMON,
        "family_architecture": "cross_sectional_reversal_v2",
        "direction_support": ["long", "short"],
        "entry_structure": "cross_sectional_rank_extreme",
        "confirmation_structure": ["none"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_hours",
        "expected_frequency_class": "multiple_per_session",
        "trend_dependency": "requires_range",
        "volatility_dependency": "agnostic",
        "volume_dependency": "agnostic",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "mean_reversion",
        "required_regime": ["range_bound", "normal_volatility"],
        "invalidation_regime": ["trending_up", "trending_down", "high_volatility"],
        "feature_dependencies": ["cross_sectional_momentum_percentile"],
    },
    parameter_grid={
        # Tightened from an earlier (0.7/0.8, 0.2/0.3) grid that traded the
        # top/bottom ~30% of the universe and produced 500+ pooled trades at
        # PF 0.07-0.19 -- research on transaction-cost drag and overtrading
        # (see Phase 13.10 PF research) points at that volume itself being a
        # likely cause: costs compound faster than a weak raw edge can beat
        # them. This grid trades only the single most extreme symbol (~1 of
        # 10) instead of the top/bottom third.
        "upper_percentile": (Decimal("0.9"), Decimal("0.95")),
        "lower_percentile": (Decimal("0.05"), Decimal("0.1")),
        "direction": ("long", "short"),
    },
    blocks={"entry": "cross_sectional_rank_extreme_reversal", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# Participant-flow research: Opening Repricing Flow v1
# ===========================================================================

class OpeningRepricingFlowV1(V2Strategy):
    """Test whether urgent opening repricing is accepted or absorbed.

    This is deliberately 30m-only. A 15m version would be a separate
    hypothesis with its own pre-declared timing and thresholds, not a free
    extra look at the same data.
    """

    architecture = "opening_repricing_flow_v1"
    feature_groups = ("session", "gap", "opening_range", "relative_volume", "vwap", "volatility")
    supported_timeframes = ("30m",)
    uses_absolute_targets = False
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Opening Repricing Flow v1",
        market_behavior=(
            "Overnight news and portfolio adjustment force participants to trade near the open. "
            "The first completed half-hour shows the price range created by that urgency; the next "
            "half-hour shows whether regular-session liquidity accepts the repricing or absorbs it."
        ),
        hypothesis=(
            "After a material overnight gap, elevated same-time-of-day volume plus either continued "
            "price acceptance beyond the first half-hour range or a decisive failure back through "
            "the session open predicts the next 30 to 240 minutes in the observed flow direction."
        ),
        required_conditions=(
            "30-minute bars and a completed first-half-hour range.",
            "An overnight gap of at least the pre-declared ATR-normalized size.",
            "A reliable same-time-of-day volume baseline from at least five prior sessions.",
            "Elevated volume during the confirmation half-hour.",
            "Acceptance mode: price extends beyond the opening range, holds the gap, and agrees with VWAP.",
            "Absorption mode: price rejects the gap through the session open and agrees with VWAP.",
        ),
        invalidation_conditions=(
            "The gap is too small to imply forced repricing.",
            "Same-time-of-day participation is ordinary or cannot be measured reliably.",
            "Price remains inside the opening range without accepting or rejecting the gap.",
            "The signal appears after the first hour, when the opening-flow interpretation is stale.",
        ),
        success_criteria={
            "minimum_trades": 50,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_symbols_with_positive_evidence": 3,
            "minimum_raw_signal_t_statistic": 2.0,
            "minimum_raw_edge_bps": 30.0,
            "notes": (
                "Declared before any campaign. Diagnose raw entry direction first; only a signal "
                "that survives drift adjustment and the 30 bps round trip may enter a campaign."
            ),
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        minutes_from_open = v2.get("minutes_from_open")
        if minutes_from_open is None or not (30 <= int(minutes_from_open) <= 60):
            return "Opening repricing is only measured during the second half-hour or first-hour close."

        if not v2.get("or30_complete"):
            return "The first 30-minute opening range is not complete."

        opening_high = v2.get("or30_high")
        opening_low = v2.get("or30_low")
        session_open = v2.get("session_open")
        gap_atr = v2.get("gap_atr")
        gap_direction = v2.get("gap_direction")
        if (
            opening_high is None
            or opening_low is None
            or session_open is None
            or gap_atr is None
            or gap_direction not in ("up", "down")
        ):
            return "Opening range or overnight repricing context is unavailable."

        minimum_gap_atr = float(params.get("minimum_gap_atr", 0.5))
        if abs(float(gap_atr)) < minimum_gap_atr:
            return f"Overnight gap {abs(float(gap_atr)):.2f} ATR is below the forced-flow threshold."

        if not v2.get("same_time_of_day_volume_reliable"):
            return "Same-time-of-day volume baseline has fewer than five prior sessions."
        relative_volume = v2.get("same_time_of_day_relative_volume")
        minimum_volume = float(params.get("minimum_relative_volume", 1.5))
        if relative_volume is None or float(relative_volume) < minimum_volume:
            return "Opening-flow participation is not elevated versus the same time of day."

        close = float(candle["close"])
        session_vwap = v2.get("session_vwap")
        if session_vwap is None:
            return "Session VWAP is unavailable for repricing acceptance."
        session_vwap = float(session_vwap)

        flow_mode = str(params.get("flow_mode", "acceptance"))
        gap_fill_fraction = float(v2.get("gap_fill_fraction") or 0.0)

        if flow_mode == "acceptance":
            maximum_fill = float(params.get("maximum_acceptance_fill_fraction", 0.25))
            if gap_fill_fraction > maximum_fill:
                return "Too much of the overnight gap has filled for repricing acceptance."
            if gap_direction == "up":
                if close <= float(opening_high) or close <= session_vwap:
                    return "Gap-up repricing has not been accepted beyond the opening range and VWAP."
                return EntryPlan(
                    "long",
                    _d(session_open),
                    (
                        f"Gap-up repricing accepted above the first-half-hour range on "
                        f"{float(relative_volume):.2f}x same-time volume."
                    ),
                )
            if close >= float(opening_low) or close >= session_vwap:
                return "Gap-down repricing has not been accepted beyond the opening range and VWAP."
            return EntryPlan(
                "short",
                _d(session_open),
                (
                    f"Gap-down repricing accepted below the first-half-hour range on "
                    f"{float(relative_volume):.2f}x same-time volume."
                ),
            )

        if flow_mode != "absorption":
            return f"Unknown opening repricing flow mode {flow_mode!r}."

        minimum_fill = float(params.get("minimum_absorption_fill_fraction", 0.5))
        if gap_fill_fraction < minimum_fill:
            return "The overnight gap has not retraced enough to show absorption."
        if gap_direction == "up":
            if close >= float(session_open) or close >= session_vwap:
                return "Gap-up flow has not failed through the session open and VWAP."
            return EntryPlan(
                "short",
                _d(max(float(opening_high), float(session_open))),
                (
                    f"Gap-up repricing absorbed through the session open on "
                    f"{float(relative_volume):.2f}x same-time volume."
                ),
            )
        if close <= float(session_open) or close <= session_vwap:
            return "Gap-down flow has not failed through the session open and VWAP."
        return EntryPlan(
            "long",
            _d(min(float(opening_low), float(session_open))),
            (
                f"Gap-down repricing absorbed through the session open on "
                f"{float(relative_volume):.2f}x same-time volume."
            ),
        )


register_v2_family(
    OpeningRepricingFlowV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "opening_repricing_flow_v1",
        "direction_support": ["long", "short"],
        "entry_structure": "opening_repricing_flow",
        "confirmation_structure": ["relative_volume", "vwap_alignment", "closing_confirmation", "session_window"],
        "exit_structure": ["fixed_r_multiple_target", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_hours",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "agnostic",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "open_only",
        "gap_dependency": "requires_gap",
        "market_structure_dependency": "uses_structure_context",
        "behavior_class": "hybrid",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": [
            "gap_atr",
            "gap_direction",
            "gap_fill_fraction",
            "or30_complete",
            "or30_high",
            "or30_low",
            "same_time_of_day_relative_volume",
            "same_time_of_day_volume_reliable",
            "session_open",
            "session_vwap",
        ],
        "evidence_confidence": "untested",
    },
    parameter_grid={
        # Eight pre-declared variants: both observed flow states, both trade
        # directions, and two economically meaningful gap-size floors.
        "flow_mode": ("acceptance", "absorption"),
        "minimum_gap_atr": (Decimal("0.5"), Decimal("1.0")),
        "minimum_relative_volume": (Decimal("1.5"),),
        "maximum_acceptance_fill_fraction": (Decimal("0.25"),),
        "minimum_absorption_fill_fraction": (Decimal("0.5"),),
        "direction": ("long", "short"),
    },
    blocks={"entry": "opening_repricing_acceptance_or_absorption", "exit": "r_multiple_or_session_close"},
)


# ===========================================================================
# Participant-flow research sequence (not campaign-eligible)
# ===========================================================================

class OvernightGapAcceptanceAbsorptionV1(V2Strategy):
    architecture = "overnight_gap_acceptance_absorption_v1"
    feature_groups = ("session", "gap", "relative_volume", "volatility")
    supported_timeframes = ("30m",)
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Overnight Gap Acceptance / Absorption v1",
        market_behavior=(
            "Overnight repricing forces investors to trade near the open. Elevated "
            "participation reveals whether regular-session liquidity accepts or absorbs it."
        ),
        hypothesis=(
            "A gap of at least 30 bps continues after the second half-hour when no more "
            "than 25% has filled, and reverses when at least 50% has filled, provided "
            "session-relative volume is at least 1.5."
        ),
        required_conditions=(
            "30-minute regular-session bars.",
            "Decision after the second half-hour.",
            "Absolute overnight gap of at least 30 bps.",
            "Session-relative volume of at least 1.5.",
        ),
        invalidation_conditions=(
            "The gap is between the acceptance and absorption thresholds.",
            "Participation is ordinary.",
            "Net evidence fails stressed execution costs.",
        ),
        success_criteria={
            "minimum_trades": 50,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "must_pass_locked_confirmation": True,
            "must_clear_stressed_observed_cost": True,
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        if int(v2.get("minutes_from_open") or -1) != 30:
            return "Gap acceptance is evaluated only after the second half-hour."
        gap = v2.get("gap_percent")
        if gap is None or abs(float(gap)) < float(params.get("minimum_gap_fraction", 0.003)):
            return "The overnight gap is below the forced-repricing threshold."
        relative_volume = v2.get("session_relative_volume")
        if relative_volume is None or float(relative_volume) < float(
            params.get("minimum_relative_volume", 1.5)
        ):
            return "Opening participation is below the required relative-volume threshold."
        fill = v2.get("gap_fill_fraction")
        if fill is None:
            return "Gap-fill state is unavailable."
        mode = str(params.get("flow_mode") or "acceptance")
        gap_direction = "long" if float(gap) > 0 else "short"
        if mode == "acceptance":
            if float(fill) > float(params.get("maximum_acceptance_fill_fraction", 0.25)):
                return "Too much of the gap filled for acceptance."
            direction = gap_direction
        elif mode == "absorption":
            if float(fill) < float(params.get("minimum_absorption_fill_fraction", 0.5)):
                return "Too little of the gap filled for absorption."
            direction = "short" if gap_direction == "long" else "long"
        else:
            return f"Unknown gap flow mode {mode!r}."
        stop = candle["low"] if direction == "long" else candle["high"]
        return EntryPlan(
            direction,
            _d(stop),
            f"Overnight gap {mode} confirmed on {float(relative_volume):.2f}x volume.",
        )


register_v2_family(
    OvernightGapAcceptanceAbsorptionV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "overnight_gap_acceptance_absorption_v1",
        "direction_support": ["long", "short"],
        "entry_structure": "overnight_gap_acceptance_absorption",
        "confirmation_structure": ["gap_fill", "relative_volume", "session_window"],
        "exit_structure": ["time_stop", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_minutes",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "agnostic",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "open_only",
        "gap_dependency": "requires_gap",
        "market_structure_dependency": "uses_structure_context",
        "behavior_class": "hybrid",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": [
            "gap_percent",
            "gap_fill_fraction",
            "session_relative_volume",
            "minutes_from_open",
        ],
        "evidence_confidence": "untested",
    },
    parameter_grid={
        "flow_mode": ("acceptance", "absorption"),
        "minimum_gap_fraction": (Decimal("0.003"),),
        "minimum_relative_volume": (Decimal("1.5"),),
        "maximum_acceptance_fill_fraction": (Decimal("0.25"),),
        "minimum_absorption_fill_fraction": (Decimal("0.5"),),
        "direction": ("both",),
        "max_holding_bars": (1,),
    },
    blocks={
        "entry": "overnight_gap_acceptance_absorption",
        "exit": "one_bar_time_stop",
    },
)


class VwapExecutionPressureV1(V2Strategy):
    architecture = "vwap_execution_pressure_v1"
    feature_groups = ("session", "relative_volume", "vwap", "volatility")
    supported_timeframes = ("30m",)
    supports_short = True
    hypothesis = HypothesisSpec(
        title="VWAP Execution Pressure v1",
        market_behavior=(
            "Scheduled institutional execution can keep price displaced from session VWAP "
            "while participation remains abnormally high."
        ),
        hypothesis=(
            "A close at least 10 bps from VWAP with session-relative volume of at least "
            "1.5 predicts one more 30-minute bar in the displacement direction."
        ),
        required_conditions=(
            "30-minute regular-session bars.",
            "Session VWAP is known at decision time.",
            "Session-relative volume is at least 1.5.",
            "Absolute VWAP displacement is at least 10 bps.",
        ),
        invalidation_conditions=(
            "Price returns to VWAP before entry.",
            "Participation falls below the threshold.",
            "Net evidence fails stressed execution costs.",
        ),
        success_criteria={
            "minimum_trades": 100,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "must_pass_locked_confirmation": True,
            "must_clear_stressed_observed_cost": True,
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        vwap = v2.get("session_vwap")
        relative_volume = v2.get("session_relative_volume")
        if vwap is None or float(vwap) <= 0:
            return "Session VWAP is unavailable."
        if relative_volume is None or float(relative_volume) < float(
            params.get("minimum_relative_volume", 1.5)
        ):
            return "Participation is below the execution-pressure threshold."
        displacement = (float(candle["close"]) - float(vwap)) / float(vwap)
        if abs(displacement) < float(params.get("minimum_vwap_displacement", 0.001)):
            return "VWAP displacement is below ten basis points."
        direction = "long" if displacement > 0 else "short"
        stop = candle["low"] if direction == "long" else candle["high"]
        return EntryPlan(
            direction,
            _d(stop),
            (
                f"Price is {displacement:.3%} from VWAP on "
                f"{float(relative_volume):.2f}x participation."
            ),
        )


register_v2_family(
    VwapExecutionPressureV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "vwap_execution_pressure_v1",
        "direction_support": ["long", "short"],
        "entry_structure": "vwap_execution_pressure",
        "confirmation_structure": ["relative_volume", "vwap_displacement"],
        "exit_structure": ["time_stop", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_minutes",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "agnostic",
        "volatility_dependency": "agnostic",
        "volume_dependency": "requires_elevated",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": [
            "session_vwap",
            "session_relative_volume",
        ],
        "evidence_confidence": "untested",
    },
    parameter_grid={
        "minimum_vwap_displacement": (Decimal("0.001"),),
        "minimum_relative_volume": (Decimal("1.5"),),
        "direction": ("both",),
        "max_holding_bars": (1,),
    },
    blocks={"entry": "vwap_execution_pressure", "exit": "one_bar_time_stop"},
)


class FirstToLastHalfHourMomentumV1(V2Strategy):
    architecture = "first_to_last_half_hour_momentum_v1"
    feature_groups = ("session", "volatility")
    supported_timeframes = ("30m",)
    supports_short = True
    # A decision after the penultimate bar enters the final half-hour. This is
    # an intentional family-specific close-auction horizon, not a relaxation
    # applied to any other strategy.
    minimum_entry_cutoff_minutes = 30
    hypothesis = HypothesisSpec(
        title="First-to-Last Half-Hour Market Momentum v1",
        market_behavior=(
            "Information and forced benchmark trading visible in the first half-hour can "
            "persist into the final half-hour as closing-auction demand completes."
        ),
        hypothesis=(
            "For broad market ETFs, the return from the prior close through the first "
            "half-hour predicts the direction of the final regular-session half-hour."
        ),
        required_conditions=(
            "30-minute regular-session bars.",
            "SPY or QQQ.",
            "A completed opening half-hour.",
            "Decision at the close of the penultimate half-hour.",
        ),
        invalidation_conditions=(
            "Opening return is below the pre-declared minimum magnitude.",
            "The signal is not available before the final bar opens.",
            "Chronological validation or locked forward confirmation fails.",
        ),
        success_criteria={
            "minimum_trades": 50,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_validation_t_statistic": 2.0,
            "maximum_false_discovery_rate": 0.1,
            "must_clear_stressed_observed_cost": True,
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        if int(v2.get("minutes_to_close") or -1) != 30:
            return "The final-half-hour signal is evaluated only after the penultimate bar."
        opening_return = v2.get("first_half_hour_return_from_prior_close")
        if opening_return is None:
            return "The first-half-hour return from the prior close is unavailable."
        threshold = float(params.get("minimum_opening_return", 0.002))
        if abs(float(opening_return)) < threshold:
            return "Opening information flow is below the pre-declared magnitude threshold."
        direction = "long" if float(opening_return) > 0 else "short"
        stop = candle["low"] if direction == "long" else candle["high"]
        return EntryPlan(
            direction,
            _d(stop),
            f"First-half-hour market return {float(opening_return):.3%} points toward the close.",
        )


register_v2_family(
    FirstToLastHalfHourMomentumV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "first_to_last_half_hour_momentum_v1",
        "direction_support": ["long", "short"],
        "entry_structure": "first_to_last_half_hour_flow",
        "confirmation_structure": ["opening_flow_direction", "session_window"],
        "exit_structure": ["session_close_forced", "stop_loss"],
        "holding_horizon_class": "intraday_minutes",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "roughly_daily",
        "trend_dependency": "agnostic",
        "volatility_dependency": "agnostic",
        "volume_dependency": "agnostic",
        "session_dependency": "power_hour_only",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": ["first_half_hour_return_from_prior_close", "minutes_to_close"],
        "evidence_confidence": "untested",
    },
    parameter_grid={
        "minimum_opening_return": (Decimal("0.001"), Decimal("0.002"), Decimal("0.003")),
        "direction": ("long", "short"),
        "max_holding_bars": (1,),
    },
    blocks={"entry": "first_to_last_half_hour_flow", "exit": "session_close"},
)


class SameSlotInstitutionalFlowV1(V2Strategy):
    architecture = "same_slot_institutional_flow_v1"
    feature_groups = ("session",)
    supported_timeframes = ("30m",)
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Cross-Sectional Same-Slot Institutional Flow v1",
        market_behavior=(
            "Large execution programs split orders through time and can repeat at the same "
            "intraday slot over successive sessions."
        ),
        hypothesis=(
            "The cross-sectional rank of a symbol's prior-session mean return at the next "
            "time slot predicts that next bar's open-to-close return."
        ),
        required_conditions=(
            "At least five prior sessions at the next slot.",
            "At least four simultaneously ranked symbols.",
            "Next-bar score computed before the next bar opens.",
        ),
        invalidation_conditions=(
            "The universe is too narrow for a cross-section.",
            "Chronological validation rank IC is non-positive.",
            "The long-short spread does not clear stressed turnover costs.",
        ),
        success_criteria={
            "minimum_trades": 100,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_validation_t_statistic": 2.0,
            "maximum_false_discovery_rate": 0.1,
            "must_clear_stressed_observed_cost": True,
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        percentile = feature.get("cross_sectional_next_same_slot_percentile")
        score = feature.get("cross_sectional_next_same_slot_score")
        if percentile is None or score is None:
            return "The next-slot cross-sectional rank is not measurable."
        upper = float(params.get("upper_percentile", 0.8))
        lower = float(params.get("lower_percentile", 0.2))
        if float(percentile) >= upper and float(score) > 0:
            return EntryPlan("long", _d(candle["low"]), "Positive next-slot flow ranks in the long tail.")
        if float(percentile) <= lower and float(score) < 0:
            return EntryPlan("short", _d(candle["high"]), "Negative next-slot flow ranks in the short tail.")
        return "Next-slot institutional-flow score is not in a directionally consistent tail."


register_v2_family(
    SameSlotInstitutionalFlowV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "same_slot_institutional_flow_v1",
        "direction_support": ["long", "short"],
        "entry_structure": "same_slot_cross_sectional_rank",
        "confirmation_structure": ["session_window"],
        "exit_structure": ["time_stop", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_minutes",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "multiple_per_session",
        "trend_dependency": "agnostic",
        "volatility_dependency": "agnostic",
        "volume_dependency": "agnostic",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": [
            "cross_sectional_next_same_slot_percentile",
            "cross_sectional_next_same_slot_score",
        ],
        "evidence_confidence": "untested",
    },
    parameter_grid={
        "cross_sectional_lookback_bars": (20, 40),
        "upper_percentile": (Decimal("0.8"),),
        "lower_percentile": (Decimal("0.2"),),
        "direction": ("long", "short"),
        "max_holding_bars": (1,),
    },
    blocks={"entry": "same_slot_cross_sectional_rank", "exit": "one_bar_time_stop"},
)


class GapDownAcceptanceShortConfirmationV1(OpeningRepricingFlowV1):
    """The single 93-signal lead frozen from dataset 36; no new parameter grid."""

    architecture = "gap_down_acceptance_short_confirmation_v1"
    supported_timeframes = ("30m",)
    hypothesis = HypothesisSpec(
        title="Gap-Down Acceptance Short — Locked Confirmation v1",
        market_behavior=(
            "Urgent negative overnight repricing may continue when the second half-hour "
            "accepts price below the opening range on elevated participation."
        ),
        hypothesis=(
            "The exact dataset-36 lead (short, acceptance, gap >= 0.5 ATR, same-slot "
            "relative volume >= 1.5) has positive one-bar excess return on untouched dates."
        ),
        required_conditions=(
            "A frozen dataset strictly later than dataset 36's observed window.",
            "No parameter changes from the original eight-variant screen.",
            "30-minute bars.",
        ),
        invalidation_conditions=(
            "Any reuse of dates observed by the source audit.",
            "Fewer than 50 forward signals.",
            "Failure to clear stressed observed costs after multiplicity control.",
        ),
        success_criteria={
            "minimum_trades": 50,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_confirmation_t_statistic": 2.0,
            "must_clear_stressed_observed_cost": True,
            "source_dataset_id": 36,
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        frozen = {
            **params,
            "flow_mode": "acceptance",
            "minimum_gap_atr": Decimal("0.5"),
            "minimum_relative_volume": Decimal("1.5"),
            "maximum_acceptance_fill_fraction": Decimal("0.25"),
            "direction": "short",
        }
        outcome = super().evaluate(candle, feature, v2, frozen)
        if isinstance(outcome, EntryPlan) and outcome.direction != "short":
            return "Locked confirmation permits the gap-down short lead only."
        return outcome


register_v2_family(
    GapDownAcceptanceShortConfirmationV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "gap_down_acceptance_short_confirmation_v1",
        "direction_support": ["short"],
        "entry_structure": "opening_repricing_flow",
        "confirmation_structure": ["relative_volume", "vwap_alignment", "session_window"],
        "exit_structure": ["time_stop", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_minutes",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "agnostic",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "open_only",
        "gap_dependency": "requires_gap",
        "market_structure_dependency": "uses_structure_context",
        "behavior_class": "momentum",
        "required_regime": ["any"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": ["gap_atr", "or30_low", "same_time_of_day_relative_volume", "session_vwap"],
        "evidence_confidence": "tested_mixed",
    },
    parameter_grid={
        "flow_mode": ("acceptance",),
        "minimum_gap_atr": (Decimal("0.5"),),
        "minimum_relative_volume": (Decimal("1.5"),),
        "maximum_acceptance_fill_fraction": (Decimal("0.25"),),
        "direction": ("short",),
        "max_holding_bars": (1,),
    },
    blocks={"entry": "locked_gap_down_acceptance_short", "exit": "one_bar_time_stop"},
)


class LiquidityShockReversalV1(V2Strategy):
    architecture = "liquidity_shock_reversal_v1"
    feature_groups = ("session", "relative_volume", "volatility", "microstructure")
    supported_timeframes = ("30m",)
    supports_short = True
    hypothesis = HypothesisSpec(
        title="Liquidity-Shock Reversal v1",
        market_behavior=(
            "A temporary liquidity demand shock creates an abnormal price/range move; "
            "quote-flow exhaustion identifies shocks likely to reverse."
        ),
        hypothesis=(
            "Bars with at least 2x normal volume and range reverse on the next bar when "
            "the price shock and terminal normalized quote imbalance disagree."
        ),
        required_conditions=(
            "Historical bid/ask prices and sizes.",
            "Bar-level normalized order-flow imbalance.",
            "At least twenty prior bars for range and volume baselines.",
        ),
        invalidation_conditions=(
            "Quote coverage is absent or IEX-only coverage is represented as consolidated.",
            "The move is market-wide rather than idiosyncratic.",
            "Forward validation fails after spread/slippage costs.",
        ),
        success_criteria={
            "minimum_trades": 100,
            "minimum_net_profit_factor": 1.2,
            "minimum_net_expectancy": 0,
            "minimum_validation_t_statistic": 2.0,
            "maximum_false_discovery_rate": 0.1,
            "must_clear_stressed_observed_cost": True,
        },
    )

    def evaluate(self, candle, feature, v2, params) -> EntryPlan | str:
        ofi = v2.get("normalized_order_flow_imbalance")
        if ofi is None:
            return "Quote-derived order-flow imbalance is unavailable."
        relative_volume = v2.get("same_time_of_day_relative_volume")
        range_expansion = v2.get("range_expansion_ratio")
        if relative_volume is None or float(relative_volume) < 2:
            return "Volume is below the 2x liquidity-shock threshold."
        if range_expansion is None or float(range_expansion) < 2:
            return "Range is below the 2x liquidity-shock threshold."
        bar_return = (float(candle["close"]) - float(candle["open"])) / float(candle["open"])
        if bar_return * float(ofi) >= 0:
            return "Price and quote flow agree; exhaustion is not visible."
        direction = "short" if bar_return > 0 else "long"
        stop = candle["high"] if direction == "short" else candle["low"]
        return EntryPlan(direction, _d(stop), "Abnormal price shock conflicts with terminal quote flow.")


register_v2_family(
    LiquidityShockReversalV1,
    dna={
        **_INTRADAY_DNA_COMMON,
        "strategy_version": "v1",
        "family_architecture": "liquidity_shock_reversal_v1",
        "direction_support": ["long", "short"],
        "entry_structure": "liquidity_shock_exhaustion",
        "confirmation_structure": ["relative_volume", "volatility_expansion", "quote_flow_exhaustion"],
        "exit_structure": ["time_stop", "stop_loss", "session_close_forced"],
        "holding_horizon_class": "intraday_minutes",
        "timeframe_class": "intraday_30m",
        "expected_frequency_class": "few_per_week",
        "trend_dependency": "agnostic",
        "volatility_dependency": "requires_expansion",
        "volume_dependency": "requires_elevated",
        "session_dependency": "any_session_time",
        "gap_dependency": "agnostic",
        "market_structure_dependency": "agnostic",
        "behavior_class": "mean_reversion",
        "required_regime": ["high_volatility"],
        "invalidation_regime": ["none_declared"],
        "feature_dependencies": [
            "normalized_order_flow_imbalance",
            "same_time_of_day_relative_volume",
            "range_expansion_ratio",
        ],
        "evidence_confidence": "untested",
    },
    parameter_grid={
        "minimum_relative_volume": (Decimal("2.0"),),
        "minimum_range_expansion": (Decimal("2.0"),),
        "direction": ("long", "short"),
        "max_holding_bars": (1,),
    },
    blocks={"entry": "liquidity_shock_quote_exhaustion", "exit": "one_bar_time_stop"},
)


NEGATIVE_SIGNAL_AUDIT_V2_ARCHITECTURES: tuple[str, ...] = (
    "opening_range_breakout_v2",
    "opening_range_fade_v2",
    "vwap_bounce_v2",
    "vwap_mean_reversion_v2",
    "gap_continuation_v2",
    "gap_fill_v2",
    "relative_volume_momentum_v2",
    "volatility_squeeze_breakout_v2",
    "intraday_seasonality_v2",
    "market_structure_break_v2",
    "cross_sectional_momentum_v2",
    "cross_sectional_reversal_v2",
)

NEGATIVE_SIGNAL_AUDIT_V2_ARCHITECTURES = (
    *NEGATIVE_SIGNAL_AUDIT_V2_ARCHITECTURES,
    "opening_repricing_flow_v1",
)

RESEARCH_ONLY_V2_ARCHITECTURES: tuple[str, ...] = (
    "overnight_gap_acceptance_absorption_v1",
    "vwap_execution_pressure_v1",
    "first_to_last_half_hour_momentum_v1",
    "same_slot_institutional_flow_v1",
)

CONFIRMATION_ONLY_V2_ARCHITECTURES: tuple[str, ...] = (
    "gap_down_acceptance_short_confirmation_v1",
)

BLOCKED_DATA_V2_ARCHITECTURES: tuple[str, ...] = (
    "liquidity_shock_reversal_v1",
)

ACTIVE_V2_ARCHITECTURES: tuple[str, ...] = ()

V2_ARCHITECTURES: tuple[str, ...] = (
    *NEGATIVE_SIGNAL_AUDIT_V2_ARCHITECTURES,
    *RESEARCH_ONLY_V2_ARCHITECTURES,
    *CONFIRMATION_ONLY_V2_ARCHITECTURES,
    *BLOCKED_DATA_V2_ARCHITECTURES,
)
