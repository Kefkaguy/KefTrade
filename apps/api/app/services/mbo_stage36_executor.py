"""Stage 3.6: news-triggered L3 consensus, measured against the certified book.

Nothing about market reconstruction or filling is reimplemented here. The book
is Stage-3's ``BookReplay`` over ``MboBook``; the fill is Stage-3's
``walk_book`` at 100 shares through at most 10 displayed levels; the raw source
is bound by Stage-3's ``resolve_raw_source``; the coverage bound is Stage-3.5's
``CoverageTracker``; the per-date models are Stage-3.5's own reconstruction. A
second implementation of any of those would be a second definition, and the two
would eventually disagree in whichever direction happened to flatter the result.

## The four instants

    t0            = known_at                      the news becomes knowable
    td            = t0 + 30s                      the decision instant
    entry_arrival = td + 250ms                    the entry order reaches the book
    exit_arrival  = td + 5min + 250ms             the exit order reaches the book

Arrival-to-arrival is therefore exactly five minutes: both legs carry the same
latency, so it cancels out of the holding period rather than shortening it.

## What decides direction, and what does not

The four frozen predictors decide direction, by sign agreement. The 30-second
price move decides **nothing** -- it is carried as a diagnostic so that
continuation and reversal can be reported afterwards, and it is structurally
incapable of selecting a trade because no function here reads it for that
purpose.

Only ``4_of_4`` and ``3_of_4`` trade. A ``2_vs_2`` split, a zero prediction, or
any unavailable model means no trade: the design requires all four models to
speak before any of them is acted on.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.services.mbo_stage3_executor import (
    BPS,
    clustered_t,
    walk_book,
)
from app.services.mbo_stage3_plan import FEE_SCHEDULES
from app.services.mbo_stage36_plan import (
    CONSENSUS_2_VS_2,
    CONSENSUS_3_OF_4,
    CONSENSUS_4_OF_4,
    CONSENSUS_INCOMPLETE,
    CSV_FILENAMES,
    DECISION_DELAY_NS,
    EXPECTED_COUNTS,
    EXPECTED_CSV_SHA256,
    EXPECTED_MANIFEST_SHA256,
    FROZEN_CELLS,
    HOLDING_NS,
    LATENCY_NS,
    LONG,
    MANIFEST_FILENAME,
    MIN_EXECUTABLE_TRADES,
    MIN_SESSIONS,
    PLAN_DESIGN_HASH,
    PREOUTCOME_RELATIVE_DIR,
    PRIMARY_FEE_SCHEDULE,
    PRIMARY_TARGET_BPS,
    SHORT,
    STAGE36_PLAN_VERSION,
    STRETCH_TARGET_BPS,
    STRONG_CONSENSUS,
    T_HURDLE,
    TRADE_SIZE_SHARES,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_MECHANISM,
    VERDICT_SUPPORTED,
    assert_frozen_design,
    sha256_of,
)

STAGE36_EXECUTOR_VERSION = "tier1_stage36_executor_v2"

# Why a candidate produced no executable trade. Counted, never dropped.
FAIL_NO_ENTRY_BOOK = "no_two_sided_book_at_entry_arrival"
FAIL_NO_EXIT_BOOK = "no_two_sided_book_at_exit_arrival"
FAIL_ENTRY_LIQUIDITY = "entry_leg_insufficient_displayed_liquidity"
FAIL_EXIT_LIQUIDITY = "exit_leg_insufficient_displayed_liquidity"
FAIL_OUTSIDE_COVERAGE = "arrival_outside_certified_market_data_coverage"
FAIL_BAD_TS_RECV = "uncertifiable_timing_bad_ts_recv"
FAIL_NO_CONSENSUS = "no_strong_consensus"

EXECUTION_FAILURE_REASONS: tuple[str, ...] = (
    FAIL_NO_ENTRY_BOOK,
    FAIL_NO_EXIT_BOOK,
    FAIL_ENTRY_LIQUIDITY,
    FAIL_EXIT_LIQUIDITY,
    FAIL_OUTSIDE_COVERAGE,
    FAIL_BAD_TS_RECV,
)


def assert_frozen_plan(repo_root: Path) -> dict[str, Any]:
    """Verify the design document before anything substantive happens."""
    return assert_frozen_design(repo_root)


# ---------------------------------------------------------------------------
# The frozen pre-outcome artefacts
# ---------------------------------------------------------------------------


def verify_preoutcome_artifacts(repo_root: Path) -> dict[str, Any]:
    """Hash the manifest and every CSV it records, and refuse on any mismatch.

    Both halves are checked: the constants transcribed into the plan module, and
    the hashes the manifest itself records. Agreement between the two is what
    makes the artefacts self-describing rather than merely asserted.
    """
    base = repo_root / PREOUTCOME_RELATIVE_DIR
    manifest_path = base / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ValueError(f"the frozen pre-outcome manifest is missing at {manifest_path}")

    observed_manifest = sha256_of(manifest_path)
    if observed_manifest != EXPECTED_MANIFEST_SHA256:
        raise ValueError(
            f"the Stage-3.6 pre-outcome manifest has changed: {observed_manifest} "
            f"!= {EXPECTED_MANIFEST_SHA256}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("contains_post_decision_economic_outcome") is not False:
        raise ValueError(
            "the pre-outcome manifest does not declare itself free of "
            "post-decision economic outcomes"
        )

    problems: list[str] = []
    files: dict[str, Any] = {}
    for key, expected_sha in sorted(EXPECTED_CSV_SHA256.items()):
        recorded = (manifest.get("files") or {}).get(key)
        if not recorded:
            problems.append(f"{key}: the manifest records no such file")
            continue
        if recorded.get("sha256") != expected_sha:
            problems.append(
                f"{key}: the manifest records sha256={recorded.get('sha256')!r}, "
                f"the frozen plan expects {expected_sha}"
            )
            continue
        path = base / CSV_FILENAMES[key]
        if not path.is_file():
            problems.append(f"{key}: missing at {path}")
            continue
        observed = sha256_of(path)
        if observed != expected_sha:
            problems.append(f"{key}: on disk {observed} != recorded {expected_sha}")
            continue
        recorded_bytes = recorded.get("bytes")
        actual_bytes = path.stat().st_size
        if recorded_bytes is not None and recorded_bytes != actual_bytes:
            problems.append(
                f"{key}: {actual_bytes} bytes on disk, manifest records "
                f"{recorded_bytes}"
            )
            continue
        files[key] = {"path": str(path), "sha256": observed, "bytes": actual_bytes}

    # The manifest's own governance block must match the frozen ledger.
    governance = manifest.get("governance") or {}
    from app.services.mbo_stage36_plan import (
        EFFECTIVE_TRIALS_AFTER_OUTCOME,
        PRIOR_EFFECTIVE_TRIALS,
    )

    if governance.get("prior_effective_trials") != PRIOR_EFFECTIVE_TRIALS:
        problems.append(
            f"manifest prior_effective_trials="
            f"{governance.get('prior_effective_trials')!r}, expected "
            f"{PRIOR_EFFECTIVE_TRIALS}"
        )
    if governance.get("effective_trials_after_outcome") != EFFECTIVE_TRIALS_AFTER_OUTCOME:
        problems.append(
            f"manifest effective_trials_after_outcome="
            f"{governance.get('effective_trials_after_outcome')!r}, expected "
            f"{EFFECTIVE_TRIALS_AFTER_OUTCOME}"
        )
    if governance.get("authorizes_paper_or_live") is not False:
        problems.append("manifest does not declare authorizes_paper_or_live=false")

    if problems:
        raise ValueError(
            "the frozen Stage-3.6 pre-outcome artefacts do not verify:"
            + "".join(f"\n  - {p}" for p in problems)
        )
    return {"manifest": {"path": str(manifest_path), "sha256": observed_manifest}, "files": files}


# ---------------------------------------------------------------------------
# The frozen candidate population
# ---------------------------------------------------------------------------


def _parse_timestamp(text: str) -> int:
    """A recorded timestamp, in UTC nanoseconds, to the last nanosecond.

    The CSVs carry ISO-8601 with an offset and nine fractional digits. The
    stdlib is not usable here: ``datetime`` resolves to microseconds, and
    ``fromisoformat`` *silently truncates* the final three digits rather than
    refusing them -- so ``...494493570`` parsed to ``...494493000`` with no
    error anywhere. Every recomputed prediction instant then disagreed with the
    census by a few hundred nanoseconds, which reads exactly like a model
    mismatch and is nothing of the sort.

    ``pandas.Timestamp`` carries epoch nanoseconds natively, so the round trip
    is exact. Anything unparseable is still a refusal rather than a guess: a
    misread instant would silently move a decision.
    """
    import pandas as pd

    cleaned = text.strip()
    if not cleaned:
        raise ValueError("empty timestamp")
    try:
        moment = pd.Timestamp(cleaned)
    except Exception as error:  # pandas raises several types for bad input
        raise ValueError(f"timestamp {text!r} is not a parseable instant: {error}") from error
    if moment is pd.NaT or pd.isna(moment):
        raise ValueError(f"timestamp {text!r} is not a valid instant")
    if moment.tzinfo is None:
        raise ValueError(f"timestamp {text!r} carries no timezone")
    # ``.value`` is UTC epoch nanoseconds as an exact integer. No rounding, no
    # truncation, no tolerance: the runtime instant and the frozen instant must
    # be the same nanosecond or the reconciliation refuses.
    return int(moment.tz_convert("UTC").value)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One frozen news event, with its consensus decision already fixed."""

    symbol: str
    session_date: str
    story_id: str
    known_at_ns: int
    consensus: str
    direction: int | None
    abs_shock_bps: float | None
    cell_signs: tuple[float, ...]
    cell_prediction_ts: tuple[int, ...]

    @property
    def t0_ns(self) -> int:
        return self.known_at_ns

    @property
    def td_ns(self) -> int:
        return self.known_at_ns + DECISION_DELAY_NS

    @property
    def entry_arrival_ns(self) -> int:
        return self.td_ns + LATENCY_NS

    @property
    def exit_request_ns(self) -> int:
        return self.td_ns + HOLDING_NS

    @property
    def exit_arrival_ns(self) -> int:
        return self.exit_request_ns + LATENCY_NS

    @property
    def is_strong_consensus(self) -> bool:
        return self.consensus in STRONG_CONSENSUS


