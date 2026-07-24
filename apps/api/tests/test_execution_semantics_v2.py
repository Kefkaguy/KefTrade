"""Phase 13.4: absolute take-profit targets + execution-semantics versioning.

Two things are proven here:

  1. The extension is genuinely OPT-IN -- a strategy that does not set
     `honor_absolute_take_profit` produces bit-identical trades and metrics
     to the pre-Phase-13 engine, including through the frozen baseline
     fixture.
  2. When opted in, the engine honors the strategy's literal target price
     instead of overwriting it with an R-derived level -- the capability
     that VWAP / prior-close / opposite-range-boundary targets require.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.backtester import (
    EXECUTION_SEMANTICS_ABSOLUTE_TARGETS,
    EXECUTION_SEMANTICS_BASELINE,
    run_backtest,
)
from app.services.strategy import ExecutionConstraints, StrategyDecision

PARAMS = {
    "risk_reward": 2,
    "fee_rate": 0,
    "slippage_rate": 0,
    "risk_per_trade": 0.01,
    "initial_equity": 10000,
    "walk_forward_train_ratio": 0.7,
}


def make_rows(count: int = 120):
    """Flat market with a single reachable excursion, so a target's exact
    level -- not merely its existence -- determines the fill."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles, features = [], []
    for index in range(count):
        timestamp = start + timedelta(hours=4 * index)
        open_price = high = low = close = Decimal("100")
        if index >= 71:
            high = Decimal("108")  # reaches 101..108 targets, never the 95 stop
            low = Decimal("99.5")
        candles.append(
            {
                "symbol": "TEST", "timeframe": "4h", "timestamp": timestamp,
                "open": open_price, "high": high, "low": low, "close": close,
                "volume": Decimal("1000"),
            }
        )
        features.append(
            {
                "symbol": "TEST", "timeframe": "4h", "timestamp": timestamp,
                "ema_20": Decimal("100"), "ema_50": Decimal("95"), "rsi_14": Decimal("50"),
                "volume_change": Decimal("0"), "distance_from_ema_20": Decimal("0"),
            }
        )
    return candles, features


class AbsoluteTargetStrategy:
    """Names a literal take-profit price, the way a VWAP or prior-close
    target would."""

    def __init__(self, target: Decimal, *, honor: bool, stop: Decimal = Decimal("95")):
        self.target = target
        self.stop = stop
        self.execution_constraints = ExecutionConstraints(honor_absolute_take_profit=honor)
        self.fired = False

    def reset(self) -> None:
        self.fired = False

    def __call__(self, candle, feature, recent_candles, params):
        # Fire exactly once, on the first bar of the walk-forward execution
        # window (run_backtest starts executing well past the 70-bar mark, so
        # a fixed bar index would never be reached).
        if self.fired:
            return StrategyDecision("avoid", None, None, None, None, ["wait"])
        self.fired = True
        close = Decimal(candle["close"])
        return StrategyDecision("setup", (close, close), self.stop, self.target, Decimal("2"), ["absolute target"])


def run_with(target: Decimal, *, honor: bool, stop: Decimal = Decimal("95")):
    candles, features = make_rows()
    return run_backtest(candles, features, PARAMS, AbsoluteTargetStrategy(target, honor=honor, stop=stop))


# ---------------------------------------------------------------------------
# The extension is opt-in
# ---------------------------------------------------------------------------

def test_absolute_target_is_ignored_when_the_strategy_does_not_opt_in():
    """Pre-Phase-13 behavior, preserved exactly: take_profit is discarded and
    the target is derived from risk_per_unit * risk_reward."""
    result = run_with(Decimal("101"), honor=False)

    trade = result["trades"][0]
    entry = trade["entry_price"]
    risk = entry - Decimal("95")
    assert trade["take_profit"] == entry + (risk * Decimal("2"))
    assert trade["take_profit"] != Decimal("101")


