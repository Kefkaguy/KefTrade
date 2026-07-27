"""Phase A: an automated audit of the shared execution path.

Before concluding that a family of strategies has no edge, the simulator that
judged it has to be verified. Campaign 101 screened 19 families and found none
worth expanding, with several showing deeply negative expectancy -- a result
that is either a genuine negative finding or an artifact of the one code path
every family shares. This module decides which, using deterministic synthetic
price series whose correct answer is known before the simulator runs.

Every check here is a *falsifiable* statement about `run_backtest`, not a
regression snapshot: each builds a price path where the right answer follows
from arithmetic (rising prices must pay a long, costs must reduce returns,
reversing a signal must reverse gross P&L), runs the real simulator, and
compares. A check that fails means the simulator is wrong, not that a
threshold moved.

The audit deliberately separates two kinds of finding:

  * `defect`  -- the simulator computes something incorrectly.
  * `bias`    -- the simulator is arithmetically correct but systematically
                 optimistic or pessimistic, so results are real but skewed.
  * `economics` -- the simulator and the strategy are both behaving as
                 written, and the configuration itself is uneconomic.

That distinction is the whole point: only the first justifies changing the
backtester, and only the third justifies concluding the strategies were
never viable as configured.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from app.services.backtester import run_backtest
from app.services.strategy import StrategyDecision

AUDIT_VERSION = "simulator_audit_v1"

# A cost burden above this share of the risk unit means the configuration
# cannot realistically be profitable regardless of signal quality -- it is
# reported as an `economics` finding, never as a simulator defect.
COST_BURDEN_R_WARNING_THRESHOLD = 0.25


@dataclass(frozen=True)
class AuditCheck:
    name: str
    category: str
    passed: bool
    detail: str
    finding_type: str | None = None
    observed: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Synthetic price construction
# ---------------------------------------------------------------------------

BASE_PARAMS: dict[str, Any] = {
    "risk_reward": Decimal("2"),
    "fee_rate": Decimal("0"),
    "slippage_rate": Decimal("0"),
    "risk_per_trade": Decimal("0.01"),
    "initial_equity": Decimal("10000"),
    "walk_forward_train_ratio": 0.7,
    "max_holding_bars": 0,
}


def _params(**overrides: Any) -> dict[str, Any]:
    return {**BASE_PARAMS, **overrides}


_SERIES_START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
_SERIES_STEP = timedelta(minutes=30)


def _bar_index(candle: dict[str, Any]) -> int:
    """Absolute row index of a candle, recovered from its timestamp.

    The simulator does not start calling a strategy at row 0 -- it skips the
    training window -- so a strategy that counted its own invocations would
    be numbering bars from the wrong origin. Deriving the index from the
    timestamp keeps 'signal on row N' meaning the same thing here as it does
    in the series that was built.
    """
    return int((candle["timestamp"] - _SERIES_START) / _SERIES_STEP)


def _series(
    closes: list[float],
    *,
    wick: float = 0.25,
    opens: list[float] | None = None,
    symbol: str = "TEST",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a candle/feature pair from a close path.

    Each bar opens at the previous bar's close unless `opens` overrides it,
    so the path is continuous by default and any gap in a test is deliberate.
    Highs/lows extend `wick` beyond the bar's own open/close range, which is
    small enough that a stop placed several points away is never grazed by
    construction -- so a stop-out in these tests always means a real move.
    """
    candles: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    for index, close in enumerate(closes):
        open_price = opens[index] if opens is not None else (closes[index - 1] if index else close)
        timestamp = _SERIES_START + _SERIES_STEP * index
        candles.append(
            {
                "symbol": symbol,
                "timeframe": "30m",
                "timestamp": timestamp,
                "open": Decimal(str(open_price)),
                "high": Decimal(str(max(open_price, close) + wick)),
                "low": Decimal(str(min(open_price, close) - wick)),
                "close": Decimal(str(close)),
                "volume": Decimal("1000"),
            }
        )
        features.append({"symbol": symbol, "timeframe": "30m", "timestamp": timestamp})
    return candles, features


def _ramp(start_price: float, step: float, count: int) -> list[float]:
    return [start_price + step * index for index in range(count)]


