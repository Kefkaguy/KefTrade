from typing import Any

from fastapi import APIRouter, Depends
import psycopg

from app.db import get_connection

router = APIRouter(tags=["symbols"])


@router.get("/symbols")
def list_symbols(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.symbol,
            s.asset_class,
            s.exchange,
            s.currency,
            s.name,
            s.provider_symbol,
            s.primary_provider,
            s.sector,
            s.market_cap,
            s.index_membership,
            s.is_active,
            COALESCE(c.ready_1h_candles, 0) AS ready_1h_candles,
            COALESCE(c.ready_4h_candles, 0) AS ready_4h_candles,
            COALESCE(f.ready_1h_features, 0) AS ready_1h_features,
            COALESCE(f.ready_4h_features, 0) AS ready_4h_features
        FROM symbols s
        LEFT JOIN (
            SELECT
                symbol,
                COUNT(*) FILTER (WHERE timeframe = '1h') AS ready_1h_candles,
                COUNT(*) FILTER (WHERE timeframe = '4h') AS ready_4h_candles
            FROM candles
            WHERE timeframe IN ('1h', '4h')
            GROUP BY symbol
        ) c ON c.symbol = s.symbol
        LEFT JOIN (
            SELECT
                symbol,
                COUNT(*) FILTER (WHERE timeframe = '1h') AS ready_1h_features,
                COUNT(*) FILTER (WHERE timeframe = '4h') AS ready_4h_features
            FROM features
            WHERE timeframe IN ('1h', '4h')
            GROUP BY symbol
        ) f ON f.symbol = s.symbol
        ORDER BY
            (COALESCE(c.ready_1h_candles, 0) >= 120 AND COALESCE(c.ready_4h_candles, 0) >= 120 AND COALESCE(f.ready_1h_features, 0) >= 80 AND COALESCE(f.ready_4h_features, 0) >= 80) DESC,
            s.symbol
        """
    ).fetchall()
    return list(rows)
