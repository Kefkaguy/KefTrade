from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from app.workers import broker_worker_healthcheck as hc

BASE_ENV = {
    "DATABASE_URL": "postgresql://x",
    "ALPACA_API_KEY": "key",
    "ALPACA_API_SECRET": "secret",
    "BROKER_WORKER_POLL_SECONDS": "60",
}


def row(status: str, age_seconds: float, *, now: datetime) -> dict:
    ts = now - timedelta(seconds=age_seconds)
    return {"status": status, "started_at": ts, "completed_at": ts}


def test_missing_database_url_is_unhealthy() -> None:
    env = {**BASE_ENV, "DATABASE_URL": ""}
    healthy, reason = hc.evaluate_health(env, [])
    assert healthy is False
    assert "DATABASE_URL" in reason


def test_missing_alpaca_credentials_is_unhealthy() -> None:
    for missing in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
        env = {**BASE_ENV, missing: ""}
        healthy, reason = hc.evaluate_health(env, [])
        assert healthy is False
        assert "credentials" in reason


def test_no_cycles_yet_is_healthy_awaiting_first_cycle() -> None:
    healthy, reason = hc.evaluate_health(BASE_ENV, [])
    assert healthy is True
    assert "awaiting first cycle" in reason


def test_fresh_successful_cycle_is_healthy() -> None:
    now = datetime.now(UTC)
    rows = [row("complete", 10, now=now)]
    healthy, reason = hc.evaluate_health(BASE_ENV, rows, now=now)
    assert healthy is True
    assert "complete" in reason


def test_stale_cycle_is_unhealthy() -> None:
    now = datetime.now(UTC)
    # threshold for 60s poll is max(120, 180) = 180s
    rows = [row("complete", 500, now=now)]
    healthy, reason = hc.evaluate_health(BASE_ENV, rows, now=now)
    assert healthy is False
    assert "old" in reason


def test_freshness_threshold_scales_with_poll_interval() -> None:
    assert hc.freshness_threshold_seconds(10) == 120  # floor applies
    assert hc.freshness_threshold_seconds(100) == 300  # 100*3


def test_single_transient_failure_is_tolerated() -> None:
    now = datetime.now(UTC)
    rows = [row("failed", 5, now=now), row("complete", 65, now=now), row("complete", 125, now=now)]
    healthy, reason = hc.evaluate_health(BASE_ENV, rows, now=now)
    assert healthy is True


def test_persistent_failures_are_unhealthy() -> None:
    now = datetime.now(UTC)
    rows = [row("failed", 5, now=now), row("failed", 65, now=now), row("failed", 125, now=now)]
    healthy, reason = hc.evaluate_health(BASE_ENV, rows, now=now)
    assert healthy is False
    assert "failed" in reason


def test_running_status_counts_as_healthy_when_fresh() -> None:
    now = datetime.now(UTC)
    rows = [{"status": "running", "started_at": now - timedelta(seconds=5), "completed_at": None}]
    healthy, reason = hc.evaluate_health(BASE_ENV, rows, now=now)
    assert healthy is True


def test_healthcheck_never_inspects_capability_flags() -> None:
    """Pin the requirement: only the module docstring may mention either
    broker execution flag (as an explanation of what NOT to do); no executable
    line may read or assert a value for them. Both must be legitimately
    settable by environment without affecting the healthcheck's verdict."""
    source = inspect.getsource(hc)
    for flag in ("BROKER_ORDER_SUBMISSION_ENABLED", "EXTERNAL_PAPER_EXECUTION_ENABLED"):
        assert f"env.get(\"{flag}\")" not in source
        assert f"env.get('{flag}')" not in source
        assert f"environ.get(\"{flag}\")" not in source
        assert f"environ.get('{flag}')" not in source


def test_evaluate_health_ignores_capability_flags_present_in_env() -> None:
    now = datetime.now(UTC)
    rows = [row("complete", 10, now=now)]
    for flags in (
        {"BROKER_ORDER_SUBMISSION_ENABLED": "true", "EXTERNAL_PAPER_EXECUTION_ENABLED": "true"},
        {"BROKER_ORDER_SUBMISSION_ENABLED": "false", "EXTERNAL_PAPER_EXECUTION_ENABLED": "false"},
    ):
        healthy, _ = hc.evaluate_health({**BASE_ENV, **flags}, rows, now=now)
        assert healthy is True
