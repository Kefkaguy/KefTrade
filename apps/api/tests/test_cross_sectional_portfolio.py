"""Phase D: the ranking is evaluated as one portfolio process.

Every universe here is synthetic and constructed so the correct answer is
known before the backtester runs -- a persistent-momentum universe must
produce a positive information coefficient, a mean-reverting one must produce
a negative one, and a universe with no cross-sectional structure must produce
neither.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.labs.intraday.cross_sectional_portfolio import (
    MINIMUM_ELIGIBLE_SYMBOLS,
    PortfolioConfig,
    build_target_weights,
    evaluate_cross_sectional_campaign,
    run_cross_sectional_portfolio_backtest,
    spearman,
)

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def _candles(symbol, closes, *, volume=1_000_000):
    """Opens equal the previous close, so execution at the next bar's open is
    continuous with the ranking bar's close."""
    rows = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        rows.append(
            {
                "symbol": symbol,
                "timeframe": "30m",
                "timestamp": START + timedelta(minutes=30 * index),
                "open": open_price,
                "high": max(open_price, close),
                "low": min(open_price, close),
                "close": close,
                "volume": volume,
            }
        )
    return rows


def _persistent_momentum_universe(count=120):
    """Each symbol has a fixed drift. The strongest performers over the
    lookback keep being the strongest, so ranking must carry signal."""
    drifts = {"A": 0.004, "B": 0.002, "C": 0.001, "D": -0.001, "E": -0.002, "F": -0.004}
    universe = {}
    for symbol, drift in drifts.items():
        price = 100.0
        closes = []
        for _ in range(count):
            price *= 1.0 + drift
            closes.append(price)
        universe[symbol] = _candles(symbol, closes)
    return universe


def _mean_reverting_universe(count=120):
    """Every symbol alternates direction every bar-block, so whoever led over
    the lookback is about to lag."""
    universe = {}
    for offset, symbol in enumerate(("A", "B", "C", "D", "E", "F")):
        price = 100.0
        closes = []
        for index in range(count):
            direction = 1 if ((index + offset) // 4) % 2 == 0 else -1
            price *= 1.0 + direction * 0.004
            closes.append(price)
        universe[symbol] = _candles(symbol, closes)
    return universe


def _flat_universe(count=120):
    universe = {}
    for symbol in ("A", "B", "C", "D", "E", "F"):
        universe[symbol] = _candles(symbol, [100.0] * count)
    return universe


ZERO_COST = PortfolioConfig(fee_rate=0.0, slippage_rate=0.0, lookback_bars=4)


# ---------------------------------------------------------------------------
# Rank correlation primitive
# ---------------------------------------------------------------------------

def test_spearman_detects_a_perfect_monotonic_relationship():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_is_none_when_undefined():
    assert spearman([1, 2], [3, 4]) is None
    assert spearman([1, 1, 1], [1, 2, 3]) is None


def test_spearman_handles_ties_without_favoring_a_symbol():
    assert spearman([1, 1, 2, 2], [5, 5, 9, 9]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Portfolio construction
# ---------------------------------------------------------------------------

def test_both_legs_are_formed_simultaneously():
    ranked = [(f"S{index}", 1.0 - index / 10) for index in range(10)]

    weights, _ = build_target_weights(ranked, PortfolioConfig(long_quantile=0.2, short_quantile=0.2))

    assert sum(1 for value in weights.values() if value > 0) == 2
    assert sum(1 for value in weights.values() if value < 0) == 2


def test_a_long_short_book_is_dollar_neutral():
    ranked = [(f"S{index}", 1.0 - index / 10) for index in range(10)]

    weights, _ = build_target_weights(ranked, PortfolioConfig())

    assert sum(weights.values()) == pytest.approx(0.0)
    assert sum(abs(value) for value in weights.values()) == pytest.approx(1.0)


def test_a_long_only_book_is_fully_long():
    """max_weight is raised so the per-name cap does not bind here -- a
    long-only book concentrates into half as many names as a long/short one,
    so the default cap would legitimately hold gross below target."""
    ranked = [(f"S{index}", 1.0 - index / 10) for index in range(10)]

    weights, cap_bound = build_target_weights(ranked, PortfolioConfig(long_only=True, max_weight=0.5))

    assert all(value > 0 for value in weights.values())
    assert sum(weights.values()) == pytest.approx(1.0)
    assert cap_bound is False


def test_the_legs_never_overlap():
    """A symbol cannot be both the strongest and the weakest; overlapping legs
    would net to a phantom zero position."""
    ranked = [(f"S{index}", 1.0 - index / 5) for index in range(5)]

    weights, _ = build_target_weights(ranked, PortfolioConfig(long_quantile=0.8, short_quantile=0.8))

    assert len(weights) <= 5
    assert all(abs(value) > 0 for value in weights.values())


def test_the_per_name_cap_binds_rather_than_being_silently_violated():
    ranked = [(f"S{index}", 1.0 - index / 10) for index in range(10)]

    weights, cap_bound = build_target_weights(ranked, PortfolioConfig(max_weight=0.1))

    assert cap_bound is True
    assert all(abs(value) <= 0.1 + 1e-12 for value in weights.values())
    assert sum(abs(value) for value in weights.values()) < 1.0


def test_too_few_symbols_is_not_a_cross_section():
    ranked = [("A", 1.0), ("B", 0.5)]

    weights, _ = build_target_weights(ranked, PortfolioConfig())

    assert weights == {}
    assert MINIMUM_ELIGIBLE_SYMBOLS == 4


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def test_persistent_momentum_produces_a_positive_information_coefficient():
    result = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(), config=ZERO_COST
    )

    assert result["evaluable"] is True
    assert result["information_coefficient"]["mean"] > 0.5
    assert result["top_minus_bottom_spread"] > 0
    assert result["total_return"] > 0


def test_mean_reversion_produces_a_negative_momentum_information_coefficient():
    result = run_cross_sectional_portfolio_backtest(
        _mean_reverting_universe(), config=ZERO_COST
    )

    assert result["information_coefficient"]["mean"] < 0


def test_the_reversal_flag_recovers_the_edge_a_momentum_ranking_loses():
    """Same universe, same machinery, inverted ranking -- the reversal family
    must not need a second code path."""
    momentum = run_cross_sectional_portfolio_backtest(_mean_reverting_universe(), config=ZERO_COST)
    reversal = run_cross_sectional_portfolio_backtest(
        _mean_reverting_universe(), config=ZERO_COST, reversal=True
    )

    assert reversal["information_coefficient"]["mean"] == pytest.approx(
        -momentum["information_coefficient"]["mean"], abs=1e-6
    )
    assert reversal["top_minus_bottom_spread"] > momentum["top_minus_bottom_spread"]


def test_a_universe_with_no_cross_sectional_structure_yields_no_signal():
    result = run_cross_sectional_portfolio_backtest(_flat_universe(), config=ZERO_COST)

    assert result["evaluable"] is False or result["information_coefficient"]["mean"] in (None, 0.0)


# ---------------------------------------------------------------------------
# Portfolio properties
# ---------------------------------------------------------------------------

def test_the_book_is_dollar_neutral():
    result = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(), config=ZERO_COST
    )

    assert result["is_dollar_neutral"] is True
    assert abs(result["dollar_neutrality"]) < 0.05


