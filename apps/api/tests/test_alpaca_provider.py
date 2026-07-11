from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.providers.alpaca import exclude_incomplete_latest, normalize_stock_bar, normalize_stock_bars, start_for_limit


def test_normalize_stock_bar_maps_alpaca_payload_to_candle() -> None:
    candle = normalize_stock_bar(
        "AAPL",
        "1h",
        {
            "t": "2026-01-05T14:30:00Z",
            "o": 100,
            "h": 105,
            "l": 99,
            "c": 104,
            "v": 12345,
        },
    )

    assert candle == {
        "symbol": "AAPL",
        "source": "alpaca_iex",
        "timeframe": "1h",
        "timestamp": datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        "open": Decimal("100"),
        "high": Decimal("105"),
        "low": Decimal("99"),
        "close": Decimal("104"),
        "volume": Decimal("12345"),
    }


def test_normalize_stock_bars_rejects_invalid_ohlc() -> None:
    candles, invalid = normalize_stock_bars(
        "AAPL",
        "1h",
        [
            {"t": "2026-01-05T14:30:00Z", "o": 100, "h": 105, "l": 99, "c": 104, "v": 10},
            {"t": "2026-01-05T15:30:00Z", "o": 100, "h": 98, "l": 99, "c": 104, "v": 10},
        ],
    )

    assert len(candles) == 1
    assert invalid == 1


def test_exclude_incomplete_latest_intraday_bar() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    candles = [
        {"timestamp": datetime(2026, 1, 5, 13, 30, tzinfo=UTC)},
        {"timestamp": datetime(2026, 1, 5, 14, 30, tzinfo=UTC)},
    ]

    complete, excluded = exclude_incomplete_latest(candles, "1h", now=now)

    assert complete == candles[:1]
    assert excluded is True


def test_start_for_limit_looks_back_far_enough_for_hourly_stock_research() -> None:
    start = start_for_limit("1h", 5000)

    assert start < datetime.now(tz=UTC) - timedelta(days=900)
