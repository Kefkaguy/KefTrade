-- 040_campaign_repair.sql
-- Phase A campaign reliability repair.
--
-- Adds a distinct terminal job status `blocked_terminal` so a job whose
-- blocker is real but unresolvable within the campaign's lifetime (for
-- example 1h equity data that will not refresh until the market reopens,
-- after the configured retry ceiling is exhausted) can be moved out of the
-- "open" set. Without this, `open_job_count` counts `blocked_data` forever
-- and a campaign can never finalize -- the exact cause of Campaign 33 being
-- stuck at 99%.
--
-- `blocked_terminal` is deliberately NOT in the open-job set used by
-- finalization, and is treated as terminal alongside completed / rejected /
-- promoted / failed / canceled. The blocking reason is preserved in the
-- existing blocked_reason / failure_classification columns for audit.

ALTER TABLE research_campaign_jobs
    DROP CONSTRAINT IF EXISTS research_campaign_jobs_status_check;

ALTER TABLE research_campaign_jobs
    ADD CONSTRAINT research_campaign_jobs_status_check
    CHECK (status IN (
        'queued',
        'running',
        'completed',
        'rejected',
        'promoted',
        'failed',
        'canceled',
        'blocked_data',
        'blocked_terminal',
        'deferred_rate_limit',
        'retrying'
    ));

-- Index to make repair scans (find non-terminal / stale-lease jobs per
-- campaign) cheap even on large campaigns.
CREATE INDEX IF NOT EXISTS research_campaign_jobs_campaign_status_idx
    ON research_campaign_jobs (campaign_id, status);

CREATE INDEX IF NOT EXISTS research_campaign_jobs_lease_idx
    ON research_campaign_jobs (campaign_id, lease_expires_at)
    WHERE status = 'running';
