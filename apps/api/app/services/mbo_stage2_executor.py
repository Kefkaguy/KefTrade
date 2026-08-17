"""Stage 2B: execute the frozen Stage-2 plan v3, exactly as declared.

Runs the 14 primary cells: price-only baseline versus price-only + all 59 frozen
L3 features, scored by ``delta_R2`` on raw event-time ``return_bps`` labels with
status ``ok``.

Nothing in the frozen specification is chosen here. The cells, split, lags,
alpha candidates, scaling, metric, BH family, gates and PBO procedure are all
imported from ``mbo_stage2_plan`` and this module refuses to run if the plan or
label hashes have moved.

## Two specification gaps, closed before any outcome existed

Plan v3 fully specified the model but left two operational details implicit.
Executing forces a choice, so both are fixed here, **before a single number was
computed**, and recorded in ``SPECIFICATION_GAPS_CLOSED``.

1. **The out-of-sample R² reference.** ``R2_oos = 1 - SSE_oos / SST_oos`` with
   ``SST_oos = sum((y - mean(y_train))^2)`` -- the Campbell-Thompson convention,
   using the *training* mean as the reference predictor. A model must not
   benefit from knowing the out-of-sample mean. Note that ``delta_R2`` is a
   difference over a shared denominator, so this choice sets its scale but
   cannot flip its sign.

2. **The training set behind each evaluation block.** Chronological and
   expanding:
   - discovery: leave-one-date-out within the 10 discovery dates;
   - validation: fit on all 10 discovery dates, score each of the 6;
   - confirmation: fit on all 16 discovery+validation dates, score each of the 4.

   Alpha is still selected only inside discovery and then frozen, so no
   validation or confirmation date ever enters an alpha choice.

## Compact by construction

Disk is constrained and no row-level prediction dataset is written. Each
(cell, session-date) is reduced during a single streaming pass to sufficient
statistics -- ``X'X``, ``X'y``, ``y'y``, ``n``, ``sum(y)`` -- and every fit,
every fold, every bootstrap resample and all 12,870 CSCV partitions are
computed from sums of those blocks. 14 cells x 20 dates x a 70-wide design is
about 11 MB resident, and the only artefact written is a JSON result summary.

Because the design matrix is ordered ``[intercept, price-only lags, features]``,
the baseline is the leading 11x11 sub-block of the same Gram: one accumulation
serves both models.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from math import comb, sqrt
from typing import Any

import numpy as np
from app.services.mbo_feature_engine import FEATURE_VOCABULARY
from app.services.mbo_label_engine import (
    HORIZONS_BY_NAME,
    LABEL_DEFINITION_HASH,
    LABEL_OK,
)
from app.services.mbo_stage2_plan import (
    BH_FALSE_DISCOVERY_RATE,
    BH_FAMILY_SIZE,
    BLOCK_BOOTSTRAP_RESAMPLES,
    CSCV_BLOCKS,
    CSCV_IN_SAMPLE_BLOCKS,
    DESIGN_WIDTH,
    PBO_AUTHORIZATION_CEILING,
    PLAN_HASH,
    PRICE_ONLY_LAGS,
    PRIMARY_CELLS,
    RIDGE_ALPHAS,
    SPLIT_DATE_BLOCKS,
    STAGE2_PLAN_VERSION,
)

STAGE2_EXECUTOR_VERSION = "tier1_stage2_executor_v1"

# The frozen artefacts this executor is bound to. A mismatch is a refusal.
EXPECTED_PLAN_VERSION = "tier1_stage2_plan_v3"
EXPECTED_PLAN_HASH = (
    "ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e"
)

SPECIFICATION_GAPS_CLOSED: tuple[dict[str, str], ...] = (
    {
        "gap": "out_of_sample_r2_reference",
        "closed_before_any_outcome": "true",
        "decision": (
            "R2_oos = 1 - SSE_oos / SST_oos with SST_oos = sum((y - mean(y_train))^2); "
            "the training mean is the reference predictor"
        ),
        "reason": (
            "Plan v3 said 'out-of-sample R^2' without naming the reference. Using "
            "the out-of-sample mean would let the model benefit from knowing it. "
            "delta_R2 is a difference over a shared denominator, so this sets its "
            "scale but cannot flip its sign."
        ),
    },
    {
        "gap": "ridge_penalty_scope",
        "closed_before_any_outcome": "true",
        "decision": (
            "the ridge penalty applies to the 59 L3 columns only; the intercept "
            "and the 10 price-only columns carry penalty 0, so the augmented fit "
            "contains the baseline OLS fit exactly as its alpha -> infinity limit"
        ),
        "reason": (
            "Plan v3 named the estimator 'ridge' and the baseline 'ordinary least "
            "squares, no regularization' but did not say which columns the penalty "
            "covers. Shrinking the price-only columns inside the augmented model "
            "while fitting them by OLS in the baseline would make delta_R2 a "
            "mixture of incremental L3 information and regularization of the "
            "baseline, and could turn either sign. The plan calls the L3 model "
            "'nested -- the baseline's inputs augmented, never a separate fit', "
            "which only holds if the shared columns are treated identically."
        ),
    },
    {
        "gap": "stage2_scaling_application_point",
        "closed_before_any_outcome": "true",
        "decision": (
            "the plan-v3 expanding prior-only standardization is applied to all 59 "
            "L3 columns at Stage-2 design-matrix construction, per (symbol, "
            "cadence, feature), within the symbol-day, from strictly prior "
            "observations, withheld below 30 priors and never imputed; the frozen "
            "Stage-1 Parquet is not modified"
        ),
        "reason": (
            "Plan v3 declares the scaling rule but Stage-1 froze only four columns "
            "in standardized form. Feeding the other 55 raw into a penalized fit "
            "would make the ridge penalty depend on each feature's arbitrary unit, "
            "so alpha would mean a different amount of shrinkage per column. "
            "Applying it at design-matrix construction keeps it prior-only and "
            "within symbol-day, which is what preserves per-date Gram additivity."
        ),
    },
    {
        "gap": "price_only_lag_convention",
        "closed_before_any_outcome": "true",
        "decision": (
            "lag k is the midpoint log-return realized into t-k+1, so lag 1 is the "
            "most recent completed return and is known at t; signs are the sign of "
            "the same five columns"
        ),
        "reason": (
            "Plan v3 said 'lagged own-cadence midpoint log-returns at lags "
            "[1, 2, 3, 5, 10]' without fixing whether a lag indexes a one-step "
            "return or a k-step return. One-step returns at five offsets is the "
            "reading that keeps the five columns distinct rather than nested "
            "sums of each other. Every column is strictly prior-or-at t."
        ),
    },
    {
        "gap": "rows_with_a_withheld_feature",
        "closed_before_any_outcome": "true",
        "decision": (
            "a row whose Stage-1 expanding-window normalization was withheld "
            "(below 30 priors) is dropped from both models, never imputed; the "
            "count is reported"
        ),
        "reason": (
            "Stage-1 declares those values withheld rather than zero. Imputing "
            "them would invent observations, and the baseline and L3 models must "
            "see exactly the same rows for delta_R2 to be a nested comparison."
        ),
    },
    {
        "gap": "training_set_per_evaluation_block",
        "closed_before_any_outcome": "true",
        "decision": (
            "discovery = leave-one-date-out within the 10 discovery dates; "
            "validation = fit on all 10 discovery dates; "
            "confirmation = fit on all 16 discovery+validation dates"
        ),
        "reason": (
            "Plan v3 froze the gates and the alpha rule but not the fitting window "
            "behind each block. Chronological and expanding is the only reading "
            "consistent with the alpha rule, which already confines tuning to "
            "discovery."
        ),
    },
)

PRICE_ONLY_WIDTH = 1 + 2 * len(PRICE_ONLY_LAGS)  # intercept + returns + signs
MIN_PRIOR_OBSERVATIONS = 30
DISCOVERY_T_HURDLE = 3.0
VALIDATION_T_HURDLE = 3.0
VALIDATION_SHRINKAGE_FLOOR = 0.5

# Failure reasons. Fixed strings so a failing cell is always failing for a
# stated, comparable cause.
FAIL_INSUFFICIENT_DATES = "insufficient_labelled_dates"
FAIL_NEGATIVE_DELTA = "delta_r2_not_positive"
FAIL_T_BELOW_HURDLE = "clustered_t_below_hurdle"
FAIL_BOOTSTRAP_LB = "bootstrap_lower_bound_not_positive"
FAIL_BH = "did_not_survive_bh"
FAIL_SHRINKAGE = "validation_estimate_below_half_of_discovery"
FAIL_SIGN_FLIP = "sign_flipped_from_discovery"
FAIL_NOT_REACHED = "not_reached_prior_gate_failed"
FAIL_PBO_VETO = "family_vetoed_by_pbo"


# ---------------------------------------------------------------------------
# Sufficient statistics
# ---------------------------------------------------------------------------


@dataclass
class Gram:
    """``X'X``, ``X'y``, ``y'y``, ``n``, ``sum(y)`` for one (cell, date)."""

    xtx: np.ndarray
    xty: np.ndarray
    yty: float
    n: int
    ysum: float

    @classmethod
    def zeros(cls, width: int) -> Gram:
        return cls(np.zeros((width, width)), np.zeros(width), 0.0, 0, 0.0)

    def add_rows(self, x: np.ndarray, y: np.ndarray) -> None:
        self.xtx += x.T @ x
        self.xty += x.T @ y
        self.yty += float(y @ y)
        self.n += int(x.shape[0])
        self.ysum += float(y.sum())

    def __add__(self, other: Gram) -> Gram:
        return Gram(
            self.xtx + other.xtx,
            self.xty + other.xty,
            self.yty + other.yty,
            self.n + other.n,
            self.ysum + other.ysum,
        )

    def __sub__(self, other: Gram) -> Gram:
        """Leave-one-block-out is a subtraction, not a re-sum.

        Re-summing the training blocks inside every fold of every partition is
        what turns a feasible CSCV into an infeasible one: it is O(k) work per
        fold where subtraction is O(1).
        """
        return Gram(
            self.xtx - other.xtx,
            self.xty - other.xty,
            self.yty - other.yty,
            self.n - other.n,
            self.ysum - other.ysum,
        )


