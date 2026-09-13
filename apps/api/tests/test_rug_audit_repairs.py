from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services import research_campaigns as rc
from app.services.production_validation import classify_forward_trade


START = datetime(2026, 8, 3, 14, tzinfo=UTC)


def fill(id, side, price, **kwargs):
    return {"id": id, "order_id": id, "account_id": 1, "symbol": "AAPL", "deployment_id": 1,
            "side": side, "quantity": 1, "fill_price": price, "fee": 0,
            "filled_at": START + timedelta(minutes=id), **kwargs}


@pytest.mark.parametrize("scope", [{"symbol": "AMD"}, {"account_id": 2}, {"deployment_id": 2}, {"deployment_id": None}])
def test_fifo_never_matches_across_owners(scope):
    rows = [fill(1, "buy", 100), fill(2, "buy", 200, **scope),
            fill(3, "sell", 190, **scope), fill(4, "sell", 105)]
    result = rc.closed_trade_attribution(rows)
    assert [t["realized_pnl"] for t in result["closed_trades"]] == [-10, 5]
    assert result["paper_profit_factor"] == .5


def test_short_attribution_and_partial_fees_are_conserved():
    rows = [fill(1, "sell", 100, quantity=1, fee=1, slippage=.2),
            fill(2, "sell", 102, quantity=1, fee=1, slippage=.2),
            fill(3, "buy", 90, quantity=2, fee=2, slippage=.4)]
    result = rc.closed_trade_attribution(rows)
    assert sum(t["realized_pnl"] for t in result["closed_trades"]) == 18
    assert sum(t["commission"] for t in result["closed_trades"]) == 4
    assert sum(t["slippage"] for t in result["closed_trades"]) == pytest.approx(.8)
    assert {t["direction"] for t in result["closed_trades"]} == {"short"}


def test_evidence_does_not_discard_older_losses_after_50_trades():
    rows = []
    for i in range(60):
        rows.extend([fill(i * 2, "buy", 100), fill(i * 2 + 1, "sell", 50 if i < 10 else 101)])
    result = rc.closed_trade_attribution(rows)
    assert len(result["closed_trades"]) == 60
    assert sum(t["realized_pnl"] for t in result["closed_trades"]) == -450


def valid_trade():
    return {"deployment_id": 1, "candidate_id": "rug_a", "campaign_id": 1, "strategy_version": "v1",
            "entry_fill_id": 1, "exit_fill_id": 2, "entry_order_id": 1, "exit_order_id": 2,
            "quantity": 1, "realized_pnl": 10, "deployment_created_at": START,
            "forward_validation_started_at": START + timedelta(minutes=1),
            "signal_timestamp": START + timedelta(minutes=2), "entry_timestamp": START + timedelta(minutes=3),
            "exit_timestamp": START + timedelta(minutes=4), "simulation_only": True,
            "evidence_origin": "candidate_forward_validation", "deployment_lifecycle_state": "active_forward_validation"}


def test_forward_chronology_accepts_later_activation_but_rejects_backfill():
    trade = valid_trade()
    assert classify_forward_trade(trade)["readiness_eligible"]
    for change in ({"signal_timestamp": START}, {"entry_timestamp": START},
                   {"exit_timestamp": START}, {"signal_timestamp": None}, {"forward_validation_started_at": None},
                   {"entry_candle_timestamp": START}, {"entry_evidence_origin": "manual_simulation"}):
        assert not classify_forward_trade({**trade, **change})["readiness_eligible"]


def test_global_refresh_isolates_candidate_failure(monkeypatch):
    class Conn:
        def __init__(self): self.rollbacks = 0; self.commits = 0
        def execute(self, *args): return self
        def fetchall(self): return [{"id": i, "candidate_id": f"c{i}"} for i in (1, 2, 3)]
        @contextmanager
        def transaction(self):
            try: yield
            except Exception:
                self.rollbacks += 1
                raise
        def commit(self): self.commits += 1
    monkeypatch.setattr(rc, "ensure_campaign_tables", lambda conn: None)
    monkeypatch.setattr(rc, "log_exception", lambda *args, **kwargs: None)
    def refresh(conn, elite):
        if elite["id"] == 2: raise ValueError("bad historical record")
        return {"metrics": {}}, "insufficient_forward_sample", []
    monkeypatch.setattr(rc, "refresh_one_elite_forward_evidence", refresh)
    conn = Conn()
    result = rc.refresh_elite_candidate_forward_evidence(conn)
    assert [e["elite_candidate_id"] for e in result["elite_candidates"]] == [1, 3]
    assert result["failed"] == 1 and result["complete"] is False
    assert conn.rollbacks == 1 and conn.commits == 1


def test_drawdown_does_not_interleave_different_account_capital():
    class Conn:
        def execute(self, *args): return self
        def fetchall(self):
            return [{"account_id": 1, "equity": 10000}, {"account_id": 2, "equity": 100},
                    {"account_id": 1, "equity": 9000}, {"account_id": 2, "equity": 105}]
    assert rc.paper_max_drawdown(Conn(), [{"account_id": 1}, {"account_id": 2}]) == .1


def test_forward_gate_cannot_pass_with_unvalued_open_positions():
    metrics = {**rc.empty_paper_metrics(), "simulated_orders": 100, "active_paper_trading_days": 100,
               "closed_trade_count": 100, "paper_expectancy": 100, "paper_profit_factor": 10,
               "valuation_complete": False}
    assert rc.forward_validation_state(metrics, rc.DEFAULT_FORWARD_THRESHOLDS, True) == "insufficient_forward_sample"


