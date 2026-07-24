import pytest

from app.services.labs.intraday.phase_analysis import (
    MINIMUM_EVIDENCE_RULES,
    _annotate_stability,
    _bucket_entry_time,
    _bucket_relative_volume,
    _dominance_share,
    _subgroup_rows,
    classify_family,
    compute_group_metrics,
    cost_and_sizing_analysis,
    entry_quality_analysis,
    exit_reason_breakdown,
    performance_decomposition,
    research_allocation,
    stability_subgroups,
)


def make_trade(**overrides):
    trade = {
        "symbol": "AMD",
        "timeframe": "30m",
        "direction": "long",
        "candidate_id": "c1",
        "entry_time": None,
        "exit_time": None,
        "entry_price": 100.0,
        "exit_price": 95.0,
        "quantity": 10.0,
        "stop_loss": 95.0,
        "take_profit": 115.0,
        "risk_per_unit": 5.0,
        "gross_pnl": -50.0,
        "fees": 5.0,
        "slippage_cost": 1.0,
        "net_pnl": -56.0,
        "pnl_pct": -0.0056,
        "exit_reason": "stop_loss",
        "holding_period_hours": 1.0,
        "mfe_amount": 30.0,
        "mae_amount": 50.0,
        "mfe_r": 6.0,
        "mae_r": 10.0,
        "bars_to_mfe": 3,
        "bars_to_mae": 5,
        "entry_minutes_from_open": 20,
        "entry_minutes_to_close": 370,
        "entry_session_relative_volume": 0.8,
        "entry_gap_percent": 0.005,
        "market_regime": "unknown",
        "volatility_regime": "unknown",
        "month_key": "2026-01",
    }
    trade.update(overrides)
    return trade


def four_trade_fixture():
    trade1 = make_trade()  # AMD 30m long, stop_loss, month 2026-01
    trade2 = make_trade(
        exit_price=115.0, gross_pnl=150.0, fees=5.0, slippage_cost=1.0, net_pnl=144.0, pnl_pct=0.0144,
        exit_reason="take_profit", mfe_amount=150.0, mae_amount=0.0, mfe_r=30.0, mae_r=0.0,
        bars_to_mfe=8, bars_to_mae=0, entry_minutes_from_open=45, entry_minutes_to_close=345,
        entry_session_relative_volume=1.2, entry_gap_percent=0.01, holding_period_hours=2.0,
    )
    trade3 = make_trade(
        symbol="NVDA", timeframe="15m", candidate_id="c2", month_key="2026-02",
        entry_price=200.0, exit_price=198.0, quantity=5.0, stop_loss=190.0, take_profit=230.0, risk_per_unit=10.0,
        gross_pnl=-10.0, fees=2.0, slippage_cost=0.5, net_pnl=-12.5, pnl_pct=-0.00125,
        exit_reason="session_close", mfe_amount=10.0, mae_amount=20.0, mfe_r=1.0, mae_r=2.0,
        bars_to_mfe=1, bars_to_mae=2, entry_minutes_from_open=200, entry_minutes_to_close=190,
        entry_session_relative_volume=1.6, entry_gap_percent=0.0, holding_period_hours=3.5,
    )
    trade4 = make_trade(
        symbol="NVDA", timeframe="15m", direction="short", candidate_id="c2", month_key="2026-02",
        entry_price=200.0, exit_price=205.0, quantity=5.0, stop_loss=205.0, take_profit=170.0, risk_per_unit=5.0,
        gross_pnl=-25.0, fees=2.0, slippage_cost=0.5, net_pnl=-27.5, pnl_pct=-0.00275,
        exit_reason="stop_loss", mfe_amount=5.0, mae_amount=25.0, mfe_r=1.0, mae_r=5.0,
        bars_to_mfe=0, bars_to_mae=0, entry_minutes_from_open=250, entry_minutes_to_close=140,
        entry_session_relative_volume=0.5, entry_gap_percent=-0.01, holding_period_hours=0.5,
    )
    return [trade1, trade2, trade3, trade4]


