-- Phase 12.5 Step 1: schema only, no backfill, no application-code wiring yet.
--
-- Adds trade-level train/validation designation and the seven pre-entry
-- scalar features described in the Phase 12.5 architecture proposal
-- (docs/2026-07-24-phase12-5-architecture-proposal.md, section 2.2-2.3) to
-- research_campaign_trades. Every column is nullable with no default -- no
-- existing row (Campaign 47 has none; Campaign 50's rows predate this
-- migration) is backfilled with a computed or guessed value. A NULL here
-- means "not computed for this trade," never a fabricated zero.

ALTER TABLE research_campaign_trades
    ADD COLUMN IF NOT EXISTS dataset_split TEXT,
    ADD COLUMN IF NOT EXISTS pre_entry_return_1 NUMERIC,
    ADD COLUMN IF NOT EXISTS pre_entry_return_5 NUMERIC,
    ADD COLUMN IF NOT EXISTS pre_entry_atr_relative_move NUMERIC,
    ADD COLUMN IF NOT EXISTS pre_entry_vwap_distance NUMERIC,
    ADD COLUMN IF NOT EXISTS pre_entry_trend_slope NUMERIC,
    ADD COLUMN IF NOT EXISTS pre_entry_volume_acceleration NUMERIC,
    ADD COLUMN IF NOT EXISTS pre_entry_session_progress NUMERIC;

ALTER TABLE research_campaign_trades DROP CONSTRAINT IF EXISTS research_campaign_trades_dataset_split_check;
ALTER TABLE research_campaign_trades ADD CONSTRAINT research_campaign_trades_dataset_split_check
    CHECK (dataset_split IS NULL OR dataset_split IN ('train', 'validation'));
