from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from random import Random
from statistics import mean, pstdev
from typing import Any

from app.services.backtester import calculate_metrics, run_backtest
from app.services.strategy import StrategyDecision, StrategyDefinition
from app.services.strategy_research import (
    build_context_by_time,
    compare_by_regime,
    compare_by_year,
    finite_metric,
    metrics_for_trades,
)


ALPHA_RANK_METRICS = (
    "profit_factor",
    "expectancy_per_trade",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "stability_score",
    "number_of_trades",
    "consistency_score",
)


DEFAULT_SEARCH_SPACE = {
    "trend_filter": ["ema", "sma"],
    "trend_fast": [20, 50],
    "trend_slow": [50, 100, 200],
    "momentum_block": ["rsi", "macd", "roc"],
    "rsi_min": [30, 35, 40],
    "roc_min": [0.01, 0.02],
    "volatility_block": ["none", "volatility"],
    "volatility_min": [0.01, 0.015],
    "volume_block": ["none", "relative_volume"],
    "volume_change_min": [-0.1, 0.0],
    "price_action": ["pullback", "breakout"],
    "breakout_lookback": [12, 20],
    "swing_lookback": [5, 10],
    "risk_reward": [1.5, 2.0, 2.5, 3.0],
    "atr_multiplier": [1.5, 2.0],
}


BASE_ALPHA_PARAMETERS = {
    "fee_rate": 0.001,
    "slippage_rate": 0.0005,
    "risk_per_trade": 0.01,
    "initial_equity": 10000,
    "walk_forward_train_ratio": 0.7,
}


@dataclass(frozen=True)
class AlphaCandidate:
    name: str
    version: str
    description: str
    parameters: dict[str, Any]
    blocks: dict[str, Any]


