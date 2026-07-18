from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from itertools import product
from statistics import median
from typing import Any

from app.services.strategy import StrategyDecision


PHASE_2_FAMILY_VERSION = "research_strategy_families_v1"


@dataclass(frozen=True)
class StrategyFamilySpec:
    name: str
    slug: str
    observation_set: tuple[str, ...]
    observation: str
    hypothesis_template: str
    expected_behavior: str
    success_criteria: str
    falsification_criteria: str
    relevant_conditions: tuple[str, ...]
    entry_logic: str
    confirmation_logic: str
    exit_logic: str
    frequency_sensitive_parameters: tuple[str, ...]
    parameter_ranges: dict[str, tuple[Any, ...]]
    exploration_ranges: dict[str, tuple[Any, ...]]


_COMMON_RISK_RANGES = {
    "risk_reward": (1.4, 1.8, 2.2),
    "atr_multiplier": (1.2, 1.6, 2.0),
    "max_holding_bars": (6, 12, 18),
}


STRATEGY_FAMILY_SPECS: dict[str, StrategyFamilySpec] = {
    "Breakout": StrategyFamilySpec(
        name="Breakout",
        slug="breakout",
        observation_set=("range_compression", "breakout_distance", "volume_expansion", "close_location"),
        observation="A close exceeds a prior rolling high after measurable true-range compression with expanding participation.",
        hypothesis_template=(
            "On {scope}, closes at least {breakout_buffer:.4f} above the {breakout_lookback}-bar high after "
            "compression at or below {compression_ratio_max:.2f} and volume at or above {volume_ratio_min:.2f}x "
            "its rolling median will produce positive walk-forward expectancy under the unchanged validation policy."
        ),
        expected_behavior="Price follows through after escaping a compressed rolling range with participation confirmation.",
        success_criteria="At least one candidate passes every existing single-market gate; cluster support additionally requires the unchanged cross-market gates.",
        falsification_criteria="No candidate passes the unchanged gates, or apparent quality disappears out of sample, across regimes, or on another cluster member.",
        relevant_conditions=("range_compression", "positive_breakout_distance", "volume_expansion"),
        entry_logic="Close above the prior rolling high by a controlled buffer.",
        confirmation_logic="Prior true-range compression, rolling-median volume expansion, and a strong close location.",
        exit_logic="ATR/swing-risk stop, fixed reward/risk target, and bounded holding period.",
        frequency_sensitive_parameters=("breakout_lookback", "breakout_buffer", "compression_lookback", "compression_ratio_max"),
        parameter_ranges={
            "breakout_lookback": (12, 20, 30),
            "breakout_buffer": (0.0, 0.001, 0.0025),
            "compression_lookback": (4, 6, 8),
            "compression_ratio_max": (0.75, 0.9, 1.05),
            "volume_ratio_min": (0.9, 1.1, 1.3),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "breakout_lookback": (8, 40),
            "breakout_buffer": (0.0, 0.004),
            "compression_lookback": (3, 10),
            "compression_ratio_max": (0.65, 1.2),
            "volume_ratio_min": (0.75, 1.5),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.4),
            "max_holding_bars": (4, 24),
        },
    ),
    "Momentum": StrategyFamilySpec(
        name="Momentum",
        slug="momentum",
        observation_set=("short_return", "long_return", "return_acceleration", "momentum_persistence"),
        observation="Positive multi-horizon returns accelerate while momentum and directional close confirmation remain aligned.",
        hypothesis_template=(
            "On {scope}, {momentum_short_bars}-bar returns above {momentum_short_min:.4f}, long-horizon returns above "
            "{momentum_long_min:.4f}, and acceleration above {momentum_acceleration_min:.4f} will produce positive "
            "walk-forward expectancy under the unchanged validation policy."
        ),
        expected_behavior="An accelerating directional move persists beyond the signal bar.",
        success_criteria="At least one candidate passes every existing single-market gate with positive out-of-sample expectancy.",
        falsification_criteria="Acceleration-filtered candidates fail the existing trade-count or economic gates, or lose the edge out of sample.",
        relevant_conditions=("positive_return_persistence", "return_acceleration", "constructive_rsi"),
        entry_logic="A new short-horizon high during positive multi-horizon momentum.",
        confirmation_logic="Return acceleration plus RSI and optional MACD alignment.",
        exit_logic="ATR/swing-risk stop, fixed reward/risk target, and bounded momentum holding period.",
        frequency_sensitive_parameters=("momentum_short_bars", "momentum_short_min", "momentum_long_bars", "momentum_acceleration_min"),
        parameter_ranges={
            "momentum_short_bars": (3, 5, 8),
            "momentum_long_bars": (10, 15, 20),
            "momentum_short_min": (0.0025, 0.005, 0.008),
            "momentum_long_min": (0.004, 0.008, 0.014),
            "momentum_acceleration_min": (-0.002, 0.0, 0.002),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "momentum_short_bars": (2, 10),
            "momentum_long_bars": (8, 30),
            "momentum_short_min": (0.0, 0.012),
            "momentum_long_min": (0.0, 0.02),
            "momentum_acceleration_min": (-0.005, 0.005),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.4),
            "max_holding_bars": (4, 24),
        },
    ),
    "Pullback": StrategyFamilySpec(
        name="Pullback",
        slug="pullback",
        observation_set=("trend_alignment", "pullback_depth", "trend_reference_distance", "reclaim"),
        observation="A controlled retracement occurs inside an aligned moving-average trend and then reclaims direction.",
        hypothesis_template=(
            "On {scope}, pullbacks between {pullback_depth_min:.3f} and {pullback_depth_max:.3f} inside an aligned "
            "{trend_fast}/{trend_slow} EMA trend that close back in the trend direction will produce positive "
            "walk-forward expectancy under the unchanged validation policy."
        ),
        expected_behavior="The trend resumes after a bounded retracement rather than continuing to reverse.",
        success_criteria="At least one candidate passes every existing single-market gate without sacrificing walk-forward or regime stability.",
        falsification_criteria="The retracement cohort fails unchanged quality gates or only survives on its source asset.",
        relevant_conditions=("aligned_uptrend", "bounded_retracement", "directional_reclaim"),
        entry_logic="A bullish reclaim after a measured pullback from a rolling peak.",
        confirmation_logic="Fast EMA above slow EMA, close above slow EMA, and RSI inside a controlled band.",
        exit_logic="ATR/swing-risk stop below the pullback structure, fixed reward/risk target, and bounded holding period.",
        frequency_sensitive_parameters=("pullback_lookback", "pullback_depth_min", "pullback_depth_max", "reclaim_buffer"),
        parameter_ranges={
            "pullback_lookback": (12, 20, 30),
            "pullback_depth_min": (0.003, 0.008, 0.015),
            "pullback_depth_max": (0.025, 0.04, 0.06),
            "reclaim_buffer": (-0.004, 0.0, 0.003),
            "pullback_rsi_min": (38, 45, 50),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "pullback_lookback": (8, 40),
            "pullback_depth_min": (0.0, 0.025),
            "pullback_depth_max": (0.02, 0.09),
            "reclaim_buffer": (-0.008, 0.006),
            "pullback_rsi_min": (32, 55),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.4),
            "max_holding_bars": (4, 24),
        },
    ),
    "Mean Reversion": StrategyFamilySpec(
        name="Mean Reversion",
        slug="mean_reversion",
        observation_set=("overextension", "return_reversal", "distance_from_mean", "trend_exclusion"),
        observation="Price is measurably below its local mean, momentum is oversold, and the current bar reverses upward outside a strong trend.",
        hypothesis_template=(
            "On {scope}, downside extensions of at least {mean_reversion_distance_min:.3f} below EMA20 with RSI at or "
            "below {mean_reversion_rsi_max} and an upward reversal bar will revert with positive walk-forward expectancy "
            "under the unchanged validation policy."
        ),
        expected_behavior="An overextended move reverts toward its local mean after a measurable reversal signal.",
        success_criteria="At least one candidate passes every existing gate, including drawdown and regime stability.",
        falsification_criteria="Reversal candidates retain negative expectancy, excessive drawdown, or unstable trend-regime behavior under unchanged gates.",
        relevant_conditions=("downside_overextension", "oversold_momentum", "upward_reversal", "strong_trend_exclusion"),
        entry_logic="Bullish reversal bar after a downside extension from EMA20.",
        confirmation_logic="Oversold RSI and an upper bound on distance from EMA50 to avoid catching strong downtrends.",
        exit_logic="ATR/swing-risk stop, conservative fixed reward/risk target, and short bounded holding period.",
        frequency_sensitive_parameters=("mean_reversion_distance_min", "mean_reversion_rsi_max", "mean_reversion_trend_distance_max"),
        parameter_ranges={
            "mean_reversion_distance_min": (0.008, 0.015, 0.025),
            "mean_reversion_rsi_max": (32, 38, 44),
            "mean_reversion_trend_distance_max": (0.03, 0.05, 0.08),
            "reversal_body_min": (0.0, 0.001, 0.003),
            "risk_reward": (1.2, 1.5, 1.8),
            "atr_multiplier": (1.0, 1.4, 1.8),
            "max_holding_bars": (4, 8, 12),
        },
        exploration_ranges={
            "mean_reversion_distance_min": (0.004, 0.04),
            "mean_reversion_rsi_max": (25, 50),
            "mean_reversion_trend_distance_max": (0.02, 0.12),
            "reversal_body_min": (0.0, 0.006),
            "risk_reward": (1.0, 2.0),
            "atr_multiplier": (0.8, 2.2),
            "max_holding_bars": (3, 18),
        },
    ),
    "Volatility Expansion": StrategyFamilySpec(
        name="Volatility Expansion",
        slug="volatility_expansion",
        observation_set=("true_range_expansion", "volatility_level", "directional_close", "volume_expansion"),
        observation="Current true range expands relative to its rolling median and closes directionally with participation.",
        hypothesis_template=(
            "On {scope}, bars with true range at least {true_range_expansion_min:.2f}x the rolling median, close location "
            "at or above {directional_close_min:.2f}, and volume at or above {volume_ratio_min:.2f}x the median will "
            "continue with positive walk-forward expectancy under the unchanged validation policy."
        ),
        expected_behavior="A directional range expansion persists after volatility leaves its recent baseline.",
        success_criteria="At least one candidate passes every existing gate without excessive drawdown or unstable high-volatility behavior.",
        falsification_criteria="Expansion entries reverse, fail trade-count/economic gates, or collapse in high-volatility regimes.",
        relevant_conditions=("true_range_expansion", "directional_close", "volume_participation"),
        entry_logic="Directional close during a current-bar true-range expansion.",
        confirmation_logic="Expansion versus rolling median range, minimum realized volatility, and rolling-median volume.",
        exit_logic="ATR/swing-risk stop scaled to expansion, fixed reward/risk target, and bounded holding period.",
        frequency_sensitive_parameters=("range_baseline_lookback", "true_range_expansion_min", "volatility_20_min"),
        parameter_ranges={
            "range_baseline_lookback": (10, 20, 30),
            "true_range_expansion_min": (1.15, 1.35, 1.6),
            "directional_close_min": (0.6, 0.72, 0.82),
            "volatility_20_min": (0.004, 0.008, 0.012),
            "volume_ratio_min": (0.8, 1.0, 1.2),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "range_baseline_lookback": (6, 40),
            "true_range_expansion_min": (1.0, 2.0),
            "directional_close_min": (0.52, 0.9),
            "volatility_20_min": (0.0, 0.02),
            "volume_ratio_min": (0.65, 1.5),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.6),
            "max_holding_bars": (3, 24),
        },
    ),
    "Range Breakout": StrategyFamilySpec(
        name="Range Breakout",
        slug="range_breakout",
        observation_set=("consolidation_duration", "normalized_range_width", "range_boundary", "breakout_distance"),
        observation="Price exits a duration-defined narrow consolidation rather than merely exceeding a rolling high.",
        hypothesis_template=(
            "On {scope}, closes above a {range_lookback}-bar consolidation no wider than {range_width_max:.3f} of its "
            "median price by at least {range_break_buffer:.4f} will produce positive walk-forward expectancy under the "
            "unchanged validation policy."
        ),
        expected_behavior="A duration-defined consolidation resolves upward and follows through beyond its boundary.",
        success_criteria="At least one candidate passes every existing gate and does not depend on near-duplicate range definitions.",
        falsification_criteria="Range exits fail unchanged economic/stability gates or only differ cosmetically from rejected breakout candidates.",
        relevant_conditions=("bounded_consolidation_width", "minimum_consolidation_duration", "positive_range_exit"),
        entry_logic="Close beyond the high of a narrow, duration-defined prior consolidation.",
        confirmation_logic="Normalized range-width ceiling and optional rolling-median volume participation.",
        exit_logic="Range/ATR-aware stop, fixed reward/risk target, and bounded holding period.",
        frequency_sensitive_parameters=("range_lookback", "range_width_max", "range_break_buffer", "volume_ratio_min"),
        parameter_ranges={
            "range_lookback": (8, 12, 20),
            "range_width_max": (0.02, 0.035, 0.055),
            "range_break_buffer": (0.0, 0.001, 0.0025),
            "volume_ratio_min": (0.75, 0.95, 1.15),
            "range_stop_fraction": (0.25, 0.5, 0.75),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "range_lookback": (6, 30),
            "range_width_max": (0.012, 0.08),
            "range_break_buffer": (0.0, 0.004),
            "volume_ratio_min": (0.6, 1.4),
            "range_stop_fraction": (0.15, 0.9),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.4),
            "max_holding_bars": (4, 24),
        },
    ),
    "Continuation": StrategyFamilySpec(
        name="Continuation",
        slug="continuation",
        observation_set=("prior_impulse", "pause_depth", "pause_duration", "resumption_break"),
        observation="A prior impulse pauses without deep retracement and then resumes above the pause boundary.",
        hypothesis_template=(
            "On {scope}, impulses above {impulse_return_min:.4f} followed by a {pause_bars}-bar pause no deeper than "
            "{pause_depth_max:.3f} that resumes above the pause high will produce positive walk-forward expectancy "
            "under the unchanged validation policy."
        ),
        expected_behavior="Directional persistence resumes after a bounded pause rather than requiring a new long-range breakout.",
        success_criteria="At least one candidate passes every existing gate and retains stability across pause/impulse variants.",
        falsification_criteria="Resumption entries fail unchanged gates or quality disappears when pause depth/duration changes slightly.",
        relevant_conditions=("positive_impulse", "shallow_pause", "resumption_break", "aligned_trend"),
        entry_logic="Close above the pause high following a measurable prior impulse.",
        confirmation_logic="Bounded pause depth and fast/slow trend alignment.",
        exit_logic="ATR/swing-risk stop below pause structure, fixed reward/risk target, and bounded holding period.",
        frequency_sensitive_parameters=("impulse_bars", "impulse_return_min", "pause_bars", "pause_depth_max"),
        parameter_ranges={
            "impulse_bars": (3, 5, 8),
            "impulse_return_min": (0.004, 0.008, 0.014),
            "pause_bars": (2, 3, 5),
            "pause_depth_max": (0.01, 0.02, 0.035),
            "continuation_buffer": (0.0, 0.001, 0.0025),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "impulse_bars": (2, 12),
            "impulse_return_min": (0.0, 0.025),
            "pause_bars": (1, 8),
            "pause_depth_max": (0.006, 0.06),
            "continuation_buffer": (0.0, 0.005),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.4),
            "max_holding_bars": (4, 24),
        },
    ),
    "Gap": StrategyFamilySpec(
        name="Gap",
        slug="gap",
        observation_set=("gap_size", "gap_direction", "gap_continuation", "volume_expansion"),
        observation="The current open gaps above the prior close and the bar continues in the gap direction with participation.",
        hypothesis_template=(
            "On {scope}, upward gaps of at least {gap_size_min:.4f} that close at least {gap_continuation_min:.4f} "
            "above the open with volume at or above {volume_ratio_min:.2f}x its rolling median will produce positive "
            "walk-forward expectancy under the unchanged validation policy."
        ),
        expected_behavior="An opening displacement continues instead of immediately filling.",
        success_criteria="At least one candidate passes every existing gate with a sufficient gap-event sample.",
        falsification_criteria="The gap cohort fails the unchanged trade-count/economic gates or systematically fills rather than continues.",
        relevant_conditions=("positive_opening_gap", "same_bar_continuation", "volume_participation"),
        entry_logic="Positive open-to-prior-close gap followed by same-bar continuation above the open.",
        confirmation_logic="Minimum gap size, strong close location, and rolling-median volume participation.",
        exit_logic="Gap/ATR-aware stop, fixed reward/risk target, and bounded holding period.",
        frequency_sensitive_parameters=("gap_size_min", "gap_continuation_min", "volume_ratio_min"),
        parameter_ranges={
            "gap_size_min": (0.0025, 0.005, 0.01),
            "gap_continuation_min": (0.0, 0.002, 0.005),
            "gap_close_location_min": (0.55, 0.7, 0.82),
            "volume_ratio_min": (0.75, 0.95, 1.2),
            "gap_stop_fraction": (0.25, 0.5, 0.75),
            **_COMMON_RISK_RANGES,
        },
        exploration_ranges={
            "gap_size_min": (0.001, 0.02),
            "gap_continuation_min": (-0.001, 0.01),
            "gap_close_location_min": (0.5, 0.9),
            "volume_ratio_min": (0.6, 1.5),
            "gap_stop_fraction": (0.15, 0.9),
            "risk_reward": (1.2, 2.6),
            "atr_multiplier": (1.0, 2.4),
            "max_holding_bars": (3, 24),
        },
    ),
}


