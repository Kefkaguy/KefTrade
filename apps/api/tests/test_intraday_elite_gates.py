from datetime import UTC, date, datetime, timedelta

from app.services.intraday_elite_gates import (
    REQUIRED_EVIDENCE_REFERENCES,
    FamilyRecipe,
    evaluate_live_safeguards,
    execution_semantics_report,
    fill_calibration_report,
    qualify_elite,
    robustness_report,
)


def trade(**overrides):
    base = {
        "symbol": "AAPL",
        "side": "long",
        "decision_timestamp": datetime(2025, 3, 3, 15, 0, tzinfo=UTC),
        "entry_timestamp": datetime(2025, 3, 3, 15, 0, tzinfo=UTC),
        "entry_price": 100.05,
        "signal_close": 99.90,
        "bid": 99.95,
        "ask": 100.05,
        "cost_bps": 6.0,
        "net_return": 0.0012,
        "entry_session_date": date(2025, 3, 3),
        "exit_session_date": date(2025, 3, 3),
        "participation_rate": 0.002,
        "execution_evidence_present": True,
    }
    base.update(overrides)
    return base


def spread_of_trades(count=40, **overrides):
    trades = []
    for index in range(count):
        session = date(2025, 1, 6) + timedelta(days=index * 7)
        trades.append(
            trade(
                symbol=f"SYM{index % 8}",
                entry_session_date=session,
                exit_session_date=session,
                **overrides,
            )
        )
    return trades


def test_execution_report_rejects_a_fill_at_the_signal_close():
    report = execution_semantics_report([trade(entry_price=99.90)])

    assert report["checks"]["entry_is_not_the_signal_close"] is False
    assert report["passed"] is False


def test_execution_report_rejects_a_decision_taken_after_entry():
    report = execution_semantics_report(
        [trade(decision_timestamp=datetime(2025, 3, 3, 16, 0, tzinfo=UTC))]
    )

    assert report["checks"]["decision_precedes_entry"] is False


def test_execution_report_rejects_the_wrong_side_of_the_spread():
    report = execution_semantics_report([trade(side="long", entry_price=99.95)])

    assert report["checks"]["spread_side_respected"] is False


def test_execution_report_rejects_an_uncharged_trade():
    report = execution_semantics_report([trade(cost_bps=0)])

    assert report["checks"]["costs_charged_on_every_trade"] is False


def test_execution_report_rejects_an_undeclared_overnight_position():
    report = execution_semantics_report([trade(exit_session_date=date(2025, 3, 4))])

    assert report["checks"]["no_undeclared_overnight_positions"] is False


def test_execution_report_rejects_a_manufactured_fill():
    report = execution_semantics_report([trade(execution_evidence_present=False)])

    assert report["checks"]["no_fill_without_execution_evidence"] is False
    assert report["passed"] is False


def test_execution_report_accepts_a_clean_trade_set():
    report = execution_semantics_report(spread_of_trades())

    assert report["passed"] is True


def test_robustness_rejects_an_edge_that_dies_under_cost_stress():
    report = robustness_report(spread_of_trades(net_return=0.0002, cost_bps=8.0))

    assert report["checks"]["positive_mean_net_return"] is True
    assert report["checks"]["survives_cost_stress"] is False
    assert report["passed"] is False


def test_robustness_rejects_profit_concentrated_in_one_symbol():
    trades = spread_of_trades(count=20, net_return=0.00001)
    trades.append(trade(symbol="MEGA", net_return=0.5, entry_session_date=date(2025, 9, 1),
                        exit_session_date=date(2025, 9, 1)))

    report = robustness_report(trades)

    assert report["checks"]["no_single_symbol_majority"] is False


def test_robustness_rejects_an_edge_that_needs_its_best_symbol():
    trades = spread_of_trades(count=12, net_return=-0.0002)
    trades.append(trade(symbol="MEGA", net_return=0.9, entry_session_date=date(2025, 9, 1),
                        exit_session_date=date(2025, 9, 1)))

    report = robustness_report(trades)

    assert report["checks"]["survives_best_symbol_removal"] is False