def test_compute_group_metrics_separates_gross_and_net_correctly():
    metrics = compute_group_metrics(four_trade_fixture())

    assert metrics["trade_count"] == 4
    assert metrics["gross_profit"] == pytest.approx(150.0)
    assert metrics["gross_loss"] == pytest.approx(85.0)
    assert metrics["gross_profit_factor"] == pytest.approx(150 / 85, rel=1e-3)
    assert metrics["net_profit_factor"] == pytest.approx(144 / 96, rel=1e-3)
    assert metrics["win_rate"] == pytest.approx(0.25)
    assert metrics["average_win"] == pytest.approx(144.0)
    assert metrics["average_loss"] == pytest.approx(32.0)
    assert metrics["payoff_ratio"] == pytest.approx(144 / 32, rel=1e-3)
    assert metrics["gross_expectancy"] == pytest.approx(65 / 4)
    assert metrics["net_expectancy"] == pytest.approx(48 / 4)
    assert metrics["fees"] == pytest.approx(14.0)
    assert metrics["slippage_cost"] == pytest.approx(3.0)
    assert metrics["total_transaction_costs"] == pytest.approx(17.0)


def test_compute_group_metrics_handles_empty_and_all_winning_trades():
    empty = compute_group_metrics([])
    assert empty["trade_count"] == 0
    assert empty["gross_profit_factor"] is None
    assert empty["net_profit_factor"] is None

    all_winners = [make_trade(gross_pnl=100.0, net_pnl=90.0, fees=10.0, slippage_cost=0.0, exit_reason="take_profit")]
    metrics = compute_group_metrics(all_winners)
    assert metrics["gross_profit_factor"] == float("inf")
    assert metrics["net_profit_factor"] == float("inf")
    assert metrics["average_loss"] == 0.0
    assert metrics["payoff_ratio"] is None


def test_performance_decomposition_classifies_weak_gross_edge_destroyed_by_costs():
    # Gross profit factor >= 1 but net profit factor < 1: costs destroyed a real edge.
    trades = [
        make_trade(gross_pnl=100.0, fees=60.0, slippage_cost=0.0, net_pnl=40.0, exit_reason="take_profit"),
        make_trade(gross_pnl=-90.0, fees=5.0, slippage_cost=0.0, net_pnl=-95.0, exit_reason="stop_loss"),
    ]
    jobs = [{"metrics": {"profit_factor": 1.1, "expectancy_per_trade": 5.0}}]

    result = performance_decomposition(trades, jobs)

    assert result["gross_profit_factor"] == pytest.approx(100 / 90, rel=1e-3)
    assert result["net_profit_factor"] == pytest.approx(40 / 95, rel=1e-3)
    assert result["verdict"] == "weak_gross_edge_destroyed_by_costs"
    assert result["median_job_profit_factor"] == 1.1


def test_performance_decomposition_classifies_already_unprofitable_before_costs():
    trades = [
        make_trade(gross_pnl=-10.0, fees=1.0, slippage_cost=0.0, net_pnl=-11.0, exit_reason="stop_loss"),
        make_trade(gross_pnl=-20.0, fees=1.0, slippage_cost=0.0, net_pnl=-21.0, exit_reason="stop_loss"),
        make_trade(gross_pnl=5.0, fees=1.0, slippage_cost=0.0, net_pnl=4.0, exit_reason="take_profit"),
    ]

    result = performance_decomposition(trades, [])

    assert result["gross_profit_factor"] < 1.0
    assert result["verdict"] == "already_unprofitable_before_costs"


def test_exit_reason_breakdown_groups_and_aggregates_correctly():
    rows = exit_reason_breakdown(four_trade_fixture())
    by_reason = {row["exit_reason"]: row for row in rows}

    assert by_reason["stop_loss"]["trade_count"] == 2
    assert by_reason["stop_loss"]["pct_of_trades"] == pytest.approx(0.5)
    assert by_reason["take_profit"]["trade_count"] == 1
    assert by_reason["session_close"]["trade_count"] == 1
    assert by_reason["stop_loss"]["net_pnl"] == pytest.approx(-56.0 + -27.5)
    assert by_reason["take_profit"]["average_mfe"] == pytest.approx(150.0)


def test_entry_quality_analysis_flags_immediate_adverse_moves_and_target_reach():
    result = entry_quality_analysis(four_trade_fixture())

    # trade2 and trade4 both have bars_to_mae <= 0 -> 1 of them (trade4, bars_to_mae=0) is "immediate";
    # trade2 also has bars_to_mae=0 -> both trade2 and trade4 count as immediate.
    assert result["pct_moved_against_position_immediately"] == pytest.approx(2 / 4)
    # trade2: mfe_amount/quantity = 150/10 = 15 == target_distance (115-100) -> reached.
    assert result["pct_mfe_reached_intended_target_distance"] == pytest.approx(1 / 4)
    assert "pre_entry_price_movement" in result["insufficient_evidence"]
    assert len(result["by_entry_time_bucket"]) == len(entry_quality_analysis(four_trade_fixture())["by_entry_time_bucket"])