def load_candidates(repo_root: Path) -> list[Candidate]:
    """The complete frozen candidate population, read from the certified CSV.

    This is the *commitment*, not the signal. It fixes which events exist and
    what the models were claimed to have said about them, so that the runtime
    recomputation has something to be checked against. Trading directions are
    never taken from here -- see ``recompute_consensus``, which regenerates every
    prediction from the frozen fits, and ``reconcile_with_frozen_census``, which
    refuses if the two disagree about any single event.
    """
    path = repo_root / PREOUTCOME_RELATIVE_DIR / CSV_FILENAMES["consensus_census"]
    candidates: list[Candidate] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            signs: list[float] = []
            times: list[int] = []
            for index in range(1, len(FROZEN_CELLS) + 1):
                raw_sign = (row.get(f"cell_{index}_sign") or "").strip()
                raw_time = (row.get(f"cell_{index}_prediction_time") or "").strip()
                signs.append(float(raw_sign) if raw_sign else float("nan"))
                times.append(_parse_timestamp(raw_time) if raw_time else -1)
            raw_direction = (row.get("consensus_direction") or "").strip()
            raw_shock = (row.get("abs_shock_bps") or "").strip()
            candidates.append(
                Candidate(
                    symbol=row["symbol"],
                    session_date=row["session_date"],
                    story_id=row["story_id"],
                    known_at_ns=_parse_timestamp(row["known_at"]),
                    consensus=row["consensus"],
                    direction=int(float(raw_direction)) if raw_direction else None,
                    abs_shock_bps=float(raw_shock) if raw_shock else None,
                    cell_signs=tuple(signs),
                    cell_prediction_ts=tuple(times),
                )
            )
    return candidates


