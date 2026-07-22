-- Align the persisted drift classifications with the forward-validation model.
-- No rows are changed or removed by this migration.

ALTER TABLE elite_candidate_evidence_drift
    DROP CONSTRAINT IF EXISTS elite_candidate_evidence_drift_classification_check;

ALTER TABLE elite_candidate_evidence_drift
    ADD CONSTRAINT elite_candidate_evidence_drift_classification_check
    CHECK (
        drift_classification IN (
            'normal',
            'warning',
            'severe',
            'insufficient_forward_sample'
        )
    ) NOT VALID;

ALTER TABLE elite_candidate_evidence_drift
    VALIDATE CONSTRAINT elite_candidate_evidence_drift_classification_check;
