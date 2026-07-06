from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal


SignalName = Literal["setup", "watchlist", "avoid"]


@dataclass(frozen=True)
class StrategyDecision:
    signal: SignalName
    entry_zone: tuple[Decimal, Decimal] | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    risk_reward: Decimal | None
    explanation: list[str]


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


def calculate_ema_from_candles(candles: list[dict[str, Any]], period: int) -> Decimal | None:
    if period <= 0 or len(candles) < period:
        return None
    closes = [Decimal(row["close"]) for row in candles]
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = sum(closes[:period]) / Decimal(period)
    for close in closes[period:]:
        ema = (close - ema) * multiplier + ema
    return ema
