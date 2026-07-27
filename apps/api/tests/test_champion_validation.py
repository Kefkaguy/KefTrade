from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services import champion_validation
from app.services.champion_validation import (
    DEFAULT_VALIDATION_THRESHOLDS,
    GATE_FAILED,
    GATE_INCONCLUSIVE,
    GATE_ORDER,
    GATE_PASSED,
    _duplication_measurements,
    _regime_buckets,
    classify_validation,
    evaluate_gates,
    gate_counts,
    VALIDATION_STATES,
    profit_factor_value,
    run_champion_validation,
    thresholds_weakened,
    validation_thresholds,
)


def metrics(*, pf=2.0, expectancy=8.0, trades=60, drawdown=0.05, infinite=False):
    return {
        "profit_factor": None if infinite else pf,
        "profit_factor_is_infinite": infinite,
        "expectancy_per_trade": expectancy,
        "number_of_trades": trades,
        "max_drawdown": drawdown,
    }


def run(**overrides):
    return {"status": "measured", "metrics": metrics(**overrides), "row_count": 2000, "symbol": "AMD", "timeframe": "30m"}


def unavailable(reason="no data"):
    return {"status": "unavailable", "reason": reason}


def healthy_measurements(**overrides):
    """Every measurement present and comfortably clearing every gate."""
    base = {
        "full": run(),
        "in_sample": run(pf=2.1, trades=40),
        "out_of_sample": run(pf=1.8, trades=25),
        "cost_stress": run(pf=1.4),
        "cross_symbol": [
            {"status": "measured", "symbol": "NVDA", "metrics": metrics(pf=1.5, trades=30)},
            {"status": "measured", "symbol": "TSLA", "metrics": metrics(pf=1.3, trades=22)},
        ],
        "timeframe_stability": {"status": "measured", "timeframe": "15m", "metrics": metrics(pf=1.4, trades=35)},
        "regime_basis": "market_regime",
        "regime_buckets": [
            {"bucket": "trending", "metrics": metrics(pf=2.0, trades=30)},
            {"bucket": "ranging", "metrics": metrics(pf=1.3, trades=20)},
        ],
        "peer_correlations": [],
        "peer_parameter_similarity": [],
    }
    base.update(overrides)
    return base


def verdicts(measurements, thresholds=None):
    return {gate["gate_id"]: gate for gate in evaluate_gates(measurements, thresholds)}


def test_a_champion_that_clears_every_gate_is_validated() -> None:
    gates = evaluate_gates(healthy_measurements())

    assert [gate["gate_id"] for gate in gates] == list(GATE_ORDER)
    assert all(gate["status"] == GATE_PASSED for gate in gates)
    assert classify_validation(gates) == ("validated", f"Passed all {len(GATE_ORDER)} graduation gates.")
    assert gate_counts(gates) == {"passed": len(GATE_ORDER), "failed": 0, "inconclusive": 0}


def test_out_of_sample_collapse_fails_the_gate() -> None:
    gate = verdicts(healthy_measurements(out_of_sample=run(pf=0.8, expectancy=-1.2, trades=25)))["out_of_sample"]

    assert gate["status"] == GATE_FAILED
    assert "profit factor" in gate["detail"]
    assert gate["observed"]["out_of_sample_profit_factor"] == 0.8


def test_profit_factor_decay_fails_even_when_out_of_sample_is_still_profitable() -> None:
    # 1.15 clears the absolute 1.10 floor on its own, but it is only 38% of the
    # in-sample 3.0 -- the "it worked until the exact window that found it
    # ended" signature the retention threshold exists to catch.
    measurements = healthy_measurements(in_sample=run(pf=3.0, trades=40), out_of_sample=run(pf=1.15, trades=25))
    gate = verdicts(measurements)["out_of_sample"]

    assert gate["status"] == GATE_FAILED
    assert "decayed" in gate["detail"]
    assert gate["observed"]["profit_factor_retention"] == pytest.approx(1.15 / 3.0)


