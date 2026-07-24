"""Intraday Lab campaign wiring (Phase 12).

Generates small, deliberately bounded parameter grids as `DiscoveryCandidate`
objects -- reusing the existing candidate/job/lifecycle machinery unchanged
(`app.services.strategy_discovery.DiscoveryCandidate`, `candidate_from_payload`,
`queue_campaign_jobs`) -- and a shared campaign-creation entry point mirroring
`app.services.research_campaigns.create_high_frequency_campaign`'s pattern.

This module calls back into `research_campaigns.py` for the shared campaign
primitives (table setup, universe lookup, job queueing, campaign-key
hashing) rather than duplicating them, matching the labs convention already
used by `labs/intraday/features.py`. Every family (Opening-Range Breakout,
VWAP Reversion, and any future one) shares one campaign-creation helper
(`_create_intraday_campaign`) and one dispatch check
(`is_intraday_lab_candidate`, keyed off `INTRADAY_STRATEGY_FACTORIES`) --
adding a new family never means adding another branch here.
"""

from __future__ import annotations

from hashlib import sha256
from itertools import product
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.labs.intraday.strategy import (
    DEFAULT_ORB_PARAMETERS,
    DEFAULT_VWAP_REVERSION_PARAMETERS,
    INTRADAY_STRATEGY_FACTORIES,
    OPENING_RANGE_BREAKOUT_ARCHITECTURE,
    VWAP_REVERSION_ARCHITECTURE,
)
from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

SUPPORTED_ORB_TIMEFRAMES = ("15m", "30m")
SUPPORTED_VWAP_REVERSION_TIMEFRAMES = ("15m", "30m")

ORB_BLOCKS: dict[str, str] = {
    "trend": "orb_session_context",
    "momentum": "orb_relative_volume_confirmation",
    "volatility": "orb_opening_range_span",
    "volume": "orb_relative_volume",
    "entry": "opening_range_breakout",
    "exit": "orb_session_close_forced",
}

VWAP_REVERSION_BLOCKS: dict[str, str] = {
    "trend": "vwap_session_context",
    "momentum": "vwap_relative_volume_confirmation",
    "volatility": "vwap_deviation_distance",
    "volume": "vwap_relative_volume",
    "entry": "vwap_reversion",
    "exit": "vwap_session_close_forced",
}

# Deliberately small, bounded grid for pipeline validation -- not a
# profitability search. Two breakout-buffer levels x two directions = 4
# candidates (long-only and short-only kept separate rather than "both" so
# the pilot can report setup/trade counts per direction cleanly).
ORB_BREAKOUT_BUFFER_LEVELS = ("0.05", "0.15")
ORB_DIRECTIONS = ("long", "short")

# Same shape for VWAP Reversion: two deviation thresholds (~p75/~p90 of the
# real |distance_from_session_vwap| distribution, see the strategy module's
# comment) x two directions = 4 candidates.
VWAP_REVERSION_DEVIATION_THRESHOLDS = ("0.006", "0.010")
VWAP_REVERSION_DIRECTIONS = ("long", "short")


