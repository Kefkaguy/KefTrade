"""Phase E: nested splits, the access ledger, and the locked confirmation."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.research_splits import (
    CONFIRMATION_PROTOCOL_VERSION,
    PHASES,
    VALIDATION_REUSE_WARNING_THRESHOLD,
    ConfirmationAlreadySpentError,
    compute_nested_splits,
    compute_session_nested_splits,
    confirmation_status,
    filter_rows_to_phase,
    freeze_fingerprint,
    get_dataset_splits,
    multiple_testing_ledger,
    persist_dataset_splits,
    record_split_access,
    run_confirmation,
    split_usage_summary,
)

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def _timestamps(count=100):
    return [START + timedelta(minutes=30 * index) for index in range(count)]


def _rows(count=100):
    return [{"timestamp": timestamp, "close": 100.0} for timestamp in _timestamps(count)]


# ---------------------------------------------------------------------------
# Split geometry
# ---------------------------------------------------------------------------

def test_the_three_windows_are_contiguous_and_never_overlap():
    splits = compute_nested_splits(_timestamps())

    assert splits.discovery_end < splits.validation_start
    assert splits.validation_end < splits.confirmation_start
    assert splits.discovery_start == _timestamps()[0]
    assert splits.confirmation_end == _timestamps()[-1]


def test_intraday_split_never_cuts_a_session_in_half():
    observations = []
    for session in range(10):
        session_date = (START + timedelta(days=session)).date()
        for bar in range(13):
            observations.append(
                (START + timedelta(days=session, minutes=30 * bar), session_date)
            )

    splits = compute_session_nested_splits(observations)

    phases_by_session = {}
    for timestamp, session_date in observations:
        phases_by_session.setdefault(session_date, set()).add(splits.phase_for(timestamp))
    assert all(len(phases) == 1 for phases in phases_by_session.values())


def test_every_bar_belongs_to_exactly_one_phase():
    timestamps = _timestamps()
    splits = compute_nested_splits(timestamps)

    phases = [splits.phase_for(timestamp) for timestamp in timestamps]

    assert set(phases) == set(PHASES)
    assert None not in phases


def test_the_split_is_chronological_not_random():
    """A random split would let a strategy learn from bars that come after the
    ones it is tested on -- lookahead wearing a disguise."""
    timestamps = _timestamps()
    splits = compute_nested_splits(timestamps)

    discovery = [t for t in timestamps if splits.phase_for(t) == "discovery"]
    validation = [t for t in timestamps if splits.phase_for(t) == "validation"]
    confirmation = [t for t in timestamps if splits.phase_for(t) == "confirmation"]

    assert max(discovery) < min(validation)
    assert max(validation) < min(confirmation)


def test_window_sizes_follow_the_configured_ratios():
    timestamps = _timestamps(1000)
    splits = compute_nested_splits(timestamps, discovery_ratio=0.5, validation_ratio=0.3)

    counts = {phase: 0 for phase in PHASES}
    for timestamp in timestamps:
        counts[splits.phase_for(timestamp)] += 1

    assert counts["discovery"] == pytest.approx(500, abs=5)
    assert counts["validation"] == pytest.approx(300, abs=5)
    assert counts["confirmation"] == pytest.approx(200, abs=5)


def test_boundaries_come_from_evidence_not_elapsed_time():
    """An irregular calendar must still yield proportional amounts of
    evidence, not proportional amounts of wall-clock time."""
    dense = [START + timedelta(minutes=30 * index) for index in range(60)]
    sparse = [START + timedelta(days=30 * index) for index in range(1, 41)]
    splits = compute_nested_splits(dense + sparse)

    counts = {phase: 0 for phase in PHASES}
    for timestamp in dense + sparse:
        counts[splits.phase_for(timestamp)] += 1

    assert counts["confirmation"] == pytest.approx(20, abs=2)


def test_ratios_must_leave_a_confirmation_window():
    with pytest.raises(ValueError, match="confirmation window"):
        compute_nested_splits(_timestamps(), discovery_ratio=0.8, validation_ratio=0.2)


def test_a_range_too_short_to_split_three_ways_is_rejected():
    with pytest.raises(ValueError, match="three windows"):
        compute_nested_splits(_timestamps(2))


# ---------------------------------------------------------------------------
# Phase filtering
# ---------------------------------------------------------------------------

def test_filtering_makes_the_confirmation_window_structurally_unreachable():
    """Research backtests cannot peek at confirmation because the bars are
    simply not in the list they are handed."""
    rows = _rows()
    splits = compute_nested_splits([row["timestamp"] for row in rows])

    research_rows = filter_rows_to_phase(rows, splits, "validation")

    assert research_rows
    assert all(row["timestamp"] < splits.confirmation_start for row in research_rows)


def test_each_phase_filter_returns_only_that_phase():
    rows = _rows()
    splits = compute_nested_splits([row["timestamp"] for row in rows])

    totals = sum(len(filter_rows_to_phase(rows, splits, phase)) for phase in PHASES)

    assert totals == len(rows)


def test_an_unknown_phase_is_rejected():
    rows = _rows()
    splits = compute_nested_splits([row["timestamp"] for row in rows])

    with pytest.raises(ValueError, match="unknown phase"):
        filter_rows_to_phase(rows, splits, "test")


# ---------------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------------

class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeSplitConn:
    def __init__(self, *, variants=10, symbols=5, jobs=50, families=2):
        self.splits: dict[int, dict] = {}
        self.access: list[dict] = []
        self.confirmations: dict[str, dict] = {}
        self.counts = {"variants": variants, "symbols": symbols, "jobs": jobs, "families": families}
        self._next_id = 1

    def execute(self, query, params=None):
        q = " ".join(query.split())
        params = params or ()

        if q.startswith("CREATE TABLE") or q.startswith("CREATE INDEX"):
            return FakeResult([])

        if q.startswith("INSERT INTO research_dataset_splits"):
            dataset_id = params[0]
            if dataset_id in self.splits:
                return FakeResult([])
            row = {
                "id": self._take_id(),
                "dataset_id": dataset_id,
                "discovery_start": params[1],
                "discovery_end": params[2],
                "validation_start": params[3],
                "validation_end": params[4],
                "confirmation_start": params[5],
                "confirmation_end": params[6],
                "split_version": params[7],
            }
            self.splits[dataset_id] = row
            return FakeResult([row])

        if q.startswith("SELECT * FROM research_dataset_splits"):
            row = self.splits.get(params[0])
            return FakeResult([row] if row else [])

        if q.startswith("INSERT INTO research_split_access_log"):
            self.access.append(
                {
                    "dataset_id": params[0],
                    "phase": params[1],
                    "decision_type": params[2],
                    "campaign_id": params[3],
                    "candidate_id": params[4],
                    "detail": params[5].obj,
                }
            )
            return FakeResult([])

        if q.startswith("SELECT phase, decision_type"):
            grouped: dict[tuple[str, str], int] = {}
            for row in self.access:
                if row["dataset_id"] == params[0]:
                    key = (row["phase"], row["decision_type"])
                    grouped[key] = grouped.get(key, 0) + 1
            return FakeResult(
                [
                    {"phase": phase, "decision_type": decision, "uses": uses}
                    for (phase, decision), uses in sorted(grouped.items())
                ]
            )

        if q.startswith("SELECT COUNT(DISTINCT candidate_id)"):
            return FakeResult(
                [
                    {
                        "variants": self.counts["variants"],
                        "families": self.counts["families"],
                        "jobs": self.counts["jobs"],
                        "symbols": self.counts["symbols"],
                    }
                ]
            )

        if q.startswith("SELECT COUNT(*) FILTER"):
            return FakeResult([{"descendants": 4, "distinct_parents": 2, "deepest_generation": 3}])

        if q.startswith("SELECT * FROM research_confirmation_runs WHERE frozen_fingerprint"):
            row = self.confirmations.get(params[0])
            return FakeResult([row] if row else [])

        if q.startswith("INSERT INTO research_confirmation_runs"):
            row = {
                "id": self._take_id(),
                "frozen_fingerprint": params[0],
                "campaign_id": params[1],
                "candidate_id": params[2],
                "dataset_id": params[3],
                "frozen_spec": params[4].obj,
                "metrics": params[5].obj,
                "gate_results": params[6].obj,
                "passed": params[7],
                "effective_trials": params[8],
                "protocol_version": params[9],
                "created_at": datetime(2026, 7, 27, tzinfo=UTC),
            }
            self.confirmations[params[0]] = row
            return FakeResult([row])

        if q.startswith("SELECT * FROM research_confirmation_runs WHERE campaign_id"):
            return FakeResult([r for r in self.confirmations.values() if r["campaign_id"] == params[0]])

        if q.startswith("SELECT * FROM research_confirmation_runs"):
            return FakeResult(list(self.confirmations.values()))

        raise AssertionError(f"unexpected query: {q[:70]}")

    def _take_id(self):
        value = self._next_id
        self._next_id += 1
        return value


# ---------------------------------------------------------------------------
# Persistence and immutability
# ---------------------------------------------------------------------------

def test_split_boundaries_cannot_be_redrawn_after_the_fact():
    """Boundaries that moved after seeing results would let a disappointing
    confirmation window be redrawn until it cooperated."""
    conn = FakeSplitConn()
    first = compute_nested_splits(_timestamps())
    second = compute_nested_splits(_timestamps(), discovery_ratio=0.2, validation_ratio=0.2)

    persist_dataset_splits(conn, dataset_id=7, splits=first)
    stored = persist_dataset_splits(conn, dataset_id=7, splits=second)

    assert stored["confirmation_start"] == first.confirmation_start
    assert stored["confirmation_start"] != second.confirmation_start


def test_stored_splits_round_trip():
    conn = FakeSplitConn()
    splits = compute_nested_splits(_timestamps())
    persist_dataset_splits(conn, dataset_id=7, splits=splits)

    loaded = get_dataset_splits(conn, 7)

    assert loaded is not None
    assert loaded.validation_start == splits.validation_start


def test_an_unsplit_dataset_returns_none():
    assert get_dataset_splits(FakeSplitConn(), 999) is None


# ---------------------------------------------------------------------------
# The access ledger
# ---------------------------------------------------------------------------

def test_each_look_at_validation_is_counted():
    conn = FakeSplitConn()
    for _ in range(3):
        record_split_access(conn, dataset_id=7, phase="validation", decision_type="family_screen")

    summary = split_usage_summary(conn, 7)

    assert summary["uses_by_phase"]["validation"] == 3
    assert summary["uses_by_phase"]["confirmation"] == 0


def test_heavy_validation_reuse_is_reported_as_training():
    """Selecting the best of many variants on validation fits validation just
    as surely as gradient descent would; the search merely happened in the
    researcher's head."""
    conn = FakeSplitConn()
    for _ in range(VALIDATION_REUSE_WARNING_THRESHOLD):
        record_split_access(conn, dataset_id=7, phase="validation", decision_type="candidate_selection")

    summary = split_usage_summary(conn, 7)

    assert summary["validation_is_effectively_training"] is True


