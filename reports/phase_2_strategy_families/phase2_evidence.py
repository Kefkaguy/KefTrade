from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from statistics import median
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.services.research_architecture import append_hypothesis_version, create_intelligent_research_campaign, verify_dataset_snapshot
from app.services.research_campaigns import candidate_from_payload, run_campaign_job, run_research_campaign_batch
from app.services.strategy_discovery import candidate_execution_key
from app.services.strategy_families import PHASE_2_FAMILY_NAMES, PHASE_2_FAMILY_VERSION
from app.settings import settings


DATASET_ID = 1
BASELINE_CAMPAIGN_ID = 52
PHASE_2_CLUSTER_KEY = "cluster_899a8ec60d3869eb0930"
UNIVERSE_KEY = "research_core_ten"
CANDIDATES_PER_FAMILY = 10


def execute_phase2(conn: psycopg.Connection) -> dict[str, Any]:
    active = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, name, status, immutable_config
            FROM research_campaigns
            WHERE status IN ('queued', 'running')
            ORDER BY id
            """
        ).fetchall()
    ]
    foreign_active = [
        row
        for row in active
        if (row.get("immutable_config") or {}).get("family_version") != PHASE_2_FAMILY_VERSION
    ]
    if foreign_active:
        raise RuntimeError(
            "Phase 2 will not start while another campaign is active: "
            + ", ".join(f"{row['id']} ({row['status']})" for row in foreign_active)
        )

    hypothesis_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT *
        FROM research_hypothesis_versions
        WHERE scope_type = 'cluster'
          AND scope_ref = %s
          AND test_summary->>'source_dataset_id' = %s
          AND test_summary->>'family_version' = %s
        ORDER BY strategy_family, version DESC, id DESC
        """,
        (PHASE_2_CLUSTER_KEY, str(DATASET_ID), PHASE_2_FAMILY_VERSION),
    ).fetchall():
        hypothesis_history[str(row["strategy_family"])].append(dict(row))
    hypotheses = {
        family: rows[0]
        for family, rows in hypothesis_history.items()
        if rows
    }
    missing = [family for family in PHASE_2_FAMILY_NAMES if family not in hypotheses]
    if missing:
        raise RuntimeError(f"Missing Phase 2 hypotheses: {', '.join(missing)}")

    results = []
    for family in PHASE_2_FAMILY_NAMES:
        existing = conn.execute(
            """
            SELECT c.*
            FROM research_campaigns c
            JOIN research_hypothesis_versions h ON h.id = c.hypothesis_version_id
            WHERE c.dataset_id = %s
              AND h.strategy_family = %s
              AND c.immutable_config->>'family_version' = %s
              AND c.requested_candidates = %s
            ORDER BY c.id DESC
            LIMIT 1
            """,
            (DATASET_ID, family, PHASE_2_FAMILY_VERSION, CANDIDATES_PER_FAMILY),
        ).fetchone()
        if existing:
            campaign_id = int(existing["id"])
        else:
            hypothesis = hypotheses[family]
            if hypothesis["status"] in {"rejected", "retired"}:
                pilot_campaign_id = (hypothesis.get("test_summary") or {}).get("campaign_id")
                hypothesis = append_hypothesis_version(
                    conn,
                    hypothesis,
                    status="proposed",
                    test_summary={
                        **dict(hypothesis.get("test_summary") or {}),
                        "campaign_id": None,
                        "protocol_revision": "Minimum allocation-valid comparison: 10 candidates produce exact 7/2/1 channels.",
                        "prior_pilot_campaign_id": pilot_campaign_id,
                        "prior_pilot_missing_exploration": True,
                        "confirmation_status": "unconfirmed",
                    },
                    supporting_evidence=list(hypothesis.get("supporting_evidence") or []),
                    contradictory_evidence=list(hypothesis.get("contradictory_evidence") or [])
                    + ([f"research_campaign:{pilot_campaign_id}"] if pilot_campaign_id else []),
                )
                hypotheses[family] = hypothesis
            created = create_intelligent_research_campaign(
                conn,
                universe_key=UNIVERSE_KEY,
                name=f"Phase 2 frozen regression — {family}",
                max_candidates=CANDIDATES_PER_FAMILY,
                asset_limit=10,
                timeframes=["1h"],
                dataset_mode="rolling",
                dataset_id=DATASET_ID,
                hypothesis_id=int(hypothesis["id"]),
            )
            campaign_id = int(created["campaign"]["id"])
        batches = []
        while True:
            campaign = conn.execute("SELECT status FROM research_campaigns WHERE id = %s", (campaign_id,)).fetchone()
            if campaign["status"] == "completed":
                break
            result = run_research_campaign_batch(
                conn,
                campaign_id=campaign_id,
                batch_size=1,
                worker_id=f"phase2_single_worker_{campaign_id}",
            )
            batches.append({key: result.get(key) for key in ("processed", "completed", "failed", "remaining")})
            if int(result.get("processed") or 0) == 0 and int(result.get("remaining") or 0) > 0:
                raise RuntimeError(f"Phase 2 campaign {campaign_id} stopped making progress")
        results.append({"family": family, "campaign_id": campaign_id, "batches": batches})
    return {"dataset_id": DATASET_ID, "campaigns": results, "simulation_only": True}