PHASE_2_FAMILY_NAMES = tuple(STRATEGY_FAMILY_SPECS)


def family_specification_payload() -> dict[str, Any]:
    return {
        "version": PHASE_2_FAMILY_VERSION,
        "families": [asdict(STRATEGY_FAMILY_SPECS[name]) for name in PHASE_2_FAMILY_NAMES],
        "shared_controls": {
            "allocation": {"exploitation": 0.70, "nearby_controlled_mutation": 0.20, "broader_exploration": 0.10},
            "execution_deduplication": True,
            "deterministic_generation": True,
            "validation_threshold_overrides": False,
            "simulation_only": True,
        },
    }


def family_parameter_combinations(strategy_family: str, *, role: str, seed: int = 0) -> list[dict[str, Any]]:
    spec = strategy_family_spec(strategy_family)
    ranges = spec.parameter_ranges if role == "core" else spec.exploration_ranges
    keys = tuple(ranges)
    rows = [dict(zip(keys, values, strict=True)) for values in product(*(ranges[key] for key in keys))]
    return sorted(
        rows,
        key=lambda row: sha256(f"{PHASE_2_FAMILY_VERSION}|{strategy_family}|{role}|{seed}|{repr(row)}".encode()).hexdigest(),
    )


def family_mutation_grid(strategy_family: str) -> list[tuple[str, list[Any]]]:
    spec = strategy_family_spec(strategy_family)
    return [(key, list(values)) for key, values in spec.parameter_ranges.items()]