def _bracket_strategy(
    *,
    direction: str,
    stop_distance: float,
    signal_at: int | None = None,
) -> Callable[..., StrategyDecision]:
    """A strategy with no opinion beyond direction: it signals on every bar
    (or exactly one bar) and brackets the trade a fixed distance away. Any
    P&L it produces comes from the price path and the simulator alone."""

    def decide(candle, feature, recent_candles, params) -> StrategyDecision:
        if signal_at is not None and _bar_index(candle) != signal_at:
            return StrategyDecision("avoid", None, None, None, None, ["not the signal bar"])
        close = Decimal(candle["close"])
        offset = Decimal(str(stop_distance))
        stop = close - offset if direction == "long" else close + offset
        target = close + offset if direction == "long" else close - offset
        return StrategyDecision("setup", (close, close), stop, target, None, ["audit"], direction=direction)

    return decide


def _run(closes: list[float], decide: Callable[..., StrategyDecision], **param_overrides: Any) -> dict[str, Any]:
    candles, features = _series(closes)
    return run_backtest(candles, features, _params(**param_overrides), decide)


# ---------------------------------------------------------------------------
# Direction and sign
# ---------------------------------------------------------------------------

def check_long_profits_on_rising_prices() -> AuditCheck:
    """Continuously rising prices must pay a long. If this fails nothing else
    in the audit matters."""
    result = _run(_ramp(100.0, 1.0, 200), _bracket_strategy(direction="long", stop_distance=5.0))
    metrics = result["metrics"]
    winners = [trade for trade in result["trades"] if trade["pnl"] > 0]
    passed = metrics["number_of_trades"] > 0 and metrics["total_return"] > 0 and len(winners) == metrics["number_of_trades"]
    return AuditCheck(
        name="long_profits_on_rising_prices",
        category="direction",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="A long on a monotonically rising path must finish profitable on every trade.",
        observed={
            "trades": metrics["number_of_trades"],
            "total_return": metrics["total_return"],
            "winning_trades": len(winners),
        },
    )


def check_short_profits_on_falling_prices() -> AuditCheck:
    """The mirror image. A sign error in short P&L would show up here and
    nowhere else, because every long-only test would still pass."""
    result = _run(_ramp(300.0, -1.0, 200), _bracket_strategy(direction="short", stop_distance=5.0))
    metrics = result["metrics"]
    winners = [trade for trade in result["trades"] if trade["pnl"] > 0]
    passed = metrics["number_of_trades"] > 0 and metrics["total_return"] > 0 and len(winners) == metrics["number_of_trades"]
    return AuditCheck(
        name="short_profits_on_falling_prices",
        category="direction",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="A short on a monotonically falling path must finish profitable on every trade.",
        observed={
            "trades": metrics["number_of_trades"],
            "total_return": metrics["total_return"],
            "winning_trades": len(winners),
        },
    )


def check_signal_reversal_reverses_gross_pnl() -> AuditCheck:
    """With symmetric exits and no costs, flipping the signal must flip gross
    P&L exactly.

    Compared on a single trade, deliberately. Position size is derived from
    *current* equity, so across a sequence the winning side compounds up and
    the losing side compounds down and the two stop being mirror images --
    that divergence is correct risk-based sizing, not an asymmetry in the
    P&L arithmetic. Isolating one trade holds equity identical between the
    two runs, which is the only condition under which exact cancellation is
    the right expectation.
    """
    closes = _ramp(100.0, 0.7, 200)
    shared = {"max_holding_bars": 5}
    long_result = _run(closes, _bracket_strategy(direction="long", stop_distance=500.0, signal_at=150), **shared)
    short_result = _run(closes, _bracket_strategy(direction="short", stop_distance=500.0, signal_at=150), **shared)
    if not long_result["trades"] or not short_result["trades"]:
        return AuditCheck(
            "signal_reversal_reverses_gross_pnl", "direction", False, "No trade was produced.", "defect", {}
        )
    long_trade = long_result["trades"][0]
    short_trade = short_result["trades"][0]
    long_gross = float(long_trade["gross_pnl"])
    short_gross = float(short_trade["gross_pnl"])
    combined = long_gross + short_gross
    scale = max(abs(long_gross), abs(short_gross), 1.0)
    same_fill = long_trade["entry_price"] == short_trade["entry_price"] and long_trade["exit_price"] == short_trade["exit_price"]
    passed = same_fill and abs(combined) / scale < 1e-9
    return AuditCheck(
        name="signal_reversal_reverses_gross_pnl",
        category="direction",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="Long and short gross P&L for one trade on an identical path must sum to zero.",
        observed={
            "long_gross_pnl": long_gross,
            "short_gross_pnl": short_gross,
            "sum": combined,
            "identical_fills": same_fill,
        },
    )


