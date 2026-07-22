from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.elite_shadow_replay import (
    apply_portfolio_arbitration,
    replay_decisions,
    replay_summary,
    simulate_calculated_risk,
)
from app.services.research_campaigns import aggregate_snapshot_overview
from app.services.strategy import StrategyDecision


RISK_CONFIG = {
    "allocated_capital": Decimal("10000"),
    "deterministic_risk_cap_pct": Decimal("0.01"),
    "model_shadow_risk_cap_pct": Decimal("0.005"),
    "portfolio_strategy_cap_pct": Decimal("0.006"),
    "total_exposure_cap_pct": Decimal("0.03"),
    "active_elites": 5,
}


def test_calculated_risk_applies_model_portfolio_and_notional_bounds() -> None:
    result = simulate_calculated_risk(
        reference_price=Decimal("100"),
        stop_price=Decimal("97"),
        config=RISK_CONFIG,
    )

    assert result["risk_cap_pct"] == Decimal("0.005")
    assert result["quantity"] == 3
    assert result["expected_risk"] == Decimal("9")
    assert result["risk_pct"] == Decimal("0.0009")
    assert result["would_submit"] is True


def test_replay_preserves_strategy_signal_and_produces_explainable_setup() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        {"symbol": "TEST", "timeframe": "1h", "timestamp": started, "open": 90, "high": 91, "low": 89, "close": 90},
        {"symbol": "TEST", "timeframe": "1h", "timestamp": started + timedelta(hours=1), "open": 100, "high": 102, "low": 99, "close": 100},
    ]
    features = [
        {"timestamp": started, "ema_20": 95, "ema_50": 100, "rsi_14": 40, "volume_change": -0.1},
        {"timestamp": started + timedelta(hours=1), "ema_20": 98, "ema_50": 95, "rsi_14": 55, "volume_change": 0.2},
    ]

    def decide(candle, _feature, _recent, _params):
        if Decimal(str(candle["close"])) < Decimal("100"):
            return StrategyDecision("avoid", None, None, None, None, ["Trend block failed."])
        return StrategyDecision(
            "setup",
            (Decimal("99"), Decimal("102")),
            Decimal("97"),
            Decimal("106"),
            Decimal("2"),
            ["All deterministic blocks passed."],
        )

    rows = replay_decisions(
        decide=decide,
        candles=candles,
        features=features,
        params={"ema_fast": 20, "ema_slow": 50},
        risk_config=RISK_CONFIG,
        candle_limit=10,
    )

    assert [row["signal_type"] for row in rows] == ["avoid", "setup"]
    assert rows[0]["would_submit"] is False
    assert "TREND_PRICE_ABOVE_SLOW" in rows[0]["rejection_reasons"]
    assert rows[1]["would_submit"] is True
    assert rows[1]["simulated_quantity"] == 3
    assert rows[1]["decision"]["calculated_risk_shadow"]["broker_mutation"] is False


def test_same_symbol_arbitration_selects_highest_research_score() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        replay_row(2, "AAXJ", timestamp, Decimal("5.5")),
        replay_row(1, "AAXJ", timestamp, Decimal("6.1")),
        replay_row(3, "AAAU", timestamp, Decimal("4.0")),
    ]

    apply_portfolio_arbitration(rows, RISK_CONFIG)

    assert rows[1]["would_submit"] is True
    assert rows[0]["would_submit"] is False
    assert rows[0]["rejection_reasons"] == ["SAME_SYMBOL_HIGHER_RANKED_WINNER"]
    assert rows[2]["would_submit"] is True


def test_replay_summary_reports_opportunity_and_gate_frequency() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    setup = replay_row(1, "AAXJ", timestamp, Decimal("6"))
    rejected = replay_row(2, "AAAU", timestamp, Decimal("5"))
    rejected.update({
        "signal_type": "avoid",
        "would_submit": False,
        "gates": [{"code": "TREND_FAST_ABOVE_SLOW", "status": "failed"}],
        "rejection_reasons": ["TREND_FAST_ABOVE_SLOW"],
    })

    summary = replay_summary([setup, rejected])

    assert summary["evaluations"] == 2
    assert summary["setups"] == 1
    assert summary["would_submit_true"] == 1
    assert summary["opportunity_frequency"] == 0.5
    assert summary["failed_gates"][0]["code"] == "TREND_FAST_ABOVE_SLOW"
    assert summary["broker_mutation"] is False


def test_command_center_snapshot_uses_authoritative_exclusive_counts() -> None:
    expected = {
        "campaign_jobs": 41676,
        "candidates_generated": 2111,
        "candidates_tested": 2111,
        "candidates_rejected": 2037,
        "candidates_completed": 2111,
        "needs_more_evidence": 0,
        "research_candidates": 69,
        "elite_candidates": 5,
        "candidate_linked_deployments": 5,
    }
    conn = CountConnection(expected)

    result = aggregate_snapshot_overview(conn, [{"id": 16}, {"id": 28}])

    assert result == expected
    assert "elite_research_candidates" in conn.query
    assert "strategy_deployments" in conn.query
    assert "candidate_status.promoted AND elite_ids.candidate_id IS NULL" in conn.query


def replay_row(deployment_id: int, symbol: str, timestamp: datetime, score: Decimal) -> dict:
    return {
        "external_deployment_id": deployment_id,
        "symbol": symbol,
        "completed_bar_timestamp": timestamp,
        "research_score": score,
        "signal_type": "setup",
        "reference_price": Decimal("100"),
        "stop_price": Decimal("98"),
        "would_submit": True,
        "simulated_quantity": 1,
        "simulated_expected_risk": Decimal("2"),
        "simulated_risk_pct": Decimal("0.0002"),
        "rejection_reasons": [],
        "gates": [],
        "decision": {},
    }


class CountResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class CountConnection:
    def __init__(self, row):
        self.row = row
        self.query = ""

    def execute(self, query, _params=None):
        self.query = query
        return CountResult(self.row)