def strategy_family_spec(strategy_family: str) -> StrategyFamilySpec:
    try:
        return STRATEGY_FAMILY_SPECS[strategy_family]
    except KeyError as exc:
        raise ValueError(f"unsupported Phase 2 strategy family: {strategy_family}") from exc


def family_observation_evidence(strategy_family: str, metrics: dict[str, Any]) -> dict[str, float]:
    values = {key: _number(value) for key, value in metrics.items()}
    atr = values.get("atr_ratio", 0.0)
    atr_p90 = values.get("atr_ratio_p90", 0.0)
    evidence = {
        "Breakout": {
            "breakout_follow_through": values.get("breakout_follow_through", 0.0),
            "volume_expansion_ratio": values.get("volume_expansion_ratio", 0.0),
            "momentum_persistence": values.get("momentum_persistence", 0.0),
        },
        "Momentum": {
            "momentum_persistence": values.get("momentum_persistence", 0.0),
            "return_autocorrelation_lag1": values.get("return_autocorrelation_lag1", 0.0),
            "trend_strength": values.get("trend_strength", 0.0),
        },
        "Pullback": {
            "trend_persistence": values.get("trend_persistence", 0.0),
            "median_pullback_depth": values.get("median_pullback_depth", 0.0),
            "momentum_persistence": values.get("momentum_persistence", 0.0),
        },
        "Mean Reversion": {
            "mean_reversion_score": values.get("mean_reversion_score", 0.0),
            "reversal_rate": values.get("reversal_rate", 0.0),
            "trend_strength": values.get("trend_strength", 0.0),
        },
        "Volatility Expansion": {
            "atr_tail_ratio": atr_p90 / atr if atr > 0 else 0.0,
            "realized_volatility": values.get("realized_volatility", 0.0),
            "volume_expansion_ratio": values.get("volume_expansion_ratio", 0.0),
        },
        "Range Breakout": {
            "breakout_follow_through": values.get("breakout_follow_through", 0.0),
            "mean_reversion_score": values.get("mean_reversion_score", 0.0),
            "atr_ratio": atr,
        },
        "Continuation": {
            "trend_persistence": values.get("trend_persistence", 0.0),
            "momentum_persistence": values.get("momentum_persistence", 0.0),
            "return_autocorrelation_lag1": values.get("return_autocorrelation_lag1", 0.0),
        },
        "Gap": {
            "gap_frequency": values.get("gap_frequency", 0.0),
            "breakout_follow_through": values.get("breakout_follow_through", 0.0),
            "volume_expansion_ratio": values.get("volume_expansion_ratio", 0.0),
        },
    }
    return evidence[strategy_family]


