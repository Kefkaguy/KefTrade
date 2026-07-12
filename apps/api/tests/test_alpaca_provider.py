import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.providers import alpaca
from app.providers.alpaca import exclude_incomplete_latest, fetch_stock_bars, normalize_stock_bar, normalize_stock_bars, start_for_limit


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


def test_start_for_limit_caps_hourly_stock_research_to_recent_monitoring_window() -> None:
    start = start_for_limit("1h", 5000)
    age = datetime.now(tz=UTC) - start

    assert timedelta(days=400) < age < timedelta(days=430)


def test_fetch_stock_bars_returns_latest_paginated_tail(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self.headers = {"X-Request-ID": "request-id"}
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, endpoint, params):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    {
                        "bars": [
                            {"t": "2025-06-23T18:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                            {"t": "2025-06-23T19:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 1},
                        ],
                        "next_page_token": "next",
                    }
                )
            assert params["page_token"] == "next"
            return FakeResponse(
                {
                    "bars": [
                        {"t": "2026-07-10T18:00:00Z", "o": 3, "h": 3, "l": 3, "c": 3, "v": 1},
                        {"t": "2026-07-10T19:00:00Z", "o": 4, "h": 4, "l": 4, "c": 4, "v": 1},
                    ],
                    "next_page_token": None,
                }
            )

    monkeypatch.setattr(alpaca.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(alpaca.settings, "alpaca_api_key", "key")
    monkeypatch.setattr(alpaca.settings, "alpaca_api_secret", "secret")

    _status, bars, request_log, _request_id = asyncio.run(fetch_stock_bars("TSLA", "1h", 2))

    assert [bar["t"] for bar in bars] == ["2026-07-10T18:00:00Z", "2026-07-10T19:00:00Z"]
    assert len(request_log) == 2
