-- 042_family_registry.sql
-- Strategy-family registry: evidence-based classification and lifecycle.
--
-- The 2026-07-23 library audit showed ~70% of campaign compute was spent on
-- families with no edge (dead, negative-edge, or weak). This registry records
-- each family's audited statistics and classification, and marks families as
-- 'legacy' so candidate generation stops spending compute on them. Legacy is
-- an archive state -- evidence is never deleted, and a family can be
-- reactivated by a future re-audit if new evidence supports it.

CREATE TABLE IF NOT EXISTS research_family_registry (
    family_id TEXT PRIMARY KEY,
    classification TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    classification_version TEXT NOT NULL,
    jobs INTEGER NOT NULL DEFAULT 0,
    candidates INTEGER NOT NULL DEFAULT 0,
    promoted_jobs INTEGER NOT NULL DEFAULT 0,
    elites INTEGER NOT NULL DEFAULT 0,
    median_profit_factor DOUBLE PRECISION,
    avg_win_rate DOUBLE PRECISION,
    avg_drawdown DOUBLE PRECISION,
    avg_trades DOUBLE PRECISION,
    avg_holding_hours DOUBLE PRECISION,
    reason TEXT,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT research_family_registry_status_check CHECK (status IN ('active', 'legacy')),
    CONSTRAINT research_family_registry_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_family_registry_status_idx
    ON research_family_registry (status, classification);