def test_a_missing_holdout_window_is_inconclusive_not_a_pass() -> None:
    measurements = healthy_measurements(out_of_sample=unavailable("only 40 usable bars in this window"))
    gates = evaluate_gates(measurements)
    by_id = {gate["gate_id"]: gate for gate in gates}

    assert by_id["out_of_sample"]["status"] == GATE_INCONCLUSIVE
    assert by_id["minimum_trades"]["status"] == GATE_INCONCLUSIVE
    assert "40 usable bars" in by_id["out_of_sample"]["detail"]
    state, reason = classify_validation(gates)
    assert state == "needs_more_data"
    assert "out-of-sample period" in reason


def test_too_few_out_of_sample_trades_is_a_failure_not_missing_data() -> None:
    gate = verdicts(healthy_measurements(out_of_sample=run(pf=4.0, trades=3)))["minimum_trades"]

    assert gate["status"] == GATE_FAILED
    assert gate["observed"]["out_of_sample_trades"] == 3


def test_a_single_symbol_result_cannot_pass_the_cross_asset_gate() -> None:
    measurements = healthy_measurements(
        cross_symbol=[
            {"status": "measured", "symbol": "NVDA", "metrics": metrics(pf=1.6, trades=30)},
            {"status": "measured", "symbol": "TSLA", "metrics": metrics(pf=0.7, expectancy=-2.0, trades=30)},
            {"status": "measured", "symbol": "AAPL", "metrics": metrics(pf=0.9, expectancy=-0.5, trades=30)},
        ]
    )
    gate = verdicts(measurements)["cross_symbol"]

    assert gate["status"] == GATE_FAILED
    assert gate["observed"]["symbols_passed"] == 1
    assert gate["observed"]["symbols_tested"] == 3


def test_a_cross_symbol_pass_needs_real_trade_counts() -> None:
    # Profit factor looks superb on both alternates, but on two trades each it
    # is noise; the trade floor keeps it out of the "passed" column.
    measurements = healthy_measurements(
        cross_symbol=[
            {"status": "measured", "symbol": "NVDA", "metrics": metrics(pf=6.0, trades=2)},
            {"status": "measured", "symbol": "TSLA", "metrics": metrics(pf=5.0, trades=2)},
        ]
    )
    gate = verdicts(measurements)["cross_symbol"]

    assert gate["status"] == GATE_FAILED
    assert [row["passed"] for row in gate["observed"]["results"]] == [False, False]


def test_missing_alternate_symbols_are_inconclusive_and_name_the_blocker() -> None:
    measurements = healthy_measurements(
        cross_symbol=[
            {"status": "measured", "symbol": "NVDA", "metrics": metrics()},
            {"status": "unavailable", "symbol": "TSLA", "reason": "No 30m dataset is available for TSLA."},
        ]
    )
    gate = verdicts(measurements)["cross_symbol"]

    assert gate["status"] == GATE_INCONCLUSIVE
    assert "TSLA" in gate["detail"]


def test_one_profitable_regime_out_of_several_fails() -> None:
    measurements = healthy_measurements(
        regime_buckets=[
            {"bucket": "trending", "metrics": metrics(pf=3.0, expectancy=12.0, trades=40)},
            {"bucket": "ranging", "metrics": metrics(pf=0.6, expectancy=-4.0, trades=25)},
            {"bucket": "volatile", "metrics": metrics(pf=0.8, expectancy=-1.0, trades=18)},
        ]
    )
    gate = verdicts(measurements)["regime_robustness"]

    assert gate["status"] == GATE_FAILED
    assert gate["observed"]["buckets_profitable"] == 1


def test_regimes_without_enough_trades_are_inconclusive_not_failed() -> None:
    measurements = healthy_measurements(
        regime_buckets=[
            {"bucket": "trending", "metrics": metrics(trades=40)},
            {"bucket": "ranging", "metrics": metrics(expectancy=-3.0, trades=1)},
        ]
    )
    gate = verdicts(measurements)["regime_robustness"]

    assert gate["status"] == GATE_INCONCLUSIVE
    assert gate["observed"]["buckets_with_enough_trades"] == 1