def generate_alpha_candidates(search_space: dict[str, list[Any]] | None = None, max_candidates: int = 500) -> list[AlphaCandidate]:
    search_space = search_space or DEFAULT_SEARCH_SPACE
    keys = list(search_space.keys())
    candidates = []
    for values in product(*(search_space[key] for key in keys)):
        params = {**BASE_ALPHA_PARAMETERS, **dict(zip(keys, values))}
        if int(params["trend_fast"]) >= int(params["trend_slow"]):
            continue
        blocks = {
            "trend_filter": params["trend_filter"],
            "momentum": params["momentum_block"],
            "volatility": params["volatility_block"],
            "volume": params["volume_block"],
            "price_action": params["price_action"],
        }
        candidates.append(
            AlphaCandidate(
                name="generated_alpha",
                version="v1",
                description="Generated deterministic strategy assembled from reusable alpha research blocks.",
                parameters=params,
                blocks=blocks,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def make_strategy_definition(candidate: AlphaCandidate) -> StrategyDefinition:
    return StrategyDefinition(
        name=candidate.name,
        version=candidate.version,
        description=candidate.description,
        parameters=candidate.parameters,
        entry_rules=[
            f"Trend filter: {candidate.blocks['trend_filter']} {candidate.parameters['trend_fast']}/{candidate.parameters['trend_slow']}.",
            f"Momentum block: {candidate.blocks['momentum']}.",
            f"Volatility block: {candidate.blocks['volatility']}.",
            f"Volume block: {candidate.blocks['volume']}.",
            f"Price action block: {candidate.blocks['price_action']}.",
        ],
        exit_rules=["Stop below generated swing/ATR proxy.", "Target at configured risk/reward multiple."],
        supported_market_regimes=["bull_trend", "bear_trend", "sideways", "high_volatility", "low_volatility"],
        decide=generated_alpha_decision,
    )


def generated_alpha_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    close = Decimal(candle["close"])
    trend_fast = int(params["trend_fast"])
    trend_slow = int(params["trend_slow"])
    fast_ma = moving_average(recent_candles, trend_fast, params["trend_filter"])
    slow_ma = moving_average(recent_candles, trend_slow, params["trend_filter"])
    if fast_ma is None or slow_ma is None:
        return StrategyDecision("avoid", None, None, None, None, ["Not enough history for generated trend filter."])
    if not trend_filter_passes(close, fast_ma, slow_ma):
        return StrategyDecision("avoid", None, None, None, None, ["Generated trend filter failed."])
    if not momentum_passes(feature, params):
        return StrategyDecision("avoid", None, None, None, None, ["Generated momentum block failed."])
    if not volatility_passes(feature, params):
        return StrategyDecision("avoid", None, None, None, None, ["Generated volatility block failed."])
    if not volume_passes(feature, params):
        return StrategyDecision("avoid", None, None, None, None, ["Generated volume block failed."])
    if not price_action_passes(candle, recent_candles, params):
        return StrategyDecision("avoid", None, None, None, None, ["Generated price action block failed."])

    stop = generated_stop(close, recent_candles, params)
    if stop is None or stop >= close:
        return StrategyDecision("avoid", None, None, None, None, ["Generated stop is invalid."])
    risk_reward = Decimal(str(params["risk_reward"]))
    take_profit = close + ((close - stop) * risk_reward)
    return StrategyDecision(
        "setup",
        (Decimal(candle["low"]), Decimal(candle["high"])),
        stop,
        take_profit,
        risk_reward,
        ["Generated alpha setup passed all deterministic blocks."],
    )


def moving_average(candles: list[dict[str, Any]], period: int, method: str) -> Decimal | None:
    if period <= 0 or len(candles) < period:
        return None
    closes = [Decimal(row["close"]) for row in candles]
    if method == "sma":
        return sum(closes[-period:]) / Decimal(period)
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = sum(closes[:period]) / Decimal(period)
    for close in closes[period:]:
        ema = (close - ema) * multiplier + ema
    return ema


def trend_filter_passes(close: Decimal, fast_ma: Decimal, slow_ma: Decimal) -> bool:
    return close > slow_ma and fast_ma > slow_ma


def momentum_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    block = params["momentum_block"]
    if block == "rsi":
        value = feature.get("rsi_14")
        return value is not None and Decimal(value) >= Decimal(str(params["rsi_min"]))
    if block == "macd":
        macd = feature.get("macd")
        signal = feature.get("macd_signal")
        return macd is not None and signal is not None and Decimal(macd) > Decimal(signal)
    if block == "roc":
        value = feature.get("returns_5")
        return value is not None and Decimal(value) >= Decimal(str(params["roc_min"]))
    return False


def volatility_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    if params["volatility_block"] == "none":
        return True
    value = feature.get("volatility_20")
    return value is not None and Decimal(value) >= Decimal(str(params["volatility_min"]))


def volume_passes(feature: dict[str, Any], params: dict[str, Any]) -> bool:
    if params["volume_block"] == "none":
        return True
    value = feature.get("volume_change")
    return value is not None and Decimal(value) >= Decimal(str(params["volume_change_min"]))


def price_action_passes(candle: dict[str, Any], recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> bool:
    close = Decimal(candle["close"])
    if params["price_action"] == "pullback":
        fast_ma = moving_average(recent_candles, int(params["trend_fast"]), params["trend_filter"])
        if fast_ma is None:
            return False
        return abs((close - fast_ma) / fast_ma) <= Decimal("0.035")
    lookback = int(params["breakout_lookback"])
    if len(recent_candles) <= lookback:
        return False
    prior_high = max(Decimal(row["high"]) for row in recent_candles[-lookback - 1 : -1])
    return close > prior_high


def generated_stop(close: Decimal, recent_candles: list[dict[str, Any]], params: dict[str, Any]) -> Decimal | None:
    lookback = int(params["swing_lookback"])
    if len(recent_candles) < lookback:
        return None
    swing_low = min(Decimal(row["low"]) for row in recent_candles[-lookback:])
    volatility_proxy = close * Decimal("0.01") * Decimal(str(params["atr_multiplier"]))
    return min(swing_low, close - volatility_proxy)


def run_alpha_discovery(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    regimes: list[dict[str, Any]] | None = None,
    max_candidates: int = 250,
    monte_carlo_runs: int = 200,
) -> dict[str, Any]:
    candidates = generate_alpha_candidates(max_candidates=max_candidates)
    context_by_time = build_context_by_time(candles, features, regimes)
    rows = []
    for index, candidate in enumerate(candidates, start=1):
        strategy = make_strategy_definition(candidate)
        result = run_backtest(candles, features, strategy.parameters, strategy.decide)
        metrics = dict(result["metrics"])
        by_year = compare_by_year(result["trades"])
        by_regime = compare_by_regime(result["trades"], context_by_time, "trend_regime")
        by_volatility = compare_by_regime(result["trades"], context_by_time, "volatility_regime")
        stability = calculate_stability_score(by_year, by_regime, by_volatility)
        consistency = calculate_consistency_score(result["trades"])
        sortino = calculate_sortino(result["trades"])
        monte_carlo = run_monte_carlo(result["trades"], monte_carlo_runs)
        alpha_score = calculate_alpha_score(metrics, stability, consistency, sortino, monte_carlo)
        confidence_score = calculate_confidence_score(metrics, stability, consistency, monte_carlo)
        rows.append(
            {
                "rank": 0,
                "candidate_id": f"alpha_{index:04d}",
                "strategy_name": candidate.name,
                "strategy_version": candidate.version,
                "blocks": candidate.blocks,
                "parameters": candidate.parameters,
                "metrics": {**metrics, "sortino_ratio": sortino},
                "stability": {
                    "year": by_year,
                    "regime": by_regime,
                    "volatility": by_volatility,
                    "stability_score": stability,
                    "consistency_score": consistency,
                },
                "monte_carlo": monte_carlo,
                "alpha_score": alpha_score,
                "confidence_score": confidence_score,
                "recommendation": recommend_alpha(alpha_score, confidence_score, metrics),
                "alpha_report": "",
            }
        )
    ranked = sorted(rows, key=lambda row: row["alpha_score"], reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
        row["alpha_report"] = build_alpha_report(row)
    return {
        "candidate_count": len(ranked),
        "rank_metrics": list(ALPHA_RANK_METRICS),
        "leaderboard": ranked,
        "summary": summarize_alpha_discovery(ranked),
    }


def calculate_sortino(trades: list[dict[str, Any]]) -> float | None:
    returns = [float(trade["pnl_pct"]) for trade in trades]
    downside = [value for value in returns if value < 0]
    if len(returns) < 2 or not downside:
        return None
    downside_deviation = pstdev(downside)
    return mean(returns) / downside_deviation if downside_deviation else None


def calculate_stability_score(by_year: list[dict[str, Any]], by_regime: list[dict[str, Any]], by_volatility: list[dict[str, Any]]) -> float:
    groups = by_year + by_regime + by_volatility
    evaluated = [row for row in groups if row["metrics"].get("number_of_trades", 0) > 0]
    if not evaluated:
        return 0.0
    positive = [row for row in evaluated if finite_metric(row["metrics"].get("expectancy_per_trade")) > 0]
    return len(positive) / len(evaluated)


def calculate_consistency_score(trades: list[dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for trade in trades if Decimal(trade["pnl"]) > 0)
    return wins / len(trades)


def run_monte_carlo(trades: list[dict[str, Any]], runs: int = 200) -> dict[str, Any]:
    if not trades:
        return {"runs": runs, "p05_final_equity": None, "p50_final_equity": None, "p95_final_equity": None, "p95_max_drawdown": None}
    rng = Random(42)
    finals = []
    drawdowns = []
    for _ in range(runs):
        shuffled = list(trades)
        rng.shuffle(shuffled)
        initial = Decimal("10000")
        equity = initial
        curve = [equity]
        normalized = []
        for trade in shuffled:
            pnl = Decimal(trade["pnl"])
            equity += pnl
            curve.append(equity)
            normalized_trade = dict(trade)
            normalized_trade["pnl_pct"] = pnl / initial
            normalized.append(normalized_trade)
        metrics = calculate_metrics(initial, equity, normalized, curve)
        finals.append(metrics["final_equity"])
        drawdowns.append(metrics["max_drawdown"])
    return {
        "runs": runs,
        "p05_final_equity": percentile(finals, 0.05),
        "p50_final_equity": percentile(finals, 0.50),
        "p95_final_equity": percentile(finals, 0.95),
        "p95_max_drawdown": percentile(drawdowns, 0.95),
    }


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def calculate_alpha_score(
    metrics: dict[str, Any],
    stability: float,
    consistency: float,
    sortino: float | None,
    monte_carlo: dict[str, Any],
) -> float:
    profit_factor = finite_metric(metrics.get("profit_factor"))
    expectancy = finite_metric(metrics.get("expectancy_per_trade"))
    sharpe = finite_metric(metrics.get("sharpe_ratio"))
    sortino_value = finite_metric(sortino)
    max_drawdown = finite_metric(metrics.get("max_drawdown"))
    trade_count = finite_metric(metrics.get("number_of_trades"))
    mc_penalty = 0.0
    if monte_carlo.get("p05_final_equity") is not None and monte_carlo["p05_final_equity"] < 10000:
        mc_penalty = (10000 - monte_carlo["p05_final_equity"]) / 100
    low_sample_penalty = max(0.0, 30.0 - trade_count) * 2.0
    return (
        profit_factor * 20
        + expectancy * 0.1
        + sharpe * 8
        + sortino_value * 6
        - max_drawdown * 40
        + stability * 25
        + consistency * 15
        + min(trade_count, 100) * 0.2
        - low_sample_penalty
        - mc_penalty
    )


def calculate_confidence_score(
    metrics: dict[str, Any],
    stability: float,
    consistency: float,
    monte_carlo: dict[str, Any],
) -> float:
    trade_count = finite_metric(metrics.get("number_of_trades"))
    sample_score = min(trade_count / 50, 1.0)
    mc_score = 0.0
    if monte_carlo.get("p05_final_equity") is not None:
        mc_score = 1.0 if monte_carlo["p05_final_equity"] >= 10000 else max(0.0, monte_carlo["p05_final_equity"] / 10000)
    return round((sample_score * 0.35 + stability * 0.35 + consistency * 0.15 + mc_score * 0.15) * 100, 2)


def recommend_alpha(alpha_score: float, confidence_score: float, metrics: dict[str, Any]) -> str:
    if alpha_score >= 60 and confidence_score >= 70 and finite_metric(metrics.get("number_of_trades")) >= 50:
        return "Candidate for Paper Trading"
    if alpha_score >= 20 and confidence_score >= 40:
        return "Research More"
    return "Reject"


def build_alpha_report(row: dict[str, Any]) -> str:
    metrics = row["metrics"]
    return "\n".join(
        [
            f"# {row['candidate_id']} Alpha Report",
            "",
            f"Alpha Score: {row['alpha_score']:.2f}",
            f"Confidence Score: {row['confidence_score']:.2f}",
            f"Recommendation: {row['recommendation']}",
            "",
            f"Profit Factor: {metrics.get('profit_factor')}",
            f"Expectancy: {metrics.get('expectancy_per_trade')}",
            f"Sharpe: {metrics.get('sharpe_ratio')}",
            f"Sortino: {metrics.get('sortino_ratio')}",
            f"Max Drawdown: {metrics.get('max_drawdown')}",
            f"Trade Count: {metrics.get('number_of_trades')}",
            "",
            f"Blocks: {row['blocks']}",
        ]
    )


def summarize_alpha_discovery(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    recommendations: dict[str, int] = {}
    for row in ranked:
        recommendation = row["recommendation"]
        recommendations[recommendation] = recommendations.get(recommendation, 0) + 1
    return {
        "best_candidate": ranked[0]["candidate_id"] if ranked else None,
        "best_alpha_score": ranked[0]["alpha_score"] if ranked else None,
        "recommendations": recommendations,
    }
