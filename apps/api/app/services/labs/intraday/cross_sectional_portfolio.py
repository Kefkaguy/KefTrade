"""Phase D: evaluate cross-sectional families as one portfolio process.

A cross-sectional strategy's edge lives in *relative* ranking, not in any
single symbol's behavior. Backtesting each ranked symbol on its own and then
averaging the results -- what the current campaign path does, via
`run_cross_sectional_campaign_job` -> `evaluate_candidate` -> `run_backtest`
per symbol -- destroys exactly the thing being tested:

  * The rank is reduced to a per-symbol entry filter ("am I in the top
    decile?") when the actual claim is "the top decile outperforms the bottom
    decile". A strategy can be right about the spread and still lose money on
    every leg in a falling market, and the per-symbol view scores it as a
    failure.
  * Long and short legs never coexist, so market exposure is never cancelled.
    A long-only reading of a market-neutral strategy is mostly a bet on the
    market, and its drawdowns are the market's.
  * There is no portfolio: no turnover, no netting of offsetting positions,
    no weight constraints, and no way for costs to be charged on the actual
    traded notional rather than per isolated round trip.
  * Per-symbol trade counts are tiny, so the per-job elite gate rejects the
    family on sample size even when the portfolio has hundreds of rebalances
    of evidence.

This module runs the process the strategy actually describes. At each
rebalance it builds the eligible universe from information available at that
timestamp, ranks it, forms both legs simultaneously, applies weight
constraints, and measures the portfolio -- returns, turnover, costs,
exposure, neutrality, and the rank information coefficient that says whether
the ranking carries any signal at all.

No lookahead: ranking uses closes up to and including bar i, and execution
happens at bar i+1's open, the same convention `run_backtest` uses.

Two limitations are reported rather than hidden. Sector exposure requires a
sector map this system does not store, so it is reported as unavailable
unless one is supplied -- never estimated. Capacity is a participation-limit
estimate from traded dollar volume, not a market-impact model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean, pstdev
from typing import Any, Sequence

CROSS_SECTIONAL_PORTFOLIO_VERSION = "cross_sectional_portfolio_v1"

# Below this many eligible symbols a "cross-section" is not one.
MINIMUM_ELIGIBLE_SYMBOLS = 4


@dataclass(frozen=True)
class PortfolioConfig:
    """Portfolio construction rules. Costs are per unit of traded notional."""

    lookback_bars: int = 8
    holding_bars: int = 1
    long_quantile: float = 0.2
    short_quantile: float = 0.2
    long_only: bool = False
    max_weight: float = 0.25
    gross_leverage: float = 1.0
    fee_rate: float = 0.0001
    slippage_rate: float = 0.0002
    participation_limit: float = 0.01
    sector_by_symbol: dict[str, str] = field(default_factory=dict)

    @property
    def round_trip_cost_rate(self) -> float:
        return self.fee_rate + self.slippage_rate


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties never arbitrarily favor one symbol."""
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and values[ordered[j + 1]] == values[ordered[i]]:
            j += 1
        average_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            result[ordered[k]] = average_rank
        i = j + 1
    return result


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation. None when it is not defined rather than 0.0, so
    "no relationship" and "not measurable" stay distinguishable."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    x_ranks = _ranks(xs)
    y_ranks = _ranks(ys)
    x_mean = fmean(x_ranks)
    y_mean = fmean(y_ranks)
    covariance = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_ranks, y_ranks))
    x_var = sum((x - x_mean) ** 2 for x in x_ranks)
    y_var = sum((y - y_mean) ** 2 for y in y_ranks)
    if x_var <= 0 or y_var <= 0:
        return None
    return covariance / ((x_var * y_var) ** 0.5)