def test_doubling_costs_that_kills_the_edge_fails_the_stress_gate() -> None:
    gate = verdicts(healthy_measurements(cost_stress=run(pf=0.85, expectancy=-0.4)))["cost_stress"]

    assert gate["status"] == GATE_FAILED
    assert "2× fees" in gate["detail"]


def test_drawdown_stress_reports_the_worst_run_across_the_whole_battery() -> None:
    measurements = healthy_measurements(
        cross_symbol=[
            {"status": "measured", "symbol": "NVDA", "metrics": metrics(pf=1.5, trades=30, drawdown=0.31)},
            {"status": "measured", "symbol": "TSLA", "metrics": metrics(pf=1.3, trades=22)},
        ]
    )
    gate = verdicts(measurements)["drawdown_stress"]

    assert gate["status"] == GATE_FAILED
    assert gate["observed"]["worst_run"] == "cross_symbol:NVDA"
    assert gate["observed"]["worst_max_drawdown"] == 0.31


def test_a_missing_sibling_timeframe_blocks_graduation_as_needs_more_data() -> None:
    measurements = healthy_measurements(
        timeframe_stability=unavailable("No 15m dataset is available for AMD. Snapshot both timeframes first.")
    )
    gates = evaluate_gates(measurements)

    assert {gate["gate_id"]: gate["status"] for gate in gates}["timeframe_stability"] == GATE_INCONCLUSIVE
    assert classify_validation(gates)[0] == "needs_more_data"


def test_an_edge_that_only_exists_on_one_bar_size_fails() -> None:
    measurements = healthy_measurements(
        timeframe_stability={"status": "measured", "timeframe": "15m", "metrics": metrics(pf=0.7, expectancy=-2.0, trades=40)}
    )
    gate = verdicts(measurements)["timeframe_stability"]

    assert gate["status"] == GATE_FAILED
    assert "15m" in gate["detail"]


def test_no_existing_elite_means_nothing_can_be_duplicated() -> None:
    by_id = verdicts(healthy_measurements())

    assert by_id["correlation_duplication"]["status"] == GATE_PASSED
    assert by_id["correlation_duplication"]["observed"]["peers_compared"] == 0
    assert by_id["parameter_similarity"]["status"] == GATE_PASSED


def test_a_champion_that_tracks_an_existing_elite_is_rejected_as_a_duplicate() -> None:
    measurements = healthy_measurements(
        peer_correlations=[
            {"peer_key": "cand_a|AMD|30m", "coefficient": 0.42, "observations": 180},
            {"peer_key": "cand_b|AMD|30m", "coefficient": 0.94, "observations": 200},
        ]
    )
    gate = verdicts(measurements)["correlation_duplication"]

    assert gate["status"] == GATE_FAILED
    assert gate["observed"]["most_correlated_peer"] == "cand_b|AMD|30m"


def test_correlation_without_enough_overlap_is_inconclusive() -> None:
    measurements = healthy_measurements(
        peer_correlations=[{"peer_key": "cand_a|AMD|30m", "coefficient": 0.99, "observations": 4}]
    )
    gate = verdicts(measurements)["correlation_duplication"]

    assert gate["status"] == GATE_INCONCLUSIVE
    assert gate["observed"]["peers_with_enough_overlap"] == 0


def test_a_reparameterised_clone_of_a_same_slot_elite_fails() -> None:
    measurements = healthy_measurements(
        peer_parameter_similarity=[{"peer_key": "cand_b|AMD|30m", "similarity": 0.97, "compared_parameter_count": 12}]
    )
    gate = verdicts(measurements)["parameter_similarity"]

    assert gate["status"] == GATE_FAILED
    assert gate["observed"]["most_similar_peer"] == "cand_b|AMD|30m"


