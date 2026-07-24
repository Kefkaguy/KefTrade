-- The Intraday Research Lab overview endpoint filters research_campaign_jobs
-- by candidate->'parameters'->>'strategy_architecture' once per registered
-- family (2 architectures pre-Phase-12.3, 8 after). Without an index on that
-- JSONB expression, each filter is a full sequential scan; with 8 families x
-- ~5 queries each, this made GET /research/intraday/overview take ~17s once
-- research_campaign_jobs grew past a few thousand rows (discovered during
-- the Phase 12.3 multi-family pilot).
CREATE INDEX IF NOT EXISTS research_campaign_jobs_strategy_architecture_idx
    ON research_campaign_jobs ((candidate->'parameters'->>'strategy_architecture'));
