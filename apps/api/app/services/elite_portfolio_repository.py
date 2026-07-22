from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.services.elite_portfolio_builder import (
    DEFAULT_CONSTRAINTS,
    DEFAULT_THRESHOLDS,
    SOLVER_VERSION,
    candidate_key,
    decision_hash,
    normalized_configuration,
    preview,
)
from app.services.shared_cache import get_json, set_json


class PortfolioNotFound(ValueError):
    pass


class PortfolioStale(ValueError):
    pass


class PortfolioStateError(ValueError):
    pass


CORRELATION_EVIDENCE_VERSION = "aligned_marked_returns_v1"


def backfill_correlation_evidence(conn: psycopg.Connection, *, limit: int = 20) -> dict[str, Any]:
    """Generate compact evidence from frozen datasets without rewriting job results."""
    from app.services.research_campaigns import run_campaign_job

    bounded_limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        """
        SELECT DISTINCT ON (j.id)
            j.id, j.symbol, j.timeframe, j.candidate, j.dataset_id,
            e.id AS elite_candidate_id
        FROM elite_research_candidates e
        JOIN research_campaign_jobs j
          ON j.campaign_id = e.campaign_id
         AND j.candidate_id = e.candidate_id
         AND j.status IN ('completed', 'promoted')
         AND j.simulation_only = TRUE
        WHERE e.simulation_only = TRUE
          AND j.dataset_id IS NOT NULL
          AND COALESCE(jsonb_object_length(j.result->'strategy_returns'), 0) = 0
          AND NOT EXISTS (
              SELECT 1
              FROM elite_candidate_correlation_evidence evidence
              WHERE evidence.research_job_id = j.id
                AND evidence.evidence_version = %s
                AND evidence.simulation_only = TRUE
          )
        ORDER BY j.id, e.id
        LIMIT %s
        """,
        (CORRELATION_EVIDENCE_VERSION, bounded_limit + 1),
    ).fetchall()
    remaining = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    cache: dict[Any, Any] = {}
    generated = 0
    insufficient = 0
    failures: list[dict[str, Any]] = []
    for raw in rows:
        job = dict(raw)
        try:
            rerun = run_campaign_job(conn, {**job, "_dataset_cache": cache})
            strategy_returns = dict(rerun.get("strategy_returns") or {})
            signal_exposure = dict(rerun.get("signal_exposure") or {})
            evidence_hash = decision_hash({
                "research_job_id": job["id"],
                "dataset_id": job["dataset_id"],
                "candidate": job["candidate"],
                "evidence_version": CORRELATION_EVIDENCE_VERSION,
                "strategy_returns": strategy_returns,
                "signal_exposure": signal_exposure,
            })
            conn.execute(
                """
                INSERT INTO elite_candidate_correlation_evidence(
                    research_job_id, elite_candidate_id, dataset_id, evidence_version,
                    strategy_returns, signal_exposure, observation_count, evidence_hash,
                    simulation_only
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT(research_job_id, evidence_version, evidence_hash) DO NOTHING
                """,
                (
                    job["id"], job["elite_candidate_id"], job["dataset_id"],
                    CORRELATION_EVIDENCE_VERSION, Jsonb(strategy_returns), Jsonb(signal_exposure),
                    len(strategy_returns), evidence_hash,
                ),
            )
            conn.commit()
            generated += 1
            if len(strategy_returns) < 30:
                insufficient += 1
        except Exception as error:
            conn.rollback()
            failures.append({"research_job_id": job["id"], "error": str(error)})
    return {
        "evidence_version": CORRELATION_EVIDENCE_VERSION,
        "examined": len(rows),
        "generated": generated,
        "insufficient": insufficient,
        "failures": failures,
        "remaining": remaining,
        "simulation_only": True,
        "historical_results_rewritten": False,
        "constraints_relaxed": 0,
    }


