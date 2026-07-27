"""Phase B: baselines and null models.

A profit factor above 1.0 is not evidence. It is a number that a coin-flip
entry rule produces roughly half the time on a drifting market. Before any
family is called promising it has to beat the thing it is competing with --
buying and holding, entering at random with the same frequency and holding
period, or a moving-average rule anyone can run for free -- and it has to
beat it by more than the spread of its own matched null distribution.

Everything here is a pure function over trade P&Ls or a candle series, is
deterministic given a seed, and takes costs explicitly. Nothing reads the
database, so a null model can be unit-tested against a known answer.

Two deliberate limitations, stated rather than hidden:

  * `deflated_sharpe_ratio` and `probability_of_backtest_overfitting` are
    the published estimators, but they inherit those papers' assumptions
    (i.i.d.-ish returns, a trial count that honestly reflects every variant
    tried). A trial count that undercounts the real search makes both look
    better than they should, so `trials` must come from the campaign's own
    variant count, never from a guess.
  * Symbol-shuffled cross-sectional ranks are NOT here. Shuffling symbols is
    only meaningful against a portfolio-level cross-sectional backtest,
    which does not exist yet (Phase D). Implementing it now against the
    current per-symbol evaluation would produce a null for a process the
    system is not actually running.
"""

from __future__ import annotations

import math
import random
from statistics import fmean, pstdev
from typing import Any, Sequence

NULL_MODEL_VERSION = "null_models_v1"

# Euler-Mascheroni constant, used by the expected-maximum-Sharpe term.
_EULER_MASCHERONI = 0.5772156649015329


# ---------------------------------------------------------------------------
# Normal distribution helpers (no scipy in this environment)
# ---------------------------------------------------------------------------

def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_ppf(probability: float) -> float:
    """Inverse normal CDF via Acklam's rational approximation.

    Accurate to ~1.15e-9 across the open interval, which is far tighter than
    anything the surrounding statistics can justify.
    """
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be strictly between 0 and 1")

    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    low, high = 0.02425, 1 - 0.02425

    if probability < low:
        q = math.sqrt(-2 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if probability > high:
        q = math.sqrt(-2 * math.log(1 - probability))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = probability - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def buy_and_hold_return(closes: Sequence[float]) -> float:
    """The return of simply owning the asset over the same window."""
    if len(closes) < 2 or closes[0] <= 0:
        return 0.0
    return (closes[-1] - closes[0]) / closes[0]


def moving_average_trend_return(
    closes: Sequence[float],
    *,
    window: int = 20,
    cost_per_switch: float = 0.0,
) -> float:
    """Long while price is above its moving average, flat otherwise.

    Charged `cost_per_switch` (as a fraction) each time the position changes,
    so a baseline that trades constantly is not credited as free.
    """
    if len(closes) <= window:
        return 0.0
    equity = 1.0
    position = 0
    for index in range(window, len(closes)):
        average = fmean(closes[index - window : index])
        desired = 1 if closes[index - 1] > average else 0
        if desired != position:
            equity *= 1.0 - cost_per_switch
            position = desired
        if position and closes[index - 1] > 0:
            equity *= closes[index] / closes[index - 1]
    return equity - 1.0


def random_entry_null_distribution(
    closes: Sequence[float],
    *,
    trade_count: int,
    holding_bars: int,
    direction: str = "long",
    samples: int = 1000,
    seed: int = 0,
    round_trip_cost: float = 0.0,
) -> list[float]:
    """Total return of `trade_count` randomly-timed trades of the same length.

    This is the null a strategy actually has to beat: same market, same
    number of bets, same holding period, same costs -- only the timing is
    uninformed. Returns one total-return figure per sample.
    """
    if trade_count <= 0 or holding_bars <= 0:
        return []
    last_entry = len(closes) - holding_bars - 1
    if last_entry <= 0:
        return []

    rng = random.Random(seed)
    sign = 1.0 if direction == "long" else -1.0
    distribution: list[float] = []
    for _ in range(samples):
        total = 0.0
        for _ in range(trade_count):
            entry_index = rng.randint(0, last_entry)
            entry = closes[entry_index]
            exit_price = closes[entry_index + holding_bars]
            if entry <= 0:
                continue
            total += sign * ((exit_price - entry) / entry) - round_trip_cost
        distribution.append(total)
    return sorted(distribution)


def time_shuffled_null_distribution(
    trade_returns: Sequence[float],
    *,
    samples: int = 1000,
    seed: int = 0,
) -> list[float]:
    """Resample the strategy's own trade outcomes in a random order.

    Order-independent statistics (total return, expectancy) are unchanged by
    shuffling, so this null speaks only to path-dependent claims -- drawdown,
    streaks, equity-curve smoothness. It answers "is the *sequencing* of these
    outcomes special", not "is the edge real".
    """
    if not trade_returns:
        return []
    rng = random.Random(seed)
    values = list(trade_returns)
    distribution: list[float] = []
    for _ in range(samples):
        shuffled = values[:]
        rng.shuffle(shuffled)
        equity = 1.0
        peak = 1.0
        max_drawdown = 0.0
        for value in shuffled:
            equity *= 1.0 + value
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)
        distribution.append(max_drawdown)
    return sorted(distribution)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def null_percentile(observed: float, distribution: Sequence[float]) -> float | None:
    """Share of the null distribution the observed result exceeds.

    0.95 means the strategy beat 95% of matched random attempts. Returns None
    for an empty distribution rather than a fabricated 0.5.
    """
    if not distribution:
        return None
    beaten = sum(1 for value in distribution if observed > value)
    return round(beaten / len(distribution), 6)