def consensus_counts(candidates: Sequence[Candidate]) -> dict[str, int]:
    """The tallies the frozen design requires to reproduce exactly."""
    counts = {
        CONSENSUS_4_OF_4: 0,
        CONSENSUS_3_OF_4: 0,
        CONSENSUS_2_VS_2: 0,
        CONSENSUS_INCOMPLETE: 0,
    }
    for candidate in candidates:
        if candidate.consensus not in counts:
            raise ValueError(
                f"unrecognised consensus label {candidate.consensus!r}; Stage 3.6 "
                "will not classify a label its frozen design does not define"
            )
        counts[candidate.consensus] += 1
    counts["strong_consensus"] = sum(
        1 for c in candidates if c.is_strong_consensus
    )
    counts["measured_events"] = len(candidates)
    return counts


def assert_frozen_counts(candidates: Sequence[Candidate]) -> dict[str, int]:
    """Refuse unless the frozen population reproduces exactly.

    The design names these numbers and says mismatch means refuse to run. A
    population that differs by even one event is a different experiment from the
    one that was frozen.
    """
    observed = consensus_counts(candidates)
    problems: list[str] = []

    def require(label: str, got: int, want: int) -> None:
        if got != want:
            problems.append(f"{label}: {got}, expected {want}")

    require("measured events", observed["measured_events"], EXPECTED_COUNTS["news_events"])
    for label in (CONSENSUS_4_OF_4, CONSENSUS_3_OF_4, CONSENSUS_2_VS_2, CONSENSUS_INCOMPLETE):
        require(label, observed[label], EXPECTED_COUNTS[label])
    require(
        "strong consensus",
        observed["strong_consensus"],
        EXPECTED_COUNTS["strong_consensus"],
    )

    if problems:
        raise ValueError(
            "the Stage-3.6 frozen counts do not reproduce, so this is not the "
            "population the design froze:" + "".join(f"\n  - {p}" for p in problems)
        )
    return observed


