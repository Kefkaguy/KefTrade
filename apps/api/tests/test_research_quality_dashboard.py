"""Phase F: the research-quality control panel reports process health."""

import pytest

from app.services.research_quality_dashboard import DASHBOARD_VERSION, research_quality_dashboard


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeDashboardConn:
    """Answers only what the dashboard's panels ask for."""

    def __init__(self, *, dataset_id=7, splits=None, access=(), trades=(), confirmations=()):
        self.campaign = {"id": 101, "dataset_id": dataset_id, "status": "completed"}
        self.splits = splits
        self.access = list(access)
        self.trades = list(trades)
        self.confirmations = list(confirmations)

    def execute(self, query, params=None):
        q = " ".join(query.split())

        if q.startswith("CREATE TABLE") or q.startswith("CREATE INDEX"):
            return FakeResult([])
        if q.startswith("SELECT id, dataset_id, status FROM research_campaigns"):
            return FakeResult([self.campaign])
        if q.startswith("SELECT * FROM research_dataset_splits"):
            return FakeResult([self.splits] if self.splits else [])
        if q.startswith("SELECT phase, decision_type"):
            grouped = {}
            for row in self.access:
                key = (row["phase"], row["decision_type"])
                grouped[key] = grouped.get(key, 0) + 1
            return FakeResult(
                [{"phase": p, "decision_type": d, "uses": n} for (p, d), n in sorted(grouped.items())]
            )
        if q.startswith("SELECT COUNT(DISTINCT candidate_id)"):
            return FakeResult([{"variants": 120, "families": 19, "jobs": 1200, "symbols": 10}])
        if q.startswith("SELECT COUNT(*) FILTER"):
            return FakeResult([{"descendants": 0, "distinct_parents": 0, "deepest_generation": 1}])
        if q.startswith("SELECT strategy_architecture, candidate_id, symbol, month_key"):
            return FakeResult(self.trades)
        if q.startswith("SELECT DISTINCT ON (candidate_id)"):
            return FakeResult([])
        if q.startswith("SELECT strategy_architecture, symbol, direction, exit_reason"):
            return FakeResult(self.trades)
        if q.startswith("SELECT * FROM research_confirmation_runs WHERE campaign_id"):
            return FakeResult(self.confirmations)
        if q.startswith("SELECT * FROM research_confirmation_runs"):
            return FakeResult(self.confirmations)
        raise AssertionError(f"unexpected query: {q[:70]}")


def test_the_dashboard_reports_the_simulator_audit_as_a_gate():
    """A broken simulator invalidates every strategy conclusion, so it is the
    first thing the panel establishes."""
    dashboard = research_quality_dashboard(FakeDashboardConn(), 101)

    audit = dashboard["panels"]["simulator_audit"]
    assert audit["simulator_sound"] is True
    assert audit["blocks_all_conclusions"] is False
    assert dashboard["dashboard_version"] == DASHBOARD_VERSION


def test_a_dataset_without_recorded_splits_makes_leakage_checks_unavailable_not_passing():
    """An unavailable check must never read as a clean bill of health."""
    dashboard = research_quality_dashboard(FakeDashboardConn(splits=None), 101)

    leakage = dashboard["panels"]["data_leakage_checks"]
    assert leakage["available"] is False
    assert "no nested splits" in leakage["reason"]
    assert dashboard["trustworthy"] is False
    assert any("data-leakage checks unavailable" in blocker for blocker in dashboard["blockers"])


def test_a_campaign_with_no_dataset_says_so():
    dashboard = research_quality_dashboard(FakeDashboardConn(dataset_id=None), 101)

    assert dashboard["panels"]["data_leakage_checks"]["available"] is False
    assert "no dataset snapshot" in dashboard["panels"]["data_leakage_checks"]["reason"]


def _splits_row():
    from datetime import UTC, datetime

    return {
        "dataset_id": 7,
        "discovery_start": datetime(2026, 1, 1, tzinfo=UTC),
        "discovery_end": datetime(2026, 2, 1, tzinfo=UTC),
        "validation_start": datetime(2026, 2, 2, tzinfo=UTC),
        "validation_end": datetime(2026, 3, 1, tzinfo=UTC),
        "confirmation_start": datetime(2026, 3, 2, tzinfo=UTC),
        "confirmation_end": datetime(2026, 4, 1, tzinfo=UTC),
        "split_version": "nested_research_splits_v1",
    }


