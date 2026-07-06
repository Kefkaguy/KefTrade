from decimal import Decimal
from typing import Any

import psycopg

from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.features import load_candles, sync_features


HIGH_VOLATILITY_THRESHOLD = Decimal("0.02")
LOW_VOLATILITY_THRESHOLD = Decimal("0.01")
TREND_STRENGTH_THRESHOLD = Decimal("0.01")
MOMENTUM_THRESHOLD = Decimal("0")


def classify_market_regime(candle: dict[str, Any], feature: dict[str, Any]) -> dict[str, Any]:
    close = Decimal(candle["close"])
    ema_50 = feature.get("ema_50")
    returns_5 = feature.get("returns_5")
    distance_from_ema_50 = feature.get("distance_from_ema_50")
    volatility_20 = feature.get("volatility_20")

    if ema_50 is None or returns_5 is None or distance_from_ema_50 is None:
        trend_regime = "unknown"
        trend_strength = Decimal("0")
        close_vs_ema50 = None
    else:
        ema = Decimal(ema_50)
        momentum = Decimal(returns_5)
        close_vs_ema50 = (close - ema) / ema if ema else Decimal("0")
        trend_strength = abs(Decimal(distance_from_ema_50))
        if close_vs_ema50 >= TREND_STRENGTH_THRESHOLD and momentum > MOMENTUM_THRESHOLD:
            trend_regime = "bull_trend"
        elif close_vs_ema50 <= -TREND_STRENGTH_THRESHOLD and momentum < MOMENTUM_THRESHOLD:
            trend_regime = "bear_trend"
        else:
            trend_regime = "sideways"

    if volatility_20 is None:
        volatility_regime = "unknown"
        volatility_score = None
    else:
        volatility_score = Decimal(volatility_20)
        if volatility_score >= HIGH_VOLATILITY_THRESHOLD:
            volatility_regime = "high_volatility"
        elif volatility_score <= LOW_VOLATILITY_THRESHOLD:
            volatility_regime = "low_volatility"
        else:
            volatility_regime = "normal_volatility"

    return {
        "symbol": candle["symbol"],
        "timeframe": candle["timeframe"],
        "timestamp": candle["timestamp"],
        "trend_regime": trend_regime,
        "volatility_regime": volatility_regime,
        "trend_strength": trend_strength,
        "volatility_score": volatility_score,
        "close_vs_ema50": close_vs_ema50,
    }


def calculate_regimes(candles: list[dict[str, Any]], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    features_by_time = {row["timestamp"]: row for row in features}
    regimes = []
    for candle in candles:
        feature = features_by_time.get(candle["timestamp"])
        if not feature:
            continue
        regimes.append(classify_market_regime(candle, feature))
    return regimes


def upsert_regimes(conn: psycopg.Connection, regimes: list[dict[str, Any]]) -> int:
    affected = 0
    for regime in regimes:
        result = conn.execute(
            """
            INSERT INTO market_regimes(
                symbol, timeframe, timestamp, trend_regime, volatility_regime,
                trend_strength, volatility_score, close_vs_ema50
            )
            VALUES (
                %(symbol)s, %(timeframe)s, %(timestamp)s, %(trend_regime)s, %(volatility_regime)s,
                %(trend_strength)s, %(volatility_score)s, %(close_vs_ema50)s
            )
            ON CONFLICT(symbol, timeframe, timestamp)
            DO UPDATE SET
                trend_regime = EXCLUDED.trend_regime,
                volatility_regime = EXCLUDED.volatility_regime,
                trend_strength = EXCLUDED.trend_strength,
                volatility_score = EXCLUDED.volatility_score,
                close_vs_ema50 = EXCLUDED.close_vs_ema50,
                detected_at = NOW()
            """,
            regime,
        )
        affected += result.rowcount or 0
    return affected


def sync_market_regimes(conn: psycopg.Connection, symbol: str = DEFAULT_DEV_SYMBOL, timeframe: str = DEFAULT_DEV_TIMEFRAME) -> dict[str, Any]:
    sync_features(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe)
    features = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    regimes = calculate_regimes(candles, list(features))
    upserted = upsert_regimes(conn, regimes)
    conn.commit()
    counts = summarize_regimes(regimes)
    return {"symbol": symbol, "timeframe": timeframe, "calculated": len(regimes), "upserted": upserted, "counts": counts}


def load_regimes(conn: psycopg.Connection, symbol: str = DEFAULT_DEV_SYMBOL, timeframe: str = DEFAULT_DEV_TIMEFRAME) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, timeframe, timestamp, trend_regime, volatility_regime,
               trend_strength, volatility_score, close_vs_ema50
        FROM market_regimes
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    return list(rows)


def summarize_regimes(regimes: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    trend_counts: dict[str, int] = {}
    volatility_counts: dict[str, int] = {}
    for regime in regimes:
        trend = regime["trend_regime"]
        volatility = regime["volatility_regime"]
        trend_counts[trend] = trend_counts.get(trend, 0) + 1
        volatility_counts[volatility] = volatility_counts.get(volatility, 0) + 1
    return {"trend": trend_counts, "volatility": volatility_counts}
