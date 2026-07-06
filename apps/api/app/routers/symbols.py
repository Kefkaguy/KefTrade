from typing import Any

from fastapi import APIRouter, Depends
import psycopg

from app.db import get_connection

router = APIRouter(tags=["symbols"])


@router.get("/symbols")
def list_symbols(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, sector, market_cap, index_membership, is_active
        FROM symbols
        ORDER BY symbol
        """
    ).fetchall()
    return list(rows)