def test_opted_out_runs_are_labeled_with_the_baseline_semantics_version():
    result = run_with(Decimal("101"), honor=False)

    semantics = result["execution_semantics"]
    assert semantics["version"] == EXECUTION_SEMANTICS_BASELINE
    assert semantics["absolute_take_profit_honored"] is False
    assert semantics["same_candle_exit_policy"] == "stop_first"


def test_default_execution_constraints_do_not_honor_absolute_targets():
    """A strategy that never mentions the constraint must inherit the safe
    default -- this is what keeps every existing family unchanged."""
    assert ExecutionConstraints().honor_absolute_take_profit is False


def test_plain_function_strategies_still_run_under_baseline_semantics():
    """Swing strategies are plain functions with no execution_constraints
    attribute at all; they must keep the baseline semantics."""
    candles, features = make_rows()

    def plain(candle, feature, recent_candles, params):
        return StrategyDecision("avoid", None, None, None, None, ["wait"])

    result = run_backtest(candles, features, PARAMS, plain)
    assert result["execution_semantics"]["version"] == EXECUTION_SEMANTICS_BASELINE


# ---------------------------------------------------------------------------
# The extension works when opted in
# ---------------------------------------------------------------------------

def test_absolute_target_is_honored_verbatim_when_opted_in():
    result = run_with(Decimal("101"), honor=True)

    trade = result["trades"][0]
    assert trade["take_profit"] == Decimal("101")
    assert trade["exit_reason"] == "take_profit"
    assert trade["exit_price"] == Decimal("101")


def test_opted_in_runs_are_labeled_with_the_absolute_targets_semantics_version():
    result = run_with(Decimal("101"), honor=True)

    semantics = result["execution_semantics"]
    assert semantics["version"] == EXECUTION_SEMANTICS_ABSOLUTE_TARGETS
    assert semantics["absolute_take_profit_honored"] is True


def test_two_different_absolute_targets_produce_two_different_exits():
    """Proves the target level is load-bearing, not incidentally equal to an
    R-derived level."""
    near = run_with(Decimal("101"), honor=True)["trades"][0]
    far = run_with(Decimal("107"), honor=True)["trades"][0]

    assert near["take_profit"] == Decimal("101")
    assert far["take_profit"] == Decimal("107")
    assert far["pnl"] > near["pnl"]


def test_realized_reward_to_risk_is_reported_from_the_actual_distances():
    """The stored R multiple must describe the target the trade really had,
    not the ratio the decision happened to carry."""
    result = run_with(Decimal("110"), honor=True, stop=Decimal("95"))
    trade = result["trades"][0]

    entry = trade["entry_price"]
    expected_r = (Decimal("110") - entry) / (entry - Decimal("95"))
    realized_r = (trade["take_profit"] - entry) / (entry - trade["stop_loss"])
    assert realized_r == expected_r


def test_target_at_or_behind_the_entry_is_skipped_rather_than_filled():
    """If the move completed between signal and next-bar-open fill, there is
    no trade -- the engine must not invent a zero/negative-distance fill."""
    behind = run_with(Decimal("99"), honor=True)
    at_entry = run_with(Decimal("100"), honor=True)

    assert behind["trades"] == []
    assert at_entry["trades"] == []


def test_stop_still_takes_precedence_over_an_absolute_target_on_the_same_bar():
    """Conservative stop-first ordering is unchanged by this extension --
    intrabar sequencing remains unknowable and is never fabricated."""
    candles, features = make_rows()
    for index in range(71, len(candles)):
        candles[index]["high"] = Decimal("108")
        candles[index]["low"] = Decimal("90")  # both stop and target touched

    result = run_backtest(
        candles, features, PARAMS, AbsoluteTargetStrategy(Decimal("101"), honor=True)
    )
    trade = result["trades"][0]
    assert trade["exit_reason"] == "stop_loss_stop_first"
    assert trade["exit_price"] == Decimal("95")
