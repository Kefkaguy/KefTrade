"""Phase 12: VWAP Reversion v1.

Covers the same acceptance properties Opening-Range Breakout v1 was held to
(Phase 12 Step 2B), adapted to VWAP Reversion's own entry condition (extended
deviation from session VWAP rather than a settled opening-range breakout):
no setup below the deviation threshold, first eligible setup only once the
threshold is cleared, long/short correctness, relative-volume confirmation,
session reset (from feature session_date, not UTC), max entries per session,
directional consumption, late-entry rejection, no-lookahead, unchanged
next-bar-open execution, normal/early-close forced exits, fees/slippage
effect, deterministic reruns, and -- learned directly from the Opening-Range
Breakout pilot defect -- a realistic-scale (>=80 row) walk-forward
regression guard from the very first commit of this file, not added after
the fact.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.services.backtester import combine_candles_features, run_backtest
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.labs.intraday.strategy import DEFAULT_VWAP_REVERSION_PARAMETERS, VwapReversionStrategy


def make_vwap_dataset(bar_specs: list[dict], *, timeframe: str = "30m", symbol: str = "TEST") -> tuple[list[dict], list[dict]]:
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
                "session_vwap": spec.get("session_vwap", Decimal("100")),
                "session_relative_volume": spec.get("session_relative_volume", Decimal("2.0")),
            }
        )
    return candles, features


def session_bar_specs(
    session_date: date,
    count: int,
    *,
    bar_minutes: int = 30,
    session_vwap: Decimal = Decimal("100"),
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
                "session_vwap": session_vwap,
                "session_relative_volume": relative_volume_overrides.get(index, relative_volume),
                "close": close_overrides.get(index, Decimal("100")),
            }
        )
    return specs


def sustain_close_from(specs: list[dict], start_index: int, price: Decimal) -> None:
    for index in range(start_index, len(specs)):
        specs[index]["close"] = price


def make_params(**overrides) -> dict:
    return {**DEFAULT_VWAP_REVERSION_PARAMETERS, **overrides}


def run_vwap(bar_specs: list[dict], params: dict, *, timeframe: str = "30m"):
    candles, features = make_vwap_dataset(bar_specs, timeframe=timeframe)
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)
    strategy = VwapReversionStrategy(params, timeframe=timeframe)
    result = run_backtest(candles, features, params, strategy, session_end_index=session_end_index)
    return candles, features, result, strategy


# --- 1 & 2: no setup below threshold; first eligible setup once threshold is cleared.


def test_no_setup_below_threshold_and_first_eligible_setup_at_threshold() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    # bars 50-54: deviation just under threshold (0.006 * 100 = 0.6); bar 55: clears it.
    for index in range(50, 55):
        specs[index]["close"] = Decimal("99.5")  # deviation 0.5 < 0.6
    sustain_close_from(specs, 55, Decimal("99"))  # deviation 1.0 >= 0.6
    params = make_params(entry_deviation_threshold="0.006", direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_time"] == make_vwap_dataset(specs)[0][56]["timestamp"]


# --- 3: long reversion correctness.


def test_long_reversion_direction_and_risk_levels_are_correct() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))  # 1.0 below vwap 100
    params = make_params(entry_deviation_threshold="0.006", stop_multiple="1.5", reward_risk_multiple="1.0", direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["side"] == "long"
    expected_stop = Decimal("99") - Decimal("1.5")  # deviation_distance(1.0) * stop_multiple(1.5)
    assert trade["stop_loss"] == expected_stop


# --- 4: short reversion correctness (mirrors long).


def test_short_reversion_direction_and_risk_levels_are_correct() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("101"))  # 1.0 above vwap 100
    params = make_params(entry_deviation_threshold="0.006", stop_multiple="1.5", reward_risk_multiple="1.0", direction="short")

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["side"] == "short"
    expected_stop = Decimal("101") + Decimal("1.5")
    assert trade["stop_loss"] == expected_stop


# --- 5: threshold enforced.


def test_deviation_below_threshold_produces_no_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[55]["close"] = Decimal("99.7")  # deviation 0.3, below 0.6 threshold
    params = make_params(entry_deviation_threshold="0.006", direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert result["trades"] == []


def test_deviation_at_or_beyond_threshold_produces_a_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))  # deviation 1.0, clears 0.6
    params = make_params(entry_deviation_threshold="0.006", direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1


# --- 6: relative-volume confirmation enforced.


def test_relative_volume_below_threshold_blocks_the_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70, relative_volume_overrides={55: Decimal("0.5")})
    specs[55]["close"] = Decimal("99")  # single bar only: no trade should ever open, so no continuation is needed
    params = make_params(minimum_session_relative_volume="1.0", direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert result["trades"] == []


def test_relative_volume_at_or_above_threshold_allows_the_setup() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70, relative_volume_overrides={55: Decimal("1.5")})
    sustain_close_from(specs, 55, Decimal("99"))
    params = make_params(minimum_session_relative_volume="1.0", direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1


# --- 7: session reset works (not inferred from UTC date).


def test_state_resets_on_session_change_allowing_a_new_reversion_in_the_next_session() -> None:
    session_1 = session_bar_specs(date(2026, 1, 2), 58)
    sustain_close_from(session_1, 50, Decimal("99"))
    session_2 = session_bar_specs(date(2026, 1, 3), 15)
    sustain_close_from(session_2, 2, Decimal("99"))
    specs = session_1 + session_2
    params = make_params(direction="long", maximum_entries_per_session=1)

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 2
    assert result["trades"][0]["side"] == "long"
    assert result["trades"][1]["side"] == "long"


# --- 8: maximum entries per session enforced.


def _with_quick_take_profit_exit(specs: list[dict], signal_index: int) -> None:
    """Makes the trade opened from a signal at `signal_index` close via
    take_profit within a couple of bars (stop_multiple=1.5, reward_risk=1.0
    on a deviation of 1.0 from a vwap of 100 -> take_profit = 100.5), so the
    simulator loop actually reaches later bars in the same session instead
    of riding one position all the way to the forced session close."""
    specs[signal_index]["close"] = Decimal("99")
    specs[signal_index + 1]["close"] = Decimal("99")  # entry bar (next-bar-open)
    specs[signal_index + 2]["close"] = Decimal("100.5")  # touches take_profit -> exits here


def test_maximum_entries_per_session_is_enforced() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    _with_quick_take_profit_exit(specs, 55)
    specs[60]["close"] = Decimal("98")  # a second, distinct reversion opportunity (blocked regardless)
    params = make_params(direction="long", maximum_entries_per_session=1, allow_repeat_reversion_direction=True)

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1


# --- 9: directional consumption (isolated from the max-entries budget).


def test_directional_consumption_blocks_a_second_long_even_with_budget_remaining() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    _with_quick_take_profit_exit(specs, 55)
    specs[60]["close"] = Decimal("98")
    params = make_params(direction="long", maximum_entries_per_session=5, allow_repeat_reversion_direction=False)

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1


def test_allow_repeat_reversion_direction_permits_a_second_long_when_explicitly_set() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    _with_quick_take_profit_exit(specs, 55)
    sustain_close_from(specs, 60, Decimal("98"))
    params = make_params(direction="long", maximum_entries_per_session=5, allow_repeat_reversion_direction=True)

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 2


# --- 10: late entries are rejected.


def test_late_entry_near_session_close_is_rejected() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[68]["minutes_to_close"] = 30
    specs[68]["close"] = Decimal("99")
    params = make_params(direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert result["trades"] == []


def test_entry_with_enough_session_time_remaining_is_accepted() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    specs[55]["minutes_to_close"] = 90
    sustain_close_from(specs, 55, Decimal("99"))
    params = make_params(direction="long")

    _, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1


# --- 11: no future-bar data is used (no-lookahead).


def test_truncating_future_bars_does_not_change_the_already_computed_entry() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))
    params = make_params(direction="long")

    candles, features = make_vwap_dataset(specs)
    full_rows = combine_candles_features(candles, features)
    full_session_end_index = build_session_end_index(full_rows)
    full_strategy = VwapReversionStrategy(params, timeframe="30m")
    full_result = run_backtest(candles, features, params, full_strategy, session_end_index=full_session_end_index)

    truncated_candles = candles[:60]
    truncated_features = features[:60]
    truncated_rows = combine_candles_features(truncated_candles, truncated_features)
    truncated_session_end_index = build_session_end_index(truncated_rows)
    truncated_strategy = VwapReversionStrategy(params, timeframe="30m")
    truncated_result = run_backtest(truncated_candles, truncated_features, params, truncated_strategy, session_end_index=truncated_session_end_index)

    assert full_result["trades"][0]["entry_time"] == truncated_result["trades"][0]["entry_time"]
    assert full_result["trades"][0]["entry_price"] == truncated_result["trades"][0]["entry_price"]
    assert full_result["trades"][0]["stop_loss"] == truncated_result["trades"][0]["stop_loss"]


# --- 12: next-bar-open execution is unchanged.


def test_entry_executes_at_the_next_bar_open() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))
    params = make_params(direction="long", slippage_rate=Decimal("0"))

    candles, _, result, _ = run_vwap(specs, params)

    trade = result["trades"][0]
    assert trade["entry_time"] == candles[56]["timestamp"]
    assert trade["entry_price"] == candles[56]["open"]


# --- 13: normal and early-close forced exits work.


def test_forced_session_close_exit_when_no_stop_or_target_hit() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))
    params = make_params(direction="long", stop_multiple="50", reward_risk_multiple="50")  # unreachable stop/target

    candles, _, result, _ = run_vwap(specs, params)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[69]["timestamp"]


def test_forced_exit_on_a_short_early_close_style_session() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 60)  # warmup, never traded
    short_session = session_bar_specs(date(2026, 1, 3), 5)
    sustain_close_from(short_session, 1, Decimal("99"))  # index 0 has no prior bar to deviate from meaningfully; use index 1
    specs = specs + short_session
    params = make_params(direction="long", stop_multiple="50", reward_risk_multiple="50", maximum_entries_per_session=1)

    candles, _, result, _ = run_vwap(specs, params)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[-1]["timestamp"]


# --- 14: fees and slippage affect the outcome.


def test_fees_and_slippage_change_pnl() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))
    zero_cost_params = make_params(direction="long", fee_rate=Decimal("0"), slippage_rate=Decimal("0"), stop_multiple="50", reward_risk_multiple="50")
    with_cost_params = make_params(direction="long", fee_rate=Decimal("0.01"), slippage_rate=Decimal("0.01"), stop_multiple="50", reward_risk_multiple="50")

    _, _, zero_cost_result, _ = run_vwap(specs, zero_cost_params)
    _, _, with_cost_result, _ = run_vwap(specs, with_cost_params)

    assert zero_cost_result["trades"][0]["pnl"] != with_cost_result["trades"][0]["pnl"]


# --- 15: deterministic reruns are identical.


def test_deterministic_reruns_produce_identical_trades() -> None:
    specs = session_bar_specs(date(2026, 1, 2), 70)
    sustain_close_from(specs, 55, Decimal("99"))
    params = make_params(direction="long")

    _, _, result_1, _ = run_vwap(specs, params)
    _, _, result_2, _ = run_vwap(specs, params)

    assert result_1["trades"] == result_2["trades"]
    assert result_1["metrics"] == result_2["metrics"]


# --- 16: realistic-scale walk-forward regression guard (learned from the ORB pilot defect).


def test_produces_trades_on_a_realistic_scale_dataset_above_the_walk_forward_threshold() -> None:
    sessions = [session_bar_specs(date(2026, 1, 2) + timedelta(days=day), 20) for day in range(20)]
    specs = [bar for session in sessions for bar in session]  # 400 bars, comfortably over 80
    deviation_index = 15 * 20 + 5  # well inside the validation window regardless of split ratio
    sustain_close_from(specs, deviation_index, Decimal("99"))
    params = make_params(direction="long", maximum_entries_per_session=1)

    _, _, result, _ = run_vwap(specs, params)

    assert result["metrics"]["walk_forward"]["enabled"] is True
    assert len(result["trades"]) >= 1