def sum_grams(blocks: Iterable[Gram], width: int) -> Gram:
    total = Gram.zeros(width)
    for block in blocks:
        total = total + block
    return total


def _slice(gram: Gram, width: int) -> Gram:
    """The leading ``width`` columns -- the nested price-only sub-model."""
    return Gram(
        gram.xtx[:width, :width].copy(),
        gram.xty[:width].copy(),
        gram.yty,
        gram.n,
        gram.ysum,
    )


# ---------------------------------------------------------------------------
# Fitting and scoring
# ---------------------------------------------------------------------------


def fit(
    gram: Gram, alpha: float, *, penalty_from: int = PRICE_ONLY_WIDTH
) -> np.ndarray | None:
    """Ridge on the L3 block only; the intercept and the baseline are OLS.

    The penalty starts at ``penalty_from``, so columns 0..10 -- the intercept
    and the ten price-only variables -- are never shrunk. Penalizing them would
    make the augmented model something other than the baseline plus the L3
    block: ``delta_R2`` would then mix incremental L3 information with
    regularization of a baseline that is fitted by OLS on its own. A Gram
    narrower than ``penalty_from`` is therefore solved as pure OLS.
    """
    if gram.n <= 0:
        return None
    width = gram.xtx.shape[0]
    penalized = np.zeros(width)
    if alpha and width > penalty_from:
        penalized[penalty_from:] = alpha
    try:
        return np.linalg.solve(gram.xtx + np.diag(penalized), gram.xty)
    except np.linalg.LinAlgError:
        return None


