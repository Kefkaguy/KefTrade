"""Dataset loading for cross-sectional families -- Phase 13.10 follow-up.

Reuses the existing single-symbol intraday dataset builder entirely
unmodified (`load_intraday_backtest_dataset`), then attaches one extra
feature -- `cross_sectional_momentum_percentile` -- computed from every
other symbol in the campaign's own universe over the SAME immutable dataset
snapshot. Nothing about candle loading, session-end indexing, or market
array construction changes; a cross-sectional family is otherwise an
ordinary V2Strategy from the simulator's point of view.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app.services.labs.intraday.cross_sectional import (
    compute_cross_sectional_percentiles,
    merge_percentiles_into_features,
)

# Architectures that need the full campaign universe's candles (not just
# their own symbol's) to compute their signal. Checked by
# run_campaign_job's dispatch BEFORE the ordinary intraday-lab check, so
# every existing single-symbol family's dispatch is completely unaffected.
CROSS_SECTIONAL_ARCHITECTURES = {"cross_sectional_momentum_v2", "cross_sectional_reversal_v2"}


def is_cross_sectional_candidate(candidate_payload: dict[str, Any]) -> bool:
    parameters = candidate_payload.get("parameters") or {}
    return parameters.get("strategy_architecture") in CROSS_SECTIONAL_ARCHITECTURES


def load_cross_sectional_intraday_dataset(
    conn: psycopg.Connection,
    symbol: str,
    timeframe: str,
    *,
    dataset_id: int,
    lookback_bars: int = 8,
) -> dict[str, Any]:
    """Same return shape as `load_intraday_backtest_dataset`, with
    `cross_sectional_momentum_percentile` attached to every feature row.

    The universe is read from the dataset snapshot's own manifest
    (`research_dataset_manifests.assets`) -- the exact symbol set the
    campaign was created against, not a separately-specified list that
    could drift from what was actually snapshotted.
    """
    from app.services.labs.intraday.dataset import load_intraday_backtest_dataset
    from app.services.research_architecture import load_snapshot_candles

    dataset = load_intraday_backtest_dataset(conn, symbol, timeframe, dataset_id=dataset_id)

    manifest = conn.execute(
        "SELECT assets FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"No dataset manifest for dataset_id={dataset_id}.")
    universe = [str(item) for item in (manifest["assets"] or [])]
    if symbol not in universe:
        universe = [*universe, symbol]

    candles_by_symbol = {peer: load_snapshot_candles(conn, dataset_id, peer, timeframe) for peer in universe}
    candles_by_symbol = {peer: rows for peer, rows in candles_by_symbol.items() if rows}
    percentiles = compute_cross_sectional_percentiles(candles_by_symbol, lookback_bars=lookback_bars)

    dataset["features"] = merge_percentiles_into_features(dataset["features"], percentiles.get(symbol, {}))
    dataset["cross_sectional_universe"] = sorted(candles_by_symbol)
    return dataset