def load_elite_candidate_variants(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            e.*,
            j.id AS research_job_id,
            j.symbol,
            j.timeframe,
            j.candidate,
            j.result,
            j.validation_score,
            j.consistency_score,
            stored.strategy_returns AS stored_strategy_returns,
            stored.signal_exposure AS stored_signal_exposure,
            replay.replay_trades
        FROM elite_research_candidates e
        JOIN LATERAL (
            SELECT DISTINCT ON (symbol, timeframe)
                id, symbol, timeframe, candidate, result, validation_score, consistency_score
            FROM research_campaign_jobs
            WHERE campaign_id = e.campaign_id
              AND candidate_id = e.candidate_id
              AND status IN ('completed', 'promoted')
              AND simulation_only = TRUE
            ORDER BY symbol, timeframe, validation_score DESC, id ASC
        ) j ON TRUE
        LEFT JOIN LATERAL (
            SELECT evidence.strategy_returns, evidence.signal_exposure
            FROM elite_candidate_correlation_evidence evidence
            WHERE evidence.research_job_id = j.id
              AND evidence.simulation_only = TRUE
            ORDER BY evidence.created_at DESC, evidence.id DESC
            LIMIT 1
        ) stored ON TRUE
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'entry_time', outcome.entry_time,
                    'exit_time', outcome.exit_time,
                    'side', 'long',
                    'pnl_pct', outcome.net_return_on_allocated_capital
                ) ORDER BY outcome.exit_time, outcome.id
            ) AS replay_trades
            FROM (
                SELECT x.id
                FROM external_paper_deployments x
                WHERE x.elite_candidate_id = e.id
                  AND x.symbol = j.symbol
                  AND x.timeframe = j.timeframe
                ORDER BY x.id DESC
                LIMIT 1
            ) deployment
            JOIN LATERAL (
                SELECT MAX(o.replay_run_id) AS replay_run_id
                FROM elite_shadow_replay_outcomes o
                WHERE o.external_deployment_id = deployment.id
                  AND o.status = 'completed'
            ) latest ON latest.replay_run_id IS NOT NULL
            JOIN elite_shadow_replay_outcomes outcome
              ON outcome.external_deployment_id = deployment.id
             AND outcome.replay_run_id = latest.replay_run_id
             AND outcome.status = 'completed'
        ) replay ON TRUE
        WHERE e.simulation_only = TRUE
        ORDER BY e.candidate_id, j.symbol, j.timeframe, e.id
        """
    ).fetchall()
    return [candidate_variant(dict(row)) for row in rows]


def candidate_variant(row: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(row.get("candidate") or {})
    result = dict(row.get("result") or {})
    metrics = dict(result.get("metrics") or {})
    parameters = dict(candidate.get("parameters") or {})
    direction = str(row.get("strategy_direction") or candidate.get("direction") or "long")
    symbol = str(row.get("symbol") or "").upper()
    timeframe = str(row.get("timeframe") or "")
    original_id = str(row["candidate_id"])
    key = f"{original_id}|{symbol}|{timeframe}"
    evidence_result = result
    if not result.get("strategy_returns") and row.get("stored_strategy_returns"):
        evidence_result = {
            **result,
            "strategy_returns": dict(row["stored_strategy_returns"]),
            "signal_exposure": dict(row.get("stored_signal_exposure") or {}),
        }
    elif not result.get("strategy_returns") and row.get("replay_trades"):
        evidence_result = {**result, "trades": list(row["replay_trades"])}
    strategy_returns, signal_returns = aligned_daily_evidence(evidence_result)
    health = health_classification(row)
    return {
        "elite_id": row.get("id"),
        "candidate_key": key,
        "candidate_id": original_id,
        "campaign_id": row.get("campaign_id"),
        "research_job_id": row.get("research_job_id"),
        "strategy_name": row.get("strategy_name"),
        "strategy_version": row.get("strategy_version"),
        "symbol": symbol,
        "timeframe": timeframe,
        "family_id": str(row.get("family_id") or candidate.get("family_id") or candidate.get("strategy_family") or "unknown"),
        "strategy_direction": direction,
        "execution_capability": str(row.get("execution_capability") or ("internal_only" if direction == "short" else "external_observe")),
        "parameters": parameters,
        "research_score": float(row.get("research_score") or 0),
        "quality_score": float(candidate.get("multi_objective_score") or row.get("research_score") or 0),
        "profit_factor": float(row.get("profit_factor") or metrics.get("profit_factor") or 0),
        "expectancy": float(row.get("expectancy") or metrics.get("expectancy_per_trade") or 0),
        "max_drawdown": float(row.get("max_drawdown") or metrics.get("max_drawdown") or 0),
        "trade_count": int(row.get("trade_count") or metrics.get("number_of_trades") or 0),
        "stability": float(row.get("stability") or row.get("consistency_score") or 0),
        "assets_passed": int(row.get("assets_passed") or 0),
        "timeframes_passed": int(row.get("timeframes_passed") or 0),
        "regimes_passed": int(row.get("regimes_passed") or 0),
        "health": health,
        "forward_validation_state": row.get("forward_validation_state"),
        "forward_evidence": {
            "state": row.get("forward_validation_state"),
            "paper_performance": row.get("paper_performance") or {},
            "validation_history": row.get("validation_history") or [],
        },
        "dataset_ids": sorted(_dataset_ids(candidate, result)),
        "data_snapshot_hash": decision_hash({"candidate": candidate, "result": result}),
        "strategy_returns": strategy_returns,
        "signal_returns": signal_returns,
        "opportunity_frequency": float(metrics.get("number_of_trades") or 0),
        "sector": candidate.get("sector"),
        "asset_class": candidate.get("asset_class") or "equity",
    }


def trade_return_series(result: dict[str, Any]) -> dict[str, float]:
    series: dict[str, float] = {}
    for index, trade in enumerate(result.get("trades") or []):
        key = str(trade.get("exit_time") or trade.get("exit_timestamp") or trade.get("entry_time") or trade.get("entry_timestamp") or f"trade-{index:06d}")
        while key in series:
            key = f"{key}#{index}"
        series[key] = float(trade.get("pnl_pct") or 0)
    return series


def aligned_daily_evidence(result: dict[str, Any], maximum_observations: int = 252) -> tuple[dict[str, float], dict[str, float]]:
    """Build aligned normalized equity changes and exposure on a weekday grid.

    New backtests persist per-bar marked returns and exposure. Historical
    backtests are reconstructed as realized daily equity changes plus active
    directional exposure, preserving existing outcomes without recalculation.
    """
    raw_returns = dict(result.get("strategy_returns") or {})
    raw_exposure = dict(result.get("signal_exposure") or {})
    trades = list(result.get("trades") or [])
    start, end = _evidence_window(result, raw_returns, trades)
    if start is None or end is None:
        return {}, {}
    days = [current for current in _date_range(start, end) if current.weekday() < 5]
    days = days[-maximum_observations:]
    if not days:
        return {}, {}

    daily_returns: dict[date, float] = {day: 0.0 for day in days}
    daily_exposure: dict[date, float] = {day: 0.0 for day in days}
    if raw_returns:
        grouped: dict[date, list[float]] = {}
        for timestamp, value in raw_returns.items():
            parsed = _parse_datetime(timestamp)
            if parsed and parsed.date() in daily_returns:
                grouped.setdefault(parsed.date(), []).append(float(value))
        for day, values in grouped.items():
            compounded = 1.0
            for value in values:
                compounded *= 1.0 + value
            daily_returns[day] = compounded - 1.0
    else:
        for trade in trades:
            exited = _parse_datetime(trade.get("exit_time") or trade.get("exit_timestamp"))
            if exited and exited.date() in daily_returns:
                daily_returns[exited.date()] += float(
                    trade.get("pnl_pct")
                    if trade.get("pnl_pct") is not None
                    else trade.get("net_return_on_allocated_capital") or 0
                )

    if raw_exposure:
        for timestamp, value in raw_exposure.items():
            parsed = _parse_datetime(timestamp)
            if not parsed or parsed.date() not in daily_exposure:
                continue
            exposure = float(value)
            current = daily_exposure[parsed.date()]
            if abs(exposure) >= abs(current):
                daily_exposure[parsed.date()] = exposure
    else:
        for trade in trades:
            entered = _parse_datetime(trade.get("entry_time") or trade.get("entry_timestamp"))
            exited = _parse_datetime(trade.get("exit_time") or trade.get("exit_timestamp"))
            if not entered or not exited:
                continue
            direction = -1.0 if str(trade.get("side") or "long") == "short" else 1.0
            for day in days:
                if entered.date() <= day <= exited.date():
                    daily_exposure[day] = direction

    return (
        {_daily_key(day): daily_returns[day] for day in days},
        {_daily_key(day): daily_exposure[day] for day in days},
    )


def _evidence_window(result: dict[str, Any], raw_returns: dict[str, Any], trades: list[dict[str, Any]]) -> tuple[date | None, date | None]:
    walk_forward = dict((result.get("metrics") or {}).get("walk_forward") or {})
    start = None if raw_returns else _parse_datetime(walk_forward.get("validation_start"))
    end = None if raw_returns else _parse_datetime(walk_forward.get("validation_end"))
    timestamps = [
        parsed
        for value in (
            list(raw_returns)
            + [trade.get("entry_time") or trade.get("entry_timestamp") for trade in trades]
            + [trade.get("exit_time") or trade.get("exit_timestamp") for trade in trades]
        )
        if (parsed := _parse_datetime(value)) is not None
    ]
    start = start or (min(timestamps) if timestamps else None)
    end = end or (max(timestamps) if timestamps else None)
    return (start.date() if start else None, end.date() if end else None)


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _daily_key(day: date) -> str:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value or str(value).startswith("trade-"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00").split("#", 1)[0])
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def health_classification(row: dict[str, Any]) -> str:
    trade_count = int(row.get("trade_count") or 0)
    profit_factor = float(row.get("profit_factor") or 0)
    expectancy = float(row.get("expectancy") or 0)
    stability = float(row.get("stability") or 0)
    if trade_count == 0:
        return "dead"
    if trade_count < 30:
        return "insufficient_data"
    if profit_factor < 1 or expectancy <= 0:
        return "broken"
    if stability < 0.60:
        return "unstable"
    return "healthy"


def options(conn: psycopg.Connection) -> dict[str, Any]:
    candidates = load_elite_candidate_variants(conn)
    return {
        "solver_version": SOLVER_VERSION,
        "universes": sorted({row["symbol"] for row in candidates}),
        "families": sorted({row["family_id"] for row in candidates}),
        "directions": ["long", "short"],
        "timeframes": sorted({row["timeframe"] for row in candidates}),
        "candidate_count": len(candidates),
        "default_thresholds": deepcopy(DEFAULT_THRESHOLDS),
        "default_constraints": deepcopy(DEFAULT_CONSTRAINTS),
        "objectives": ["balanced", "profit_factor", "expectancy", "minimum_drawdown"],
        "maximum_portfolio_size": 20,
        "execution_policy": {
            "long": "Internal activation; external record remains disabled and requires separate approval.",
            "short": "Internal simulation only. No external deployment, order, authorization, or broker path exists.",
        },
    }


def preview_from_database(
    conn: psycopg.Connection,
    configuration: dict[str, Any],
    *,
    use_cache: bool = True,
    include_decision_inputs: bool = False,
) -> dict[str, Any]:
    normalized = normalized_configuration(configuration)
    evidence_version = candidate_evidence_version(conn) if use_cache else None
    cache_key = f"elite-portfolio-preview:{decision_hash({'configuration': normalized, 'evidence_version': evidence_version})}"
    if use_cache:
        cached = get_json(cache_key)
        if cached is not None:
            cached["cache"] = {"hit": True, "key": cache_key}
            return cached
    candidates = load_elite_candidate_variants(conn)
    result = preview(candidates, normalized)
    if not include_decision_inputs:
        result["snapshot"] = {
            "decision_hash": result["snapshot"]["decision_hash"],
            "solver_version": SOLVER_VERSION,
            "candidate_evidence_count": len(candidates),
        }
        result["response_size_bytes"] = len(str(jsonable_encoder(result)).encode("utf-8"))
    result["cache"] = {"hit": False, "key": cache_key}
    if use_cache:
        set_json(cache_key, result, 300)
    return result


def create_run(conn: psycopg.Connection, configuration: dict[str, Any]) -> dict[str, Any]:
    result = preview_from_database(conn, configuration, use_cache=False, include_decision_inputs=True)
    config = result["configuration"]
    run_key = f"ep_{uuid4().hex}"
    snapshot_hash = result["snapshot"]["decision_hash"]
    row = conn.execute(
        """
        INSERT INTO elite_portfolio_runs(
            run_key, status, solver_version, objective, constraints, quality_thresholds,
            source_configuration, candidate_order, solver_iterations, solver_operations,
            termination_reason, statistics, portfolio_analytics, snapshot_hash, simulation_only
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (
            run_key, result["status"], SOLVER_VERSION, config["objective"], Jsonb(config["constraints"]),
            Jsonb(config["thresholds"]), Jsonb(config), Jsonb(result["candidate_order"]), result["iterations"],
            Jsonb(result["operations"]), result["termination_reason"], Jsonb(_statistics(result)),
            Jsonb(result["analytics"]), snapshot_hash,
        ),
    ).fetchone()
    run_id = int(row["id"])
    conn.execute(
        "INSERT INTO elite_portfolio_snapshots(portfolio_run_id, snapshot_hash, decision_inputs, simulation_only) VALUES (%s, %s, %s, TRUE)",
        (run_id, snapshot_hash, Jsonb(result["snapshot"])),
    )
    candidates = {candidate_key(item): item for item in load_elite_candidate_variants(conn)}
    _persist_eligibility(conn, run_id, result["eligibility"], candidates)
    _persist_correlations(conn, run_id, result["correlations"])
    _persist_conflicts(conn, run_id, result["conflicts"])
    _persist_members(conn, run_id, result["selected"], candidates)
    conn.commit()
    return get_run(conn, run_id)


