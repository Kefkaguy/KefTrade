from decimal import Decimal
from typing import Any

import pandas as pd
import psycopg


def load_candles(conn: psycopg.Connection, symbol: str = "BTCUSDT", timeframe: str = "4h") -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, timeframe, timestamp, open, high, low, close, volume
        FROM candles
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    return list(rows)


def calculate_features(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candles:
        return []

    df = pd.DataFrame(candles)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    df["returns_1"] = df["close"].pct_change(1)
    df["returns_5"] = df["close"].pct_change(5)
    df["ema_20"] = df["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    df["ema_50"] = df["close"].ewm(span=50, adjust=False, min_periods=50).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=14).mean()
    avg_loss = loss.rolling(window=14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi_14"] = 100
    df.loc[(avg_loss == 0) & (avg_gain == 0), "rsi_14"] = 50

    ema_12 = df["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    ema_26 = df["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    df["macd"] = ema_12 - ema_26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    df["volume_change"] = df["volume"].pct_change(1)
    df["volatility_20"] = df["returns_1"].rolling(window=20, min_periods=20).std()
    df["distance_from_ema_20"] = (df["close"] - df["ema_20"]) / df["ema_20"]
    df["distance_from_ema_50"] = (df["close"] - df["ema_50"]) / df["ema_50"]

    feature_rows: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        feature_rows.append(
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "timestamp": row["timestamp"],
                "returns_1": _to_decimal(row.get("returns_1")),
                "returns_5": _to_decimal(row.get("returns_5")),
                "ema_20": _to_decimal(row.get("ema_20")),
                "ema_50": _to_decimal(row.get("ema_50")),
                "rsi_14": _to_decimal(row.get("rsi_14")),
                "macd": _to_decimal(row.get("macd")),
                "macd_signal": _to_decimal(row.get("macd_signal")),
                "volume_change": _to_decimal(row.get("volume_change")),
                "volatility_20": _to_decimal(row.get("volatility_20")),
                "distance_from_ema_20": _to_decimal(row.get("distance_from_ema_20")),
                "distance_from_ema_50": _to_decimal(row.get("distance_from_ema_50")),
            }
        )
    return feature_rows


def _to_decimal(value: Any) -> Decimal | None:
    if pd.isna(value):
        return None
    return Decimal(str(round(float(value), 12)))


def upsert_features(conn: psycopg.Connection, feature_rows: list[dict[str, Any]]) -> int:
    affected = 0
    for row in feature_rows:
        result = conn.execute(
            """
            INSERT INTO features(
                symbol, timeframe, timestamp, returns_1, returns_5, ema_20, ema_50, rsi_14,
                macd, macd_signal, volume_change, volatility_20, distance_from_ema_20, distance_from_ema_50
            )
            VALUES (
                %(symbol)s, %(timeframe)s, %(timestamp)s, %(returns_1)s, %(returns_5)s, %(ema_20)s, %(ema_50)s, %(rsi_14)s,
                %(macd)s, %(macd_signal)s, %(volume_change)s, %(volatility_20)s, %(distance_from_ema_20)s, %(distance_from_ema_50)s
            )
            ON CONFLICT(symbol, timeframe, timestamp)
            DO UPDATE SET
                returns_1 = EXCLUDED.returns_1,
                returns_5 = EXCLUDED.returns_5,
                ema_20 = EXCLUDED.ema_20,
                ema_50 = EXCLUDED.ema_50,
                rsi_14 = EXCLUDED.rsi_14,
                macd = EXCLUDED.macd,
                macd_signal = EXCLUDED.macd_signal,
                volume_change = EXCLUDED.volume_change,
                volatility_20 = EXCLUDED.volatility_20,
                distance_from_ema_20 = EXCLUDED.distance_from_ema_20,
                distance_from_ema_50 = EXCLUDED.distance_from_ema_50
            """,
            row,
        )
        affected += result.rowcount or 0
    return affected


def sync_features(conn: psycopg.Connection, symbol: str = "BTCUSDT", timeframe: str = "4h") -> dict[str, Any]:
    candles = load_candles(conn, symbol, timeframe)
    feature_rows = calculate_features(candles)
    upserted = upsert_features(conn, feature_rows)
    conn.commit()
    complete_rows = sum(1 for row in feature_rows if row["ema_50"] is not None and row["rsi_14"] is not None)
    return {"symbol": symbol, "timeframe": timeframe, "calculated": len(feature_rows), "usable": complete_rows, "upserted": upserted}
