from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.intraday_execution_costs import (
    aggregate_microstructure_bars,
    calibrate_execution_costs,
    match_fills_to_quotes,
)


def quote(timestamp, *, bid, ask, bid_size=10, ask_size=10):
    midpoint = (Decimal(str(bid)) + Decimal(str(ask))) / 2
    return {
        "symbol": "AAPL",
        "provider": "alpaca",
        "feed": "iex",
        "timestamp": timestamp,
        "bid_price": Decimal(str(bid)),
        "ask_price": Decimal(str(ask)),
        "bid_size": Decimal(str(bid_size)),
        "ask_size": Decimal(str(ask_size)),
        "midpoint": midpoint,
        "spread_bps": (Decimal(str(ask)) - Decimal(str(bid))) / midpoint * 10_000,
    }


def test_fill_matching_never_uses_a_future_quote():
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    quotes = [
        quote(start, bid=99.99, ask=100.01),
        quote(start + timedelta(seconds=10), bid=100, ask=100.02),
    ]
    fills = [{
        "symbol": "AAPL",
        "side": "buy",
        "price": Decimal("100.01"),
        "transaction_at": start + timedelta(seconds=5),
    }]

    matched = match_fills_to_quotes(fills, quotes)

    assert len(matched) == 1
    assert matched[0]["quote"]["timestamp"] == start


def test_cost_calibration_does_not_double_count_spread_and_fill_slippage():
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    quotes = [quote(start, bid=99.99, ask=100.01)]
    fills = [{
        "symbol": "AAPL",
        "side": "buy",
        "price": Decimal("100.01"),
        "transaction_at": start + timedelta(seconds=1),
    }]

    result = calibrate_execution_costs(quotes, fills, regulatory_bps=0)

    assert result["median_spread_bps"] == 2
    assert result["median_signed_fill_slippage_bps"] == 1
    assert result["observed_round_trip_bps"] == 2
    assert result["conservative_round_trip_bps"] == 30
    assert "never summed" in result["methodology"]["double_counting_guard"]


def test_quote_updates_aggregate_to_bar_level_ofi():
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    rows = [
        quote(start, bid=100, ask=100.02, bid_size=10, ask_size=10),
        quote(start + timedelta(minutes=1), bid=100.01, ask=100.02, bid_size=20, ask_size=8),
    ]

    bars = aggregate_microstructure_bars(rows, timeframe="15m")

    assert len(bars) == 1
    assert bars[0]["quote_count"] == 2
    assert bars[0]["order_flow_imbalance"] > 0
    assert bars[0]["normalized_order_flow_imbalance"] is not None
