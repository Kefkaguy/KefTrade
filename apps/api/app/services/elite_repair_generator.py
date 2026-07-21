from __future__ import annotations

from hashlib import sha256
from typing import Any

import psycopg

from app.services.strategy_discovery import DiscoveryCandidate, canonical_candidate_key, candidate_payload, jsonable


ELITE_REPAIR_GENERATOR_VERSION = "elite_shadow_repair_v1"


def elite_repair_proposals(conn: psycopg.Connection, limit: int = 50) -> dict[str, Any]:
    deployments = conn.execute(
        """
        SELECT
            x.id AS external_deployment_id,
            x.state,
            d.id AS internal_deployment_id,
            d.campaign_id,
            d.candidate_id,
            d.strategy_name,
            d.strategy_version,
            d.symbol,
            d.timeframe,
            d.parameters,
            e.id AS elite_candidate_id,
            e.research_score,
            e.forward_validation_state,
            s.id AS shadow_execution_id,
            s.trace_id AS shadow_trace_id,
            s.rejection_reasons,
            s.decision,
            s.created_at AS shadow_created_at
        FROM external_paper_deployments x
        JOIN strategy_deployments d ON d.id = x.internal_deployment_id
        JOIN elite_research_candidates e
          ON e.campaign_id = d.campaign_id
         AND e.candidate_id = d.candidate_id
         AND e.simulation_only = TRUE
        LEFT JOIN LATERAL (
            SELECT *
            FROM shadow_executions s
            WHERE s.external_deployment_id = x.id
            ORDER BY s.created_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE x.state = 'enabled_observe_only'
        ORDER BY e.research_score DESC, x.id
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    parent_rows = [dict(row) for row in deployments]
    proposals = []
    for row in parent_rows:
        proposals.extend(repair_proposals_for_parent(row))
    return {
        "version": ELITE_REPAIR_GENERATOR_VERSION,
        "mode": "research_only_shadow_repair",
        "parents": [parent_summary(row) for row in parent_rows],
        "proposal_count": len(proposals),
        "proposals": proposals,
        "constraints": {
            "auto_promote": False,
            "auto_launch_campaign": False,
            "broker_mutation": False,
            "validation_thresholds_changed": False,
            "requires_normal_research_validation": True,
        },
    }


def repair_proposals_for_parent(row: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = list(row.get("rejection_reasons") or [])
    decision = dict(row.get("decision") or {})
    signal = dict(decision.get("signal") or {})
    explanation = [str(item) for item in signal.get("explanation") or []]
    if "NO_ACTIONABLE_SETUP" not in reasons and "Trend block failed." not in explanation:
        return []
    parent = candidate_from_external_deployment(row)
    mutations = trend_repair_mutations(row)
    proposals = []
    for mutation in mutations:
        child = child_candidate(parent, row, mutation)
        proposals.append(
            {
                "parent": parent_summary(row),
                "candidate": candidate_payload(child),
                "mutation": mutation,
                "rationale": {
                    "observed_shadow_reasons": reasons,
                    "observed_signal_explanation": explanation,
                    "why": mutation["reason"],
                    "expected_effect": mutation["expected_effect"],
                },
                "next_step": "Queue this child in a normal research campaign; promote only if the existing validation gates pass.",
            }
        )
    return proposals


def trend_repair_mutations(row: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_ref = f"shadow_execution:{row.get('shadow_execution_id') or 'none'}"
    return [
        {
            "id": "close_gt_slow_ema",
            "changes": {
                "trend_repair_mode": "price_above_slow",
                "phase10_shadow_repair_mutation": "close_gt_slow_ema",
                "shadow_repair_reason": "The parent failed because fast EMA was still below slow EMA even when price may recover first.",
                "shadow_repair_evidence_ref": evidence_ref,
                "elite_repair_version": ELITE_REPAIR_GENERATOR_VERSION,
            },
            "reason": "Tests whether the elite is too late because it waits for the fast EMA to cross after price has already reclaimed the slow EMA.",
            "expected_effect": "More completed-bar opportunities while preserving the slow-trend price filter.",
            "confidence_score": 0.62,
        },
        {
            "id": "near_cross_with_momentum",
            "changes": {
                "trend_repair_mode": "near_cross_with_momentum",
                "trend_fast_slow_ratio_min": 0.985,
                "returns_5_min": max(float(dict(row.get("parameters") or {}).get("returns_5_min") or 0), 0.0),
                "phase10_shadow_repair_mutation": "near_cross_with_momentum",
                "shadow_repair_reason": "The parent failed during a near-cross setup; this tests momentum-confirmed early continuation.",
                "shadow_repair_evidence_ref": evidence_ref,
                "elite_repair_version": ELITE_REPAIR_GENERATOR_VERSION,
            },
            "reason": "Tests a controlled early-entry variant when fast EMA is close to slow EMA and short-term returns are not negative.",
            "expected_effect": "Catches earlier continuation candidates without removing momentum confirmation.",
            "confidence_score": 0.58,
        },
        {
            "id": "fast_slope_or_cross",
            "changes": {
                "trend_repair_mode": "fast_slope_or_price_above_slow",
                "phase_9_9_ema_slope_min": 0,
                "phase10_shadow_repair_mutation": "fast_slope_or_cross",
                "shadow_repair_reason": "The parent failed the absolute EMA cross; this tests improving fast EMA slope as a repair signal.",
                "shadow_repair_evidence_ref": evidence_ref,
                "elite_repair_version": ELITE_REPAIR_GENERATOR_VERSION,
            },
            "reason": "Tests whether improving fast EMA slope is enough evidence before the full EMA cross completes.",
            "expected_effect": "Creates more candidates during recoveries while still requiring price above the slow EMA.",
            "confidence_score": 0.54,
        },
    ]


def candidate_from_external_deployment(row: dict[str, Any]) -> DiscoveryCandidate:
    params = dict(row.get("parameters") or {})
    blocks = {
        "trend": str(params.get("trend_block") or "ema_20_50"),
        "momentum": str(params.get("momentum") or "unknown_momentum"),
        "volatility": str(params.get("volatility") or "unknown_volatility"),
        "volume": str(params.get("volume") or "unknown_volume"),
        "entry": str(params.get("entry") or "unknown_entry"),
        "exit": str(params.get("exit") or "unknown_exit"),
    }
    key = canonical_candidate_key(blocks, params, str(row["candidate_id"]))
    return DiscoveryCandidate(
        candidate_id=str(row["candidate_id"]),
        family_id="elite_shadow_parent",
        parent_candidate_id=None,
        generation=1,
        blocks=blocks,
        parameters=params,
        complexity=6,
        canonical_key=key,
    )


def child_candidate(parent: DiscoveryCandidate, row: dict[str, Any], mutation: dict[str, Any]) -> DiscoveryCandidate:
    params = {**parent.parameters, **mutation["changes"]}
    params["parent_elite_candidate_id"] = row.get("elite_candidate_id")
    params["parent_external_deployment_id"] = row.get("external_deployment_id")
    params["generation_channel"] = "phase10_shadow_elite_repair"
    key = canonical_candidate_key(parent.blocks, params, parent.candidate_id)
    return DiscoveryCandidate(
        candidate_id=f"sr_{sha256(key.encode()).hexdigest()[:14]}",
        family_id="elite_shadow_repair",
        parent_candidate_id=parent.candidate_id,
        generation=parent.generation + 1,
        blocks=dict(parent.blocks),
        parameters=params,
        complexity=parent.complexity + 1,
        canonical_key=key,
    )


def parent_summary(row: dict[str, Any]) -> dict[str, Any]:
    return jsonable(
        {
            "external_deployment_id": row.get("external_deployment_id"),
            "internal_deployment_id": row.get("internal_deployment_id"),
            "campaign_id": row.get("campaign_id"),
            "elite_candidate_id": row.get("elite_candidate_id"),
            "candidate_id": row.get("candidate_id"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "research_score": row.get("research_score"),
            "forward_validation_state": row.get("forward_validation_state"),
            "latest_shadow_execution_id": row.get("shadow_execution_id"),
            "latest_shadow_trace_id": str(row["shadow_trace_id"]) if row.get("shadow_trace_id") else None,
            "latest_shadow_at": row.get("shadow_created_at"),
        }
    )
