"""The cheap test that runs before a campaign.

Every series here is synthetic and built so the right answer is known before
the measurement runs: a signal that fires before real up-moves must score, a
coin-flip signal must not, and — the case that matters most — a signal with no
skill on a rising market must NOT be credited for the drift it merely sat in.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.signal_diagnostics import (
    MINIMUM_SIGNALS_FOR_A_VERDICT,
    MINIMUM_T_STATISTIC,
    measure_signal_edge,
    round_trip_cost_bps,
    summarize_edge,
)
from app.services.strategy import StrategyDecision

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def _rows(closes, *, opens=None):
    rows = []
    for index, close in enumerate(closes):
        open_price = opens[index] if opens is not None else (closes[index - 1] if index else close)
        timestamp = START + timedelta(minutes=30 * index)
        rows.append(
            {
                "candle": {
                    "symbol": "TEST",
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "open": Decimal(str(open_price)),
                    "high": Decimal(str(max(open_price, close))),
                    "low": Decimal(str(min(open_price, close))),
                    "close": Decimal(str(close)),
                    "volume": Decimal("1000"),
                },
                "feature": {"timestamp": timestamp},
            }
        )
    return rows


def _setup(direction="long"):
    close = Decimal("100")
    return StrategyDecision("setup", (close, close), None, None, None, ["test"], direction=direction)


def _avoid():
    return StrategyDecision("avoid", None, None, None, None, ["test"])


def _flat_with_jumps(count=600, period=7, jump=0.004):
    """Flat except for a jump every `period` bars, so a signal that fires one
    bar before each jump has genuine foresight."""
    closes = [100.0]
    for index in range(1, count):
        closes.append(closes[-1] * (1 + jump) if index % period == 0 else closes[-1])
    return closes


# ---------------------------------------------------------------------------
# Detecting a signal that is really there
# ---------------------------------------------------------------------------

def test_a_signal_that_fires_before_real_moves_is_detected():
    rows = _rows(_flat_with_jumps())

    # Entry fills at bar i+1's open, which equals bar i's close; the jump lands
    # on the bar after that. Firing at i == period-2 captures it.
    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 5 else _avoid()

    measurement = measure_signal_edge(rows, decide, {}, horizons=(1, 2, 4))
    summary = summarize_edge(measurement, cost_bps=1.0)

    assert measurement["signal_count"] >= MINIMUM_SIGNALS_FOR_A_VERDICT
    assert summary["excess_edge_bps"] > 0
    assert summary["t_statistic"] > MINIMUM_T_STATISTIC
    assert summary["verdict"] == "predictive"


def test_a_short_signal_before_real_drops_is_detected():
    closes = [100.0]
    for index in range(1, 600):
        closes.append(closes[-1] * (1 - 0.004) if index % 7 == 0 else closes[-1])
    rows = _rows(closes)

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup("short") if index % 7 == 5 else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2)), cost_bps=1.0)

    assert summary["excess_edge_bps"] > 0
    assert summary["verdict"] == "predictive"


# ---------------------------------------------------------------------------
# The case that matters most: drift is not skill
# ---------------------------------------------------------------------------

def test_a_skill_free_signal_on_a_rising_market_earns_no_credit():
    """A long-only signal with random timing on a steadily rising market shows
    a large POSITIVE raw return and zero real edge. Reporting the raw number
    would call every long-only family on a bull market predictive."""
    rows = _rows([100.0 * (1.0015**index) for index in range(600)])

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 5 == 0 else _avoid()

    measurement = measure_signal_edge(rows, decide, {}, horizons=(4,))
    summary = summarize_edge(measurement, cost_bps=1.0)
    horizon = measurement["by_horizon"][0]

    assert horizon["raw_edge_bps"] > 50, "the drift alone should look impressive"
    assert abs(horizon["excess_edge_bps"]) < 1e-6, "but the timing added nothing"
    assert summary["verdict"] == "no_signal"


def test_raw_edge_and_excess_differ_by_exactly_the_drift():
    rows = _rows([100.0 * (1.001**index) for index in range(400)])

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 4 == 0 else _avoid()

    horizon = measure_signal_edge(rows, decide, {}, horizons=(2,))["by_horizon"][0]

    assert horizon["raw_edge_bps"] - horizon["excess_edge_bps"] == pytest.approx(
        horizon["unconditional_drift_bps"], abs=1e-3
    )


# ---------------------------------------------------------------------------
# Rejecting signals that are not there
# ---------------------------------------------------------------------------

def test_a_coin_flip_signal_on_a_random_walk_is_rejected():
    import random

    rng = random.Random(7)
    price = 100.0
    closes = []
    for _ in range(1200):
        price *= 1 + rng.gauss(0, 0.002)
        closes.append(price)
    rows = _rows(closes)
    flips = random.Random(11)

    def decide(candle, feature, recent, params):
        return _setup() if flips.random() < 0.2 else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2, 4, 8)), cost_bps=1.0)

    assert summary["verdict"] == "no_signal"
    assert abs(summary["t_statistic"]) < MINIMUM_T_STATISTIC


def test_a_signal_that_rarely_fires_gets_no_verdict():
    """A spectacular mean over nine observations is not a measurement."""
    rows = _rows(_flat_with_jumps())

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index in (60, 67, 74) else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2)), cost_bps=1.0)

    assert summary["verdict"] == "insufficient_signals"
    assert summary["clears_cost"] is False


def test_a_signal_that_never_fires_is_handled():
    rows = _rows([100.0] * 300)
    summary = summarize_edge(
        measure_signal_edge(rows, lambda *args: _avoid(), {}, horizons=(1,)), cost_bps=1.0
    )

    assert summary["verdict"] == "insufficient_signals"


# ---------------------------------------------------------------------------
# The cost comparison is the decision
# ---------------------------------------------------------------------------

def test_a_real_signal_smaller_than_costs_is_named_as_such():
    """The distinction that decides what to do next: 'no signal' means retire,
    'signal below cost' means widen the stop or lengthen the hold."""
    rows = _rows(_flat_with_jumps(jump=0.0006))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 5 else _avoid()

    summary = summarize_edge(measure_signal_edge(rows, decide, {}, horizons=(1, 2)), cost_bps=30.0)

    assert summary["statistically_significant"] is True
    assert summary["clears_cost"] is False
    assert summary["verdict"] == "signal_below_cost"
    assert "not the problem" in summary["detail"]


def test_the_same_signal_clears_a_realistic_cost():
    rows = _rows(_flat_with_jumps(jump=0.0006))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 5 else _avoid()

    measurement = measure_signal_edge(rows, decide, {}, horizons=(1, 2))

    assert summarize_edge(measurement, cost_bps=30.0)["verdict"] == "signal_below_cost"
    assert summarize_edge(measurement, cost_bps=1.0)["verdict"] == "predictive"


def test_the_cost_comes_from_the_live_configuration():
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    expected = 2 * (float(BASE_V2_PARAMETERS["fee_rate"]) + float(BASE_V2_PARAMETERS["slippage_rate"])) * 10_000

    assert round_trip_cost_bps() == pytest.approx(expected)


# ---------------------------------------------------------------------------
# No lookahead
# ---------------------------------------------------------------------------

def test_a_signal_fired_on_the_bar_that_already_moved_captures_nothing():
    """The jump happens inside bar j, between its open and its close. A signal
    reading bar j can only fill at bar j+1's open — after the move — so it must
    earn nothing and score below the drift it missed. Filling at the signal
    bar's close instead would show a large fake edge here."""
    rows = _rows(_flat_with_jumps(period=7, jump=0.01))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 0 else _avoid()

    horizon = measure_signal_edge(rows, decide, {}, horizons=(1,))["by_horizon"][0]

    assert horizon["raw_edge_bps"] == pytest.approx(0.0, abs=1e-6)
    assert horizon["excess_edge_bps"] < 0


