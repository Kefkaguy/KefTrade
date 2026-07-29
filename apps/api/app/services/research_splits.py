"""Phase E: nested research splits and the locked confirmation protocol.

The Phase A audit found that `run_backtest` skips its training window and
trades only the validation portion, so every stored trade is tagged
`dataset_split = 'validation'` and never `'train'`. The train/validation tag
therefore describes one sample, not two, and any check that treats it as an
out-of-sample comparison is comparing a set with itself. This module replaces
that degenerate split with three windows that are genuinely separate:

  * **discovery**    -- explore freely. Look as often as you like.
  * **validation**   -- select families and candidates. Every look is logged
                        and counted, because looking is what spends it.
  * **confirmation** -- untouched until a candidate is frozen, then read
                        exactly once.

The reason validation needs a counter and confirmation needs a lock is the
same reason: a hold-out set stops being a hold-out the moment you iterate
against it. Testing 120 variants and keeping the one that scored best on
validation has fitted the validation window just as surely as gradient
descent would have -- the search just happened in the researcher's head. That
is why `multiple_testing_ledger` reports the honest trial count, and why
`run_confirmation` refuses a second run against the same frozen candidate.

Nothing here changes the elite gate, and nothing here promotes anything. It
constrains which data a decision is allowed to see, and records what has
already been spent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

SPLIT_VERSION = "nested_research_splits_v1"
SESSION_SPLIT_VERSION = "nested_research_session_splits_v2"
CONFIRMATION_PROTOCOL_VERSION = "locked_confirmation_v1"

PHASES = ("discovery", "validation", "confirmation")

# Default proportions. Confirmation is the most recent window because it is
# the one that must resemble the conditions a deployment would actually meet;
# discovery gets the largest share because exploration is where sample size
# is cheapest to spend.
DEFAULT_DISCOVERY_RATIO = 0.50
DEFAULT_VALIDATION_RATIO = 0.30
# Confirmation takes the remainder (0.20 by default).

# Once validation has influenced this many decisions, selections made against
# it should be treated as in-sample. This is a reporting threshold, not an
# enforcement one: the number of looks is a fact, and what to do about it is
# a research decision.
VALIDATION_REUSE_WARNING_THRESHOLD = 20


@dataclass(frozen=True)
class NestedSplits:
    """Three contiguous, non-overlapping windows over one dataset."""

    discovery_start: datetime
    discovery_end: datetime
    validation_start: datetime
    validation_end: datetime
    confirmation_start: datetime
    confirmation_end: datetime
    split_version: str = SPLIT_VERSION

    def phase_for(self, timestamp: datetime) -> str | None:
        if self.discovery_start <= timestamp < self.validation_start:
            return "discovery"
        if self.validation_start <= timestamp < self.confirmation_start:
            return "validation"
        if self.confirmation_start <= timestamp <= self.confirmation_end:
            return "confirmation"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "split_version": self.split_version,
            "discovery_start": self.discovery_start.isoformat(),
            "discovery_end": self.discovery_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "confirmation_start": self.confirmation_start.isoformat(),
            "confirmation_end": self.confirmation_end.isoformat(),
        }


def compute_nested_splits(
    timestamps: Sequence[datetime],
    *,
    discovery_ratio: float = DEFAULT_DISCOVERY_RATIO,
    validation_ratio: float = DEFAULT_VALIDATION_RATIO,
) -> NestedSplits:
    """Split a sorted timestamp range chronologically into three windows.

    Chronological, never random: a random split would let a strategy learn
    from bars that come after the ones it is tested on, which for time-series
    data is lookahead wearing a disguise.

    Boundaries are derived from the ordered timestamps rather than from wall
    clock dates, so an irregular calendar (holidays, half days, missing bars)
    still yields three windows with proportional amounts of *evidence* rather
    than proportional amounts of elapsed time.
    """
    if discovery_ratio <= 0 or validation_ratio <= 0:
        raise ValueError("discovery and validation ratios must be positive")
    if discovery_ratio + validation_ratio >= 1.0:
        raise ValueError("discovery + validation must leave a non-empty confirmation window")

    ordered = sorted(set(timestamps))
    if len(ordered) < len(PHASES):
        raise ValueError("at least three distinct timestamps are required to form three windows")

    total = len(ordered)
    discovery_end_index = max(0, min(total - 3, int(total * discovery_ratio) - 1))
    validation_end_index = max(
        discovery_end_index + 1,
        min(total - 2, int(total * (discovery_ratio + validation_ratio)) - 1),
    )
    return NestedSplits(
        discovery_start=ordered[0],
        discovery_end=ordered[discovery_end_index],
        validation_start=ordered[discovery_end_index + 1],
        validation_end=ordered[validation_end_index],
        confirmation_start=ordered[validation_end_index + 1],
        confirmation_end=ordered[-1],
    )


def compute_session_nested_splits(
    observations: Sequence[tuple[datetime, Any]],
    *,
    discovery_ratio: float = DEFAULT_DISCOVERY_RATIO,
    validation_ratio: float = DEFAULT_VALIDATION_RATIO,
) -> NestedSplits:
    """Split intraday evidence only between complete exchange sessions."""
    if discovery_ratio <= 0 or validation_ratio <= 0:
        raise ValueError("discovery and validation ratios must be positive")
    if discovery_ratio + validation_ratio >= 1.0:
        raise ValueError("discovery + validation must leave a non-empty confirmation window")
    by_session: dict[Any, list[datetime]] = {}
    for timestamp, session_date in observations:
        by_session.setdefault(session_date, []).append(timestamp)
    ordered_sessions = sorted(by_session)
    if len(ordered_sessions) < len(PHASES):
        raise ValueError("at least three complete sessions are required to form three windows")
    total = len(ordered_sessions)
    discovery_end_index = max(0, min(total - 3, int(total * discovery_ratio) - 1))
    validation_end_index = max(
        discovery_end_index + 1,
        min(total - 2, int(total * (discovery_ratio + validation_ratio)) - 1),
    )
    discovery_sessions = ordered_sessions[: discovery_end_index + 1]
    validation_sessions = ordered_sessions[discovery_end_index + 1 : validation_end_index + 1]
    confirmation_sessions = ordered_sessions[validation_end_index + 1 :]
    return NestedSplits(
        discovery_start=min(by_session[discovery_sessions[0]]),
        discovery_end=max(by_session[discovery_sessions[-1]]),
        validation_start=min(by_session[validation_sessions[0]]),
        validation_end=max(by_session[validation_sessions[-1]]),
        confirmation_start=min(by_session[confirmation_sessions[0]]),
        confirmation_end=max(by_session[confirmation_sessions[-1]]),
        split_version=SESSION_SPLIT_VERSION,
    )


def filter_rows_to_phase(
    rows: Iterable[dict[str, Any]],
    splits: NestedSplits,
    phase: str,
    *,
    timestamp_key: str = "timestamp",
) -> list[dict[str, Any]]:
    """Restrict candles/features to one phase.

    Applied BEFORE the simulator runs, so a research backtest is structurally
    unable to observe the confirmation window -- the bars are simply not in
    the list it is given. `run_backtest`'s own `walk_forward_train_ratio`
    still applies inside whichever phase it is handed, where it acts as a
    warm-up, not as a second validation split.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
    return [row for row in rows if splits.phase_for(row[timestamp_key]) == phase]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def ensure_research_split_tables(conn: psycopg.Connection) -> None:
    """Idempotent table creation, matching the convention used by
    `ensure_research_architecture_tables`."""
    statements = (
        """
        CREATE TABLE IF NOT EXISTS research_dataset_splits (
            id BIGSERIAL PRIMARY KEY,
            dataset_id BIGINT NOT NULL UNIQUE,
            discovery_start TIMESTAMPTZ NOT NULL,
            discovery_end TIMESTAMPTZ NOT NULL,
            validation_start TIMESTAMPTZ NOT NULL,
            validation_end TIMESTAMPTZ NOT NULL,
            confirmation_start TIMESTAMPTZ NOT NULL,
            confirmation_end TIMESTAMPTZ NOT NULL,
            split_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            immutable BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_dataset_splits_immutable_check CHECK (immutable = TRUE),
            CONSTRAINT research_dataset_splits_order_check
                CHECK (discovery_end < validation_start AND validation_end < confirmation_start)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_split_access_log (
            id BIGSERIAL PRIMARY KEY,
            dataset_id BIGINT NOT NULL,
            phase TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            campaign_id BIGINT,
            candidate_id TEXT,
            detail JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT research_split_access_phase_check
                CHECK (phase IN ('discovery', 'validation', 'confirmation'))
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS research_split_access_dataset_idx
            ON research_split_access_log(dataset_id, phase)
        """,
        """
        CREATE TABLE IF NOT EXISTS research_confirmation_runs (
            id BIGSERIAL PRIMARY KEY,
            frozen_fingerprint TEXT NOT NULL UNIQUE,
            campaign_id BIGINT,
            candidate_id TEXT NOT NULL,
            dataset_id BIGINT NOT NULL,
            frozen_spec JSONB NOT NULL,
            metrics JSONB NOT NULL,
            gate_results JSONB NOT NULL,
            passed BOOLEAN NOT NULL,
            effective_trials INTEGER NOT NULL DEFAULT 1,
            protocol_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            immutable BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT research_confirmation_runs_immutable_check CHECK (immutable = TRUE)
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


def persist_dataset_splits(
    conn: psycopg.Connection, *, dataset_id: int, splits: NestedSplits
) -> dict[str, Any]:
    """Store a dataset's split boundaries once and never move them.

    Boundaries that could be recomputed after seeing results would let a
    disappointing confirmation window be redrawn until it cooperated, so the
    first write wins and later calls return the stored row unchanged.
    """
    ensure_research_split_tables(conn)
    row = conn.execute(
        """
        INSERT INTO research_dataset_splits(
            dataset_id, discovery_start, discovery_end, validation_start,
            validation_end, confirmation_start, confirmation_end, split_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset_id) DO NOTHING
        RETURNING *
        """,
        (
            dataset_id,
            splits.discovery_start,
            splits.discovery_end,
            splits.validation_start,
            splits.validation_end,
            splits.confirmation_start,
            splits.confirmation_end,
            splits.split_version,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM research_dataset_splits WHERE dataset_id = %s", (dataset_id,)
        ).fetchone()
    return dict(row)


def get_dataset_splits(conn: psycopg.Connection, dataset_id: int) -> NestedSplits | None:
    row = conn.execute(
        "SELECT * FROM research_dataset_splits WHERE dataset_id = %s", (dataset_id,)
    ).fetchone()
    if not row:
        return None
    return NestedSplits(
        discovery_start=row["discovery_start"],
        discovery_end=row["discovery_end"],
        validation_start=row["validation_start"],
        validation_end=row["validation_end"],
        confirmation_start=row["confirmation_start"],
        confirmation_end=row["confirmation_end"],
        split_version=str(row["split_version"]),
    )


# ---------------------------------------------------------------------------
# The access ledger
# ---------------------------------------------------------------------------

def record_split_access(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    phase: str,
    decision_type: str,
    campaign_id: int | None = None,
    candidate_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Log that a phase influenced a research decision.

    Every entry is one unit of statistical budget spent. Selecting on a
    window and then reporting a result from it as though it were independent
    is only defensible while this count is small, and the count is only
    knowable if it is written down at the moment of use.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
    ensure_research_split_tables(conn)
    conn.execute(
        """
        INSERT INTO research_split_access_log(
            dataset_id, phase, decision_type, campaign_id, candidate_id, detail
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (dataset_id, phase, decision_type, campaign_id, candidate_id, Jsonb(detail or {})),
    )


def split_usage_summary(conn: psycopg.Connection, dataset_id: int) -> dict[str, Any]:
    """How many times each phase has influenced a decision."""
    ensure_research_split_tables(conn)
    rows = conn.execute(
        """
        SELECT phase, decision_type, COUNT(*) AS uses
        FROM research_split_access_log
        WHERE dataset_id = %s
        GROUP BY phase, decision_type
        ORDER BY phase, decision_type
        """,
        (dataset_id,),
    ).fetchall()
    by_phase: dict[str, int] = {phase: 0 for phase in PHASES}
    by_decision: list[dict[str, Any]] = []
    for row in rows:
        by_phase[str(row["phase"])] = by_phase.get(str(row["phase"]), 0) + int(row["uses"])
        by_decision.append(
            {"phase": row["phase"], "decision_type": row["decision_type"], "uses": int(row["uses"])}
        )
    validation_uses = by_phase.get("validation", 0)
    confirmation_uses = by_phase.get("confirmation", 0)
    return {
        "dataset_id": dataset_id,
        "split_version": SPLIT_VERSION,
        "uses_by_phase": by_phase,
        "uses_by_decision_type": by_decision,
        "validation_is_effectively_training": validation_uses >= VALIDATION_REUSE_WARNING_THRESHOLD,
        "confirmation_is_spent": confirmation_uses > 0,
        "note": (
            "Each validation access spends statistical budget. Once validation has been "
            f"consulted {VALIDATION_REUSE_WARNING_THRESHOLD}+ times, treat selections made against it as "
            "in-sample and require confirmation evidence before believing them."
        ),
    }


# ---------------------------------------------------------------------------
# Multiple-testing accounting
# ---------------------------------------------------------------------------

def multiple_testing_ledger(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """The honest trial count behind a campaign's best-looking result.

    Feeds `null_models.deflated_sharpe_ratio`, whose whole purpose is to ask
    how impressive a track record is *given how many were tried*. Passing a
    guessed trial count there would defeat the estimator, so it is counted
    from the campaign's own rows.
    """
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT candidate_id) AS variants,
               COUNT(DISTINCT COALESCE(candidate->'parameters'->>'strategy_architecture', family_id)) AS families,
               COUNT(*) AS jobs,
               COUNT(DISTINCT symbol) AS symbols
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        """,
        (campaign_id,),
    ).fetchone()
    lineage = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE candidate->>'parent_candidate_id' IS NOT NULL) AS descendants,
               COUNT(DISTINCT candidate->>'parent_candidate_id') AS distinct_parents,
               MAX((candidate->>'generation')::int) AS deepest_generation
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND candidate IS NOT NULL
        """,
        (campaign_id,),
    ).fetchone()

    variants = int((row or {}).get("variants") or 0)
    families = int((row or {}).get("families") or 0)
    symbols = int((row or {}).get("symbols") or 0)
    # Each (variant, symbol) pair is a separate opportunity for one of them to
    # look good by chance, so the effective trial count is their product --
    # not the variant count alone, which is what a naive reading would use.
    effective_trials = max(1, variants * max(1, symbols))
    return {
        "campaign_id": campaign_id,
        "variants_tested": variants,
        "families_tested": families,
        "symbols_tested": symbols,
        "jobs_run": int((row or {}).get("jobs") or 0),
        "effective_trials": effective_trials,
        "lineage": {
            "descendant_candidates": int((lineage or {}).get("descendants") or 0),
            "distinct_parents": int((lineage or {}).get("distinct_parents") or 0),
            "deepest_generation": int((lineage or {}).get("deepest_generation") or 1),
        },
        "note": (
            "effective_trials counts every (variant, symbol) evaluation, because each is an "
            "independent chance for a result to look good by luck. Pass it to "
            "null_models.deflated_sharpe_ratio rather than the variant count alone."
        ),
    }


