from contextlib import asynccontextmanager
import time
import traceback
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db import connect
from app.observability import (
    configure_logging,
    cpu_usage_seconds,
    current_request_id,
    elapsed_ms,
    git_commit,
    log_event,
    log_exception,
    memory_usage_mb,
    new_request_id,
    reset_request_context,
    set_request_context,
)
from app.routers import alpha, backtests, broker, data, diagnostics, features, paper, regimes, research, research_copilot, research_intelligence, research_lab, risk, signals, symbols, validation
from app.settings import cors_origin_list, settings
from app.services.paper_scheduler import start_scheduler, stop_scheduler
from app.services.shared_cache import invalidate_summary_cache

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = time.perf_counter()
    log_event("Startup started", environment=settings.environment, git_commit=git_commit(), log_level=settings.log_level)
    if settings.broker_order_submission_enabled != settings.external_paper_execution_enabled:
        raise RuntimeError("broker order submission and external paper execution flags must be enabled or disabled together")
    if settings.model_risk_authority == "bounded_paper" and not (settings.broker_order_submission_enabled and settings.external_paper_execution_enabled):
        raise RuntimeError("bounded model paper authority requires both broker execution flags")
    try:
        conn = connect()
        try:
            before = time.perf_counter()
            conn.execute("SELECT 1")
            log_event("Database connected", elapsed_ms=elapsed_ms(before), backend_pid=conn.info.backend_pid)
        finally:
            conn.close()
            log_event("Connection released", source="startup")
    except Exception as exc:
        log_exception("Startup database check failed", exc)
        raise
    log_event("Providers initialized", providers="binance_dev,yfinance_research,alpaca_iex")
    log_event("Routes registered", route_count=len(app.routes))
    start_scheduler()
    log_event("Startup complete", elapsed_ms=elapsed_ms(started), memory_mb=memory_usage_mb(), cpu_seconds=cpu_usage_seconds())
    try:
        yield
    finally:
        shutdown_started = time.perf_counter()
        log_event("Shutdown started")
        await stop_scheduler()
        log_event("Shutdown complete", elapsed_ms=elapsed_ms(shutdown_started))


app = FastAPI(title="KefTrade API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)


def apply_error_cors_headers(request: Request, response: Response) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed_origins = cors_origin_list()
    if "*" not in allowed_origins and origin not in allowed_origins:
        return
    response.headers["Access-Control-Allow-Origin"] = origin if "*" in allowed_origins else origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next: Callable[[Request], Response]) -> Response:
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    tokens = set_request_context(request_id, request.method, request.url.path)
    started = time.perf_counter()
    sanitized_headers = {key: value for key, value in request.headers.items() if key.lower() != "authorization"}
    body_size = int(request.headers.get("content-length") or 0)
    log_event("Incoming request", headers=sanitized_headers, body_size=body_size)
    try:
        response = await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and response.status_code < 400:
            invalidate_summary_cache()
        response.headers["X-Request-ID"] = request_id
        log_event("Request finished", status_code=response.status_code, elapsed_ms=elapsed_ms(started), success=response.status_code < 500)
        return response
    except Exception as exc:
        log_exception("Unhandled request exception", exc, elapsed_ms=elapsed_ms(started), stack_trace=traceback.format_exc())
        response = JSONResponse(status_code=500, content={"detail": "Internal Server Error", "request_id": request_id})
        response.headers["X-Request-ID"] = request_id
        apply_error_cors_headers(request, response)
        return response
    finally:
        reset_request_context(tokens)


app.include_router(symbols.router)
app.include_router(data.router)
app.include_router(features.router)
app.include_router(regimes.router)
app.include_router(signals.router)
app.include_router(backtests.router)
app.include_router(research.router)
app.include_router(research_lab.router)
app.include_router(research_intelligence.router)
app.include_router(research_copilot.router)
app.include_router(alpha.router)
app.include_router(validation.router)
app.include_router(risk.router)
app.include_router(paper.router)
app.include_router(broker.router)
app.include_router(diagnostics.router)


@app.get("/health")
def health() -> dict[str, str]:
    started = time.perf_counter()
    try:
        conn = connect()
        try:
            db_started = time.perf_counter()
            conn.execute("SELECT 1")
            database_latency_ms = elapsed_ms(db_started)
        finally:
            conn.close()
        log_event("Health check finished", status="ok", database_latency_ms=database_latency_ms, provider_latency_ms=0, queue_length=0, memory_mb=memory_usage_mb(), cpu_seconds=cpu_usage_seconds(), elapsed_ms=elapsed_ms(started))
        return {"status": "ok"}
    except Exception as exc:
        log_exception("Health check failed", exc, elapsed_ms=elapsed_ms(started), memory_mb=memory_usage_mb(), cpu_seconds=cpu_usage_seconds())
        raise