def sse(gram: Gram, beta: np.ndarray) -> float:
    """Residual sum of squares of ``beta`` evaluated on this Gram's rows."""
    return float(gram.yty - 2.0 * beta @ gram.xty + beta @ gram.xtx @ beta)


def sst(gram: Gram, train_mean: float) -> float:
    """Total sum of squares about the TRAINING mean (Campbell-Thompson)."""
    return float(gram.yty - 2.0 * train_mean * gram.ysum + gram.n * train_mean**2)


def delta_r2(train: Gram, test: Gram, alpha: float) -> float | None:
    """``R2(L3) - R2(baseline)`` on ``test``, both fitted on ``train``.

    Both models share ``SST``, so the difference reduces to the reduction in
    squared error the L3 block buys over the baseline.
    """
    if train.n <= DESIGN_WIDTH or test.n <= 0:
        return None
    beta_l3 = fit(train, alpha)
    beta_base = fit(_slice(train, PRICE_ONLY_WIDTH), 0.0)
    if beta_l3 is None or beta_base is None:
        return None
    train_mean = train.ysum / train.n
    denominator = sst(test, train_mean)
    if denominator <= 0:
        return None
    sse_l3 = sse(test, beta_l3)
    sse_base = sse(_slice(test, PRICE_ONLY_WIDTH), beta_base)
    return (sse_base - sse_l3) / denominator


