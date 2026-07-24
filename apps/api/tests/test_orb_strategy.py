"""Phase 12, Step 2B: Opening-Range Breakout v1.

Covers the required acceptance properties: no setup during the opening-range
window, first eligible setup only after settle, long/short breakout
correctness, buffer enforcement, relative-volume confirmation, session reset
(from feature session_date, not UTC), max entries per session, directional
breakout consumption, late-entry rejection, no-lookahead, unchanged
next-bar-open execution, normal/early-close forced exits, fees/slippage
effect, and deterministic reruns. Existing swing regression is proven
unmodified by the full suite (see test_backtester.py), not duplicated here.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.backtester import combine_candles_features, run_backtest
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.labs.intraday.strategy import DEFAULT_ORB_PARAMETERS, OpeningRangeBreakoutStrategy


def make_orb_dataset(bar_specs: list[dict], *, timeframe: str = "30m", symbol: str = "TEST") -> tuple[list[dict], list[dict]]:
    bar_minutes = 30 if timeframe == "30m" else 15
    candles: list[dict] = []
    features: list[dict] = []
    cursor = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    for spec in bar_specs:
        timestamp = cursor
        cursor += timedelta(minutes=bar_minutes)
        close = spec.get("close", Decimal("100"))
        candles.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "open": close,
                "high": close + Decimal("0.25"),
                "low": close - Decimal("0.25"),
                "close": close,
                "volume": Decimal("1000"),
            }
        )
        features.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "session_date": spec["session_date"],
                "minutes_from_open": spec["minutes_from_open"],
                "minutes_to_close": spec["minutes_to_close"],
                "opening_range_minutes": spec.get("opening_range_minutes", 30),
                "opening_range_high": spec.get("opening_range_high", Decimal("101")),
                "opening_range_low": spec.get("opening_range_low", Decimal("99")),
                "session_relative_volume": spec.get("session_relative_volume", Decimal("2.0")),
            }
        )
    return candles, features


def session_bar_specs(
    session_date: date,
    count: int,
    *,
    opening_range_minutes: int = 30,
    bar_minutes: int = 30,
    opening_range_high: Decimal = Decimal("101"),
    opening_range_low: Decimal = Decimal("99"),
    relative_volume: Decimal | None = Decimal("2.0"),
    close_overrides: dict[int, Decimal] | None = None,
    relative_volume_overrides: dict[int, Decimal | None] | None = None,
) -> list[dict]:
    close_overrides = close_overrides or {}
    relative_volume_overrides = relative_volume_overrides or {}
    specs = []
    for index in range(count):
        specs.append(
            {
                "session_date": session_date,
                "minutes_from_open": index * bar_minutes,
                "minutes_to_close": (count - 1 - index) * bar_minutes,
                "opening_range_minutes": opening_range_minutes,
                "opening_range_high": opening_range_high,
                "opening_range_low": opening_range_low,
                "session_relative_volume": relative_volume_overrides.get(index, relative_volume),
                "close": close_overrides.get(index, Decimal("100")),
            }
        )
    return specs


def sustain_close_from(specs: list[dict], start_index: int, price: Decimal) -> None:
    """Holds price at `price` for every bar from `start_index` onward.

    Entries execute at the NEXT bar's open (this fixture sets open == close),
    so a breakout at bar N only produces a valid (positive risk_per_unit)
    trade if bar N+1's price is consistent with the breakout, not reverted to
    the flat baseline -- exactly like a real continuation candle would be.
    """
    for index in range(start_index, len(specs)):
        specs[index]["close"] = price


def make_params(**overrides) -> dict:
    return {**DEFAULT_ORB_PARAMETERS, **overrides}


def run_orb(bar_specs: list[dict], params: dict, *, timeframe: str = "30m"):
    candles, features = make_orb_dataset(bar_specs, timeframe=timeframe)
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)
    strategy = OpeningRangeBreakoutStrategy(params, timeframe=timeframe)
    result = run_backtest(candles, features, params, strategy, session_end_index=session_end_index)
    return candles, features, result, strategy


# --- 1 & 2: no setup during the opening-range window; first eligible setup only after settle.


def test_no_setup_during_window_and_first_eligible_setup_after_settle() -> None:
    # opening_range_minutes = 1650 keeps the range "open" through bar 54
    # (minutes_from_open 1620), so bars 50-54 (the first ones the >=50-bar
    # warmup lets the loop visit) are still inside the window, and bar 55
    # (minutes_from_open 1650) is the first settled bar.
    specs = session_bar_specs(date(2026, 1, 2), 70, opening_range_minutes=1650)
    for index in range(50, 70):
        specs[index]["close"] = Decimal("110")  # would clearly breakout if the range were settled
    params = make_params(breakout_buffer_atr="0.1", direction="long", maximum_entries_per_session=5)

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == make_orb_dataset(specs)[0][56]["timestamp"]  # next bar after signal bar 55


# --- 3: long breakout correctness.


def test_long_breakout_direction_and_risk_levels_are_correct() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))  # 101 (OR high) + buffer(0.2) = 101.2 -> 110 clears it
    params = make_params(breakout_buffer_atr="0.2", stop_atr_multiple="1.0", reward_risk_multiple="2.0", direction="long")

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["side"] == "long"
    range_span = Decimal("101") - Decimal("99")  # = 2
    expected_stop = Decimal("110") - range_span  # 108
    assert trade["stop_loss"] == expected_stop


# --- 4: short breakout correctness (mirrors long).


def test_short_breakout_direction_and_risk_levels_are_correct() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("90"))  # 99 (OR low) - buffer(0.2) = 98.8 -> 90 clears it
    params = make_params(breakout_buffer_atr="0.2", stop_atr_multiple="1.0", reward_risk_multiple="2.0", direction="short")

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["side"] == "short"
    range_span = Decimal("101") - Decimal("99")
    expected_stop = Decimal("90") + range_span  # 92
    assert trade["stop_loss"] == expected_stop


# --- 5: breakout buffer enforced.


def test_breakout_buffer_is_enforced() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[55]["close"] = Decimal("101.05")  # just above OR high (101), inside a 0.5-span buffer
    params = make_params(breakout_buffer_atr="0.5", direction="long")  # buffer = 2 * 0.5 = 1.0 -> requires close > 102

    _, _, result, _ = run_orb(specs, params)

    assert result["trades"] == []


def test_breakout_beyond_buffer_produces_a_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("103"))  # clears 102 (101 + buffer 1.0)
    params = make_params(breakout_buffer_atr="0.5", direction="long")

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1


# --- 6: relative-volume threshold enforced.


def test_relative_volume_below_threshold_blocks_the_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70, relative_volume_overrides={55: Decimal("0.5")})
    specs[55]["close"] = Decimal("110")
    params = make_params(minimum_session_relative_volume="1.0", direction="long")

    _, _, result, _ = run_orb(specs, params)

    assert result["trades"] == []


def test_relative_volume_at_or_above_threshold_allows_the_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70, relative_volume_overrides={55: Decimal("1.5")})
    sustain_close_from(specs, 55, Decimal("110"))
    params = make_params(minimum_session_relative_volume="1.0", direction="long")

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1


# --- 7: session reset works (not inferred from UTC date).


def test_state_resets_on_session_change_allowing_a_new_breakout_in_the_next_session() -> None:
    # Combined row count is kept under 80 -- at/above that, run_backtest's
    # walk-forward split carves off a validation tail and only evaluates the
    # last few rows, which isn't what this test is exercising.
    session_1 = session_bar_specs(date(2026, 1, 2), 58)
    sustain_close_from(session_1, 50, Decimal("110"))  # long breakout consumed in session 1
    session_2 = session_bar_specs(date(2026, 1, 3), 15)
    sustain_close_from(session_2, 2, Decimal("110"))  # would be blocked if long_breakout_taken leaked across sessions
    specs = session_1 + session_2
    params = make_params(direction="long", maximum_entries_per_session=1)

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 2
    assert result["trades"][0]["side"] == "long"
    assert result["trades"][1]["side"] == "long"


# --- 8: maximum entries per session enforced.


def test_maximum_entries_per_session_is_enforced() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))  # first long breakout
    specs[60]["close"] = Decimal("115")  # a second, distinct breakout opportunity (blocked regardless of geometry)
    params = make_params(direction="long", maximum_entries_per_session=1, allow_repeat_breakout_direction=True)

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1


# --- 9: directional breakout consumption (isolated from the max-entries budget).


def test_directional_breakout_consumption_blocks_a_second_long_even_with_budget_remaining() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))  # first long breakout consumes the long direction
    specs[60]["close"] = Decimal("115")  # second long attempt, budget allows it, direction flag should not (blocked regardless of geometry)
    params = make_params(direction="long", maximum_entries_per_session=5, allow_repeat_breakout_direction=False)

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1


def test_allow_repeat_breakout_direction_permits_a_second_long_when_explicitly_set() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))
    sustain_close_from(specs, 60, Decimal("115"))  # second breakout must also execute, so its continuation needs sustaining too
    params = make_params(direction="long", maximum_entries_per_session=5, allow_repeat_breakout_direction=True)

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 2


# --- 10: late entries are rejected.


def test_late_entry_near_session_close_is_rejected() -> None:
    # 30m bars: minimum_entry_lookahead_minutes("30m", 1, 1) == 60. minutes_to_close
    # of 30 on the evaluated bar is below that, so the setup must be rejected
    # even though the breakout price is clear.
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[68]["minutes_to_close"] = 30
    specs[68]["close"] = Decimal("110")
    params = make_params(direction="long")

    _, _, result, _ = run_orb(specs, params)

    assert result["trades"] == []


def test_entry_with_enough_session_time_remaining_is_accepted() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[55]["minutes_to_close"] = 90  # comfortably above the 60-minute requirement
    sustain_close_from(specs, 55, Decimal("110"))
    params = make_params(direction="long")

    _, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1


# --- 11: no future-bar data is used (no-lookahead).


def test_truncating_future_bars_does_not_change_the_already_computed_entry() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))
    params = make_params(direction="long")

    candles, features = make_orb_dataset(specs)
    full_rows = combine_candles_features(candles, features)
    full_session_end_index = build_session_end_index(full_rows)
    full_strategy = OpeningRangeBreakoutStrategy(params, timeframe="30m")
    full_result = run_backtest(candles, features, params, full_strategy, session_end_index=full_session_end_index)

    truncated_candles = candles[:60]
    truncated_features = features[:60]
    truncated_rows = combine_candles_features(truncated_candles, truncated_features)
    truncated_session_end_index = build_session_end_index(truncated_rows)
    truncated_strategy = OpeningRangeBreakoutStrategy(params, timeframe="30m")
    truncated_result = run_backtest(truncated_candles, truncated_features, params, truncated_strategy, session_end_index=truncated_session_end_index)

    assert full_result["trades"][0]["entry_time"] == truncated_result["trades"][0]["entry_time"]
    assert full_result["trades"][0]["entry_price"] == truncated_result["trades"][0]["entry_price"]
    assert full_result["trades"][0]["stop_loss"] == truncated_result["trades"][0]["stop_loss"]


# --- 12: next-bar-open execution is unchanged.


def test_entry_executes_at_the_next_bar_open() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))
    params = make_params(direction="long", slippage_rate=Decimal("0"))

    candles, _, result, _ = run_orb(specs, params)

    trade = result["trades"][0]
    assert trade["entry_time"] == candles[56]["timestamp"]
    assert trade["entry_price"] == candles[56]["open"]


# --- 13: normal and early-close forced exits work.


def test_forced_session_close_exit_when_no_stop_or_target_hit() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[55]["close"] = Decimal("110")
    params = make_params(direction="long", stop_atr_multiple="50", reward_risk_multiple="50")  # unreachable stop/target

    candles, _, result, _ = run_orb(specs, params)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[69]["timestamp"]


def test_forced_exit_on_a_short_early_close_style_session() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 60)  # warmup, never traded
    short_session = session_bar_specs(date(2026, 1, 3), 5)  # short/"early close" session
    # index 0 is still inside the opening-range window (minutes_from_open=0);
    # the breakout must be on the first *settled* bar, index 1.
    sustain_close_from(short_session, 1, Decimal("110"))
    specs = specs + short_session
    params = make_params(direction="long", stop_atr_multiple="50", reward_risk_multiple="50", maximum_entries_per_session=1)

    candles, _, result, _ = run_orb(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[-1]["timestamp"]


# --- 14: fees and slippage affect the outcome.


def test_fees_and_slippage_change_pnl() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[55]["close"] = Decimal("110")
    zero_cost_params = make_params(direction="long", fee_rate=Decimal("0"), slippage_rate=Decimal("0"), stop_atr_multiple="50", reward_risk_multiple="50")
    with_cost_params = make_params(direction="long", fee_rate=Decimal("0.01"), slippage_rate=Decimal("0.01"), stop_atr_multiple="50", reward_risk_multiple="50")

    _, _, zero_cost_result, _ = run_orb(specs, zero_cost_params)
    _, _, with_cost_result, _ = run_orb(specs, with_cost_params)

    assert zero_cost_result["trades"][0]["pnl"] != with_cost_result["trades"][0]["pnl"]


# --- 15: deterministic reruns are identical.


def test_produces_trades_on_a_realistic_scale_dataset_above_the_walk_forward_threshold() -> None:
    # Regression guard for a real defect the production pilot caught: every
    # unit test above deliberately keeps len(rows) < 80 to sidestep
    # run_backtest's walk-forward split. Real datasets have thousands of
    # rows, and DEFAULT_ORB_PARAMETERS' walk_forward_train_ratio must leave a
    # validation window large enough for the >=50-bar warmup to actually
    # reach live bars -- a ratio of 1.0 previously left a 1-bar window and
    # silently produced zero trades on every real job.
    sessions = [session_bar_specs(date(2026, 1, 2) + timedelta(days=day), 20) for day in range(20)]
    specs = [bar for session in sessions for bar in session]  # 400 bars, comfortably over 80
    breakout_index = 15 * 20 + 5  # well inside the validation window regardless of split ratio
    sustain_close_from(specs, breakout_index, Decimal("110"))
    params = make_params(direction="long", maximum_entries_per_session=1)

    _, _, result, _ = run_orb(specs, params)

    assert result["metrics"]["walk_forward"]["enabled"] is True
    assert len(result["trades"]) >= 1


def test_deterministic_reruns_produce_identical_trades() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("110"))
    params = make_params(direction="long")

    _, _, result_1, _ = run_orb(specs, params)
    _, _, result_2, _ = run_orb(specs, params)

    assert result_1["trades"] == result_2["trades"]
    assert result_1["metrics"] == result_2["metrics"]
