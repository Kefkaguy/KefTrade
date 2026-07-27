-- 056_research_graveyard.sql
-- The research graveyard (Phase F).
--
-- "Rejected" is not a research finding. A family that fails because it has no
-- predictive signal and a family that fails because a real signal is eaten by
-- costs need opposite responses -- retire the first, restructure the second --
-- and until now the pipeline recorded both as the same rejection.
--
-- This table stores the diagnosed cause, the evidence behind it, and the single
-- causal change proposed next, keyed by (architecture, campaign_id) so a later
-- campaign cannot rediscover a dead end that has already been paid for.
--
-- Additive and idempotent: the migrate job re-applies every file on every
-- deploy. Nothing here alters an existing table, changes any gate, or promotes
-- anything.

CREATE TABLE IF NOT EXISTS research_family_graveyard (
    id BIGSERIAL PRIMARY KEY,
    architecture TEXT NOT NULL,
    campaign_id BIGINT,
    failure_reason TEXT NOT NULL,
    confidence TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    decomposition JSONB NOT NULL DEFAULT '{}'::jsonb,
    next_experiment JSONB NOT NULL DEFAULT '{}'::jsonb,
    diagnostics_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_family_graveyard_unique UNIQUE (architecture, campaign_id)
);

CREATE INDEX IF NOT EXISTS research_family_graveyard_reason_idx
    ON research_family_graveyard(failure_reason);