def family_observation_score(strategy_family: str, metrics: dict[str, Any]) -> float:
    evidence = family_observation_evidence(strategy_family, metrics)
    if strategy_family == "Breakout":
        score = evidence["breakout_follow_through"] * 0.50 + min(2.0, evidence["volume_expansion_ratio"]) / 2 * 0.30 + evidence["momentum_persistence"] * 0.20
    elif strategy_family == "Momentum":
        score = evidence["momentum_persistence"] * 0.55 + max(0.0, evidence["return_autocorrelation_lag1"]) * 0.25 + min(1.0, evidence["trend_strength"] * 8) * 0.20
    elif strategy_family == "Pullback":
        score = min(1.0, evidence["trend_persistence"] / 6) * 0.50 + min(1.0, evidence["median_pullback_depth"] / 0.04) * 0.20 + evidence["momentum_persistence"] * 0.30
    elif strategy_family == "Mean Reversion":
        score = min(1.0, evidence["mean_reversion_score"] * 4) * 0.55 + evidence["reversal_rate"] * 0.35 + max(0.0, 1 - min(1.0, evidence["trend_strength"] * 10)) * 0.10
    elif strategy_family == "Volatility Expansion":
        score = min(1.0, evidence["atr_tail_ratio"] / 2) * 0.45 + min(1.0, evidence["realized_volatility"] / 0.03) * 0.25 + min(2.0, evidence["volume_expansion_ratio"]) / 2 * 0.30
    elif strategy_family == "Range Breakout":
        score = evidence["breakout_follow_through"] * 0.50 + min(1.0, evidence["mean_reversion_score"] * 4) * 0.25 + min(1.0, evidence["atr_ratio"] / 0.03) * 0.25
    elif strategy_family == "Continuation":
        score = min(1.0, evidence["trend_persistence"] / 6) * 0.40 + evidence["momentum_persistence"] * 0.40 + max(0.0, evidence["return_autocorrelation_lag1"]) * 0.20
    else:
        score = min(1.0, evidence["gap_frequency"] / 0.05) * 0.45 + evidence["breakout_follow_through"] * 0.30 + min(2.0, evidence["volume_expansion_ratio"]) / 2 * 0.25
    return round(max(0.0, min(1.0, score)), 6)


