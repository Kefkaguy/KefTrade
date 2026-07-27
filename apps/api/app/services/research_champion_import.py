from __future__ import annotations

from hashlib import sha256
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_campaigns import jsonable


RESEARCH_CHAMPION_RULE_VERSION = "research_champion_v1"


def _metric(result: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float((result.get("metrics") or {}).get(key))
    except (TypeError, ValueError):
        return default


def _candidate_direction(candidate: dict[str, Any]) -> str:
    parameters = dict(candidate.get("parameters") or {})
    return str(parameters.get("direction") or candidate.get("direction") or "long")


def _regimes_passed(result: dict[str, Any]) -> int:
    regimes: set[str] = set()
    for bucket in ("by_market_regime", "by_volatility_regime"):
        for row in ((result.get("regime_analysis") or {}).get(bucket) or []):
            metrics = row.get("metrics") or {}
            if _metric({"metrics": metrics}, "number_of_trades") > 0 and _metric({"metrics": metrics}, "expectancy_per_trade") > 0:
                regimes.add(str(row.get("regime") or "unknown"))
    return len(regimes)


def _cluster_key(row: dict[str, Any]) -> str:
    candidate = dict(row.get("candidate") or {})
    blocks_hash = sha256(str(candidate.get("blocks") or {}).encode("utf-8")).hexdigest()[:12]
    parent = row.get("parent_candidate_id") or candidate.get("parent_candidate_id") or row.get("candidate_id")
    return "|".join(
        [
            str(row.get("symbol") or "").upper(),
            str(row.get("timeframe") or ""),
            str(row.get("strategy_family") or row.get("family_id") or ""),
            str(parent or ""),
            blocks_hash,
            _candidate_direction(candidate),
        ]
    )


def _score(row: dict[str, Any]) -> float:
    result = dict(row.get("result") or {})
    pf = _metric(result, "profit_factor")
    expectancy = _metric(result, "expectancy_per_trade")
    drawdown = _metric(result, "max_drawdown", 1.0)
    trades = _metric(result, "number_of_trades")
    validation = float(row.get("validation_score") or 0)
    pf_score = min(1.2, pf / 1.5)
    expectancy_score = max(0.0, min(1.0, expectancy / 12.0))
    drawdown_score = max(0.0, 1.0 - drawdown / 0.12)
    trade_score = min(1.0, trades / 90.0)
    validation_score = max(0.0, min(1.0, validation))
    return round(
        pf_score * 0.30
        + expectancy_score * 0.25
        + drawdown_score * 0.20
        + trade_score * 0.20
        + validation_score * 0.05,
        6,
    )


def _load_promoted_jobs(
    conn: psycopg.Connection,
    *,
    limit: int,
    min_profit_factor: float,
    min_trades: int,
    max_drawdown: float,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            j.*,
            c.name AS campaign_name
        FROM research_campaign_jobs j
        JOIN research_campaigns c ON c.id = j.campaign_id
        WHERE j.simulation_only = TRUE
          AND j.status = 'promoted'
          AND j.candidate IS NOT NULL
          AND j.result IS NOT NULL
          AND COALESCE((j.result->'metrics'->>'profit_factor')::double precision, 0) >= %s
          AND COALESCE((j.result->'metrics'->>'expectancy_per_trade')::double precision, 0) > 0
          AND COALESCE((j.result->'metrics'->>'number_of_trades')::double precision, 0) >= %s
          AND COALESCE((j.result->'metrics'->>'max_drawdown')::double precision, 1) <= %s
          AND NOT EXISTS (
              SELECT 1
              FROM elite_research_candidates e
              WHERE e.campaign_id = j.campaign_id
                AND e.candidate_id = j.candidate_id
                AND e.simulation_only = TRUE
          )
        ORDER BY j.updated_at DESC NULLS LAST, j.id DESC
        LIMIT %s
        """,
        (min_profit_factor, min_trades, max_drawdown, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def research_champion_status(conn: psycopg.Connection) -> dict[str, Any]:
    backlog = conn.execute(
        """
        SELECT
            COUNT(*) AS eligible_promoted_jobs,
            COUNT(DISTINCT j.symbol) AS symbols,
            COUNT(DISTINCT j.timeframe) AS timeframes,
            COUNT(DISTINCT COALESCE(j.strategy_family, j.family_id)) AS families
        FROM research_campaign_jobs j
        WHERE j.simulation_only = TRUE
          AND j.status = 'promoted'
          AND j.candidate IS NOT NULL
          AND j.result IS NOT NULL
          AND COALESCE((j.result->'metrics'->>'profit_factor')::double precision, 0) >= 1.25
          AND COALESCE((j.result->'metrics'->>'expectancy_per_trade')::double precision, 0) > 0
          AND COALESCE((j.result->'metrics'->>'number_of_trades')::double precision, 0) >= 30
          AND COALESCE((j.result->'metrics'->>'max_drawdown')::double precision, 1) <= 0.12
          AND NOT EXISTS (
              SELECT 1
              FROM elite_research_candidates e
              WHERE e.campaign_id = j.campaign_id
                AND e.candidate_id = j.candidate_id
                AND e.simulation_only = TRUE
          )
        """
    ).fetchone()
    imported = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion') AS research_champions,
            COUNT(*) FILTER (WHERE promotion_state = 'elite') AS final_elites,
            COUNT(*) FILTER (WHERE forward_validation_state = 'insufficient_forward_sample') AS awaiting_forward_sample,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'pending_validation') AS pending_validation,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'validating') AS validating,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'failed_validation') AS failed_validation,
            COUNT(*) FILTER (WHERE promotion_state = 'research_champion' AND validation_state = 'needs_more_data') AS needs_more_data,
            COUNT(*) FILTER (WHERE promotion_state = 'elite' AND validation_state = 'validated') AS graduated_elites
        FROM elite_research_candidates
        WHERE simulation_only = TRUE
        """
    ).fetchone()
    return {
        "eligible_promoted_jobs": int((backlog or {}).get("eligible_promoted_jobs") or 0),
        "symbols": int((backlog or {}).get("symbols") or 0),
        "timeframes": int((backlog or {}).get("timeframes") or 0),
        "families": int((backlog or {}).get("families") or 0),
        "research_champions": int((imported or {}).get("research_champions") or 0),
        "final_elites": int((imported or {}).get("final_elites") or 0),
        "awaiting_forward_sample": int((imported or {}).get("awaiting_forward_sample") or 0),
        # Champion graduation state (Phase 13.9). `graduated_elites` counts the
        # final elites that reached that state through the validation battery,
        # as distinct from elites promoted by the earlier campaign gates.
        "pending_validation": int((imported or {}).get("pending_validation") or 0),
        "validating": int((imported or {}).get("validating") or 0),
        "failed_validation": int((imported or {}).get("failed_validation") or 0),
        "needs_more_data": int((imported or {}).get("needs_more_data") or 0),
        "graduated_elites": int((imported or {}).get("graduated_elites") or 0),
        "promotion_rule_version": RESEARCH_CHAMPION_RULE_VERSION,
    }


def _existing_cluster_keys(conn: psycopg.Connection, *, promotion_states: tuple[str, ...]) -> set[str]:
    """Cluster keys already covered by a live (non-demoted) champion or elite.

    `_load_promoted_jobs`'s `NOT EXISTS` only ever excluded a literal
    (campaign_id, candidate_id) pair already imported. It never excluded a
    *different* pair that happens to be the same effective strategy -- which
    is exactly what independent campaign runs of a near-identical family
    produce, each under a fresh candidate_id. This closes that gap by
    recomputing `_cluster_key` for everything already imported and folding it
    into the dedup set before ranking new candidates.
    """
    rows = conn.execute(
        """
        SELECT
            e.candidate_id, e.campaign_id,
            j.symbol, j.timeframe, j.candidate, j.strategy_family,
            j.family_id AS job_family_id, j.parent_candidate_id AS job_parent_candidate_id
        FROM elite_research_candidates e
        JOIN LATERAL (
            SELECT symbol, timeframe, candidate, strategy_family, family_id, parent_candidate_id
            FROM research_campaign_jobs
            WHERE campaign_id = e.campaign_id AND candidate_id = e.candidate_id AND simulation_only = TRUE
            ORDER BY (status = 'promoted') DESC, id DESC
            LIMIT 1
        ) j ON TRUE
        WHERE e.simulation_only = TRUE
          AND e.promotion_state = ANY(%s)
        """,
        (list(promotion_states),),
    ).fetchall()
    return {
        _cluster_key(
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "strategy_family": row["strategy_family"],
                "family_id": row["job_family_id"],
                "parent_candidate_id": row["job_parent_candidate_id"],
                "candidate": row["candidate"],
            }
        )
        for row in rows
    }