# ---------------------------------------------------------------------------
# The locked confirmation protocol
# ---------------------------------------------------------------------------

def freeze_fingerprint(
    *, candidate_id: str, dataset_id: int, parameters: dict[str, Any], blocks: dict[str, Any] | None = None
) -> str:
    """Content hash of exactly what is being confirmed.

    Any change to the parameters produces a different fingerprint and
    therefore a different confirmation slot -- which is the point. Tweaking a
    parameter after a failed confirmation is a new hypothesis, and it must
    cost a new confirmation rather than silently overwriting the old verdict.
    """
    payload = "|".join(
        [
            CONFIRMATION_PROTOCOL_VERSION,
            str(candidate_id),
            str(dataset_id),
            repr(sorted((str(key), str(value)) for key, value in (parameters or {}).items())),
            repr(sorted((str(key), str(value)) for key, value in (blocks or {}).items())),
        ]
    )
    return sha256(payload.encode("utf-8")).hexdigest()


class ConfirmationAlreadySpentError(RuntimeError):
    """Raised when a frozen candidate's single confirmation run already happened."""


def existing_confirmation(conn: psycopg.Connection, fingerprint: str) -> dict[str, Any] | None:
    ensure_research_split_tables(conn)
    row = conn.execute(
        "SELECT * FROM research_confirmation_runs WHERE frozen_fingerprint = %s", (fingerprint,)
    ).fetchone()
    return dict(row) if row else None