def check_mean_reversion_is_rewarded() -> AuditCheck:
    """A dip-buy on a sawtooth that always recovers must be paid. This
    exercises a different exit branch (target hit after an adverse leg) than
    the trend checks."""
    closes: list[float] = []
    while len(closes) < 200:
        closes.extend([100.0, 97.0, 95.0, 97.0, 100.0, 103.0])
    closes = closes[:200]

    def decide(candle, feature, recent_candles, params) -> StrategyDecision:
        close = Decimal(candle["close"])
        if close > Decimal("96"):
            return StrategyDecision("avoid", None, None, None, None, ["not a dip"])
        return StrategyDecision(
            "setup", (close, close), close - Decimal("6"), close + Decimal("6"), None, ["dip"], direction="long"
        )

    result = _run(closes, decide)
    metrics = result["metrics"]
    passed = metrics["number_of_trades"] > 0 and metrics["expectancy_per_trade"] > 0
    return AuditCheck(
        name="mean_reversion_is_rewarded",
        category="direction",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="Buying a controlled dip that always recovers must produce positive expectancy.",
        observed={"trades": metrics["number_of_trades"], "expectancy": metrics["expectancy_per_trade"]},
    )


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------

def check_costs_reduce_performance() -> AuditCheck:
    """Adding fees and slippage must strictly reduce net P&L and must never
    improve it. A sign error on either would show up as an improvement."""
    closes = _ramp(100.0, 1.0, 200)
    strategy = lambda: _bracket_strategy(direction="long", stop_distance=5.0)  # noqa: E731
    free = _run(closes, strategy())
    costly = _run(closes, strategy(), fee_rate=Decimal("0.001"), slippage_rate=Decimal("0.0005"))
    free_return = free["metrics"]["total_return"]
    costly_return = costly["metrics"]["total_return"]
    fees = sum(float(trade["fees"]) for trade in costly["trades"])
    slippage = sum(float(trade["slippage_cost"]) for trade in costly["trades"])
    passed = costly_return < free_return and fees > 0 and slippage > 0
    return AuditCheck(
        name="costs_reduce_performance",
        category="costs",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="Fees and slippage must strictly reduce net return and be recorded as positive costs.",
        observed={
            "return_without_costs": free_return,
            "return_with_costs": costly_return,
            "total_fees": fees,
            "total_slippage": slippage,
        },
    )


def check_fees_charged_on_both_legs() -> AuditCheck:
    fee_rate = Decimal("0.001")
    result = _run(
        _ramp(100.0, 1.0, 200),
        _bracket_strategy(direction="long", stop_distance=5.0, signal_at=150),
        fee_rate=fee_rate,
    )
    if not result["trades"]:
        return AuditCheck("fees_charged_on_both_legs", "costs", False, "No trade was produced.", "defect", {})
    trade = result["trades"][0]
    expected = (trade["entry_price"] * trade["quantity"] * fee_rate) + (trade["exit_price"] * trade["quantity"] * fee_rate)
    passed = abs(float(trade["fees"] - expected)) < 1e-9
    return AuditCheck(
        name="fees_charged_on_both_legs",
        category="costs",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="Fees must equal entry notional x rate plus exit notional x rate.",
        observed={"recorded_fees": float(trade["fees"]), "expected_fees": float(expected)},
    )


