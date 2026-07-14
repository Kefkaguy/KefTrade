from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.candidate_lifecycle import METRIC_DEFINITIONS, build_research_portfolio
from app.services.evidence_alerts import detect_research_report_alert
from app.services.promising_research import build_promising_research_candidates
from app.services.production_validation import (
    create_soak_snapshot,
    learning_quality_metrics,
    paper_ledger_reconciliation,
    phase10_readiness_assessment,
    production_validation_status,
    recommendation_outcomes,
    run_data_integrity_audit,
    run_fault_injection_test,
    safety_audit,
    start_validation_campaign,
)
from app.services.features import load_candles
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.research_automation import analyze_research_automation, queue_research_automation, research_automation_status, run_research_automation_batch
from app.services.research_campaigns import (
    blocked_campaign_jobs,
    campaign_control,
    campaign_status,
    create_research_campaign,
    elite_candidate_forward_details,
    generate_campaign_report,
    get_campaign_analytics,
    get_campaign_intelligence,
    get_campaign_report,
    get_campaign_scheduler_status,
    list_campaign_jobs,
    list_research_universes,
    refresh_elite_candidate_forward_evidence,
    retry_campaign_job,
    run_campaign_scheduler_cycle,
    run_research_campaign_batch,
    update_campaign_scheduler,
    update_campaign_scheduling_config,
    upsert_research_universe,
)
from app.services.research_learning import get_learning_table, get_strategy_timeline, learn_from_completed_campaign, research_learning_summary
from app.services.strategy_discovery import discovery_dashboard, evolve_discovered_strategies, generate_discovery_candidates, rule_library_payload, run_strategy_discovery
from app.services.strategy_experiments import list_strategy_experiments, run_strategy_experiment
from app.services.strategy_research import run_strategy_research

router = APIRouter(tags=["strategy-research"])


class ResearchUniversePayload(BaseModel):
    universe_key: str
    name: str
    description: str
    assets: list[str]
    default_timeframes: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CampaignSchedulingPayload(BaseModel):
    mode: str | None = None
    batch_size: int | None = Field(default=None, ge=1, le=250)
    max_jobs_per_cycle: int | None = Field(default=None, ge=1, le=1000)
    max_concurrent_workers: int | None = Field(default=None, ge=1, le=20)
    retry_limit: int | None = Field(default=None, ge=0, le=20)
    retry_backoff_seconds: int | None = Field(default=None, ge=0, le=86400)
    worker_lease_seconds: int | None = Field(default=None, ge=30, le=86400)
    execution_window_utc: dict[str, str] | None = None
    daily_experiment_budget: int | None = Field(default=None, ge=1, le=100000)
    max_concurrent_backtests: int | None = Field(default=None, ge=1, le=100)
    max_concurrent_data_requests: int | None = Field(default=None, ge=1, le=100)
    provider_rate_limits: dict[str, Any] | None = None
    global_daily_job_limit: int | None = Field(default=None, ge=1, le=1000000)
    job_timeout_seconds: int | None = Field(default=None, ge=30, le=86400)
    max_generated_candidates: int | None = Field(default=None, ge=1, le=5000)
    max_database_queue_depth: int | None = Field(default=None, ge=1, le=1000000)


class CampaignSchedulerPayload(BaseModel):
    enabled: bool | None = None
    cadence_seconds: int | None = Field(default=None, ge=30, le=86400)
    global_daily_job_limit: int | None = Field(default=None, ge=1, le=1000000)
    max_concurrent_workers: int | None = Field(default=None, ge=1, le=20)
    max_concurrent_backtests: int | None = Field(default=None, ge=1, le=100)
    max_concurrent_data_requests: int | None = Field(default=None, ge=1, le=100)
    max_database_queue_depth: int | None = Field(default=None, ge=1, le=1000000)
    provider_rate_limits: dict[str, Any] | None = None


class ProductionValidationStartPayload(BaseModel):
    name: str | None = None
    assets: list[str] | None = None
    timeframes: list[str] | None = None
    max_candidates: int | None = Field(default=None, ge=1, le=100000)
    daily_execution_budget: int | None = Field(default=None, ge=1, le=100000)
    universe_version: str | None = None
    strategy_generation_version: str | None = None
    confidence_score_version: str | None = None
    validation_thresholds: dict[str, Any] | None = None
    runtime_environment: dict[str, Any] | None = None
    code_version: str | None = None


class SoakSnapshotPayload(BaseModel):
    validation_run_id: int | None = None
    window_hours: int = Field(default=24, ge=1, le=2160)


