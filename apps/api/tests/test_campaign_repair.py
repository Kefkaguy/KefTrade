from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.research_campaigns import (
    OPEN_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    campaign_repair_plan,
)


NOW = datetime(2026, 7, 23, 5, 0, 0, tzinfo=UTC)
RETRY_LIMIT = 3


def job(job_id, status, *, attempts=0, lease_offset_minutes=None):
    lease = None if lease_offset_minutes is None else NOW + timedelta(minutes=lease_offset_minutes)
    return {"id": job_id, "status": status, "attempts": attempts, "lease_expires_at": lease}


def plan(jobs, *, campaign_status="running", terminalize=True):
    return campaign_repair_plan(
        jobs,
        campaign_status=campaign_status,
        retry_limit=RETRY_LIMIT,
        now=NOW,
        terminalize_exhausted_blocks=terminalize,
    )


def test_open_and_terminal_status_sets_are_disjoint_and_cover_expected_states() -> None:
    assert OPEN_JOB_STATUSES.isdisjoint(TERMINAL_JOB_STATUSES)
    assert "blocked_terminal" in TERMINAL_JOB_STATUSES
    assert "blocked_data" in OPEN_JOB_STATUSES
    assert "blocked_terminal" not in OPEN_JOB_STATUSES


def test_worker_dies_holding_jobs_releases_expired_leases_back_to_queued() -> None:
    jobs = [
        job(1, "running", lease_offset_minutes=-10),  # dead worker, lease expired
        job(2, "running", lease_offset_minutes=+10),  # healthy, lease still valid
        job(3, "running", lease_offset_minutes=None),  # running with no lease -> stale
    ]
    result = plan(jobs)
    assert set(result["release_lease_ids"]) == {1, 3}
    assert 2 not in result["release_lease_ids"]
    # Released jobs are queued (open), so an actively-running campaign is not finalized.
    assert result["finalize"] is False


def test_campaign_at_99_percent_with_exhausted_blocks_terminalizes_and_finalizes() -> None:
    jobs = [job(i, "completed") for i in range(1, 4711)]
    jobs += [job(5000 + i, "blocked_data", attempts=3) for i in range(30)]  # exhausted
    result = plan(jobs, campaign_status="paused")
    assert len(result["terminalize_ids"]) == 30
    assert result["open_after"] == 0
    assert result["finalize"] is True
    assert result["reopen"] is False


def test_blocked_jobs_not_yet_exhausted_are_left_recoverable() -> None:
    jobs = [job(1, "completed"), job(2, "blocked_data", attempts=1)]
    result = plan(jobs)
    assert result["terminalize_ids"] == []
    assert result["open_after"] == 1  # the blocked_data job is still open/recoverable
    assert result["finalize"] is False


def test_keep_blocked_flag_disables_terminalization() -> None:
    jobs = [job(1, "completed"), job(2, "blocked_data", attempts=3)]
    result = plan(jobs, terminalize=False)
    assert result["terminalize_ids"] == []
    assert result["open_after"] == 1
    assert result["finalize"] is False


def test_completed_campaign_with_queued_jobs_is_reopened_not_finalized() -> None:
    # Campaign 34 invariant: marked completed while 480 jobs remain queued.
    jobs = [job(i, "completed") for i in range(1, 935)]
    jobs += [job(2000 + i, "queued") for i in range(480)]
    result = plan(jobs, campaign_status="completed")
    assert result["reopen"] is True
    assert result["finalize"] is False
    assert result["open_after"] == 480


def test_retry_exhaustion_produces_terminal_state_allowing_finalization() -> None:
    jobs = [job(1, "completed"), job(2, "blocked_data", attempts=RETRY_LIMIT)]
    result = plan(jobs, campaign_status="running")
    assert result["terminalize_ids"] == [2]
    assert result["finalize"] is True


def test_finalization_with_mixed_terminal_states_including_blocked_terminal() -> None:
    jobs = [
        job(1, "completed"),
        job(2, "rejected"),
        job(3, "promoted"),
        job(4, "failed"),
        job(5, "canceled"),
        job(6, "blocked_terminal"),
    ]
    result = plan(jobs, campaign_status="running")
    assert result["open_after"] == 0
    assert result["finalize"] is True
    assert result["terminalize_ids"] == []  # already terminal, nothing to do


def test_all_terminal_already_is_idempotent_noop_plan() -> None:
    jobs = [job(1, "completed"), job(2, "rejected")]
    result = plan(jobs, campaign_status="completed")
    assert result["release_lease_ids"] == []
    assert result["terminalize_ids"] == []
    assert result["reopen"] is False
    # Already completed and all terminal: finalize is True but repair application is a no-op.
    assert result["finalize"] is True


def test_cancellation_state_is_terminal_and_not_reopened() -> None:
    jobs = [job(1, "canceled"), job(2, "canceled")]
    result = plan(jobs, campaign_status="canceled")
    assert result["open_after"] == 0
    assert result["reopen"] is False
    assert result["finalize"] is True


def test_plan_is_deterministic_across_repeated_calls() -> None:
    jobs = [
        job(1, "running", lease_offset_minutes=-5),
        job(2, "blocked_data", attempts=3),
        job(3, "queued"),
    ]
    first = plan(jobs, campaign_status="completed")
    second = plan(jobs, campaign_status="completed")
    assert first == second


def test_repair_uses_only_allowed_recovery_classifications() -> None:
    """The DB CHECK constraint permits a fixed set; repair must stay inside it."""
    import pathlib
    import re

    allowed = {
        "recovered_stale_lease",
        "actual_worker_execution_timeout",
        "provider_timeout",
        "database_timeout",
        "permanent_job_failure",
    }
    source = pathlib.Path(__file__).resolve().parents[1].joinpath("app", "services", "research_campaigns.py").read_text(encoding="utf-8")
    start = source.index("def repair_campaign(")
    body = source[start:start + 6000]
    written = set(re.findall(r"recovery_classification = '([a-z_]+)'", body))
    assert written, "repair_campaign should set a recovery_classification"
    assert written <= allowed, f"disallowed recovery_classification(s): {written - allowed}"
