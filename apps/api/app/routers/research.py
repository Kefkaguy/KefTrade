from typing import Any
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
import psycopg

from app.db import get_connection
from app.domain.assets import DEFAULT_DEV_SYMBOL, DEFAULT_DEV_TIMEFRAME
from app.services.candidate_lifecycle import METRIC_DEFINITIONS, build_research_portfolio
from app.services.deeper_observations import create_deeper_observation_hypotheses
from app.services.automated_scientific_reporting import generate_automated_scientific_report
from app.services.evidence_alerts import detect_research_report_alert
from app.services.edge_discovery import run_edge_discovery
from app.services.multi_generation_evolution import create_multi_generation_evolution_campaign
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
from app.services.features import sync_features
from app.observability import elapsed_ms, log_event, log_exception
from app.providers.registry import get_market_data_provider
from app.settings import settings
from app.services.regimes import load_regimes, sync_market_regimes
from app.services.research_automation import analyze_research_automation, queue_research_automation, research_automation_status, run_research_automation_batch
from app.services.research_architecture import (
    create_intelligent_research_campaign,
    export_dataset_bundle,
    persist_campaign_archive,
    research_architecture_state,
    run_autonomous_research_cycle,
    verify_dataset_snapshot,
)
from app.services.research_campaigns import (
    blocked_campaign_jobs,
    campaign_control,
    campaign_status,
    create_quality_first_research_campaign,
    create_overfit_regime_robustness_campaign,
    create_research_campaign,
    create_single_asset_generalization_campaign,
    create_strategy_redesign_campaign,
    create_volatility_adaptive_relative_strength_campaign,
    create_transferability_sample_size_campaign,
    elite_candidate_forward_details,
    get_campaign_analytics,
    get_campaign_intelligence,
    get_campaign_report,
    get_campaign_performance_profile,
    get_campaign_scheduler_status,
    list_campaign_jobs,
    list_research_campaigns,
    list_research_universes,
    MIN_CAMPAIGN_CANDLES,
    refresh_elite_candidate_forward_evidence,
    research_campaign_preflight,
    retry_campaign_job,
    run_campaign_scheduler_cycle,
    run_parallel_campaign_batch,
    run_research_campaign_batch,
    delete_research_campaign,
    update_campaign_scheduler,
    update_campaign_scheduling_config,
    upsert_research_universe,
)
from app.services.research_command_center import candidate_library, candidate_profile, research_command_center
from app.services.research_learning import get_learning_table, get_strategy_timeline, learn_from_completed_campaign, research_learning_summary
from app.services.strategy_discovery import discovery_dashboard, evolve_discovered_strategies, generate_discovery_candidates, rule_library_payload, run_strategy_discovery
from app.services.strategy_experiments import list_strategy_experiments, run_strategy_experiment
from app.services.strategy_research import run_strategy_research

router = APIRouter(tags=["strategy-research"])