class FaultInjectionPayload(BaseModel):
    fault_type: str = Field(default="expired_worker_lease")


class ReadinessAssessmentPayload(BaseModel):
    persist: bool = True
    thresholds: dict[str, Any] | None = None


@router.post("/research/strategies")
def create_strategy_research_report(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    strategy: str | None = Query(None),
    trend_regime: str | None = Query(None),
    volatility_regime: str | None = Query(None),
    trend_strength_bucket: str | None = Query(None),
    outcome: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe)
    regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
    features = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    report = run_strategy_research(
        candles=candles,
        features=list(features),
        regimes=regimes,
        strategy_name=strategy,
        filters={
            "trend_regime": trend_regime or "",
            "volatility_regime": volatility_regime or "",
            "trend_strength_bucket": trend_strength_bucket or "",
            "outcome": outcome or "",
        },
    )
    detect_research_report_alert(conn, symbol, timeframe, report)
    conn.commit()
    return {"symbol": symbol, "timeframe": timeframe, **report}


@router.get("/research/strategy-experiments")
def list_strategy_research_experiments(strategy: str | None = Query(None)) -> list[dict[str, Any]]:
    return list_strategy_experiments(strategy)


@router.get("/research/strategy-experiments/{experiment_id}")
def get_strategy_research_experiment(experiment_id: str) -> dict[str, Any]:
    for experiment in list_strategy_experiments():
        if experiment["id"] == experiment_id:
            return experiment
    raise HTTPException(status_code=404, detail="Strategy experiment not found")


