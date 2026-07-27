"""Pooled cross-sectional evidence for Strategy Engine V2 candidates.

KefTrade's elite gate has always evaluated one (candidate, symbol) job in
isolation -- a hypothesis that fires 8 times on TSLA, 10 times on NVDA, and
12 times on GOOGL never gets credit for having been genuinely tested 30
times. Real quantitative research gets statistical power from breadth (many
independent trials of the same hypothesis) rather than forcing depth on a
single instrument -- see Grinold's Fundamental Law of Active Management
(Information Ratio = skill x sqrt(breadth)).

This module ADDS a second, pooled evaluation on top of the unchanged
per-symbol gate. For each canonical candidate (same frozen parameters,
tested across multiple symbols) in a campaign, it pools every contributing
symbol's trades and re-evaluates the SAME gate functions
(`paper_readiness_report`, `status_for_candidate`, `score_metrics`) used
everywhere else in the codebase -- same thresholds, same logic, just over
combined evidence instead of siloed evidence. It never reads or writes
`elite_research_candidates` and cannot promote anything the per-symbol gate
already rejected from that table; it writes to
`research_candidate_stage_evidence` (migration 028), a staging table that
has existed since Phase 11/12 and was never wired up.

Two important honesty constraints, both explicit in the output:
  * Pooling requires at least 2 distinct contributing symbols -- pooling a
    candidate with itself adds no real breadth and is skipped.
  * Drawdown cannot be pooled into one meaningful blended equity curve
    (each symbol's job ran its own independent simulation with its own
    notional capital); fabricating one would invent false precision. The
    honest substitute is the worst single contributing symbol's own
    drawdown -- pooled evidence can never look safer on drawdown than its
    riskiest contributor.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

POOLED_EVIDENCE_VERSION = "pooled_cross_sectional_evidence_v1"

HOLDOUT_CONFIRMATION_VERSION = "holdout_confirmed_cluster_elite_v1"

# A pool built from fewer contributing symbols than this is not a
# cross-sectional evidence unit -- see the module docstring.
MINIMUM_CONTRIBUTING_SYMBOLS = 2


def compute_pooled_candidate_evidence(conn: psycopg.Connection, *, campaign_id: int) -> list[dict[str, Any]]:
    """Pool every symbol's trades for each canonical candidate in a campaign
    and evaluate the SAME elite gate over the combined evidence.

    Idempotent by `evidence_key`: rerunning a campaign's pooling updates the
    existing rows in place rather than duplicating them, so a candidate's
    pooled evidence always reflects the campaign's current, immutable trade
    data -- never an accumulation of duplicate runs.
    """
    from app.services.strategy_discovery import failure_reasons_for, status_for_candidate
    from app.services.strategy_research import paper_readiness_report, score_metrics

    jobs = list(
        conn.execute(
            """
            SELECT id, symbol, dataset_id, candidate->>'candidate_id' AS candidate_id,
                   candidate->'parameters'->>'strategy_architecture' AS strategy_architecture,
                   result->'metrics'->>'max_drawdown' AS job_max_drawdown,
                   (result->'metrics'->'walk_forward'->>'enabled')::boolean AS walk_forward_enabled
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND status <> 'queued'
            """,
            (campaign_id,),
        ).fetchall()
    )

    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        candidate_id = job["candidate_id"]
        if not candidate_id:
            continue
        by_candidate.setdefault(candidate_id, []).append(job)

    results: list[dict[str, Any]] = []
    for candidate_id, candidate_jobs in sorted(by_candidate.items()):
        symbols = sorted({job["symbol"] for job in candidate_jobs})
        if len(symbols) < MINIMUM_CONTRIBUTING_SYMBOLS:
            continue

        job_ids = [job["id"] for job in candidate_jobs]
        trade_rows = list(
            conn.execute(
                """
                SELECT symbol, net_pnl, pnl_pct, holding_period_hours
                FROM research_campaign_trades
                WHERE job_id = ANY(%s)
                ORDER BY entry_time ASC
                """,
                (job_ids,),
            ).fetchall()
        )
        if not trade_rows:
            continue

        trades = [
            {
                "pnl": Decimal(str(row["net_pnl"])),
                "pnl_pct": row["pnl_pct"],
                "holding_period_hours": row["holding_period_hours"],
            }
            for row in trade_rows
        ]
        metrics = _pooled_metrics(trades, candidate_jobs, contributing_symbols=len(symbols))

        # Intraday V2 jobs already pass empty regime breakdowns to this same
        # gate (run_intraday_campaign_job never builds context_by_time), so
        # regime_stability trivially passes there today; pooling preserves
        # that exact behavior rather than inventing a stricter check.
        readiness = paper_readiness_report(metrics, [], [])
        research_score = round(score_metrics(metrics), 4)
        status = status_for_candidate(metrics, readiness, research_score)
        failure_reasons = failure_reasons_for(metrics, readiness, [], [])

        architecture = candidate_jobs[0]["strategy_architecture"]
        evidence_key = f"{POOLED_EVIDENCE_VERSION}:{campaign_id}:{candidate_id}"
        row = conn.execute(
            """
            INSERT INTO research_candidate_stage_evidence(
                evidence_key, campaign_id, candidate_id, candidate_level, scope_type, scope_ref,
                gate_results, metrics, evidence_refs, promoted, calculation_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (evidence_key) DO UPDATE SET
                gate_results = EXCLUDED.gate_results,
                metrics = EXCLUDED.metrics,
                evidence_refs = EXCLUDED.evidence_refs,
                promoted = EXCLUDED.promoted
            RETURNING *
            """,
            (
                evidence_key,
                campaign_id,
                candidate_id,
                "cluster_candidate",
                "cluster",
                ",".join(symbols),
                Jsonb({**readiness, "research_score": research_score, "failure_reasons": failure_reasons}),
                Jsonb(metrics),
                Jsonb(
                    {
                        "job_ids": job_ids,
                        "symbols": symbols,
                        "architecture": architecture,
                        "trade_count_by_symbol": _trade_count_by_symbol(trade_rows),
                    }
                ),
                status == "promoted",
                POOLED_EVIDENCE_VERSION,
            ),
        ).fetchone()
        conn.commit()
        results.append(dict(row))
    return results


def _pooled_metrics(
    trades: list[dict[str, Any]],
    candidate_jobs: list[dict[str, Any]],
    *,
    contributing_symbols: int,
) -> dict[str, Any]:
    from app.services.backtester import average_holding_time_hours, longest_losing_streak

    wins = [trade["pnl"] for trade in trades if trade["pnl"] > 0]
    losses = [trade["pnl"] for trade in trades if trade["pnl"] <= 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    win_rate = Decimal(len(wins)) / Decimal(len(trades)) if trades else Decimal("0")
    avg_win = gross_profit / Decimal(len(wins)) if wins else Decimal("0")
    avg_loss = gross_loss / Decimal(len(losses)) if losses else Decimal("0")
    loss_rate = Decimal("1") - win_rate if trades else Decimal("0")
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    profit_factor_is_infinite = gross_loss == 0 and gross_profit > 0

    contributing_drawdowns = [
        float(job["job_max_drawdown"]) for job in candidate_jobs if job.get("job_max_drawdown") is not None
    ]
    max_drawdown = max(contributing_drawdowns) if contributing_drawdowns else 0.0

    walk_forward_enabled = bool(candidate_jobs) and all(job.get("walk_forward_enabled") for job in candidate_jobs)

    return {
        "win_rate": float(win_rate),
        "average_win": float(avg_win),
        "average_loss": float(avg_loss),
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
        "profit_factor_is_infinite": profit_factor_is_infinite,
        "max_drawdown": max_drawdown,
        "max_drawdown_source": "worst_contributing_symbol",
        "number_of_trades": len(trades),
        "expectancy_per_trade": float(expectancy),
        "longest_losing_streak": longest_losing_streak(trades),
        "average_holding_time_hours": average_holding_time_hours(trades),
        "walk_forward": {"enabled": walk_forward_enabled},
        "contributing_symbol_count": contributing_symbols,
    }


def _trade_count_by_symbol(trade_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in trade_rows:
        counts[row["symbol"]] = counts.get(row["symbol"], 0) + 1
    return counts


def compute_holdout_confirmation(conn: psycopg.Connection, *, campaign_id: int) -> list[dict[str, Any]]:
    """Confirm pooled candidates on trades the pooled pass did not already use.

    **Phase A correction.** This function was written believing that
    `persist_intraday_job_trades`' `dataset_split` tag produced two
    independent samples. The simulator audit disproved that: `run_backtest`
    skips its training window and trades only the validation portion, so every
    stored trade is tagged `'validation'` and none is tagged `'train'`. Under
    that condition the "holdout" is the *entire* traded sample -- byte for byte
    the same trades the pooled gate already scored -- and re-running the gate
    on it confirms nothing.

    So independence is now checked rather than assumed. When a candidate has
    no `'train'`-tagged trades at all, the split is degenerate and the
    candidate is NOT confirmed, with reason `HOLDOUT_NOT_INDEPENDENT`. That
    deliberately leaves `cluster_elite` unpopulated for such campaigns, which
    is the honest state: no genuine out-of-sample evidence exists in stored
    trades, and inventing a confirmation from a set compared with itself would
    be worse than having none.

    Genuine confirmation lives in `app/services/research_splits.py`, which
    holds a locked window out of research entirely and permits exactly one run
    per frozen candidate.

    Where independence does hold, this stays strictly ADDITIVE and strictly
    HARDER: a candidate reaches `cluster_elite` only if it passed the pooled
    gate AND passes that same unchanged gate again on holdout trades alone.
    Two further constraints, both explicit in the returned rows:
      * The holdout must itself clear `MINIMUM_CONTRIBUTING_SYMBOLS` distinct
        symbols -- a confirmation resting on one symbol is not cross-sectional.
      * Drawdown still comes from each contributing job's own full-period
        figure (the same limitation the pooled path documents). That number
        covers the whole run, so it is at least as large as a holdout-only
        drawdown would be -- it makes this check harder to pass, never easier.

    Idempotent by `evidence_key`, exactly like the pooled pass.
    """
    from app.services.strategy_discovery import failure_reasons_for, status_for_candidate
    from app.services.strategy_research import paper_readiness_report, score_metrics

    pooled_rows = list(
        conn.execute(
            """
            SELECT candidate_id, promoted
            FROM research_candidate_stage_evidence
            WHERE campaign_id = %s AND calculation_version = %s AND candidate_level = 'cluster_candidate'
            ORDER BY candidate_id
            """,
            (campaign_id, POOLED_EVIDENCE_VERSION),
        ).fetchall()
    )
    if not pooled_rows:
        return []

    jobs = list(
        conn.execute(
            """
            SELECT id, symbol, candidate->>'candidate_id' AS candidate_id,
                   candidate->'parameters'->>'strategy_architecture' AS strategy_architecture,
                   result->'metrics'->>'max_drawdown' AS job_max_drawdown,
                   (result->'metrics'->'walk_forward'->>'enabled')::boolean AS walk_forward_enabled
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND status <> 'queued'
            """,
            (campaign_id,),
        ).fetchall()
    )
    jobs_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        candidate_id = job["candidate_id"]
        if not candidate_id:
            continue
        jobs_by_candidate.setdefault(candidate_id, []).append(job)

    results: list[dict[str, Any]] = []
    for pooled in pooled_rows:
        candidate_id = pooled["candidate_id"]
        candidate_jobs = jobs_by_candidate.get(candidate_id, [])
        evaluation: dict[str, Any] = {
            "candidate_id": candidate_id,
            "pooled_promoted": bool(pooled["promoted"]),
            "confirmed": False,
            "calculation_version": HOLDOUT_CONFIRMATION_VERSION,
        }
        if not candidate_jobs:
            evaluation["unconfirmed_reason"] = "NO_CONTRIBUTING_JOBS"
            results.append(evaluation)
            continue

        job_ids = [job["id"] for job in candidate_jobs]
        all_trade_rows = list(
            conn.execute(
                """
                SELECT job_id, symbol, net_pnl, pnl_pct, holding_period_hours, dataset_split
                FROM research_campaign_trades
                WHERE job_id = ANY(%s)
                ORDER BY entry_time ASC
                """,
                (job_ids,),
            ).fetchall()
        )
        trade_rows = [row for row in all_trade_rows if row["dataset_split"] == "validation"]
        training_rows = [row for row in all_trade_rows if row["dataset_split"] == "train"]
        if not trade_rows:
            # Either walk-forward never ran for these jobs (no split boundary
            # to tag trades with) or every trade fell in the training window.
            # Both mean "not confirmed out of sample", never "confirmed".
            evaluation["unconfirmed_reason"] = "NO_HOLDOUT_TRADES"
            results.append(evaluation)
            continue
        if not training_rows:
            # Degenerate split: the "holdout" is the whole traded sample, i.e.
            # exactly the trades the pooled gate already scored. Confirming
            # against it would be comparing a set with itself.
            evaluation["is_independent_sample"] = False
            evaluation["holdout_trade_count"] = len(trade_rows)
            evaluation["unconfirmed_reason"] = "HOLDOUT_NOT_INDEPENDENT"
            evaluation["remedy"] = (
                "No trade is tagged 'train', so this split cannot confirm anything. Use the locked "
                "confirmation protocol in app/services/research_splits.py."
            )
            results.append(evaluation)
            continue
        evaluation["is_independent_sample"] = True

        holdout_symbols = sorted({row["symbol"] for row in trade_rows})
        evaluation["holdout_trade_count"] = len(trade_rows)
        evaluation["holdout_symbols"] = holdout_symbols
        if len(holdout_symbols) < MINIMUM_CONTRIBUTING_SYMBOLS:
            evaluation["unconfirmed_reason"] = "INSUFFICIENT_HOLDOUT_SYMBOLS"
            results.append(evaluation)
            continue

        contributing_job_ids = {row["job_id"] for row in trade_rows}
        contributing_jobs = [job for job in candidate_jobs if job["id"] in contributing_job_ids]
        trades = [
            {
                "pnl": Decimal(str(row["net_pnl"])),
                "pnl_pct": row["pnl_pct"],
                "holding_period_hours": row["holding_period_hours"],
            }
            for row in trade_rows
        ]
        metrics = _pooled_metrics(trades, contributing_jobs, contributing_symbols=len(holdout_symbols))
        metrics["dataset_split"] = "validation"
        metrics["max_drawdown_source"] = "worst_contributing_symbol_full_period"

        readiness = paper_readiness_report(metrics, [], [])
        research_score = round(score_metrics(metrics), 4)
        status = status_for_candidate(metrics, readiness, research_score)
        failure_reasons = failure_reasons_for(metrics, readiness, [], [])
        holdout_passed = status == "promoted"
        evaluation["holdout_status"] = status
        evaluation["holdout_research_score"] = research_score
        evaluation["holdout_failure_reasons"] = failure_reasons
        evaluation["confirmed"] = holdout_passed and bool(pooled["promoted"])
        if not evaluation["confirmed"]:
            evaluation["unconfirmed_reason"] = (
                "HOLDOUT_GATE_FAILED" if not holdout_passed else "POOLED_GATE_NOT_PROMOTED"
            )
            results.append(evaluation)
            continue

        # Only a confirmed candidate gets a `cluster_elite` row, matching the
        # convention in research_architecture.build_candidate_stage_evidence
        # where a stage row's existence means that stage was actually reached.
        evidence_key = f"{HOLDOUT_CONFIRMATION_VERSION}:{campaign_id}:{candidate_id}"
        row = conn.execute(
            """
            INSERT INTO research_candidate_stage_evidence(
                evidence_key, campaign_id, candidate_id, candidate_level, scope_type, scope_ref,
                gate_results, metrics, evidence_refs, promoted, calculation_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (evidence_key) DO UPDATE SET
                gate_results = EXCLUDED.gate_results,
                metrics = EXCLUDED.metrics,
                evidence_refs = EXCLUDED.evidence_refs,
                promoted = EXCLUDED.promoted
            RETURNING *
            """,
            (
                evidence_key,
                campaign_id,
                candidate_id,
                "cluster_elite",
                "cluster",
                ",".join(holdout_symbols),
                Jsonb({**readiness, "research_score": research_score, "failure_reasons": failure_reasons}),
                Jsonb(metrics),
                Jsonb(
                    {
                        "job_ids": sorted(contributing_job_ids),
                        "symbols": holdout_symbols,
                        "architecture": contributing_jobs[0]["strategy_architecture"],
                        "trade_count_by_symbol": _trade_count_by_symbol(trade_rows),
                        "pooled_evidence_key": f"{POOLED_EVIDENCE_VERSION}:{campaign_id}:{candidate_id}",
                        "confirmation_basis": "validation_split_only",
                    }
                ),
                True,
                HOLDOUT_CONFIRMATION_VERSION,
            ),
        ).fetchone()
        conn.commit()
        evaluation["evidence"] = dict(row)
        results.append(evaluation)
    return results


def list_holdout_confirmations(conn: psycopg.Connection, *, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM research_candidate_stage_evidence
        WHERE campaign_id = %s AND calculation_version = %s
        ORDER BY candidate_id
        """,
        (campaign_id, HOLDOUT_CONFIRMATION_VERSION),
    ).fetchall()
    return [dict(row) for row in rows]


def list_pooled_evidence(conn: psycopg.Connection, *, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM research_candidate_stage_evidence
        WHERE campaign_id = %s AND calculation_version = %s
        ORDER BY promoted DESC, candidate_id
        """,
        (campaign_id, POOLED_EVIDENCE_VERSION),
    ).fetchall()
    return [dict(row) for row in rows]
