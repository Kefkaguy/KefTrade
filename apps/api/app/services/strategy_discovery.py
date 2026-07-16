from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from itertools import product
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.backtester import count_setup_opportunities, run_backtest
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.strategy import BASE_PARAMETERS, StrategyDecision, StrategyDefinition
from app.services.strategy_research import (
    build_context_by_time,
    calculate_feature_correlations,
    compare_by_regime,
    compare_by_year,
    finite_metric,
    paper_readiness_report,
    score_metrics,
)
from app.services.research_learning import mutation_options


DISCOVERY_VERSION = "strategy_discovery_v1"
SAFETY_STATEMENT = "Research-only deterministic discovery. simulation_only=TRUE; no broker routing, live trading, margin, leverage, shorting, or automatic execution."


@dataclass(frozen=True)
class RuleBlock:
    id: str
    category: str
    label: str
    parameters: dict[str, Any]
    complexity: int = 1
    incompatible_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryCandidate:
    candidate_id: str
    family_id: str
    parent_candidate_id: str | None
    generation: int
    blocks: dict[str, str]
    parameters: dict[str, Any]
    complexity: int
    canonical_key: str


RULE_LIBRARY: dict[str, list[RuleBlock]] = {
    "trend": [
        RuleBlock("ema_20_50", "trend", "EMA20 above EMA50", {"trend_method": "ema", "trend_fast": 20, "trend_slow": 50}),
        RuleBlock("ema_50_200", "trend", "EMA50 above EMA200", {"trend_method": "ema", "trend_fast": 50, "trend_slow": 200}),
        RuleBlock("sma_20_50", "trend", "SMA20 above SMA50", {"trend_method": "sma", "trend_fast": 20, "trend_slow": 50}),
        RuleBlock("supertrend_proxy", "trend", "SuperTrend proxy", {"trend_method": "ema", "trend_fast": 20, "trend_slow": 100, "trend_requires_positive_returns": True}, 2),
        RuleBlock("vwap_reclaim", "trend", "VWAP reclaim", {"trend_method": "vwap", "trend_fast": 20, "trend_slow": 50}, 2),
    ],
    "momentum": [
        RuleBlock("rsi_55", "momentum", "RSI above 55", {"momentum": "rsi", "rsi_min": 55}),
        RuleBlock("rsi_60", "momentum", "RSI above 60", {"momentum": "rsi", "rsi_min": 60}),
        RuleBlock("macd_bullish", "momentum", "MACD bullish", {"momentum": "macd"}),
        RuleBlock("roc_positive", "momentum", "ROC positive", {"momentum": "roc", "returns_5_min": 0.01}),
        RuleBlock("adx_proxy", "momentum", "ADX trend proxy", {"momentum": "adx_proxy", "returns_5_min": 0.015}, 2),
        RuleBlock("stochastic_proxy", "momentum", "Stochastic pullback proxy", {"momentum": "stochastic_proxy", "rsi_min": 45, "rsi_max": 72}, 2),
        RuleBlock("rsi_oversold", "momentum", "RSI oversold", {"momentum": "rsi_oversold", "rsi_oversold": 42}),
    ],
    "volatility": [
        RuleBlock("atr_stop_ready", "volatility", "ATR stop ready", {"volatility": "atr", "atr_multiplier": 1.5}),
        RuleBlock("bollinger_reversion", "volatility", "Bollinger mean reversion proxy", {"volatility": "bollinger", "distance_from_ema_20_max": -0.015}, 2, ("breakout", "trend_continuation")),
        RuleBlock("keltner_expansion", "volatility", "Keltner expansion proxy", {"volatility": "keltner", "volatility_20_min": 0.01}, 2),
        RuleBlock("donchian_range", "volatility", "Donchian range", {"volatility": "donchian", "breakout_lookback": 20}, 2),
    ],
    "volume": [
        RuleBlock("relative_volume", "volume", "Relative volume", {"volume": "relative", "volume_change_min": 0.0}),
        RuleBlock("volume_spike", "volume", "Volume spike", {"volume": "spike", "volume_change_min": 0.15}, 2),
        RuleBlock("obv_proxy", "volume", "OBV proxy", {"volume": "obv_proxy", "volume_change_min": -0.05}, 2),
    ],
    "entry": [
        RuleBlock("breakout", "entry", "Breakout entry", {"entry": "breakout", "breakout_lookback": 20}),
        RuleBlock("pullback", "entry", "Pullback entry", {"entry": "pullback", "entry_distance_to_ema20_max": 0.035}),
        RuleBlock("mean_reversion", "entry", "Mean reversion entry", {"entry": "mean_reversion", "rsi_oversold": 38}, 2, ("rsi_60", "breakout")),
        RuleBlock("trend_continuation", "entry", "Trend continuation entry", {"entry": "trend_continuation", "returns_5_min": 0.012}),
        RuleBlock("opening_range_proxy", "entry", "Opening range proxy", {"entry": "opening_range_proxy", "breakout_lookback": 8}, 2),
        RuleBlock("gap_proxy", "entry", "Gap continuation proxy", {"entry": "gap_proxy", "returns_5_min": 0.02}, 2),
    ],
    "exit": [
        RuleBlock("fixed_rr_15", "exit", "Fixed 1.5R target", {"exit": "fixed_rr", "risk_reward": 1.5, "swing_lookback": 5}),
        RuleBlock("fixed_rr_20", "exit", "Fixed 2R target", {"exit": "fixed_rr", "risk_reward": 2.0, "swing_lookback": 8}),
        RuleBlock("atr_stop_20", "exit", "ATR stop with 2R target", {"exit": "atr_stop", "risk_reward": 2.0, "atr_multiplier": 2.0, "swing_lookback": 8}),
        RuleBlock("trailing_proxy", "exit", "Trailing stop proxy", {"exit": "trailing_proxy", "risk_reward": 1.8, "atr_multiplier": 1.5, "max_holding_bars": 18}, 2),
        RuleBlock("time_exit_12", "exit", "12-bar timeout", {"exit": "time_exit", "risk_reward": 1.6, "max_holding_bars": 12, "swing_lookback": 5}),
        RuleBlock("ema_exit_proxy", "exit", "EMA exit proxy", {"exit": "ema_exit_proxy", "risk_reward": 1.6, "max_holding_bars": 20, "swing_lookback": 5}, 2),
    ],
}


