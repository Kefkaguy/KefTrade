-- Stage 0 data-fidelity cleanup.
--
-- The Stage 0 microstructure probe (2026-08-16) measured Alpaca's SIP NBBO
-- feed against a threshold declared before the number existed.  Venue rotation
-- accounted for 45.224% of gross |e_n| across 23,016,760 quote updates, versus
-- a 30% ceiling.  Two consequences are recorded here.
--
-- Nothing in this migration rewrites historical evidence.  Snapshot rows stay
-- byte-identical and their immutability triggers stay in force; the
-- certification below is a separate, additive record *about* them.

-- ---------------------------------------------------------------------------
-- 1. Nanosecond fidelity for future quote ingestion.
-- ---------------------------------------------------------------------------
--
-- `intraday_quote_snapshots` keyed on a microsecond-truncated timestamp, so two
-- NBBO updates inside the same microsecond collapsed to one row under
-- ON CONFLICT DO UPDATE -- last writer wins, silently.  The probe measured
-- 37,526 of 23,016,760 updates (0.163%) lost this way, and zero true
-- nanosecond ties, so the source instant is a genuine unique key.
--
-- The surviving row of a collapsed microsecond is its *latest* state, which is
-- future information relative to anything else inside it.  Small leak, real
-- leak.

ALTER TABLE intraday_quote_snapshots
    ADD COLUMN IF NOT EXISTS timestamp_ns BIGINT;

-- Existing rows were stored at microsecond precision, so their nanosecond
-- instant is exactly the stored timestamp scaled up.  This derives a column
-- from what is already there; it does not alter a single recorded value.
UPDATE intraday_quote_snapshots
   SET timestamp_ns = (EXTRACT(EPOCH FROM timestamp) * 1000000)::BIGINT * 1000
 WHERE timestamp_ns IS NULL;

-- Historical rows were unique on (symbol, provider, feed, timestamp), so the
-- derived nanosecond values are unique too and the new key is safe to adopt.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'intraday_quote_snapshots_symbol_provider_feed_timestamp_key'
    ) THEN
        ALTER TABLE intraday_quote_snapshots
            DROP CONSTRAINT intraday_quote_snapshots_symbol_provider_feed_timestamp_key;
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS intraday_quote_snapshots_instant_key
    ON intraday_quote_snapshots(symbol, provider, feed, timestamp_ns);

-- ---------------------------------------------------------------------------
-- 2. Certification of snapshot fields that are no longer interpretable.
-- ---------------------------------------------------------------------------
--
-- `order_flow_imbalance`, `normalized_order_flow_imbalance` and `mean_depth`
-- are computed from Alpaca's quoted sizes, which are one venue's queue at a
-- best price that is usually a tie between several venues.  Any snapshot
-- carrying them carries the artefact.
--
-- Spread columns are computed from NBBO *prices* and are unaffected; they are
-- deliberately not certified against, so a later reader does not retire them
-- by association.

CREATE TABLE IF NOT EXISTS research_dataset_field_certifications (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    table_name TEXT NOT NULL,
    field_name TEXT NOT NULL,
    certification TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_report TEXT NOT NULL,
    measured_value NUMERIC,
    allowed_value NUMERIC,
    observations BIGINT,
    data_fidelity_version TEXT NOT NULL,
    certified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_dataset_field_certification_kind_check
        CHECK (certification IN ('approved', 'not_approved_for_queue_interpretation')),
    CONSTRAINT research_dataset_field_certification_immutable_check CHECK (immutable = TRUE),
    -- A NULL dataset_id is the platform-wide statement covering every snapshot,
    -- present and future; a non-NULL one certifies a specific manifest.
    UNIQUE NULLS NOT DISTINCT (dataset_id, table_name, field_name, data_fidelity_version)
);

CREATE INDEX IF NOT EXISTS research_dataset_field_certifications_field_idx
    ON research_dataset_field_certifications(table_name, field_name);

DO $$
DECLARE
    trigger_name TEXT := 'research_dataset_field_certifications_immutable_trigger';
BEGIN
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON research_dataset_field_certifications', trigger_name);
    EXECUTE format(
        'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON research_dataset_field_certifications '
        || 'FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation()',
        trigger_name
    );
END;
$$;

-- The platform-wide certification.  Applies to every snapshot that carries
-- these fields, so a dataset frozen before Stage 0 and one frozen after are
-- covered by the same statement.
INSERT INTO research_dataset_field_certifications(
    dataset_id, table_name, field_name, certification, reason,
    evidence_report, measured_value, allowed_value, observations,
    data_fidelity_version
)
SELECT
    NULL,
    source.table_name,
    source.field_name,
    'not_approved_for_queue_interpretation',
    'retired_data_fidelity: Alpaca SIP venue rotation measured at 45.224% of '
        || 'gross OFI vs 30% allowed. Quoted sizes are a single venue''s queue at '
        || 'an NBBO that is usually tied between venues, so changes in them do '
        || 'not reliably represent liquidity arriving or leaving.',
    'docs/2026-08-16-stage0-microstructure-probe-results.md',
    0.45224,
    0.30,
    23016760,
    'stage0_venue_rotation_v1'
FROM (
    VALUES
        ('intraday_microstructure_features', 'order_flow_imbalance'),
        ('intraday_microstructure_features', 'normalized_order_flow_imbalance'),
        ('intraday_microstructure_features', 'mean_depth'),
        -- The snapshot copies: `record_intraday_dataset_snapshot` joins the
        -- microstructure columns onto `research_dataset_intraday_features`,
        -- so the frozen artefact lives there rather than in a table of its own.
        ('research_dataset_intraday_features', 'order_flow_imbalance'),
        ('research_dataset_intraday_features', 'normalized_order_flow_imbalance'),
        ('research_dataset_intraday_features', 'mean_depth')
) AS source(table_name, field_name)
ON CONFLICT DO NOTHING;
