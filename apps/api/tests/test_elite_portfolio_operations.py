from __future__ import annotations

from typing import Any

import pytest

from app.services import elite_portfolio_operations as ops
from app.services.elite_portfolio_operations import (
    PortfolioOperationError,
    _available_actions,
    approve_member_external_paper,
    enable_member_paper_execution,
    execution_preflight,
)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


READY = {
    "external": {"id": 7, "broker_account_id": 3, "state": "enabled_observe_only", "symbol": "AMD", "timeframe": "30m", "latest_blockers": []},
    "epoch": {"id": 11},
    "shadow": {"id": 21},
    "portfolio_decision": {"id": 31},
    "sync": {"id": 41, "completed_at": "2026-07-26T14:00:00Z"},
    "reconciliation": {"id": 51, "status": "clean"},
    "halts": [],
    "candle": {"timestamp": "fresh"},
    "adapter": {"id": 61, "version": "1.2.3", "change_class": "compatible_patch"},
    "account": {"id": 3, "provider": "alpaca", "environment": "paper", "account_number_masked": "****1234"},
    "fingerprint": {"matches": True, "detail": "Matches the configuration frozen at approval (abc123…)."},
}


class FakePreflightConnection:
    """Answers only the reads `execution_preflight` performs."""

    def __init__(self, **overrides):
        self.data = {**READY, **overrides}
        self.statements: list[str] = []

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.statements.append(text)
        if "FROM external_paper_deployments WHERE id=" in text:
            row = self.data["external"]
            return FakeResult([row] if row else [])
        if "FROM external_execution_epochs" in text:
            return FakeResult([self.data["epoch"]] if self.data["epoch"] else [])
        if "would_submit=TRUE" in text:
            return FakeResult([self.data["shadow"]] if self.data["shadow"] else [])
        if "FROM portfolio_risk_decisions" in text:
            return FakeResult([self.data["portfolio_decision"]] if self.data["portfolio_decision"] else [])
        if "FROM broker_accounts" in text:
            return FakeResult([self.data["account"]] if self.data["account"] else [])
        if "FROM broker_adapter_releases" in text:
            return FakeResult([self.data["adapter"]] if self.data["adapter"] else [])
        if "FROM broker_sync_runs" in text:
            return FakeResult([self.data["sync"]] if self.data["sync"] else [])
        if "FROM broker_reconciliation_runs" in text:
            return FakeResult([self.data["reconciliation"]] if self.data["reconciliation"] else [])
        if "FROM execution_halts" in text:
            return FakeResult(list(self.data["halts"]))
        if "FROM candles" in text:
            return FakeResult([self.data["candle"]] if self.data["candle"] else [])
        return FakeResult([])

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture(autouse=True)
def _stub_fingerprint(monkeypatch, request):
    """Fingerprint recomputation needs the whole deployment graph; the checks
    that care about it stub it directly."""
    if "no_fingerprint_stub" in request.keywords:
        return
    monkeypatch.setattr(ops, "_fingerprint_state", lambda conn, external: FakePreflightConnection().data["fingerprint"])


@pytest.fixture(autouse=True)
def _all_flags_on(monkeypatch):
    monkeypatch.setattr(
        ops,
        "feature_flags",
        lambda: {
            "broker_sync_enabled": True,
            "broker_reconciliation_enabled": True,
            "broker_shadow_execution_enabled": True,
            "broker_order_submission_enabled": True,
            "external_paper_execution_enabled": True,
            "model_risk_enabled": True,
        },
    )
    monkeypatch.setattr(ops, "bar_is_complete", lambda timestamp, timeframe: timestamp == "fresh")


def codes(report):
    return {row["code"]: row["passed"] for row in report["checks"]}


def test_a_fully_ready_deployment_passes_every_preflight_check() -> None:
    report = execution_preflight(FakePreflightConnection(), 7)

    assert report["passed"] is True
    assert report["outstanding"] == []
    assert report["next_action"] == "Enable Alpaca Paper execution"
    assert all(codes(report).values())
    assert report["live_money_supported"] is False


