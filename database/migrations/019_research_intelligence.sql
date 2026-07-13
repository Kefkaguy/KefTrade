CREATE TABLE IF NOT EXISTS research_ranking_snapshots (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    research_score NUMERIC NOT NULL,
    rank INTEGER NOT NULL,
    classification TEXT NOT NULL,
    review_priority TEXT NOT NULL,
    component_scores JSONB NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS research_ranking_snapshots_candidate_created_idx
    ON research_ranking_snapshots(candidate_id, created_at DESC);

CREATE INDEX IF NOT EXISTS research_ranking_snapshots_created_idx
    ON research_ranking_snapshots(created_at DESC);