def test_a_failure_outranks_an_inconclusive_gate_in_the_verdict() -> None:
    measurements = healthy_measurements(
        out_of_sample=run(pf=0.5, expectancy=-3.0, trades=25),
        timeframe_stability=unavailable("no 15m data"),
    )
    state, reason = classify_validation(evaluate_gates(measurements))

    assert state == "failed_validation"
    assert "out-of-sample period" in reason
    assert "timeframe stability" not in reason


def test_an_infinite_profit_factor_is_not_read_as_zero() -> None:
    assert profit_factor_value(metrics(infinite=True)) > 100
    gate = verdicts(healthy_measurements(out_of_sample=run(infinite=True, trades=25)))["out_of_sample"]
    assert gate["status"] == GATE_PASSED


def test_an_infinite_in_sample_profit_factor_does_not_manufacture_fake_decay() -> None:
    # In-sample had zero losing trades, so the "999" sentinel would make any
    # finite out-of-sample number look like a 99% collapse. Retention is
    # reported as unmeasured instead; the absolute floors still decide.
    measurements = healthy_measurements(in_sample=run(infinite=True, trades=40), out_of_sample=run(pf=1.5, trades=25))
    gate = verdicts(measurements)["out_of_sample"]

    assert gate["status"] == GATE_PASSED
    assert gate["observed"]["profit_factor_retention"] is None


def test_parameter_similarity_only_compares_elites_in_the_same_slot() -> None:
    champion = {"symbol": "AMD", "timeframe": "30m", "family_id": "session_momentum", "parameters": {"rsi_min": 55, "atr_multiplier": 1.5}}
    peers = [
        {
            "peer_key": "same_slot|AMD|30m",
            "symbol": "AMD",
            "timeframe": "30m",
            "family_id": "session_momentum",
            "parameters": {"rsi_min": 55, "atr_multiplier": 1.5},
            "strategy_returns": {},
        },
        {
            "peer_key": "other_symbol|NVDA|30m",
            "symbol": "NVDA",
            "timeframe": "30m",
            "family_id": "session_momentum",
            "parameters": {"rsi_min": 55, "atr_multiplier": 1.5},
            "strategy_returns": {},
        },
    ]

    correlations, similarities = _duplication_measurements(champion=champion, champion_returns={}, peers=peers)

    # Correlation is checked against every elite; identical parameters on a
    # different symbol are cross-asset confirmation, not duplication.
    assert [row["peer_key"] for row in correlations] == ["same_slot|AMD|30m", "other_symbol|NVDA|30m"]
    assert [row["peer_key"] for row in similarities] == ["same_slot|AMD|30m"]
    assert similarities[0]["similarity"] == 1.0


def test_correlation_measurement_reports_overlap_size() -> None:
    champion = {"symbol": "AMD", "timeframe": "30m", "family_id": "f", "parameters": {}}
    peers = [
        {
            "peer_key": "peer|AMD|30m",
            "symbol": "AMD",
            "timeframe": "30m",
            "family_id": "f",
            "parameters": {},
            "strategy_returns": {"2026-01-01": 0.01, "2026-01-02": 0.02, "2026-01-03": 0.03},
        }
    ]

    correlations, _ = _duplication_measurements(
        champion=champion,
        champion_returns={"2026-01-01": 0.02, "2026-01-02": 0.04, "2026-01-03": 0.06},
        peers=peers,
    )

    assert correlations[0]["observations"] == 3
    assert correlations[0]["coefficient"] == pytest.approx(1.0)


def test_regime_buckets_prefer_real_regimes_and_fall_back_to_calendar_years() -> None:
    real = _regime_buckets(
        {
            "by_market_regime": [{"regime": "trending", "metrics": metrics()}, {"regime": "ranging", "metrics": metrics()}],
            "by_year": [{"year": 2025, "metrics": metrics()}],
        }
    )
    assert real[0] == "market_regime"
    assert [row["bucket"] for row in real[1]] == ["trending", "ranging"]

    # Intraday campaigns carry no regime context, so the honest fallback is the
    # calendar bucketing every run produces -- and the basis says so.
    fallback = _regime_buckets({"by_market_regime": [{"regime": "unknown", "metrics": metrics()}], "by_year": [{"year": 2025, "metrics": metrics()}]})
    assert fallback[0] == "calendar_year"
    assert [row["bucket"] for row in fallback[1]] == ["2025"]

    assert _regime_buckets({}) == ("unavailable", [])


