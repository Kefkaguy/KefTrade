from datetime import UTC, datetime

from app.services.broker_read_models import elite_observability, opportunity_coverage
from app.workers.broker_runner import cycle_result_summary


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ObservabilityConn:
    def __init__(self, submitted_attempts: int = 0):
        self.submitted_attempts = submitted_attempts

    def execute(self, query, params=()):
        if "FROM external_paper_deployments x" in query:
            return Result(
                [
                    {
                        "id": 3,
                        "candidate_id": "elite-aaau",
                        "symbol": "AAAU",
                        "timeframe": "4h",
                        "state": "enabled_observe_only",
                        "evaluations_today": 1,
                        "setups_today": 0,
                        "avoids_today": 1,
                        "shadow_decisions_today": 1,
                        "would_submit_today": 0,
                        "execution_attempts_today": self.submitted_attempts,
                        "submitted_attempts_today": self.submitted_attempts,
                    }
                ]
            )
        if "FROM elite_shadow_replay_runs" in query:
            return Result(
                [
                    {
                        "id": 2,
                        "completed_at": datetime.now(UTC),
                        "outcome_summary": {
                            "by_deployment": {
                                "3": {"completed_trades": 75, "profit_factor": 1.2558, "net_pnl": 47.50, "health": "healthy"}
                            }
                        },
                    }
                ]
            )
        raise AssertionError(query)


def test_elite_observability_separates_today_from_historical_replay() -> None:
    row = elite_observability(ObservabilityConn())[0]

    assert row["today_performance"]["realized_pnl"] == 0.0
    assert row["today_performance"]["attribution_status"] == "observation_only_no_paper_trades"
    assert row["historical_replay"]["profit_factor"] == 1.2558
    assert row["historical_replay"]["net_pnl"] == 47.50


def test_elite_observability_does_not_invent_pnl_for_submitted_orders() -> None:
    conn = ObservabilityConn(submitted_attempts=1)
    original_execute = conn.execute

    def execution_enabled(query, params=()):
        result = original_execute(query, params)
        if "FROM external_paper_deployments x" in query:
            result.rows[0]["state"] = "enabled_execution"
        return result

    conn.execute = execution_enabled
    row = elite_observability(conn)[0]

    assert row["today_performance"]["realized_pnl"] is None
    assert row["today_performance"]["attribution_status"] == "awaiting_broker_lifecycle_attribution"


def test_cycle_log_summary_includes_failed_gates_for_duplicate_cycles() -> None:
    summary = cycle_result_summary(
        3,
        {
            "status": "duplicate_skipped",
            "strategy_evaluation": {
                "external_deployment_id": 3,
                "signal_type": "avoid",
                "completed_bar_timestamp": datetime(2026, 7, 21, 16, tzinfo=UTC),
                "gates": [
                    {"code": "RSI_MIN", "status": "failed"},
                    {"code": "VOLUME_MIN", "status": "passed"},
                ],
            },
        },
    )

    assert summary["deployment_id"] == 3
    assert summary["signal"] == "avoid"
    assert summary["rejection_reasons"] == ["RSI_MIN"]
    assert summary["broker_mutation"] is False


def test_opportunity_coverage_reports_symbol_concentration_without_enabling_shorts() -> None:
    coverage = opportunity_coverage(
        [
            {"state": "enabled_observe_only", "symbol": "AAXJ", "timeframe": "1h", "evaluations_today": 1, "setups_today": 0},
            {"state": "enabled_observe_only", "symbol": "AAXJ", "timeframe": "4h", "evaluations_today": 1, "setups_today": 0},
            {"state": "enabled_observe_only", "symbol": "AAXJ", "timeframe": "4h", "evaluations_today": 1, "setups_today": 0},
            {"state": "enabled_observe_only", "symbol": "AAXJ", "timeframe": "1h", "evaluations_today": 1, "setups_today": 0},
            {"state": "enabled_observe_only", "symbol": "AAAU", "timeframe": "4h", "evaluations_today": 1, "setups_today": 0},
        ]
    )

    assert coverage["classification"] == "concentrated_long_only"
    assert coverage["unique_symbols"] == 2
    assert coverage["dominant_symbol"] == "AAXJ"
    assert coverage["dominant_symbol_share"] == 0.8
    assert coverage["setup_frequency_today"] == 0.0
    assert coverage["external_short_execution_enabled"] is False