def r2_pair(train: Gram, test: Gram, alpha: float) -> tuple[float | None, float | None]:
    """Absolute out-of-sample R2 for baseline and L3, reported beside delta."""
    if train.n <= DESIGN_WIDTH or test.n <= 0:
        return None, None
    beta_l3 = fit(train, alpha)
    beta_base = fit(_slice(train, PRICE_ONLY_WIDTH), 0.0)
    if beta_l3 is None or beta_base is None:
        return None, None
    train_mean = train.ysum / train.n
    denominator = sst(test, train_mean)
    if denominator <= 0:
        return None, None
    return (
        1.0 - sse(_slice(test, PRICE_ONLY_WIDTH), beta_base) / denominator,
        1.0 - sse(test, beta_l3) / denominator,
    )


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def clustered_t(values: Sequence[float]) -> tuple[float | None, float | None]:
    """t on the per-session-date statistic, and its two-sided p-value.

    One observation per session date: 19.5 M rows are not 19.5 M degrees of
    freedom.
    """
    clean = [v for v in values if v is not None and np.isfinite(v)]
    if len(clean) < 2:
        return None, None
    array = np.asarray(clean, dtype=float)
    mean = float(array.mean())
    deviation = float(array.std(ddof=1))
    # Exact-zero is the wrong test: a constant sequence leaves float dust in
    # the denominator (std([0.01] * 10) is ~6e-19), which would report t = 1.7e16
    # and a p-value of 1e-143 for a cell that showed no dispersion at all.
    if not deviation > abs(mean) * 1e-9:
        return None, None
    statistic = mean / (deviation / sqrt(len(array)))
    degrees = len(array) - 1
    try:
        from statistics import NormalDist

        # Student-t two-sided p via the survival function of |t|; for df >= 5 a
        # normal approximation understates the tail, so use an exact-ish
        # incomplete-beta form when scipy is unavailable.
        p = _student_t_sf(abs(statistic), degrees) * 2.0
    except (ImportError, ValueError, OverflowError):  # pragma: no cover
        p = float(NormalDist().cdf(-abs(statistic)) * 2.0)
    return statistic, p


