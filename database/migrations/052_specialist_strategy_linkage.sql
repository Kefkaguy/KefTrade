-- Phase 13.7: link specialist research threads to the versioned research
-- objects Phase 12.5 and Phase 13.1 introduced, so a thread can answer
-- "which hypothesis, which strategy version, which frozen dataset, which
-- behavioral DNA?" without reconstructing it from prose.
--
-- Purely additive: every column is nullable with no default, so the one
-- existing thread (amd_30m_long_session_momentum) and every future one keep
-- working untouched. A NULL here means "not linked", never a fabricated
-- association. No existing row is modified by this migration.

ALTER TABLE research_specialist_threads
    ADD COLUMN IF NOT EXISTS hypothesis_version_id BIGINT REFERENCES research_hypothesis_versions(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS strategy_version TEXT,
    ADD COLUMN IF NOT EXISTS strategy_architecture TEXT,
    ADD COLUMN IF NOT EXISTS dna_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS dataset_snapshot_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS research_specialist_threads_architecture_idx
    ON research_specialist_threads(strategy_architecture);

CREATE INDEX IF NOT EXISTS research_specialist_threads_hypothesis_idx
    ON research_specialist_threads(hypothesis_version_id);

CREATE INDEX IF NOT EXISTS research_specialist_threads_dna_idx
    ON research_specialist_threads(dna_fingerprint);

-- Phase 13.7 also records the investigation questions a thread is meant to
-- answer, so an investigation row can state which question it addresses
-- rather than leaving that implicit in free text.
ALTER TABLE research_specialist_investigations
    ADD COLUMN IF NOT EXISTS question TEXT,
    ADD COLUMN IF NOT EXISTS evidence_tier TEXT;
