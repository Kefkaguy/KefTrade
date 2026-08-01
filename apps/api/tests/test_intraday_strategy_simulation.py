from datetime import UTC, datetime, timedelta

from app.services.intraday_elite_gates import FamilyRecipe, execution_semantics_report
from app.services.intraday_strategy_simulation import simulate_family

OPEN = datetime(2025, 3, 3, 14, 30, tzinfo=UTC)  # 09:30 ET
COSTS = {"stressed_round_trip_bps": 10.0, "conservative_round_trip_bps": 30.0}


def recipe(**overrides):
    fields = {
        "factor_key": "signed_trade_imbalance_continuation_1bar",
        "entry_condition": "signed imbalance at or above 0.30",
        "direction": "both",
        "holding_bars": 1,
        "stop_loss": "none; horizon exit only",
        "forced_session_close_exit": True,
        "max_concurrent_positions": 5,
        "position_size_fraction": 0.1,
        "max_gross_exposure": 0.5,
        "eligible_symbols": (),
        "eligible_session_slots": (),
        "cost_calibration_id": 1,
    }
    fields.update(overrides)
    return FamilyRecipe(**fields)


def bar(index, *, close=100.0, open_price=100.0, volume=1_000_000.0, day=None):
    start = OPEN if day is None else OPEN.replace(day=day)
    return {
        "timestamp": start + timedelta(minutes=30 * index),
        "open": open_price,
        "high": max(open_price, close) * 1.001,
        "low": min(open_price, close) * 0.999,
        "close": close,
        "volume": volume,
    }


def candles(symbols=("AAPL",), *, closes=None, day=None, slots=13, volume=1_000_000.0):
    closes = closes or {}
    return {
        symbol: [
            bar(
                index,
                close=closes.get(symbol, 100.0),
                open_price=100.0,
                volume=volume,
                day=day,
            )
            for index in range(slots)
        ]
        for symbol in symbols
    }


def observation(symbol="AAPL", *, index=0, score=0.6, horizon=1, day=None):
    rows = candles((symbol,), day=day)[symbol]
    return {
        "factor_key": "signed_trade_imbalance_continuation_1bar",
        "symbol": symbol,
        "score": score,
        "signal_bar_timestamp": rows[index]["timestamp"],
        "decision_timestamp": rows[index]["timestamp"] + timedelta(minutes=30),
        "entry_bar_timestamp": rows[index + 1]["timestamp"],
        "exit_bar_timestamp": rows[index + horizon]["timestamp"],
    }


def run(observations, *, bars=None, capital=1_000_000.0, **overrides):
    return simulate_family(
        observations,
        recipe=recipe(**overrides),
        candles_by_symbol=bars or candles(),
        cost_model=COSTS,
        capital=capital,
    )


def test_a_qualifying_observation_becomes_a_trade():
    result = run([observation()])

    assert result["trade_count"] == 1
    assert result["trades"][0]["side"] == "long"


def test_a_negative_score_is_taken_short():
    result = run([observation(score=-0.6)])

    assert result["trades"][0]["side"] == "short"


def test_a_long_lifts_the_offer_and_a_short_hits_the_bid():
    long_trade = run([observation(score=0.6)])["trades"][0]
    short_trade = run([observation(score=-0.6)])["trades"][0]

    assert long_trade["entry_price"] == long_trade["ask"]
    assert short_trade["entry_price"] == short_trade["bid"]
    assert long_trade["ask"] > long_trade["bid"]


def test_crossing_the_spread_costs_money_on_a_flat_move():
    # Price unchanged from entry open to exit close: the mid-to-mid return is
    # zero, and the trade still loses the spread it paid twice.
    result = run([observation()], bars=candles(closes={"AAPL": 100.0}))
    trade = result["trades"][0]

    assert trade["gross_return"] == 0.0
    assert trade["net_return"] < 0


def test_the_simulated_trades_satisfy_the_execution_gate():
    result = run([observation()])

    report = execution_semantics_report(result["trades"])

    assert report["passed"] is True
    assert report["checks"]["decision_precedes_entry"] is True
    assert report["checks"]["spread_side_respected"] is True
    assert report["checks"]["costs_charged_on_every_trade"] is True