def test_thresholds_can_be_tightened_but_weakening_is_reported() -> None:
    assert thresholds_weakened(validation_thresholds()) == []
    assert thresholds_weakened(validation_thresholds({"minimum_out_of_sample_trades": 40})) == []
    assert thresholds_weakened(validation_thresholds({"minimum_out_of_sample_trades": 2})) == ["minimum_out_of_sample_trades"]
    assert thresholds_weakened(validation_thresholds({"maximum_stressed_drawdown": 0.9})) == ["maximum_stressed_drawdown"]


def test_an_unknown_threshold_name_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="unknown champion validation threshold"):
        validation_thresholds({"minimum_profit_facter": 1.5})


def test_every_gate_has_a_default_threshold_and_a_label() -> None:
    gates = evaluate_gates(healthy_measurements())
    for gate in gates:
        assert gate["label"]
        assert gate["detail"]
        assert isinstance(gate["required"], dict)
    assert set(DEFAULT_VALIDATION_THRESHOLDS) >= {"holdout_ratio", "cost_stress_multiplier", "maximum_parameter_similarity"}


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


CHAMPION_ROW = {
    "id": 41,
    "candidate_id": "cand_amd_session_momentum",
    "campaign_id": 51,
    "family_id": "session_momentum",
    "research_score": 0.82,
    "profit_factor": 1.9,
    "expectancy": 7.4,
    "max_drawdown": 0.06,
    "trade_count": 84,
    "promotion_state": "research_champion",
    "validation_state": "pending_validation",
    "validation_state_reason": None,
    "validated_at": None,
    "last_validation_run_id": None,
    "strategy_direction": "long",
    "research_job_id": 9001,
    "symbol": "AMD",
    "timeframe": "30m",
    "candidate": {"candidate_id": "cand_amd_session_momentum", "parameters": {"fee_rate": 0.0004}},
    "dataset_id": 7,
    "strategy_family": "session_momentum",
}