def analyze_phase2(conn: psycopg.Connection, *, verify_baseline: bool = False) -> dict[str, Any]:
    manifest = dict(conn.execute("SELECT * FROM research_dataset_manifests WHERE id = %s", (DATASET_ID,)).fetchone())
    policy = dict(
        conn.execute(
            "SELECT * FROM research_validation_policy_versions WHERE policy_key = 'strong_research_gates' ORDER BY version DESC LIMIT 1"
        ).fetchone()
    )
    phase2_campaigns = [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.*, h.strategy_family, h.hypothesis_key, h.status AS hypothesis_status,
                   h.test_summary AS hypothesis_test_summary, h.supporting_evidence, h.contradictory_evidence
            FROM research_campaigns c
            JOIN research_hypothesis_versions h ON h.id = c.hypothesis_version_id
            WHERE c.dataset_id = %s
              AND c.immutable_config->>'family_version' = %s
              AND c.requested_candidates = %s
            ORDER BY c.id
            """,
            (DATASET_ID, PHASE_2_FAMILY_VERSION, CANDIDATES_PER_FAMILY),
        ).fetchall()
    ]
    latest_by_family = {}
    for campaign in phase2_campaigns:
        latest_by_family[str(campaign["strategy_family"])] = campaign

    family_rows = []
    for family in PHASE_2_FAMILY_NAMES:
        campaign = latest_by_family.get(family)
        if campaign is None:
            family_rows.append({"family": family, "status": "not_run"})
            continue
        jobs = [dict(row) for row in conn.execute("SELECT * FROM research_campaign_jobs WHERE campaign_id = %s ORDER BY candidate_id, symbol, timeframe", (campaign["id"],)).fetchall()]
        stages = [dict(row) for row in conn.execute("SELECT * FROM research_candidate_stage_evidence WHERE campaign_id = %s ORDER BY candidate_id, candidate_level", (campaign["id"],)).fetchall()]
        family_rows.append(campaign_metrics(campaign, jobs, stages))

    baseline_campaign = dict(conn.execute("SELECT * FROM research_campaigns WHERE id = %s", (BASELINE_CAMPAIGN_ID,)).fetchone())
    baseline_channel_counts = {"exploitation": 7, "nearby": 2, "exploration": 1}
    baseline_candidates = []
    for channel, count in baseline_channel_counts.items():
        baseline_candidates.extend(
            str(row["candidate_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT candidate_id
                FROM research_campaign_jobs
                WHERE campaign_id = %s AND generation_channel = %s
                ORDER BY candidate_id
                LIMIT %s
                """,
                (BASELINE_CAMPAIGN_ID, channel, count),
            ).fetchall()
        )
    if len(baseline_candidates) != CANDIDATES_PER_FAMILY:
        raise RuntimeError(
            "Frozen baseline does not contain the required deterministic 7/2/1 candidate allocation "
            f"(found {len(baseline_candidates)} candidates)."
        )
    baseline_jobs = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM research_campaign_jobs
            WHERE campaign_id = %s AND candidate_id = ANY(%s)
            ORDER BY candidate_id, symbol, timeframe
            """,
            (BASELINE_CAMPAIGN_ID, baseline_candidates),
        ).fetchall()
    ]
    baseline = campaign_metrics(
        {**baseline_campaign, "strategy_family": "Trend Following", "hypothesis_status": "historical_baseline"},
        baseline_jobs,
        [],
    )
    baseline["subset_rule"] = (
        "first candidate IDs in deterministic lexical order within frozen channels: "
        "7 exploitation, 2 nearby, 1 exploration"
    )
    baseline["source_campaign_total_jobs"] = int(
        conn.execute("SELECT COUNT(*) AS count FROM research_campaign_jobs WHERE campaign_id = %s", (BASELINE_CAMPAIGN_ID,)).fetchone()["count"]
    )
    reproducibility = verify_baseline_jobs(conn, baseline_jobs) if verify_baseline else {"performed": False}

    phase1 = phase1_baseline(conn)
    return jsonable(
        {
            "phase": 2,
            "implementation_version": PHASE_2_FAMILY_VERSION,
            "dataset": {
                "id": DATASET_ID,
                "dataset_key": manifest["dataset_key"],
                "content_hash": manifest["content_hash"],
                "mode": manifest["mode"],
                "assets": manifest["assets"],
                "timeframes": manifest["timeframes"],
                "integrity": verify_dataset_snapshot(conn, DATASET_ID),
            },
            "validation_policy": {
                "id": policy["id"],
                "key": policy["policy_key"],
                "version": policy["version"],
                "thresholds": policy["thresholds"],
                "calculation_version": policy["calculation_version"],
            },
            "comparable_budget": {
                "candidates_per_family": CANDIDATES_PER_FAMILY,
                "assets": ["QQQ", "SPY"],
                "timeframe": "1h",
                "jobs_per_family": CANDIDATES_PER_FAMILY * 2,
                "generation_seed": 0,
                "worker_count": 1,
            },
            "families": family_rows,
            "trend_following_frozen_baseline": baseline,
            "trend_following_reproducibility": reproducibility,
            "phase1_transfer_baseline": phase1,
        }
    )


def campaign_metrics(campaign: dict[str, Any], jobs: list[dict[str, Any]], stages: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        candidate_groups[str(job["candidate_id"])].append(job)
    execution_keys = {
        candidate_execution_key(candidate_from_payload(dict(rows[0]["candidate"])))
        for rows in candidate_groups.values()
    }
    promoted_candidates = {
        candidate_id
        for candidate_id, rows in candidate_groups.items()
        if any(row["status"] == "promoted" for row in rows)
    }
    transferred = {
        candidate_id
        for candidate_id, rows in candidate_groups.items()
        if len({str(row["symbol"]) for row in rows if row["status"] == "promoted"}) >= 2
    }
    metrics = [dict((row.get("result") or {}).get("metrics") or {}) for row in jobs]
    profit_factors = [numeric(row.get("profit_factor")) for row in metrics if row.get("profit_factor") is not None]
    expectancies = [numeric(row.get("expectancy_per_trade")) for row in metrics if row.get("expectancy_per_trade") is not None]
    stage_counts = Counter(str(row["candidate_level"]) for row in stages)
    status_counts = Counter(str(row["status"]) for row in jobs)
    failed_jobs = sum(1 for row in jobs if row["status"] in {"failed", "blocked_data", "retrying"})
    runtime_ms = sum(int(row.get("execution_runtime_ms") or 0) for row in jobs)
    generation_channels = Counter(str(row.get("generation_channel") or "legacy") for row in jobs)
    return {
        "campaign_id": int(campaign["id"]),
        "family": str(campaign.get("strategy_family") or "unknown"),
        "campaign_status": campaign.get("status"),
        "hypothesis_version_id": campaign.get("hypothesis_version_id"),
        "hypothesis_status_at_campaign": campaign.get("hypothesis_status"),
        "hypothesis_result": ((campaign.get("analytics") or {}).get("research_architecture") or {}).get("hypothesis_result"),
        "post_hoc": bool((campaign.get("hypothesis_test_summary") or {}).get("post_hoc")),
        "confirmation_status": (campaign.get("hypothesis_test_summary") or {}).get("confirmation_status"),
        "jobs": len(jobs),
        "unique_candidates": len(candidate_groups),
        "unique_execution_keys": len(execution_keys),
        "duplicate_execution_keys": max(0, len(candidate_groups) - len(execution_keys)),
        "duplicate_rate": round(max(0, len(candidate_groups) - len(execution_keys)) / max(1, len(candidate_groups)), 6),
        "status_counts": dict(status_counts),
        "generation_channel_jobs": dict(generation_channels),
        "promoted_market_jobs": int(status_counts.get("promoted", 0)),
        "promoted_candidates": len(promoted_candidates),
        "asset_specialists": int(stage_counts.get("asset_specialist", 0)),
        "cluster_elites": int(stage_counts.get("cluster_elite", 0)),
        "universal_elites": int(stage_counts.get("universal_elite", 0)),
        "transferable_candidates": len(transferred),
        "transfer_success_rate": round(len(transferred) / max(1, len(candidate_groups)), 6),
        "median_profit_factor": round(median(profit_factors), 6) if profit_factors else None,
        "median_expectancy": round(median(expectancies), 6) if expectancies else None,
        "walk_forward_survival_rate": round(sum(1 for row in metrics if (row.get("walk_forward") or {}).get("enabled")) / max(1, len(metrics)), 6),
        "regime_stability_survival_rate": round(sum(1 for row in jobs if ((row.get("result") or {}).get("paper_readiness") or {}).get("paper_ready")) / max(1, len(jobs)), 6),
        "jobs_per_promoted_candidate": round(len(jobs) / len(promoted_candidates), 6) if promoted_candidates else None,
        "jobs_per_confirmed_hypothesis": None,
        "confirmed_hypotheses": 0,
        "hypothesis_confirmation_rate": 0.0,
        "runtime_ms": runtime_ms,
        "operational_failures": failed_jobs,
        "operational_failure_rate": round(failed_jobs / max(1, len(jobs)), 6),
        "candidate_levels": dict(stage_counts),
    }


def verify_baseline_jobs(conn: psycopg.Connection, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    comparisons = []
    fields = ("profit_factor", "profit_factor_is_infinite", "expectancy_per_trade", "max_drawdown", "number_of_trades", "walk_forward")
    for job in jobs:
        rerun = run_campaign_job(conn, {**job, "_dataset_cache": cache})
        stored = dict((job.get("result") or {}).get("metrics") or {})
        actual = dict(rerun.get("metrics") or {})
        differences = {
            field: {"stored": stored.get(field), "rerun": actual.get(field)}
            for field in fields
            if jsonable(stored.get(field)) != jsonable(actual.get(field))
        }
        comparisons.append(
            {
                "campaign_id": int(job["campaign_id"]),
                "job_id": int(job["id"]),
                "candidate_id": job["candidate_id"],
                "asset": job["symbol"],
                "timeframe": job["timeframe"],
                "dataset_id": job.get("dataset_id"),
                "matched": not differences,
                "differences": differences,
            }
        )
    return {
        "performed": True,
        "jobs_rerun": len(comparisons),
        "matched_jobs": sum(1 for row in comparisons if row["matched"]),
        "all_matched": all(row["matched"] for row in comparisons),
        "comparisons": comparisons,
    }


def phase1_baseline(conn: psycopg.Connection) -> dict[str, Any]:
    campaign_ids = [51, 53, 54]
    stages = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM research_candidate_stage_evidence
            WHERE campaign_id = ANY(%s) AND candidate_level = 'asset_specialist'
            ORDER BY campaign_id, candidate_id, scope_ref
            """,
            (campaign_ids,),
        ).fetchall()
    ]
    candidate_ids = sorted({str(row["candidate_id"]) for row in stages})
    return {
        "campaign_ids": campaign_ids,
        "asset_specialist_stage_records": len(stages),
        "unique_specialist_candidate_ids": len(candidate_ids),
        "transfer_attempts": 76,
        "unique_transfer_attempts": 52,
        "transfer_passes": 0,
        "unique_transfer_passes": 0,
        "transfer_success_rate": 0.0,
        "source": "reports/phase_1_transfer_failure/evidence.json",
    }


def numeric(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute or analyze the bounded Phase 2 frozen-dataset regression.")
    parser.add_argument("--execute", action="store_true", help="Create and run the eight bounded family campaigns sequentially with one worker.")
    parser.add_argument("--verify-baseline", action="store_true", help="Rerun the comparable Trend Following baseline jobs from frozen evidence.")
    args = parser.parse_args()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        payload = execute_phase2(conn) if args.execute else analyze_phase2(conn, verify_baseline=args.verify_baseline)
        print(json.dumps(jsonable(payload), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