def test_robustness_rejects_an_unrealistic_participation_rate():
    report = robustness_report(spread_of_trades(participation_rate=0.25))

    assert report["checks"]["within_participation_limit"] is False


def test_robustness_accepts_a_diversified_profitable_set():
    report = robustness_report(spread_of_trades(net_return=0.0030, cost_bps=4.0))

    assert report["passed"] is True


def fill(**overrides):
    base = {
        "side": "long",
        "midpoint_at_decision": 100.0,
        "filled_price": 100.02,
        "bid": 99.98,
        "ask": 100.02,
        "partial_fill": False,
        "status": "filled",
        "session_date": date(2025, 3, 3),
    }
    base.update(overrides)
    return base


def test_fill_calibration_requires_enough_matched_fills():
    report = fill_calibration_report([fill()] * 5, confirmed_gross_edge_bps=40.0)

    assert report["checks"]["sufficient_matched_fills"] is False
    assert report["passed"] is False


def test_fill_calibration_rejects_costs_that_exceed_the_confirmed_edge():
    fills = [
        fill(session_date=date(2025, 3, 3) + timedelta(days=index), filled_price=100.30)
        for index in range(40)
    ]

    report = fill_calibration_report(fills, confirmed_gross_edge_bps=20.0)

    assert report["p90_round_trip_cost_bps"] > 20.0
    assert report["checks"]["p90_cost_below_confirmed_gross_edge"] is False
    assert report["checks"]["execution_does_not_reverse_expectancy"] is False


def test_fill_calibration_flags_a_signal_frequency_that_does_not_match_research():
    fills = [fill(session_date=date(2025, 3, 3)) for _ in range(40)]

    report = fill_calibration_report(
        fills, confirmed_gross_edge_bps=200.0, research_signals_per_session=1.0
    )

    assert report["checks"]["signal_frequency_matches_research"] is False


def test_fill_calibration_passes_when_execution_preserves_the_edge():
    fills = [
        fill(session_date=date(2025, 3, 3) + timedelta(days=index)) for index in range(40)
    ]

    report = fill_calibration_report(
        fills, confirmed_gross_edge_bps=60.0, research_signals_per_session=1.0
    )

    assert report["passed"] is True
    assert report["net_edge_after_observed_cost_bps"] > 0


def test_family_recipe_hash_changes_with_any_frozen_field():
    def recipe(**overrides):
        base = {
            "factor_key": "gap_down_absorption_reversal",
            "entry_condition": "absorption at 10:00",
            "direction": "long",
            "holding_bars": 1,
            "stop_loss": "none",
            "forced_session_close_exit": True,
            "max_concurrent_positions": 5,
            "position_size_fraction": 0.02,
            "max_gross_exposure": 0.10,
            "eligible_symbols": ("AAPL", "MSFT"),
            "eligible_session_slots": ("10:30",),
            "cost_calibration_id": 3,
        }
        base.update(overrides)
        return FamilyRecipe(**base)

    assert recipe().recipe_hash() == recipe().recipe_hash()
    assert recipe().recipe_hash() != recipe(holding_bars=2).recipe_hash()
    assert recipe().recipe_hash() != recipe(position_size_fraction=0.05).recipe_hash()


def full_evidence():
    return {name: index + 1 for index, name in enumerate(REQUIRED_EVIDENCE_REFERENCES)}


def test_elite_qualification_requires_the_whole_evidence_chain():
    evidence = full_evidence()
    del evidence["confirmation_run_id"]

    verdict = qualify_elite(
        evidence=evidence,
        discovery_passed=True,
        confirmation_passed=True,
        quality_report={"ready_for_discovery": True, "power_passed": True},
        execution_report={"passed": True},
        robustness={"passed": True, "checks": {"survives_cost_stress": True}},
        fill_calibration={"passed": True},
        risk_approved=True,
    )

    assert verdict["qualified"] is False
    assert verdict["missing_evidence_references"] == ["confirmation_run_id"]


