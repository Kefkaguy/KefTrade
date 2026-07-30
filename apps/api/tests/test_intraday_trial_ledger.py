from datetime import UTC, datetime

import pytest

from app.services.intraday_trial_ledger import (
    assert_declared,
    declaration_fingerprint,
    declare_trials,
    effective_trials_for_run,
    trial_fingerprint,
)


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class LedgerConn:
    """Minimal stand-in for the append-only ledger tables."""

    def __init__(self, *, historical=0, declared=0, recorded_fingerprints=()):
        self.historical = historical
        self.declared = declared
        self.recorded = set(recorded_fingerprints)
        self.inserted = []
        self.committed = False

    def execute(self, query, params=None):
        text = " ".join(query.split())
        if "COUNT(DISTINCT trial_fingerprint)" in text:
            return Result({"trials": self.historical})
        if "jsonb_array_elements_text(declared_factor_keys)" in text:
            return Result({"trials": self.declared})
        if "SELECT DISTINCT trial_fingerprint" in text:
            requested = set(params[0])
            return Result(
                rows=[
                    {"trial_fingerprint": item}
                    for item in requested & self.recorded
                ]
            )
        if text.startswith("SELECT * FROM intraday_research_trial_declarations"):
            return Result(None)
        if text.startswith("INSERT INTO intraday_research_trial_declarations"):
            self.inserted.append(params)
            return Result(
                {
                    "id": 1,
                    "declaration_fingerprint": params[0],
                    "purpose": params[1],
                    "timeframe": params[2],
                    "declared_factor_keys": params[4].obj,
                    "declared_test_count": params[5],
                    "created_at": datetime(2026, 7, 30, tzinfo=UTC),
                }
            )
        return Result(None)

    def commit(self):
        self.committed = True


def test_declaration_fingerprint_ignores_the_order_of_the_test_list():
    left = declaration_fingerprint(
        purpose="gap", timeframe="30m", factor_keys=["a", "b"], hypothesis=None
    )
    right = declaration_fingerprint(
        purpose="gap", timeframe="30m", factor_keys=["b", "a"], hypothesis=None
    )
    other = declaration_fingerprint(
        purpose="gap", timeframe="30m", factor_keys=["a", "c"], hypothesis=None
    )

    assert left == right
    assert left != other


def test_declaring_records_the_test_count():
    conn = LedgerConn()

    declaration = declare_trials(
        conn,
        purpose="bounded gap experiment",
        timeframe="30m",
        factor_keys=["gap_down_acceptance_continuation", "gap_down_absorption_reversal"],
        protocol_version="test",
    )

    assert declaration["declared_test_count"] == 2
    assert declaration["already_declared"] is False
    assert conn.committed is True


def test_declaring_no_test_is_refused():
    with pytest.raises(ValueError):
        declare_trials(
            LedgerConn(),
            purpose="empty",
            timeframe="30m",
            factor_keys=[],
            protocol_version="test",
        )


def test_an_undeclared_factor_cannot_be_scored():
    declaration = {
        "id": 3,
        "timeframe": "30m",
        "declared_factor_keys": ["gap_down_acceptance_continuation"],
        "declared_test_count": 1,
    }

    with pytest.raises(ValueError) as error:
        assert_declared(
            declaration,
            timeframe="30m",
            factor_keys=[
                "gap_down_acceptance_continuation",
                "gap_up_absorption_reversal",
            ],
        )

    assert "gap_up_absorption_reversal" in str(error.value)


def test_a_declaration_for_another_timeframe_is_refused():
    declaration = {
        "id": 3,
        "timeframe": "15m",
        "declared_factor_keys": ["gap_down_acceptance_continuation"],
        "declared_test_count": 1,
    }

    with pytest.raises(ValueError):
        assert_declared(
            declaration,
            timeframe="30m",
            factor_keys=["gap_down_acceptance_continuation"],
        )


def test_declared_but_untested_factors_are_reported():
    declaration = {
        "id": 3,
        "timeframe": "30m",
        "declared_factor_keys": ["a", "b", "c"],
        "declared_test_count": 3,
        "created_at": datetime(2026, 7, 30, tzinfo=UTC),
    }

    check = assert_declared(declaration, timeframe="30m", factor_keys=["a"])

    assert check["declared_but_not_tested"] == ["b", "c"]
    assert check["declared_test_count"] == 3


def test_effective_trials_accumulate_across_runs_rather_than_resetting():
    conn = LedgerConn(historical=30)

    ledger = effective_trials_for_run(
        conn, timeframe="30m", factor_keys=["a", "b"], spec_hash="hash"
    )

    # Two brand-new tests on top of thirty already spent, not two.
    assert ledger["historical_trials"] == 30
    assert ledger["new_trials_in_this_run"] == 2
    assert ledger["effective_trials"] == 32


def test_rerunning_the_identical_trial_does_not_inflate_the_count():
    fingerprints = [
        trial_fingerprint(spec_hash="hash", factor_key=key) for key in ("a", "b")
    ]
    conn = LedgerConn(historical=30, recorded_fingerprints=fingerprints)

    ledger = effective_trials_for_run(
        conn, timeframe="30m", factor_keys=["a", "b"], spec_hash="hash"
    )

    assert ledger["repeat_trials_in_this_run"] == 2
    assert ledger["new_trials_in_this_run"] == 0
    assert ledger["effective_trials"] == 30


def test_declared_but_never_run_tests_still_count_as_trials():
    conn = LedgerConn(historical=2, declared=12)

    ledger = effective_trials_for_run(
        conn, timeframe="30m", factor_keys=["a"], spec_hash="hash"
    )

    # Looking at a hypothesis and choosing not to report it is still a look.
    assert ledger["declared_trials"] == 12
    assert ledger["effective_trials"] == 12


def test_effective_trials_never_fall_below_the_size_of_the_run():
    conn = LedgerConn(historical=0)

    ledger = effective_trials_for_run(
        conn, timeframe="30m", factor_keys=["a", "b", "c"], spec_hash="hash"
    )

    assert ledger["effective_trials"] == 3
