"""Evidence-guided low-timeframe expansion campaigns.

The 15m/30m work should not be another broad random sweep. Production evidence
showed the blocker is edge and frequency appearing separately: profitable
parents are often under the trade-count gate, while high-frequency parents
usually have no edge. This module turns promoted and near-pass intraday rows
into a bounded parent-child campaign around the strongest observed regions.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Iterable
from uuid import uuid4

import psycopg

from app.services.research_campaigns import candidate_from_payload
from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

LOW_TIMEFRAME_EXPANSION_VERSION = "low_timeframe_near_pass_expansion_v1"
SUPPORTED_LOW_TIMEFRAMES = ("15m", "30m")
DEFAULT_LOW_TIMEFRAME_EXPANSION_TIMEFRAMES = ("30m",)


def _metric(result: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = (result.get("metrics") or {}).get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _passes_near_edge(row: dict[str, Any], *, min_trades: int, max_drawdown: float) -> bool:
    result = row.get("result") or {}
    profit_factor = _metric(result, "profit_factor")
    expectancy = _metric(result, "expectancy_per_trade")
    trades = _metric(result, "number_of_trades")
    drawdown = _metric(result, "max_drawdown", 1.0)
    return profit_factor >= 1.20 and expectancy > 0 and trades >= min_trades and drawdown <= max_drawdown


def _sort_parent_key(row: dict[str, Any], preferred_family: str | None) -> tuple[Any, ...]:
    result = row.get("result") or {}
    family = str(row.get("strategy_family") or "")
    timeframe = str(row.get("timeframe") or "")
    return (
        1 if row.get("status") == "promoted" else 0,
        1 if timeframe == "30m" else 0,
        1 if preferred_family and family == preferred_family else 0,
        _metric(result, "number_of_trades"),
        _metric(result, "profit_factor"),
        _metric(result, "expectancy_per_trade"),
        -_metric(result, "max_drawdown", 1.0),
    )


def select_low_timeframe_parent_rows(
    rows: Iterable[dict[str, Any]],
    *,
    parent_limit: int = 12,
    preferred_family: str | None = "Momentum",
    min_trades: int = 12,
    max_drawdown: float = 0.10,
) -> list[dict[str, Any]]:
    """Pick promoted/near-pass parents without over-weighting duplicate jobs."""

    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in sorted(rows, key=lambda item: _sort_parent_key(item, preferred_family), reverse=True):
        timeframe = str(row.get("timeframe") or "")
        if timeframe not in SUPPORTED_LOW_TIMEFRAMES:
            continue
        if not row.get("candidate"):
            continue
        if row.get("status") != "promoted" and not _passes_near_edge(row, min_trades=min_trades, max_drawdown=max_drawdown):
            continue
        identity = (str(row.get("candidate_id") or ""), str(row.get("asset") or ""), timeframe)
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(dict(row))
        if len(selected) >= parent_limit:
            break
    return selected


def _numeric_variants(key: str, value: Any) -> list[Any]:
    if isinstance(value, bool):
        return []
    try:
        number = float(value)
    except (TypeError, ValueError):
        return []

    lowered = {
        "threshold",
        "min",
        "minimum",
        "buffer",
        "distance",
        "deviation",
        "percentile",
        "volume",
        "relative",
    }
    widened = {"lookback", "window", "period", "bars"}
    key_lower = key.lower()
    variants: list[Any] = []
    if key in {"reward_risk_multiple", "risk_reward"}:
        variants = [1.2, 1.5, 1.8, 2.0]
    elif key == "maximum_entries_per_session":
        variants = [1, 2]
    elif key == "minimum_minutes_before_close_for_entry":
        variants = [0, 15, 30]
    elif any(token in key_lower for token in lowered):
        variants = [number * 0.75, number * 0.9, number * 1.1]
    elif any(token in key_lower for token in widened):
        variants = [max(2, round(number - 2)), max(2, round(number - 1)), round(number + 1), round(number + 2)]

    cleaned: list[Any] = []
    for candidate in variants:
        if candidate == number:
            continue
        if isinstance(value, int):
            candidate = int(round(float(candidate)))
        elif isinstance(value, str):
            candidate = f"{float(candidate):.6g}"
        cleaned.append(candidate)
    return cleaned


def _source_metadata(parent_row: dict[str, Any]) -> dict[str, Any]:
    result = parent_row.get("result") or {}
    metrics = result.get("metrics") or {}
    return {
        "campaign_id": parent_row.get("campaign_id"),
        "job_id": parent_row.get("job_id") or parent_row.get("id"),
        "asset": parent_row.get("asset"),
        "timeframe": parent_row.get("timeframe"),
        "status": parent_row.get("status"),
        "strategy_family": parent_row.get("strategy_family"),
        "metrics": {
            "profit_factor": metrics.get("profit_factor"),
            "expectancy_per_trade": metrics.get("expectancy_per_trade"),
            "number_of_trades": metrics.get("number_of_trades"),
            "max_drawdown": metrics.get("max_drawdown"),
        },
    }


def generate_low_timeframe_variants(parent_row: dict[str, Any], *, variants_per_parent: int = 8) -> list[DiscoveryCandidate]:
    parent = candidate_from_payload(parent_row["candidate"])
    source = _source_metadata(parent_row)
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()

    def add_variant(parameters: dict[str, Any]) -> None:
        if len(candidates) >= variants_per_parent:
            return
        enriched = {
            **parameters,
            "low_timeframe_expansion_version": LOW_TIMEFRAME_EXPANSION_VERSION,
            "generation_channel": "low_timeframe_near_pass_expansion",
            "source_evidence": source,
        }
        canonical_key = canonical_candidate_key(parent.blocks, enriched, parent.candidate_id)
        if canonical_key in seen:
            return
        seen.add(canonical_key)
        candidates.append(
            DiscoveryCandidate(
                candidate_id=f"ltf_{sha256(canonical_key.encode()).hexdigest()[:14]}",
                family_id=parent.family_id,
                parent_candidate_id=parent.candidate_id,
                generation=parent.generation + 1,
                blocks=dict(parent.blocks),
                parameters=enriched,
                complexity=parent.complexity + 1,
                canonical_key=canonical_key,
            )
        )

    add_variant(dict(parent.parameters))
    mutable_parameters = sorted((parent.parameters or {}).items())
    for key, value in mutable_parameters:
        if len(candidates) >= variants_per_parent:
            break
        if key in {"initial_equity", "fee_rate", "slippage_rate", "risk_per_trade"}:
            continue
        for next_value in _numeric_variants(key, value):
            mutated = dict(parent.parameters)
            mutated[key] = next_value
            add_variant(mutated)
            if len(candidates) >= variants_per_parent:
                break
    return candidates


def low_timeframe_expansion_blueprint(
    rows: Iterable[dict[str, Any]],
    *,
    parent_limit: int = 12,
    variants_per_parent: int = 8,
    asset_limit: int = 8,
    timeframes: list[str] | None = None,
    preferred_family: str | None = "Momentum",
) -> dict[str, Any]:
    parents = select_low_timeframe_parent_rows(rows, parent_limit=parent_limit, preferred_family=preferred_family)
    candidates: list[DiscoveryCandidate] = []
    seen_candidates: set[str] = set()
    assets: list[str] = []
    for parent in parents:
        symbol = str(parent.get("asset") or "").upper()
        if symbol and symbol not in assets:
            assets.append(symbol)
        for candidate in generate_low_timeframe_variants(parent, variants_per_parent=variants_per_parent):
            if candidate.canonical_key in seen_candidates:
                continue
            seen_candidates.add(candidate.canonical_key)
            candidates.append(candidate)

    selected_timeframes = [tf for tf in (timeframes or list(DEFAULT_LOW_TIMEFRAME_EXPANSION_TIMEFRAMES)) if tf in SUPPORTED_LOW_TIMEFRAMES]
    if not selected_timeframes:
        selected_timeframes = list(DEFAULT_LOW_TIMEFRAME_EXPANSION_TIMEFRAMES)

    return {
        "parents": parents,
        "candidates": candidates,
        "assets": assets[:asset_limit],
        "timeframes": selected_timeframes,
        "expansion_version": LOW_TIMEFRAME_EXPANSION_VERSION,
    }


def _load_parent_rows(conn: psycopg.Connection, *, row_limit: int, preferred_family: str | None) -> list[dict[str, Any]]:
    family_clause = "AND COALESCE(j.strategy_family, '') = %(preferred_family)s" if preferred_family else ""
    rows = conn.execute(
        f"""
        SELECT
            j.id AS job_id,
            j.campaign_id,
            j.candidate_id,
            j.symbol AS asset,
            j.timeframe,
            j.status,
            j.strategy_family,
            j.candidate,
            j.result
        FROM research_campaign_jobs j
        WHERE j.simulation_only = TRUE
          AND j.timeframe IN ('15m', '30m')
          AND j.candidate IS NOT NULL
          AND j.result IS NOT NULL
          {family_clause}
        ORDER BY j.updated_at DESC NULLS LAST, j.id DESC
        LIMIT %(row_limit)s
        """,
        {"row_limit": row_limit, "preferred_family": preferred_family},
    ).fetchall()
    return [dict(row) for row in rows]


def create_low_timeframe_expansion_campaign(
    conn: psycopg.Connection,
    *,
    name: str | None = None,
    parent_limit: int = 12,
    variants_per_parent: int = 8,
    asset_limit: int = 8,
    timeframes: list[str] | None = None,
    preferred_family: str | None = "Momentum",
) -> dict[str, Any]:
    from app.services.labs.intraday.campaign import _create_intraday_campaign

    rows = _load_parent_rows(conn, row_limit=max(parent_limit * 80, 500), preferred_family=preferred_family)
    blueprint = low_timeframe_expansion_blueprint(
        rows,
        parent_limit=parent_limit,
        variants_per_parent=variants_per_parent,
        asset_limit=asset_limit,
        timeframes=timeframes,
        preferred_family=preferred_family,
    )
    if not blueprint["parents"] and preferred_family:
        rows = _load_parent_rows(conn, row_limit=max(parent_limit * 80, 500), preferred_family=None)
        blueprint = low_timeframe_expansion_blueprint(
            rows,
            parent_limit=parent_limit,
            variants_per_parent=variants_per_parent,
            asset_limit=asset_limit,
            timeframes=timeframes,
            preferred_family=None,
        )
    if not blueprint["parents"]:
        raise ValueError("no promoted or near-pass 15m/30m parent rows found")
    if not blueprint["assets"]:
        raise ValueError("no source assets found for low-timeframe expansion")

    result = _create_intraday_campaign(
        conn,
        name=name or "30m Momentum Near-Pass Expansion",
        architecture=LOW_TIMEFRAME_EXPANSION_VERSION,
        strategy_family_label="Low-timeframe near-pass expansion",
        candidates=blueprint["candidates"],
        supported_timeframes=SUPPORTED_LOW_TIMEFRAMES,
        timeframes=blueprint["timeframes"],
        asset_limit=asset_limit,
        campaign_label=(
            f"{LOW_TIMEFRAME_EXPANSION_VERSION}_{len(blueprint['parents'])}p_"
            f"{len(blueprint['candidates'])}c_"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"
        ),
        assets_override=blueprint["assets"],
    )
    result["parent_count"] = len(blueprint["parents"])
    result["parents"] = [
        {
            "campaign_id": row.get("campaign_id"),
            "job_id": row.get("job_id"),
            "candidate_id": row.get("candidate_id"),
            "asset": row.get("asset"),
            "timeframe": row.get("timeframe"),
            "status": row.get("status"),
            "strategy_family": row.get("strategy_family"),
            "metrics": (row.get("result") or {}).get("metrics") or {},
        }
        for row in blueprint["parents"]
    ]
    result["expansion_version"] = LOW_TIMEFRAME_EXPANSION_VERSION
    result["candidate_payloads"] = [asdict(candidate) for candidate in blueprint["candidates"][:5]]
    return result