def test_heavy_validation_reuse_makes_the_campaign_untrustworthy():
    access = [{"phase": "validation", "decision_type": "candidate_selection"} for _ in range(25)]
    dashboard = research_quality_dashboard(
        FakeDashboardConn(splits=_splits_row(), access=access), 101
    )

    assert dashboard["trustworthy"] is False
    assert any("count as training" in blocker for blocker in dashboard["blockers"])


def test_a_clean_campaign_is_trustworthy():
    dashboard = research_quality_dashboard(FakeDashboardConn(splits=_splits_row()), 101)

    assert dashboard["blockers"] == []
    assert dashboard["trustworthy"] is True


def test_the_multiple_testing_burden_is_reported():
    dashboard = research_quality_dashboard(FakeDashboardConn(splits=_splits_row()), 101)

    burden = dashboard["panels"]["multiple_testing"]["value"]
    assert burden["strategies_tested"] == 120
    assert burden["families_tested"] == 19
    assert burden["effective_independent_hypotheses"] == 1200


def test_panels_without_trade_evidence_report_unavailable():
    dashboard = research_quality_dashboard(FakeDashboardConn(splits=_splits_row()), 101)

    assert dashboard["panels"]["response_surface"]["available"] is False
    assert dashboard["panels"]["loss_diagnostics"]["available"] is False


def _trade(architecture="demo_v2", symbol="NVDA", gross=1.0, fees=1.0, slippage=0.5):
    return {
        "strategy_architecture": architecture,
        "candidate_id": "c1",
        "symbol": symbol,
        "month_key": "2026-01",
        "direction": "long",
        "exit_reason": "take_profit",
        "gross_pnl": gross,
        "fees": fees,
        "slippage_cost": slippage,
        "net_pnl": gross - fees - slippage,
        "holding_period_hours": 3.0,
        "mfe_r": 1.2,
        "mae_r": 0.4,
        "risk_per_unit": 1.0,
        "quantity": 1.0,
        "entry_minutes_from_open": 60,
        "market_regime": None,
        "volatility_regime": None,
    }


def test_families_rejected_because_of_costs_are_named_separately_from_no_edge():
    """The dashboard's whole purpose: 'rejected' is not a finding, but
    'rejected because costs ate a real edge' is."""
    trades = [_trade(gross=1.0, fees=1.0, slippage=0.5) for _ in range(20)]
    dashboard = research_quality_dashboard(
        FakeDashboardConn(splits=_splits_row(), trades=trades), 101
    )

    diagnostics = dashboard["panels"]["loss_diagnostics"]["value"]
    assert diagnostics["rejected_because_of_costs"] == ["demo_v2"]
    assert diagnostics["rejected_for_no_raw_edge"] == []


def test_families_with_no_edge_are_named_separately():
    trades = [_trade(gross=-5.0, fees=0.1, slippage=0.1) for _ in range(20)]
    dashboard = research_quality_dashboard(
        FakeDashboardConn(splits=_splits_row(), trades=trades), 101
    )

    diagnostics = dashboard["panels"]["loss_diagnostics"]["value"]
    assert diagnostics["rejected_for_no_raw_edge"] == ["demo_v2"]
    assert diagnostics["rejected_because_of_costs"] == []


def test_confirmation_outcomes_are_reported():
    confirmations = [
        {"candidate_id": "c1", "passed": True, "campaign_id": 101},
        {"candidate_id": "c2", "passed": False, "campaign_id": 101},
    ]
    dashboard = research_quality_dashboard(
        FakeDashboardConn(splits=_splits_row(), confirmations=confirmations), 101
    )

    panel = dashboard["panels"]["locked_confirmation"]["value"]
    assert panel["candidates_passing_final_confirmation"] == ["c1"]
    assert panel["candidates_failing_final_confirmation"] == ["c2"]


def test_a_missing_campaign_is_rejected():
    conn = FakeDashboardConn()
    conn.campaign = None

    with pytest.raises(ValueError, match="not found"):
        research_quality_dashboard(conn, 101)
