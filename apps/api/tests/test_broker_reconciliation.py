from __future__ import annotations

from decimal import Decimal

from app.services.broker_reconciliation import (
    open_order_is_unexpected,
    position_ownership_findings,
    reconcile_broker_snapshot,
)


def test_attributed_or_execution_owned_open_orders_are_expected() -> None:
    assert not open_order_is_unexpected(
        {"status": "accepted", "strategy_attribution_id": 1}
    )
    assert not open_order_is_unexpected(
        {"status": "partially_filled", "execution_attempt_id": 9}
    )
    assert open_order_is_unexpected({"status": "accepted"})
    assert not open_order_is_unexpected({"status": "filled"})


class _Result:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = list(many or [])

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _AlreadyReconciledConnection:
    def __init__(self):
        self.queries: list[str] = []
        self.commits = 0

    def execute(self, query, params=()):
        self.queries.append(query)
        if "FROM broker_sync_runs WHERE id" in query:
            return _Result(
                one={"id": 61969, "status": "complete", "broker_account_id": 2}
            )
        if "FROM broker_reconciliation_runs WHERE sync_run_id" in query:
            return _Result(
                one={
                    "id": 60754,
                    "sync_run_id": 61969,
                    "broker_account_id": 2,
                    "status": "findings",
                    "trace_id": "f845ac25-2bd6-469e-9763-ffa64967a05f",
                }
            )
        if "FROM broker_reconciliation_findings" in query:
            return _Result(many=[])
        raise AssertionError(f"unexpected query: {query}")

    def commit(self):
        self.commits += 1


def test_reconcile_is_idempotent_for_an_already_reconciled_sync() -> None:
    conn = _AlreadyReconciledConnection()

    result = reconcile_broker_snapshot(conn, 61969)

    assert result["id"] == 60754
    assert result["status"] == "findings"
    assert result["idempotent_replay"] is True
    assert conn.commits == 1
    assert not any(
        "INSERT INTO broker_reconciliation_runs" in query for query in conn.queries
    )


def test_fully_owned_broker_position_is_not_unexpected() -> None:
    findings = position_ownership_findings(
        [{"symbol": "SPY", "quantity": Decimal("1.311531096"), "market_value": 1014}],
        [{"symbol": "SPY", "owned_quantity": Decimal("1.311531096")}],
        broker_account_id=2,
    )

    assert findings == []


def test_ownership_is_aggregated_across_strategies() -> None:
    findings = position_ownership_findings(
        [{"symbol": "SPY", "quantity": "2.5", "market_value": "1900"}],
        [
            {"symbol": "SPY", "quantity": "1.0"},
            {"symbol": "spy", "quantity": "1.5"},
        ],
        broker_account_id=2,
    )

    assert findings == []


def test_only_unexplained_excess_is_reported() -> None:
    findings = position_ownership_findings(
        [{"symbol": "SPY", "quantity": "2.0", "market_value": "1524.92"}],
        [{"symbol": "SPY", "owned_quantity": "1.311531096"}],
        broker_account_id=2,
    )

    assert len(findings) == 1
    assert findings[0]["finding_key"] == "unexpected_position:SPY"
    assert findings[0]["details"] == {
        "symbol": "SPY",
        "broker_quantity": "2.000000000",
        "owned_quantity": "1.311531096",
        "unexplained_quantity": "0.688468904",
        "mismatch_kind": "unexpected_broker_excess",
        "market_value": "1524.92",
    }


def test_owned_quantity_without_broker_position_is_a_mismatch() -> None:
    findings = position_ownership_findings(
        [], [{"symbol": "SPY", "owned_quantity": "1.0"}], broker_account_id=2
    )

    assert findings[0]["details"]["unexplained_quantity"] == "-1.000000000"
    assert (
        findings[0]["details"]["mismatch_kind"] == "strategy_ownership_exceeds_broker"
    )


def test_negative_broker_position_can_never_be_explained_by_long_ownership() -> None:
    findings = position_ownership_findings(
        [{"symbol": "SPY", "quantity": "-1", "market_value": "762"}],
        [],
        broker_account_id=2,
    )

    assert findings[0]["details"]["broker_quantity"] == "-1.000000000"
    assert (
        findings[0]["details"]["mismatch_kind"] == "strategy_ownership_exceeds_broker"
    )
