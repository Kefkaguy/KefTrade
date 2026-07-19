ALTER TABLE research_campaign_workers
    ADD COLUMN IF NOT EXISTS campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS current_job_id BIGINT,
    ADD COLUMN IF NOT EXISTS error_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE research_campaign_workers
    DROP CONSTRAINT IF EXISTS research_campaign_workers_status_check;

ALTER TABLE research_campaign_workers
    ADD CONSTRAINT research_campaign_workers_status_check
    CHECK (status IN ('starting', 'running', 'idle', 'draining', 'stopping', 'stopped', 'stale', 'error'));

CREATE INDEX IF NOT EXISTS research_campaign_workers_campaign_live_idx
    ON research_campaign_workers(campaign_id, status, last_heartbeat_at DESC)
    WHERE campaign_id IS NOT NULL;