def test_elite_qualification_fails_without_locked_confirmation():
    verdict = qualify_elite(
        evidence=full_evidence(),
        discovery_passed=True,
        confirmation_passed=False,
        quality_report={"ready_for_discovery": True, "power_passed": True},
        execution_report={"passed": True},
        robustness={"passed": True, "checks": {"survives_cost_stress": True}},
        fill_calibration={"passed": True},
        risk_approved=True,
    )

    assert verdict["qualified"] is False
    assert "locked_confirmation" in verdict["failed_gates"]


def test_elite_qualification_fails_on_an_underpowered_dataset():
    verdict = qualify_elite(
        evidence=full_evidence(),
        discovery_passed=True,
        confirmation_passed=True,
        quality_report={"ready_for_discovery": False, "power_passed": False},
        execution_report={"passed": True},
        robustness={"passed": True, "checks": {"survives_cost_stress": True}},
        fill_calibration={"passed": True},
        risk_approved=True,
    )

    assert verdict["qualified"] is False
    assert "powered_discovery" in verdict["failed_gates"]


def test_elite_qualification_passes_only_with_every_stage():
    verdict = qualify_elite(
        evidence=full_evidence(),
        discovery_passed=True,
        confirmation_passed=True,
        quality_report={"ready_for_discovery": True, "power_passed": True},
        execution_report={"passed": True},
        robustness={"passed": True, "checks": {"survives_cost_stress": True}},
        fill_calibration={"passed": True},
        risk_approved=True,
    )

    assert verdict["qualified"] is True
    assert verdict["failed_gates"] == []


def live_state(**overrides):
    base = {
        "market_data_age_seconds": 30,
        "expected_session_date": date(2025, 3, 3),
        "observed_session_date": date(2025, 3, 3),
        "observed_spread_bps": 5.0,
        "observed_slippage_bps": 3.0,
        "signals_per_session": 2.0,
        "realized_edge_bps": 20.0,
        "realized_cost_bps": 8.0,
        "drawdown": -0.02,
        "rejection_rate": 0.0,
        "fill_anomaly": None,
        "recipe_hash": "abc",
    }
    base.update(overrides)
    return base


LIMITS = {
    "max_market_data_age_seconds": 300,
    "max_spread_bps": 15.0,
    "max_slippage_bps": 10.0,
    "expected_signals_per_session": 2.0,
    "max_drawdown": 0.10,
    "max_rejection_rate": 0.05,
    "expected_recipe_hash": "abc",
}


def test_live_safeguards_are_quiet_when_everything_is_normal():
    result = evaluate_live_safeguards(live_state(), LIMITS)

    assert result["pause_required"] is False
    assert result["triggered"] == {}


def test_stale_market_data_pauses():
    result = evaluate_live_safeguards(live_state(market_data_age_seconds=1200), LIMITS)

    assert result["pause_required"] is True
    assert "stale_market_data" in result["triggered"]


def test_a_calendar_mismatch_pauses():
    result = evaluate_live_safeguards(
        live_state(observed_session_date=date(2025, 3, 4)), LIMITS
    )

    assert "calendar_session_mismatch" in result["triggered"]


def test_realized_edge_below_cost_pauses():
    result = evaluate_live_safeguards(
        live_state(realized_edge_bps=4.0, realized_cost_bps=9.0), LIMITS
    )

    assert "realized_edge_below_cost" in result["triggered"]


def test_a_version_mismatch_pauses():
    result = evaluate_live_safeguards(live_state(recipe_hash="changed"), LIMITS)

    assert "version_mismatch" in result["triggered"]


def test_signal_frequency_outside_bounds_pauses():
    result = evaluate_live_safeguards(live_state(signals_per_session=12.0), LIMITS)

    assert "signal_frequency_out_of_bounds" in result["triggered"]