@router.get("/research/command-center")
def get_research_command_center(
    campaign_id: int | None = Query(None),
    asset: str | None = Query(None),
    asset_class: str | None = Query(None),
    timeframe: str | None = Query(None),
    strategy_family: str | None = Query(None),
    candidate_state: str | None = Query(None),
    validation_rule: str | None = Query(None),
    regime: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return research_command_center(
        conn,
        campaign_id=campaign_id,
        filters={
            "asset": asset,
            "asset_class": asset_class,
            "timeframe": timeframe,
            "strategy_family": strategy_family,
            "candidate_state": candidate_state,
            "validation_rule": validation_rule,
            "regime": regime,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@router.get("/research/candidates")
def get_persisted_candidate_library(
    search: str | None = Query(None),
    state: str | None = Query(None),
    deployment_status: str | None = Query(None),
    asset: str | None = Query(None),
    family: str | None = Query(None),
    timeframe: str | None = Query(None),
    campaign_id: int | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return candidate_library(
        conn,
        search=search,
        state=state,
        deployment_status=deployment_status,
        asset=asset,
        family=family,
        timeframe=timeframe,
        campaign_id=campaign_id,
        limit=limit,
    )


@router.get("/research/candidates/{candidate_id}")
def get_persisted_candidate_profile(
    candidate_id: str,
    campaign_id: int | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return candidate_profile(conn, candidate_id, campaign_id=campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ResearchUniversePayload(BaseModel):
    universe_key: str
    name: str
    description: str
    assets: list[str]
    default_timeframes: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchCampaignPreflightPayload(BaseModel):
    assets: list[str]
    timeframes: list[str]


class AutonomousResearchCyclePayload(BaseModel):
    universe_key: str = "research_core_ten"
    timeframes: list[str] | None = None
    max_candidates: int = Field(default=250, ge=1, le=5000)
    asset_limit: int = Field(default=10, ge=1, le=100)
    dataset_mode: str = Field(default="rolling", pattern="^(rolling|reproducibility)$")
    approval_mode: str = Field(default="manual", pattern="^(manual|auto_queue)$")


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


@router.get("/research/architecture")
def get_research_architecture(
    dataset_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return research_architecture_state(conn, dataset_id=dataset_id, limit=limit)


@router.post("/research/architecture/cycles")
def create_autonomous_research_cycle(
    payload: AutonomousResearchCyclePayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return run_autonomous_research_cycle(
            conn,
            universe_key=payload.universe_key,
            timeframes=payload.timeframes,
            max_candidates=payload.max_candidates,
            asset_limit=payload.asset_limit,
            dataset_mode=payload.dataset_mode,
            approval_mode=payload.approval_mode,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/research/datasets/{dataset_id}/verify")
def verify_research_dataset(dataset_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return verify_dataset_snapshot(conn, dataset_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/research/datasets/{dataset_id}/export")
def export_research_dataset(dataset_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    try:
        return export_dataset_bundle(conn, dataset_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/research/architecture/edge-discovery")
def create_edge_discovery_hypotheses(
    dataset_id: int | None = Query(None, ge=1),
    max_hypotheses: int = Query(12, ge=1, le=50),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_edge_discovery(conn, dataset_id=dataset_id, max_hypotheses=max_hypotheses)


@router.post("/research/architecture/multi-generation-evolution")
def create_multi_generation_evolution(
    dataset_id: int = Query(1, ge=1),
    validation_dataset_id: int | None = Query(None, ge=1),
    max_parents: int = Query(3, ge=1, le=10),
    children_per_parent: int = Query(4, ge=1, le=12),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return create_multi_generation_evolution_campaign(
            conn,
            dataset_id=dataset_id,
            validation_dataset_id=validation_dataset_id,
            max_parents=max_parents,
            children_per_parent=children_per_parent,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/research/architecture/deeper-observations")
def create_phase5_deeper_observations(
    dataset_id: int = Query(1, ge=1),
    max_hypotheses: int = Query(11, ge=1, le=20),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return create_deeper_observation_hypotheses(conn, dataset_id=dataset_id, max_hypotheses=max_hypotheses)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/research/universes")
def save_research_universe(
    payload: ResearchUniversePayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Universe save started", universe_key=payload.universe_key, assets=len(payload.assets), timeframes=len(payload.default_timeframes))
    try:
        result = upsert_research_universe(
            conn,
            universe_key=payload.universe_key,
            name=payload.name,
            description=payload.description,
            assets=payload.assets,
            default_timeframes=payload.default_timeframes,
            metadata=payload.metadata,
        )
        log_event("Universe save completed", universe_key=payload.universe_key, elapsed_ms=elapsed_ms(started))
        return result
    except Exception as error:
        log_exception("Universe save exception", error, universe_key=payload.universe_key, elapsed_ms=elapsed_ms(started))
        raise


@router.post("/research/campaigns")
def create_large_scale_research_campaign(
    universe_key: str = Query("research_core_ten"),
    name: str | None = Query(None),
    max_candidates: int = Query(250, ge=1, le=5000),
    asset_limit: int = Query(100, ge=1, le=50000),
    timeframes: list[str] | None = Query(None),
    architecture_mode: str = Query("intelligent", pattern="^(intelligent|legacy)$"),
    dataset_mode: str = Query("rolling", pattern="^(rolling|reproducibility)$"),
    dataset_id: int | None = Query(None, ge=1),
    hypothesis_id: int | None = Query(None, ge=1),
    search_mode: str = Query("scout_expand", pattern="^(scout_expand|full)$"),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Campaign creation started", universe_key=universe_key, name=name, max_candidates=max_candidates, asset_limit=asset_limit, timeframes=timeframes, architecture_mode=architecture_mode, dataset_mode=dataset_mode, dataset_id=dataset_id, hypothesis_id=hypothesis_id, search_mode=search_mode)
    if architecture_mode == "intelligent":
        try:
            result = create_intelligent_research_campaign(
                conn,
                universe_key=universe_key,
                name=name,
                max_candidates=max_candidates,
                asset_limit=min(asset_limit, 100),
                timeframes=timeframes,
                dataset_mode=dataset_mode,
                dataset_id=dataset_id,
                hypothesis_id=hypothesis_id,
            )
            log_event("Campaign launch complete", campaign_id=result.get("campaign", {}).get("id"), elapsed_ms=elapsed_ms(started))
            return result
        except ValueError as error:
            log_exception("Campaign creation rejected", error, elapsed_ms=elapsed_ms(started))
            raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        result = create_research_campaign(
            conn,
            universe_key=universe_key,
            name=name,
            max_candidates=max_candidates,
            asset_limit=asset_limit,
            timeframes=timeframes,
            search_mode=search_mode,
        )
        log_event("Campaign launch complete", campaign_id=result.get("campaign", {}).get("id"), elapsed_ms=elapsed_ms(started))
        return result
    except Exception as error:
        log_exception("Campaign creation exception", error, elapsed_ms=elapsed_ms(started))
        raise


@router.get("/research/campaigns")
def get_research_campaigns(
    limit: int = Query(50, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return list_research_campaigns(conn, limit=limit)


@router.post("/research/campaigns/preflight")
def preflight_large_scale_research_campaign(
    payload: ResearchCampaignPreflightPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return research_campaign_preflight(conn, assets=payload.assets, timeframes=payload.timeframes)
    except ValueError as error:
        log_exception("Campaign preflight exception", error, assets=len(payload.assets), timeframes=len(payload.timeframes))
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/research/campaigns/prepare")
async def prepare_large_scale_research_campaign(
    payload: ResearchCampaignPreflightPayload,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    request_started = time.perf_counter()
    prepared: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    initial_readiness = research_campaign_preflight(conn, assets=payload.assets, timeframes=payload.timeframes)
    remediable = {"missing_dataset", "insufficient_historical_depth", "feature_generation_failure", "stale_data"}
    targets = [
        {"symbol": issue["symbol"], "timeframe": issue["timeframe"], "classification": issue["classification"]}
        for issue in initial_readiness.get("issues", [])
        if issue.get("classification") in remediable
    ]
    sync_limit = max(400, MIN_CAMPAIGN_CANDLES * 3)
    for target in targets:
        symbol = str(target["symbol"])
        timeframe = str(target["timeframe"])
        provider_name = "binance_dev" if symbol.upper().endswith("USDT") else "alpaca_iex" if settings.alpaca_api_key and settings.alpaca_api_secret else "yfinance_research"
        provider = get_market_data_provider(provider_name)
        asset_started = time.perf_counter()
        log_event("Starting asset sync", asset=symbol, provider=provider_name, timeframe=timeframe, classification=target["classification"], retry_count=0)
        try:
            download_started = time.perf_counter()
            log_event("Download start", asset=symbol, provider=provider_name, timeframe=timeframe, limit=sync_limit)
            candles = await provider.sync_candles(conn, symbol=symbol, timeframe=timeframe, limit=sync_limit)
            log_event("Download finished", asset=symbol, provider=provider_name, timeframe=timeframe, candles_received=candles.candle_count, elapsed_ms=elapsed_ms(download_started))
            feature_started = time.perf_counter()
            features = sync_features(conn, symbol=symbol, timeframe=timeframe, candle_limit=sync_limit)
            log_event("Features calculated", asset=symbol, provider=provider_name, timeframe=timeframe, features=features["usable"], elapsed_ms=elapsed_ms(feature_started))
            prepared.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "provider": provider_name,
                "candles": candles.candle_count,
                "features": features["usable"],
            })
            log_event("Features committed", asset=symbol, provider=provider_name, timeframe=timeframe, elapsed_ms=elapsed_ms(asset_started))
        except Exception as error:
            log_exception("Asset sync failure", error, asset=symbol, provider=provider_name, timeframe=timeframe, retry_count=0, elapsed_ms=elapsed_ms(asset_started))
            conn.rollback()
            errors.append({"symbol": symbol, "timeframe": timeframe, "reason": str(error)})
    readiness = research_campaign_preflight(conn, assets=payload.assets, timeframes=payload.timeframes)
    log_event("Campaign prepare finished", ready=readiness["ready"], prepared=len(prepared), errors=len(errors), elapsed_ms=elapsed_ms(request_started))
    return {
        "ready": readiness["ready"],
        "initial_readiness": initial_readiness,
        "datasets_considered": len(targets),
        "prepared": prepared,
        "errors": errors,
        "readiness": readiness,
        "simulation_only": True,
    }


@router.post("/research/campaigns/{campaign_id}/quality-follow-up")
def create_quality_first_campaign_follow_up(
    campaign_id: int,
    name: str | None = Query(None),
    max_variants_per_parent: int = Query(6, ge=1, le=12),
    asset_limit: int = Query(4, ge=2, le=10),
    timeframes: list[str] | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_quality_first_research_campaign(
        conn,
        source_campaign_id=campaign_id,
        name=name,
        max_variants_per_parent=max_variants_per_parent,
        asset_limit=asset_limit,
        timeframes=timeframes,
    )


@router.post("/research/campaigns/phase-9-8-transferability")
def create_phase_9_8_transferability_campaign(
    pullback_campaign_id: int = Query(2, ge=1),
    trend_source_campaign_id: int = Query(1, ge=1),
    name: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_transferability_sample_size_campaign(
        conn,
        pullback_campaign_id=pullback_campaign_id,
        trend_source_campaign_id=trend_source_campaign_id,
        name=name,
    )


@router.post("/research/campaigns/phase-9-9-overfit-regime-robustness")
def create_phase_9_9_overfit_regime_robustness_campaign(
    source_campaign_id: int = Query(3, ge=1),
    name: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_overfit_regime_robustness_campaign(
        conn,
        source_campaign_id=source_campaign_id,
        name=name,
    )


@router.post("/research/campaigns/phase-9-10-single-asset-generalization")
def create_phase_9_10_single_asset_generalization_campaign(
    source_campaign_id: int = Query(4, ge=1),
    name: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_single_asset_generalization_campaign(
        conn,
        source_campaign_id=source_campaign_id,
        name=name,
    )


@router.post("/research/campaigns/phase-9-11-strategy-redesign")
def create_phase_9_11_strategy_redesign_campaign(
    source_campaign_id: int = Query(5, ge=1),
    name: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_strategy_redesign_campaign(
        conn,
        source_campaign_id=source_campaign_id,
        name=name,
    )


@router.post("/research/campaigns/phase-9-12-volatility-adaptive-relative-strength")
def create_phase_9_12_volatility_adaptive_relative_strength_campaign(
    source_campaign_id: int = Query(6, ge=1),
    name: str | None = Query(None),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return create_volatility_adaptive_relative_strength_campaign(
        conn,
        source_campaign_id=source_campaign_id,
        name=name,
    )


@router.post("/research/campaigns/{campaign_id}/run")
def run_large_scale_research_campaign_batch(
    campaign_id: int,
    batch_size: int = Query(50, ge=1, le=250),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    return run_research_campaign_batch(conn, campaign_id=campaign_id, batch_size=batch_size)


@router.post("/research/campaigns/{campaign_id}/run-parallel")
def run_large_scale_research_campaign_parallel_batch(
    campaign_id: int,
    workers: int = Query(1, ge=1, le=8),
    jobs_per_worker: int = Query(10, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event("Parallel campaign launch started", campaign_id=campaign_id, workers=workers, jobs_per_worker=jobs_per_worker)
    try:
        result = run_parallel_campaign_batch(conn, campaign_id=campaign_id, workers=workers, jobs_per_worker=jobs_per_worker)
        log_event("Worker dispatch started", campaign_id=campaign_id, started=result.get("started"), already_active=result.get("already_active"), elapsed_ms=elapsed_ms(started))
        return result
    except ValueError as error:
        log_exception("Parallel campaign launch rejected", error, campaign_id=campaign_id, elapsed_ms=elapsed_ms(started))
        raise HTTPException(status_code=409, detail=str(error)) from error


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


@router.get("/research/campaigns/{campaign_id}/profile")
def get_large_scale_research_campaign_profile(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return get_campaign_performance_profile(conn, campaign_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


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
    report = generate_automated_scientific_report(conn, campaign_id)
    conn.commit()
    return report


@router.post("/research/campaigns/{campaign_id}/archive")
def archive_large_scale_research_campaign(
    campaign_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return persist_campaign_archive(conn, campaign_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


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


@router.delete("/research/campaigns/{campaign_id}")
def remove_research_campaign(
    campaign_id: int,
    force: bool = Query(False),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    try:
        return delete_research_campaign(conn, campaign_id, force=force)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


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
