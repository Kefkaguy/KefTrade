from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "database" / "migrations" / "038_elite_portfolio_builder.sql"


def test_migration_038_is_additive_and_preserves_external_buy_only_boundary() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "DELETE FROM" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "ALTER TABLE proposed_broker_orders" not in sql
    assert "ALTER TABLE broker_orders" not in sql
    assert "ADD COLUMN IF NOT EXISTS strategy_direction" in sql
    assert "DEFAULT 'long'" in sql
    assert "internal_only" in sql
    assert "external_observe" in sql
    assert "paper_eligible" in sql


def test_migration_038_contains_resumable_portfolio_audit_tables() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in (
        "elite_portfolio_runs",
        "elite_portfolio_snapshots",
        "elite_portfolio_eligibility",
        "elite_portfolio_correlations",
        "elite_portfolio_conflicts",
        "elite_portfolio_members",
        "elite_portfolio_activation_attempts",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

    for state in ("stale", "failed", "superseded", "cancelled", "activated_internal"):
        assert f"'{state}'" in sql
