-- 055_nested_research_splits.sql
-- Nested discovery/validation/confirmation splits and the locked confirmation
-- protocol (Phase E).
--
-- Why this exists: the Phase A simulator audit established that `run_backtest`
-- skips its training window and trades only the validation portion, so every
-- row in `research_campaign_trades` carries dataset_split = 'validation' and
-- none carries 'train'. That tag describes one sample, not two, so any check
-- treating it as an out-of-sample comparison compares a set with itself. These
-- tables provide three windows that are genuinely separate.
--
-- `research_dataset_splits` boundaries are immutable and unique per dataset:
-- boundaries that could be recomputed after seeing results would let a
-- disappointing confirmation window be redrawn until it cooperated.
--
-- `research_split_access_log` counts how often each phase influenced a
-- decision. A hold-out stops being a hold-out once you iterate against it, and
-- the number of looks is only knowable if it is written down at the time.
--
-- `research_confirmation_runs` enforces one confirmation per frozen candidate
-- via a UNIQUE fingerprint over (candidate, dataset, parameters, blocks). A
-- confirmation that may be re-run until it passes is a validation set with
-- extra steps.
--
-- Additive and idempotent: the migrate job re-applies every file on every
-- deploy. Nothing here alters an existing table, changes any gate, or promotes
-- anything.

CREATE TABLE IF NOT EXISTS research_dataset_splits (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL UNIQUE,
    discovery_start TIMESTAMPTZ NOT NULL,
    discovery_end TIMESTAMPTZ NOT NULL,
    validation_start TIMESTAMPTZ NOT NULL,
    validation_end TIMESTAMPTZ NOT NULL,
    confirmation_start TIMESTAMPTZ NOT NULL,
    confirmation_end TIMESTAMPTZ NOT NULL,
    split_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_dataset_splits_immutable_check CHECK (immutable = TRUE),
    CONSTRAINT research_dataset_splits_order_check
        CHECK (discovery_end < validation_start AND validation_end < confirmation_start)
);

CREATE TABLE IF NOT EXISTS research_split_access_log (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL,
    phase TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    campaign_id BIGINT,
    candidate_id TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_split_access_phase_check
        CHECK (phase IN ('discovery', 'validation', 'confirmation'))
);

CREATE INDEX IF NOT EXISTS research_split_access_dataset_idx
    ON research_split_access_log(dataset_id, phase);

CREATE TABLE IF NOT EXISTS research_confirmation_runs (
    id BIGSERIAL PRIMARY KEY,
    frozen_fingerprint TEXT NOT NULL UNIQUE,
    campaign_id BIGINT,
    candidate_id TEXT NOT NULL,
    dataset_id BIGINT NOT NULL,
    frozen_spec JSONB NOT NULL,
    metrics JSONB NOT NULL,
    gate_results JSONB NOT NULL,
    passed BOOLEAN NOT NULL,
    effective_trials INTEGER NOT NULL DEFAULT 1,
    protocol_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_confirmation_runs_immutable_check CHECK (immutable = TRUE)
);