def get_run(conn: psycopg.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM elite_portfolio_runs WHERE id = %s", (run_id,)).fetchone()
    if not run:
        raise PortfolioNotFound("elite portfolio run not found")
    members = conn.execute("SELECT * FROM elite_portfolio_members WHERE portfolio_run_id = %s ORDER BY rank", (run_id,)).fetchall()
    eligibility = conn.execute(
        """SELECT id,portfolio_run_id,elite_candidate_id,candidate_id,campaign_id,symbol,timeframe,
                  strategy_family,strategy_direction,execution_capability,eligible,health_classification,checks,created_at
           FROM elite_portfolio_eligibility WHERE portfolio_run_id = %s ORDER BY candidate_id, symbol, timeframe""",
        (run_id,),
    ).fetchall()
    conflicts = conn.execute("SELECT * FROM elite_portfolio_conflicts WHERE portfolio_run_id = %s ORDER BY conflict_type, left_candidate_key, right_candidate_key LIMIT 500", (run_id,)).fetchall()
    correlations = conn.execute("SELECT * FROM elite_portfolio_correlations WHERE portfolio_run_id = %s ORDER BY left_candidate_key, right_candidate_key, correlation_type LIMIT 500", (run_id,)).fetchall()
    snapshot = conn.execute("SELECT id,portfolio_run_id,snapshot_hash,created_at,simulation_only FROM elite_portfolio_snapshots WHERE portfolio_run_id = %s ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    attempts = conn.execute("SELECT * FROM elite_portfolio_activation_attempts WHERE portfolio_run_id = %s ORDER BY id", (run_id,)).fetchall()
    return jsonable_encoder({
        **dict(run),
        "members": [dict(item) for item in members],
        "eligibility": [dict(item) for item in eligibility],
        "conflicts": [dict(item) for item in conflicts],
        "correlations": [dict(item) for item in correlations],
        "snapshot": dict(snapshot) if snapshot else None,
        "activation_attempts": [dict(item) for item in attempts],
        "detail_limits": {"conflicts": 500, "correlations": 500, "full_decision_inputs_persisted": True},
        "execution_notice": "Internal simulation only. Broker order submission remains disabled.",
    })


def approve_run(conn: psycopg.Connection, run_id: int, requested_snapshot_hash: str) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM elite_portfolio_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        raise PortfolioNotFound("elite portfolio run not found")
    if run["status"] not in {"review_ready", "stale"}:
        raise PortfolioStateError("only a review-ready portfolio can be approved")
    if requested_snapshot_hash != run.get("snapshot_hash"):
        _mark_stale(conn, run_id)
        raise PortfolioStale("requested snapshot does not match the review snapshot")
    current = preview_from_database(conn, dict(run.get("source_configuration") or {}), use_cache=False)
    if current["snapshot"]["decision_hash"] != run.get("snapshot_hash"):
        _mark_stale(conn, run_id)
        raise PortfolioStale("portfolio evidence changed; recalculate before approval")
    conn.execute(
        "UPDATE elite_portfolio_runs SET status='approved', approved_snapshot_hash=snapshot_hash, approved_at=NOW(), updated_at=NOW() WHERE id=%s",
        (run_id,),
    )
    conn.execute("UPDATE elite_portfolio_members SET activation_state='approved', updated_at=NOW() WHERE portfolio_run_id=%s", (run_id,))
    conn.commit()
    return get_run(conn, run_id)


def recalculate_run(conn: psycopg.Connection, run_id: int) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM elite_portfolio_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
    if not run:
        raise PortfolioNotFound("elite portfolio run not found")
    if run["status"] in {"approved", "activated_internal"}:
        raise PortfolioStateError("approved or activated portfolios cannot be superseded by recalculation")
    conn.execute("UPDATE elite_portfolio_runs SET status='superseded', updated_at=NOW() WHERE id=%s", (run_id,))
    conn.commit()
    return create_run(conn, dict(run.get("source_configuration") or {}))


def _mark_stale(conn: psycopg.Connection, run_id: int) -> None:
    conn.execute("UPDATE elite_portfolio_runs SET status='stale', updated_at=NOW() WHERE id=%s", (run_id,))
    conn.commit()


def _persist_eligibility(conn: psycopg.Connection, run_id: int, decisions: list[dict[str, Any]], candidates: dict[str, dict[str, Any]]) -> None:
    for decision in decisions:
        candidate = candidates[decision["candidate_key"]]
        conn.execute(
            """
            INSERT INTO elite_portfolio_eligibility(
                portfolio_run_id, elite_candidate_id, candidate_id, campaign_id, symbol, timeframe,
                strategy_family, strategy_direction, execution_capability, eligible,
                health_classification, checks, evidence
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, candidate.get("elite_id"), candidate["candidate_id"], candidate.get("campaign_id"), candidate["symbol"],
                candidate["timeframe"], candidate["family_id"], candidate["strategy_direction"], candidate["execution_capability"],
                decision["eligible"], candidate["health"], Jsonb(decision), Jsonb(candidate),
            ),
        )


def _persist_correlations(conn: psycopg.Connection, run_id: int, correlations: list[dict[str, Any]]) -> None:
    for row in correlations:
        conn.execute(
            """
            INSERT INTO elite_portfolio_correlations(
                portfolio_run_id, left_candidate_key, right_candidate_key, correlation_type, coefficient,
                observation_count, confidence_classification, method, return_frequency, window_start,
                window_end, data_snapshot_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, row["left_candidate_id"], row["right_candidate_id"],
                "signal_behavior" if row["correlation_type"] == "signal" else row["correlation_type"],
                row["coefficient"], row["observation_count"], row["confidence"], row["method"], row["return_frequency"],
                _timestamp_or_none(row.get("window_start")), _timestamp_or_none(row.get("window_end")), row["data_snapshot_hash"],
            ),
        )


def _persist_conflicts(conn: psycopg.Connection, run_id: int, conflicts: list[dict[str, Any]]) -> None:
    for row in conflicts:
        conn.execute(
            """
            INSERT INTO elite_portfolio_conflicts(
                portfolio_run_id, left_candidate_key, right_candidate_key, conflict_type,
                hard_conflict, reason, evidence
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, row["left_candidate_id"], row.get("right_candidate_id"), row["conflict_type"], row.get("hard_conflict", True), row["conflict_type"].replace("_", " ").title(), Jsonb(row.get("evidence") or {})),
        )


def _persist_members(conn: psycopg.Connection, run_id: int, selected: list[str], candidates: dict[str, dict[str, Any]]) -> None:
    for rank, key in enumerate(selected, start=1):
        candidate = candidates[key]
        conn.execute(
            """
            INSERT INTO elite_portfolio_members(
                portfolio_run_id, elite_candidate_id, campaign_id, candidate_id, symbol, timeframe,
                strategy_family, strategy_direction, execution_capability, rank, objective_score,
                quality_score, evidence, selection_reasons
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id, candidate.get("elite_id"), candidate.get("campaign_id"), candidate["candidate_id"], candidate["symbol"],
                candidate["timeframe"], candidate["family_id"], candidate["strategy_direction"], candidate["execution_capability"],
                rank, float(candidate.get("quality_score") or 0), float(candidate.get("quality_score") or 0), Jsonb(candidate),
                Jsonb(["Selected by deterministic objective hierarchy."]),
            ),
        )


def _statistics(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "eligible_count": result["eligible_count"],
        "excluded_count": result["excluded_count"],
        "selected_count": len(result["selected"]),
        "conflict_count": len(result["conflicts"]),
        "conflict_count_by_type": result["conflict_count_by_type"],
        "timing": result["timing"],
        "response_size_bytes": result["response_size_bytes"],
        "peak_memory_mb": result["peak_memory_mb"],
        "constraint_relaxation_count": 0,
        "binding_constraints": result["binding_constraints"],
    }


def _timestamp_or_none(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00").split("#", 1)[0])
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _dataset_ids(candidate: dict[str, Any], result: dict[str, Any]) -> set[str]:
    values = set()
    for source in (candidate, result):
        for key in ("dataset_id", "dataset_snapshot_id", "snapshot_id"):
            if source.get(key) is not None:
                values.add(str(source[key]))
    return values


def candidate_evidence_version(conn: psycopg.Connection) -> dict[str, Any]:
    """Small, deterministic cache validator without transferring evidence JSON."""
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS variant_count,
            (SELECT MAX(id) FROM elite_candidate_correlation_evidence) AS correlation_evidence_id,
            (SELECT MAX(id) FROM elite_shadow_replay_outcomes) AS replay_outcome_id,
            MD5(COALESCE(STRING_AGG(
                MD5(CONCAT_WS('|',
                    e.id::text,e.candidate_id,e.research_score::text,e.profit_factor::text,
                    e.expectancy::text,e.max_drawdown::text,e.trade_count::text,e.stability::text,
                    e.assets_passed::text,e.timeframes_passed::text,e.regimes_passed::text,
                    e.strategy_direction,e.execution_capability,e.forward_validation_state,
                    MD5(e.validation_history::text),MD5(e.paper_performance::text),
                    j.id::text,j.symbol,j.timeframe,j.validation_score::text,j.consistency_score::text,
                    MD5(j.candidate::text),MD5(j.result::text)
                )), '' ORDER BY e.id,j.id
            ), '')) AS evidence_digest
        FROM elite_research_candidates e
        JOIN research_campaign_jobs j
          ON j.campaign_id=e.campaign_id AND j.candidate_id=e.candidate_id
         AND j.status IN ('completed','promoted') AND j.simulation_only=TRUE
        WHERE e.simulation_only=TRUE
        """
    ).fetchone() or {}
    return {
        "variant_count": int(row.get("variant_count") or 0),
        "evidence_digest": row.get("evidence_digest"),
        "correlation_evidence_id": row.get("correlation_evidence_id"),
        "replay_outcome_id": row.get("replay_outcome_id"),
    }
