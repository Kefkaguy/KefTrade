ALTER TABLE research_campaign_workers
    DROP CONSTRAINT IF EXISTS research_campaign_workers_status_check;

ALTER TABLE research_campaign_workers
    ADD CONSTRAINT research_campaign_workers_status_check
    CHECK (status IN ('starting', 'running', 'idle', 'draining', 'stopping', 'stopped', 'stale', 'error'));

CREATE INDEX IF NOT EXISTS research_campaign_workers_campaign_live_idx
    ON research_campaign_workers(campaign_id, status, last_heartbeat_at DESC)
    WHERE campaign_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS research_campaign_jobs_dataset_claim_idx
    ON research_campaign_jobs(campaign_id, status, symbol, timeframe, id);
