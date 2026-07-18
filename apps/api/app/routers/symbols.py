from typing import Any

from fastapi import APIRouter, Depends
import psycopg

from app.db import get_connection
from app.services.research_campaigns import MIN_CAMPAIGN_CANDLES, MIN_CAMPAIGN_FEATURES, data_freshness

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
            c.latest_1h_candle_timestamp,
            c.latest_4h_candle_timestamp,
            COALESCE(f.ready_1h_features, 0) AS ready_1h_features,
            COALESCE(f.ready_4h_features, 0) AS ready_4h_features
        FROM symbols s
        LEFT JOIN (
            SELECT
                symbol,
                COUNT(*) FILTER (WHERE timeframe = '1h') AS ready_1h_candles,
                COUNT(*) FILTER (WHERE timeframe = '4h') AS ready_4h_candles,
                MAX(timestamp) FILTER (WHERE timeframe = '1h') AS latest_1h_candle_timestamp,
                MAX(timestamp) FILTER (WHERE timeframe = '4h') AS latest_4h_candle_timestamp
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
    symbols = [dict(row) for row in rows]
    for symbol in symbols:
        one_hour_freshness = data_freshness(symbol.get("latest_1h_candle_timestamp"), "1h", symbol.get("asset_class"))
        four_hour_freshness = data_freshness(symbol.get("latest_4h_candle_timestamp"), "4h", symbol.get("asset_class"))
        symbol["research_ready"] = (
            bool(symbol.get("is_active"))
            and int(symbol.get("ready_1h_candles") or 0) >= MIN_CAMPAIGN_CANDLES
            and int(symbol.get("ready_4h_candles") or 0) >= MIN_CAMPAIGN_CANDLES
            and int(symbol.get("ready_1h_features") or 0) >= MIN_CAMPAIGN_FEATURES
            and int(symbol.get("ready_4h_features") or 0) >= MIN_CAMPAIGN_FEATURES
            and not one_hour_freshness["stale"]
            and not four_hour_freshness["stale"]
        )
    symbols.sort(key=lambda item: (not item["research_ready"], item["symbol"]))
    return symbols