def test_light_validation_use_is_not_flagged():
    conn = FakeSplitConn()
    record_split_access(conn, dataset_id=7, phase="validation", decision_type="family_screen")

    assert split_usage_summary(conn, 7)["validation_is_effectively_training"] is False


def test_the_ledger_rejects_an_unknown_phase():
    with pytest.raises(ValueError, match="unknown phase"):
        record_split_access(FakeSplitConn(), dataset_id=7, phase="train", decision_type="x")


# ---------------------------------------------------------------------------
# Multiple-testing accounting
# ---------------------------------------------------------------------------

def test_effective_trials_counts_every_variant_symbol_evaluation():
    """The variant count alone understates the search: each (variant, symbol)
    pair is an independent chance for something to look good by luck."""
    conn = FakeSplitConn(variants=10, symbols=5)

    ledger = multiple_testing_ledger(conn, 101)

    assert ledger["variants_tested"] == 10
    assert ledger["symbols_tested"] == 5
    assert ledger["effective_trials"] == 50


def test_the_ledger_reports_lineage():
    ledger = multiple_testing_ledger(FakeSplitConn(), 101)

    assert ledger["lineage"]["distinct_parents"] == 2
    assert ledger["lineage"]["deepest_generation"] == 3


def test_effective_trials_feeds_the_deflated_sharpe():
    """The two halves of the multiple-testing story must actually connect."""
    from app.services.null_models import deflated_sharpe_ratio

    ledger = multiple_testing_ledger(FakeSplitConn(variants=10, symbols=5), 101)
    returns = [0.01, 0.02, -0.005, 0.015, 0.008, 0.012, -0.002, 0.02] * 4

    naive = deflated_sharpe_ratio(returns, trials=1)
    honest = deflated_sharpe_ratio(returns, trials=ledger["effective_trials"])

    assert honest["deflated_sharpe"] < naive["deflated_sharpe"]


