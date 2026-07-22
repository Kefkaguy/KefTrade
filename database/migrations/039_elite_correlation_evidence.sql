-- Compact, append-only correlation evidence for historical elite campaign jobs.
-- Results and historical outcomes are not rewritten. Evidence is generated only
-- from an immutable research dataset and remains simulation-only.

CREATE TABLE IF NOT EXISTS elite_candidate_correlation_evidence (
    id BIGSERIAL PRIMARY KEY,
    research_job_id BIGINT NOT NULL REFERENCES research_campaign_jobs(id) ON DELETE RESTRICT,
    elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE RESTRICT,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    evidence_version TEXT NOT NULL,
    strategy_returns JSONB NOT NULL,
    signal_exposure JSONB NOT NULL,
    observation_count INTEGER NOT NULL,
    evidence_hash TEXT NOT NULL,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(research_job_id, evidence_version, evidence_hash),
    CONSTRAINT elite_correlation_evidence_count_check CHECK (observation_count >= 0),
    CONSTRAINT elite_correlation_evidence_hash_check CHECK (length(evidence_hash) = 64),
    CONSTRAINT elite_correlation_evidence_simulation_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS elite_candidate_correlation_evidence_job_idx
    ON elite_candidate_correlation_evidence(research_job_id, created_at DESC, id DESC);
