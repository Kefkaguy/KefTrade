from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.research_campaigns import jsonable
from app.services.strategy_discovery import candidate_execution_key


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


def _execution_parameters_key(candidate: dict[str, Any]) -> str:
    """The campaign-level "same executable strategy" key, applied to a stored payload.

    Reuses `candidate_execution_key` rather than re-deriving its exclusion list
    (research-provenance parameters such as hypothesis ids and campaign
    versions, which differ between runs of an identical strategy) so the two
    definitions cannot drift apart. That function reads only `.parameters`,
    which is why a namespace stands in for a full DiscoveryCandidate here.
    """
    return candidate_execution_key(SimpleNamespace(parameters=dict(candidate.get("parameters") or {})))


def _cluster_key(row: dict[str, Any]) -> str:
    """Identity of the *strategy*, not of the row that happens to carry it.

    Deliberately excludes candidate_id, campaign_id and lineage. Two
    independent campaign runs that produce the same executable strategy are one
    strategy, and keying on anything row-specific is what let the same AMD 30m
    Momentum variant into the champion queue over a thousand times -- each copy
    then costing a full validation battery to reach an identical verdict.

    The previous key fell back to `candidate_id` whenever lineage was absent
    (which is always: `research_campaign_jobs.parent_candidate_id` is never
    populated on insert, and generated intraday candidates carry
    `parent_candidate_id=None`), making every row its own cluster and the
    dedup a no-op. Blocks are also hashed from canonical JSON rather than
    `str(dict)`, whose output depends on insertion order.
    """
    candidate = dict(row.get("candidate") or {})
    blocks_hash = sha256(json.dumps(candidate.get("blocks") or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    execution_hash = sha256(_execution_parameters_key(candidate).encode("utf-8")).hexdigest()[:16]
    return "|".join(
        [
            str(row.get("symbol") or "").upper(),
            str(row.get("timeframe") or ""),
            str(row.get("strategy_family") or row.get("family_id") or ""),
            blocks_hash,
            execution_hash,
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


STATUS_BACKLOG_SCAN_LIMIT = 5000


def selectable_champion_rows(
    conn: psycopg.Connection,
    *,
    limit: int,
    min_profit_factor: float = 1.25,
    min_trades: int = 30,
    max_drawdown: float = 0.12,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The rows an import would actually insert, and why the rest were skipped.

    One definition, used by both the backlog count and the import. They used
    to disagree: the count excluded only an exact (campaign_id, candidate_id)
    already imported, while the import ALSO dropped anything whose
    `_cluster_key` matched a live champion. A job that was a new row but the
    same effective strategy therefore counted as eligible and could never be
    imported -- the backlog showed a number the button could not deliver, and
    it never went down.
    """
    candidates = _load_promoted_jobs(
        conn,
        limit=max(500, limit * 80),
        min_profit_factor=min_profit_factor,
        min_trades=min_trades,
        max_drawdown=max_drawdown,
    )
    ranked = sorted(candidates, key=_score, reverse=True)
    already_covered = _existing_cluster_keys(conn, promotion_states=("research_champion", "elite"))
    seen_clusters: set[str] = set(already_covered)
    selected: list[dict[str, Any]] = []
    duplicate_of_existing = 0
    duplicate_within_backlog = 0
    for row in ranked:
        cluster = _cluster_key(row)
        if cluster in already_covered:
            duplicate_of_existing += 1
            continue
        if cluster in seen_clusters:
            duplicate_within_backlog += 1
            continue
        seen_clusters.add(cluster)
        if len(selected) < limit:
            selected.append(row)
    return selected, {
        "eligible_jobs_scanned": len(candidates),
        "duplicate_of_existing_champion": duplicate_of_existing,
        "duplicate_within_backlog": duplicate_within_backlog,
    }


def research_champion_status(conn: psycopg.Connection) -> dict[str, Any]:
    importable, skipped = selectable_champion_rows(conn, limit=STATUS_BACKLOG_SCAN_LIMIT)
    backlog = {
        "eligible_promoted_jobs": len(importable),
        "symbols": len({str(row.get("symbol") or "") for row in importable}),
        "timeframes": len({str(row.get("timeframe") or "") for row in importable}),
        "families": len(
            {str(row.get("strategy_family") or row.get("family_id") or "") for row in importable}
        ),
    }
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
        # `eligible_promoted_jobs` above counts strategies the import would
        # actually add. These explain the gap to the raw job count, so a
        # backlog that refuses to shrink is legible rather than mysterious.
        "eligible_jobs_scanned": skipped["eligible_jobs_scanned"],
        "duplicate_of_existing_champion": skipped["duplicate_of_existing_champion"],
        "duplicate_within_backlog": skipped["duplicate_within_backlog"],
        "backlog_scan_limit": STATUS_BACKLOG_SCAN_LIMIT,
        "backlog_note": (
            "Eligible counts distinct strategies, not job rows. Jobs whose symbol, timeframe, family, "
            "blocks, execution parameters and direction already match a live champion are not importable "
            "and are reported separately."
        ),
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
    # Same selection the backlog count reports, so the number on the button and
    # the number actually inserted cannot disagree -- see
    # `selectable_champion_rows`.
    selected, skipped = selectable_champion_rows(
        conn,
        limit=bounded,
        min_profit_factor=min_profit_factor,
        min_trades=min_trades,
        max_drawdown=max_drawdown,
    )

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
        "examined": skipped["eligible_jobs_scanned"],
        # New distinct strategies this call could add -- not the running total,
        # which also includes every cluster a prior import already covered.
        "dedupe_clusters_seen": len(selected),
        "already_covered_clusters": skipped["duplicate_of_existing_champion"],
        # Why eligible job rows did not become champions. When `imported` is 0
        # but rows were examined, these two say which -- the answer is almost
        # always that the strategy is already represented.
        "skipped_duplicate_of_existing_champion": skipped["duplicate_of_existing_champion"],
        "skipped_duplicate_within_backlog": skipped["duplicate_within_backlog"],
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