def strategy_family_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    family = str(params.get("phase2_strategy_family") or "")
    strategy_family_spec(family)
    candles = recent_candles
    if not candles or candles[-1].get("timestamp") != candle.get("timestamp"):
        candles = [*candles, candle]
    decision = {
        "Breakout": _breakout_signal,
        "Momentum": _momentum_signal,
        "Pullback": _pullback_signal,
        "Mean Reversion": _mean_reversion_signal,
        "Volatility Expansion": _volatility_expansion_signal,
        "Range Breakout": _range_breakout_signal,
        "Continuation": _continuation_signal,
        "Gap": _gap_signal,
    }[family](candle, feature, candles, params)
    if not decision[0]:
        return _avoid(str(decision[1]))
    stop = _family_stop(family, candle, candles, params, dict(decision[2]))
    close = _decimal(candle.get("close"))
    if stop is None or close is None or stop <= 0 or stop >= close:
        return _avoid("Family risk rule did not produce a valid long-only stop.")
    rr = Decimal(str(params.get("risk_reward", 1.8)))
    take_profit = close + ((close - stop) * rr)
    measurements = ", ".join(f"{key}={value:.6f}" for key, value in sorted(dict(decision[2]).items()))
    return StrategyDecision(
        "setup",
        (_decimal(candle.get("low")) or close, _decimal(candle.get("high")) or close),
        stop,
        take_profit,
        rr,
        [f"{family} {PHASE_2_FAMILY_VERSION} conditions passed.", measurements],
    )