def import_research_champions(
    conn: psycopg.Connection,
    *,
    max_champions: int = 25,
    min_profit_factor: float = 1.25,
    min_trades: int = 30,
    max_drawdown: float = 0.12,
) -> dict[str, Any]:
    from app.services.research_campaigns import ensure_campaign_tables

    ensure_campaign_tables(conn)
    # The WHERE clause already bounds the query to actual eligible jobs, so a
    # caller can safely ask for "all of them" via a large max_champions rather
    # than needing to know the exact backlog size up front.
    bounded = max(1, min(int(max_champions), 5000))
    candidates = _load_promoted_jobs(
        conn,
        limit=max(500, bounded * 80),
        min_profit_factor=min_profit_factor,
        min_trades=min_trades,
        max_drawdown=max_drawdown,
    )
    ranked = sorted(candidates, key=_score, reverse=True)
    # Seeded with every cluster a live champion or elite already covers, so a
    # different campaign's near-identical job can no longer sneak in under a
    # fresh candidate_id -- see `_existing_cluster_keys`.
    already_covered = _existing_cluster_keys(conn, promotion_states=("research_champion", "elite"))
    seen_clusters: set[str] = set(already_covered)
    selected: list[dict[str, Any]] = []
    for row in ranked:
        cluster = _cluster_key(row)
        if cluster in seen_clusters:
            continue
        seen_clusters.add(cluster)
        selected.append(row)
        if len(selected) >= bounded:
            break

    imported: list[dict[str, Any]] = []
    for row in selected:
        result = dict(row.get("result") or {})
        metrics = dict(result.get("metrics") or {})
        candidate = dict(row.get("candidate") or {})
        score = _score(row)
        direction = _candidate_direction(candidate)
        execution_capability = "external_observe" if direction == "long" else "internal_only"
        trade_count = int(_metric(result, "number_of_trades"))
        validation_history = {
            "source": "research_campaign_jobs",
            "source_job_id": row.get("id"),
            "source_campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "status": row.get("status"),
            "result": jsonable(result),
            "import_policy": {
                "stage": "research_champion",
                "final_elite": False,
                "requires_forward_validation": True,
                "requires_cross_asset_confirmation": True,
                "thresholds_weakened": False,
            },
        }
        paper_performance = {
            "paper_trades": 0,
            "paper_pnl": 0,
            "drawdown": 0,
            "daily_performance": [],
            "signal_frequency": 0,
            "stage": "awaiting_forward_validation",
        }
        inserted = conn.execute(
            """
            INSERT INTO elite_research_candidates(
                campaign_id, candidate_id, family_id, strategy_name, strategy_version, research_score,
                profit_factor, expectancy, max_drawdown, trade_count, stability, assets_passed,
                timeframes_passed, regimes_passed, validation_history, paper_performance, simulation_only,
                forward_validation_state, drift_status, candidate_level, scope_type, scope_ref, dataset_id,
                hypothesis_version_id, parent_candidate_id, strategy_direction, execution_capability,
                promotion_state, promotion_rule_version, median_profit_factor, median_expectancy,
                median_max_drawdown, median_variant_trade_count, demotion_reason, reevaluated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, TRUE,
                'insufficient_forward_sample', 'insufficient_forward_sample', 'research_champion',
                'research_job', %s, %s,
                %s, %s, %s, %s,
                'research_champion', %s, %s, %s,
                %s, %s, %s, NOW()
            )
            ON CONFLICT(candidate_id, campaign_id) DO UPDATE
            SET research_score = EXCLUDED.research_score,
                profit_factor = EXCLUDED.profit_factor,
                expectancy = EXCLUDED.expectancy,
                max_drawdown = EXCLUDED.max_drawdown,
                trade_count = EXCLUDED.trade_count,
                validation_history = EXCLUDED.validation_history,
                paper_performance = EXCLUDED.paper_performance,
                forward_validation_state = 'insufficient_forward_sample',
                drift_status = 'insufficient_forward_sample',
                candidate_level = 'research_champion',
                scope_type = 'research_job',
                scope_ref = EXCLUDED.scope_ref,
                dataset_id = EXCLUDED.dataset_id,
                parent_candidate_id = EXCLUDED.parent_candidate_id,
                strategy_direction = EXCLUDED.strategy_direction,
                execution_capability = EXCLUDED.execution_capability,
                promotion_state = 'research_champion',
                promotion_rule_version = EXCLUDED.promotion_rule_version,
                median_profit_factor = EXCLUDED.median_profit_factor,
                median_expectancy = EXCLUDED.median_expectancy,
                median_max_drawdown = EXCLUDED.median_max_drawdown,
                median_variant_trade_count = EXCLUDED.median_variant_trade_count,
                demotion_reason = EXCLUDED.demotion_reason,
                reevaluated_at = NOW()
            RETURNING id
            """,
            (
                row["campaign_id"],
                row["candidate_id"],
                row.get("family_id") or row.get("strategy_family") or "unknown_family",
                "research_champion",
                row["candidate_id"],
                score,
                _metric(result, "profit_factor"),
                _metric(result, "expectancy_per_trade"),
                _metric(result, "max_drawdown", 1.0),
                trade_count,
                1.0,
                1,
                1,
                _regimes_passed(result),
                Jsonb(jsonable(validation_history)),
                Jsonb(jsonable(paper_performance)),
                str(row.get("id")),
                row.get("dataset_id"),
                row.get("hypothesis_version_id"),
                row.get("parent_candidate_id"),
                direction,
                execution_capability,
                RESEARCH_CHAMPION_RULE_VERSION,
                _metric(result, "profit_factor"),
                _metric(result, "expectancy_per_trade"),
                _metric(result, "max_drawdown", 1.0),
                trade_count,
                "research champion awaiting cross-asset and forward validation",
            ),
        ).fetchone()
        imported.append(
            {
                "id": int(inserted["id"]),
                "campaign_id": int(row["campaign_id"]),
                "job_id": int(row["id"]),
                "candidate_id": row["candidate_id"],
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "strategy_family": row.get("strategy_family"),
                "profit_factor": _metric(result, "profit_factor"),
                "expectancy": _metric(result, "expectancy_per_trade"),
                "trade_count": trade_count,
                "max_drawdown": _metric(result, "max_drawdown", 1.0),
                "score": score,
                "cluster_key": _cluster_key(row),
            }
        )
    conn.commit()
    return {
        "imported": len(imported),
        "examined": len(candidates),
        # New distinct clusters found in this call's candidate pool -- not the
        # running total, which also includes every cluster a prior import
        # already covered (see `already_covered` above).
        "dedupe_clusters_seen": len(seen_clusters) - len(already_covered),
        "already_covered_clusters": len(already_covered),
        "max_champions": bounded,
        "promotion_rule_version": RESEARCH_CHAMPION_RULE_VERSION,
        "promotion_state": "research_champion",
        "final_elites_created": 0,
        "thresholds_weakened": False,
        "champions": imported,
        "status": research_champion_status(conn),
    }