def test_beta_is_none_when_the_market_has_no_dispersion_to_regress_against():
    """A constant market return leaves variance at floating-point noise.
    Dividing by that manufactures an enormous beta out of nothing, so the
    estimator must decline instead."""
    from app.services.labs.intraday.cross_sectional_portfolio import _beta

    assert _beta([0.001, 0.002, 0.003, 0.004], [0.0005] * 4) is None

    result = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(),
        config=PortfolioConfig(lookback_bars=4, fee_rate=0.001, slippage_rate=0.0005),
    )
    assert result["market_beta"] is None


def test_beta_is_measured_when_the_market_actually_varies():
    from app.services.labs.intraday.cross_sectional_portfolio import _beta

    market = [0.01, -0.02, 0.03, -0.01, 0.02]
    assert _beta([2 * value for value in market], market) == pytest.approx(2.0)


def test_a_long_only_book_is_not_market_neutral():
    """The measurement that the per-symbol path could never make: a long-only
    reading of the same ranking is mostly a bet on the market."""
    config = PortfolioConfig(fee_rate=0.0, slippage_rate=0.0, lookback_bars=4, long_only=True)
    result = run_cross_sectional_portfolio_backtest(_persistent_momentum_universe(), config=config)

    assert result["is_dollar_neutral"] is False
    assert result["dollar_neutrality"] == pytest.approx(1.0)
    assert result["short_leg_mean_return"] is None


def test_costs_reduce_the_portfolio_return_and_are_charged_on_traded_notional():
    free = run_cross_sectional_portfolio_backtest(_persistent_momentum_universe(), config=ZERO_COST)
    costly = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(),
        config=PortfolioConfig(fee_rate=0.001, slippage_rate=0.0005, lookback_bars=4),
    )

    assert costly["total_return"] < free["total_return"]
    assert costly["cost_drag_per_rebalance"] > 0
    assert free["cost_drag_per_rebalance"] == 0.0


def test_a_stable_ranking_turns_over_less_than_a_churning_one():
    stable = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(), config=ZERO_COST
    )
    churning = run_cross_sectional_portfolio_backtest(
        _mean_reverting_universe(), config=ZERO_COST
    )

    assert stable["mean_one_way_turnover"] < churning["mean_one_way_turnover"]