def _breakout_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    lookback = int(params["breakout_lookback"])
    compression_lookback = int(params["compression_lookback"])
    if len(candles) < max(lookback + 2, 22):
        return False, "Breakout requires more rolling history.", {}
    prior = candles[-lookback - 1 : -1]
    prior_high = max(_float(row["high"]) for row in prior)
    close = _float(candle["close"])
    breakout_distance = close / prior_high - 1 if prior_high else 0.0
    prior_ranges = _true_ranges(candles[-22:-1])[-20:]
    recent_ranges = prior_ranges[-compression_lookback:]
    compression = median(recent_ranges) / median(prior_ranges) if prior_ranges and median(prior_ranges) else 99.0
    volume_ratio = _volume_ratio(candles, 20)
    close_location = _close_location(candle)
    passed = (
        breakout_distance >= float(params["breakout_buffer"])
        and compression <= float(params["compression_ratio_max"])
        and volume_ratio >= float(params["volume_ratio_min"])
        and close_location >= 0.55
    )
    return passed, "Breakout distance, compression, volume, or close-location confirmation failed.", {"breakout_distance": breakout_distance, "compression_ratio": compression, "volume_ratio": volume_ratio, "close_location": close_location}


def _momentum_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    short = int(params["momentum_short_bars"])
    long = int(params["momentum_long_bars"])
    if len(candles) < max(long + 2, short * 2 + 2):
        return False, "Momentum requires more multi-horizon history.", {}
    close = _float(candle["close"])
    short_return = close / _float(candles[-short - 1]["close"]) - 1
    long_return = close / _float(candles[-long - 1]["close"]) - 1
    prior_short_end = _float(candles[-short - 1]["close"])
    prior_short_start = _float(candles[-short * 2 - 1]["close"])
    prior_short_return = prior_short_end / prior_short_start - 1
    acceleration = short_return - prior_short_return
    rsi = _number(feature.get("rsi_14"))
    macd = _number(feature.get("macd"))
    macd_signal = _number(feature.get("macd_signal"))
    price_confirmation = close >= max(_float(row["high"]) for row in candles[-short:-1])
    passed = (
        short_return >= float(params["momentum_short_min"])
        and long_return >= float(params["momentum_long_min"])
        and acceleration >= float(params["momentum_acceleration_min"])
        and rsi >= 52
        and macd >= macd_signal
        and price_confirmation
    )
    return passed, "Multi-horizon return, acceleration, RSI/MACD, or price confirmation failed.", {"short_return": short_return, "long_return": long_return, "acceleration": acceleration, "rsi": rsi}


