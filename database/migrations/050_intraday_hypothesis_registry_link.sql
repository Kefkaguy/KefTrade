-- Phase 12.5 Step 1: indexes only. No new tables, no new columns, no
-- application-code changes. Supports the campaign-lineage and
-- specialist-thread queries described in the architecture proposal
-- (sections 2.5, 5, 7) once that application code is written in a later
-- step -- this migration only makes those future lookups fast.

CREATE INDEX IF NOT EXISTS research_campaigns_hypothesis_version_idx
    ON research_campaigns(hypothesis_version_id);

CREATE INDEX IF NOT EXISTS research_campaigns_parent_campaign_idx
    ON research_campaigns(parent_campaign_id);

CREATE INDEX IF NOT EXISTS research_specialist_threads_origin_campaign_idx
    ON research_specialist_threads(origin_campaign_id);

CREATE INDEX IF NOT EXISTS research_specialist_investigations_campaign_idx
    ON research_specialist_investigations(campaign_id);

CREATE INDEX IF NOT EXISTS research_specialist_investigations_dataset_idx
    ON research_specialist_investigations(dataset_id);
