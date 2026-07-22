from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.elite_replay_outcomes import (
    apply_historical_portfolio_constraints,
    execution_timing_diagnostics,
    outcome_metrics,
    resize_outcome,
    simulate_replay_outcome,
    skipped_outcome,
    wilson_interval,
)


def test_outcome_uses_next_bar_entry_costs_and_target() -> None:
    started = datetime(2026, 7, 1, 14, tzinfo=UTC)
    decision = replay_decision(started)
    candles = [
        candle(started, 100, 101, 99, 100),
        candle(started + timedelta(hours=1), 100, 108, 99, 107),
        candle(started + timedelta(hours=2), 107, 108, 106, 107),
    ]

    outcome, exit_index = simulate_replay_outcome(
        decision,
        candles,
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
        max_holding_bars=12,
        allocated_capital=Decimal("10000"),
        risk_cap_pct=Decimal("0.005"),
        total_exposure_cap_pct=Decimal("0.03"),
    )

    assert outcome["status"] == "completed"
    assert outcome["entry_time"] == started + timedelta(hours=1)
    assert outcome["entry_price"] == Decimal("100.0500")
    assert outcome["exit_reason"] == "take_profit"
    assert outcome["fees"] > 0
    assert outcome["net_pnl"] > 0
    assert exit_index == 1


def test_same_candle_stop_and_target_uses_stop_first() -> None:
    started = datetime(2026, 7, 1, 14, tzinfo=UTC)
    candles = [
        candle(started, 100, 101, 99, 100),
        candle(started + timedelta(hours=1), 100, 110, 95, 101),
    ]

    outcome, _ = simulate_replay_outcome(
        replay_decision(started),
        candles,
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.0005"),
        max_holding_bars=12,
        allocated_capital=Decimal("10000"),
        risk_cap_pct=Decimal("0.005"),
        total_exposure_cap_pct=Decimal("0.03"),
    )

    assert outcome["exit_reason"] == "stop_loss_same_candle"
    assert outcome["net_pnl"] < 0


def test_metrics_include_confidence_profit_factor_and_drawdown() -> None:
    rows = [completed(10), completed(5), completed(-4), completed(-3)]

    metrics = outcome_metrics(rows, Decimal("10000"))

    assert metrics["completed_trades"] == 4
    assert metrics["win_rate"] == 0.5
    assert metrics["profit_factor"] == 15 / 7
    assert metrics["expectancy"] == 2.0
    assert metrics["max_drawdown"] == 7.0
    assert metrics["win_rate_confidence_95"]["lower"] < 0.5 < metrics["win_rate_confidence_95"]["upper"]
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_timing_diagnostic_exposes_four_hour_market_close_conflict() -> None:
    four_hour_open = datetime(2026, 7, 1, 16, tzinfo=UTC)  # 12:00 ET; completes at 16:00 ET
    one_hour_open = datetime(2026, 7, 1, 14, tzinfo=UTC)  # 10:00 ET; completes at 11:00 ET

    result = execution_timing_diagnostics({
        ("AAXJ", "4h"): [candle(four_hour_open, 100, 101, 99, 100)],
        ("AAXJ", "1h"): [candle(one_hour_open, 100, 101, 99, 100)],
    })

    assert result["4h"]["classification"] == "market_open_conflict"
    assert result["4h"]["complete_while_market_open"] == 0
    assert result["1h"]["classification"] == "compatible"


def test_portfolio_skip_and_resize_do_not_preserve_phantom_pnl() -> None:
    outcome = completed(10)
    outcome.update({"quantity": 2, "gross_pnl": Decimal("12"), "net_return_on_allocated_capital": Decimal("0.001")})

    resize_outcome(outcome, 1, Decimal("10000"))
    skipped = skipped_outcome(outcome, "existing_portfolio_symbol_position")

    assert outcome["quantity"] == 1
    assert outcome["net_pnl"] == Decimal("5")
    assert skipped["status"] == "skipped_overlap"
    assert skipped["quantity"] == 0
    assert skipped["net_pnl"] == 0
    assert skipped["assumptions"]["portfolio_skip_reason"] == "existing_portfolio_symbol_position"


def test_mixed_timeframes_are_arbitrated_by_entry_time_not_signal_order() -> None:
    early = portfolio_outcome(datetime(2026, 7, 1, 15, tzinfo=UTC), datetime(2026, 7, 1, 16, tzinfo=UTC), score=5)
    later = portfolio_outcome(datetime(2026, 7, 2, 14, tzinfo=UTC), datetime(2026, 7, 2, 18, tzinfo=UTC), score=6)

    constrained = apply_historical_portfolio_constraints(
        [later, early],
        allocated_capital=Decimal("10000"),
        total_exposure_cap_pct=Decimal("0.03"),
        max_open_positions=2,
    )

    assert [row["status"] for row in constrained] == ["completed", "completed"]
    assert [row["entry_time"] for row in constrained] == [early["entry_time"], later["entry_time"]]


def replay_decision(timestamp):
    return {
        "id": 1,
        "replay_run_id": 2,
        "external_deployment_id": 4,
        "symbol": "AAXJ",
        "timeframe": "1h",
        "completed_bar_timestamp": timestamp,
        "reference_price": Decimal("100"),
        "stop_price": Decimal("97"),
        "target_price": Decimal("106"),
        "simulated_quantity": 3,
        "regime": {"trend_regime": "bull_trend"},
    }


def candle(timestamp, opened, high, low, close):
    return {
        "symbol": "AAXJ",
        "timeframe": "1h",
        "timestamp": timestamp,
        "open": Decimal(str(opened)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": Decimal("1000"),
    }


def completed(pnl):
    return {
        "status": "completed",
        "net_pnl": Decimal(str(pnl)),
        "fees": Decimal("1"),
        "holding_hours": 2.0,
        "exit_time": datetime(2026, 7, 1, tzinfo=UTC),
        "entry_time": datetime(2026, 7, 1, tzinfo=UTC),
        "external_deployment_id": 1,
        "exit_reason": "take_profit" if pnl > 0 else "stop_loss",
    }


def portfolio_outcome(entry_time, exit_time, score):
    row = completed(5)
    row.update({
        "entry_time": entry_time,
        "exit_time": exit_time,
        "symbol": "AAXJ",
        "entry_price": Decimal("100"),
        "quantity": 1,
        "gross_pnl": Decimal("6"),
        "net_return_on_allocated_capital": Decimal("0.0005"),
        "_research_score": score,
    })
    return row
