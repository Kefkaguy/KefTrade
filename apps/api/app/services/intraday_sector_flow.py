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


def sector_flow_coverage(
    candles_by_symbol: dict[str, Sequence[dict[str, Any]]],
    *,
    sector_by_symbol: dict[str, str],
) -> dict[str, Any]:
    """How much of the universe can be measured against a peer group at all."""
    sectors = {
        str(key).upper(): str(value)
        for key, value in (sector_by_symbol or {}).items()
        if value
    }
    known = [symbol for symbol in candles_by_symbol if str(symbol).upper() in sectors]
    counts: dict[str, int] = defaultdict(int)
    for symbol in known:
        counts[sectors[str(symbol).upper()]] += 1
    usable = [sector for sector, count in counts.items() if count > MINIMUM_PEERS]
    return {
        "sector_flow_version": SECTOR_FLOW_VERSION,
        "symbols": len(candles_by_symbol),
        "symbols_with_sector": len(known),
        "sector_coverage": (
            _round(len(known) / len(candles_by_symbol)) if candles_by_symbol else None
        ),
        "sectors": len(counts),
        "sectors_with_enough_peers": len(usable),
        "symbols_in_usable_sectors": sum(
            count for sector, count in counts.items() if sector in usable
        ),
        "minimum_peers": MINIMUM_PEERS,
        "by_sector": dict(sorted(counts.items())),
    }