def test_cost_and_sizing_analysis_computes_stop_target_distances_and_realized_r():
    trades = four_trade_fixture()
    result = cost_and_sizing_analysis(trades)

    assert result["trade_count"] == 4
    assert result["average_fees_per_trade"] == pytest.approx((5 + 5 + 2 + 2) / 4)
    assert result["average_stop_distance"] == pytest.approx((5 + 5 + 10 + 5) / 4)
    assert result["average_target_distance"] == pytest.approx((15 + 15 + 30 + 30) / 4)
    assert result["loss_by_stop_distance_bucket"]
    assert sum(row["trade_count"] for row in result["loss_by_stop_distance_bucket"]) == 4


def test_bucket_entry_time_boundaries():
    assert _bucket_entry_time(0) == "0-30m"
    assert _bucket_entry_time(29) == "0-30m"
    assert _bucket_entry_time(30) == "30-60m"
    assert _bucket_entry_time(119) == "60-120m"
    assert _bucket_entry_time(240) == "240m+"
    assert _bucket_entry_time(None) == "unknown"


def test_bucket_relative_volume_boundaries():
    assert _bucket_relative_volume(0.5) == "below_average (<1.0x)"
    assert _bucket_relative_volume(1.0) == "average (1.0x-1.5x)"
    assert _bucket_relative_volume(1.6) == "elevated (>1.5x)"
    assert _bucket_relative_volume(None) == "unknown"


def test_subgroup_rows_generates_one_row_per_distinct_key():
    trades = four_trade_fixture()
    rows = _subgroup_rows(trades, lambda t: t["symbol"])
    keys = {row["key"] for row in rows}

    assert keys == {"AMD", "NVDA"}
    amd_row = next(row for row in rows if row["key"] == "AMD")
    assert amd_row["trade_count"] == 2


def test_annotate_stability_flags_low_trade_count_and_negative_expectancy():
    rows = [{"key": "AMD", "trade_count": 5, "net_profit_factor": 0.5, "net_expectancy": -10.0}]

    annotated = _annotate_stability(rows, dimension="symbol", min_trades=20)

    assert annotated[0]["meets_minimum_evidence"] is False
    assert any("trade_count" in reason for reason in annotated[0]["stability_notes"])
    assert any("net_profit_factor" in reason for reason in annotated[0]["stability_notes"])


def test_annotate_stability_passes_a_genuinely_strong_subgroup():
    rows = [{"key": "AMD", "trade_count": 25, "net_profit_factor": 1.4, "net_expectancy": 12.0}]

    annotated = _annotate_stability(rows, dimension="symbol", min_trades=20)

    assert annotated[0]["meets_minimum_evidence"] is True
    assert annotated[0]["stability_notes"] == []


def test_dominance_share_identifies_the_single_dominant_key():
    values = {"AMD": 100.0, "NVDA": -20.0}
    dominant_key, share = _dominance_share(values, total_abs=120.0)

    assert dominant_key == "AMD"
    assert share == pytest.approx(100 / 120, abs=1e-4)


def test_dominance_share_handles_zero_total():
    dominant_key, share = _dominance_share({}, total_abs=0.0)
    assert dominant_key is None
    assert share == 0.0


def test_stability_subgroups_detects_symbol_and_month_dominance():
    # AMD trades net = -56 + 144 = 88; NVDA trades net = -12.5 - 27.5 = -40.
    # total_abs is the sum of each trade's own |net_pnl| (56+144+12.5+27.5=240),
    # not the sum of per-symbol group magnitudes.
    result = stability_subgroups(four_trade_fixture())

    dominance = result["campaign_level_dominance"]
    assert dominance["distinct_symbols"] == 2
    assert dominance["distinct_months"] == 2
    assert dominance["dominant_symbol"] == "AMD"
    assert dominance["dominant_symbol_share_of_net_pnl"] == pytest.approx(88 / 240, rel=1e-3)
    assert dominance["meets_symbol_diversity_requirement"] is True
    assert dominance["meets_month_diversity_requirement"] is True

    by_month_keys = {row["key"] for row in result["by_month"]}
    assert by_month_keys == {"2026-01", "2026-02"}
    by_symbol_keys = {row["key"] for row in result["by_symbol"]}
    assert by_symbol_keys == {"AMD", "NVDA"}