def assert_predictions_are_causal(candidates: Sequence[Candidate]) -> dict[str, Any]:
    """Every contributing prediction must lie in ``[t0, td]``.

    A prediction formed before ``t0`` is stale -- it cannot be a reaction to news
    that was not yet knowable -- and one after ``td`` would not have existed at
    the decision instant. Both are refusals.
    """
    stale: list[str] = []
    late: list[str] = []
    checked = 0
    for candidate in candidates:
        if not candidate.is_strong_consensus:
            continue
        for cell, moment in zip(FROZEN_CELLS, candidate.cell_prediction_ts, strict=True):
            if moment < 0:
                continue
            checked += 1
            if moment < candidate.t0_ns:
                stale.append(f"{candidate.symbol} {candidate.story_id[:12]} {cell}")
            elif moment > candidate.td_ns:
                late.append(f"{candidate.symbol} {candidate.story_id[:12]} {cell}")
    if stale or late:
        raise ValueError(
            "Stage-3.6 predictions violate the frozen causal window [t0, td]:"
            + "".join(f"\n  - stale (before t0): {s}" for s in stale[:5])
            + "".join(f"\n  - late (after td): {s}" for s in late[:5])
        )
    return {"predictions_checked": checked, "all_within_t0_to_td": True}


def assert_consensus_is_internally_consistent(
    candidates: Sequence[Candidate],
) -> dict[str, Any]:
    """The recorded label must follow from the recorded signs.

    Reading a frozen decision is only safe if the decision is checkable. Every
    label is re-derived from the four signs under the design's own rule, and a
    disagreement is a refusal.
    """
    problems: list[str] = []
    for candidate in candidates:
        signs = [s for s in candidate.cell_signs if not np.isnan(s)]
        directional = [s for s in signs if s != 0.0]
        if len(directional) < len(FROZEN_CELLS):
            expected = CONSENSUS_INCOMPLETE
        else:
            positive = sum(1 for s in directional if s > 0)
            negative = len(directional) - positive
            if positive == 4 or negative == 4:
                expected = CONSENSUS_4_OF_4
            elif positive == 3 or negative == 3:
                expected = CONSENSUS_3_OF_4
            else:
                expected = CONSENSUS_2_VS_2
        if expected != candidate.consensus:
            problems.append(
                f"{candidate.symbol} {candidate.story_id[:12]}: recorded "
                f"{candidate.consensus}, signs imply {expected}"
            )
            continue
        if candidate.is_strong_consensus:
            positive = sum(1 for s in directional if s > 0)
            majority = LONG if positive > len(directional) - positive else SHORT
            if candidate.direction != majority:
                problems.append(
                    f"{candidate.symbol} {candidate.story_id[:12]}: recorded "
                    f"direction {candidate.direction}, majority is {majority}"
                )
    if problems:
        raise ValueError(
            "the frozen consensus labels do not follow from the frozen signs:"
            + "".join(f"\n  - {p}" for p in problems[:10])
        )
    return {"candidates_rederived": len(candidates), "labels_consistent": True}


