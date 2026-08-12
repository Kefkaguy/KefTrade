from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.services.intraday_intrabar_helper import _event_diagnostics
from app.services.intraday_trade_flow import aggregate_trade_flow, bar_start


def _bar(minute: int, *, open_: float, high: float, low: float, close: float, volume: int = 1000):
    timestamp = datetime(2026, 1, 5, 15, minute, tzinfo=UTC)
    return {
        "symbol": "TEST",
        "timeframe": "1m",
        "timestamp": timestamp,
        "open": Decimal(str(open_)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": Decimal(volume),
        "source": "alpaca_sip",
    }


def _trade(minute: int, second: int, price: float, size: int):
    return {
        "symbol": "TEST",
        "timestamp": datetime(2026, 1, 5, 15, minute, second, tzinfo=UTC),
        "price": Decimal(str(price)),
        "size": Decimal(size),
        "conditions": [],
    }


def test_trade_flow_supports_1m_bar_grid():
    assert bar_start(datetime(2026, 1, 5, 15, 2, 34, tzinfo=UTC), timeframe="1m") == datetime(
        2026, 1, 5, 15, 2, tzinfo=UTC
    )
    rows = aggregate_trade_flow(
        [
            _trade(2, 1, 100.00, 100),
            _trade(2, 20, 100.02, 100),
            _trade(3, 5, 100.01, 50),
        ],
        symbol="TEST",
        timeframe="1m",
        feed="sip",
    )

    assert [row["timestamp"] for row in rows] == [
        datetime(2026, 1, 5, 15, 2, tzinfo=UTC),
        datetime(2026, 1, 5, 15, 3, tzinfo=UTC),
    ]
    assert rows[0]["timeframe"] == "1m"
    assert rows[0]["trade_count"] == 2


def test_intrabar_diagnostics_marks_long_reversal_confirmation():
    bars = [
        _bar(0, open_=100.0, high=100.1, low=99.0, close=99.4),
        _bar(1, open_=99.4, high=99.8, low=99.2, close=99.7),
        _bar(2, open_=99.7, high=100.2, low=99.6, close=100.1),
        _bar(3, open_=100.1, high=100.5, low=99.9, close=100.4),
        _bar(4, open_=100.4, high=100.8, low=100.2, close=100.7),
    ]
    flow_rows = [
        {"timestamp": bars[0]["timestamp"], "total_volume": 100, "signed_trade_imbalance": -0.25},
        {"timestamp": bars[1]["timestamp"], "total_volume": 100, "signed_trade_imbalance": -0.05},
        {"timestamp": bars[2]["timestamp"], "total_volume": 100, "signed_trade_imbalance": 0.10},
        {"timestamp": bars[3]["timestamp"], "total_volume": 100, "signed_trade_imbalance": 0.25},
        {"timestamp": bars[4]["timestamp"], "total_volume": 100, "signed_trade_imbalance": 0.30},
    ]
    observation = {
        "factor_key": "gap_down_absorption_reversal_2bar",
        "symbol": "TEST",
        "session_date": datetime(2026, 1, 5, tzinfo=UTC).date(),
        "score": 0.01,
        "target_return": 0.002,
        "signal_bar_timestamp": datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
        "decision_timestamp": datetime(2026, 1, 5, 15, 5, tzinfo=UTC),
        "entry_bar_timestamp": datetime(2026, 1, 5, 15, 5, tzinfo=UTC),
    }

    result = _event_diagnostics(
        observation,
        parent_timeframe="15m",
        intrabar_timeframe="1m",
        bars=bars,
        flow_rows=flow_rows,
    )

    assert result["direction"] == "long"
    assert result["flags"]["intrabar_price_confirms_direction"] is True
    assert result["flags"]["imbalance_improving"] is True
    assert result["flags"]["imbalance_confirms_direction"] is True
    assert result["confirmation_score"] >= 3
