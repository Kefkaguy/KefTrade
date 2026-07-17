from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import logging
import os
import subprocess
import time
import traceback
from typing import Any, Callable
from uuid import uuid4

import psycopg
from psycopg import errors
try:
    import resource
except ImportError:  # pragma: no cover - Windows local development fallback
    resource = None


request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
request_path_var: ContextVar[str] = ContextVar("request_path", default="-")
request_method_var: ContextVar[str] = ContextVar("request_method", default="-")

LOG_LEVEL = os.getenv("KEFTRADE_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")).upper()
DIAGNOSTIC_LOGGING = os.getenv("KEFTRADE_DIAGNOSTIC_LOGGING", "true").lower() in {"1", "true", "yes", "on"}
SLOW_QUERY_MS = float(os.getenv("KEFTRADE_SLOW_QUERY_MS", "250"))


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.endpoint = request_path_var.get()
        record.method = request_method_var.get()
        return True


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [RID=%(request_id)s] %(name)s %(method)s %(endpoint)s %(message)s",
    )
    root = logging.getLogger()
    if not any(isinstance(item, RequestContextFilter) for item in root.filters):
        root.addFilter(RequestContextFilter())
    for handler in root.handlers:
        if not any(isinstance(item, RequestContextFilter) for item in handler.filters):
            handler.addFilter(RequestContextFilter())


logger = logging.getLogger("keftrade.diagnostics")


def new_request_id() -> str:
    return str(uuid4())


def current_request_id() -> str:
    return request_id_var.get()


def set_request_context(request_id: str, method: str, endpoint: str) -> tuple[Any, Any, Any]:
    return (
        request_id_var.set(request_id),
        request_method_var.set(method),
        request_path_var.set(endpoint),
    )


def reset_request_context(tokens: tuple[Any, Any, Any]) -> None:
    request_id_var.reset(tokens[0])
    request_method_var.reset(tokens[1])
    request_path_var.reset(tokens[2])


def log_event(message: str, **fields: Any) -> None:
    if fields:
        message = f"{message} " + " ".join(f"{key}={safe_log_value(value)}" for key, value in fields.items())
    logger.info(message)


def log_debug(message: str, **fields: Any) -> None:
    if not DIAGNOSTIC_LOGGING:
        return
    if fields:
        message = f"{message} " + " ".join(f"{key}={safe_log_value(value)}" for key, value in fields.items())
    logger.debug(message)


def log_exception(message: str, exc: BaseException, **fields: Any) -> None:
    fields = {
        **fields,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "traceback": traceback.format_exc(),
    }
    message = f"{message} " + " ".join(f"{key}={safe_log_value(value)}" for key, value in fields.items())
    logger.error(message)


def safe_log_value(value: Any) -> str:
    text = str(value).replace("\n", "\\n")
    return text if len(text) <= 2000 else f"{text[:2000]}...[truncated]"


def elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return "unknown"


def memory_usage_mb() -> float:
    if resource is None:
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes. Containers here are Linux, but keep this bounded.
    return round((usage / 1024) if usage > 10_000 else usage, 2)


def cpu_usage_seconds() -> float:
    if resource is None:
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return round(float(usage.ru_utime + usage.ru_stime), 4)


def connection_diagnostics(conn: Any) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for attr in ("closed", "autocommit"):
        try:
            diagnostics[attr] = getattr(conn, attr)
        except Exception:
            pass
    try:
        diagnostics["transaction_status"] = conn.pgconn.transaction_status.name
    except Exception:
        pass
    try:
        diagnostics["connection_status"] = conn.pgconn.status.name
    except Exception:
        pass
    try:
        diagnostics["backend_pid"] = conn.info.backend_pid
    except Exception:
        pass
    return diagnostics


def log_database_exception(exc: BaseException, conn: Any | None = None, *, query: str | None = None, elapsed: int | None = None, rollback_succeeded: bool | None = None, reconnect_attempted: bool = False) -> None:
    if not isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, psycopg.DatabaseError, errors.AdminShutdown, psycopg.IntegrityError)):
        return
    log_exception(
        "PostgreSQL exception",
        exc,
        query=query,
        elapsed_ms=elapsed,
        rollback_succeeded=rollback_succeeded,
        reconnect_attempted=reconnect_attempted,
        **(connection_diagnostics(conn) if conn is not None else {}),
    )


class DiagnosticConnection:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn
        self._transaction_started_at = time.perf_counter()

    def __enter__(self) -> "DiagnosticConnection":
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        return self._conn.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, query: str, params: Any = None, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        query_text = " ".join(str(query).split())
        try:
            result = self._conn.execute(query, params, *args, **kwargs)
            duration = elapsed_ms(started)
            if duration > SLOW_QUERY_MS:
                log_event("Slow query", elapsed_ms=duration, query=query_text)
            else:
                log_debug("Query finished", elapsed_ms=duration, query=query_text)
            return result
        except Exception as exc:
            duration = elapsed_ms(started)
            log_database_exception(exc, self._conn, query=query_text, elapsed=duration)
            raise

    def commit(self) -> None:
        started = time.perf_counter()
        log_event("COMMIT started", **connection_diagnostics(self._conn))
        try:
            self._conn.commit()
            log_event("COMMIT finished", elapsed_ms=elapsed_ms(started), transaction_duration_ms=elapsed_ms(self._transaction_started_at), **connection_diagnostics(self._conn))
            self._transaction_started_at = time.perf_counter()
        except Exception as exc:
            log_database_exception(exc, self._conn, elapsed=elapsed_ms(started))
            raise

    def rollback(self) -> None:
        started = time.perf_counter()
        log_event("ROLLBACK started", **connection_diagnostics(self._conn))
        try:
            self._conn.rollback()
            log_event("ROLLBACK finished", elapsed_ms=elapsed_ms(started), transaction_duration_ms=elapsed_ms(self._transaction_started_at), **connection_diagnostics(self._conn))
            self._transaction_started_at = time.perf_counter()
        except Exception as exc:
            log_database_exception(exc, self._conn, elapsed=elapsed_ms(started), rollback_succeeded=False)
            raise


def with_timing(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            log_event(f"{name} started")
            try:
                result = fn(*args, **kwargs)
                log_event(f"{name} completed", elapsed_ms=elapsed_ms(started))
                return result
            except Exception as exc:
                log_exception(f"{name} exception", exc, elapsed_ms=elapsed_ms(started))
                raise

        return wrapper

    return decorator