# ---------------------------------------------------------------------------
# One executed trade
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutedTrade:
    """A completed 100-share round trip against the certified book."""

    symbol: str
    session_date: str
    story_id: str
    direction: int
    consensus: str
    abs_shock_bps: float | None
    entry_arrival_ns: int
    exit_arrival_ns: int
    entry_fill: float
    exit_fill: float
    entry_midpoint: float
    exit_midpoint: float
    entry_levels: int
    exit_levels: int
    entry_displayed: int
    exit_displayed: int
    shares: int
    price_scale: float

    @property
    def holding_ns(self) -> int:
        return self.exit_arrival_ns - self.entry_arrival_ns

    @property
    def realized_return_bps(self) -> float:
        """``s * (exit_fill - entry_fill) / entry_fill * 10000``, exactly."""
        return (
            self.direction
            * (self.exit_fill - self.entry_fill)
            / self.entry_fill
            * BPS
        )

    @property
    def gross_midpoint_return_bps(self) -> float:
        """What an infinitely liquid, fee-free version would have earned."""
        return (
            self.direction
            * (self.exit_midpoint - self.entry_midpoint)
            / self.entry_midpoint
            * BPS
        )

    @property
    def execution_cost_bps(self) -> float:
        """Gross minus realized: what crossing the book cost, both legs."""
        return self.gross_midpoint_return_bps - self.realized_return_bps

    def fees_bps(self, schedule: dict[str, Any]) -> float:
        """The frozen Stage-3 schedule, in basis points of entry notional.

        Section 31 and TAF attach to the sale leg, which is the exit for a long
        and the entry for a short.
        """
        entry_notional = self.entry_fill / self.price_scale * self.shares
        exit_notional = self.exit_fill / self.price_scale * self.shares
        per_share = (
            schedule["commission_usd_per_share"]
            + schedule["exchange_take_fee_usd_per_share"]
            + schedule["clearing_usd_per_share"]
            + schedule["cat_usd_per_share"]
        ) * self.shares * 2
        sale_notional = exit_notional if self.direction > 0 else entry_notional
        sec = (
            schedule["sec_section_31_usd_per_million_sold"]
            * sale_notional
            / 1_000_000.0
        )
        taf = min(
            schedule["finra_taf_usd_per_share_sold"] * self.shares,
            schedule["finra_taf_cap_usd_per_trade"],
        )
        return (per_share + sec + taf) / entry_notional * BPS

    def net_return_bps(self, schedule: dict[str, Any]) -> float:
        return self.realized_return_bps - self.fees_bps(schedule)


def entry_side(direction: int) -> str:
    """LONG consumes asks on entry; SHORT consumes bids."""
    return "buy" if direction == LONG else "sell"


def exit_side(direction: int) -> str:
    """LONG consumes bids on exit; SHORT consumes asks."""
    return "sell" if direction == LONG else "buy"