def check_slippage_is_adverse_in_both_directions() -> AuditCheck:
    """Slippage must always work against the trader: worse entry, worse exit,
    on both sides of the market."""
    slippage = Decimal("0.001")
    long_result = _run(
        _ramp(100.0, 1.0, 200),
        _bracket_strategy(direction="long", stop_distance=5.0, signal_at=150),
        slippage_rate=slippage,
    )
    short_result = _run(
        _ramp(300.0, -1.0, 200),
        _bracket_strategy(direction="short", stop_distance=5.0, signal_at=150),
        slippage_rate=slippage,
    )
    if not long_result["trades"] or not short_result["trades"]:
        return AuditCheck(
            "slippage_is_adverse_in_both_directions", "costs", False, "No trade was produced.", "defect", {}
        )
    long_trade = long_result["trades"][0]
    short_trade = short_result["trades"][0]
    long_raw_entry = Decimal(long_trade["entry_candle"]["open"])
    short_raw_entry = Decimal(short_trade["entry_candle"]["open"])
    long_adverse = long_trade["entry_price"] > long_raw_entry
    short_adverse = short_trade["entry_price"] < short_raw_entry
    passed = long_adverse and short_adverse
    return AuditCheck(
        name="slippage_is_adverse_in_both_directions",
        category="costs",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="A long must fill above the open and a short below it; slippage may never be a credit.",
        observed={
            "long_entry_vs_open": float(long_trade["entry_price"] - long_raw_entry),
            "short_entry_vs_open": float(short_trade["entry_price"] - short_raw_entry),
        },
    )


# ---------------------------------------------------------------------------
# Execution mechanics
# ---------------------------------------------------------------------------

def check_entry_fills_at_next_bar_open() -> AuditCheck:
    """The bar that produces the signal must not be tradeable. Filling at the
    signal bar's close is the classic lookahead that makes a dead strategy
    look alive."""
    closes = _ramp(100.0, 1.0, 200)
    candles, features = _series(closes)
    result = run_backtest(
        candles, features, _params(), _bracket_strategy(direction="long", stop_distance=5.0, signal_at=150)
    )
    if not result["trades"]:
        return AuditCheck("entry_fills_at_next_bar_open", "timing", False, "No trade was produced.", "defect", {})
    trade = result["trades"][0]
    signal_bar = candles[150]
    next_bar = candles[151]
    passed = trade["entry_time"] == next_bar["timestamp"] and trade["entry_price"] == next_bar["open"]
    return AuditCheck(
        name="entry_fills_at_next_bar_open",
        category="timing",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="A signal on bar i must fill at bar i+1's open, never at bar i's close.",
        observed={
            "signal_bar_time": signal_bar["timestamp"].isoformat(),
            "entry_time": trade["entry_time"].isoformat(),
            "entry_price": float(trade["entry_price"]),
            "next_bar_open": float(next_bar["open"]),
        },
    )


def check_no_lookahead_in_decision_inputs() -> AuditCheck:
    """The strategy must never be handed a bar dated after the one it is
    deciding on."""
    violations: list[str] = []

    def decide(candle, feature, recent_candles, params) -> StrategyDecision:
        for row in recent_candles:
            if row["timestamp"] > candle["timestamp"]:
                violations.append(row["timestamp"].isoformat())
        return StrategyDecision("avoid", None, None, None, None, ["observer"])

    _run(_ramp(100.0, 1.0, 200), decide)
    passed = not violations
    return AuditCheck(
        name="no_lookahead_in_decision_inputs",
        category="lookahead",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="Every candle offered to a strategy must be dated at or before the decision bar.",
        observed={"future_bars_observed": len(violations)},
    )


