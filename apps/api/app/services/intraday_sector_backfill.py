"""Backfill sector classification for symbols that have none.

The sector map was a thirteen-entry table written by hand.  Every symbol the
Alpaca asset sync added since then has a null sector, because that feed does
not carry one.  That is why a 237-symbol universe reports eleven symbols with
a sector and one usable peer group: not a market fact, a metadata hole.

A known limitation, recorded rather than papered over: the classification
retrieved here is *current*, not point-in-time.  A company that changed sector
is grouped under today's label for its whole history.  This is acceptable for
a peer-group control and would not be for a traded signal -- the sector is
never the thing being predicted, only the thing being controlled for -- but it
is a real assumption and it is stamped on every row.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from typing import Any, Sequence

import psycopg

SECTOR_BACKFILL_VERSION = "intraday_sector_backfill_v1"

# Point-in-time membership is enforced elsewhere; sector classification is not
# available point-in-time from any source this system has, so the assumption
# is named here and travels with the data.
SECTOR_PROVENANCE = "current_classification_applied_to_full_history"


def symbols_missing_sector(
    conn: psycopg.Connection, *, universe_key: str | None = None
) -> list[str]:
    """Symbols with no sector, optionally limited to one universe's members."""
    if universe_key:
        rows = conn.execute(
            """
            SELECT DISTINCT m.symbol
            FROM research_point_in_time_universe_membership m
            LEFT JOIN symbols s ON s.symbol = m.symbol
            WHERE m.universe_key = %s AND s.sector IS NULL
            ORDER BY 1
            """,
            (universe_key,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT symbol FROM symbols WHERE sector IS NULL ORDER BY symbol"
        ).fetchall()
    return [str(row["symbol"]) for row in rows]


def fetch_sector(symbol: str) -> dict[str, Any]:
    """Look up one symbol's sector and industry.

    Failure is returned, never raised: one unreachable ticker must not abandon
    a backfill of two hundred.
    """
    try:
        import yfinance as yf
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise RuntimeError("Install yfinance to backfill sector metadata.") from error
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as error:  # noqa: BLE001 - recorded per symbol
        return {"symbol": symbol, "sector": None, "error": str(error)[:200]}
    sector = info.get("sector")
    return {
        "symbol": symbol,
        "sector": str(sector) if sector else None,
        "industry": str(info.get("industry")) if info.get("industry") else None,
        "name": str(info.get("shortName")) if info.get("shortName") else None,
        "error": None if sector else "no sector returned",
    }


def apply_sector(
    conn: psycopg.Connection, *, symbol: str, sector: str, industry: str | None
) -> bool:
    """Write a sector onto an existing symbol row.

    Only ever fills a hole. An existing classification is left alone, so a
    rerun cannot silently reclassify a symbol whose peer group has already
    been used in a measured result.
    """
    result = conn.execute(
        "UPDATE symbols SET sector = %s WHERE symbol = %s AND sector IS NULL",
        (sector, symbol),
    )
    if not result.rowcount:
        return False
    # Provenance lives beside the classification, not inside it, so the
    # point-in-time caveat is attached to every symbol it applies to.
    conn.execute(
        """
        INSERT INTO symbol_sector_provenance(
            symbol, sector, industry, source, provenance, backfill_version
        )
        VALUES (%s, %s, %s, 'yfinance', %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            sector = EXCLUDED.sector,
            industry = EXCLUDED.industry,
            source = EXCLUDED.source,
            provenance = EXCLUDED.provenance,
            backfill_version = EXCLUDED.backfill_version
        """,
        (symbol, sector, industry, SECTOR_PROVENANCE, SECTOR_BACKFILL_VERSION),
    )
    return True


def backfill_sectors(
    conn: psycopg.Connection,
    *,
    universe_key: str | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fill missing sectors one symbol at a time, isolating failures."""
    targets = (
        [item.upper() for item in symbols]
        if symbols
        else symbols_missing_sector(conn, universe_key=universe_key)
    )
    if limit is not None:
        targets = targets[:limit]

    filled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for symbol in targets:
        result = fetch_sector(symbol)
        if not result["sector"]:
            failed.append(result)
            continue
        if apply_sector(
            conn,
            symbol=symbol,
            sector=result["sector"],
            industry=result.get("industry"),
        ):
            filled.append({"symbol": symbol, "sector": result["sector"]})
        else:
            failed.append({**result, "error": "row absent or already classified"})
    conn.commit()

    by_sector: dict[str, int] = {}
    for row in filled:
        by_sector[row["sector"]] = by_sector.get(row["sector"], 0) + 1
    return {
        "sector_backfill_version": SECTOR_BACKFILL_VERSION,
        "sector_provenance": SECTOR_PROVENANCE,
        "symbols_missing_sector": len(targets),
        "symbols_filled": len(filled),
        "symbols_still_missing": len(failed),
        "by_sector": dict(sorted(by_sector.items())),
        # Truncated: a long tail of delisted tickers is expected and is not
        # worth burying the summary under.
        "failures": failed[:25],
    }
