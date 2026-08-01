from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.intraday_trade_flow import (
    BUY,
    LARGE_TRADE_SIZE,
    LEE_READY,
    SELL,
    TICK_RULE,
    UNCLASSIFIED,
    TradeFlowAccumulator,
    aggregate_trade_flow,
    bar_start,
    classifier_agreement_report,
    classify_trades,
)

OPEN = datetime(2025, 3, 3, 14, 30, tzinfo=UTC)  # 09:30 ET


def trade(offset_seconds, price, size=100, conditions=None):
    return {
        "timestamp": OPEN + timedelta(seconds=offset_seconds),
        "price": Decimal(str(price)),
        "size": Decimal(str(size)),
        "conditions": conditions or ["@"],
    }


def quote(offset_seconds, bid, ask):
    midpoint = (Decimal(str(bid)) + Decimal(str(ask))) / Decimal(2)
    return {
        "timestamp": OPEN + timedelta(seconds=offset_seconds),
        "bid_price": Decimal(str(bid)),
        "ask_price": Decimal(str(ask)),
        "midpoint": midpoint,
    }


def flow(trades, quotes=None, timeframe="30m"):
    return aggregate_trade_flow(
        trades, symbol="AAPL", timeframe=timeframe, feed="sip", quotes=quotes
    )


def test_a_trade_above_the_midpoint_is_a_buy():
    signed = classify_trades([trade(5, 100.06)], quotes=[quote(1, 100.00, 100.10)])

    assert signed[0]["side"] == BUY
    assert signed[0]["classification"] == LEE_READY


def test_a_trade_below_the_midpoint_is_a_sell():
    signed = classify_trades([trade(5, 100.02)], quotes=[quote(1, 100.00, 100.10)])

    assert signed[0]["side"] == SELL


def test_a_quote_posted_after_the_trade_cannot_be_the_quote_it_crossed():
    # The only quote arrives a second later, so there is no prevailing quote
    # and the classifier must fall back rather than peek forward.
    signed = classify_trades([trade(5, 100.06)], quotes=[quote(6, 100.00, 100.10)])

    assert signed[0]["midpoint"] is None


def test_the_prevailing_quote_is_the_latest_one_before_the_trade():
    quotes = [quote(1, 100.00, 100.10), quote(4, 100.20, 100.30)]

    signed = classify_trades([trade(5, 100.22)], quotes=quotes)

    # Against the stale quote 100.22 is a buy; against the prevailing one it is
    # a sell.
    assert signed[0]["side"] == SELL


def test_a_midpoint_trade_falls_back_to_the_tick_rule():
    trades = [trade(1, 100.00), trade(5, 100.05)]
    quotes = [quote(0, 100.00, 100.10)]

    signed = classify_trades(trades, quotes=quotes)

    assert signed[1]["classification"] == "midpoint_tick_fallback"
    assert signed[1]["side"] == BUY


def test_the_tick_rule_works_without_any_quotes():
    signed = classify_trades([trade(1, 100.00), trade(2, 100.05), trade(3, 99.95)])

    assert signed[1]["side"] == BUY
    assert signed[2]["side"] == SELL


def test_the_first_trade_of_the_stream_has_no_tick_to_compare():
    signed = classify_trades([trade(1, 100.00)])

    assert signed[0]["side"] == UNCLASSIFIED


def test_a_repeated_price_inherits_the_direction_that_started_the_run():
    trades = [trade(1, 100.00), trade(2, 100.05), trade(3, 100.05), trade(4, 100.05)]

    signed = classify_trades(trades)

    assert [row["side"] for row in signed[1:]] == [BUY, BUY, BUY]
    assert signed[2]["classification"] == "zero_tick"


def test_auction_prints_are_not_signed():
    # A call auction has no side crossing a spread; signing it would invent
    # imbalance out of the opening cross.
    signed = classify_trades(
        [trade(1, 100.00), trade(2, 100.50, conditions=["O"])],
        quotes=[quote(0, 100.00, 100.10)],
    )

    assert signed[1]["side"] == UNCLASSIFIED
    assert signed[1]["classification"] == "non_aggressor_condition"


def test_out_of_sequence_prints_are_not_signed():
    signed = classify_trades([trade(1, 100.00), trade(2, 100.50, conditions=["Z"])])

    assert signed[1]["side"] == UNCLASSIFIED


def test_the_tick_rule_survives_a_page_boundary():
    first = classify_trades([trade(1, 100.00), trade(2, 100.05)])
    carried = classify_trades([trade(3, 100.10)], previous_price=Decimal("100.05"))
    orphaned = classify_trades([trade(3, 100.10)])

    assert first[-1]["side"] == BUY
    assert carried[0]["side"] == BUY
    assert orphaned[0]["side"] == UNCLASSIFIED


