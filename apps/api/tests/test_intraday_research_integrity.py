from datetime import UTC, date, datetime, timedelta

from app.services.intraday_research_integrity import (
    clustered_outcome_statistics,
    cost_model_readiness,
    dataset_research_readiness,
    estimated_round_trip_cost_bps,
    rows_after_session,
)


def test_forward_cutoff_excludes_the_entire_source_session():
    rows = [
        {"timestamp": datetime(2026, 7, 24, 14, 30, tzinfo=UTC), "session_date": date(2026, 7, 24)},
        {"timestamp": datetime(2026, 7, 24, 19, 30, tzinfo=UTC), "session_date": date(2026, 7, 24)},
        {"timestamp": datetime(2026, 7, 27, 14, 30, tzinfo=UTC), "session_date": date(2026, 7, 27)},
    ]

    forward = rows_after_session(rows, session_date_exclusive=date(2026, 7, 24))

    assert forward == [rows[-1]]


def test_many_symbols_on_one_day_are_one_cluster_not_many_observations():
    session = date(2026, 7, 24)
    outcomes = [
        {"value": 0.01, "session_date": session, "symbol": f"S{index}"}
        for index in range(100)
    ]

    result = clustered_outcome_statistics(outcomes, effective_trials=1)

    assert result["signals"] == 100
    assert result["distinct_sessions"] == 1
    assert result["independent_evidence_ready"] is False


def test_repeated_positive_edge_across_independent_sessions_can_pass():
    outcomes = [
        {
            "value": 0.001 + (index % 5) * 0.0001,
            "session_date": date(2025, 1, 1) + timedelta(days=index),
            "symbol": f"S{index % 5}",
        }
        for index in range(60)
    ]

    result = clustered_outcome_statistics(
        outcomes,
        effective_trials=1,
        require_symbol_diversification=True,
    )

    assert result["distinct_sessions"] == 60
    assert result["quality_gates"]["minimum_day_clustered_t"] is True
    assert result["quality_gates"]["positive_block_bootstrap_lower_bound"] is True
    assert result["selection_adjusted_signal"] is True


def test_symbol_and_slot_costs_make_the_estimate_more_conservative():
    timestamp = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    model = {
        "stressed_round_trip_bps": 4,
        "conservative_round_trip_bps": 30,
        "by_symbol": {"AAPL": {"p90_bar_spread_bps": 7}},
        "by_time_slot": {"15:00": {"p90_bar_spread_bps": 9}},
    }

    assert estimated_round_trip_cost_bps(
        model,
        symbol="AAPL",
        timestamp=timestamp,
    ) == 9


def test_data_and_cost_readiness_report_missing_execution_evidence():
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    candles = {
        f"S{symbol}": [
            {
                "timestamp": start + timedelta(days=session),
                "session_date": date(2025, 1, 2) + timedelta(days=session),
            }
            for session in range(252)
        ]
        for symbol in range(20)
    }

    data = dataset_research_readiness(candles)
    cost = cost_model_readiness(
        {
            "observed_round_trip_bps": 3,
            "stressed_round_trip_bps": 6,
            "conservative_round_trip_bps": 30,
        },
        symbols=list(candles),
    )

    assert data["candle_research_ready"] is True
    assert data["execution_research_ready"] is False
    assert cost["research_cost_available"] is True
    assert cost["production_cost_ready"] is False