def _pullback_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    lookback = int(params["pullback_lookback"])
    if len(candles) < max(lookback + 1, 52):
        return False, "Pullback requires trend and peak history.", {}
    fast = _number(feature.get("ema_20")) or _ema(candles, 20)
    slow = _number(feature.get("ema_50")) or _ema(candles, 50)
    close = _float(candle["close"])
    peak = max(_float(row["high"]) for row in candles[-lookback:])
    depth = (peak - close) / peak if peak else 0.0
    rsi = _number(feature.get("rsi_14"))
    previous_close = _float(candles[-2]["close"])
    reclaim_level = (fast or close) * (1 + float(params["reclaim_buffer"]))
    passed = (
        fast is not None
        and slow is not None
        and fast > slow
        and close > slow
        and float(params["pullback_depth_min"]) <= depth <= float(params["pullback_depth_max"])
        and close >= reclaim_level
        and close > previous_close
        and float(params["pullback_rsi_min"]) <= rsi <= 72
    )
    return passed, "Trend alignment, pullback depth, reclaim, or RSI confirmation failed.", {"pullback_depth": depth, "ema_separation": ((fast or 0) / slow - 1) if slow else 0.0, "rsi": rsi, "reclaim_distance": close / reclaim_level - 1 if reclaim_level else 0.0}


def _mean_reversion_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    if len(candles) < 52:
        return False, "Mean reversion requires local-mean history.", {}
    close = _float(candle["close"])
    open_price = _float(candle["open"])
    previous_close = _float(candles[-2]["close"])
    ema20 = _number(feature.get("ema_20")) or (_ema(candles, 20) or 0.0)
    ema50 = _number(feature.get("ema_50")) or (_ema(candles, 50) or 0.0)
    distance20 = (close - ema20) / ema20 if ema20 else 0.0
    distance50 = abs((close - ema50) / ema50) if ema50 else 99.0
    rsi = _number(feature.get("rsi_14"))
    reversal_body = (close - open_price) / open_price if open_price else 0.0
    passed = (
        distance20 <= -float(params["mean_reversion_distance_min"])
        and distance50 <= float(params["mean_reversion_trend_distance_max"])
        and rsi <= float(params["mean_reversion_rsi_max"])
        and reversal_body >= float(params["reversal_body_min"])
        and close > previous_close
    )
    return passed, "Overextension, trend exclusion, RSI, or reversal confirmation failed.", {"distance_from_ema20": distance20, "distance_from_ema50_abs": distance50, "rsi": rsi, "reversal_body": reversal_body}


def _volatility_expansion_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    lookback = int(params["range_baseline_lookback"])
    if len(candles) < lookback + 2:
        return False, "Volatility expansion requires baseline range history.", {}
    ranges = _true_ranges(candles[-lookback - 2 :])
    baseline = median(ranges[-lookback - 1 : -1])
    expansion = ranges[-1] / baseline if baseline else 0.0
    close_location = _close_location(candle)
    volatility = _number(feature.get("volatility_20"))
    volume_ratio = _volume_ratio(candles, 20)
    returns_1 = _number(feature.get("returns_1"))
    passed = (
        expansion >= float(params["true_range_expansion_min"])
        and close_location >= float(params["directional_close_min"])
        and volatility >= float(params["volatility_20_min"])
        and volume_ratio >= float(params["volume_ratio_min"])
        and returns_1 > 0
    )
    return passed, "True-range expansion, directional close, volatility, volume, or positive-return confirmation failed.", {"true_range_expansion": expansion, "close_location": close_location, "volatility_20": volatility, "volume_ratio": volume_ratio}


def _range_breakout_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    lookback = int(params["range_lookback"])
    if len(candles) < lookback + 2:
        return False, "Range breakout requires consolidation history.", {}
    consolidation = candles[-lookback - 1 : -1]
    range_high = max(_float(row["high"]) for row in consolidation)
    range_low = min(_float(row["low"]) for row in consolidation)
    center = median(_float(row["close"]) for row in consolidation)
    width = (range_high - range_low) / center if center else 99.0
    close = _float(candle["close"])
    break_distance = close / range_high - 1 if range_high else 0.0
    volume_ratio = _volume_ratio(candles, 20)
    passed = width <= float(params["range_width_max"]) and break_distance >= float(params["range_break_buffer"]) and volume_ratio >= float(params["volume_ratio_min"])
    return passed, "Consolidation width, range-boundary break, or volume confirmation failed.", {"normalized_range_width": width, "range_break_distance": break_distance, "volume_ratio": volume_ratio, "consolidation_bars": float(lookback)}


