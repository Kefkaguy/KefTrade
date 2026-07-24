"""Phase 12, Step 2A: generic simulator extension for intraday strategies.

Covers the acceptance tests required before ORB v1 implementation begins:
execution-constraint metadata, the single stable strategy calling
convention, strategy-owned state lifecycle (reset before every run), the
structural session-close exit cap, and the guarantee that no strategy-family
identity branching exists in the simulator.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import inspect

import pytest

from app.services import backtester
from app.services.backtester import combine_candles_features, find_exit_index, run_backtest
from app.services.labs.intraday.dataset import build_session_end_index
from app.services.strategy import (
    DEFAULT_EXECUTION_CONSTRAINTS,
    ExecutionConstraints,
    StrategyDecision,
    get_execution_constraints,
    reset_strategy_state,
)


PARAMS = {
    "risk_reward": 2,
    "fee_rate": Decimal("0.001"),
    "slippage_rate": Decimal("0.0005"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 1.0,
    "max_holding_bars": 0,
}


def make_session_rows(session_bar_counts: list[int]) -> tuple[list[dict], list[dict]]:
    """Builds flat candles/features for consecutive sessions of given bar counts.

    Every bar is flat (open=high=close=100, low=99) unless overridden by the
    caller after construction -- callers mutate specific bars in the
    returned candle dicts to script stop/target touches.
    """
    candles: list[dict] = []
    features: list[dict] = []
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    cursor = start
    for session_index, bar_count in enumerate(session_bar_counts):
        session_date = date(2026, 1, 2 + session_index)
        for bar_index in range(bar_count):
            timestamp = cursor
            cursor += timedelta(minutes=30)
            candles.append(
                {
                    "symbol": "TEST",
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "open": Decimal("100"),
                    "high": Decimal("100.5"),
                    "low": Decimal("99.5"),
                    "close": Decimal("100"),
                    "volume": Decimal("1000"),
                }
            )
            features.append(
                {
                    "symbol": "TEST",
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "session_date": session_date,
                    "minutes_from_open": bar_index * 30,
                    "minutes_to_close": (bar_count - 1 - bar_index) * 30,
                }
            )
    return candles, features


class ScriptedSessionStrategy:
    """Test double for a stateful intraday-style strategy.

    Fires exactly one "setup" at a caller-chosen bar index, tracks how many
    times it has been called and how many entries it produced -- used to
    prove state resets cleanly between runs/symbols/reruns.
    """

    execution_constraints = ExecutionConstraints(flat_by_session_close=True)

    def __init__(self, entry_index: int, stop_loss: Decimal, take_profit: Decimal):
        self.entry_index = entry_index
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.calls = 0
        self.entries_taken = 0
        self._index = -1

    def reset(self) -> None:
        self.calls = 0
        self.entries_taken = 0
        self._index = -1

    def __call__(self, candle, feature, recent_candles, params) -> StrategyDecision:
        self.calls += 1
        self._index += 1
        if self._index == self.entry_index:
            self.entries_taken += 1
            return StrategyDecision("setup", (Decimal("100"), Decimal("100")), self.stop_loss, self.take_profit, Decimal("2"), ["scripted entry"])
        return StrategyDecision("avoid", None, None, None, None, ["not the scripted bar"])


def plain_never_setup(candle, feature, recent_candles, params) -> StrategyDecision:
    return StrategyDecision("avoid", None, None, None, None, ["plain function, never fires"])


# --- 1. Execution constraints: plain functions default, no dynamic dispatch on identity.


def test_plain_function_has_default_execution_constraints() -> None:
    assert get_execution_constraints(plain_never_setup) is DEFAULT_EXECUTION_CONSTRAINTS
    assert get_execution_constraints(plain_never_setup).flat_by_session_close is False


def test_reset_strategy_state_is_a_no_op_for_plain_functions() -> None:
    reset_strategy_state(plain_never_setup)  # must not raise


def test_stateful_strategy_execution_constraints_are_read_from_the_instance() -> None:
    strategy = ScriptedSessionStrategy(entry_index=2, stop_loss=Decimal("50"), take_profit=Decimal("200"))
    assert get_execution_constraints(strategy).flat_by_session_close is True


# --- 2. Missing session metadata / malformed inputs fail honestly.


def test_flat_by_session_close_strategy_requires_session_end_index() -> None:
    candles, features = make_session_rows([6])
    strategy = ScriptedSessionStrategy(entry_index=1, stop_loss=Decimal("50"), take_profit=Decimal("200"))

    with pytest.raises(ValueError, match="session_end_index"):
        run_backtest(candles, features, PARAMS, strategy)


def test_session_end_index_length_mismatch_raises() -> None:
    candles, features = make_session_rows([6])
    strategy = ScriptedSessionStrategy(entry_index=1, stop_loss=Decimal("50"), take_profit=Decimal("200"))

    with pytest.raises(ValueError, match="1:1"):
        run_backtest(candles, features, PARAMS, strategy, session_end_index=[0, 1, 2])


# --- 3. Structural forced-flat exit: normal session and a short ("early close") session.


def test_forced_exit_at_normal_session_close_when_no_stop_or_target_hit() -> None:
    # `run_backtest` only starts evaluating signals at row 50 (existing
    # swing warmup convention, unrelated to this change), so the session
    # needs to be long enough for a signal to actually fire within it.
    candles, features = make_session_rows([70])
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)
    strategy = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))

    result = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[69]["timestamp"]


def test_forced_exit_on_a_short_session_mimics_early_close() -> None:
    # A 55-bar warmup session (never traded) followed by a 5-bar session
    # stands in for a real calendar early close: the simulator never needs
    # to know *why* the second session is short, only where it ends, which
    # comes entirely from session_end_index.
    candles, features = make_session_rows([55, 5])
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)
    # Call index 5 (rows 50..55) lands the signal on row 55, the first bar
    # of the short session.
    strategy = ScriptedSessionStrategy(entry_index=5, stop_loss=Decimal("50"), take_profit=Decimal("500"))

    result = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[59]["timestamp"]


# --- 4. Stops/targets before session close take precedence.


def test_stop_loss_before_session_close_wins() -> None:
    candles, features = make_session_rows([70])
    candles[58]["low"] = Decimal("40")  # stop touched well before session end (bar 69)
    strategy = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)

    result = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)

    trade = result["trades"][0]
    assert trade["exit_reason"].startswith("stop_loss")
    assert trade["exit_time"] == candles[58]["timestamp"]


def test_take_profit_before_session_close_wins() -> None:
    candles, features = make_session_rows([70])
    candles[58]["high"] = Decimal("300")
    strategy = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("10"), take_profit=Decimal("150"))
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)

    result = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "take_profit"
    assert trade["exit_time"] == candles[58]["timestamp"]


# --- 5. Exit scans never cross a session boundary.


def test_exit_scan_never_reads_into_the_next_session() -> None:
    # 65-bar session 1, 10-bar session 2 (75 rows total, under the 80-row
    # walk-forward split threshold so the whole set is in scope). If the
    # scan incorrectly read past the session boundary, this extreme low on
    # the FIRST bar of session 2 would be picked up as a stop-loss touch. It
    # must not be: the entry (session 1) should instead be forced flat at
    # session 1's own close.
    candles, features = make_session_rows([65, 10])
    candles[65]["low"] = Decimal("1")  # first bar of session 2
    strategy = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)

    result = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)

    trade = result["trades"][0]
    assert trade["exit_reason"] == "session_close"
    assert trade["exit_time"] == candles[64]["timestamp"]


def test_find_exit_index_caps_the_array_slice_at_the_session_boundary() -> None:
    candles, features = make_session_rows([10, 10])
    rows = combine_candles_features(candles, features)
    arrays = backtester.build_market_arrays(rows)
    session_end_index = build_session_end_index(rows)

    index, reason = find_exit_index(
        rows,
        arrays,
        start_index=4,
        stop_loss=Decimal("50"),
        take_profit=Decimal("500"),
        max_holding_bars=0,
        direction="long",
        session_end_index=session_end_index,
    )

    assert index == 9
    assert reason == "session_close"


# --- 6. Slippage and fees apply to session_close exits exactly like any other exit.


def test_session_close_exit_applies_slippage_and_fees() -> None:
    candles, features = make_session_rows([70])
    strategy = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)

    result = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)
    trade = result["trades"][0]

    slippage_rate = PARAMS["slippage_rate"]
    fee_rate = PARAMS["fee_rate"]
    expected_exit_price = Decimal(candles[69]["close"]) * (Decimal("1") - slippage_rate)
    assert trade["exit_price"] == expected_exit_price
    expected_fees = (trade["entry_price"] * trade["quantity"] * fee_rate) + (expected_exit_price * trade["quantity"] * fee_rate)
    expected_gross = (expected_exit_price - trade["entry_price"]) * trade["quantity"]
    assert trade["pnl"] == expected_gross - expected_fees


# --- 7. State isolation: reruns, and across symbols/parameter combinations.


def test_state_resets_between_repeated_runs_with_the_same_instance() -> None:
    candles, features = make_session_rows([70])
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)
    strategy = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))

    first = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)
    assert strategy.entries_taken == 1
    assert strategy.calls == strategy.entry_index + 1  # loop stops scanning once the entry fires

    second = run_backtest(candles, features, PARAMS, strategy, session_end_index=session_end_index)
    assert strategy.entries_taken == 1  # not 2 -- reset() zeroed it before this run
    assert strategy.calls == strategy.entry_index + 1  # not doubled

    assert first["trades"] == second["trades"]


def test_state_does_not_leak_across_symbols_or_parameter_combinations() -> None:
    strategy = ScriptedSessionStrategy(entry_index=2, stop_loss=Decimal("50"), take_profit=Decimal("500"))

    candles_a, features_a = make_session_rows([60])
    for candle in candles_a:
        candle["symbol"] = "SYMBOL_A"
    rows_a = combine_candles_features(candles_a, features_a)
    run_backtest(candles_a, features_a, PARAMS, strategy, session_end_index=build_session_end_index(rows_a))
    assert strategy.entries_taken == 1

    candles_b, features_b = make_session_rows([60])
    for candle in candles_b:
        candle["symbol"] = "SYMBOL_B"
    rows_b = combine_candles_features(candles_b, features_b)
    different_params = {**PARAMS, "risk_reward": 3}
    result_b = run_backtest(candles_b, features_b, different_params, strategy, session_end_index=build_session_end_index(rows_b))

    assert strategy.entries_taken == 1  # fresh for symbol B / different params, not 2
    assert result_b["trades"][0]["symbol"] == "SYMBOL_B"


def test_identical_inputs_produce_identical_outputs() -> None:
    candles, features = make_session_rows([70])
    rows = combine_candles_features(candles, features)
    session_end_index = build_session_end_index(rows)

    strategy_1 = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))
    strategy_2 = ScriptedSessionStrategy(entry_index=3, stop_loss=Decimal("50"), take_profit=Decimal("500"))

    result_1 = run_backtest(candles, features, PARAMS, strategy_1, session_end_index=session_end_index)
    result_2 = run_backtest(candles, features, PARAMS, strategy_2, session_end_index=session_end_index)

    assert result_1["trades"] == result_2["trades"]
    assert result_1["metrics"] == result_2["metrics"]


# --- 8. No strategy-family identity branching exists in the simulator.


def test_backtester_module_contains_no_strategy_family_identity_branching() -> None:
    source = inspect.getsource(backtester)
    forbidden_terms = ["opening_range", "orb_", "vwap_reversion", "gap_fill", "session_momentum", "strategy_family", "strategy ==", "family =="]
    lowered = source.lower()
    hits = [term for term in forbidden_terms if term.lower() in lowered]
    assert hits == [], f"backtester.py must stay strategy-agnostic; found forbidden identity terms: {hits}"
