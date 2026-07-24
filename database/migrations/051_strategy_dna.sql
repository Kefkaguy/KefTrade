-- Phase 13.1: Strategy DNA -- a versioned, append-only behavioral
-- description of every strategy family, separate from raw parameters.
--
-- One row = one (family architecture, strategy_version, dna_schema_version)
-- combination. Rows are immutable once written (reuses the blanket
-- immutability trigger from migration 048/049): correcting a DNA record
-- means appending a new row with a higher dna_schema_version and, where
-- relevant, marking the old row superseded via the superseded_by_id pointer
-- column on the NEW row -- never editing or deleting history.
--
-- `fingerprint` is a deterministic sha256 over the canonical (sorted-key)
-- JSON of the behavioral payload plus identity fields, computed in
-- app/services/strategy_dna.py. Two DNA rows with the same fingerprint
-- describe behaviorally identical strategies regardless of parameter values.
--
-- This migration adds evidence structure only: no deployment, promotion, or
-- trading capability, and no change to any existing table or row.

CREATE TABLE IF NOT EXISTS strategy_dna (
    id BIGSERIAL PRIMARY KEY,
    family_architecture TEXT NOT NULL,          -- e.g. 'ema_trend_continuation_v1' (matches FAMILY_REGISTRY key)
    strategy_version TEXT NOT NULL,             -- e.g. 'v1'
    dna_schema_version INTEGER NOT NULL,        -- version of the DNA vocabulary itself
    fingerprint TEXT NOT NULL,                  -- deterministic sha256 of canonical payload
    dna JSONB NOT NULL,                         -- the behavioral payload (see strategy_dna.py for the field vocabulary)
    superseded_by_id BIGINT REFERENCES strategy_dna(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (family_architecture, strategy_version, dna_schema_version),
    CONSTRAINT strategy_dna_payload_is_object CHECK (jsonb_typeof(dna) = 'object')
);

CREATE INDEX IF NOT EXISTS strategy_dna_fingerprint_idx ON strategy_dna (fingerprint);
CREATE INDEX IF NOT EXISTS strategy_dna_family_idx ON strategy_dna (family_architecture, dna_schema_version DESC);

-- Append-only: block UPDATE and DELETE outright, EXCEPT the one legitimate
-- mutation -- setting superseded_by_id on an old row when a newer row lands.
CREATE OR REPLACE FUNCTION prevent_strategy_dna_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'strategy_dna rows are append-only and can never be deleted';
    END IF;
    IF ROW(NEW.family_architecture, NEW.strategy_version, NEW.dna_schema_version, NEW.fingerprint, NEW.dna, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.family_architecture, OLD.strategy_version, OLD.dna_schema_version, OLD.fingerprint, OLD.dna, OLD.created_at) THEN
        RAISE EXCEPTION 'strategy_dna rows are immutable; append a new dna_schema_version instead (only superseded_by_id may be set)';
    END IF;
    IF OLD.superseded_by_id IS NOT NULL AND NEW.superseded_by_id IS DISTINCT FROM OLD.superseded_by_id THEN
        RAISE EXCEPTION 'strategy_dna.superseded_by_id can only be set once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS strategy_dna_immutable_trigger ON strategy_dna;
CREATE TRIGGER strategy_dna_immutable_trigger
    BEFORE UPDATE OR DELETE ON strategy_dna
    FOR EACH ROW EXECUTE FUNCTION prevent_strategy_dna_mutation();