# ---------------------------------------------------------------------------
# The locked confirmation protocol
# ---------------------------------------------------------------------------

def test_the_same_frozen_specification_hashes_the_same():
    first = freeze_fingerprint(candidate_id="c1", dataset_id=7, parameters={"a": 1, "b": 2})
    second = freeze_fingerprint(candidate_id="c1", dataset_id=7, parameters={"b": 2, "a": 1})

    assert first == second


def test_changing_a_parameter_is_a_different_hypothesis():
    first = freeze_fingerprint(candidate_id="c1", dataset_id=7, parameters={"a": 1})
    second = freeze_fingerprint(candidate_id="c1", dataset_id=7, parameters={"a": 2})

    assert first != second


def _confirm(conn, **overrides):
    payload = {
        "candidate_id": "c1",
        "dataset_id": 7,
        "parameters": {"threshold": 1},
        "campaign_id": 101,
        "metrics": {"profit_factor": 1.4},
        "gate_results": {"paper_ready": True},
        "passed": True,
        "effective_trials": 50,
    }
    payload.update(overrides)
    return run_confirmation(conn, **payload)


def test_a_frozen_candidate_gets_exactly_one_confirmation():
    """A confirmation you may re-run until it passes is a validation set with
    extra steps. The refusal is the whole value of the protocol."""
    conn = FakeSplitConn()
    _confirm(conn)

    with pytest.raises(ConfirmationAlreadySpentError, match="already confirmed"):
        _confirm(conn)


