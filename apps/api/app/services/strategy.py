from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


SignalName = Literal["setup", "watchlist", "avoid"]
StrategyFn = Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]], "StrategyDecision"]


@dataclass(frozen=True)
class StrategyDecision:
    signal: SignalName
    entry_zone: tuple[Decimal, Decimal] | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    risk_reward: Decimal | None
    explanation: list[str]


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    version: str
    description: str
    parameters: dict[str, Any]
    entry_rules: list[str]
    exit_rules: list[str]
    supported_market_regimes: list[str]
    decide: StrategyFn

    @property
    def key(self) -> str:
        return f"{self.name}_{self.version}"


def get_strategy_version(conn: Any, name: str = "trend_pullback", version: str = "v1") -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT name, version, parameters, description
        FROM strategy_versions
        WHERE name = %s AND version = %s
        """,
        (name, version),
    ).fetchone()
    if not row:
        raise ValueError(f"Missing strategy version: {name}_{version}")
    return dict(row)


BASE_PARAMETERS: dict[str, Any] = {
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_min": 40,
    "rsi_max": 60,
    "volume_change_min": -0.25,
    "entry_distance_to_ema20_max": 0.015,
    "swing_lookback": 5,
    "risk_reward": 2,
    "fee_rate": 0.001,
    "slippage_rate": 0.0005,
    "risk_per_trade": 0.01,
    "initial_equity": 10000,
    "walk_forward_train_ratio": 0.7,
}


def get_strategy_library() -> dict[str, StrategyDefinition]:
    strategies = [
        StrategyDefinition(
            name="trend_pullback",
            version="v1",
            description="Long-only trend pullback strategy using EMA trend, RSI pullback, volume confirmation, and swing-low risk.",
            parameters=BASE_PARAMETERS,
            entry_rules=[
                "Close is above slow EMA.",
                "Fast EMA is above slow EMA.",
                "RSI is within configured pullback range.",
                "Volume change is above configured minimum.",
                "Price is close to the fast EMA entry zone.",
            ],
            exit_rules=["Stop at recent swing low.", "Target at configured risk/reward multiple."],
            supported_market_regimes=["bull", "sideways", "normal_volatility"],
            decide=trend_pullback_decision,
        ),
        StrategyDefinition(
            name="breakout",
            version="v1",
            description="Long-only range breakout strategy that enters after closing above the prior lookback high with volume confirmation.",
            parameters={**BASE_PARAMETERS, "breakout_lookback": 20, "volume_change_min": 0.05, "risk_reward": 2},
            entry_rules=[
                "Close breaks above the highest high from the prior breakout lookback window.",
                "Volume change confirms participation.",
            ],
            exit_rules=["Stop below the lowest low in the breakout lookback window.", "Target at configured risk/reward multiple."],
            supported_market_regimes=["bull", "high_volatility"],
            decide=breakout_decision,
        ),
        StrategyDefinition(
            name="mean_reversion",
            version="v1",
            description="Long-only mean reversion strategy that buys stretched closes below EMA20 when RSI is oversold.",
            parameters={**BASE_PARAMETERS, "rsi_oversold": 35, "distance_from_ema_20_min": -0.025, "swing_lookback": 10, "risk_reward": 1.5},
            entry_rules=[
                "Close is below EMA20 by the configured minimum distance.",
                "RSI is below the oversold threshold.",
                "Price remains above EMA50 to avoid structurally weak downtrends.",
            ],
            exit_rules=["Stop below recent swing low.", "Target near EMA20 using configured risk/reward cap."],
            supported_market_regimes=["sideways", "normal_volatility"],
            decide=mean_reversion_decision,
        ),
        StrategyDefinition(
            name="momentum",
            version="v1",
            description="Long-only momentum continuation strategy using positive multi-candle return, MACD confirmation, and EMA trend.",
            parameters={**BASE_PARAMETERS, "returns_5_min": 0.025, "risk_reward": 2, "swing_lookback": 8},
            entry_rules=[
                "Five-candle return is above configured momentum threshold.",
                "MACD is above its signal line.",
                "Close is above EMA50.",
            ],
            exit_rules=["Stop below recent swing low.", "Target at configured risk/reward multiple."],
            supported_market_regimes=["bull", "high_volatility"],
            decide=momentum_decision,
        ),
        StrategyDefinition(
            name="volatility_breakout",
            version="v1",
            description="Long-only volatility expansion breakout using prior range, recent volatility, and volume confirmation.",
            parameters={**BASE_PARAMETERS, "breakout_lookback": 12, "volatility_20_min": 0.015, "volume_change_min": 0.1, "risk_reward": 2},
            entry_rules=[
                "Close breaks above prior lookback high.",
                "Twenty-period volatility is above configured threshold.",
                "Volume expands by the configured minimum.",
            ],
            exit_rules=["Stop below prior range midpoint.", "Target at configured risk/reward multiple."],
            supported_market_regimes=["high_volatility", "bull"],
            decide=volatility_breakout_decision,
        ),
        StrategyDefinition(
            name="trend_following_200ema",
            version="v1",
            description="Long-only trend-following strategy using a 200 EMA regime filter and positive intermediate momentum.",
            parameters={**BASE_PARAMETERS, "ema_long": 200, "returns_5_min": 0.01, "risk_reward": 2.5, "swing_lookback": 20},
            entry_rules=[
                "Close is above EMA200.",
                "EMA50 is above EMA200.",
                "Five-candle return is positive enough to confirm trend continuation.",
            ],
            exit_rules=["Stop below recent swing low.", "Target at configured risk/reward multiple."],
            supported_market_regimes=["bull", "normal_volatility"],
            decide=trend_following_200ema_decision,
        ),
    ]
    return {strategy.key: strategy for strategy in strategies}


def get_strategy_definition(name: str, version: str = "v1") -> StrategyDefinition:
    key = f"{name}_{version}"
    library = get_strategy_library()
    try:
        return library[key]
    except KeyError as exc:
        supported = ", ".join(sorted(library))
        raise ValueError(f"Unsupported strategy '{key}'. Supported strategies: {supported}") from exc


def trend_pullback_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ["rsi_14", "volume_change"]
    if any(feature.get(key) is None for key in required):
        return StrategyDecision("avoid", None, None, None, None, ["Not enough historical candles to calculate required indicators."])

    close = Decimal(candle["close"])
    ema_fast_period = int(params["ema_fast"])
    ema_slow_period = int(params["ema_slow"])
    ema_fast = calculate_ema_from_candles(recent_candles, ema_fast_period)
    ema_slow = calculate_ema_from_candles(recent_candles, ema_slow_period)
    if ema_fast is None or ema_slow is None:
        return StrategyDecision("avoid", None, None, None, None, ["Not enough historical candles to calculate configured EMA periods."])
    rsi_14 = Decimal(feature["rsi_14"])
    volume_change = Decimal(feature["volume_change"])
    distance = abs((close - ema_fast) / ema_fast)

    explanation: list[str] = []
    if close > ema_slow:
        explanation.append(f"Price is above EMA{ema_slow_period}.")
    if ema_fast > ema_slow:
        explanation.append(f"EMA{ema_fast_period} is above EMA{ema_slow_period}.")
    if Decimal(str(params["rsi_min"])) <= rsi_14 <= Decimal(str(params["rsi_max"])):
        explanation.append("RSI is in the neutral pullback range.")
    if volume_change >= Decimal(str(params["volume_change_min"])):
        explanation.append("Volume is not collapsing versus the previous candle.")
    if distance <= Decimal(str(params["entry_distance_to_ema20_max"])):
        explanation.append("Price is near EMA20 entry zone.")

    trend_ok = close > ema_slow and ema_fast > ema_slow
    rsi_ok = Decimal(str(params["rsi_min"])) <= rsi_14 <= Decimal(str(params["rsi_max"]))
    volume_ok = volume_change >= Decimal(str(params["volume_change_min"]))
    entry_ok = distance <= Decimal(str(params["entry_distance_to_ema20_max"]))

    if not trend_ok:
        return StrategyDecision("avoid", None, None, None, None, explanation + ["Trend filter failed."])
    if not (rsi_ok and volume_ok):
        return StrategyDecision("watchlist", None, None, None, None, explanation + ["Setup needs healthier RSI and volume confirmation."])

    swing_lookback = int(params["swing_lookback"])
    swing_window = recent_candles[-swing_lookback:] if len(recent_candles) >= swing_lookback else recent_candles
    if not swing_window:
        return StrategyDecision("watchlist", None, None, None, None, explanation + ["Not enough recent candles for stop placement."])

    swing_low = min(Decimal(row["low"]) for row in swing_window)
    if swing_low >= close:
        return StrategyDecision("watchlist", None, None, None, None, explanation + ["Recent swing low does not provide a valid long stop."])

    risk_per_unit = close - swing_low
    take_profit = close + (risk_per_unit * Decimal(str(params["risk_reward"])))
    entry_zone = (ema_fast * Decimal("0.995"), ema_fast * Decimal("1.005"))

    if not entry_ok:
        return StrategyDecision(
            "watchlist",
            entry_zone,
            swing_low,
            take_profit,
            Decimal(str(params["risk_reward"])),
            explanation + ["Wait for pullback closer to EMA20 before entry."],
        )

    return StrategyDecision(
        "setup",
        entry_zone,
        swing_low,
        take_profit,
        Decimal(str(params["risk_reward"])),
        explanation + ["Research setup is present; this is not execution advice."],
    )


def breakout_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ["volume_change"]
    if any(feature.get(key) is None for key in required):
        return StrategyDecision("avoid", None, None, None, None, ["Not enough feature history for breakout confirmation."])
    lookback = int(params["breakout_lookback"])
    if len(recent_candles) <= lookback:
        return StrategyDecision("avoid", None, None, None, None, ["Not enough candles for breakout lookback."])
    prior_window = recent_candles[-lookback - 1 : -1]
    close = Decimal(candle["close"])
    prior_high = max(Decimal(row["high"]) for row in prior_window)
    prior_low = min(Decimal(row["low"]) for row in prior_window)
    volume_ok = Decimal(feature["volume_change"]) >= Decimal(str(params["volume_change_min"]))
    if close <= prior_high:
        return StrategyDecision("avoid", None, None, None, None, ["Close has not broken above prior range high."])
    if not volume_ok:
        return StrategyDecision("watchlist", None, prior_low, None, None, ["Breakout lacks volume confirmation."])
    return long_decision_from_stop(candle, close, prior_low, params, ["Range breakout confirmed."])


def mean_reversion_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ["ema_20", "ema_50", "rsi_14", "distance_from_ema_20"]
    if any(feature.get(key) is None for key in required):
        return StrategyDecision("avoid", None, None, None, None, ["Not enough feature history for mean reversion."])
    close = Decimal(candle["close"])
    ema_20 = Decimal(feature["ema_20"])
    ema_50 = Decimal(feature["ema_50"])
    rsi_14 = Decimal(feature["rsi_14"])
    distance = Decimal(feature["distance_from_ema_20"])
    if close <= ema_50:
        return StrategyDecision("avoid", None, None, None, None, ["Close is below EMA50; avoiding weak downtrend mean reversion."])
    if rsi_14 > Decimal(str(params["rsi_oversold"])):
        return StrategyDecision("avoid", None, None, None, None, ["RSI is not oversold."])
    if distance > Decimal(str(params["distance_from_ema_20_min"])):
        return StrategyDecision("avoid", None, None, None, None, ["Close is not stretched far enough below EMA20."])
    stop = recent_swing_low(recent_candles, int(params["swing_lookback"]))
    if stop is None:
        return StrategyDecision("avoid", None, None, None, None, ["Not enough candles for mean reversion stop."])
    take_profit = min(ema_20, close + ((close - stop) * Decimal(str(params["risk_reward"]))))
    if stop >= close or take_profit <= close:
        return StrategyDecision("avoid", None, None, None, None, ["Mean reversion stop/target geometry is invalid."])
    return StrategyDecision("setup", (close, ema_20), stop, take_profit, Decimal(str(params["risk_reward"])), ["Oversold stretch toward EMA20 mean reversion."])


def momentum_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ["ema_50", "returns_5", "macd", "macd_signal"]
    if any(feature.get(key) is None for key in required):
        return StrategyDecision("avoid", None, None, None, None, ["Not enough feature history for momentum confirmation."])
    close = Decimal(candle["close"])
    if close <= Decimal(feature["ema_50"]):
        return StrategyDecision("avoid", None, None, None, None, ["Close is below EMA50 trend filter."])
    if Decimal(feature["returns_5"]) < Decimal(str(params["returns_5_min"])):
        return StrategyDecision("avoid", None, None, None, None, ["Five-candle return is below momentum threshold."])
    if Decimal(feature["macd"]) <= Decimal(feature["macd_signal"]):
        return StrategyDecision("avoid", None, None, None, None, ["MACD confirmation failed."])
    stop = recent_swing_low(recent_candles, int(params["swing_lookback"]))
    return long_decision_from_stop(candle, close, stop, params, ["Momentum continuation confirmed."])


def volatility_breakout_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ["volatility_20", "volume_change"]
    if any(feature.get(key) is None for key in required):
        return StrategyDecision("avoid", None, None, None, None, ["Not enough feature history for volatility breakout."])
    if Decimal(feature["volatility_20"]) < Decimal(str(params["volatility_20_min"])):
        return StrategyDecision("avoid", None, None, None, None, ["Volatility is below breakout threshold."])
    lookback = int(params["breakout_lookback"])
    if len(recent_candles) <= lookback:
        return StrategyDecision("avoid", None, None, None, None, ["Not enough candles for volatility breakout range."])
    prior_window = recent_candles[-lookback - 1 : -1]
    close = Decimal(candle["close"])
    prior_high = max(Decimal(row["high"]) for row in prior_window)
    prior_low = min(Decimal(row["low"]) for row in prior_window)
    range_midpoint = prior_low + ((prior_high - prior_low) / Decimal("2"))
    if close <= prior_high:
        return StrategyDecision("avoid", None, None, None, None, ["Close has not broken above prior volatility range."])
    if Decimal(feature["volume_change"]) < Decimal(str(params["volume_change_min"])):
        return StrategyDecision("watchlist", None, range_midpoint, None, None, ["Volatility breakout lacks volume expansion."])
    return long_decision_from_stop(candle, close, range_midpoint, params, ["Volatility expansion breakout confirmed."])


def trend_following_200ema_decision(
    candle: dict[str, Any],
    feature: dict[str, Any],
    recent_candles: list[dict[str, Any]],
    params: dict[str, Any],
) -> StrategyDecision:
    required = ["ema_50", "returns_5"]
    if any(feature.get(key) is None for key in required):
        return StrategyDecision("avoid", None, None, None, None, ["Not enough feature history for trend following."])
    close = Decimal(candle["close"])
    ema_200 = calculate_ema_from_candles(recent_candles, int(params["ema_long"]))
    if ema_200 is None:
        return StrategyDecision("avoid", None, None, None, None, ["Not enough candles for EMA200."])
    if close <= ema_200:
        return StrategyDecision("avoid", None, None, None, None, ["Close is below EMA200."])
    if Decimal(feature["ema_50"]) <= ema_200:
        return StrategyDecision("avoid", None, None, None, None, ["EMA50 is not above EMA200."])
    if Decimal(feature["returns_5"]) < Decimal(str(params["returns_5_min"])):
        return StrategyDecision("avoid", None, None, None, None, ["Intermediate trend momentum is too weak."])
    stop = recent_swing_low(recent_candles, int(params["swing_lookback"]))
    return long_decision_from_stop(candle, close, stop, params, ["EMA200 trend-following setup confirmed."])


def recent_swing_low(candles: list[dict[str, Any]], lookback: int) -> Decimal | None:
    if lookback <= 0 or len(candles) < lookback:
        return None
    return min(Decimal(row["low"]) for row in candles[-lookback:])


def long_decision_from_stop(
    candle: dict[str, Any],
    close: Decimal,
    stop: Decimal | None,
    params: dict[str, Any],
    explanation: list[str],
) -> StrategyDecision:
    if stop is None:
        return StrategyDecision("avoid", None, None, None, None, explanation + ["Stop could not be calculated."])
    if stop >= close:
        return StrategyDecision("avoid", None, None, None, None, explanation + ["Stop is not below close."])
    risk_reward = Decimal(str(params["risk_reward"]))
    take_profit = close + ((close - stop) * risk_reward)
    return StrategyDecision("setup", (Decimal(candle["low"]), Decimal(candle["high"])), stop, take_profit, risk_reward, explanation)


def calculate_ema_from_candles(candles: list[dict[str, Any]], period: int) -> Decimal | None:
    if period <= 0 or len(candles) < period:
        return None
    closes = [Decimal(row["close"]) for row in candles]
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = sum(closes[:period]) / Decimal(period)
    for close in closes[period:]:
        ema = (close - ema) * multiplier + ema
    return ema