def test_capacity_is_a_real_constraint_not_a_note():
    symbols = tuple(f"S{index}" for index in range(8))
    bars = candles(symbols)
    observations = [observation(symbol, score=0.6) for symbol in symbols]

    result = run(observations, bars=bars, max_concurrent_positions=3)

    assert result["trade_count"] == 3
    assert result["skipped"]["no_capacity"] == 5


def test_the_scarce_slot_goes_to_the_strongest_signal_not_the_luckiest():
    symbols = ("WEAK", "STRONG")
    bars = candles(symbols, closes={"WEAK": 130.0, "STRONG": 100.5})
    observations = [
        observation("WEAK", score=0.31),
        observation("STRONG", score=0.95),
    ]

    result = run(observations, bars=bars, max_concurrent_positions=1)

    # WEAK has by far the better outcome. Priority must not notice that.
    assert result["trades"][0]["symbol"] == "STRONG"


def test_gross_exposure_caps_the_book_even_below_the_position_count():
    symbols = tuple(f"S{index}" for index in range(6))
    bars = candles(symbols)
    observations = [observation(symbol) for symbol in symbols]

    result = run(
        observations,
        bars=bars,
        max_concurrent_positions=10,
        position_size_fraction=0.25,
        max_gross_exposure=0.5,
    )

    assert result["trade_count"] == 2


def test_one_position_per_symbol():
    bars = candles(("AAPL",))
    observations = [observation(index=0, horizon=4), observation(index=1, horizon=1)]

    result = run(observations, bars=bars, max_concurrent_positions=5)

    assert result["trade_count"] == 1


def test_capacity_frees_up_once_a_position_has_exited():
    symbols = ("AAA", "BBB")
    bars = candles(symbols)
    observations = [observation("AAA", index=0), observation("BBB", index=4)]

    result = run(observations, bars=bars, max_concurrent_positions=1)

    assert result["trade_count"] == 2


def test_an_ineligible_symbol_is_not_traded():
    bars = candles(("AAPL", "MSFT"))
    observations = [observation("AAPL"), observation("MSFT")]

    result = run(observations, bars=bars, eligible_symbols=("AAPL",))

    assert result["trade_count"] == 1
    assert result["skipped"]["ineligible_symbol"] == 1


def test_a_direction_filter_excludes_rather_than_flips():
    observations = [observation(score=0.6), observation(score=-0.6)]

    result = run(observations, direction="long", max_concurrent_positions=5)

    assert result["trade_count"] == 1
    assert result["trades"][0]["side"] == "long"
    assert result["skipped"]["wrong_direction"] == 1


def test_an_observation_with_no_bar_is_skipped_not_priced():
    result = run([observation("MISSING")], bars=candles(("AAPL",)))

    assert result["trade_count"] == 0
    assert result["skipped"]["missing_bar"] == 1


def test_participation_is_measured_against_the_bar_it_would_trade_in():
    thin = run([observation()], bars=candles(volume=1_000.0))["trades"][0]
    deep = run([observation()], bars=candles(volume=100_000_000.0))["trades"][0]

    assert thin["participation_rate"] > deep["participation_rate"]


def test_the_fill_rate_reports_the_gap_between_events_and_trades():
    symbols = tuple(f"S{index}" for index in range(10))
    bars = candles(symbols)
    observations = [observation(symbol) for symbol in symbols]

    result = run(observations, bars=bars, max_concurrent_positions=2)

    assert result["fill_rate"] == 0.2
    assert result["observations"] == 10


def test_simulation_is_deterministic():
    symbols = tuple(f"S{index}" for index in range(8))
    bars = candles(symbols)
    observations = [observation(symbol, score=0.5) for symbol in symbols]

    first = run(observations, bars=bars, max_concurrent_positions=3)
    second = run(list(reversed(observations)), bars=bars, max_concurrent_positions=3)

    assert first["trades"] == second["trades"]