def bootstrap_confidence_interval(
    values: Sequence[float],
    *,
    samples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict[str, Any]:
    """Percentile bootstrap for the mean of per-trade outcomes."""
    if len(values) < 2:
        return {"mean": values[0] if values else None, "lower": None, "upper": None, "sample_size": len(values)}
    rng = random.Random(seed)
    size = len(values)
    means: list[float] = []
    for _ in range(samples):
        means.append(fmean(rng.choices(values, k=size)))
    means.sort()
    tail = (1.0 - confidence) / 2.0
    lower_index = max(0, int(math.floor(tail * len(means))) - 1)
    upper_index = min(len(means) - 1, int(math.ceil((1.0 - tail) * len(means))) - 1)
    return {
        "mean": round(fmean(values), 8),
        "lower": round(means[lower_index], 8),
        "upper": round(means[upper_index], 8),
        "confidence": confidence,
        "sample_size": size,
        "excludes_zero": means[lower_index] > 0 or means[upper_index] < 0,
    }


def sharpe_ratio(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    deviation = pstdev(returns)
    if deviation == 0:
        return None
    return fmean(returns) / deviation


def _skewness(values: Sequence[float]) -> float:
    n = len(values)
    deviation = pstdev(values)
    if n < 3 or deviation == 0:
        return 0.0
    mean = fmean(values)
    return sum(((value - mean) / deviation) ** 3 for value in values) / n


def _kurtosis(values: Sequence[float]) -> float:
    """Non-excess (Pearson) kurtosis; 3.0 for a normal distribution."""
    n = len(values)
    deviation = pstdev(values)
    if n < 4 or deviation == 0:
        return 3.0
    mean = fmean(values)
    return sum(((value - mean) / deviation) ** 4 for value in values) / n


def expected_maximum_sharpe(*, trials: int, sharpe_variance: float) -> float:
    """Expected best Sharpe from `trials` independent attempts at zero edge.

    This is the bar a search has to clear just to be unremarkable: try enough
    variants and one of them looks good for free.
    """
    if trials <= 1 or sharpe_variance <= 0:
        return 0.0
    deviation = math.sqrt(sharpe_variance)
    first = _normal_ppf(1.0 - 1.0 / trials)
    second = _normal_ppf(1.0 - 1.0 / (trials * math.e))
    return deviation * ((1.0 - _EULER_MASCHERONI) * first + _EULER_MASCHERONI * second)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    trials: int,
    sharpe_variance: float | None = None,
) -> dict[str, Any]:
    """Probability the observed Sharpe is real given how many variants were tried.

    Bailey & Lopez de Prado's deflated Sharpe: it discounts the observed
    Sharpe by the best one would expect from `trials` worthless attempts, and
    adjusts for the non-normality of the return distribution. A value below
    ~0.95 means the result is not distinguishable from the best of a lucky
    search.
    """
    observed = sharpe_ratio(returns)
    n = len(returns)
    if observed is None or n < 4:
        return {
            "deflated_sharpe": None,
            "observed_sharpe": observed,
            "trials": trials,
            "reason": "at least 4 return observations are required",
        }

    variance = sharpe_variance if sharpe_variance is not None else (1.0 + 0.5 * observed**2) / max(1, n - 1)
    benchmark = expected_maximum_sharpe(trials=trials, sharpe_variance=variance)
    skew = _skewness(returns)
    kurt = _kurtosis(returns)
    denominator = 1.0 - skew * observed + ((kurt - 1.0) / 4.0) * observed**2
    if denominator <= 0:
        return {
            "deflated_sharpe": None,
            "observed_sharpe": round(observed, 6),
            "expected_maximum_sharpe": round(benchmark, 6),
            "trials": trials,
            "reason": "return distribution too non-normal for the estimator to be defined",
        }
    statistic = (observed - benchmark) * math.sqrt(n - 1) / math.sqrt(denominator)
    return {
        "deflated_sharpe": round(_normal_cdf(statistic), 6),
        "observed_sharpe": round(observed, 6),
        "expected_maximum_sharpe": round(benchmark, 6),
        "skewness": round(skew, 6),
        "kurtosis": round(kurt, 6),
        "trials": trials,
        "sample_size": n,
    }


def probability_of_backtest_overfitting(performance_matrix: Sequence[Sequence[float]]) -> dict[str, Any]:
    """PBO via combinatorially symmetric cross-validation (Bailey et al.).

    `performance_matrix` is folds x variants: entry [f][v] is variant v's
    performance in fold f. The method splits the folds every possible way
    into in-sample and out-of-sample halves, picks the best variant in-sample,
    and asks how often that winner lands in the bottom half out-of-sample.
    A PBO near 0.5 means selecting on backtest performance carries no
    information at all.
    """
    from itertools import combinations

    folds = [list(row) for row in performance_matrix]
    if len(folds) < 2 or not folds[0]:
        return {"probability_of_backtest_overfitting": None, "reason": "at least 2 folds and 1 variant are required"}
    variant_count = len(folds[0])
    if any(len(row) != variant_count for row in folds):
        raise ValueError("every fold must score the same number of variants")
    if variant_count < 2:
        return {"probability_of_backtest_overfitting": None, "reason": "at least 2 variants are required"}

    fold_indices = list(range(len(folds)))
    half = len(folds) // 2
    if half == 0:
        return {"probability_of_backtest_overfitting": None, "reason": "at least 2 folds are required"}

    logits: list[float] = []
    underperformance = 0
    splits = 0
    for in_sample in combinations(fold_indices, half):
        out_sample = [index for index in fold_indices if index not in in_sample]
        in_scores = [sum(folds[f][v] for f in in_sample) for v in range(variant_count)]
        out_scores = [sum(folds[f][v] for f in out_sample) for v in range(variant_count)]
        best = max(range(variant_count), key=lambda v: in_scores[v])
        ranked = sorted(range(variant_count), key=lambda v: out_scores[v])
        rank = ranked.index(best) + 1
        relative = rank / (variant_count + 1)
        splits += 1
        if relative <= 0.5:
            underperformance += 1
        relative = min(max(relative, 1e-9), 1 - 1e-9)
        logits.append(math.log(relative / (1 - relative)))

    return {
        "probability_of_backtest_overfitting": round(underperformance / splits, 6) if splits else None,
        "splits_evaluated": splits,
        "median_out_of_sample_logit": round(sorted(logits)[len(logits) // 2], 6) if logits else None,
        "variants": variant_count,
        "folds": len(folds),
    }


def cost_break_even_multiplier(
    *,
    gross_profit_factor: float | None,
    total_costs: float,
    gross_profit: float,
    gross_loss: float,
) -> dict[str, Any]:
    """How many times current costs the edge could absorb before dying.

    Below 1.0 the strategy is already cost-negative; a value of 2.0 means it
    would survive costs doubling. Reported alongside gross PF so "no signal"
    and "signal destroyed by costs" stop looking identical.
    """
    net_profit = gross_profit - gross_loss - total_costs
    if total_costs <= 0:
        return {
            "cost_break_even_multiplier": None,
            "gross_profit_factor": gross_profit_factor,
            "net_edge": round(net_profit, 8),
            "reason": "no costs were charged",
        }
    gross_edge = gross_profit - gross_loss
    multiplier = gross_edge / total_costs if gross_edge > 0 else 0.0
    return {
        "cost_break_even_multiplier": round(multiplier, 6),
        "gross_profit_factor": gross_profit_factor,
        "gross_edge": round(gross_edge, 8),
        "total_costs": round(total_costs, 8),
        "net_edge": round(net_profit, 8),
        "survives_current_costs": multiplier > 1.0,
        "diagnosis": (
            "no_gross_edge" if gross_edge <= 0
            else "cost_destroyed_signal" if multiplier <= 1.0
            else "edge_survives_costs"
        ),
    }


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------

def benchmark_report(
    *,
    strategy_total_return: float,
    trade_returns: Sequence[float],
    closes: Sequence[float],
    holding_bars: int,
    direction: str = "long",
    trials: int = 1,
    round_trip_cost: float = 0.0,
    samples: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Everything a family needs to be judged against its alternatives."""
    hold = buy_and_hold_return(closes)
    trend = moving_average_trend_return(closes, cost_per_switch=round_trip_cost)
    random_null = random_entry_null_distribution(
        closes,
        trade_count=len(trade_returns),
        holding_bars=holding_bars,
        direction=direction,
        samples=samples,
        seed=seed,
        round_trip_cost=round_trip_cost,
    )
    percentile = null_percentile(strategy_total_return, random_null)
    baselines = {
        "buy_and_hold": round(hold, 8),
        "moving_average_trend": round(trend, 8),
        "random_entry_median": round(random_null[len(random_null) // 2], 8) if random_null else None,
    }
    best_baseline = max((value for value in baselines.values() if value is not None), default=0.0)
    return {
        "null_model_version": NULL_MODEL_VERSION,
        "strategy_total_return": round(strategy_total_return, 8),
        "baselines": baselines,
        "baseline_return": round(best_baseline, 8),
        "excess_return": round(strategy_total_return - best_baseline, 8),
        "null_percentile": percentile,
        "beats_random_at_95": bool(percentile is not None and percentile >= 0.95),
        "bootstrap_confidence_interval": bootstrap_confidence_interval(trade_returns, seed=seed),
        "deflated_sharpe": deflated_sharpe_ratio(trade_returns, trials=trials),
        "verdict": (
            "not_distinguishable_from_random"
            if percentile is None or percentile < 0.95
            else "beats_matched_random_null"
        ),
    }
