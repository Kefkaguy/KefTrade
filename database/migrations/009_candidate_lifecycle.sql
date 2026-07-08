CREATE TABLE IF NOT EXISTS candidate_lifecycle_events (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    metrics JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS candidate_lifecycle_events_candidate_idx
ON candidate_lifecycle_events(candidate_id, created_at DESC);
