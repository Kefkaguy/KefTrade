-- 041_elite_consistency_gate.sql
-- Honest elite promotion gate.
--
-- The original gate promoted a candidate to elite when its POOLED profit
-- factor (sum of gross profit / sum of gross loss across every symbol-variant)
-- cleared 1.2. A few lucky symbols could carry a candidate whose typical
-- variant loses money -- which is why 4 of 7 elites came from families whose
-- median backtest is unprofitable.
--
-- This migration adds the columns needed to record and enforce a median-based
-- gate (the typical variant must itself be profitable), and a promotion_state
-- so a candidate that no longer qualifies is DEMOTED rather than deleted --
-- its immutable research evidence is preserved for audit and re-evaluation.

ALTER TABLE elite_research_candidates
    ADD COLUMN IF NOT EXISTS promotion_state TEXT NOT NULL DEFAULT 'elite',
    ADD COLUMN IF NOT EXISTS promotion_rule_version TEXT,
    ADD COLUMN IF NOT EXISTS median_profit_factor DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS median_expectancy DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS median_max_drawdown DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS median_variant_trade_count INTEGER,
    ADD COLUMN IF NOT EXISTS demotion_reason TEXT,
    ADD COLUMN IF NOT EXISTS reevaluated_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.constraint_column_usage
        WHERE table_name = 'elite_research_candidates'
          AND constraint_name = 'elite_research_candidates_promotion_state_check'
    ) THEN
        ALTER TABLE elite_research_candidates
            ADD CONSTRAINT elite_research_candidates_promotion_state_check
            CHECK (promotion_state IN ('elite', 'demoted'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS elite_research_candidates_promotion_state_idx
    ON elite_research_candidates (promotion_state, simulation_only);