def test_stability_subgroups_regime_breakdown_is_marked_insufficient_evidence():
    result = stability_subgroups(four_trade_fixture())
    assert "insufficient_evidence" in result["by_market_regime"]


def test_classify_family_flags_no_directional_edge_for_a_weak_losing_family():
    trades = [
        make_trade(gross_pnl=-40.0, net_pnl=-45.0, fees=5.0, exit_reason="stop_loss", mfe_amount=5.0, mae_amount=40.0)
        for _ in range(30)
    ]
    performance = performance_decomposition(trades, [])
    exits = exit_reason_breakdown(trades)
    entry_quality = entry_quality_analysis(trades)
    cost_sizing = cost_and_sizing_analysis(trades)
    stability = stability_subgroups(trades)

    classifications = classify_family(performance=performance, exits=exits, entry_quality=entry_quality, cost_sizing=cost_sizing, stability=stability)
    labels = {entry["classification"] for entry in classifications}

    assert "no_directional_edge" in labels
    assert "stop_sizing_failure" in labels  # 100% stop_loss exits


def test_classify_family_flags_insufficient_evidence_for_small_sample():
    trades = four_trade_fixture()  # only 4 trades, far below the 60-trade floor
    performance = performance_decomposition(trades, [])
    exits = exit_reason_breakdown(trades)
    entry_quality = entry_quality_analysis(trades)
    cost_sizing = cost_and_sizing_analysis(trades)
    stability = stability_subgroups(trades)

    classifications = classify_family(performance=performance, exits=exits, entry_quality=entry_quality, cost_sizing=cost_sizing, stability=stability)
    labels = {entry["classification"] for entry in classifications}

    assert "insufficient_evidence" in labels


def test_research_allocation_archives_a_family_with_no_directional_edge():
    trades = [
        make_trade(gross_pnl=-40.0, net_pnl=-45.0, fees=5.0, exit_reason="stop_loss", symbol="AMD", month_key=f"2026-{(i % 6) + 1:02d}")
        for i in range(80)
    ]
    performance = performance_decomposition(trades, [])
    exits = exit_reason_breakdown(trades)
    entry_quality = entry_quality_analysis(trades)
    cost_sizing = cost_and_sizing_analysis(trades)
    stability = stability_subgroups(trades)
    classifications = classify_family(performance=performance, exits=exits, entry_quality=entry_quality, cost_sizing=cost_sizing, stability=stability)

    allocation = research_allocation(family_name="Test Family", performance=performance, classifications=classifications, stability=stability)

    assert allocation["decision"] == "archive"
    assert allocation["recommended_research_budget"].startswith("0 jobs")


def test_research_allocation_retains_a_genuinely_stable_profitable_family():
    trades = []
    for i in range(80):
        symbol = "AMD" if i % 2 == 0 else "NVDA"
        month = f"2026-{(i % 4) + 1:02d}"
        trades.append(
            make_trade(
                symbol=symbol, month_key=month, gross_pnl=30.0, net_pnl=25.0, fees=5.0,
                exit_reason="take_profit" if i % 3 else "session_close", mfe_amount=40.0, mae_amount=5.0,
            )
        )

    performance = performance_decomposition(trades, [{"metrics": {"profit_factor": 1.5, "expectancy_per_trade": 25.0}}])
    exits = exit_reason_breakdown(trades)
    entry_quality = entry_quality_analysis(trades)
    cost_sizing = cost_and_sizing_analysis(trades)
    stability = stability_subgroups(trades)
    classifications = classify_family(performance=performance, exits=exits, entry_quality=entry_quality, cost_sizing=cost_sizing, stability=stability)

    allocation = research_allocation(family_name="Test Family", performance=performance, classifications=classifications, stability=stability)

    assert performance["net_profit_factor"] == float("inf")
    assert allocation["decision"] == "retain_for_focused_investigation"


def test_minimum_evidence_rules_are_exposed_and_documented():
    assert MINIMUM_EVIDENCE_RULES["min_trades_for_subgroup_evidence"] == 20
    assert "min_symbols_for_stability" in MINIMUM_EVIDENCE_RULES
    assert "max_single_symbol_share_of_net_pnl" in MINIMUM_EVIDENCE_RULES