def test_a_signal_fired_one_bar_early_does_capture_the_move():
    """The mirror of the test above, so the two together pin the fill
    convention rather than just asserting a negative number."""
    rows = _rows(_flat_with_jumps(period=7, jump=0.01))

    def decide(candle, feature, recent, params):
        index = int((candle["timestamp"] - START) / timedelta(minutes=30))
        return _setup() if index % 7 == 6 else _avoid()

    horizon = measure_signal_edge(rows, decide, {}, horizons=(1,))["by_horizon"][0]

    assert horizon["raw_edge_bps"] > 90
    assert horizon["excess_edge_bps"] > 0


def test_the_best_horizon_is_chosen_by_significance_not_size():
    """The largest edge in a sweep is often the noisiest; picking it is how a
    horizon sweep becomes a selection bias."""
    measurement = {
        "signal_count": 500,
        "by_horizon": [
            {"horizon_bars": 2, "signals": 500, "raw_edge_bps": 5.0, "unconditional_drift_bps": 0.0,
             "excess_edge_bps": 5.0, "t_statistic": 6.0, "hit_rate": 0.6},
            {"horizon_bars": 32, "signals": 500, "raw_edge_bps": 40.0, "unconditional_drift_bps": 0.0,
             "excess_edge_bps": 40.0, "t_statistic": 1.2, "hit_rate": 0.52},
        ],
    }

    summary = summarize_edge(measurement, cost_bps=1.0)

    assert summary["best_horizon_bars"] == 2
    assert summary["excess_edge_bps"] == 5.0


def test_the_significance_bar_is_above_the_conventional_two():
    """Several horizons are tested and the best kept, so 2.0 would under-state
    the real false-positive rate."""
    assert MINIMUM_T_STATISTIC > 2.0