def run_confirmation(
    conn: psycopg.Connection,
    *,
    candidate_id: str,
    dataset_id: int,
    parameters: dict[str, Any],
    blocks: dict[str, Any] | None = None,
    campaign_id: int | None = None,
    metrics: dict[str, Any],
    gate_results: dict[str, Any],
    passed: bool,
    effective_trials: int = 1,
) -> dict[str, Any]:
    """Record the one confirmation run a frozen candidate is allowed.

    The caller supplies metrics already computed against the confirmation
    window (use `filter_rows_to_phase(..., 'confirmation')` to build it). This
    function's job is the protocol, not the arithmetic: it makes the run
    unrepeatable, logs that confirmation was spent, and stores the verdict
    immutably.

    Raises `ConfirmationAlreadySpentError` on a second attempt. That refusal
    is the entire value of the protocol -- a confirmation you may re-run until
    it passes is a validation set with extra steps.
    """
    ensure_research_split_tables(conn)
    fingerprint = freeze_fingerprint(
        candidate_id=candidate_id, dataset_id=dataset_id, parameters=parameters, blocks=blocks
    )
    previous = existing_confirmation(conn, fingerprint)
    if previous is not None:
        raise ConfirmationAlreadySpentError(
            f"candidate {candidate_id} was already confirmed against dataset {dataset_id} on "
            f"{previous['created_at']} (passed={previous['passed']}). Change the frozen parameters to "
            "create a new hypothesis, or accept the recorded verdict."
        )

    row = conn.execute(
        """
        INSERT INTO research_confirmation_runs(
            frozen_fingerprint, campaign_id, candidate_id, dataset_id, frozen_spec,
            metrics, gate_results, passed, effective_trials, protocol_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            fingerprint,
            campaign_id,
            candidate_id,
            dataset_id,
            Jsonb({"parameters": parameters, "blocks": blocks or {}}),
            Jsonb(metrics),
            Jsonb(gate_results),
            bool(passed),
            int(effective_trials),
            CONFIRMATION_PROTOCOL_VERSION,
        ),
    ).fetchone()
    record_split_access(
        conn,
        dataset_id=dataset_id,
        phase="confirmation",
        decision_type="final_confirmation",
        campaign_id=campaign_id,
        candidate_id=candidate_id,
        detail={"frozen_fingerprint": fingerprint, "passed": bool(passed)},
    )
    return dict(row)


def freeze_and_confirm_candidate(
    conn: psycopg.Connection,
    *,
    campaign_id: int,
    candidate_id: str,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    """Run a frozen candidate against the locked window, once, and record it.

    This is the only path that executes a backtest over confirmation data. It
    loads the candidate exactly as stored (no re-tuning -- that is what
    "frozen" means), restricts the dataset to the confirmation phase so the
    simulator structurally cannot see anything else, applies the UNCHANGED
    elite gate, and hands the verdict to `run_confirmation`, which refuses a
    second attempt.

    The trial count it is judged against comes from the campaign's own
    `multiple_testing_ledger`, not from a guess.
    """
    from app.services.labs.intraday.dataset import load_intraday_backtest_dataset
    from app.services.research_campaigns import candidate_from_payload
    from app.services.strategy_discovery import evaluate_candidate

    job = conn.execute(
        """
        SELECT id, candidate, dataset_id
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND candidate_id = %s AND symbol = %s AND timeframe = %s
          AND candidate IS NOT NULL
        LIMIT 1
        """,
        (campaign_id, candidate_id, symbol, timeframe),
    ).fetchone()
    if not job:
        raise ValueError(
            f"no stored job for candidate {candidate_id} on {symbol}/{timeframe} in campaign {campaign_id}"
        )
    if job["dataset_id"] is None:
        raise ValueError("confirmation requires an immutable dataset snapshot; this job has none")

    dataset_id = int(job["dataset_id"])
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(
            f"dataset {dataset_id} has no recorded splits, so it has no locked confirmation window. "
            "Datasets snapshotted before Phase E cannot support confirmation."
        )

    payload = dict(job["candidate"])
    parameters = dict(payload.get("parameters") or {})
    blocks = dict(payload.get("blocks") or {})
    fingerprint = freeze_fingerprint(
        candidate_id=candidate_id, dataset_id=dataset_id, parameters=parameters, blocks=blocks
    )
    already = existing_confirmation(conn, fingerprint)
    if already is not None:
        raise ConfirmationAlreadySpentError(
            f"candidate {candidate_id} already spent its confirmation on {already['created_at']} "
            f"(passed={already['passed']})."
        )

    dataset = load_intraday_backtest_dataset(conn, symbol, timeframe, dataset_id=dataset_id)
    candles = filter_rows_to_phase(dataset["candles"], splits, "confirmation")
    features = filter_rows_to_phase(dataset["features"], splits, "confirmation")
    if len(candles) < 80:
        raise ValueError(
            f"confirmation window holds only {len(candles)} bars for {symbol}/{timeframe}; too few to "
            "produce a verdict worth locking in"
        )

    candidate = candidate_from_payload(payload)
    result = evaluate_candidate(candidate, candles, features, {})
    metrics = dict(result.get("metrics") or {})
    readiness = dict(result.get("paper_readiness") or {})
    passed = bool(readiness.get("paper_ready")) and str(result.get("status")) == "promoted"
    ledger = multiple_testing_ledger(conn, campaign_id)

    row = run_confirmation(
        conn,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        parameters=parameters,
        blocks=blocks,
        campaign_id=campaign_id,
        metrics=metrics,
        gate_results={
            "paper_readiness": readiness,
            "status": result.get("status"),
            "failure_reasons": result.get("failure_reasons") or [],
        },
        passed=passed,
        effective_trials=int(ledger["effective_trials"]),
    )
    return {
        "confirmation": row,
        "passed": passed,
        "confirmation_window": {
            "start": splits.confirmation_start.isoformat(),
            "end": splits.confirmation_end.isoformat(),
            "bars": len(candles),
        },
        "effective_trials": ledger["effective_trials"],
        "note": (
            "This candidate's single confirmation run is now spent. Changing its parameters creates a new "
            "hypothesis with its own confirmation slot; it does not reopen this one."
        ),
    }


def confirmation_status(conn: psycopg.Connection, *, campaign_id: int | None = None) -> dict[str, Any]:
    """Which candidates have spent their confirmation, and how it went."""
    ensure_research_split_tables(conn)
    if campaign_id is None:
        rows = conn.execute(
            "SELECT * FROM research_confirmation_runs ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM research_confirmation_runs WHERE campaign_id = %s ORDER BY created_at DESC",
            (campaign_id,),
        ).fetchall()
    runs = [dict(row) for row in rows]
    return {
        "protocol_version": CONFIRMATION_PROTOCOL_VERSION,
        "confirmations_run": len(runs),
        "confirmations_passed": sum(1 for row in runs if row["passed"]),
        "runs": runs,
    }