def _beta(portfolio: Sequence[float], market: Sequence[float]) -> float | None:
    """Regression beta against the equal-weight universe return.

    Returns None when the market series has no meaningful dispersion to
    regress against. The floor is *relative* to the size of the returns, not
    a test against exact zero: a market whose return is constant still has a
    variance of ~1e-38 from floating-point noise, and dividing by that
    produces an enormous beta out of nothing.
    """
    if len(portfolio) != len(market) or len(portfolio) < 3:
        return None
    market_mean = fmean(market)
    variance = sum((value - market_mean) ** 2 for value in market)
    scale = max((abs(value) for value in market), default=0.0)
    noise_floor = max(1e-24, (scale**2) * len(market) * 1e-12)
    if variance <= noise_floor:
        return None
    portfolio_mean = fmean(portfolio)
    covariance = sum((p - portfolio_mean) * (m - market_mean) for p, m in zip(portfolio, market))
    return covariance / variance


# ---------------------------------------------------------------------------
# Portfolio construction
# ---------------------------------------------------------------------------

def build_target_weights(
    ranked_symbols: Sequence[tuple[str, float]],
    config: PortfolioConfig,
) -> tuple[dict[str, float], bool]:
    """Equal-weight the top and bottom quantiles into two simultaneous legs.

    `ranked_symbols` is (symbol, percentile) sorted strongest-first. Returns
    the target weights and whether the per-name cap bound. When it binds the
    leg is deliberately left under its target gross rather than being forced
    back up by concentrating into fewer names -- the constraint is real and
    silently violating it would misreport the strategy's true exposure.
    """
    count = len(ranked_symbols)
    if count < MINIMUM_ELIGIBLE_SYMBOLS:
        return {}, False

    long_count = max(1, int(round(count * config.long_quantile)))
    longs = ranked_symbols[:long_count]
    shorts: Sequence[tuple[str, float]] = ()
    if not config.long_only:
        short_count = max(1, int(round(count * config.short_quantile)))
        # Never let the legs overlap: a symbol cannot be both the strongest
        # and the weakest, and allowing it would net to a phantom zero.
        short_count = min(short_count, count - long_count)
        if short_count <= 0:
            return {}, False
        shorts = ranked_symbols[-short_count:]

    leg_gross = config.gross_leverage if config.long_only else config.gross_leverage / 2.0
    cap_bound = False
    weights: dict[str, float] = {}

    long_weight = leg_gross / len(longs)
    if long_weight > config.max_weight:
        long_weight = config.max_weight
        cap_bound = True
    for symbol, _ in longs:
        weights[symbol] = long_weight

    if shorts:
        short_weight = leg_gross / len(shorts)
        if short_weight > config.max_weight:
            short_weight = config.max_weight
            cap_bound = True
        for symbol, _ in shorts:
            weights[symbol] = -short_weight

    return weights, cap_bound


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_cross_sectional_portfolio_backtest(
    candles_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    config: PortfolioConfig | None = None,
    reversal: bool = False,
) -> dict[str, Any]:
    """Run the ranking as one portfolio process and measure it as one.

    `reversal` inverts the ranking, so the same machinery evaluates
    cross-sectional reversal (buy the losers) without a second code path.
    """
    from app.services.labs.intraday.cross_sectional import compute_cross_sectional_percentiles

    config = config or PortfolioConfig()
    percentiles = compute_cross_sectional_percentiles(
        candles_by_symbol, lookback_bars=config.lookback_bars
    )

    candles_by_time: dict[str, dict[datetime, dict[str, Any]]] = {
        symbol: {row["timestamp"]: row for row in rows} for symbol, rows in candles_by_symbol.items()
    }
    ranking_times: set[datetime] = set()
    for symbol_percentiles in percentiles.values():
        ranking_times.update(symbol_percentiles)
    timeline = sorted(ranking_times)

    periods: list[dict[str, Any]] = []
    previous_weights: dict[str, float] = {}
    equity = 1.0
    step = max(1, config.holding_bars)

    index = 0
    while index + 1 + step < len(timeline):
        signal_time = timeline[index]
        entry_time = timeline[index + 1]
        exit_time = timeline[index + 1 + step]

        eligible: list[tuple[str, float]] = []
        forward_returns: dict[str, float] = {}
        for symbol, symbol_percentiles in percentiles.items():
            percentile = symbol_percentiles.get(signal_time)
            if percentile is None:
                continue
            entry_candle = candles_by_time.get(symbol, {}).get(entry_time)
            exit_candle = candles_by_time.get(symbol, {}).get(exit_time)
            if not entry_candle or not exit_candle:
                continue
            entry_price = float(entry_candle["open"])
            if entry_price <= 0:
                continue
            eligible.append((symbol, percentile if not reversal else 1.0 - percentile))
            forward_returns[symbol] = (float(exit_candle["open"]) - entry_price) / entry_price

        if len(eligible) < MINIMUM_ELIGIBLE_SYMBOLS:
            index += step
            continue

        eligible.sort(key=lambda item: item[1], reverse=True)
        weights, cap_bound = build_target_weights(eligible, config)
        if not weights:
            index += step
            continue

        traded = sum(
            abs(weights.get(symbol, 0.0) - previous_weights.get(symbol, 0.0))
            for symbol in set(weights) | set(previous_weights)
        )
        cost = traded * config.round_trip_cost_rate
        gross_return = sum(weight * forward_returns[symbol] for symbol, weight in weights.items())
        net_return = gross_return - cost
        equity *= 1.0 + net_return

        long_symbols = [symbol for symbol, weight in weights.items() if weight > 0]
        short_symbols = [symbol for symbol, weight in weights.items() if weight < 0]
        market_return = fmean([forward_returns[symbol] for symbol, _ in eligible])

        capacity = None
        dollar_volumes = []
        for symbol, weight in weights.items():
            candle = candles_by_time[symbol][entry_time]
            volume = float(candle.get("volume") or 0)
            dollar_volume = float(candle["open"]) * volume
            if dollar_volume > 0 and abs(weight) > 0:
                dollar_volumes.append(config.participation_limit * dollar_volume / abs(weight))
        if dollar_volumes:
            capacity = min(dollar_volumes)

        sector_exposure: dict[str, float] = {}
        if config.sector_by_symbol:
            for symbol, weight in weights.items():
                sector = config.sector_by_symbol.get(symbol)
                if sector:
                    sector_exposure[sector] = round(sector_exposure.get(sector, 0.0) + weight, 8)

        periods.append(
            {
                "signal_time": signal_time,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "eligible_symbols": len(eligible),
                "gross_return": gross_return,
                "net_return": net_return,
                "cost": cost,
                "traded_notional_fraction": traded,
                "one_way_turnover": traded / 2.0,
                "gross_exposure": sum(abs(weight) for weight in weights.values()),
                "net_exposure": sum(weights.values()),
                "long_leg_return": fmean([forward_returns[symbol] for symbol in long_symbols]) if long_symbols else 0.0,
                "short_leg_return": fmean([forward_returns[symbol] for symbol in short_symbols]) if short_symbols else None,
                "market_return": market_return,
                "information_coefficient": spearman(
                    [percentile for _, percentile in eligible],
                    [forward_returns[symbol] for symbol, _ in eligible],
                ),
                "capacity_usd": capacity,
                "sector_exposure": sector_exposure,
                "weight_cap_binding": cap_bound,
            }
        )
        previous_weights = weights
        index += step

    if previous_weights:
        # Closing the book is a real cost; omitting it would flatter the run.
        final_cost = sum(abs(weight) for weight in previous_weights.values()) * config.round_trip_cost_rate
        equity *= 1.0 - final_cost

    return _summarize(periods, equity, config, reversal=reversal)


