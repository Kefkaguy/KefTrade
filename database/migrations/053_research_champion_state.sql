-- 053_research_champion_state.sql
-- Allow Elite Builder to store research-promoted, deduped candidates without
-- pretending they are final deployable elites.

ALTER TABLE elite_research_candidates
    DROP CONSTRAINT IF EXISTS elite_research_candidates_promotion_state_check;

ALTER TABLE elite_research_candidates
    ADD CONSTRAINT elite_research_candidates_promotion_state_check
    CHECK (promotion_state IN ('elite', 'demoted', 'research_champion'));

CREATE INDEX IF NOT EXISTS elite_research_candidates_research_champion_idx
    ON elite_research_candidates (promotion_state, forward_validation_state, simulation_only);
