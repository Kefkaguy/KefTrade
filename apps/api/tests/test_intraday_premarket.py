from datetime import date, timedelta

from app.services.intraday_premarket import (
    MINIMUM_BASELINE_SESSIONS,
    premarket_features,
)


def session(
    day,
    *,
    symbol="AAPL",
    bars=6,
    volume=100_000.0,
    first=100.0,
    last=101.0,
    high=101.5,
    low=99.5,
    regular_open=101.0,
    regular_close=102.0,
):
    return {
        "symbol": symbol,
        "session_date": day,
        "premarket_bars": bars,
        "premarket_volume": volume,
        "first_premarket_price": first,
        "last_premarket_price": last,
        "premarket_high": high,
        "premarket_low": low,
        "regular_open": regular_open,
        "regular_close": regular_close,
    }


def build(rows):
    return premarket_features(rows, timeframe="30m", source="alpaca_sip")


def consecutive(count, **overrides):
    start = date(2025, 1, 6)
    days = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return [session(day, **overrides) for day in days]


def test_a_session_without_premarket_bars_produces_no_row():
    rows = build([session(date(2025, 1, 6), bars=0, volume=0.0)])

    assert rows == []


def test_relative_volume_needs_enough_prior_sessions():
    rows = build(consecutive(MINIMUM_BASELINE_SESSIONS + 2))

    # The baseline only draws on strictly prior sessions, so the earliest
    # sessions cannot have one.
    assert rows[0]["premarket_relative_volume"] is None
    assert rows[-1]["premarket_relative_volume"] is not None


def test_relative_volume_never_includes_the_session_it_measures():
    rows = consecutive(10)
    # A single enormous premarket must not inflate its own baseline.
    rows[-1]["premarket_volume"] = 10_000_000.0

    built = build(rows)

    assert built[-1]["premarket_relative_volume"] > 50


def test_premarket_return_and_range_are_measured_within_the_premarket():
    rows = build(consecutive(8, first=100.0, last=104.0, high=105.0, low=100.0))

    assert rows[-1]["premarket_return"] == 0.04
    assert rows[-1]["premarket_range"] == 0.05


def test_gap_is_not_measured_across_a_membership_hole():
    early = consecutive(6)
    late = [session(row["session_date"] + timedelta(days=200)) for row in early]

    built = build(early + late)
    first_after_hole = min(row["session_date"] for row in late)
    rejoining = next(row for row in built if row["session_date"] == first_after_hole)

    assert rejoining["premarket_gap"] is None
    assert rejoining["gap_discovered_premarket"] is None


def test_the_first_session_has_no_prior_close_to_gap_from():
    built = build(consecutive(4))

    assert built[0]["premarket_gap"] is None
    assert built[0]["prior_regular_close"] is None


def test_gap_discovery_reports_the_share_premarket_had_already_priced():
    rows = consecutive(4)
    # Prior close 102. Premarket ends at 103 (+0.98%), open at 104 (+1.96%),
    # so premarket discovered about half the eventual gap.
    rows[-1] = session(
        rows[-1]["session_date"], last=103.0, regular_open=104.0, regular_close=104.0
    )

    built = build(rows)
    final = built[-1]

    assert final["prior_regular_close"] == 102.0
    assert 0.45 < final["gap_discovered_premarket"] < 0.55


def test_premarket_overshooting_the_open_is_reported_above_one():
    rows = consecutive(4)
    # Premarket runs to 106 but the open pulls back to 104.
    rows[-1] = session(
        rows[-1]["session_date"], last=106.0, regular_open=104.0, regular_close=104.0
    )

    built = build(rows)

    assert built[-1]["gap_discovered_premarket"] > 1.0


def test_each_symbol_keeps_its_own_baseline_and_prior_close():
    rows = consecutive(8) + consecutive(8, symbol="MSFT", volume=5_000.0)

    built = build(rows)
    apple = [row for row in built if row["symbol"] == "AAPL"]
    microsoft = [row for row in built if row["symbol"] == "MSFT"]

    assert len(apple) == len(microsoft) == 8
    assert apple[-1]["premarket_volume"] != microsoft[-1]["premarket_volume"]
    assert apple[-1]["premarket_relative_volume"] is not None
    assert microsoft[-1]["premarket_relative_volume"] is not None
