"""Sector-relative context, so idiosyncratic flow is separable from beta.

A symbol falling three percent on heavy volume while its whole sector falls
three percent is not evidence of forced selling in that name; it is the sector
moving. The same move against a flat sector is. Every hypothesis about forced
single-name flow needs that distinction, and a bar in isolation cannot make it.

Nothing here requires new ingestion: it is the candles already held plus the
sector map. It is nonetheless information the individual bar does not contain,
which is the point.

The module contains no campaign, broker, order-submission, or UI code.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import fmean, median, pstdev
from typing import Any, Sequence

import psycopg

SECTOR_FLOW_VERSION = "intraday_sector_flow_v1"

# A peer group thinner than this cannot say what "the sector did"; the
# relative measure is withheld rather than computed against one other name.
MINIMUM_PEERS = 4


def _round(value: float | None) -> float | None:
    return round(float(value), 8) if value is not None else None


def sector_relative_bars(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    sector_by_symbol: dict[str, str],
    minimum_peers: int = MINIMUM_PEERS,
) -> dict[str, dict[datetime, dict[str, Any]]]:
    """Per bar, a symbol's move and participation against its sector peers.

    The peer aggregate deliberately excludes the symbol itself: including it
    would let a large move drag its own benchmark and shrink the residual it
    is supposed to be measured against.
    """
    sectors = {
        str(key).upper(): str(value)
        for key, value in (sector_by_symbol or {}).items()
        if value
    }
    # returns[sector][timestamp] -> list of (symbol, return, relative volume)
    grouped: dict[str, dict[datetime, list[tuple[str, float, float | None]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for symbol, rows in candles_by_symbol.items():
        sector = sectors.get(str(symbol).upper())
        if sector is None:
            continue
        for row in rows:
            open_price = float(row["open"])
            if open_price <= 0:
                continue
            value = (float(row["close"]) - open_price) / open_price
            relative_volume = row.get("session_relative_volume")
            grouped[sector][row["timestamp"]].append(
                (
                    str(symbol).upper(),
                    value,
                    float(relative_volume) if relative_volume is not None else None,
                )
            )

    output: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for sector, by_timestamp in grouped.items():
        for timestamp, entries in by_timestamp.items():
            if len(entries) < minimum_peers + 1:
                continue
            for symbol, value, relative_volume in entries:
                peers = [item for item in entries if item[0] != symbol]
                peer_returns = [item[1] for item in peers]
                peer_volumes = [item[2] for item in peers if item[2] is not None]
                peer_median = median(peer_returns)
                dispersion = pstdev(peer_returns) if len(peer_returns) > 1 else 0.0
                residual = value - peer_median
                output[symbol][timestamp] = {
                    "sector": sector,
                    "peers": len(peers),
                    "bar_return": _round(value),
                    "sector_median_return": _round(peer_median),
                    # What the name did that its sector did not.
                    "sector_residual_return": _round(residual),
                    "sector_dispersion": _round(dispersion),
                    # Residual scaled by how much the sector was moving at all;
                    # a 50 bps residual means something different on a calm
                    # afternoon than during a sector-wide repricing.
                    "standardized_residual": (
                        _round(residual / dispersion) if dispersion > 0 else None
                    ),
                    "relative_volume": _round(relative_volume),
                    "sector_median_relative_volume": (
                        _round(median(peer_volumes)) if peer_volumes else None
                    ),
                    # Above one means this name is being traded harder than its
                    # sector, which is what forced single-name flow looks like.
                    "excess_participation": (
                        _round(relative_volume / fmean(peer_volumes))
                        if relative_volume is not None
                        and peer_volumes
                        and fmean(peer_volumes) > 0
                        else None
                    ),
                }
    return dict(output)


def dataset_sector_coverage(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    timeframe: str,
    minimum_peers: int = MINIMUM_PEERS,
) -> dict[str, Any]:
    """Peer-group availability for a snapshot, computed in the database.

    Two questions, and the second is the one that decides whether the factor
    can produce events at all: not "does this symbol have a sector" but "at the
    instants it traded, were enough of its peers trading too".  A symbol whose
    sector is nominally well populated still has no peer group on a bar where
    the rest of the sector has no row.
    """
    members = conn.execute(
        """
        SELECT COALESCE(s.sector, 'unknown') AS sector, COUNT(*) AS symbols
        FROM (
            SELECT DISTINCT symbol
            FROM research_dataset_candles
            WHERE dataset_id = %s AND timeframe = %s
        ) c
        LEFT JOIN symbols s ON s.symbol = c.symbol
        GROUP BY 1
        ORDER BY 1
        """,
        (dataset_id, timeframe),
    ).fetchall()
    by_sector = {str(row["sector"]): int(row["symbols"]) for row in members}
    total_symbols = sum(by_sector.values())
    unknown = by_sector.pop("unknown", 0)
    usable = {
        sector: count for sector, count in by_sector.items() if count > minimum_peers
    }

    # Per (sector, bar), how many of the sector's symbols actually have a row.
    # A plain hash aggregate over the snapshot -- no correlated re-scan, which
    # is what made the earlier power query pathological.
    bars = conn.execute(
        """
        SELECT COALESCE(SUM(present), 0) AS symbol_bars,
               COALESCE(SUM(present) FILTER (WHERE present > %s), 0)
                   AS symbol_bars_with_peers,
               COUNT(DISTINCT timestamp) AS bars
        FROM (
            SELECT c.timestamp, s.sector, COUNT(*) AS present
            FROM research_dataset_candles c
            JOIN symbols s ON s.symbol = c.symbol
            WHERE c.dataset_id = %s AND c.timeframe = %s AND s.sector IS NOT NULL
            GROUP BY c.timestamp, s.sector
        ) grouped
        """,
        (minimum_peers, dataset_id, timeframe),
    ).fetchone()
    sector_bars = int((bars or {}).get("symbol_bars") or 0)
    with_peers = int((bars or {}).get("symbol_bars_with_peers") or 0)

    return {
        "sector_flow_version": SECTOR_FLOW_VERSION,
        "dataset_id": dataset_id,
        "timeframe": timeframe,
        "symbols": total_symbols,
        "symbols_with_sector": total_symbols - unknown,
        "symbols_without_sector": unknown,
        "sector_coverage": (
            _round((total_symbols - unknown) / total_symbols) if total_symbols else None
        ),
        "sectors": len(by_sector),
        "sectors_with_enough_peers": len(usable),
        "symbols_in_usable_sectors": sum(usable.values()),
        "minimum_peers": minimum_peers,
        "sector_bars": sector_bars,
        "sector_bars_with_enough_peers": with_peers,
        # The share of the snapshot the factor can actually score. This, not
        # the symbol count, is what bounds its event supply.
        "bar_level_peer_coverage": (
            _round(with_peers / sector_bars) if sector_bars else None
        ),
        "distinct_bars": int((bars or {}).get("bars") or 0),
        "by_sector": dict(sorted(by_sector.items())),
    }
