from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import get_connection
from app.services.elite_portfolio_repository import (
    PortfolioNotFound,
    PortfolioStale,
    PortfolioStateError,
    approve_run,
    backfill_correlation_evidence,
    create_paper_lab_run,
    create_run,
    get_run,
    options,
    paper_lab_preview_from_database,
    preview_from_database,
    list_runs,
    recalculate_run,
    recommend_profile_from_database,
)
from app.services.elite_portfolio_activation import PortfolioActivationError, activate_internal
from app.services.elite_portfolio_operations import (
    PortfolioOperationError,
    approve_all_members_for_alpaca_paper,
    approve_member_external_paper,
    enable_all_ready_members_paper_execution,
    enable_member_paper_execution,
    execution_preflight,
    portfolio_activation_view,
)
from app.services.champion_validation import (
    DEFAULT_RUN_BUDGET_SECONDS,
    ChampionValidationError,
    champion_validation_diagnostics,
    champion_validation_queue,
    champion_validation_run,
    run_champion_validation,
)
from app.services.research_champion_import import dedupe_research_champions, import_research_champions, research_champion_status
from app.settings import settings


router = APIRouter(prefix="/research/elite-portfolios", tags=["elite-portfolios"])


class PortfolioConfiguration(BaseModel):
    # Portfolio shape preset. Presets change how many members and how far
    # spread they must be; they never touch a quality threshold, a correlation
    # limit, or the parameter-similarity rule -- `normalized_configuration`
    # rejects any configuration that tries.
    profile: str | None = None
    universe: list[str] = Field(default_factory=list)
    families: list[str] = Field(default_factory=list)
    directions: list[str] = Field(default_factory=lambda: ["long", "short"])
    timeframes: list[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    objective: str = "balanced"
    custom_size: int | None = Field(default=None, ge=1, le=20)


class ChampionValidationRequest(BaseModel):
    # The queue query is already bounded to champions in an eligible
    # validation_state, so a caller can request "all of them" via a large
    # limit without needing to know the exact queue size up front. Each
    # champion runs a full battery of backtests, so this is deliberately the
    # slowest endpoint on the page -- see the timeout notes in lib/api.ts and
    # deploy/production/nginx/keftrade.conf.
    limit: int = Field(default=5, ge=1, le=2000)
    elite_candidate_ids: list[int] = Field(default_factory=list)
    # Overrides may only tighten a gate. `run_champion_validation` rejects any
    # value looser than the shipped default rather than quietly accepting it.
    threshold_overrides: dict[str, Any] = Field(default_factory=dict)
    revalidate: bool = False
    require_frozen_datasets: bool = False
    # Wall-clock ceiling for one call. The response reports `remaining` and
    # `budget_exhausted` so a caller draining a large queue calls repeatedly
    # instead of holding one request open past the proxy timeout.
    max_runtime_seconds: float = Field(default=DEFAULT_RUN_BUDGET_SECONDS, ge=30.0, le=3000.0)


class MemberApprovalRequest(BaseModel):
    actor: str | None = None
    reapprove: bool = False


class MemberExecutionRequest(BaseModel):
    # Mirrors the CLI's --confirm-deployment-id. The last approval before real
    # orders reach a broker is deliberately not a single unguarded click.
    confirm_member_id: int
    actor: str | None = None


class BulkApprovalRequest(BaseModel):
    actor: str | None = None
    reapprove: bool = False
    # Must repeat the path's portfolio_id: a bulk action touches every member
    # of a run at once, so it gets the same explicit-confirmation treatment as
    # a single execution-enable click, not less.
    confirm_portfolio_run_id: int


class BulkExecutionRequest(BaseModel):
    actor: str | None = None
    confirm_portfolio_run_id: int


class ApprovalRequest(BaseModel):
    snapshot_hash: str = Field(min_length=64, max_length=64)


class ActivationRequest(BaseModel):
    snapshot_hash: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=200)