def generate_orb_candidates(*, max_candidates: int = 8) -> list[DiscoveryCandidate]:
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for buffer_level, direction in product(ORB_BREAKOUT_BUFFER_LEVELS, ORB_DIRECTIONS):
        params = {
            **DEFAULT_ORB_PARAMETERS,
            "breakout_buffer_atr": buffer_level,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(ORB_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"orb_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_opening_range_breakout_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(ORB_BLOCKS),
                parameters=params,
                complexity=4,
                canonical_key=canonical_key,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def generate_vwap_reversion_candidates(*, max_candidates: int = 8) -> list[DiscoveryCandidate]:
    candidates: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for threshold, direction in product(VWAP_REVERSION_DEVIATION_THRESHOLDS, VWAP_REVERSION_DIRECTIONS):
        params = {
            **DEFAULT_VWAP_REVERSION_PARAMETERS,
            "entry_deviation_threshold": threshold,
            "direction": direction,
        }
        canonical_key = canonical_candidate_key(VWAP_REVERSION_BLOCKS, params)
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        candidate_id = f"vwap_{sha256(canonical_key.encode()).hexdigest()[:14]}"
        candidates.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                family_id="phase12_vwap_reversion_v1",
                parent_candidate_id=None,
                generation=1,
                blocks=dict(VWAP_REVERSION_BLOCKS),
                parameters=params,
                complexity=4,
                canonical_key=canonical_key,
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def _create_intraday_campaign(
    conn: psycopg.Connection,
    *,
    name: str,
    architecture: str,
    strategy_family_label: str,
    candidates: list[DiscoveryCandidate],
    supported_timeframes: tuple[str, ...],
    timeframes: list[str] | None,
    asset_limit: int,
) -> dict[str, Any]:
    from app.services.research_campaigns import (
        CAMPAIGN_VERSION,
        DEFAULT_SCHEDULING_CONFIG,
        SAFETY_STATEMENT,
        ensure_campaign_tables,
        get_universe,
        jsonable,
        queue_campaign_jobs,
        research_campaign_key,
        seed_default_universes,
        update_campaign_counts,
    )

    ensure_campaign_tables(conn)
    seed_default_universes(conn)
    selected_timeframes = [tf for tf in (timeframes or supported_timeframes) if tf in supported_timeframes]
    if not selected_timeframes:
        raise ValueError(f"{strategy_family_label} campaigns only support {supported_timeframes}")

    universe = get_universe(conn, "research_core_ten")
    assets = [str(asset).upper() for asset in (universe.get("assets") or [])][:asset_limit]
    if not candidates:
        raise ValueError(f"no {strategy_family_label} candidates generated")

    campaign_key = research_campaign_key(
        "research_core_ten",
        assets,
        selected_timeframes,
        len(candidates),
        search_mode=architecture,
        variant=architecture,
    )
    controls = Jsonb(
        {
            "timeframes": selected_timeframes,
            "campaign_version": CAMPAIGN_VERSION,
            "candidate_generation": architecture,
            "strategy_family": strategy_family_label,
            "pilot": True,
            "validation_policy": "All existing trade-count, quality, walk-forward, and cross-market gates remain unchanged.",
        }
    )
    row = conn.execute(
        """
        INSERT INTO research_campaigns(campaign_key, name, universe_key, status, requested_candidates, controls, scheduling_config, safety_statement, generator_version, simulation_only)
        VALUES (%s, %s, 'research_core_ten', 'queued', %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(campaign_key) DO UPDATE SET updated_at = NOW()
        RETURNING *
        """,
        (
            campaign_key,
            name,
            len(candidates),
            controls,
            Jsonb(DEFAULT_SCHEDULING_CONFIG),
            SAFETY_STATEMENT,
            architecture,
        ),
    ).fetchone()
    campaign_id = int(row["id"])
    created = queue_campaign_jobs(conn, campaign_id, candidates, assets, selected_timeframes)
    update_campaign_counts(conn, campaign_id)
    conn.commit()
    return {
        "campaign": jsonable(dict(row)),
        "campaign_id": campaign_id,
        "candidates_queued": len(candidates),
        "jobs_created": created,
        "assets": assets,
        "timeframes": selected_timeframes,
    }


def create_opening_range_breakout_campaign(
    conn: psycopg.Connection,
    *,
    name: str | None = None,
    asset_limit: int = 10,
    timeframes: list[str] | None = None,
    max_candidates: int = 8,
) -> dict[str, Any]:
    return _create_intraday_campaign(
        conn,
        name=name or "Opening-Range Breakout v1 pilot",
        architecture=OPENING_RANGE_BREAKOUT_ARCHITECTURE,
        strategy_family_label="Opening-Range Breakout",
        candidates=generate_orb_candidates(max_candidates=max_candidates),
        supported_timeframes=SUPPORTED_ORB_TIMEFRAMES,
        timeframes=timeframes,
        asset_limit=asset_limit,
    )


def create_vwap_reversion_campaign(
    conn: psycopg.Connection,
    *,
    name: str | None = None,
    asset_limit: int = 10,
    timeframes: list[str] | None = None,
    max_candidates: int = 8,
) -> dict[str, Any]:
    return _create_intraday_campaign(
        conn,
        name=name or "VWAP Reversion v1 pilot",
        architecture=VWAP_REVERSION_ARCHITECTURE,
        strategy_family_label="VWAP Reversion",
        candidates=generate_vwap_reversion_candidates(max_candidates=max_candidates),
        supported_timeframes=SUPPORTED_VWAP_REVERSION_TIMEFRAMES,
        timeframes=timeframes,
        asset_limit=asset_limit,
    )


def is_intraday_lab_candidate(candidate_payload: dict[str, Any]) -> bool:
    """True when a raw (JSONB) candidate payload belongs to any Intraday Lab family.

    Used at the two points that need to route intraday jobs differently
    (`run_campaign_job`'s dataset selection, `data_readiness_for_job`'s
    feature-coverage check) without branching on strategy identity anywhere
    inside the simulator itself. Keyed off `INTRADAY_STRATEGY_FACTORIES`, so
    a new family registering there is automatically covered here too.
    """
    parameters = candidate_payload.get("parameters") or {}
    return parameters.get("strategy_architecture") in INTRADAY_STRATEGY_FACTORIES