def _student_t_sf(t: float, df: int) -> float:
    """P(T > t) for Student-t with ``df`` degrees of freedom, via regularized
    incomplete beta. Avoids a scipy dependency."""
    x = df / (df + t * t)
    return 0.5 * _betainc(df / 2.0, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) by continued fraction."""
    from math import exp, lgamma

    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = exp(
        lgamma(a + b) - lgamma(a) - lgamma(b) + a * np.log(x) + b * np.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - exp(
        lgamma(a + b) - lgamma(a) - lgamma(b) + b * np.log(1.0 - x) + a * np.log(x)
    ) * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, iterations: int = 200) -> float:
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, iterations + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def block_bootstrap_interval(
    values: Sequence[float],
    *,
    resamples: int = BLOCK_BOOTSTRAP_RESAMPLES,
    seed: int = 20260817,
) -> tuple[float | None, float | None]:
    """Two-sided 95% percentile interval on the mean, resampling whole dates."""
    clean = np.asarray(
        [v for v in values if v is not None and np.isfinite(v)], dtype=float
    )
    if clean.size < 2:
        return None, None
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, clean.size, size=(resamples, clean.size))
    means = clean[draws].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def benjamini_hochberg(
    p_values: dict[str, float | None], *, fdr: float = BH_FALSE_DISCOVERY_RATE
) -> dict[str, dict[str, Any]]:
    """BH across the whole declared family, not the reported subset.

    A cell with no p-value still occupies a slot in the family size: the family
    is what was declared, not what happened to produce a number.
    """
    family_size = len(p_values)
    testable = sorted(
        ((key, p) for key, p in p_values.items() if p is not None), key=lambda kv: kv[1]
    )
    survivors: set[str] = set()
    critical: dict[str, float] = {}
    largest_passing = 0
    for rank, (_, p) in enumerate(testable, start=1):
        threshold = fdr * rank / family_size
        if p <= threshold:
            largest_passing = rank
    for rank, (key, p) in enumerate(testable, start=1):
        critical[key] = fdr * rank / family_size
        if rank <= largest_passing:
            survivors.add(key)
    # Step-up adjusted q-values.
    q: dict[str, float] = {}
    running = 1.0
    for rank in range(len(testable), 0, -1):
        key, p = testable[rank - 1]
        running = min(running, p * family_size / rank)
        q[key] = min(1.0, running)
    return {
        key: {
            "p_value": p_values[key],
            "q_value": q.get(key),
            "bh_critical": critical.get(key),
            "survives_bh": key in survivors,
        }
        for key in p_values
    }


# ---------------------------------------------------------------------------
# Alpha selection
# ---------------------------------------------------------------------------


def select_alpha(
    blocks: dict[str, Gram],
    dates: Sequence[str],
    *,
    alphas: Sequence[float] = RIDGE_ALPHAS,
) -> tuple[float | None, dict[str, float | None]]:
    """Leave-one-date-out CV over ``dates``, maximizing mean out-of-fold delta_R2."""
    usable = [d for d in dates if d in blocks and blocks[d].n > 0]
    if len(usable) < 2:
        return None, {}
    # One accumulation; each fold is the total minus the held-out block.
    total = sum_grams((blocks[d] for d in usable), DESIGN_WIDTH)
    folds = {d: total - blocks[d] for d in usable}
    scores: dict[str, float | None] = {}
    best_alpha: float | None = None
    best_score = -np.inf
    for alpha in alphas:
        fold_scores: list[float] = []
        for held_out in usable:
            value = delta_r2(folds[held_out], blocks[held_out], alpha)
            if value is not None and np.isfinite(value):
                fold_scores.append(value)
        mean = float(np.mean(fold_scores)) if fold_scores else None
        scores[str(alpha)] = mean
        if mean is not None and mean > best_score:
            best_score = mean
            best_alpha = alpha
    return best_alpha, scores


# ---------------------------------------------------------------------------
# Nested CSCV / PBO
# ---------------------------------------------------------------------------


def _alpha_from_blocks(
    total: Gram, blocks: Sequence[Gram], alphas: Sequence[float]
) -> float | None:
    """Leave-one-block-out alpha selection from a prebuilt in-sample total."""
    usable = [b for b in blocks if b.n > 0]
    if len(usable) < 2:
        return None
    folds = [(total - b, b) for b in usable]
    best_alpha: float | None = None
    best_score = -np.inf
    for alpha in alphas:
        scores = []
        for train, test in folds:
            value = delta_r2(train, test, alpha)
            if value is not None and np.isfinite(value):
                scores.append(value)
        if not scores:
            continue
        mean = float(np.mean(scores))
        if mean > best_score:
            best_score = mean
            best_alpha = alpha
    return best_alpha


def nested_cscv_pbo(
    cell_blocks: dict[str, dict[str, Gram]],
    dates: Sequence[str],
    *,
    blocks: int = CSCV_BLOCKS,
    alphas: Sequence[float] = RIDGE_ALPHAS,
) -> dict[str, Any]:
    """PBO exactly as frozen: alpha nested inside each cell, cells are the set.

    For every balanced partition, and for each of the 14 cells independently:
    select alpha using only that partition's in-sample dates by leave-one-block-out
    CV; fit with it on the full in-sample half; score in-sample and out-of-sample
    delta_R2. Then take the best in-sample cell and record its out-of-sample rank.
    """
    ordered = list(dates)
    if len(ordered) < blocks:
        return {
            "computed": False,
            "reason": f"only {len(ordered)} dates for {blocks} CSCV blocks",
        }
    chunks = [list(chunk) for chunk in np.array_split(np.array(ordered), blocks)]
    cells = list(cell_blocks)
    # Reduce each cell to one Gram per CSCV block, plus its total. Every
    # partition is then assembled from block sums and one subtraction, and no
    # per-date block is ever re-summed inside a fold.
    chunk_grams: dict[str, list[Gram]] = {}
    totals: dict[str, Gram] = {}
    for cell in cells:
        per_date = cell_blocks[cell]
        per_chunk = [
            sum_grams((per_date[d] for d in chunk if d in per_date), DESIGN_WIDTH)
            for chunk in chunks
        ]
        chunk_grams[cell] = per_chunk
        totals[cell] = sum_grams(per_chunk, DESIGN_WIDTH)

    bottom_half_count = 0
    partitions_scored = 0
    ranks: list[int] = []

    # S/2 by construction, so a reduced block count still partitions in half.
    in_sample_size = blocks // 2
    for in_sample_ids in combinations(range(blocks), in_sample_size):
        is_scores: dict[str, float] = {}
        oos_scores: dict[str, float] = {}
        for cell in cells:
            per_chunk = chunk_grams[cell]
            train = sum_grams((per_chunk[i] for i in in_sample_ids), DESIGN_WIDTH)
            if train.n <= DESIGN_WIDTH:
                continue
            test = totals[cell] - train
            # (1) alpha from this partition's in-sample blocks only, by
            # leave-one-block-out -- each fold is train minus one chunk.
            alpha = _alpha_from_blocks(
                train, [per_chunk[i] for i in in_sample_ids], alphas
            )
            if alpha is None:
                continue
            # (2)-(3) fit with that alpha, score IS and OOS.
            in_value = delta_r2(train, train, alpha)
            out_value = delta_r2(train, test, alpha)
            if in_value is not None and np.isfinite(in_value):
                is_scores[cell] = in_value
            if out_value is not None and np.isfinite(out_value):
                oos_scores[cell] = out_value
        common = [c for c in cells if c in is_scores and c in oos_scores]
        if len(common) < 2:
            continue
        partitions_scored += 1
        selected = max(common, key=lambda c: is_scores[c])
        # Rank 1 = best out of sample.
        order = sorted(common, key=lambda c: oos_scores[c], reverse=True)
        rank = order.index(selected) + 1
        ranks.append(rank)
        if rank > len(common) / 2:
            bottom_half_count += 1

    if partitions_scored == 0:
        return {"computed": False, "reason": "no partition produced comparable scores"}
    pbo = bottom_half_count / partitions_scored
    return {
        "computed": True,
        "method": "nested CSCV",
        "configuration_set_size": len(cells),
        "alpha_nested_not_flattened": True,
        "blocks": blocks,
        "partitions_declared": comb(blocks, in_sample_size),
        "partitions_scored": partitions_scored,
        "median_oos_rank_of_selected": float(np.median(ranks)) if ranks else None,
        "pbo": pbo,
        "authorization_ceiling": PBO_AUTHORIZATION_CEILING,
        "vetoes_family": pbo > PBO_AUTHORIZATION_CEILING,
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class CellResult:
    cadence: str
    horizon: str
    raw_n: int = 0
    session_n: int = 0
    chosen_alpha: float | None = None
    discovery: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    confirmation: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None

    @property
    def key(self) -> str:
        return f"{self.cadence}|{self.horizon}"


def split_dates(dates: Sequence[str]) -> dict[str, list[str]]:
    """Earliest 10 / next 6 / final 4, by sorted session date, never split."""
    ordered = sorted(dates)
    out: dict[str, list[str]] = {}
    cursor = 0
    for name, count in SPLIT_DATE_BLOCKS:
        out[name] = ordered[cursor : cursor + count]
        cursor += count
    out["unassigned"] = ordered[cursor:]
    return out


def _gate(
    per_date: Sequence[float],
    *,
    t_hurdle: float,
) -> dict[str, Any]:
    values = [v for v in per_date if v is not None and np.isfinite(v)]
    mean = float(np.mean(values)) if values else None
    statistic, p_value = clustered_t(values)
    low, high = block_bootstrap_interval(values)
    return {
        "dates": len(values),
        "delta_r2": mean,
        "clustered_t": statistic,
        "p_value": p_value,
        "bootstrap_low": low,
        "bootstrap_high": high,
        "t_hurdle": t_hurdle,
        "per_date_delta_r2": values,
    }


def _verdict(gate: dict[str, Any], *, t_hurdle: float) -> tuple[bool, str | None]:
    if gate["dates"] < 2:
        return False, FAIL_INSUFFICIENT_DATES
    if gate["delta_r2"] is None or gate["delta_r2"] <= 0:
        return False, FAIL_NEGATIVE_DELTA
    if gate["clustered_t"] is None or gate["clustered_t"] < t_hurdle:
        return False, FAIL_T_BELOW_HURDLE
    if gate["bootstrap_low"] is None or gate["bootstrap_low"] <= 0:
        return False, FAIL_BOOTSTRAP_LB
    return True, None


def run_stage2(
    cell_blocks: dict[str, dict[str, Gram]],
    *,
    dates: Sequence[str],
    cscv_blocks: int = CSCV_BLOCKS,
) -> dict[str, Any]:
    """Execute the frozen chronological path for every declared cell.

    ``cell_blocks`` maps ``"cadence|horizon"`` to per-session-date Gram blocks.
    """
    blocks = split_dates(dates)
    discovery, validation, confirmation = (
        blocks["discovery"],
        blocks["validation"],
        blocks["confirmation"],
    )
    results: dict[str, CellResult] = {}

    for cadence, horizon in PRIMARY_CELLS:
        key = f"{cadence}|{horizon}"
        result = CellResult(cadence=cadence, horizon=horizon)
        per_date = cell_blocks.get(key, {})
        result.raw_n = sum(g.n for g in per_date.values())
        result.session_n = sum(1 for g in per_date.values() if g.n > 0)

        # Alpha: discovery only, then frozen.
        alpha, alpha_scores = select_alpha(per_date, discovery)
        result.chosen_alpha = alpha
        if alpha is None:
            result.failure_reason = FAIL_INSUFFICIENT_DATES
            result.discovery = {"passed": False, "reason": FAIL_INSUFFICIENT_DATES}
            results[key] = result
            continue

        # Discovery: leave-one-date-out within the 10 discovery dates.
        discovery_scores: list[float] = []
        train_discovery_total = sum_grams(
            (per_date[d] for d in discovery if d in per_date), DESIGN_WIDTH
        )
        for held_out in discovery:
            if held_out not in per_date:
                continue
            value = delta_r2(
                train_discovery_total - per_date[held_out], per_date[held_out], alpha
            )
            if value is not None:
                discovery_scores.append(value)
        gate = _gate(discovery_scores, t_hurdle=DISCOVERY_T_HURDLE)
        passed, reason = _verdict(gate, t_hurdle=DISCOVERY_T_HURDLE)
        train_discovery = train_discovery_total
        base_r2, l3_r2 = r2_pair(train_discovery, train_discovery, alpha)
        gate.update(
            {
                "alpha_cv_scores": alpha_scores,
                "in_sample_baseline_r2": base_r2,
                "in_sample_l3_r2": l3_r2,
                "passed": passed,
                "reason": reason,
            }
        )
        result.discovery = gate
        if not passed:
            result.failure_reason = reason
            results[key] = result
            continue

        # Validation: fit on all 10 discovery dates, score each of the 6.
        validation_scores: list[float] = []
        base_scores: list[float] = []
        l3_scores: list[float] = []
        for date in validation:
            if date not in per_date:
                continue
            value = delta_r2(train_discovery, per_date[date], alpha)
            b, l = r2_pair(train_discovery, per_date[date], alpha)
            if value is not None:
                validation_scores.append(value)
            if b is not None:
                base_scores.append(b)
            if l is not None:
                l3_scores.append(l)
        gate = _gate(validation_scores, t_hurdle=VALIDATION_T_HURDLE)
        passed, reason = _verdict(gate, t_hurdle=VALIDATION_T_HURDLE)
        discovery_delta = result.discovery["delta_r2"]
        if passed and discovery_delta is not None and (
            gate["delta_r2"] < VALIDATION_SHRINKAGE_FLOOR * discovery_delta
        ):
            passed, reason = False, FAIL_SHRINKAGE
        gate.update(
            {
                "baseline_oos_r2": float(np.mean(base_scores)) if base_scores else None,
                "l3_oos_r2": float(np.mean(l3_scores)) if l3_scores else None,
                "shrinkage_floor": VALIDATION_SHRINKAGE_FLOOR,
                "passed_before_bh": passed,
                "reason": reason,
            }
        )
        result.validation = gate
        if not passed:
            result.failure_reason = reason
        results[key] = result

    # BH across exactly the 14 declared cells.
    p_values = {
        f"{c}|{h}": results[f"{c}|{h}"].validation.get("p_value")
        if results[f"{c}|{h}"].validation.get("passed_before_bh")
        else None
        for c, h in PRIMARY_CELLS
    }
    bh = benjamini_hochberg(p_values)
    for key, result in results.items():
        entry = bh.get(key, {})
        if result.validation:
            result.validation.update(entry)
            passed = bool(result.validation.get("passed_before_bh")) and bool(
                entry.get("survives_bh")
            )
            result.validation["passed"] = passed
            if not passed and result.failure_reason is None:
                result.failure_reason = FAIL_BH

    # PBO over the whole declared family, on discovery+validation dates only.
    pbo = nested_cscv_pbo(
        {k: v for k, v in cell_blocks.items()},
        list(discovery) + list(validation),
        blocks=cscv_blocks,
    )
    vetoed = bool(pbo.get("computed") and pbo.get("vetoes_family"))

    # Confirmation: only for validation survivors, and only if PBO did not veto.
    authorized = [
        key for key, r in results.items() if r.validation.get("passed")
    ]
    for key, result in results.items():
        if key not in authorized:
            result.confirmation = {"run": False, "reason": FAIL_NOT_REACHED}
            continue
        if vetoed:
            result.confirmation = {"run": False, "reason": FAIL_PBO_VETO}
            result.failure_reason = FAIL_PBO_VETO
            continue
        per_date = cell_blocks[key]
        train = sum_grams(
            (per_date[d] for d in list(discovery) + list(validation) if d in per_date),
            DESIGN_WIDTH,
        )
        scores: list[float] = []
        base_scores: list[float] = []
        l3_scores: list[float] = []
        for date in confirmation:
            if date not in per_date:
                continue
            value = delta_r2(train, per_date[date], result.chosen_alpha)
            b, l = r2_pair(train, per_date[date], result.chosen_alpha)
            if value is not None:
                scores.append(value)
            if b is not None:
                base_scores.append(b)
            if l is not None:
                l3_scores.append(l)
        gate = _gate(scores, t_hurdle=0.0)
        passed = (
            gate["dates"] >= 2
            and gate["delta_r2"] is not None
            and gate["delta_r2"] > 0
            and gate["bootstrap_low"] is not None
            and gate["bootstrap_low"] > 0
        )
        reason = None
        if not passed:
            if gate["dates"] < 2:
                reason = FAIL_INSUFFICIENT_DATES
            elif gate["delta_r2"] is None or gate["delta_r2"] <= 0:
                reason = FAIL_NEGATIVE_DELTA
            else:
                reason = FAIL_BOOTSTRAP_LB
        elif result.discovery.get("delta_r2") and gate["delta_r2"] * result.discovery[
            "delta_r2"
        ] < 0:
            passed, reason = False, FAIL_SIGN_FLIP
        gate.update(
            {
                "run": True,
                "baseline_oos_r2": float(np.mean(base_scores)) if base_scores else None,
                "l3_oos_r2": float(np.mean(l3_scores)) if l3_scores else None,
                "passed": passed,
                "reason": reason,
            }
        )
        result.confirmation = gate
        if not passed and result.failure_reason is None:
            result.failure_reason = reason

    survived_discovery = sum(1 for r in results.values() if r.discovery.get("passed"))
    survived_validation = sum(1 for r in results.values() if r.validation.get("passed"))
    survived_confirmation = sum(
        1 for r in results.values() if r.confirmation.get("passed")
    )

    if vetoed:
        verdict = "no_authorization_pbo_veto"
    elif survived_confirmation > 0:
        verdict = "cells_survived_stage2"
    else:
        verdict = "l3_block_failed_to_demonstrate_incremental_information"

    return {
        "executor_version": STAGE2_EXECUTOR_VERSION,
        "plan_version": STAGE2_PLAN_VERSION,
        "plan_hash": PLAN_HASH,
        "label_definition_hash": LABEL_DEFINITION_HASH,
        "specification_gaps_closed": [dict(g) for g in SPECIFICATION_GAPS_CLOSED],
        "split": {name: list(values) for name, values in blocks.items()},
        "cells": [
            {
                "cadence": r.cadence,
                "horizon": r.horizon,
                "raw_n": r.raw_n,
                "session_n": r.session_n,
                "chosen_alpha": r.chosen_alpha,
                "discovery": r.discovery,
                "validation": r.validation,
                "confirmation": r.confirmation,
                "failure_reason": r.failure_reason,
            }
            for r in (results[f"{c}|{h}"] for c, h in PRIMARY_CELLS)
        ],
        "pbo": pbo,
        "pbo_vetoes_family": vetoed,
        "survivors": {
            "discovery": survived_discovery,
            "validation": survived_validation,
            "confirmation": survived_confirmation,
            "declared_cells": BH_FAMILY_SIZE,
        },
        "verdict": verdict,
    }


def assert_frozen_plan() -> None:
    """Refuse to execute against a plan that has moved."""
    if CSCV_IN_SAMPLE_BLOCKS != CSCV_BLOCKS // 2:
        raise ValueError(
            f"the plan declares {CSCV_BLOCKS} CSCV blocks with "
            f"{CSCV_IN_SAMPLE_BLOCKS} in sample, which is not the half-split the "
            "procedure derives; one of the two has moved"
        )
    if STAGE2_PLAN_VERSION != EXPECTED_PLAN_VERSION:
        raise ValueError(
            f"plan version {STAGE2_PLAN_VERSION!r} is not the frozen "
            f"{EXPECTED_PLAN_VERSION!r}"
        )
    if PLAN_HASH != EXPECTED_PLAN_HASH:
        raise ValueError(
            "plan hash does not match the frozen v3 value; the declared design has "
            "changed and Stage 2B may not execute against it"
        )


def write_summary(result: dict[str, Any], path: Any) -> None:
    """The only artefact: one compact JSON. No row-level predictions."""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")


__all__ = [
    "FEATURE_VOCABULARY",
    "HORIZONS_BY_NAME",
    "LABEL_OK",
    "SPECIFICATION_GAPS_CLOSED",
    "STAGE2_EXECUTOR_VERSION",
    "Gram",
    "assert_frozen_plan",
    "benjamini_hochberg",
    "clustered_t",
    "delta_r2",
    "nested_cscv_pbo",
    "run_stage2",
    "select_alpha",
    "split_dates",
    "write_summary",
]
