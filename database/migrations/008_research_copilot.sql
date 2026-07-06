CREATE TABLE IF NOT EXISTS research_copilot_interactions (
    id BIGSERIAL PRIMARY KEY,
    question TEXT NOT NULL,
    response TEXT NOT NULL,
    evidence_refs JSONB NOT NULL,
    model TEXT NOT NULL,
    safety_flags JSONB NOT NULL,
    context_summary JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS research_copilot_interactions_created_at_idx
    ON research_copilot_interactions(created_at DESC);
