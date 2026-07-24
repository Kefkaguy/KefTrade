"""Opening-Range Breakout v1 campaign wiring (Phase 12, Step 2B).

Generates a small, deliberately bounded ORB parameter grid as
`DiscoveryCandidate` objects -- reusing the existing candidate/job/lifecycle
machinery unchanged (`app.services.strategy_discovery.DiscoveryCandidate`,
`candidate_from_payload`, `queue_campaign_jobs`) -- and a dedicated
campaign-creation entry point mirroring
`app.services.research_campaigns.create_high_frequency_campaign`'s pattern.

This module calls back into `research_campaigns.py` for the shared campaign
primitives (table setup, universe lookup, job queueing, campaign-key
hashing) rather than duplicating them, matching the labs convention already
used by `labs/intraday/features.py`.
"""

from __future__ import annotations

from hashlib import sha256
from itertools import product
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.labs.intraday.strategy import DEFAULT_ORB_PARAMETERS, OPENING_RANGE_BREAKOUT_ARCHITECTURE
from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key

SUPPORTED_ORB_TIMEFRAMES = ("15m", "30m")

ORB_BLOCKS: dict[str, str] = {
    "trend": "orb_session_context",
    "momentum": "orb_relative_volume_confirmation",
    "volatility": "orb_opening_range_span",
    "volume": "orb_relative_volume",
    "entry": "opening_range_breakout",
    "exit": "orb_session_close_forced",
}

# Deliberately small, bounded grid for pipeline validation -- not a
# profitability search. Two breakout-buffer levels x two directions = 4
# candidates (long-only and short-only kept separate rather than "both" so
# the pilot can report setup/trade counts per direction cleanly).
ORB_BREAKOUT_BUFFER_LEVELS = ("0.05", "0.15")
ORB_DIRECTIONS = ("long", "short")


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


def create_opening_range_breakout_campaign(
    conn: psycopg.Connection,
    *,
    name: str | None = None,
    asset_limit: int = 10,
    timeframes: list[str] | None = None,
    max_candidates: int = 8,
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
    selected_timeframes = [tf for tf in (timeframes or SUPPORTED_ORB_TIMEFRAMES) if tf in SUPPORTED_ORB_TIMEFRAMES]
    if not selected_timeframes:
        raise ValueError(f"Opening-Range Breakout campaigns only support {SUPPORTED_ORB_TIMEFRAMES}")

    universe = get_universe(conn, "research_core_ten")
    assets = [str(asset).upper() for asset in (universe.get("assets") or [])][:asset_limit]
    candidates = generate_orb_candidates(max_candidates=max_candidates)
    if not candidates:
        raise ValueError("no ORB candidates generated")

    campaign_key = research_campaign_key(
        "research_core_ten",
        assets,
        selected_timeframes,
        len(candidates),
        search_mode="opening_range_breakout",
        variant=OPENING_RANGE_BREAKOUT_ARCHITECTURE,
    )
    controls = Jsonb(
        {
            "timeframes": selected_timeframes,
            "campaign_version": CAMPAIGN_VERSION,
            "candidate_generation": OPENING_RANGE_BREAKOUT_ARCHITECTURE,
            "strategy_family": "Opening-Range Breakout",
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
            name or "Opening-Range Breakout v1 pilot",
            len(candidates),
            controls,
            Jsonb(DEFAULT_SCHEDULING_CONFIG),
            SAFETY_STATEMENT,
            OPENING_RANGE_BREAKOUT_ARCHITECTURE,
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


def is_opening_range_breakout_candidate(candidate_payload: dict[str, Any]) -> bool:
    """True when a raw (JSONB) candidate payload is an ORB v1 candidate.

    Used at the two points that need to route intraday jobs differently
    (`run_campaign_job`'s dataset selection, `data_readiness_for_job`'s
    feature-coverage check) without branching on strategy identity anywhere
    inside the simulator itself.
    """
    parameters = candidate_payload.get("parameters") or {}
    return parameters.get("strategy_architecture") == OPENING_RANGE_BREAKOUT_ARCHITECTURE
