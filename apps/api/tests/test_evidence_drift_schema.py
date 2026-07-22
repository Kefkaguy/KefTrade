from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ALLOWED_DRIFT_CLASSIFICATIONS = {
    "normal",
    "warning",
    "severe",
    "insufficient_forward_sample",
}


def test_evidence_drift_schema_accepts_every_application_classification() -> None:
    migration = (
        REPOSITORY_ROOT
        / "database"
        / "migrations"
        / "037_evidence_drift_classification_alignment.sql"
    ).read_text(encoding="utf-8")
    runtime_schema = (
        REPOSITORY_ROOT / "apps" / "api" / "app" / "services" / "research_campaigns.py"
    ).read_text(encoding="utf-8")

    for classification in ALLOWED_DRIFT_CLASSIFICATIONS:
        assert f"'{classification}'" in migration
        assert f"'{classification}'" in runtime_schema

    assert "VALIDATE CONSTRAINT elite_candidate_evidence_drift_classification_check" in migration