def _continuation_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    impulse_bars = int(params["impulse_bars"])
    pause_bars = int(params["pause_bars"])
    required = impulse_bars + pause_bars + 2
    if len(candles) < max(required, 52):
        return False, "Continuation requires impulse, pause, and trend history.", {}
    pause = candles[-pause_bars - 1 : -1]
    impulse_end = _float(candles[-pause_bars - 1]["close"])
    impulse_start = _float(candles[-pause_bars - impulse_bars - 1]["close"])
    impulse_return = impulse_end / impulse_start - 1 if impulse_start else 0.0
    pause_high = max(_float(row["high"]) for row in pause)
    pause_low = min(_float(row["low"]) for row in pause)
    pause_depth = (impulse_end - pause_low) / impulse_end if impulse_end else 99.0
    close = _float(candle["close"])
    resumption = close / pause_high - 1 if pause_high else 0.0
    fast = _number(feature.get("ema_20")) or _ema(candles, 20)
    slow = _number(feature.get("ema_50")) or _ema(candles, 50)
    passed = (
        impulse_return >= float(params["impulse_return_min"])
        and pause_depth <= float(params["pause_depth_max"])
        and resumption >= float(params["continuation_buffer"])
        and fast is not None
        and slow is not None
        and fast > slow
    )
    return passed, "Impulse, pause depth, resumption boundary, or trend alignment failed.", {"impulse_return": impulse_return, "pause_depth": pause_depth, "resumption_distance": resumption, "ema_separation": ((fast or 0) / slow - 1) if slow else 0.0}


def _gap_signal(candle: dict[str, Any], feature: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any]) -> tuple[bool, str, dict[str, float]]:
    if len(candles) < 22:
        return False, "Gap requires prior-close and volume history.", {}
    previous_close = _float(candles[-2]["close"])
    open_price = _float(candle["open"])
    close = _float(candle["close"])
    gap_size = open_price / previous_close - 1 if previous_close else 0.0
    continuation = close / open_price - 1 if open_price else 0.0
    close_location = _close_location(candle)
    volume_ratio = _volume_ratio(candles, 20)
    passed = (
        gap_size >= float(params["gap_size_min"])
        and continuation >= float(params["gap_continuation_min"])
        and close_location >= float(params["gap_close_location_min"])
        and volume_ratio >= float(params["volume_ratio_min"])
    )
    return passed, "Gap size, same-bar continuation, close location, or volume confirmation failed.", {"gap_size": gap_size, "gap_continuation": continuation, "close_location": close_location, "volume_ratio": volume_ratio}


def _family_stop(strategy_family: str, candle: dict[str, Any], candles: list[dict[str, Any]], params: dict[str, Any], measurements: dict[str, float]) -> Decimal | None:
    close = _decimal(candle.get("close"))
    if close is None:
        return None
    ranges = _true_ranges(candles[-15:])
    atr = Decimal(str(median(ranges[-14:]))) if ranges else close * Decimal("0.01")
    atr_stop = close - atr * Decimal(str(params.get("atr_multiplier", 1.6)))
    lookback = min(len(candles), max(2, int(params.get("swing_lookback", 5))))
    swing_stop = min(_decimal(row.get("low")) or close for row in candles[-lookback:])
    if strategy_family == "Range Breakout":
        width = Decimal(str(measurements.get("normalized_range_width", 0))) * close
        structural = close - width * Decimal(str(params.get("range_stop_fraction", 0.5)))
        return min(atr_stop, structural)
    if strategy_family == "Gap":
        gap = Decimal(str(max(0.0, measurements.get("gap_size", 0)))) * close
        structural = close - gap * Decimal(str(params.get("gap_stop_fraction", 0.5)))
        return min(atr_stop, structural)
    if strategy_family in {"Pullback", "Continuation", "Mean Reversion"}:
        return min(atr_stop, swing_stop)
    return atr_stop


def _ema(candles: list[dict[str, Any]], period: int) -> float | None:
    if len(candles) < period:
        return None
    closes = [_float(row["close"]) for row in candles]
    value = sum(closes[:period]) / period
    multiplier = 2 / (period + 1)
    for close in closes[period:]:
        value = (close - value) * multiplier + value
    return value


def _true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    ranges = []
    for index in range(1, len(candles)):
        high = _float(candles[index]["high"])
        low = _float(candles[index]["low"])
        previous_close = _float(candles[index - 1]["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return ranges


def _volume_ratio(candles: list[dict[str, Any]], lookback: int) -> float:
    if len(candles) < 2:
        return 0.0
    prior = [_float(row.get("volume")) for row in candles[-lookback - 1 : -1] if row.get("volume") is not None]
    baseline = median(prior) if prior else 0.0
    return _float(candles[-1].get("volume")) / baseline if baseline else 0.0


def _close_location(candle: dict[str, Any]) -> float:
    high = _float(candle.get("high"))
    low = _float(candle.get("low"))
    return (_float(candle.get("close")) - low) / (high - low) if high > low else 0.5


def _avoid(reason: str) -> StrategyDecision:
    return StrategyDecision("avoid", None, None, None, None, [reason])


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and result not in {float("inf"), float("-inf")} else 0.0


def _float(value: Any) -> float:
    return _number(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
