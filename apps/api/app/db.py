from collections.abc import Iterator
import time

import psycopg
from psycopg.rows import dict_row

from app.observability import DiagnosticConnection, elapsed_ms, log_database_exception, log_event, log_exception
from app.settings import settings


def get_connection() -> Iterator[DiagnosticConnection]:
    started = time.perf_counter()
    log_event("Connection acquire started")
    with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=3) as raw_conn:
        conn = DiagnosticConnection(raw_conn)
        log_event("Connection acquired", elapsed_ms=elapsed_ms(started), backend_pid=raw_conn.info.backend_pid)
        log_event("BEGIN", backend_pid=raw_conn.info.backend_pid)
        try:
            yield conn
        except Exception as exc:
            rollback_succeeded = False
            try:
                conn.rollback()
                rollback_succeeded = True
            except Exception as rollback_error:
                log_exception("Rollback failed", rollback_error, original_exception=type(exc).__name__)
            log_exception("Request database dependency exception", exc, rollback_succeeded=rollback_succeeded)
            log_database_exception(exc, raw_conn, rollback_succeeded=rollback_succeeded)
            raise
        finally:
            log_event("Connection released", transaction_duration_ms=elapsed_ms(started), backend_pid=raw_conn.info.backend_pid)


def connect() -> DiagnosticConnection:
    started = time.perf_counter()
    log_event("Connection acquire started", source="direct_connect")
    try:
        raw_conn = psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=3)
        log_event("Connection acquired", source="direct_connect", elapsed_ms=elapsed_ms(started), backend_pid=raw_conn.info.backend_pid)
        log_event("BEGIN", source="direct_connect", backend_pid=raw_conn.info.backend_pid)
        return DiagnosticConnection(raw_conn)
    except Exception as exc:
        log_exception("Connection lost or unavailable", exc, reconnect_attempted=False)
        log_database_exception(exc, reconnect_attempted=False)
        raise