def test_versioned_hold_counts_entry_bar_and_preserves_legacy():
    from app.services.backtester import build_market_arrays, find_exit_index
    rows = [{"candle": {"low": Decimal(99), "high": Decimal(101)}} for _ in range(10)]
    kwargs = dict(start_index=1, stop_loss=Decimal(90), take_profit=Decimal(110), max_holding_bars=2)
    assert find_exit_index(rows, build_market_arrays(rows), **kwargs) == (3, "time_exit")
    assert find_exit_index(rows, build_market_arrays(rows), **kwargs, holding_bars_includes_entry=True) == (2, "time_exit")


def test_versioned_symbol_cost_includes_regulatory_cost_without_double_count():
    from app.services.intraday_research_integrity import estimated_round_trip_cost_bps
    model = {"observed_round_trip_bps": 1, "regulatory_bps": 1,
             "by_symbol": {"SPY": {"median_bar_spread_bps": 20}}}
    args = dict(symbol="SPY", timestamp=START, stressed=False)
    assert estimated_round_trip_cost_bps(model, **args) == 20  # legacy replay
    model["methodology"] = {"cost_basis_version": "all_in_symbol_costs_v2"}
    assert estimated_round_trip_cost_bps(model, **args) == 21
    model["observed_round_trip_bps"] = 30
    assert estimated_round_trip_cost_bps(model, **args) == 30
    model["by_symbol"]["SPY"]["observed_round_trip_bps"] = 40
    assert estimated_round_trip_cost_bps(model, **args) == 40


def test_rug_calibration_refuses_pooled_evidence_for_unmeasured_asset():
    from app.services.rug_intraday import load_cost_model
    model = {"id": 1, "feed": "sip", "quote_observations": 10000, "matched_fill_observations": 30,
             "observed_round_trip_bps": 2, "stressed_round_trip_bps": 4,
             "methodology": {"cost_basis_version": "all_in_symbol_costs_v2", "feeds": ["sip"]},
             "by_symbol": {"SPY": {"regular_session_bars": 1000, "matched_fill_observations": 30},
                           "NVDA": {"regular_session_bars": 1, "matched_fill_observations": 0}}}
    class Conn:
        def execute(self, *args): return self
        def fetchone(self): return model
    with pytest.raises(ValueError, match="per symbol"):
        load_cost_model(Conn(), 1, ["SPY", "NVDA"])


def test_rug_diagnostics_do_not_invent_legacy_short_failures():
    from app.services.strategy import BASE_PARAMETERS, StrategyDecision
    from app.services.strategy_diagnostics import enrich_decision
    decision = StrategyDecision("setup", None, Decimal(101), Decimal(98), Decimal(2), ["RUG short setup"], direction="short")
    result = enrich_decision(decision, {"close": 100}, {}, [], {**BASE_PARAMETERS, "strategy_architecture": "rug_v2_intraday"})
    assert result.signal == "setup"
    assert {gate["status"] for gate in result.gates} == {"passed"}


def test_forced_exit_timestamp_and_duration_use_actual_bar_close():
    from app.services.backtester import run_backtest
    from app.services.strategy import BASE_PARAMETERS, StrategyDecision
    from app.services.strategy_discovery import avoid
    rows = [{"symbol": "SPY", "timeframe": "15m", "timestamp": START + timedelta(minutes=i * 15),
             "open": Decimal(100), "high": Decimal(101), "low": Decimal(99), "close": Decimal(100),
             "volume": Decimal(1000)} for i in range(100)]
    features = [{"timestamp": row["timestamp"]} for row in rows]
    def strategy(candle, *args):
        if candle["timestamp"] != rows[70]["timestamp"]: return avoid("not signal")
        return StrategyDecision("setup", None, Decimal(90), Decimal(120), Decimal(2), ["fixture"])
    result = run_backtest(rows, features, {**BASE_PARAMETERS, "fee_rate": 0, "slippage_rate": 0,
                          "max_holding_bars": 2, "holding_bar_convention": "entry_bar_is_one",
                          "exit_timestamp_convention": "bar_close_for_forced_exits"}, strategy)
    trade = result["trades"][0]
    assert trade["exit_reason"] == "time_exit"
    assert trade["exit_time"] == rows[72]["timestamp"] + timedelta(minutes=15)
    assert trade["holding_period_hours"] == .5


def test_pilot_learns_only_from_complete_valid_candidate_cohorts(monkeypatch):
    from app.cli import rug_research as cli
    from app.services.rug_intraday import generate_candidates
    candidates = generate_candidates(max_candidates=2, seed=4)[0]
    seen_evidence = []
    def generate(**kwargs):
        seen_evidence.append(list(kwargs["evidence"]))
        return [candidates[kwargs["batch_index"]]], {}
    monkeypatch.setattr(cli, "generate_candidates", generate)
    def evaluate(candidate, dataset):
        if candidate == candidates[0] and dataset == "bad": raise ValueError("invalid bars")
        return {"valid_economic_test": True, "screen_passed": False, "failure_reasons": ["baseline_costs_development_fold_failed"]}
    monkeypatch.setattr(cli, "evaluate", evaluate)
    events = []
    result = cli.run_pilot({"AAPL": "good", "AMD": "bad"}, seed=4, batches=2, batch_size=1,
                           cost_model=None, emit=events.append)
    assert seen_evidence == [[], []]
    assert result["technical_errors"] == 1
    assert result["valid_learning_candidates"] == 1
    assert result["certified_strategies"] == 0
    assert result["database_updated"] is False