def check_stop_takes_priority_over_target_in_one_candle() -> AuditCheck:
    """When a single bar touches both the stop and the target, the simulator
    must assume the worse of the two. Assuming the target instead would
    silently inflate every strategy that uses wide bars."""
    closes = [100.0] * 200
    opens = [100.0] * 200
    closes[151] = 100.0
    candles, features = _series(closes, opens=opens, wick=0.25)
    # Bar 151 straddles both brackets: entry at 100, stop 95, target 105.
    candles[151]["high"] = Decimal("106")
    candles[151]["low"] = Decimal("94")
    result = run_backtest(
        candles, features, _params(), _bracket_strategy(direction="long", stop_distance=5.0, signal_at=150)
    )
    if not result["trades"]:
        return AuditCheck(
            "stop_takes_priority_over_target_in_one_candle", "exits", False, "No trade was produced.", "defect", {}
        )
    trade = result["trades"][0]
    passed = str(trade["exit_reason"]).startswith("stop_loss") and trade["pnl"] < 0
    return AuditCheck(
        name="stop_takes_priority_over_target_in_one_candle",
        category="exits",
        passed=passed,
        finding_type=None if passed else "bias",
        detail="A bar touching both brackets must resolve as the stop, not the target.",
        observed={"exit_reason": trade["exit_reason"], "pnl": float(trade["pnl"])},
    )


def check_position_sizing_respects_risk_per_trade() -> AuditCheck:
    """Quantity must be set so that a stop-out costs exactly the configured
    fraction of equity, before costs."""
    risk_per_trade = Decimal("0.01")
    equity = Decimal("10000")
    result = _run(
        _ramp(100.0, 1.0, 200),
        _bracket_strategy(direction="long", stop_distance=5.0, signal_at=150),
        risk_per_trade=risk_per_trade,
        initial_equity=equity,
    )
    if not result["trades"]:
        return AuditCheck(
            "position_sizing_respects_risk_per_trade", "sizing", False, "No trade was produced.", "defect", {}
        )
    trade = result["trades"][0]
    implied_risk = trade["quantity"] * trade["risk_per_unit"]
    expected_risk = equity * risk_per_trade
    passed = abs(float(implied_risk - expected_risk)) < 1e-6
    return AuditCheck(
        name="position_sizing_respects_risk_per_trade",
        category="sizing",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="quantity x risk_per_unit must equal equity x risk_per_trade.",
        observed={"implied_risk": float(implied_risk), "expected_risk": float(expected_risk)},
    )


def check_return_and_expectancy_units_are_consistent() -> AuditCheck:
    """`pnl_pct` is a fraction of starting equity and `expectancy_per_trade`
    is a currency amount. Both are legitimate, but they are different units
    and must not be compared to each other by downstream code."""
    result = _run(_ramp(100.0, 1.0, 200), _bracket_strategy(direction="long", stop_distance=5.0))
    metrics = result["metrics"]
    trades = result["trades"]
    if not trades:
        return AuditCheck(
            "return_and_expectancy_units_are_consistent", "units", False, "No trade was produced.", "defect", {}
        )
    initial = Decimal(str(BASE_PARAMS["initial_equity"]))
    pct_ok = all(abs(float(trade["pnl_pct"] - (trade["pnl"] / initial))) < 1e-12 for trade in trades)
    total_ok = abs(
        metrics["total_return"] - float((Decimal(str(metrics["final_equity"])) - initial) / initial)
    ) < 1e-9
    passed = pct_ok and total_ok
    return AuditCheck(
        name="return_and_expectancy_units_are_consistent",
        category="units",
        passed=passed,
        finding_type=None if passed else "defect",
        detail="pnl_pct must be pnl/initial_equity and total_return must be (final-initial)/initial.",
        observed={
            "pnl_pct_consistent": pct_ok,
            "total_return_consistent": total_ok,
            "expectancy_unit": "currency_per_trade",
            "pnl_pct_unit": "fraction_of_initial_equity",
        },
    )


# ---------------------------------------------------------------------------
# Dataset boundaries and gaps
# ---------------------------------------------------------------------------