class FakeValidationConnection:
    """Just enough of psycopg to exercise the graduation state machine."""

    def __init__(self, champion=None):
        self.champion = dict(champion or CHAMPION_ROW)
        self.statements: list[tuple[str, Any]] = []
        self.gate_rows: list[tuple[Any, ...]] = []
        self.commits = 0

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "INSERT INTO elite_champion_validation_runs" in query:
            return FakeResult([{"id": 777}])
        if "INSERT INTO elite_champion_validation_gates" in query:
            self.gate_rows.append(params)
            return FakeResult([])
        if "UPDATE elite_research_candidates" in query:
            return FakeResult([])
        if "COUNT(*) FILTER" in query:
            return FakeResult([{}])
        if "JOIN LATERAL" in query:
            return FakeResult([self.champion])
        return FakeResult([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def updates(self):
        return [(query, params) for query, params in self.statements if "UPDATE elite_research_candidates" in query]


def _stub_measurements(monkeypatch, measurements):
    monkeypatch.setattr(champion_validation, "_peer_evidence", lambda conn: [])
    monkeypatch.setattr(
        champion_validation,
        "measure_champion",
        lambda conn, champion, **kwargs: {**measurements, "backtests_executed": 9, "_champion_returns": {"2026-01-02": 0.01}},
    )


def test_a_champion_that_passes_every_gate_is_promoted_to_final_elite(monkeypatch) -> None:
    _stub_measurements(monkeypatch, healthy_measurements())
    conn = FakeValidationConnection()

    outcome = run_champion_validation(conn, limit=1)

    assert outcome["validated"] == 1
    assert outcome["thresholds_weakened"] is False
    assert outcome["outcomes"][0]["status"] == "validated"
    assert outcome["outcomes"][0]["run_id"] == 777
    assert len(conn.gate_rows) == len(GATE_ORDER)

    # The final UPDATE is the only thing that makes a champion visible to the
    # portfolio solver, so assert on the promotion flag it is driven by.
    final_query, final_params = conn.updates()[-1]
    assert "promotion_state = CASE WHEN %s THEN 'elite' ELSE promotion_state END" in final_query
    assert final_params[0] == "validated"
    assert final_params[5] is True


def test_a_failing_champion_keeps_its_champion_state_and_records_why(monkeypatch) -> None:
    _stub_measurements(monkeypatch, healthy_measurements(out_of_sample=run(pf=0.6, expectancy=-2.0, trades=25)))
    conn = FakeValidationConnection()

    outcome = run_champion_validation(conn, limit=1)

    assert outcome["validated"] == 0
    assert outcome["failed_validation"] == 1
    assert outcome["outcomes"][0]["failed_gates"] == ["out_of_sample"]
    _, final_params = conn.updates()[-1]
    assert final_params[0] == "failed_validation"
    assert final_params[5] is False


def test_unmeasurable_evidence_parks_a_champion_in_needs_more_data(monkeypatch) -> None:
    _stub_measurements(monkeypatch, healthy_measurements(timeframe_stability=unavailable("no 15m dataset for AMD")))
    conn = FakeValidationConnection()

    outcome = run_champion_validation(conn, limit=1)

    assert outcome["needs_more_data"] == 1
    assert outcome["outcomes"][0]["inconclusive_gates"] == ["timeframe_stability"]
    assert "no 15m dataset for AMD" not in outcome["outcomes"][0]["reason"]
    _, final_params = conn.updates()[-1]
    assert final_params[0] == "needs_more_data"
    assert final_params[5] is False


def test_a_measurement_crash_returns_the_champion_to_the_queue_rather_than_failing_it(monkeypatch) -> None:
    monkeypatch.setattr(champion_validation, "_peer_evidence", lambda conn: [])

    def explode(conn, champion, **kwargs):
        raise RuntimeError("dataset snapshot 7 is missing AMD 30m")

    monkeypatch.setattr(champion_validation, "measure_champion", explode)
    conn = FakeValidationConnection()

    outcome = run_champion_validation(conn, limit=1)

    assert outcome["errors"] == 1
    assert outcome["validated"] == 0
    assert "dataset snapshot 7 is missing" in outcome["outcomes"][0]["reason"]
    # A broken loader is not evidence that a strategy is bad.
    _, final_params = conn.updates()[-1]
    assert final_params[0].startswith("Validation could not run:")


def test_weakened_thresholds_are_rejected_before_anything_runs(monkeypatch) -> None:
    _stub_measurements(monkeypatch, healthy_measurements())
    conn = FakeValidationConnection()

    with pytest.raises(ValueError, match="may not be weakened"):
        run_champion_validation(conn, limit=1, threshold_overrides={"minimum_out_of_sample_trades": 1})

    assert conn.statements == []


MIGRATION = Path(__file__).resolve().parents[3] / "database" / "migrations" / "054_champion_validation.sql"


def test_migration_054_is_additive_and_re_appliable() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    # The migrate job re-applies every file on every deploy.
    assert "DELETE FROM" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "ADD COLUMN IF NOT EXISTS validation_state" in sql
    assert "CREATE TABLE IF NOT EXISTS elite_champion_validation_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS elite_champion_validation_gates" in sql
    # `promotion_state` is what the portfolio solver reads. This migration adds
    # an independent axis next to it and must not redefine or re-constrain it.
    assert "elite_research_candidates_promotion_state_check" not in sql
    assert "ADD COLUMN IF NOT EXISTS promotion_state" not in sql


def test_migration_054_covers_every_validation_state_the_service_can_write() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for state in VALIDATION_STATES:
        assert f"'{state}'" in sql
    for status in ("passed", "failed", "inconclusive"):
        assert f"'{status}'" in sql
    # Evidence of a weakened run can never be stored.
    assert "CHECK (thresholds_weakened = FALSE)" in sql