def execute_candidate(
    candidate: Candidate,
    *,
    book_at,
    price_scale: float,
    timing_certified=None,
    within_coverage=None,
    shares: int = TRADE_SIZE_SHARES,
) -> tuple[ExecutedTrade | None, str | None]:
    """One frozen candidate against the certified book, or a named failure.

    Both legs must fill completely inside the displayed depth budget. A partial
    fill is not a trade at a worse price -- it is an execution failure, counted
    as one.
    """
    if not candidate.is_strong_consensus or candidate.direction is None:
        return None, FAIL_NO_CONSENSUS

    entry_arrival = candidate.entry_arrival_ns
    exit_arrival = candidate.exit_arrival_ns

    if within_coverage is not None and not within_coverage(entry_arrival, exit_arrival):
        return None, FAIL_OUTSIDE_COVERAGE
    if timing_certified is not None and not timing_certified(entry_arrival, exit_arrival):
        return None, FAIL_BAD_TS_RECV

    entry_book = book_at(entry_arrival)
    if entry_book is None or not entry_book.two_sided:
        return None, FAIL_NO_ENTRY_BOOK
    exit_book = book_at(exit_arrival)
    if exit_book is None or not exit_book.two_sided:
        return None, FAIL_NO_EXIT_BOOK

    entry_action = entry_side(candidate.direction)
    exit_action = exit_side(candidate.direction)
    entry = walk_book(entry_book, entry_action, shares)
    if entry is None:
        return None, FAIL_ENTRY_LIQUIDITY
    exit_fill = walk_book(exit_book, exit_action, shares)
    if exit_fill is None:
        return None, FAIL_EXIT_LIQUIDITY

    return (
        ExecutedTrade(
            symbol=candidate.symbol,
            session_date=candidate.session_date,
            story_id=candidate.story_id,
            direction=candidate.direction,
            consensus=candidate.consensus,
            abs_shock_bps=candidate.abs_shock_bps,
            entry_arrival_ns=entry_arrival,
            exit_arrival_ns=exit_arrival,
            entry_fill=entry[0],
            exit_fill=exit_fill[0],
            entry_midpoint=float(entry_book.midpoint),  # type: ignore[arg-type]
            exit_midpoint=float(exit_book.midpoint),  # type: ignore[arg-type]
            entry_levels=entry[1],
            exit_levels=exit_fill[1],
            entry_displayed=entry_book.displayed(entry_action),
            exit_displayed=exit_book.displayed(exit_action),
            shares=shares,
            price_scale=float(price_scale),
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Accumulation, in constant space
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Running:
    total: float = 0.0
    count: int = 0

    def add(self, value: float) -> None:
        self.total += float(value)
        self.count += 1

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass
class Stage36Accumulator:
    """Sufficient statistics for the single primary specification.

    Trades are folded in and discarded, as in Stage 3.5: every reported quantity
    is a mean, and a mean needs a sum and a count. The only state that grows is
    the per-session and per-symbol bookkeeping, bounded by 20 and 8.
    """

    price_scale: float = 1.0
    fee_schedule_name: str = PRIMARY_FEE_SCHEDULE

    executable_trades: int = 0
    long_trades: int = 0
    short_trades: int = 0

    net: _Running = field(default_factory=_Running)
    realized: _Running = field(default_factory=_Running)
    gross: _Running = field(default_factory=_Running)
    execution_cost: _Running = field(default_factory=_Running)
    fees: _Running = field(default_factory=_Running)
    entry_levels: _Running = field(default_factory=_Running)
    exit_levels: _Running = field(default_factory=_Running)
    holding: _Running = field(default_factory=_Running)

    by_session: dict[str, _Running] = field(default_factory=dict)
    by_symbol: dict[str, _Running] = field(default_factory=dict)
    by_consensus: dict[str, _Running] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    def record_failure(self, reason: str) -> None:
        self.failures[reason] = self.failures.get(reason, 0) + 1

    def record_trade(self, trade: ExecutedTrade) -> None:
        schedule = FEE_SCHEDULES[self.fee_schedule_name]
        net = trade.net_return_bps(schedule)
        self.executable_trades += 1
        if trade.direction == LONG:
            self.long_trades += 1
        else:
            self.short_trades += 1
        self.net.add(net)
        self.realized.add(trade.realized_return_bps)
        self.gross.add(trade.gross_midpoint_return_bps)
        self.execution_cost.add(trade.execution_cost_bps)
        self.fees.add(trade.fees_bps(schedule))
        self.entry_levels.add(trade.entry_levels)
        self.exit_levels.add(trade.exit_levels)
        self.holding.add(trade.holding_ns)
        self.by_session.setdefault(trade.session_date, _Running()).add(net)
        self.by_symbol.setdefault(trade.symbol, _Running()).add(net)
        self.by_consensus.setdefault(trade.consensus, _Running()).add(net)

    def summary(self) -> dict[str, Any]:
        """The one primary economic specification, plus declared diagnostics."""
        base: dict[str, Any] = {
            "stage36_executor_version": STAGE36_EXECUTOR_VERSION,
            "stage36_plan_version": STAGE36_PLAN_VERSION,
            "plan_design_hash": PLAN_DESIGN_HASH,
            "fee_schedule": self.fee_schedule_name,
            "executable_trades": self.executable_trades,
            "sessions_represented": len(self.by_session),
            "execution_failures": dict(sorted(self.failures.items())),
        }

        sample_gate_passed = (
            self.executable_trades >= MIN_EXECUTABLE_TRADES
            and len(self.by_session) >= MIN_SESSIONS
        )
        base["sample_gate"] = {
            "minimum_executable_trades": MIN_EXECUTABLE_TRADES,
            "minimum_sessions": MIN_SESSIONS,
            "passed": sample_gate_passed,
        }
        if not sample_gate_passed:
            return {
                **base,
                "verdict": VERDICT_INSUFFICIENT,
                "stretch_8bps_supported": False,
                "authorizes_paper_or_live": False,
            }

        session_means = {
            session: running.total / running.count
            for session, running in sorted(self.by_session.items())
        }
        statistic, p_value = clustered_t(list(session_means.values()))
        mean_net = self.net.mean or 0.0
        t_ok = statistic is not None and statistic >= T_HURDLE
        supported = mean_net >= PRIMARY_TARGET_BPS and t_ok
        stretch = mean_net >= STRETCH_TARGET_BPS and t_ok

        return {
            **base,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "mean_net_return_bps": mean_net,
            "mean_realized_return_bps": self.realized.mean,
            "mean_gross_midpoint_return_bps": self.gross.mean,
            "mean_execution_cost_bps": self.execution_cost.mean,
            "mean_fees_bps": self.fees.mean,
            "mean_entry_levels_walked": self.entry_levels.mean,
            "mean_exit_levels_walked": self.exit_levels.mean,
            "mean_holding_ns": self.holding.mean,
            "session_clustered_t": statistic,
            "p_value": p_value,
            "per_session_net_bps": session_means,
            "per_symbol_net_bps": {
                symbol: running.total / running.count
                for symbol, running in sorted(self.by_symbol.items())
            },
            "per_consensus_net_bps": {
                label: running.total / running.count
                for label, running in sorted(self.by_consensus.items())
            },
            "verdict": VERDICT_SUPPORTED if supported else VERDICT_NO_MECHANISM,
            "stretch_8bps_supported": stretch,
            "authorizes_paper_or_live": False,
            "authorizes": (
                "an untouched external confirmation experiment, only"
                if supported
                else "nothing"
            ),
        }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Runtime prediction: the models decide, not the census
# ---------------------------------------------------------------------------
#
# The frozen consensus CSV is a commitment, not a signal source. Reading a
# direction out of it would mean the experiment never actually consulted the
# models it claims to be testing -- and a transcription error, or a census built
# under a subtly different selection rule, would be undetectable. So every
# prediction is recomputed here from the frozen fits and the frozen features,
# and the census is then required to match, event by event.


def cell_cadence(cell: str) -> str:
    """``200ev|next_change`` -> ``200ev``."""
    return cell.split("|")[0]


def compute_cell_predictions(features_dir: Path, stem: str, cadence: str, beta):
    """Predictions for one symbol-day and one cadence, on the frozen features.

    Uses Stage 2's own ``_symbol_day_matrix`` and Stage 3's own ``predict``: a
    local re-implementation of either would be a second definition of the model
    input, and the two would drift.

    Reads **only** the feature parquet. No label file is opened, and no
    ``next_change`` / ``next_2_changes`` resolution timestamp is consulted --
    Stage 3.6's decision clock is ``feature_available_ts_recv`` and the news
    ``known_at``, and nothing about the future may enter it.
    """
    import pyarrow.parquet as pq

    from app.cli.mbo_stage2 import FEATURE_NAMES, _symbol_day_matrix
    from app.services.mbo_stage3_executor import predict

    path = features_dir / cadence / f"{stem}.{cadence}.parquet"
    if not path.is_file():
        return None

    table = pq.read_table(
        path, columns=["sequence_index", "feature_available_ts_recv", *FEATURE_NAMES]
    )
    design, _sequence = _symbol_day_matrix(table, FEATURE_NAMES)
    available = np.asarray(
        table.column("feature_available_ts_recv").to_numpy(zero_copy_only=False),
        np.int64,
    )
    finite = np.isfinite(design).all(axis=1)
    predictions = predict(np.nan_to_num(design, nan=0.0), beta)
    return {"available_ts_recv": available, "predictions": predictions, "finite": finite}


def select_latest_prediction(
    available_ts_recv, predictions, finite, t0_ns: int, td_ns: int
) -> tuple[float | None, int | None]:
    """The latest finite prediction whose availability lies in ``[t0, td]``.

    A prediction available before ``t0`` is stale: it cannot be a reaction to
    news that was not yet knowable. One after ``td`` did not exist at the
    decision instant. Both ends are inclusive, as the design writes them.
    """
    window = finite & (available_ts_recv >= t0_ns) & (available_ts_recv <= td_ns)
    if not window.any():
        return None, None
    indices = np.flatnonzero(window)
    # "Latest" by availability, not by row order, so an out-of-order file cannot
    # silently change which prediction is used.
    chosen = indices[int(np.argmax(available_ts_recv[indices]))]
    return float(predictions[chosen]), int(available_ts_recv[chosen])


def classify_consensus(
    predictions: Sequence[float | None],
) -> tuple[str, int | None]:
    """The frozen consensus rule, applied to four recomputed predictions.

    All four models must speak, and none may abstain with an exact zero. Only
    then does agreement decide a direction.
    """
    if any(p is None for p in predictions):
        return CONSENSUS_INCOMPLETE, None
    values = [float(p) for p in predictions]  # type: ignore[arg-type]
    if any(v == 0.0 for v in values):
        return CONSENSUS_INCOMPLETE, None
    positive = sum(1 for v in values if v > 0)
    negative = len(values) - positive
    if positive == len(values):
        return CONSENSUS_4_OF_4, LONG
    if negative == len(values):
        return CONSENSUS_4_OF_4, SHORT
    if positive == 3:
        return CONSENSUS_3_OF_4, LONG
    if negative == 3:
        return CONSENSUS_3_OF_4, SHORT
    return CONSENSUS_2_VS_2, None


def recompute_consensus(
    frozen: Sequence[Candidate], features_dir: Path, fits: dict[str, Any]
) -> list[Candidate]:
    """Rebuild every event's decision from the models themselves.

    Returns candidates whose consensus and direction come from the recomputed
    predictions. The frozen census is not consulted for any decision here -- only
    for the identity of the events to evaluate, which is what makes the
    subsequent reconciliation a real check rather than a tautology.
    """
    by_stem: dict[str, list[Candidate]] = {}
    for candidate in frozen:
        by_stem.setdefault(f"{candidate.symbol}_{candidate.session_date}", []).append(
            candidate
        )

    rebuilt: list[Candidate] = []
    for stem in sorted(by_stem):
        session_date = by_stem[stem][0].session_date
        # One parquet read per cadence, reused by both cells sharing it.
        per_cadence: dict[str, Any] = {}
        for cell in FROZEN_CELLS:
            cadence = cell_cadence(cell)
            entry = fits.get(cell, {}).get(session_date)
            if entry is None:
                per_cadence[cell] = None
                continue
            per_cadence[cell] = compute_cell_predictions(
                features_dir, stem, cadence, entry["beta"]
            )

        for candidate in by_stem[stem]:
            predictions: list[float | None] = []
            moments: list[int] = []
            for cell in FROZEN_CELLS:
                computed = per_cadence.get(cell)
                if computed is None:
                    predictions.append(None)
                    moments.append(-1)
                    continue
                value, moment = select_latest_prediction(
                    computed["available_ts_recv"],
                    computed["predictions"],
                    computed["finite"],
                    candidate.t0_ns,
                    candidate.td_ns,
                )
                predictions.append(value)
                moments.append(moment if moment is not None else -1)

            consensus, direction = classify_consensus(predictions)
            signs = tuple(
                float(np.sign(p)) if p is not None else float("nan")
                for p in predictions
            )
            rebuilt.append(
                Candidate(
                    symbol=candidate.symbol,
                    session_date=candidate.session_date,
                    story_id=candidate.story_id,
                    known_at_ns=candidate.known_at_ns,
                    consensus=consensus,
                    direction=direction,
                    abs_shock_bps=candidate.abs_shock_bps,
                    cell_signs=signs,
                    cell_prediction_ts=tuple(moments),
                )
            )
    return rebuilt


def reconcile_with_frozen_census(
    runtime: Sequence[Candidate],
    frozen: Sequence[Candidate],
    *,
    compare_prediction_times: bool = True,
) -> dict[str, Any]:
    """Require the recomputed decisions to match the frozen census, event by event.

    Aggregate counts alone would not do: two populations can share a histogram
    and disagree about which event is which. So identity, all four signs, the
    classification and the direction are compared per event, and only then are
    the totals checked.

    Any mismatch refuses. The census is a commitment made before any outcome
    existed; if the models no longer reproduce it, something has moved and the
    experiment is not the one that was frozen.
    """
    runtime_by_key = {(c.symbol, c.session_date, c.story_id): c for c in runtime}
    frozen_by_key = {(c.symbol, c.session_date, c.story_id): c for c in frozen}

    problems: list[str] = []
    missing = sorted(set(frozen_by_key) - set(runtime_by_key))
    extra = sorted(set(runtime_by_key) - set(frozen_by_key))
    for key in missing[:5]:
        problems.append(f"{key[0]} {key[2][:12]}: in the census, not recomputed")
    for key in extra[:5]:
        problems.append(f"{key[0]} {key[2][:12]}: recomputed, not in the census")

    compared = 0
    for key, expected in frozen_by_key.items():
        actual = runtime_by_key.get(key)
        if actual is None:
            continue
        compared += 1
        label = f"{key[0]} {key[1]} {key[2][:12]}"
        for index, (want, got) in enumerate(
            zip(expected.cell_signs, actual.cell_signs, strict=True), start=1
        ):
            same = (np.isnan(want) and np.isnan(got)) or want == got
            if not same:
                problems.append(
                    f"{label}: cell_{index} sign recomputed {got}, census {want}"
                )
        if expected.consensus != actual.consensus:
            problems.append(
                f"{label}: consensus recomputed {actual.consensus}, census "
                f"{expected.consensus}"
            )
        if expected.direction != actual.direction:
            problems.append(
                f"{label}: direction recomputed {actual.direction}, census "
                f"{expected.direction}"
            )
        if compare_prediction_times:
            for index, (want, got) in enumerate(
                zip(expected.cell_prediction_ts, actual.cell_prediction_ts, strict=True),
                start=1,
            ):
                if want >= 0 and got >= 0 and want != got:
                    problems.append(
                        f"{label}: cell_{index} prediction instant recomputed {got}, "
                        f"census {want}"
                    )

    if problems:
        raise ValueError(
            "the recomputed Stage-3.6 consensus does not reproduce the frozen "
            "census, so the models feeding this experiment are not the models it "
            "was frozen against:" + "".join(f"\n  - {p}" for p in problems[:15])
        )

    # Aggregates last: identity has already been established, so a count
    # mismatch here would mean the census itself disagrees with its own rows.
    runtime_counts = assert_frozen_counts(runtime)
    return {
        "events_compared": compared,
        "event_level_match": True,
        "prediction_instants_compared": compare_prediction_times,
        "recomputed_counts": runtime_counts,
        "consensus_source": "runtime model recomputation",
    }