def check_trades_occur_only_in_the_validation_window() -> AuditCheck:
    """The simulator skips the training window entirely rather than trading
    it. That is correct given parameters come from a fixed grid and nothing
    is fitted per symbol -- but it means every trade a job produces is dated
    after `walk_forward.validation_start`, so the `train`/`validation` tag on
    stored trades can only ever take one value. Any downstream check that
    treats those tags as two independent samples is comparing a set with
    itself."""
    result = _run(_ramp(100.0, 1.0, 200), _bracket_strategy(direction="long", stop_distance=5.0))
    walk_forward = result["metrics"]["walk_forward"]
    if not walk_forward.get("enabled"):
        return AuditCheck(
            "trades_occur_only_in_the_validation_window",
            "dataset_split",
            False,
            "Walk-forward was not enabled for a 200-row series.",
            "defect",
            {},
        )
    validation_start = datetime.fromisoformat(walk_forward["validation_start"])
    before = [trade for trade in result["trades"] if trade["entry_time"] < validation_start]
    passed = result["trades"] and not before
    return AuditCheck(
        name="trades_occur_only_in_the_validation_window",
        category="dataset_split",
        passed=bool(passed),
        finding_type=None if passed else "defect",
        detail=(
            "No trade may be dated before validation_start. Consequence: the stored dataset_split tag "
            "is always 'validation' and never 'train', so it cannot be used as an out-of-sample split."
        ),
        observed={
            "validation_start": walk_forward["validation_start"],
            "trades": len(result["trades"]),
            "trades_before_validation_start": len(before),
            "implication": "dataset_split is degenerate; train/validation are not two independent samples",
        },
    )


def check_stop_fill_on_a_gap_through_is_optimistic() -> AuditCheck:
    """When a bar gaps straight through the stop, the simulator still fills at
    the stop price. Real execution would fill at the gapped open, which is
    worse. This is a known optimistic bias: it can only make results look
    better than reality, so it can never explain a negative result."""
    closes = [100.0] * 200
    opens = [100.0] * 200
    opens[152] = 80.0
    closes[152] = 80.0
    candles, features = _series(closes, opens=opens, wick=0.1)
    result = run_backtest(
        candles, features, _params(), _bracket_strategy(direction="long", stop_distance=5.0, signal_at=150)
    )
    if not result["trades"]:
        return AuditCheck(
            "stop_fill_on_a_gap_through_is_optimistic", "gaps", True, "No trade was produced.", None, {}
        )
    trade = result["trades"][0]
    gapped_open = Decimal("80")
    filled_at_stop = abs(float(trade["exit_price"] - Decimal("95"))) < 1e-9
    return AuditCheck(
        name="stop_fill_on_a_gap_through_is_optimistic",
        category="gaps",
        # Reported, not failed: the arithmetic is correct, the assumption is generous.
        passed=True,
        finding_type="bias" if filled_at_stop else None,
        detail=(
            "A bar gapping through the stop fills at the stop price rather than the gapped open. "
            "Optimistic bias -- it inflates results and cannot explain a negative one."
        ),
        observed={
            "stop_price": 95.0,
            "gapped_open": float(gapped_open),
            "filled_exit_price": float(trade["exit_price"]),
            "filled_at_stop_price": filled_at_stop,
        },
    )


# ---------------------------------------------------------------------------
# Cost economics: correct arithmetic, uneconomic configuration
# ---------------------------------------------------------------------------

def cost_break_even_analysis(
    *,
    fee_rate: float,
    slippage_rate: float,
    stop_distance_pct: float,
    reward_risk_multiple: float,
) -> dict[str, Any]:
    """How much of one risk unit (R) a round trip costs, and what win rate is
    then required to break even.

    The derivation matters, because the result is counter-intuitive. With
    risk-based sizing, quantity is set so a stop-out loses exactly R:

        q = (equity x risk_per_trade) / d = R / d      where d = stop distance

    Costs are charged on notional, so for price P:

        cost = 2 x (fee_rate + slippage_rate) x P x q
             = 2 x (fee_rate + slippage_rate) x P x R / d

    Writing the stop as a fraction of price, k = d / P, the P and R cancel:

        cost_in_R = 2 x (fee_rate + slippage_rate) / k

    So cost as a share of risk depends *only* on the cost rates and how tight
    the stop is -- not on account size, price, or position size. Tightening
    the stop raises the position size proportionally, which raises notional
    costs proportionally, while the profit target (m x R) does not grow at
    all. Tight stops are therefore self-defeating under percentage-of-notional
    costs, and the effect is identical for every strategy family.

    Break-even win rate then follows from  w x m - (1 - w) x 1 - c = 0:

        w = (1 + c) / (m + 1)
    """
    if stop_distance_pct <= 0:
        raise ValueError("stop_distance_pct must be positive")
    cost_in_r = 2.0 * (fee_rate + slippage_rate) / stop_distance_pct
    required_win_rate = (1.0 + cost_in_r) / (reward_risk_multiple + 1.0)
    gross_break_even_win_rate = 1.0 / (reward_risk_multiple + 1.0)
    return {
        "stop_distance_pct": stop_distance_pct,
        "reward_risk_multiple": reward_risk_multiple,
        "cost_in_r": round(cost_in_r, 6),
        "required_win_rate_after_costs": round(required_win_rate, 6),
        "required_win_rate_before_costs": round(gross_break_even_win_rate, 6),
        "win_rate_penalty_from_costs": round(required_win_rate - gross_break_even_win_rate, 6),
        "achievable": required_win_rate < 1.0,
    }