@router.get("/options")
def get_options(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return options(conn)


@router.get("/research-champions/status")
def get_research_champion_status(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return research_champion_status(conn)


@router.post("/research-champions/import")
def import_research_champions_endpoint(
    max_champions: int = Query(25, ge=1, le=5000),
    min_profit_factor: float = Query(1.25, ge=1.0, le=10.0),
    min_trades: int = Query(30, ge=1, le=1000),
    max_drawdown: float = Query(0.12, ge=0.0, le=1.0),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return import_research_champions(
        conn,
        max_champions=max_champions,
        min_profit_factor=min_profit_factor,
        min_trades=min_trades,
        max_drawdown=max_drawdown,
    )


@router.post("/research-champions/dedupe")
def dedupe_research_champions_endpoint(
    dry_run: bool = Query(False),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return dedupe_research_champions(conn, dry_run=dry_run)


@router.get("/champion-validation/queue")
def get_champion_validation_queue(
    limit: int = Query(25, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return champion_validation_queue(conn, limit=limit)


@router.get("/champion-validation/diagnostics")
def get_champion_validation_diagnostics(
    limit: int = Query(25, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    return champion_validation_diagnostics(conn, limit=limit)


@router.post("/champion-validation/run")
def run_champion_validation_endpoint(
    payload: ChampionValidationRequest | None = None,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    _require_builder()
    request = payload or ChampionValidationRequest()
    try:
        return run_champion_validation(
            conn,
            limit=request.limit,
            elite_candidate_ids=request.elite_candidate_ids or None,
            threshold_overrides=request.threshold_overrides or None,
            revalidate=request.revalidate,
            require_frozen=request.require_frozen_datasets,
            max_runtime_seconds=request.max_runtime_seconds,
        )
    except ValueError as error:
        # Weakened or unknown thresholds: a rejected request, never a silent
        # fallback to the shipped defaults.
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/champion-validation/runs/{run_id}")
def get_champion_validation_run(run_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    try:
        return champion_validation_run(conn, run_id)
    except ChampionValidationError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/runs")
def get_portfolio_runs(
    limit: int = Query(20, ge=1, le=100),
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Recent portfolio runs plus which one Step 04 should open.

    Declared before `/{portfolio_id}` so "runs" is matched as a literal path
    rather than parsed as an id.
    """
    _require_builder()
    return list_runs(conn, limit=limit)


@router.get("/profile-recommendation")
def get_profile_recommendation(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """The strictest portfolio profile that is actually feasible right now."""
    _require_builder()
    return recommend_profile_from_database(conn)


@router.post("/preview")
def preview_portfolio(payload: PortfolioConfiguration, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate_configuration(lambda: preview_from_database(conn, payload.model_dump()))


@router.post("")
def persist_portfolio(payload: PortfolioConfiguration, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate_configuration(lambda: create_run(conn, payload.model_dump()))


@router.get("/paper-lab/preview")
def preview_paper_lab(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Every deployable validated elite, with no diversity or correlation gate.

    Read-only. This is an execution-testing lab, not a diversified portfolio --
    see `response["warning"]`. Takes no configuration: there is nothing to
    configure, since nothing is excluded except on the eligibility grounds in
    `response["rejection_explanations"]`.
    """
    _require_builder()
    return paper_lab_preview_from_database(conn)


@router.post("/paper-lab")
def persist_paper_lab_run(conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Save the current 'All Validated Elites Paper Lab' set as an immutable run."""
    _require_builder()
    return _translate_configuration(lambda: create_paper_lab_run(conn))


@router.post("/{portfolio_id}/members/approve-all-external-paper")
def approve_all_members_endpoint(
    portfolio_id: int,
    payload: BulkApprovalRequest,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Approve every eligible member of a run for Alpaca Paper, observe-only.

    Calls `approve_member_external_paper` (and therefore `enable_observe_only`)
    once per member; a member that fails does not block the rest, and a member
    already approved is reported as skipped, not as an error. Submits no orders.
    """
    _require_builder()
    if payload.confirm_portfolio_run_id != portfolio_id:
        raise HTTPException(status_code=422, detail="confirm_portfolio_run_id must exactly match the portfolio run id")
    return _translate_operation(
        lambda: approve_all_members_for_alpaca_paper(conn, portfolio_id, actor=payload.actor, reapprove=payload.reapprove)
    )


@router.post("/{portfolio_id}/members/enable-all-paper-execution")
def enable_all_ready_members_endpoint(
    portfolio_id: int,
    payload: BulkExecutionRequest,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Enable Alpaca Paper execution for every member whose full preflight passes.

    A member with any outstanding preflight check is left completely
    unchanged and reported as blocked, never partially enabled.
    """
    _require_builder()
    if payload.confirm_portfolio_run_id != portfolio_id:
        raise HTTPException(status_code=422, detail="confirm_portfolio_run_id must exactly match the portfolio run id")
    return _translate_operation(
        lambda: enable_all_ready_members_paper_execution(conn, portfolio_id, actor=payload.actor)
    )


@router.post("/evidence/backfill")
def backfill_portfolio_evidence(limit: int = 20, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    return backfill_correlation_evidence(conn, limit=limit)


@router.get("/{portfolio_id}")
def portfolio_detail(portfolio_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate(lambda: get_run(conn, portfolio_id))


@router.post("/{portfolio_id}/recalculate")
def recalculate_portfolio(portfolio_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate(lambda: recalculate_run(conn, portfolio_id))


@router.post("/{portfolio_id}/approve")
def approve_portfolio(portfolio_id: int, payload: ApprovalRequest, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    return _translate(lambda: approve_run(conn, portfolio_id, payload.snapshot_hash))


@router.post("/{portfolio_id}/activate-internal")
def activate_portfolio(portfolio_id: int, payload: ActivationRequest, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    _require_builder()
    if not settings.elite_portfolio_activation_enabled:
        raise HTTPException(status_code=503, detail="elite portfolio internal activation is disabled")
    try:
        return activate_internal(conn, portfolio_id, payload.idempotency_key, payload.snapshot_hash)
    except PortfolioActivationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/{portfolio_id}/activation")
def portfolio_activation(portfolio_id: int, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    """Step 04 in one read: members, deployment states, preflight, safety panel."""
    _require_builder()
    return _translate_operation(lambda: portfolio_activation_view(conn, portfolio_id))


@router.get("/{portfolio_id}/members/{member_id}/preflight")
def member_execution_preflight(
    portfolio_id: int,
    member_id: int,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Read-only: every condition `enable_paper_execution` will check."""
    _require_builder()

    def read() -> dict[str, Any]:
        member = conn.execute(
            "SELECT external_deployment_id FROM elite_portfolio_members WHERE id=%s AND portfolio_run_id=%s",
            (member_id, portfolio_id),
        ).fetchone()
        if not member:
            raise PortfolioOperationError("portfolio member not found")
        if not member["external_deployment_id"]:
            raise PortfolioOperationError("this member has no external deployment yet")
        return execution_preflight(conn, int(member["external_deployment_id"]))

    return _translate_operation(read)


@router.post("/{portfolio_id}/members/{member_id}/approve-external-paper")
def approve_member_for_alpaca_paper(
    portfolio_id: int,
    member_id: int,
    payload: MemberApprovalRequest | None = None,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Approve one member for Alpaca Paper, observe-only.

    Calls the same service function as
    `python -m app.cli.deployments enable-external-paper`, so the guards, audit
    trail, frozen configuration and epoch are identical. Submits no orders.
    """
    _require_builder()
    request = payload or MemberApprovalRequest()
    return _translate_operation(
        lambda: approve_member_external_paper(
            conn, portfolio_id, member_id, actor=request.actor, reapprove=request.reapprove
        )
    )


@router.post("/{portfolio_id}/members/{member_id}/enable-paper-execution")
def enable_member_execution(
    portfolio_id: int,
    member_id: int,
    payload: MemberExecutionRequest,
    conn: psycopg.Connection = Depends(get_connection),
) -> dict[str, Any]:
    """Authorise Alpaca Paper order submission for one approved member.

    The confirmation field must repeat the member id, mirroring the CLI's
    `--confirm-deployment-id`: this is the last approval before real orders
    reach a broker, so it is deliberately not a single unguarded click.
    """
    _require_builder()
    if payload.confirm_member_id != member_id:
        raise HTTPException(status_code=422, detail="confirm_member_id must exactly match the member id")
    return _translate_operation(
        lambda: enable_member_paper_execution(conn, portfolio_id, member_id, actor=payload.actor)
    )


def _require_builder() -> None:
    if not settings.elite_portfolio_builder_enabled:
        raise HTTPException(status_code=503, detail="elite portfolio builder is disabled")


def _translate_configuration(operation):
    """A rejected configuration is a 422, not a 500.

    `normalized_configuration` raises when a caller tries to weaken a protected
    constraint or names an unknown profile.
    """
    try:
        return operation()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _translate_operation(operation):
    try:
        return operation()
    except PortfolioOperationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        # Raised by the underlying external-execution guards (missing sync,
        # dirty reconciliation, disabled flags). Surfaced verbatim so the page
        # can show the real reason instead of "something went wrong".
        raise HTTPException(status_code=409, detail=str(error)) from error


def _translate(operation):
    try:
        return operation()
    except PortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PortfolioStale as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PortfolioStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