def test_preflight_names_a_dirty_reconciliation_instead_of_just_failing() -> None:
    report = execution_preflight(FakePreflightConnection(reconciliation={"id": 51, "status": "mismatched"}), 7)

    assert report["passed"] is False
    assert codes(report)["RECONCILIATION_CLEAN"] is False
    assert "mismatched" in next(row["detail"] for row in report["checks"] if row["code"] == "RECONCILIATION_CLEAN")


def test_preflight_reports_a_missing_would_submit_decision_as_an_automatic_wait() -> None:
    report = execution_preflight(FakePreflightConnection(shadow=None), 7)

    assert codes(report)["WOULD_SUBMIT_DECISION"] is False
    detail = next(row["detail"] for row in report["checks"] if row["code"] == "WOULD_SUBMIT_DECISION")
    # Framed as a transition check the runner satisfies on its own, not as a
    # separate workspace the operator has to go and drive.
    assert "automatically" in detail


def test_preflight_fails_closed_when_an_execution_halt_is_active() -> None:
    report = execution_preflight(
        FakePreflightConnection(halts=[{"reason_code": "risk_halt", "severity": "critical", "last_seen_at": None}]),
        7,
    )

    assert codes(report)["NO_ACTIVE_HALTS"] is False
    assert "risk_halt" in next(row["detail"] for row in report["checks"] if row["code"] == "NO_ACTIVE_HALTS")


def test_preflight_fails_closed_when_the_execution_flags_are_off(monkeypatch) -> None:
    monkeypatch.setattr(
        ops,
        "feature_flags",
        lambda: {
            "broker_sync_enabled": True,
            "broker_reconciliation_enabled": True,
            "broker_shadow_execution_enabled": True,
            "broker_order_submission_enabled": False,
            "external_paper_execution_enabled": True,
            "model_risk_enabled": True,
        },
    )
    report = execution_preflight(FakePreflightConnection(), 7)

    # Reported separately: when execution is blocked it matters which of the
    # two flags the operator still has to set.
    assert codes(report)["ORDER_SUBMISSION_FLAG"] is False
    assert codes(report)["PAPER_EXECUTION_FLAG"] is True
    assert report["passed"] is False


def test_a_stale_bar_is_not_treated_as_fresh() -> None:
    report = execution_preflight(FakePreflightConnection(candle={"timestamp": "stale"}), 7)

    assert codes(report)["FRESH_COMPLETED_BAR"] is False


def test_an_unknown_external_deployment_is_a_clean_error() -> None:
    with pytest.raises(PortfolioOperationError, match="not found"):
        execution_preflight(FakePreflightConnection(external=None), 7)


# --- Available actions -------------------------------------------------------


def _member(direction="long", capability="external_observe", internal=5, external=None):
    return {
        "id": 1,
        "strategy_direction": direction,
        "execution_capability": capability,
        "internal_deployment_id": internal,
        "external_deployment_id": external,
    }


def test_a_short_member_is_permanently_excluded_from_the_broker_path() -> None:
    actions = _available_actions(_member(direction="short"), None, None)

    assert [row["action"] for row in actions] == ["approve_external_paper"]
    assert actions[0]["enabled"] is False
    assert "structural" in actions[0]["reason"]


def test_an_internal_only_member_is_permanently_excluded_too() -> None:
    actions = _available_actions(_member(capability="internal_only"), None, None)

    assert actions[0]["enabled"] is False


def test_execution_is_offered_only_once_the_preflight_actually_passes() -> None:
    external = {"state": "enabled_observe_only"}
    blocked = _available_actions(_member(external=7), external, {"passed": False, "outstanding": ["NO_ACTIVE_HALTS"]})
    ready = _available_actions(_member(external=7), external, {"passed": True, "outstanding": []})

    by_action = {row["action"]: row for row in blocked}
    assert by_action["enable_paper_execution"]["enabled"] is False
    assert "NO_ACTIVE_HALTS" in by_action["enable_paper_execution"]["reason"]
    assert {row["action"]: row for row in ready}["enable_paper_execution"]["enabled"] is True


def test_approval_is_offered_again_from_a_halted_state() -> None:
    for state in ("readiness_blocked", "manually_halted", "risk_halted", "reconciliation_halted"):
        actions = {row["action"]: row for row in _available_actions(_member(external=7), {"state": state}, None)}
        assert actions["approve_external_paper"]["enabled"] is True, state