def check_cost_burden_of_live_intraday_parameters() -> AuditCheck:
    """Run the break-even analysis on the parameters the intraday families
    actually use, across a realistic range of intraday stop distances."""
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    fee_rate = float(BASE_V2_PARAMETERS["fee_rate"])
    slippage_rate = float(BASE_V2_PARAMETERS["slippage_rate"])
    reward = float(BASE_V2_PARAMETERS["reward_risk_multiple"])
    table = [
        cost_break_even_analysis(
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            stop_distance_pct=stop_pct,
            reward_risk_multiple=reward,
        )
        for stop_pct in (0.002, 0.003, 0.005, 0.01, 0.02)
    ]
    worst = max(row["cost_in_r"] for row in table)
    passed = worst <= COST_BURDEN_R_WARNING_THRESHOLD
    return AuditCheck(
        name="cost_burden_of_live_intraday_parameters",
        category="economics",
        passed=passed,
        finding_type=None if passed else "economics",
        detail=(
            f"Round-trip costs consume up to {worst:.2f}R at these rates and stop distances. "
            "The simulator arithmetic is correct; the configuration is uneconomic."
        ),
        observed={
            "fee_rate": fee_rate,
            "slippage_rate": slippage_rate,
            "reward_risk_multiple": reward,
            "round_trip_cost_rate": round(2 * (fee_rate + slippage_rate), 6),
            "by_stop_distance": table,
        },
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_CHECKS: tuple[Callable[[], AuditCheck], ...] = (
    check_long_profits_on_rising_prices,
    check_short_profits_on_falling_prices,
    check_signal_reversal_reverses_gross_pnl,
    check_mean_reversion_is_rewarded,
    check_costs_reduce_performance,
    check_fees_charged_on_both_legs,
    check_slippage_is_adverse_in_both_directions,
    check_entry_fills_at_next_bar_open,
    check_no_lookahead_in_decision_inputs,
    check_stop_takes_priority_over_target_in_one_candle,
    check_position_sizing_respects_risk_per_trade,
    check_return_and_expectancy_units_are_consistent,
    check_trades_occur_only_in_the_validation_window,
    check_stop_fill_on_a_gap_through_is_optimistic,
    check_cost_burden_of_live_intraday_parameters,
)


def run_simulator_audit() -> dict[str, Any]:
    """Run every invariant and return a structured verdict.

    `simulator_sound` is deliberately narrower than `passed`: it answers only
    "does the shared execution path compute correctly", so an uneconomic cost
    configuration does not get reported as a broken backtester.
    """
    checks = [check() for check in ALL_CHECKS]
    defects = [check for check in checks if check.finding_type == "defect"]
    biases = [check for check in checks if check.finding_type == "bias"]
    economics = [check for check in checks if check.finding_type == "economics"]
    return {
        "audit_version": AUDIT_VERSION,
        "checks_run": len(checks),
        "passed": all(check.passed for check in checks),
        "simulator_sound": not defects,
        "defects": [check.name for check in defects],
        "optimistic_or_pessimistic_biases": [check.name for check in biases],
        "economics_findings": [check.name for check in economics],
        "verdict": (
            "Shared execution path verified; no defect found."
            if not defects
            else "Shared execution path has defects -- fix the simulator before judging any strategy."
        ),
        "checks": [asdict(check) for check in checks],
    }