@router.post("/research/strategy-experiments/{experiment_id}")
def run_strategy_research_experiment(
    experiment_id: str,
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    max_runs: int = Query(120, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    sync_market_regimes(conn, symbol=symbol, timeframe=timeframe)
    candles = load_candles(conn, symbol, timeframe)
    regimes = load_regimes(conn, symbol=symbol, timeframe=timeframe)
    features = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (symbol, timeframe),
    ).fetchall()
    report = run_strategy_experiment(
        candles=candles,
        features=list(features),
        regimes=regimes,
        experiment_id=experiment_id,
        max_runs=max_runs,
    )
    return {"symbol": symbol, "timeframe": timeframe, **report}


@router.get("/research/promising-candidates")
def get_promising_research_candidates(
    max_candidates: int = Query(36, ge=1, le=120),
    max_runs_per_experiment: int = Query(8, ge=1, le=40),
    fold_count: int = Query(3, ge=1, le=6),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return build_promising_research_candidates(
        conn,
        max_candidates=max_candidates,
        max_runs_per_experiment=max_runs_per_experiment,
        fold_count=fold_count,
    )


@router.get("/research/portfolio")
def get_research_portfolio(
    max_candidates: int = Query(24, ge=1, le=80),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return build_research_portfolio(conn, max_candidates=max_candidates)


@router.get("/research/metric-definitions")
def get_research_metric_definitions() -> dict[str, Any]:
    return METRIC_DEFINITIONS


@router.post("/research/automation/queue")
def create_research_automation_queue(
    asset_limit: int = Query(100, ge=1, le=1000),
    timeframes: list[str] | None = Query(None),
    max_experiments_per_asset: int = Query(6, ge=1, le=50),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return queue_research_automation(
        conn,
        asset_limit=asset_limit,
        timeframes=timeframes,
        max_experiments_per_asset=max_experiments_per_asset,
    )


@router.post("/research/automation/run")
def run_research_automation(
    batch_size: int = Query(10, ge=1, le=100),
    max_runs_per_experiment: int = Query(24, ge=1, le=250),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_research_automation_batch(
        conn,
        batch_size=batch_size,
        max_runs_per_experiment=max_runs_per_experiment,
    )


@router.get("/research/automation/status")
def get_research_automation_status(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return research_automation_status(conn)


@router.get("/research/automation/analysis")
def get_research_automation_analysis(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return analyze_research_automation(conn)


@router.get("/research/universes")
def get_research_universes(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return list_research_universes(conn)


@router.post("/research/universes")
def save_research_universe(
    payload: ResearchUniversePayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return upsert_research_universe(
        conn,
        universe_key=payload.universe_key,
        name=payload.name,
        description=payload.description,
        assets=payload.assets,
        default_timeframes=payload.default_timeframes,
        metadata=payload.metadata,
    )


@router.post("/research/campaigns")
def create_large_scale_research_campaign(
    universe_key: str = Query("sp500_leaders"),
    name: str | None = Query(None),
    max_candidates: int = Query(1000, ge=1, le=5000),
    asset_limit: int = Query(100, ge=1, le=100),
    timeframes: list[str] | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_research_campaign(
        conn,
        universe_key=universe_key,
        name=name,
        max_candidates=max_candidates,
        asset_limit=asset_limit,
        timeframes=timeframes,
    )


@router.post("/research/campaigns/{campaign_id}/run")
def run_large_scale_research_campaign_batch(
    campaign_id: int,
    batch_size: int = Query(50, ge=1, le=250),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_research_campaign_batch(conn, campaign_id=campaign_id, batch_size=batch_size)


@router.get("/research/campaigns/{campaign_id}")
def get_large_scale_research_campaign(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return campaign_status(conn, campaign_id)


@router.get("/research/campaigns/{campaign_id}/analytics")
def get_large_scale_research_campaign_analytics(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_analytics(conn, campaign_id)


@router.get("/research/campaigns/{campaign_id}/reports")
def get_large_scale_research_campaign_report(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_report(conn, campaign_id)


@router.post("/research/campaigns/{campaign_id}/reports")
def generate_large_scale_research_campaign_report(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    report = generate_campaign_report(conn, campaign_id)
    conn.commit()
    return report


@router.get("/research/campaigns/{campaign_id}/strategy-family-analytics")
def get_large_scale_research_campaign_strategy_family_analytics(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_intelligence(conn, campaign_id, "strategy-family")


@router.get("/research/campaigns/{campaign_id}/asset-analytics")
def get_large_scale_research_campaign_asset_analytics(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_intelligence(conn, campaign_id, "asset")


@router.get("/research/campaigns/{campaign_id}/timeframe-analytics")
def get_large_scale_research_campaign_timeframe_analytics(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_intelligence(conn, campaign_id, "timeframe")


@router.get("/research/campaigns/{campaign_id}/heatmaps")
def get_large_scale_research_campaign_heatmaps(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_intelligence(conn, campaign_id, "heatmaps")


@router.get("/research/campaigns/{campaign_id}/throughput")
def get_large_scale_research_campaign_throughput(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_campaign_intelligence(conn, campaign_id, "throughput")


@router.post("/research/campaigns/{campaign_id}/control")
def control_large_scale_research_campaign(
    campaign_id: int,
    action: str = Query(..., pattern="^(pause|resume|cancel|restart)$"),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return campaign_control(conn, campaign_id=campaign_id, action=action)


@router.put("/research/campaigns/{campaign_id}/scheduling")
def update_large_scale_research_campaign_scheduling(
    campaign_id: int,
    payload: CampaignSchedulingPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return update_campaign_scheduling_config(conn, campaign_id, payload.model_dump(exclude_unset=True))


@router.get("/research/campaigns/{campaign_id}/jobs")
def get_large_scale_research_campaign_jobs(
    campaign_id: int,
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return list_campaign_jobs(conn, campaign_id, status=status, limit=limit)


@router.get("/research/campaigns/{campaign_id}/blocked-jobs")
def get_large_scale_research_campaign_blocked_jobs(
    campaign_id: int,
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return blocked_campaign_jobs(conn, campaign_id, limit=limit)


@router.post("/research/campaigns/{campaign_id}/jobs/{job_id}/retry")
def retry_large_scale_research_campaign_job(
    campaign_id: int,
    job_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return retry_campaign_job(conn, campaign_id, job_id)


@router.get("/research/campaign-scheduler")
def get_large_scale_research_campaign_scheduler(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return get_campaign_scheduler_status(conn)


@router.get("/research/campaign-workers")
def get_large_scale_research_campaign_workers(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return get_campaign_scheduler_status(conn)["workers"]


@router.post("/research/campaigns/{campaign_id}/learn")
def learn_from_large_scale_research_campaign(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    result = learn_from_completed_campaign(conn, campaign_id)
    conn.commit()
    return result


@router.get("/research/learning")
def get_research_learning_summary(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return research_learning_summary(conn)


@router.get("/research/knowledge-base")
def get_research_knowledge_base(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_knowledge_versions", limit)


@router.get("/research/failure-patterns")
def get_research_failure_patterns(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_failure_patterns", limit)


@router.get("/research/success-patterns")
def get_research_success_patterns(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_success_patterns", limit)


@router.get("/research/recommendations")
def get_research_recommendations(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_recommendations", limit)


@router.get("/research/adaptive-campaign-plans")
def get_research_adaptive_campaign_plans(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_campaign_plans", limit)


@router.get("/research/evolution-history")
def get_research_evolution_history(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_evolution_history", limit)


@router.get("/research/confidence-scores")
def get_research_confidence_scores(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_learning_table(conn, "research_confidence_history", limit)


@router.get("/research/strategy-timeline/{strategy_id}")
def get_research_strategy_timeline(
    strategy_id: str,
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return get_strategy_timeline(conn, strategy_id, limit)


@router.post("/research/production-validation/start")
def start_production_validation(
    payload: ProductionValidationStartPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return start_validation_campaign(conn, payload.model_dump(exclude_none=True))


@router.get("/research/production-validation/status")
def get_production_validation_status(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return production_validation_status(conn)


@router.post("/research/production-validation/snapshot")
def create_production_validation_soak_snapshot(
    payload: SoakSnapshotPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_soak_snapshot(conn, validation_run_id=payload.validation_run_id, window_hours=payload.window_hours)


@router.post("/research/production-validation/fault-test")
def create_production_validation_fault_test(
    payload: FaultInjectionPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_fault_injection_test(conn, payload.fault_type)


@router.get("/research/production-validation/integrity-audit")
def get_production_validation_integrity_audit(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return run_data_integrity_audit(conn, persist=False)


@router.get("/research/production-validation/paper-reconciliation")
def get_production_validation_paper_reconciliation(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return paper_ledger_reconciliation(conn, persist=False)


@router.get("/research/production-validation/recommendation-outcomes")
def get_production_validation_recommendation_outcomes(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return recommendation_outcomes(conn, persist=False)


@router.get("/research/production-validation/learning-quality")
def get_production_validation_learning_quality(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return learning_quality_metrics(conn, persist=False)


@router.get("/research/production-validation/safety-audit")
def get_production_validation_safety_audit(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    return safety_audit(conn, persist=False)


@router.post("/research/production-validation/readiness-assessment")
def create_production_validation_readiness_assessment(
    payload: ReadinessAssessmentPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return phase10_readiness_assessment(conn, persist=payload.persist, thresholds=payload.thresholds)


@router.get("/research/production-validation/readiness-history")
def get_production_validation_readiness_history(
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT *
        FROM production_readiness_snapshots
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return {"rows": [dict(row) for row in rows], "simulation_only": True}


@router.put("/research/campaign-scheduler")
def update_large_scale_research_campaign_scheduler(
    payload: CampaignSchedulerPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return update_campaign_scheduler(conn, payload.model_dump(exclude_unset=True))


@router.post("/research/campaign-scheduler/run")
def run_large_scale_research_campaign_scheduler_once(
    force: bool = Query(True),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_campaign_scheduler_cycle(conn, force=force)


@router.post("/research/elite-candidates/forward-evidence")
def refresh_elite_candidate_forward_validation(
    elite_candidate_id: int | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return refresh_elite_candidate_forward_evidence(conn, elite_candidate_id=elite_candidate_id)


@router.get("/research/elite-candidates/{candidate_id}/forward-validation")
def get_elite_candidate_forward_validation(
    candidate_id: str,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return elite_candidate_forward_details(conn, candidate_id)


@router.get("/research/strategy-discovery/rules")
def get_strategy_discovery_rules() -> dict[str, Any]:
    return rule_library_payload()


@router.post("/research/strategy-discovery/generate")
def generate_strategy_discovery_candidates(max_candidates: int = Query(100, ge=1, le=5000)) -> dict[str, Any]:
    candidates = generate_discovery_candidates(max_candidates=max_candidates)
    return {
        "generated": len(candidates),
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "family_id": candidate.family_id,
                "parent_candidate_id": candidate.parent_candidate_id,
                "generation": candidate.generation,
                "blocks": candidate.blocks,
                "parameters": candidate.parameters,
                "complexity": candidate.complexity,
            }
            for candidate in candidates[:250]
        ],
        "truncated": len(candidates) > 250,
        "safety": "Research-only deterministic generation. No order is routed.",
    }


@router.post("/research/strategy-discovery/run")
def run_autonomous_strategy_discovery(
    symbol: str = Query(DEFAULT_DEV_SYMBOL),
    timeframe: str = Query(DEFAULT_DEV_TIMEFRAME),
    max_candidates: int = Query(50, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_strategy_discovery(conn, symbol=symbol, timeframe=timeframe, max_candidates=max_candidates)


@router.post("/research/strategy-discovery/evolve")
def evolve_autonomous_strategy_discovery(
    limit: int = Query(20, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return evolve_discovered_strategies(conn, limit=limit)


@router.get("/research/strategy-discovery/dashboard")
def get_autonomous_strategy_discovery_dashboard(
    limit: int = Query(20, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return discovery_dashboard(conn, limit=limit)
