from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
import psycopg
from psycopg.types.json import Jsonb

from app.db import get_connection
from app.services.backtester import run_backtest
from app.services.features import load_candles, sync_features
from app.services.strategy import get_strategy_version

router = APIRouter(tags=["backtests"])


@router.post("/backtests")
def create_backtest(
    symbol: str = Query("BTCUSDT"),
    timeframe: str = Query("4h"),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_features(conn, symbol=symbol, timeframe=timeframe)
    strategy = get_strategy_version(conn)
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
    result = run_backtest(candles, list(features), strategy["parameters"])
    walk_forward = result["metrics"]["walk_forward"]
    backtest_row = conn.execute(
        """
        INSERT INTO backtests(symbol, timeframe, strategy_name, strategy_version, train_start, train_end, validation_start, validation_end, metrics)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            symbol,
            timeframe,
            strategy["name"],
            strategy["version"],
            walk_forward.get("train_start"),
            walk_forward.get("train_end"),
            walk_forward.get("validation_start"),
            walk_forward.get("validation_end"),
            Jsonb(result["metrics"]),
        ),
    ).fetchone()
    backtest_id = backtest_row["id"]
    for trade in result["trades"]:
        conn.execute(
            """
            INSERT INTO backtest_trades(backtest_id, symbol, side, entry_time, exit_time, entry_price, exit_price, quantity, stop_loss, take_profit, pnl, pnl_pct, exit_reason)
            VALUES (%(backtest_id)s, %(symbol)s, %(side)s, %(entry_time)s, %(exit_time)s, %(entry_price)s, %(exit_price)s, %(quantity)s, %(stop_loss)s, %(take_profit)s, %(pnl)s, %(pnl_pct)s, %(exit_reason)s)
            """,
            {"backtest_id": backtest_id, **trade},
        )
    conn.commit()
    return {"id": backtest_id, **result}


@router.get("/backtests/{backtest_id}")
def get_backtest(backtest_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    backtest = conn.execute("SELECT * FROM backtests WHERE id = %s", (backtest_id,)).fetchone()
    if not backtest:
        raise HTTPException(status_code=404, detail="Backtest not found")
    trades = conn.execute(
        """
        SELECT *
        FROM backtest_trades
        WHERE backtest_id = %s
        ORDER BY entry_time ASC
        """,
        (backtest_id,),
    ).fetchall()
    return {"backtest": backtest, "trades": list(trades)}
