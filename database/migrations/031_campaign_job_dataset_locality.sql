CREATE INDEX IF NOT EXISTS research_campaign_jobs_dataset_claim_idx
    ON research_campaign_jobs(campaign_id, status, symbol, timeframe, id);
