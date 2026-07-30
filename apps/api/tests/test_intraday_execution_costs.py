from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.intraday_execution_costs import (
    aggregate_microstructure_bars,
    calibrate_execution_costs,
    calibrate_regular_session_bar_costs,
    load_execution_evidence,
    load_regular_session_cost_bars,
    match_fills_to_quotes,
)
from app.cli import intraday_costs


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


def test_regular_session_calibration_weights_each_symbol_bar_once():
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bars = [
        {
            "symbol": "SPY",
            "provider": "alpaca",
            "feed": "iex",
            "timestamp": start,
            "quote_count": 100_000,
            "median_spread_bps": 1,
        },
        {
            "symbol": "AAPL",
            "provider": "alpaca",
            "feed": "iex",
            "timestamp": start,
            "quote_count": 10,
            "median_spread_bps": 3,
        },
    ]

    result = calibrate_regular_session_bar_costs(bars, regulatory_bps=0)

    assert result["quote_observations"] == 100_010
    assert result["median_spread_bps"] == 2
    assert result["observed_round_trip_bps"] == 2
    assert result["methodology"]["bar_observations"] == 2
    assert "one observation per symbol/bar" in result["methodology"]["event_weighting_guard"]


def test_optional_feed_filters_are_typed_for_postgres():
    class Result:
        def fetchall(self):
            return []

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, query, params):
            self.queries.append((query, params))
            return Result()

    conn = Connection()
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    end = start + timedelta(days=1)

    load_execution_evidence(
        conn,
        symbols=["AAPL"],
        start=start,
        end=end,
        feed="sip",
    )
    load_regular_session_cost_bars(
        conn,
        symbols=["AAPL"],
        timeframe="30m",
        start=start,
        end=end,
        feed="sip",
    )

    combined = "\n".join(query for query, _ in conn.queries)
    assert "%s::text IS NULL OR feed = %s::text" in combined
    assert "%s::text IS NULL OR micro.feed = %s::text" in combined


def test_calibration_cli_uses_bar_costs_without_raw_quotes_by_default(monkeypatch):
    calls = {"raw": 0, "bars": 0, "persist": 0}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fail_raw(*_args, **_kwargs):
        calls["raw"] += 1
        raise AssertionError("raw quotes should not be loaded by default")

    def fake_bars(*_args, **_kwargs):
        calls["bars"] += 1
        return [
            {
                "symbol": "AAPL",
                "provider": "alpaca",
                "feed": "sip",
                "timestamp": datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
                "quote_count": 10,
                "median_spread_bps": 1.5,
            }
        ]

    def fake_persist(_conn, result):
        calls["persist"] += 1
        assert result["matched_fill_observations"] == 0
        return 77

    monkeypatch.setattr(intraday_costs, "connect", lambda: Connection())
    monkeypatch.setattr(intraday_costs, "load_execution_evidence", fail_raw)
    monkeypatch.setattr(intraday_costs, "load_regular_session_cost_bars", fake_bars)
    monkeypatch.setattr(intraday_costs, "persist_cost_calibration", fake_persist)

    args = intraday_costs.parser().parse_args(
        [
            "calibrate",
            "--symbols",
            "AAPL",
            "--start",
            "2026-01-05T14:30:00Z",
            "--end",
            "2026-01-05T21:00:00Z",
            "--feed",
            "sip",
        ]
    )
    result = intraday_costs.calibrate(args)

    assert calls == {"raw": 0, "bars": 1, "persist": 1}
    assert result["calibration_id"] == 77


def test_calibration_cli_can_opt_into_raw_quote_fill_matching():
    args = intraday_costs.parser().parse_args(
        [
            "calibrate",
            "--symbols",
            "AAPL",
            "--include-raw-quote-fills",
        ]
    )

    assert args.include_raw_quote_fills is True