def rule_library_payload() -> dict[str, Any]:
    return {
        "version": DISCOVERY_VERSION,
        "safety": SAFETY_STATEMENT,
        "categories": {category: [asdict(block) for block in blocks] for category, blocks in RULE_LIBRARY.items()},
    }


def generate_discovery_candidates(
    max_candidates: int = 1000,
    parent: DiscoveryCandidate | None = None,
) -> list[DiscoveryCandidate]:
    categories = ["trend", "momentum", "volatility", "volume", "entry", "exit"]
    generated: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for blocks in product(*(RULE_LIBRARY[category] for category in categories)):
        block_ids = {block.id for block in blocks}
        if any(incompatible in block_ids for block in blocks for incompatible in block.incompatible_with):
            continue
        params = {**BASE_PARAMETERS}
        complexity = 0
        for block in blocks:
            params.update(block.parameters)
            complexity += block.complexity
        if is_redundant_or_impossible(params, complexity):
            continue
        block_map = {block.category: block.id for block in blocks}
        canonical_key = canonical_candidate_key(block_map, params, parent.candidate_id if parent else None)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"sd_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        generated.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id=parent.family_id if parent else f"family_{sha256('|'.join(block_map.values()).encode()).hexdigest()[:10]}",
                parent_candidate_id=parent.candidate_id if parent else None,
                generation=(parent.generation + 1) if parent else 1,
                blocks=block_map,
                parameters=params,
                complexity=complexity,
                canonical_key=canonical_key,
            )
        )
        if len(generated) >= max_candidates:
            break
    return generated


def generate_balanced_discovery_candidates(max_candidates: int = 1000) -> list[DiscoveryCandidate]:
    if max_candidates <= 0:
        return []
    minimum_balanced_pool = 7000 if max_candidates >= 6 else 1200
    pool_size = min(16000, max(minimum_balanced_pool, max_candidates * 10))
    pool = generate_discovery_candidates(max_candidates=pool_size)
    entry_order = ("trend_continuation", "pullback", "breakout", "mean_reversion", "opening_range_proxy", "gap_proxy")
    grouped = {entry: [] for entry in entry_order}
    for candidate in pool:
        entry = str(candidate.parameters.get("entry") or "")
        if entry in grouped:
            grouped[entry].append(candidate)
    for entry in entry_order:
        grouped[entry] = interleave_candidate_groups(grouped[entry], "momentum")

    selected: list[DiscoveryCandidate] = []
    offsets = {entry: 0 for entry in entry_order}
    while len(selected) < max_candidates:
        added = False
        for entry in entry_order:
            offset = offsets[entry]
            candidates = grouped[entry]
            if offset >= len(candidates):
                continue
            base = candidates[offset]
            selected.append(frequency_aware_variant(base, offset % 5))
            offsets[entry] += 1
            added = True
            if len(selected) >= max_candidates:
                break
        if not added:
            break
    return selected


def interleave_candidate_groups(candidates: list[DiscoveryCandidate], block_name: str) -> list[DiscoveryCandidate]:
    keys = list(dict.fromkeys(candidate.blocks.get(block_name, "unknown") for candidate in candidates))
    grouped = {key: [candidate for candidate in candidates if candidate.blocks.get(block_name, "unknown") == key] for key in keys}
    offsets = {key: 0 for key in keys}
    result: list[DiscoveryCandidate] = []
    while len(result) < len(candidates):
        for key in keys:
            offset = offsets[key]
            if offset < len(grouped[key]):
                result.append(grouped[key][offset])
                offsets[key] += 1
    return result