def test_imbalance_is_signed_by_the_aggressor_not_by_net_volume():
    quotes = [quote(0, 100.00, 100.10)]
    trades = [trade(1, 100.09, size=900), trade(2, 100.01, size=100)]

    bars = flow(trades, quotes=quotes)

    assert bars[0]["buy_volume"] == 900.0
    assert bars[0]["sell_volume"] == 100.0
    assert bars[0]["signed_trade_imbalance"] == 0.8


def test_imbalance_divides_by_classified_volume_not_total():
    # Half the session is an unsignable auction print. Dividing by total would
    # halve the imbalance purely because of bookkeeping.
    quotes = [quote(0, 100.00, 100.10)]
    trades = [trade(1, 100.09, size=500), trade(2, 100.50, size=500, conditions=["O"])]

    bars = flow(trades, quotes=quotes)

    assert bars[0]["signed_trade_imbalance"] == 1.0
    assert bars[0]["unclassified_share"] == 0.5


def test_a_bar_with_nothing_classifiable_reports_null_not_zero():
    bars = flow([trade(1, 100.00, conditions=["O"])])

    assert bars[0]["signed_trade_imbalance"] is None
    assert bars[0]["trade_count"] == 1


def test_trades_land_in_the_bar_that_contains_them():
    trades = [trade(60, 100.00), trade(60 * 45, 100.05), trade(60 * 75, 100.10)]

    bars = flow(trades)

    assert len(bars) == 3
    assert [row["timestamp"] for row in bars] == [
        datetime(2025, 3, 3, 14, 30, tzinfo=UTC),
        datetime(2025, 3, 3, 15, 0, tzinfo=UTC),
        datetime(2025, 3, 3, 15, 30, tzinfo=UTC),
    ]


def test_bar_start_lands_on_the_session_grid():
    # 09:30 ET is on a :30 boundary and US offsets are whole hours, so the
    # 30m grid in UTC is the candle grid.
    assert bar_start(OPEN + timedelta(seconds=1), timeframe="30m") == OPEN
    assert bar_start(OPEN + timedelta(minutes=29), timeframe="30m") == OPEN
    assert bar_start(OPEN + timedelta(minutes=30), timeframe="30m") == OPEN + timedelta(
        minutes=30
    )
    assert bar_start(OPEN + timedelta(minutes=20), timeframe="15m") == OPEN + timedelta(
        minutes=15
    )


def test_vwap_is_volume_weighted_not_an_average_of_prices():
    trades = [trade(1, 100.00, size=1), trade(2, 200.00, size=99)]

    bars = flow(trades)

    assert bars[0]["trade_vwap"] > 190


def test_large_trade_share_measures_institutional_scale_volume():
    trades = [trade(1, 100.00, size=LARGE_TRADE_SIZE), trade(2, 100.05, size=100)]

    bars = flow(trades)

    assert 0.98 < bars[0]["large_trade_share"] < 1.0


def test_effective_spread_is_twice_the_distance_from_the_midpoint():
    # Midpoint 100.05, paid 100.10 -> 5 bps away -> 10 bps effective spread.
    bars = flow([trade(5, 100.10)], quotes=[quote(0, 100.00, 100.10)])

    assert 9.9 < bars[0]["effective_spread_bps"] < 10.1


def test_effective_spread_is_null_without_quotes():
    bars = flow([trade(1, 100.00), trade(2, 100.05)])

    assert bars[0]["effective_spread_bps"] is None
    assert bars[0]["classification_method"] == TICK_RULE


def test_the_classification_method_is_recorded_on_every_bar():
    bars = flow([trade(5, 100.06)], quotes=[quote(1, 100.00, 100.10)])

    assert bars[0]["classification_method"] == LEE_READY


def test_streaming_pages_produce_the_same_bars_as_one_shot():
    trades = [trade(index * 10, 100.00 + (index % 5) * 0.01) for index in range(40)]
    quotes = [quote(0, 99.98, 100.06)]

    one_shot = flow(trades, quotes=quotes)

    accumulator = TradeFlowAccumulator(symbol="AAPL", timeframe="30m", feed="sip")
    accumulator.add(trades[:17], quotes=quotes)
    accumulator.add(trades[17:], quotes=quotes)

    assert accumulator.bars() == one_shot


def test_agreement_report_measures_the_cheap_classifier_rather_than_assuming_it():
    quotes = [quote(0, 100.00, 100.10)]
    trades = [
        trade(1, 100.09),
        trade(2, 100.08),  # down-tick, but still above the midpoint
        trade(3, 100.01),
        trade(4, 100.09),
    ]

    report = classifier_agreement_report(trades, quotes)

    assert report["comparable_trades"] > 0
    assert 0.0 <= report["agreement_rate"] <= 1.0
    assert report["agreement_rate"] < 1.0
    assert report["disagreements_where_lee_ready_said"][BUY] >= 1
