-- 057_signal_diagnostics.sql
-- Pre-simulation signal diagnostics (the IC step).
--
-- Why this exists: a campaign was the FIRST test an idea received -- 2,000 jobs
-- of full trading simulation returning one P&L number that conflates signal,
-- exits, sizing and costs. When it failed, nothing identified which part
-- failed, so the only available move was to run it again. One family reached
-- 2,233 validation batteries and zero elites that way.
--
-- These rows record the cheap prior test: does the signal predict anything at
-- all, measured with no stops, targets or position sizing, net of the
-- unconditional drift over the same horizon, and compared against the
-- round-trip cost. A family whose excess edge is indistinguishable from timing
-- luck does not need a campaign to confirm it.
--
-- Keyed by (architecture, timeframe, dataset_id) because the answer is a
-- property of a signal on a specific immutable snapshot; a new snapshot is a
-- new measurement rather than an overwrite of the old one.
--
-- Additive and idempotent: the migrate job re-applies every file on every
-- deploy. Nothing here alters an existing table, changes any gate, or promotes
-- anything.

CREATE TABLE IF NOT EXISTS research_signal_diagnostics (
    id BIGSERIAL PRIMARY KEY,
    architecture TEXT NOT NULL,
    family_name TEXT,
    timeframe TEXT NOT NULL,
    dataset_id BIGINT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL,
    best_horizon_bars INTEGER,
    excess_edge_bps DOUBLE PRECISION,
    t_statistic DOUBLE PRECISION,
    round_trip_cost_bps DOUBLE PRECISION NOT NULL,
    clears_cost BOOLEAN NOT NULL DEFAULT FALSE,
    signal_count INTEGER NOT NULL DEFAULT 0,
    symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    horizons JSONB NOT NULL DEFAULT '[]'::jsonb,
    report JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_signal_diagnostics_unique UNIQUE (architecture, timeframe, dataset_id)
);

CREATE INDEX IF NOT EXISTS research_signal_diagnostics_verdict_idx
    ON research_signal_diagnostics(verdict);