def dedupe_research_champions(conn: psycopg.Connection, *, dry_run: bool = False) -> dict[str, Any]:
    """Collapse already-imported duplicate champions down to one per cluster.

    Fixing `import_research_champions`'s dedup (see `_existing_cluster_keys`)
    stops *new* duplicates. It does nothing about champions a prior import
    already created before that fix existed -- potentially hundreds of literal
    copies of the same strategy, each of which champion validation would
    re-measure independently at full cost for an identical verdict.

    This groups every `research_champion` row by the same cluster key import
    uses, keeps the single highest-scoring row per cluster exactly as it is,
    and demotes the rest with `promotion_state = 'demoted'` -- the same
    non-destructive exclusion state `reevaluate_elite_candidates` uses for a
    failed consistency gate. Nothing is deleted, no score is recalculated, and
    an already-graduated `elite` row is never touched: if two elites turn out
    to be near-duplicates, that is what the validation battery's own
    correlation and parameter-similarity gates are for, not a blunt cleanup.
    """
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                e.id, e.candidate_id, e.campaign_id, e.research_score,
                j.symbol, j.timeframe, j.candidate, j.strategy_family,
                j.family_id AS job_family_id, j.parent_candidate_id AS job_parent_candidate_id
            FROM elite_research_candidates e
            JOIN LATERAL (
                SELECT symbol, timeframe, candidate, strategy_family, family_id, parent_candidate_id
                FROM research_campaign_jobs
                WHERE campaign_id = e.campaign_id AND candidate_id = e.candidate_id AND simulation_only = TRUE
                ORDER BY (status = 'promoted') DESC, id DESC
                LIMIT 1
            ) j ON TRUE
            WHERE e.simulation_only = TRUE
              AND e.promotion_state = 'research_champion'
            """
        ).fetchall()
    ]

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cluster = _cluster_key(
            {
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "strategy_family": row["strategy_family"],
                "family_id": row["job_family_id"],
                "parent_candidate_id": row["job_parent_candidate_id"],
                "candidate": row["candidate"],
            }
        )
        groups.setdefault(cluster, []).append(row)

    duplicate_groups = 0
    demoted_ids: list[int] = []
    kept: list[dict[str, Any]] = []
    for cluster, members in groups.items():
        if len(members) < 2:
            continue
        duplicate_groups += 1
        ranked_members = sorted(members, key=lambda row: (float(row["research_score"] or 0), row["id"]), reverse=True)
        keeper = ranked_members[0]
        losers = ranked_members[1:]
        kept.append({"id": keeper["id"], "candidate_id": keeper["candidate_id"], "cluster_key": cluster, "duplicates_demoted": len(losers)})
        demoted_ids.extend(int(loser["id"]) for loser in losers)
        if not dry_run:
            reason = f"duplicate_of:{keeper['candidate_id']}|{keeper['campaign_id']}|cluster:{cluster}"
            conn.execute(
                """
                UPDATE elite_research_candidates
                SET promotion_state = 'demoted',
                    demotion_reason = %s,
                    reevaluated_at = NOW()
                WHERE id = ANY(%s)
                """,
                (reason[:2000], [int(loser["id"]) for loser in losers]),
            )

    if not dry_run:
        conn.commit()

    return {
        "champions_examined": len(rows),
        "clusters_examined": len(groups),
        "duplicate_clusters": duplicate_groups,
        "champions_demoted": len(demoted_ids),
        "champions_kept_per_cluster": kept[:50],
        "dry_run": dry_run,
        "status": research_champion_status(conn),
    }