def frequency_aware_variant(candidate: DiscoveryCandidate, profile_index: int) -> DiscoveryCandidate:
    profiles = ("baseline", "entry_frequency", "momentum_frequency", "volume_frequency", "holding_frequency")
    profile = profiles[profile_index % len(profiles)]
    params = dict(candidate.parameters)
    params["frequency_hypothesis"] = profile
    params["frequency_screen_min_opportunities"] = 30

    if profile == "entry_frequency":
        entry = str(params.get("entry") or "")
        if entry in {"breakout", "opening_range_proxy"}:
            params["breakout_lookback"] = max(4, int(params.get("breakout_lookback", 20)) // 2)
        elif entry == "pullback":
            params["entry_distance_to_ema20_max"] = round(float(params.get("entry_distance_to_ema20_max", 0.035)) * 1.75, 4)
        elif entry == "mean_reversion":
            params["rsi_oversold"] = min(50, float(params.get("rsi_oversold", 38)) + 7)
        elif entry in {"trend_continuation", "gap_proxy"}:
            params["returns_5_min"] = round(float(params.get("returns_5_min", 0.01)) * 0.5, 5)
    elif profile == "momentum_frequency":
        momentum = str(params.get("momentum") or "")
        if momentum == "rsi":
            params["rsi_min"] = max(45, float(params.get("rsi_min", 55)) - 7)
        elif momentum in {"roc", "adx_proxy"}:
            params["returns_5_min"] = round(float(params.get("returns_5_min", 0.01)) * 0.5, 5)
        elif momentum == "stochastic_proxy":
            params["rsi_min"] = max(35, float(params.get("rsi_min", 45)) - 7)
            params["rsi_max"] = min(80, float(params.get("rsi_max", 72)) + 5)
        elif momentum == "rsi_oversold":
            params["rsi_oversold"] = min(50, float(params.get("rsi_oversold", 42)) + 5)
        elif momentum == "macd":
            params["macd_signal_tolerance"] = 0.08
    elif profile == "volume_frequency":
        params["volume_change_min"] = min(float(params.get("volume_change_min", 0)), -0.25)
    elif profile == "holding_frequency":
        params["max_holding_bars"] = 8

    canonical_key = canonical_candidate_key(candidate.blocks, params, candidate.parent_candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sd_{sha256(canonical_key.encode()).hexdigest()[:14]}",
        family_id=candidate.family_id,
        parent_candidate_id=candidate.parent_candidate_id,
        generation=candidate.generation,
        blocks=dict(candidate.blocks),
        parameters=params,
        complexity=candidate.complexity,
        canonical_key=canonical_key,
    )


def is_redundant_or_impossible(params: dict[str, Any], complexity: int) -> bool:
    if complexity > 10:
        return True
    if int(params.get("trend_fast", 0)) >= int(params.get("trend_slow", 1)):
        return True
    if params.get("entry") == "mean_reversion" and params.get("momentum") != "rsi_oversold":
        return True
    if params.get("entry") == "breakout" and params.get("volatility") == "bollinger":
        return True
    return False


def canonical_candidate_key(blocks: dict[str, str], params: dict[str, Any], parent_id: str | None = None) -> str:
    material = {
        "blocks": {key: blocks[key] for key in sorted(blocks)},
        "params": {key: params[key] for key in sorted(params) if key not in {"initial_equity", "fee_rate", "slippage_rate", "risk_per_trade"}},
        "parent": parent_id or "",
    }
    return repr(material)


def make_strategy_definition(candidate: DiscoveryCandidate) -> StrategyDefinition:
    return StrategyDefinition(
        name="autonomous_strategy_discovery",
        version=candidate.candidate_id,
        description="Generated deterministic strategy assembled from modular discovery rule blocks.",
        parameters=candidate.parameters,
        entry_rules=[
            f"Trend block: {candidate.blocks['trend']}.",
            f"Momentum block: {candidate.blocks['momentum']}.",
            f"Volatility block: {candidate.blocks['volatility']}.",
            f"Volume block: {candidate.blocks['volume']}.",
            f"Entry block: {candidate.blocks['entry']}.",
        ],
        exit_rules=[f"Exit block: {candidate.blocks['exit']}.", "Long-only simulated risk model; no order is routed."],
        supported_market_regimes=["bull_trend", "bear_trend", "sideways", "high_volatility", "low_volatility", "normal_volatility"],
        decide=discovered_strategy_decision,
    )


def discovered_strategy_decision(candle: dict[str, Any], feature: dict[str, Any], recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> StrategyDecision:
    if params.get("strategy_architecture") == "relative_strength_continuation_v2":
        return relative_strength_continuation_v2_decision(candle, feature, recent_candles, params)
    close = Decimal(candle["close"])
    if not trend_passes(close, feature, recent_candles, params):
        return avoid("Trend block failed.")
    if not momentum_passes(feature, params):
        return avoid("Momentum block failed.")
    if not volatility_passes(close, feature, recent_candles, params):
        return avoid("Volatility block failed.")
    if not volume_passes(feature, params):
        return avoid("Volume block failed.")
    if not regime_filter_passes(feature, params):
        return avoid("Regime filter failed.")
    if not structural_context_passes(feature, params):
        return avoid("Structural context filter failed.")
    if not entry_passes(close, candle, feature, recent_candles, params):
        return avoid("Entry block failed.")
    stop = build_stop(close, feature, recent_candles, params)
    if stop is None or stop >= close:
        return avoid("Exit block produced an invalid stop.")
    rr = Decimal(str(params.get("risk_reward", 2)))
    take_profit = close + ((close - stop) * rr)
    return StrategyDecision("setup", (Decimal(candle["low"]), Decimal(candle["high"])), stop, take_profit, rr, ["Autonomous discovery setup passed all deterministic rule blocks."])


def relative_strength_continuation_v2_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ("ema_20", "ema_50", "rsi_14", "returns_5")
    if any(feature.get(name) is None for name in required):
        return avoid("Relative-strength architecture is missing a required feature.")
    close = Decimal(candle["close"])
    ema_20 = Decimal(feature["ema_20"])
    ema_50 = Decimal(feature["ema_50"])
    volatility = finite_metric(feature.get("volatility_20"))
    adaptive_profile = None
    if params.get("adaptive_volatility_profiles"):
        adaptive_profile = "high_vol" if volatility >= float(params.get("volatility_profile_boundary", 0.006)) else "low_vol"

    def profile_value(name: str, default: Any) -> Any:
        if adaptive_profile is not None:
            key = f"{adaptive_profile}_{name}"
            if key in params:
                return params[key]
        return params.get(name, default)

    if close <= ema_50 or ema_20 <= ema_50:
        return avoid("Relative-strength architecture trend filter failed.")
    rsi = finite_metric(feature.get("rsi_14"))
    if not float(profile_value("rsi_min", 50)) <= rsi <= float(profile_value("rsi_max", 80)):
        return avoid("Relative-strength architecture momentum filter failed.")
    returns_5 = finite_metric(feature.get("returns_5"))
    normalize_by_volatility = bool(params.get("normalize_signal_by_volatility"))
    momentum_signal = returns_5 / volatility if normalize_by_volatility and volatility > 0 else returns_5
    if momentum_signal < float(profile_value("returns_5_min", 0)):
        return avoid("Relative-strength architecture continuation filter failed.")
    relative_return = max(
        finite_metric(feature.get("context_relative_spy_returns_5")),
        finite_metric(feature.get("context_relative_qqq_returns_5")),
    )
    relative_signal = relative_return / volatility if normalize_by_volatility and volatility > 0 else relative_return
    if relative_signal < float(profile_value("relative_returns_5_min", 0)):
        return avoid("Relative-strength architecture market-relative filter failed.")
    volume_change = feature.get("volume_change")
    if volume_change is None or finite_metric(volume_change) < float(profile_value("volume_change_min", -0.05)):
        return avoid("Relative-strength architecture volume filter failed.")
    if params.get("volatility_normalized_stop") and volatility > 0:
        stop_distance = max(
            float(params.get("minimum_stop_distance", 0.006)),
            min(float(params.get("maximum_stop_distance", 0.04)), volatility * float(params.get("stop_volatility_multiplier", 2.0))),
        )
        stop = close * (Decimal("1") - Decimal(str(stop_distance)))
    else:
        stop_params = {**params, "swing_lookback": int(profile_value("swing_lookback", 5))}
        stop = build_stop(close, feature, recent_candles, stop_params)
    if stop is None or stop >= close:
        return avoid("Relative-strength architecture produced an invalid stop.")
    rr = Decimal(str(profile_value("risk_reward", 1.4)))
    take_profit = close + ((close - stop) * rr)
    return StrategyDecision(
        "setup",
        (Decimal(candle["low"]), Decimal(candle["high"])),
        stop,
        take_profit,
        rr,
        ["Cross-market relative strength confirmed a long-only continuation setup."],
    )


def avoid(reason: str) -> StrategyDecision:
    return StrategyDecision("avoid", None, None, None, None, [reason])


def moving_average(candles: list[dict[str, Any]], period: int, method: str = "ema") -> Decimal | None:
    if len(candles) < period or period <= 0:
        return None
    closes = [Decimal(row["close"]) for row in candles]
    if method == "sma":
        return sum(closes[-period:]) / Decimal(period)
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = sum(closes[:period]) / Decimal(period)
    for close in closes[period:]:
        ema = (close - ema) * multiplier + ema
    return ema


def trend_passes(close: Decimal, feature: dict[str, Any], recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> bool:
    if params.get("trend_method") == "vwap":
        vwap = feature.get("vwap") or feature.get("ema_20")
        return vwap is not None and close > Decimal(vwap)
    fast = moving_average(recent_candles, int(params["trend_fast"]), str(params.get("trend_method", "ema")))
    slow = moving_average(recent_candles, int(params["trend_slow"]), str(params.get("trend_method", "ema")))
    if fast is None or slow is None:
        return False
    if params.get("trend_requires_positive_returns") and finite_metric(feature.get("returns_5")) <= 0:
        return False
    slope_min = params.get("phase_9_9_ema_slope_min")
    if slope_min is not None:
        previous_fast = moving_average(recent_candles[:-1], int(params["trend_fast"]), str(params.get("trend_method", "ema")))
        if previous_fast is None or previous_fast == 0:
            return False
        slope = (fast - previous_fast) / previous_fast
        if slope < Decimal(str(slope_min)):
            return False
    if params.get("phase_9_11_ema_separation_increasing"):
        previous_fast = moving_average(recent_candles[:-1], int(params["trend_fast"]), str(params.get("trend_method", "ema")))
        previous_slow = moving_average(recent_candles[:-1], int(params["trend_slow"]), str(params.get("trend_method", "ema")))
        if previous_fast is None or previous_slow is None or slow == 0 or previous_slow == 0:
            return False
        if ((fast - slow) / slow) <= ((previous_fast - previous_slow) / previous_slow):
            return False
    return close > slow and fast > slow


def momentum_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    block = params.get("momentum")
    if block == "rsi":
        return feature.get("rsi_14") is not None and Decimal(feature["rsi_14"]) >= Decimal(str(params.get("rsi_min", 50)))
    if block == "macd":
        if feature.get("macd") is None or feature.get("macd_signal") is None:
            return False
        signal = Decimal(feature["macd_signal"])
        tolerance = abs(signal) * Decimal(str(params.get("macd_signal_tolerance", 0)))
        return Decimal(feature["macd"]) >= signal - tolerance
    if block in {"roc", "adx_proxy"}:
        return feature.get("returns_5") is not None and Decimal(feature["returns_5"]) >= Decimal(str(params.get("returns_5_min", 0)))
    if block == "stochastic_proxy":
        rsi = feature.get("rsi_14")
        return rsi is not None and Decimal(str(params.get("rsi_min", 45))) <= Decimal(rsi) <= Decimal(str(params.get("rsi_max", 72)))
    if block == "rsi_oversold":
        return feature.get("rsi_14") is not None and Decimal(feature["rsi_14"]) <= Decimal(str(params.get("rsi_oversold", 42)))
    return False


def volatility_passes(close: Decimal, feature: dict[str, Any], recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> bool:
    block = params.get("volatility")
    if block == "atr":
        return len(recent_candles) >= int(params.get("swing_lookback", 5))
    if block in {"keltner", "donchian"}:
        return finite_metric(feature.get("volatility_20")) >= float(params.get("volatility_20_min", 0))
    if block == "bollinger":
        distance = feature.get("distance_from_ema_20")
        return distance is not None and Decimal(distance) <= Decimal(str(params.get("distance_from_ema_20_max", -0.015)))
    return True


def volume_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    volume_change = feature.get("volume_change")
    return volume_change is not None and Decimal(volume_change) >= Decimal(str(params.get("volume_change_min", -1)))


def regime_filter_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    if not (params.get("phase_9_8_regime_filter") or params.get("phase_9_9_regime_filter") or params.get("phase_9_10_high_volatility_block") or params.get("phase_9_11_regime_filter")):
        return True
    returns_5 = finite_metric(feature.get("returns_5"))
    volatility = finite_metric(feature.get("volatility_20"))
    distance = abs(finite_metric(feature.get("distance_from_ema_50")))
    if params.get("phase_9_9_bull_trend_only") and returns_5 < float(params.get("phase_9_9_returns_5_min", 0)):
        return False
    if params.get("phase_9_9_low_volatility_block") and volatility < float(params.get("phase_9_9_low_volatility_min", 0.008)):
        return False
    if params.get("phase_9_10_high_volatility_block") and volatility > float(params.get("phase_9_10_high_volatility_max", 0.035)):
        return False
    if params.get("phase_9_11_block_4h_sideways") and str(feature.get("context_4h_trend_regime") or "") == "sideways":
        return False
    if params.get("phase_9_11_require_4h_bull") and str(feature.get("context_4h_trend_regime") or "") != "bull_trend":
        return False
    if params.get("block_sideways") and distance < float(params.get("sideways_distance_from_ema50_min", 0.01)):
        return False
    normal_min = float(params.get("normal_volatility_min", 0.008))
    normal_max = float(params.get("normal_volatility_max", 0.018))
    if normal_min <= volatility <= normal_max:
        rsi = feature.get("rsi_14")
        volume_change = feature.get("volume_change")
        if rsi is None or Decimal(rsi) < Decimal(str(params.get("normal_volatility_rsi_min", params.get("rsi_min", 55)))):
            return False
        if volume_change is None or Decimal(volume_change) < Decimal(str(params.get("normal_volatility_volume_change_min", params.get("volume_change_min", 0)))):
            return False
    if volatility < float(params.get("low_volatility_max", 0.008)):
        return returns_5 >= float(params.get("low_volatility_returns_5_min", 0))
    return True


def structural_context_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    if params.get("phase_9_11_require_4h_positive_returns") and finite_metric(feature.get("context_4h_returns_5")) <= 0:
        return False
    if params.get("phase_9_11_require_4h_ema_positive") and finite_metric(feature.get("context_4h_distance_from_ema_50")) <= 0:
        return False
    if params.get("phase_9_11_require_spy_trend") and finite_metric(feature.get("context_spy_returns_5")) < float(params.get("phase_9_11_market_returns_min", 0)):
        return False
    if params.get("phase_9_11_require_qqq_trend") and finite_metric(feature.get("context_qqq_returns_5")) < float(params.get("phase_9_11_market_returns_min", 0)):
        return False
    if params.get("phase_9_11_require_relative_spy") and finite_metric(feature.get("context_relative_spy_returns_5")) < float(params.get("phase_9_11_relative_returns_min", 0)):
        return False
    if params.get("phase_9_11_require_relative_qqq") and finite_metric(feature.get("context_relative_qqq_returns_5")) < float(params.get("phase_9_11_relative_returns_min", 0)):
        return False
    if params.get("phase_9_11_volatility_expansion") and finite_metric(feature.get("volatility_20")) < float(params.get("phase_9_11_volatility_min", 0.008)):
        return False
    if params.get("phase_9_11_volatility_contraction") and finite_metric(feature.get("volatility_20")) > float(params.get("phase_9_11_volatility_max", 0.018)):
        return False
    return True


def entry_passes(close: Decimal, candle: dict[str, Any], feature: dict[str, Any], recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> bool:
    if params.get("phase_9_11_entry_mode") == "volatility_expansion_continuation":
        return finite_metric(feature.get("returns_5")) >= float(params.get("returns_5_min", 0.008)) and finite_metric(feature.get("volatility_20")) >= float(params.get("phase_9_11_volatility_min", 0.008))
    if params.get("phase_9_11_entry_mode") == "relative_strength_continuation":
        return finite_metric(feature.get("returns_5")) >= float(params.get("returns_5_min", 0.006)) and (
            finite_metric(feature.get("context_relative_spy_returns_5")) >= float(params.get("phase_9_11_relative_returns_min", 0))
            or finite_metric(feature.get("context_relative_qqq_returns_5")) >= float(params.get("phase_9_11_relative_returns_min", 0))
        )
    if params.get("phase_9_11_entry_mode") == "sideways_transition":
        return finite_metric(feature.get("distance_from_ema_50")) >= float(params.get("phase_9_11_transition_distance_min", 0.012)) and finite_metric(feature.get("returns_5")) >= float(params.get("returns_5_min", 0.008))
    entry = params.get("entry")
    if entry in {"breakout", "opening_range_proxy"}:
        lookback = int(params.get("breakout_lookback", 20))
        if len(recent_candles) <= lookback:
            return False
        prior_high = max(Decimal(row["high"]) for row in recent_candles[-lookback - 1 : -1])
        return close > prior_high
    if entry == "pullback":
        ema20 = moving_average(recent_candles, 20, "ema")
        return ema20 is not None and abs((close - ema20) / ema20) <= Decimal(str(params.get("entry_distance_to_ema20_max", 0.035)))
    if entry == "mean_reversion":
        return feature.get("rsi_14") is not None and Decimal(feature["rsi_14"]) <= Decimal(str(params.get("rsi_oversold", 38)))
    if entry in {"trend_continuation", "gap_proxy"}:
        return feature.get("returns_5") is not None and Decimal(feature["returns_5"]) >= Decimal(str(params.get("returns_5_min", 0.01)))
    return False


def build_stop(close: Decimal, feature: dict[str, Any], recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> Decimal | None:
    lookback = int(params.get("swing_lookback", 5))
    if len(recent_candles) < lookback:
        return None
    swing_low = min(Decimal(row["low"]) for row in recent_candles[-lookback:])
    atr_proxy = close * Decimal("0.01") * Decimal(str(params.get("atr_multiplier", 1.5)))
    return min(swing_low, close - atr_proxy)


def run_strategy_discovery(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    max_candidates: int = 100,
) -> dict[str, Any]:
    ensure_strategy_discovery_tables(conn)
    sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe)
    features = list(
        conn.execute(
            """
            SELECT *
            FROM features
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe),
        ).fetchall()
    )
    regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
    candidates = generate_discovery_candidates(max_candidates=max_candidates)
    run_id = insert_discovery_run(conn, symbol, timeframe, max_candidates)
    evaluated = []
    context_by_time = build_context_by_time(candles, features, regimes)
    for candidate in candidates:
        row = evaluate_candidate(candidate, candles, features, context_by_time)
        persist_discovered_strategy(conn, run_id, symbol, timeframe, candidate, row)
        evaluated.append(row)
    conn.commit()
    ranked = sorted(evaluated, key=lambda row: row["research_score"], reverse=True)
    return {
        "run_id": run_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "generated": len(candidates),
        "evaluated": len(evaluated),
        "promoted": sum(1 for row in evaluated if row["status"] == "promoted"),
        "rejected": sum(1 for row in evaluated if row["status"] == "rejected"),
        "leaderboard": ranked[:25],
        "safety": SAFETY_STATEMENT,
    }


def evaluate_candidate(
    candidate: DiscoveryCandidate,
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    context_by_time: dict[Any, dict[str, Any]],
    market_arrays: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strategy = make_strategy_definition(candidate)
    frequency_screen = None
    minimum_opportunities = int(strategy.parameters.get("frequency_screen_min_opportunities") or 0)
    if minimum_opportunities > 0:
        frequency_screen = count_setup_opportunities(candles, features, strategy.parameters, strategy.decide)
    if frequency_screen and int(frequency_screen["opportunities"]) < minimum_opportunities:
        result = {
            "metrics": {
                "profit_factor": None,
                "expectancy_per_trade": 0,
                "max_drawdown": 0,
                "number_of_trades": 0,
                "setup_opportunities": int(frequency_screen["opportunities"]),
                "frequency_screen_rejected": True,
                "walk_forward": {
                    "enabled": bool(frequency_screen["walk_forward_enabled"]),
                    "reason": "Candidate cannot reach the unchanged trade-count gate in the available validation window.",
                },
            },
            "trades": [],
        }
    else:
        result = run_backtest(candles, features, strategy.parameters, strategy.decide, market_arrays=market_arrays)
    metrics = dict(result["metrics"])
    trades = result.get("trades", [])
    by_year = compare_by_year(trades)
    by_market = compare_by_regime(trades, context_by_time, "trend_regime")
    by_volatility = compare_by_regime(trades, context_by_time, "volatility_regime")
    readiness = paper_readiness_report(metrics, by_market, by_volatility)
    research_score = round(score_metrics(metrics), 4)
    status = status_for_candidate(metrics, readiness, research_score)
    failure_reasons = failure_reasons_for(metrics, readiness, by_market, by_volatility)
    return {
        "candidate_id": candidate.candidate_id,
        "family_id": candidate.family_id,
        "parent_candidate_id": candidate.parent_candidate_id,
        "generation": candidate.generation,
        "blocks": candidate.blocks,
        "parameters": candidate.parameters,
        "complexity": candidate.complexity,
        "metrics": metrics,
        "validation_metrics": {"score_metrics": research_score, "paper_ready": readiness["paper_ready"], "frequency_screen": frequency_screen},
        "walk_forward_metrics": metrics.get("walk_forward", {}),
        "out_of_sample_metrics": metrics.get("walk_forward", {}),
        "regime_analysis": {"by_year": by_year, "by_market_regime": by_market, "by_volatility_regime": by_volatility},
        "feature_correlations": calculate_feature_correlations(trades),
        "paper_readiness": readiness,
        "research_score": research_score,
        "status": status,
        "failure_reasons": failure_reasons,
        "explanation": explanation_for(candidate, status, failure_reasons),
        "simulation_only": True,
    }


def status_for_candidate(metrics: dict[str, Any], readiness: dict[str, Any], research_score: float) -> str:
    if readiness.get("paper_ready") and research_score > 0:
        return "promoted"
    if finite_metric(metrics.get("profit_factor")) >= 1.05 and finite_metric(metrics.get("expectancy_per_trade")) > 0 and finite_metric(metrics.get("number_of_trades")) >= 20:
        return "promoted"
    return "rejected"


def failure_reasons_for(metrics: dict[str, Any], readiness: dict[str, Any], by_market: list[dict[str, Any]], by_volatility: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if finite_metric(metrics.get("number_of_trades")) < 20:
        reasons.append("insufficient_trades")
    if metrics.get("frequency_screen_rejected"):
        reasons.append("projected_trade_count_below_gate")
    if finite_metric(metrics.get("profit_factor")) < 1.05:
        reasons.append("weak_profit_factor")
    if finite_metric(metrics.get("expectancy_per_trade")) <= 0:
        reasons.append("poor_expectancy")
    if finite_metric(metrics.get("max_drawdown")) > 0.12:
        reasons.append("high_drawdown")
    if not (metrics.get("walk_forward") or {}).get("enabled"):
        reasons.append("unstable_walk_forward")
    for row in [*by_market, *by_volatility]:
        row_metrics = row.get("metrics", {})
        if finite_metric(row_metrics.get("number_of_trades")) >= 5 and finite_metric(row_metrics.get("expectancy_per_trade")) <= 0:
            reasons.append(f"fails_in_{row.get('regime', 'unknown')}")
    for detail in readiness.get("failed_reasons", [])[:2]:
        reasons.append(str(detail))
    return sorted(set(reasons))


def explanation_for(candidate: DiscoveryCandidate, status: str, failure_reasons: list[str]) -> str:
    base = f"{candidate.candidate_id} was generated from {', '.join(candidate.blocks.values())}."
    if status == "promoted":
        return f"{base} Stored validation metrics met promotion gates."
    return f"{base} It was rejected because: {', '.join(failure_reasons) if failure_reasons else 'stored metrics did not meet promotion gates'}."


def insert_discovery_run(conn: psycopg.Connection, symbol: str, timeframe: str, max_candidates: int) -> int:
    row = conn.execute(
        """
        INSERT INTO strategy_discovery_runs(symbol, timeframe, requested_candidates, discovery_version, safety_statement)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (symbol, timeframe, max_candidates, DISCOVERY_VERSION, SAFETY_STATEMENT),
    ).fetchone()
    return int(row["id"])


def ensure_strategy_discovery_tables(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_discovery_runs (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            requested_candidates INTEGER NOT NULL,
            discovery_version TEXT NOT NULL,
            safety_statement TEXT NOT NULL,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_discovery_strategies (
            id BIGSERIAL PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            family_id TEXT NOT NULL,
            parent_candidate_id TEXT,
            discovery_run_id BIGINT REFERENCES strategy_discovery_runs(id),
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            generation INTEGER NOT NULL,
            blocks JSONB NOT NULL,
            parameters JSONB NOT NULL,
            complexity INTEGER NOT NULL,
            metrics JSONB NOT NULL,
            validation_metrics JSONB NOT NULL,
            walk_forward_metrics JSONB NOT NULL,
            out_of_sample_metrics JSONB NOT NULL,
            regime_analysis JSONB NOT NULL,
            feature_correlations JSONB NOT NULL,
            paper_readiness JSONB NOT NULL,
            research_score NUMERIC NOT NULL,
            status TEXT NOT NULL,
            failure_reasons JSONB NOT NULL,
            explanation TEXT NOT NULL,
            discovery_version TEXT NOT NULL,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(candidate_id, symbol, timeframe)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_discovery_events (
            id BIGSERIAL PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            parent_candidate_id TEXT,
            event_type TEXT NOT NULL,
            details JSONB NOT NULL,
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS strategy_discovery_strategies_status_score_idx
        ON strategy_discovery_strategies(status, research_score DESC, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS strategy_discovery_events_created_idx
        ON strategy_discovery_events(created_at DESC)
        """
    )


def persist_discovered_strategy(conn: psycopg.Connection, run_id: int, symbol: str, timeframe: str, candidate: DiscoveryCandidate, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO strategy_discovery_strategies(
            candidate_id, family_id, parent_candidate_id, discovery_run_id, symbol, timeframe, generation,
            blocks, parameters, complexity, metrics, validation_metrics, walk_forward_metrics,
            out_of_sample_metrics, regime_analysis, feature_correlations, paper_readiness, research_score,
            status, failure_reasons, explanation, discovery_version, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (candidate_id, symbol, timeframe) DO NOTHING
        """,
        (
            candidate.candidate_id,
            candidate.family_id,
            candidate.parent_candidate_id,
            run_id,
            symbol,
            timeframe,
            candidate.generation,
            Jsonb(jsonable(row["blocks"])),
            Jsonb(jsonable(row["parameters"])),
            candidate.complexity,
            Jsonb(jsonable(row["metrics"])),
            Jsonb(jsonable(row["validation_metrics"])),
            Jsonb(jsonable(row["walk_forward_metrics"])),
            Jsonb(jsonable(row["out_of_sample_metrics"])),
            Jsonb(jsonable(row["regime_analysis"])),
            Jsonb(jsonable(row["feature_correlations"])),
            Jsonb(jsonable(row["paper_readiness"])),
            row["research_score"],
            row["status"],
            Jsonb(jsonable(row["failure_reasons"])),
            row["explanation"],
            DISCOVERY_VERSION,
        ),
    )


def evolve_discovered_strategies(conn: psycopg.Connection, limit: int = 20) -> dict[str, Any]:
    ensure_strategy_discovery_tables(conn)
    parents = conn.execute(
        """
        SELECT candidate_id, family_id, generation, blocks, parameters
        FROM strategy_discovery_strategies
        WHERE status = 'promoted'
        ORDER BY research_score DESC, created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    pattern_context = load_learning_pattern_context(conn)
    created = []
    for parent_row in parents:
        parent = candidate_from_row(parent_row)
        for child, rationale in generate_child_variants(parent, pattern_context)[:3]:
            created.append(child)
            conn.execute(
                """
                INSERT INTO strategy_discovery_events(candidate_id, parent_candidate_id, event_type, details)
                VALUES (%s, %s, 'variant_generated', %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    child.candidate_id,
                    parent.candidate_id,
                    Jsonb(
                        jsonable(
                            {
                                "blocks": child.blocks,
                                "parameters": child.parameters,
                                "generation": child.generation,
                                "reason_for_mutation": rationale["reason"],
                                "supporting_evidence": rationale["supporting_evidence"],
                                "expected_improvement": rationale["expected_improvement"],
                                "confidence_score": rationale["confidence_score"],
                                "calculation_version": "research_learning_v1",
                            }
                        )
                    ),
                ),
            )
    conn.commit()
    return {"parents": len(parents), "variants_generated": len(created), "variants": [candidate_payload(row) for row in created], "safety": SAFETY_STATEMENT}


def load_learning_pattern_context(conn: psycopg.Connection) -> dict[str, set[str]]:
    try:
        failures = conn.execute(
            """
            SELECT pattern_type, description, recommendation, evidence_refs
            FROM research_failure_patterns
            WHERE simulation_only = TRUE
            ORDER BY frequency DESC, created_at DESC
            LIMIT 50
            """
        ).fetchall()
        successes = conn.execute(
            """
            SELECT pattern_type, description, recommendation, evidence_refs
            FROM research_success_patterns
            WHERE simulation_only = TRUE
            ORDER BY frequency DESC, created_at DESC
            LIMIT 50
            """
        ).fetchall()
    except Exception:
        failures = []
        successes = []
    return {
        "failures": {str(row.get("description") or row.get("recommendation") or "") for row in failures},
        "successes": {str(row.get("description") or row.get("recommendation") or "") for row in successes},
    }


def candidate_from_row(row: dict[str, Any]) -> DiscoveryCandidate:
    blocks = dict(row["blocks"])
    params = dict(row["parameters"])
    key = canonical_candidate_key(blocks, params, row["candidate_id"])
    return DiscoveryCandidate(str(row["candidate_id"]), str(row["family_id"]), None, int(row["generation"]), blocks, params, sum(RULE_LIBRARY[cat][0].complexity for cat in []), key)


def generate_child_variants(parent: DiscoveryCandidate, pattern_context: dict[str, set[str]] | None = None) -> list[tuple[DiscoveryCandidate, dict[str, Any]]]:
    variants: list[tuple[DiscoveryCandidate, dict[str, Any]]] = []
    synthetic_parent = {"candidate_id": parent.candidate_id, "evidence_ref": f"strategy_discovery:{parent.candidate_id}", "parameters": parent.parameters}
    context = pattern_context or {"failures": set(), "successes": set()}
    evidence_mutations = mutation_options(synthetic_parent, context.get("failures", set()), context.get("successes", set()))
    if not evidence_mutations:
        evidence_mutations = [
            {
                "changes": {"risk_reward": round(float(parent.parameters.get("risk_reward", 2)) + 0.2, 2)},
                "reason": "Local deterministic reward/risk confirmation around promoted parent.",
                "supporting_evidence": [f"strategy_discovery:{parent.candidate_id}"],
                "expected_improvement": "Tests nearby reward geometry without changing entry logic.",
                "confidence_score": 0.5,
            }
        ]
    deterministic_fallbacks = [
        (
            {"risk_reward": round(float(parent.parameters.get("risk_reward", 2)) + 0.2, 2)},
            "Local deterministic reward/risk confirmation around promoted parent.",
            "Tests nearby reward geometry without changing entry logic.",
            0.5,
        ),
        (
            {"volume_change_min": round(float(parent.parameters.get("volume_change_min", 0)) + 0.05, 3)},
            "Local deterministic volume-filter confirmation around promoted parent.",
            "Tests whether slightly stricter volume confirmation improves evidence quality.",
            0.48,
        ),
        (
            {"max_holding_bars": max(8, int(parent.parameters.get("max_holding_bars", 12) or 12) + 4)},
            "Local deterministic holding-period confirmation around promoted parent.",
            "Tests whether allowing more time improves exit quality without changing entries.",
            0.46,
        ),
    ]
    existing_changes = {repr(row["changes"]) for row in evidence_mutations}
    for changes, reason, expected, confidence in deterministic_fallbacks:
        if len(evidence_mutations) >= 3:
            break
        if repr(changes) in existing_changes:
            continue
        evidence_mutations.append(
            {
                "changes": changes,
                "reason": reason,
                "supporting_evidence": [f"strategy_discovery:{parent.candidate_id}"],
                "expected_improvement": expected,
                "confidence_score": confidence,
            }
        )
    for rationale in evidence_mutations:
        params = {**parent.parameters, **rationale["changes"]}
        key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
        variants.append(
            (
                DiscoveryCandidate(
                candidate_id=f"sd_{sha256(key.encode()).hexdigest()[:14]}",
                family_id=parent.family_id,
                parent_candidate_id=parent.candidate_id,
                generation=parent.generation + 1,
                blocks=parent.blocks,
                parameters=params,
                complexity=parent.complexity + 1,
                canonical_key=key,
                ),
                rationale,
            )
        )
    return variants


def discovery_dashboard(conn: psycopg.Connection, limit: int = 20) -> dict[str, Any]:
    ensure_strategy_discovery_tables(conn)
    summary_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM strategy_discovery_strategies
        GROUP BY status
        """
    ).fetchall()
    strongest = conn.execute(
        """
        SELECT candidate_id, family_id, parent_candidate_id, symbol, timeframe, generation, blocks, metrics, research_score, status, failure_reasons, explanation, created_at
        FROM strategy_discovery_strategies
        ORDER BY research_score DESC, created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    newest = conn.execute(
        """
        SELECT candidate_id, family_id, parent_candidate_id, symbol, timeframe, generation, blocks, metrics, research_score, status, failure_reasons, explanation, created_at
        FROM strategy_discovery_strategies
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    events = conn.execute(
        """
        SELECT candidate_id, parent_candidate_id, event_type, details, created_at
        FROM strategy_discovery_events
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in summary_rows}
    return {
        "summary": {
            "generated": sum(counts.values()),
            "rejected": counts.get("rejected", 0),
            "promoted": counts.get("promoted", 0),
            "retired": counts.get("retired", 0),
            "families": len({row["family_id"] for row in strongest + newest}),
        },
        "strongest_discoveries": [dashboard_row(row) for row in strongest],
        "newest_discoveries": [dashboard_row(row) for row in newest],
        "evolution_history": [jsonable(dict(row)) for row in events],
        "successful_rule_combinations": successful_rule_combinations(strongest),
        "safety": SAFETY_STATEMENT,
    }


def successful_rule_combinations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    promoted = [row for row in rows if row["status"] == "promoted"]
    combinations: dict[str, dict[str, Any]] = {}
    for row in promoted:
        blocks = row.get("blocks") or {}
        key = " + ".join(str(blocks.get(name, "")) for name in ["trend", "momentum", "entry", "exit"])
        slot = combinations.setdefault(key, {"combination": key, "count": 0, "best_score": None})
        slot["count"] += 1
        score = finite_metric(row.get("research_score"))
        slot["best_score"] = score if slot["best_score"] is None else max(slot["best_score"], score)
    return sorted(combinations.values(), key=lambda row: (row["count"], row["best_score"] or 0), reverse=True)


def dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "family_id": row["family_id"],
        "parent_candidate_id": row.get("parent_candidate_id"),
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "generation": row["generation"],
        "blocks": row["blocks"],
        "metrics": row["metrics"],
        "research_score": row["research_score"],
        "status": row["status"],
        "failure_reasons": row["failure_reasons"],
        "explanation": row["explanation"],
        "created_at": row["created_at"],
    }


def candidate_payload(candidate: DiscoveryCandidate) -> dict[str, Any]:
    return jsonable(asdict(candidate))


def jsonable(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value.obj
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