def _summarize(
    periods: list[dict[str, Any]],
    equity: float,
    config: PortfolioConfig,
    *,
    reversal: bool,
) -> dict[str, Any]:
    if not periods:
        return {
            "portfolio_version": CROSS_SECTIONAL_PORTFOLIO_VERSION,
            "rebalances": 0,
            "evaluable": False,
            "reason": "no rebalance had enough eligible symbols to form a cross-section",
        }

    net_returns = [row["net_return"] for row in periods]
    gross_returns = [row["gross_return"] for row in periods]
    market_returns = [row["market_return"] for row in periods]
    ics = [row["information_coefficient"] for row in periods if row["information_coefficient"] is not None]
    spreads = [
        row["long_leg_return"] - row["short_leg_return"]
        for row in periods
        if row["short_leg_return"] is not None
    ]
    capacities = [row["capacity_usd"] for row in periods if row["capacity_usd"] is not None]
    gross_exposure = fmean([row["gross_exposure"] for row in periods])
    net_exposure = fmean([row["net_exposure"] for row in periods])

    ic_mean = fmean(ics) if ics else None
    ic_deviation = pstdev(ics) if len(ics) > 1 else None
    ic_t_stat = (
        ic_mean / (ic_deviation / (len(ics) ** 0.5)) if ic_mean is not None and ic_deviation else None
    )
    net_deviation = pstdev(net_returns) if len(net_returns) > 1 else None

    sector_exposure: dict[str, float] | None = None
    if config.sector_by_symbol:
        totals: dict[str, list[float]] = {}
        for row in periods:
            for sector, weight in row["sector_exposure"].items():
                totals.setdefault(sector, []).append(weight)
        sector_exposure = {sector: round(fmean(values), 6) for sector, values in sorted(totals.items())}

    return {
        "portfolio_version": CROSS_SECTIONAL_PORTFOLIO_VERSION,
        "evaluable": True,
        "reversal": reversal,
        "rebalances": len(periods),
        "total_return": round(equity - 1.0, 8),
        "mean_net_return_per_rebalance": round(fmean(net_returns), 10),
        "mean_gross_return_per_rebalance": round(fmean(gross_returns), 10),
        "cost_drag_per_rebalance": round(fmean([row["cost"] for row in periods]), 10),
        "return_volatility": round(net_deviation, 10) if net_deviation else None,
        "sharpe_per_rebalance": (
            round(fmean(net_returns) / net_deviation, 6) if net_deviation else None
        ),
        "information_coefficient": {
            "mean": round(ic_mean, 6) if ic_mean is not None else None,
            "standard_deviation": round(ic_deviation, 6) if ic_deviation is not None else None,
            "t_statistic": round(ic_t_stat, 6) if ic_t_stat is not None else None,
            "share_positive": round(sum(1 for value in ics if value > 0) / len(ics), 6) if ics else None,
            "observations": len(ics),
        },
        "top_minus_bottom_spread": round(fmean(spreads), 10) if spreads else None,
        "long_leg_mean_return": round(fmean([row["long_leg_return"] for row in periods]), 10),
        "short_leg_mean_return": (
            round(fmean([row["short_leg_return"] for row in periods if row["short_leg_return"] is not None]), 10)
            if any(row["short_leg_return"] is not None for row in periods)
            else None
        ),
        "market_mean_return": round(fmean(market_returns), 10),
        "market_beta": round(_beta(net_returns, market_returns), 6) if _beta(net_returns, market_returns) is not None else None,
        "mean_one_way_turnover": round(fmean([row["one_way_turnover"] for row in periods]), 6),
        "gross_exposure": round(gross_exposure, 6),
        "net_exposure": round(net_exposure, 6),
        "dollar_neutrality": round(net_exposure / gross_exposure, 6) if gross_exposure else None,
        "is_dollar_neutral": bool(gross_exposure and abs(net_exposure / gross_exposure) < 0.05),
        "capacity_usd_median": (
            round(sorted(capacities)[len(capacities) // 2], 2) if capacities else None
        ),
        "capacity_basis": (
            f"{config.participation_limit:.1%} participation in the entry bar's traded dollar volume; "
            "a participation limit, not a market-impact model"
        ),
        "sector_exposure": sector_exposure,
        "sector_exposure_note": (
            None if sector_exposure is not None
            else "unavailable: no sector map is stored for this universe; not estimated"
        ),
        "weight_cap_bound_share": round(
            sum(1 for row in periods if row["weight_cap_binding"]) / len(periods), 6
        ),
        "config": {
            "lookback_bars": config.lookback_bars,
            "holding_bars": config.holding_bars,
            "long_quantile": config.long_quantile,
            "short_quantile": config.short_quantile,
            "long_only": config.long_only,
            "max_weight": config.max_weight,
            "gross_leverage": config.gross_leverage,
            "fee_rate": config.fee_rate,
            "slippage_rate": config.slippage_rate,
        },
    }


# ---------------------------------------------------------------------------
# Campaign-level evaluation
# ---------------------------------------------------------------------------

def load_universe_candles(
    conn: Any, *, dataset_id: int, timeframe: str
) -> dict[str, list[dict[str, Any]]]:
    """Every symbol's candles from one immutable dataset snapshot."""
    from app.services.research_architecture import load_snapshot_candles

    manifest = conn.execute(
        "SELECT assets FROM research_dataset_manifests WHERE id = %s",
        (dataset_id,),
    ).fetchone()
    if not manifest:
        raise ValueError(f"No dataset manifest for dataset_id={dataset_id}.")
    universe = [str(item) for item in (manifest["assets"] or [])]
    candles = {symbol: load_snapshot_candles(conn, dataset_id, symbol, timeframe) for symbol in universe}
    return {symbol: rows for symbol, rows in candles.items() if rows}


def evaluate_cross_sectional_campaign(
    conn: Any,
    campaign_id: int,
    *,
    timeframe: str | None = None,
    config: PortfolioConfig | None = None,
) -> dict[str, Any]:
    """Re-evaluate a campaign's cross-sectional families as portfolios.

    Reads the campaign's own dataset snapshot and its cross-sectional
    candidates' parameters, then runs one portfolio backtest per distinct
    (architecture, lookback, holding) configuration. Deliberately does NOT
    write to `research_campaign_jobs` or any stage-evidence table: the stored
    per-symbol results were produced by a different process and overwriting
    them would destroy the record of what was actually run. This is an
    analysis, and the job-model change needed to make portfolios first-class
    is a separate decision.
    """
    campaign = conn.execute(
        "SELECT id, dataset_id, controls FROM research_campaigns WHERE id = %s",
        (campaign_id,),
    ).fetchone()
    if not campaign or campaign["dataset_id"] is None:
        raise ValueError(f"campaign {campaign_id} has no dataset snapshot to evaluate against")

    rows = conn.execute(
        """
        SELECT DISTINCT
            candidate->'parameters'->>'strategy_architecture' AS architecture,
            candidate->'parameters'->>'cross_sectional_lookback_bars' AS lookback_bars,
            timeframe
        FROM research_campaign_jobs
        WHERE campaign_id = %s
          AND candidate->'parameters'->>'strategy_architecture' IN (
              'cross_sectional_momentum_v2', 'cross_sectional_reversal_v2'
          )
        """,
        (campaign_id,),
    ).fetchall()
    if not rows:
        return {
            "campaign_id": campaign_id,
            "portfolio_version": CROSS_SECTIONAL_PORTFOLIO_VERSION,
            "configurations": [],
            "note": "campaign has no cross-sectional candidates",
        }

    dataset_id = int(campaign["dataset_id"])
    candles_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}
    results = []
    for row in rows:
        row_timeframe = timeframe or str(row["timeframe"])
        if row_timeframe not in candles_cache:
            candles_cache[row_timeframe] = load_universe_candles(
                conn, dataset_id=dataset_id, timeframe=row_timeframe
            )
        architecture = str(row["architecture"])
        lookback = int(row["lookback_bars"]) if row["lookback_bars"] else 8
        run_config = config or PortfolioConfig(lookback_bars=lookback)
        portfolio = run_cross_sectional_portfolio_backtest(
            candles_cache[row_timeframe],
            config=run_config,
            reversal=architecture == "cross_sectional_reversal_v2",
        )
        results.append({"architecture": architecture, "timeframe": row_timeframe, "portfolio": portfolio})

    return {
        "campaign_id": campaign_id,
        "dataset_id": dataset_id,
        "portfolio_version": CROSS_SECTIONAL_PORTFOLIO_VERSION,
        "configurations": results,
        "interpretation": (
            "These are portfolio-process results and are not comparable to the per-symbol job results "
            "stored for the same campaign, which evaluated the ranking as an entry filter on isolated "
            "symbols. Where the two disagree, the portfolio figure describes the strategy as specified."
        ),
    }
