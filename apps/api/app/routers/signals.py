from typing import Any

from fastapi import APIRouter, Depends, Query
import psycopg
from psycopg.types.json import Jsonb

from app.db import get_connection
from app.services.features import load_candles, sync_features
from app.services.strategy import get_strategy_version, trend_pullback_decision

router = APIRouter(tags=["signals"])


@router.get("/signals")
def list_signals(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM signals
        ORDER BY generated_at DESC
        LIMIT 50
        """
    ).fetchall()
    return list(rows)


@router.get("/signals/{symbol}")
def get_latest_signal(
    symbol: str,
    timeframe: str = Query("4h"),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_features(conn, symbol=symbol, timeframe=timeframe)
    strategy = get_strategy_version(conn)
    candles = load_candles(conn, symbol, timeframe)
    feature = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol, timeframe),
    ).fetchone()
    if not candles or not feature:
        return {"symbol": symbol, "timeframe": timeframe, "signal": "avoid", "explanation": ["Sync candles before requesting a signal."]}

    candle = candles[-1]
    decision = trend_pullback_decision(candle, feature, candles, strategy["parameters"])
    entry_zone = [float(value) for value in decision.entry_zone] if decision.entry_zone else None
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_name": strategy["name"],
        "strategy_version": strategy["version"],
        "signal": decision.signal,
        "generated_at": candle["timestamp"],
        "entry_zone": entry_zone,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
        "risk_reward": decision.risk_reward,
        "explanation": decision.explanation,
    }
    db_payload = {
        **payload,
        "entry_zone": Jsonb(payload["entry_zone"]) if payload["entry_zone"] else None,
        "explanation": Jsonb(payload["explanation"]),
    }
    conn.execute(
        """
        INSERT INTO signals(symbol, timeframe, strategy_name, strategy_version, signal, generated_at, entry_zone, stop_loss, take_profit, risk_reward, explanation)
        VALUES (%(symbol)s, %(timeframe)s, %(strategy_name)s, %(strategy_version)s, %(signal)s, %(generated_at)s, %(entry_zone)s, %(stop_loss)s, %(take_profit)s, %(risk_reward)s, %(explanation)s)
        """,
        db_payload,
    )
    conn.commit()
    return payload
