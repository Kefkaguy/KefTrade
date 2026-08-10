from app.services.broker_sync import normalize_fill


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