def test_approval_is_not_offered_when_already_enabled_for_execution() -> None:
    actions = {row["action"]: row for row in _available_actions(_member(external=7), {"state": "enabled_execution"}, None)}

    assert actions["approve_external_paper"]["enabled"] is False


# --- Guards on the two approval wrappers -------------------------------------


class FakeGuardConnection:
    def __init__(self, member=None, run=None):
        self.member = member
        self.run = run
        self.executed: list[str] = []

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.executed.append(text)
        if "FROM elite_portfolio_members WHERE id=" in text:
            return FakeResult([self.member] if self.member else [])
        if "FROM elite_portfolio_runs WHERE id=" in text:
            return FakeResult([self.run] if self.run else [])
        return FakeResult([])

    def commit(self):
        pass

    def rollback(self):
        pass


def test_external_approval_requires_an_approved_portfolio(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(ops, "enable_observe_only", lambda *a, **k: called.append(a))
    conn = FakeGuardConnection(
        member={"id": 1, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": 5},
        run={"status": "review_ready", "approved_snapshot_hash": None},
    )

    with pytest.raises(PortfolioOperationError, match="approved, internally activated"):
        approve_member_external_paper(conn, 1, 1)
    assert called == []


def test_external_approval_requires_an_internal_deployment_first(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(ops, "enable_observe_only", lambda *a, **k: called.append(a))
    conn = FakeGuardConnection(
        member={"id": 1, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": None},
        run={"status": "approved", "approved_snapshot_hash": "x"},
    )

    with pytest.raises(PortfolioOperationError, match="activate internal deployments"):
        approve_member_external_paper(conn, 1, 1)
    assert called == []


def test_external_approval_refuses_a_short_member(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(ops, "enable_observe_only", lambda *a, **k: called.append(a))
    conn = FakeGuardConnection(
        member={"id": 1, "strategy_direction": "short", "execution_capability": "external_observe", "internal_deployment_id": 5},
        run={"status": "approved", "approved_snapshot_hash": "x"},
    )

    with pytest.raises(PortfolioOperationError, match="no external broker path"):
        approve_member_external_paper(conn, 1, 1)
    assert called == []


def test_enabling_execution_refuses_while_the_preflight_is_outstanding(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(ops, "enable_paper_execution", lambda *a, **k: called.append(a))
    monkeypatch.setattr(
        ops,
        "execution_preflight",
        lambda conn, external_id: {"passed": False, "outstanding": ["NO_ACTIVE_HALTS", "FRESH_COMPLETED_BAR"]},
    )
    conn = FakeGuardConnection(member={"id": 1, "external_deployment_id": 7})

    with pytest.raises(PortfolioOperationError, match="NO_ACTIVE_HALTS"):
        enable_member_paper_execution(conn, 1, 1)
    # The authoritative guards live in enable_paper_execution; this one exists
    # so a refusal explains itself, and it must not have been reached.
    assert called == []


def test_enabling_execution_requires_external_approval_first(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(ops, "enable_paper_execution", lambda *a, **k: called.append(a))
    conn = FakeGuardConnection(member={"id": 1, "external_deployment_id": None})

    with pytest.raises(PortfolioOperationError, match="approve this member"):
        enable_member_paper_execution(conn, 1, 1)
    assert called == []


def test_preflight_covers_every_documented_requirement() -> None:
    report = execution_preflight(FakePreflightConnection(), 7)

    assert {row["code"] for row in report["checks"]} == {
        "PAPER_ACCOUNT_DETECTED",
        "OBSERVE_ONLY_APPROVED",
        "OPEN_EXECUTION_EPOCH",
        "BROKER_SYNC_COMPLETE",
        "RECONCILIATION_CLEAN",
        "ADAPTER_COMPATIBLE",
        "NO_ACTIVE_HALTS",
        "CANDIDATE_FINGERPRINT_MATCH",
        "FRESH_COMPLETED_BAR",
        "WOULD_SUBMIT_DECISION",
        "PORTFOLIO_RISK_APPROVED",
        "ORDER_SUBMISSION_FLAG",
        "PAPER_EXECUTION_FLAG",
    }


def test_an_incompatible_adapter_release_blocks_execution() -> None:
    report = execution_preflight(
        FakePreflightConnection(adapter={"id": 61, "version": "2.0.0", "change_class": "breaking"}),
        7,
    )

    assert codes(report)["ADAPTER_COMPATIBLE"] is False
    assert report["passed"] is False


def test_a_missing_broker_account_blocks_execution() -> None:
    report = execution_preflight(FakePreflightConnection(account=None), 7)

    assert codes(report)["PAPER_ACCOUNT_DETECTED"] is False


@pytest.mark.no_fingerprint_stub
def test_a_deployment_with_no_frozen_configuration_fails_the_fingerprint_check() -> None:
    # "We could not verify the fingerprint" must never read as "the fingerprint
    # is fine": a candidate that changed since approval no longer describes what
    # would actually trade.
    report = execution_preflight(
        FakePreflightConnection(
            external={**READY["external"], "active_configuration_version_id": None, "internal_deployment_id": 5}
        ),
        7,
    )

    assert codes(report)["CANDIDATE_FINGERPRINT_MATCH"] is False
    assert "re-approve" in next(
        row["detail"] for row in report["checks"] if row["code"] == "CANDIDATE_FINGERPRINT_MATCH"
    ).lower()


# --- Bulk approval / execution (All Validated Elites Paper Lab) -------------


class FakeMembersConnection:
    """Answers only the member-listing SELECT the bulk functions issue."""

    def __init__(self, members):
        self.members = members

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        if "FROM elite_portfolio_members WHERE portfolio_run_id=" in text:
            return FakeResult([dict(row) for row in self.members])
        raise AssertionError(f"unexpected query: {text}")


def test_bulk_approve_calls_the_per_member_approval_for_every_eligible_member(monkeypatch) -> None:
    members = [
        {"id": 1, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": 10},
        {"id": 2, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": 11},
    ]
    conn = FakeMembersConnection(members)
    calls: list[int] = []

    def fake_approve(conn_, run_id, member_id, *, actor=None, reapprove=False):
        calls.append(member_id)
        return {"member_id": member_id, "state": "external_approval_required"}

    monkeypatch.setattr(ops, "approve_member_external_paper", fake_approve)

    result = ops.approve_all_members_for_alpaca_paper(conn, 1)

    assert calls == [1, 2]
    assert result["summary"] == {"approved": 2, "skipped": 0, "errors": 0, "total": 2}
    assert result["live_money_supported"] is False


def test_bulk_approve_skips_a_short_member_without_calling_the_per_member_function(monkeypatch) -> None:
    members = [{"id": 1, "strategy_direction": "short", "execution_capability": "internal_only", "internal_deployment_id": 10}]
    conn = FakeMembersConnection(members)
    monkeypatch.setattr(ops, "approve_member_external_paper", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call for a short member")))

    result = ops.approve_all_members_for_alpaca_paper(conn, 1)

    assert result["summary"] == {"approved": 0, "skipped": 1, "errors": 0, "total": 1}
    assert "no external broker path" in result["skipped"][0]["reason"]


def test_bulk_approve_skips_a_member_not_yet_internally_activated(monkeypatch) -> None:
    members = [{"id": 1, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": None}]
    conn = FakeMembersConnection(members)
    monkeypatch.setattr(ops, "approve_member_external_paper", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call before internal activation")))

    result = ops.approve_all_members_for_alpaca_paper(conn, 1)

    assert result["summary"]["skipped"] == 1
    assert "not activated" in result["skipped"][0]["reason"]


def test_bulk_approve_treats_an_already_approved_member_as_skipped_not_an_error(monkeypatch) -> None:
    # The common case on a retried bulk call: most members are already
    # approved from the first attempt. That must read as progress, not failure.
    members = [{"id": 1, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": 10}]
    conn = FakeMembersConnection(members)

    def raise_already(conn_, run_id, member_id, *, actor=None, reapprove=False):
        raise ValueError("deployment is already enabled or approved")

    monkeypatch.setattr(ops, "approve_member_external_paper", raise_already)

    result = ops.approve_all_members_for_alpaca_paper(conn, 1)

    assert result["summary"] == {"approved": 0, "skipped": 1, "errors": 0, "total": 1}


def test_bulk_approve_records_a_genuine_error_without_stopping_the_batch(monkeypatch) -> None:
    members = [
        {"id": 1, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": 10},
        {"id": 2, "strategy_direction": "long", "execution_capability": "external_observe", "internal_deployment_id": 11},
    ]
    conn = FakeMembersConnection(members)
    calls: list[int] = []

    def fake_approve(conn_, run_id, member_id, *, actor=None, reapprove=False):
        calls.append(member_id)
        if member_id == 1:
            raise PortfolioOperationError("boom")
        return {"member_id": member_id}

    monkeypatch.setattr(ops, "approve_member_external_paper", fake_approve)

    result = ops.approve_all_members_for_alpaca_paper(conn, 1)

    # Member 2 is still processed even though member 1 failed.
    assert calls == [1, 2]
    assert result["summary"] == {"approved": 1, "skipped": 0, "errors": 1, "total": 2}
    assert result["errors"][0]["member_id"] == 1


def test_bulk_enable_execution_only_calls_through_for_members_with_a_passing_preflight(monkeypatch) -> None:
    members = [{"id": 1, "external_deployment_id": 100}, {"id": 2, "external_deployment_id": 200}]
    conn = FakeMembersConnection(members)
    monkeypatch.setattr(
        ops,
        "execution_preflight",
        lambda conn_, external_id: {"passed": external_id == 100, "outstanding": [] if external_id == 100 else ["NO_ACTIVE_HALTS"]},
    )
    calls: list[int] = []

    def fake_enable(conn_, run_id, member_id, *, actor=None):
        calls.append(member_id)
        return {"member_id": member_id}

    monkeypatch.setattr(ops, "enable_member_paper_execution", fake_enable)

    result = ops.enable_all_ready_members_paper_execution(conn, 1)

    assert calls == [1]
    assert result["summary"] == {"enabled": 1, "blocked": 1, "errors": 0, "total": 2}
    assert "NO_ACTIVE_HALTS" in result["blocked"][0]["reason"]


def test_bulk_enable_execution_blocks_a_member_never_approved_for_alpaca_paper(monkeypatch) -> None:
    members = [{"id": 1, "external_deployment_id": None}]
    conn = FakeMembersConnection(members)
    monkeypatch.setattr(ops, "execution_preflight", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not check preflight with no external deployment")))

    result = ops.enable_all_ready_members_paper_execution(conn, 1)

    assert result["summary"] == {"enabled": 0, "blocked": 1, "errors": 0, "total": 1}
    assert "not approved" in result["blocked"][0]["reason"]


def test_bulk_enable_execution_never_partially_enables_a_blocked_member(monkeypatch) -> None:
    members = [{"id": 1, "external_deployment_id": 100}]
    conn = FakeMembersConnection(members)
    monkeypatch.setattr(ops, "execution_preflight", lambda conn_, external_id: {"passed": False, "outstanding": ["FRESH_COMPLETED_BAR"]})
    monkeypatch.setattr(ops, "enable_member_paper_execution", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not enable a blocked member")))

    result = ops.enable_all_ready_members_paper_execution(conn, 1)

    assert result["summary"] == {"enabled": 0, "blocked": 1, "errors": 0, "total": 1}


def test_bulk_enable_execution_records_a_genuine_error_without_stopping_the_batch(monkeypatch) -> None:
    members = [{"id": 1, "external_deployment_id": 100}, {"id": 2, "external_deployment_id": 200}]
    conn = FakeMembersConnection(members)
    monkeypatch.setattr(ops, "execution_preflight", lambda conn_, external_id: {"passed": True, "outstanding": []})

    def fake_enable(conn_, run_id, member_id, *, actor=None):
        if member_id == 1:
            raise PortfolioOperationError("boom")
        return {"member_id": member_id}

    monkeypatch.setattr(ops, "enable_member_paper_execution", fake_enable)

    result = ops.enable_all_ready_members_paper_execution(conn, 1)

    assert result["summary"] == {"enabled": 1, "blocked": 0, "errors": 1, "total": 2}