def test_a_failed_confirmation_is_still_spent():
    """Especially a failed one -- retrying after a failure is the exact abuse
    the lock exists to prevent."""
    conn = FakeSplitConn()
    _confirm(conn, passed=False)

    with pytest.raises(ConfirmationAlreadySpentError):
        _confirm(conn, passed=True)


def test_changing_the_frozen_parameters_earns_a_new_confirmation_slot():
    conn = FakeSplitConn()
    _confirm(conn, parameters={"threshold": 1})

    second = _confirm(conn, parameters={"threshold": 2})

    assert second["passed"] is True
    assert len(conn.confirmations) == 2


def test_confirmation_records_that_the_locked_window_was_spent():
    conn = FakeSplitConn()
    _confirm(conn)

    summary = split_usage_summary(conn, 7)

    assert summary["uses_by_phase"]["confirmation"] == 1
    assert summary["confirmation_is_spent"] is True


def test_confirmation_stores_the_trial_count_it_was_judged_against():
    conn = FakeSplitConn()
    row = _confirm(conn, effective_trials=50)

    assert row["effective_trials"] == 50
    assert row["protocol_version"] == CONFIRMATION_PROTOCOL_VERSION


def test_splits_are_fixed_when_the_dataset_is_snapshotted():
    """Boundaries are chosen before any research has run against the data.
    Choosing them later, once results are known, would let them be nudged
    until the confirmation window cooperated."""
    from app.services.labs.intraday.dataset_snapshot import _ensure_nested_splits

    conn = FakeSplitConn()
    timestamps = _timestamps(120)

    original_execute = conn.execute

    def execute(query, params=None):
        # Whitespace-collapsed so a reformatted query still routes here: the
        # split source gained a timeframe filter and therefore a line break.
        collapsed = " ".join(query.split())
        if "SELECT DISTINCT timestamp, session_date" in collapsed:
            return FakeResult([])
        if "SELECT DISTINCT timestamp FROM research_dataset_candles" in collapsed:
            return FakeResult([{"timestamp": timestamp} for timestamp in timestamps])
        return original_execute(query, params)

    conn.execute = execute
    _ensure_nested_splits(conn, 7)

    stored = get_dataset_splits(conn, 7)
    assert stored is not None
    assert stored.discovery_start == timestamps[0]
    assert stored.confirmation_end == timestamps[-1]


def test_a_dataset_too_small_to_split_is_left_unsplit_rather_than_faked():
    from app.services.labs.intraday.dataset_snapshot import _ensure_nested_splits

    conn = FakeSplitConn()
    original_execute = conn.execute

    def execute(query, params=None):
        # Whitespace-collapsed so a reformatted query still routes here: the
        # split source gained a timeframe filter and therefore a line break.
        collapsed = " ".join(query.split())
        if "SELECT DISTINCT timestamp, session_date" in collapsed:
            return FakeResult([])
        if "SELECT DISTINCT timestamp FROM research_dataset_candles" in collapsed:
            return FakeResult([{"timestamp": START}])
        return original_execute(query, params)

    conn.execute = execute
    _ensure_nested_splits(conn, 7)

    assert get_dataset_splits(conn, 7) is None


def test_confirmation_status_summarizes_outcomes():
    conn = FakeSplitConn()
    _confirm(conn, parameters={"threshold": 1}, passed=True)
    _confirm(conn, parameters={"threshold": 2}, passed=False)

    status = confirmation_status(conn, campaign_id=101)

    assert status["confirmations_run"] == 2
    assert status["confirmations_passed"] == 1
