from app.services.broker_sync import normalize_fill, normalize_position


def test_normalize_fill_maps_alpaca_short_activity_side() -> None:
    row = normalize_fill(
        {
            "id": "activity-1",
            "order_id": "order-1",
            "symbol": "F",
            "side": "sell_short",
            "qty": "1",
            "price": "13.84",
            "cum_qty": "1",
            "leaves_qty": "0",
            "transaction_time": "2026-08-10T14:27:56.618063Z",
        }
    )

    assert row["side"] == "sell"
    assert row["raw_side"] == "sell_short"


def test_normalize_fill_maps_alpaca_cover_activity_side() -> None:
    row = normalize_fill(
        {
            "id": "activity-2",
            "order_id": "order-2",
            "symbol": "F",
            "side": "buy_to_cover",
            "qty": "1",
            "price": "13.80",
            "transaction_time": "2026-08-10T15:00:00Z",
        }
    )

    assert row["side"] == "buy"
    assert row["raw_side"] == "buy_to_cover"


def test_normalize_position_preserves_signed_short_quantity() -> None:
    row = normalize_position(
        {
            "symbol": "C",
            "qty": "-1",
            "avg_entry_price": "134.97",
            "market_value": "-134.97",
            "unrealized_pl": "0",
        }
    )

    assert row["symbol"] == "C"
    assert row["quantity"] < 0
    assert row["market_value"] > 0
