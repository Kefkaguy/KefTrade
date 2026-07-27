-- 054_champion_validation.sql
-- Champion graduation gate. Research champions imported by migration 053 are
-- deduped research winners, not deployable elites. This migration stores the
-- evidence that decides whether a champion graduates: one validation run per
-- champion, one row per gate inside that run, and the resulting state on the
-- candidate itself.
--
-- Nothing here weakens an existing gate. `promotion_state` keeps its Phase 13.8
-- meaning ('elite' is still the only state the portfolio solver reads) and the
-- new `validation_state` is an independent axis recording whether the champion
-- validation battery has actually been run and what it concluded. Existing
-- elite rows therefore start at 'pending_validation': that is the honest value
-- (this battery never ran on them), and it deliberately does not demote them.
--
-- Additive and idempotent: the migrate job re-applies every file on every
-- deploy.

ALTER TABLE elite_research_candidates
    ADD COLUMN IF NOT EXISTS validation_state TEXT NOT NULL DEFAULT 'pending_validation',
    ADD COLUMN IF NOT EXISTS validation_state_reason TEXT,
    ADD COLUMN IF NOT EXISTS validation_protocol_version TEXT,
    ADD COLUMN IF NOT EXISTS last_validation_run_id BIGINT,
    ADD COLUMN IF NOT EXISTS validation_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS validated_at TIMESTAMPTZ;

ALTER TABLE elite_research_candidates
    DROP CONSTRAINT IF EXISTS elite_research_candidates_validation_state_check;

ALTER TABLE elite_research_candidates
    ADD CONSTRAINT elite_research_candidates_validation_state_check
    CHECK (validation_state IN (
        'pending_validation',
        'validating',
        'validated',
        'failed_validation',
        'needs_more_data'
    ));

CREATE INDEX IF NOT EXISTS elite_research_candidates_validation_state_idx
    ON elite_research_candidates (validation_state, promotion_state, simulation_only);

-- One row per champion validation attempt. Immutable evidence: a re-run
-- inserts a new row rather than overwriting the previous verdict, so a
-- champion that passed once and failed later keeps both records.
CREATE TABLE IF NOT EXISTS elite_champion_validation_runs (
    id BIGSERIAL PRIMARY KEY,
    elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE CASCADE,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    candidate_id TEXT NOT NULL,
    research_job_id BIGINT,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    family_id TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    status TEXT NOT NULL,
    state_reason TEXT,
    thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
    measurements JSONB NOT NULL DEFAULT '{}'::jsonb,
    gates JSONB NOT NULL DEFAULT '[]'::jsonb,
    gates_passed INTEGER NOT NULL DEFAULT 0,
    gates_failed INTEGER NOT NULL DEFAULT 0,
    gates_inconclusive INTEGER NOT NULL DEFAULT 0,
    backtests_executed INTEGER NOT NULL DEFAULT 0,
    thresholds_weakened BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_hash TEXT NOT NULL,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    runtime_ms INTEGER,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT elite_champion_validation_runs_status_check CHECK (status IN (
        'validating',
        'validated',
        'failed_validation',
        'needs_more_data',
        'error'
    )),
    CONSTRAINT elite_champion_validation_runs_weakening_check CHECK (thresholds_weakened = FALSE),
    CONSTRAINT elite_champion_validation_runs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS elite_champion_validation_runs_candidate_idx
    ON elite_champion_validation_runs (elite_candidate_id, id DESC);

CREATE INDEX IF NOT EXISTS elite_champion_validation_runs_status_idx
    ON elite_champion_validation_runs (status, completed_at DESC);

-- One row per gate per run, so failure diagnosis can group by gate, family,
-- symbol, and timeframe without unpacking JSONB.
CREATE TABLE IF NOT EXISTS elite_champion_validation_gates (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES elite_champion_validation_runs(id) ON DELETE CASCADE,
    elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE CASCADE,
    gate_id TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    observed JSONB NOT NULL DEFAULT '{}'::jsonb,
    required JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT elite_champion_validation_gates_status_check CHECK (status IN (
        'passed',
        'failed',
        'inconclusive'
    )),
    CONSTRAINT elite_champion_validation_gates_unique UNIQUE (run_id, gate_id)
);

CREATE INDEX IF NOT EXISTS elite_champion_validation_gates_gate_idx
    ON elite_champion_validation_gates (gate_id, status);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'elite_research_candidates'
          AND constraint_name = 'elite_research_candidates_last_validation_run_fk'
    ) THEN
        ALTER TABLE elite_research_candidates
            ADD CONSTRAINT elite_research_candidates_last_validation_run_fk
            FOREIGN KEY (last_validation_run_id)
            REFERENCES elite_champion_validation_runs(id) ON DELETE SET NULL;
    END IF;
END $$;