def test_longer_holding_reduces_turnover():
    fast = run_cross_sectional_portfolio_backtest(
        _mean_reverting_universe(),
        config=PortfolioConfig(fee_rate=0.0, slippage_rate=0.0, lookback_bars=4, holding_bars=1),
    )
    slow = run_cross_sectional_portfolio_backtest(
        _mean_reverting_universe(),
        config=PortfolioConfig(fee_rate=0.0, slippage_rate=0.0, lookback_bars=4, holding_bars=8),
    )

    assert slow["rebalances"] < fast["rebalances"]


def test_both_legs_are_reported_separately():
    result = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(), config=ZERO_COST
    )

    assert result["long_leg_mean_return"] > result["short_leg_mean_return"]
    assert result["top_minus_bottom_spread"] == pytest.approx(
        result["long_leg_mean_return"] - result["short_leg_mean_return"], abs=1e-9
    )


def test_capacity_scales_with_traded_volume():
    thin = {symbol: _candles(symbol, [c["close"] for c in rows], volume=1_000)
            for symbol, rows in _persistent_momentum_universe().items()}
    deep = {symbol: _candles(symbol, [c["close"] for c in rows], volume=10_000_000)
            for symbol, rows in _persistent_momentum_universe().items()}

    thin_result = run_cross_sectional_portfolio_backtest(thin, config=ZERO_COST)
    deep_result = run_cross_sectional_portfolio_backtest(deep, config=ZERO_COST)

    assert deep_result["capacity_usd_median"] > thin_result["capacity_usd_median"]


def test_sector_exposure_is_reported_as_unavailable_rather_than_estimated():
    result = run_cross_sectional_portfolio_backtest(
        _persistent_momentum_universe(), config=ZERO_COST
    )

    assert result["sector_exposure"] is None
    assert "unavailable" in result["sector_exposure_note"]


def test_sector_exposure_is_computed_when_a_map_is_supplied():
    config = PortfolioConfig(
        fee_rate=0.0,
        slippage_rate=0.0,
        lookback_bars=4,
        sector_by_symbol={"A": "tech", "B": "tech", "C": "tech", "D": "energy", "E": "energy", "F": "energy"},
    )
    result = run_cross_sectional_portfolio_backtest(_persistent_momentum_universe(), config=config)

    assert result["sector_exposure"] is not None
    assert set(result["sector_exposure"]) <= {"tech", "energy"}
    assert result["sector_exposure_note"] is None


def test_an_unevaluable_universe_says_so_rather_than_returning_zeros():
    result = run_cross_sectional_portfolio_backtest(
        {"A": _candles("A", [100.0] * 20), "B": _candles("B", [100.0] * 20)}, config=ZERO_COST
    )

    assert result["evaluable"] is False
    assert "reason" in result


# ---------------------------------------------------------------------------
# Campaign evaluation
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, campaign, rows, manifest, candles):
        self.campaign = campaign
        self.rows = rows
        self.manifest = manifest
        self.candles = candles

    def execute(self, query, params=None):
        stripped = query.strip()
        outer = self

        class Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return self._rows

        if stripped.startswith("SELECT id, dataset_id, controls"):
            return Result([outer.campaign] if outer.campaign else [])
        if stripped.startswith("SELECT DISTINCT"):
            return Result(outer.rows)
        if stripped.startswith("SELECT assets"):
            return Result([outer.manifest])
        raise AssertionError(f"unexpected query: {stripped[:60]}")


def test_campaign_evaluation_runs_one_portfolio_per_configuration(monkeypatch):
    universe = _persistent_momentum_universe()
    monkeypatch.setattr(
        "app.services.research_architecture.load_snapshot_candles",
        lambda conn, dataset_id, symbol, timeframe: universe.get(symbol, []),
    )
    conn = FakeConn(
        campaign={"id": 101, "dataset_id": 7, "controls": {}},
        rows=[{"architecture": "cross_sectional_momentum_v2", "lookback_bars": "4", "timeframe": "30m"}],
        manifest={"assets": list(universe)},
        candles=universe,
    )

    report = evaluate_cross_sectional_campaign(conn, 101, config=ZERO_COST)

    assert len(report["configurations"]) == 1
    assert report["configurations"][0]["portfolio"]["evaluable"] is True
    assert "not comparable to the per-symbol job results" in report["interpretation"]


def test_campaign_evaluation_reports_when_there_is_nothing_cross_sectional():
    conn = FakeConn(
        campaign={"id": 101, "dataset_id": 7, "controls": {}},
        rows=[],
        manifest={"assets": []},
        candles={},
    )

    report = evaluate_cross_sectional_campaign(conn, 101)

    assert report["configurations"] == []
    assert "no cross-sectional candidates" in report["note"]


def test_campaign_evaluation_requires_a_dataset_snapshot():
    conn = FakeConn(campaign={"id": 101, "dataset_id": None, "controls": {}}, rows=[], manifest={}, candles={})

    with pytest.raises(ValueError, match="no dataset snapshot"):
        evaluate_cross_sectional_campaign(conn, 101)
