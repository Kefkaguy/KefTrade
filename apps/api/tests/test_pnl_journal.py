from datetime import UTC, datetime
from decimal import Decimal

from app.services.pnl_journal import match_fills


def fill(i, side, qty, price, symbol="SPY", strategy="SPY_RSI5_SMA200"):
    return {"id": i, "side": side, "quantity": qty, "price": price,
            "symbol": symbol, "strategy": strategy,
            "transaction_at": datetime(2026, 9, i, 1, tzinfo=UTC)}


def test_partial_exits_and_different_entry_prices():
    trades = match_fills([fill(1, "buy", 2, 100), fill(2, "buy", 3, 110),
                         fill(3, "sell", 4, 120), fill(4, "sell", 1, 90)])
    assert [t["gross_pnl"] for t in trades] == [Decimal(40), Decimal(20), Decimal(-20)]
    assert sum(t["quantity"] for t in trades) == 5
    assert all(t["net_pnl"] is None for t in trades)


def test_short_and_fractional_reversal():
    trades = match_fills([fill(1, "sell", ".5", 100), fill(2, "buy", ".75", 80),
                         fill(3, "sell", ".25", 90)])
    assert [t["side"] for t in trades] == ["short", "long"]
    assert [t["gross_pnl"] for t in trades] == [Decimal(10), Decimal("2.50")]


def test_et_session_and_entry_strategy_attribution():
    trades = match_fills([fill(1, "buy", 1, 100, strategy="rug_1"),
                         fill(2, "sell", 1, 110, strategy=None)])
    assert trades[0]["day"] == "2026-09-01"
    assert trades[0]["strategy"] == "rug_1"


def test_symbols_do_not_cross_match_and_open_positions_are_not_profit():
    assert match_fills([fill(1, "buy", 1, 100), fill(2, "sell", 1, 120, symbol="AMD")]) == []


def test_cross_strategy_matches_are_not_reported_as_strategy_profit():
    trades = match_fills([fill(1, "buy", 1, 100, strategy="rug_1"),
                         fill(2, "sell", 1, 110, strategy="rug_2")])
    assert trades[0]["strategy"] == "Shared / unattributed"


def test_missing_account_is_unavailable_not_a_zero_balance():
    from app.services.pnl_journal import pnl_journal
    class EmptyConnection:
        def execute(self, *args):
            return self

        def fetchone(self):
            return None

    result = pnl_journal(EmptyConnection(), "2026-09")
    assert result["account"] is None
    assert result["trades"] == []
    assert result["fees_available"] is False
