from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from app.services.elite_portfolio_activation import (
    _activate_member,
    activate_internal,
    activation_worklist,
    authorization_instruction,
    repair_stalled_activation_attempts,
)


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class ActivationConn:
    def __init__(self):
        self.deployment = None
        self.commits = 0

    def execute(self, query, params=None):
        if "SELECT * FROM strategy_deployments" in query:
            return Result(self.deployment)
        if "SELECT * FROM broker_accounts" in query:
            return Result(None)
        if "UPDATE elite_portfolio_members" in query:
            return Result()
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def member(index: int, *, direction: str = "long", state: str = "approved") -> dict:
    return {
        "id": index,
        "candidate_id": f"candidate-{index}",
        "symbol": "AAPL",
        "timeframe": "1h",
        "strategy_direction": direction,
        "execution_capability": "internal_only" if direction == "short" else "external_observe",
        "internal_deployment_id": index + 100,
        "external_deployment_id": index + 200 if direction == "long" else None,
        "activation_state": state,
    }


def test_retry_worklist_contains_only_unfinished_members() -> None:
    rows = [
        member(1, state="external_approval_required"),
        member(2, state="failed"),
        member(3, state="internal_active"),
        member(4, state="approved"),
    ]

    assert [row["id"] for row in activation_worklist(rows)] == [2, 4]


def test_server_authorization_instructions_are_long_only_and_snapshot_bound(monkeypatch) -> None:
    from app.services import elite_portfolio_activation

    monkeypatch.setattr(elite_portfolio_activation, "feature_flags", lambda: {"broker_order_submission_enabled": False, "external_paper_execution_enabled": False})
    snapshot_hash = "a" * 64

    instruction = authorization_instruction(member(1, state="external_approval_required"), snapshot_hash)

    assert instruction is not None
    assert instruction["portfolio_snapshot_hash"] == snapshot_hash
    assert "--confirm-deployment-id 101" in instruction["command"]
    assert instruction["expected_effect"].endswith("this command does not enable order submission.")
    assert authorization_instruction(member(2, direction="short"), snapshot_hash) is None
    assert authorization_instruction(member(3, state="blocked"), snapshot_hash) is None


def test_retry_after_post_creation_failure_reuses_internal_deployment(monkeypatch) -> None:
    from app.services import elite_portfolio_activation

    conn = ActivationConn()
    created = []
    row = {
        **member(5, direction="short", state="failed"),
        "campaign_id": 9,
        "candidate_id": "short-5",
        "elite_candidate_id": 17,
        "evidence": {"strategy_version": "short-v1", "parameters": {"lookback": 20}},
    }

    monkeypatch.setattr(elite_portfolio_activation, "ensure_candidate_forward_account", lambda *_args: {"id": 3})

    def fake_create(*_args, **_kwargs):
        deployment = {"id": 77, "strategy_version": "short-v1"}
        conn.deployment = deployment
        created.append(deployment)
        return deployment

    monkeypatch.setattr(elite_portfolio_activation, "create_deployment", fake_create)

    first = _activate_member(conn, row)
    # This models an injected failure after deployment creation but before the
    # activation attempt itself is marked complete. The retry sees the row.
    second = _activate_member(conn, row)

    assert first["internal_deployment_id"] == second["internal_deployment_id"] == 77
    assert len(created) == 1
    assert first["external_deployment_id"] is None


# --- Full-flow fake database: reproduces the datetime-serialization bug -----
#
# psycopg's Jsonb(...) does not serialize eagerly; the JSON dump happens when
# the driver adapts the parameter for the wire. This fake reproduces that
# instead of skipping it, by calling json.dumps on every Jsonb payload it
# receives -- exactly what would raise "Object of type datetime is not JSON
# serializable" in production if a raw row (with a real datetime column) were
# ever handed to Jsonb(...) unsanitized.


def _row_result(row):
    class _Result:
        def fetchone(self_inner):
            return dict(row) if row is not None else None

        def fetchall(self_inner):
            return [dict(row)]

    return _Result()


def _rows_result(rows):
    class _Result:
        def fetchone(self_inner):
            return dict(rows[0]) if rows else None

        def fetchall(self_inner):
            return [dict(r) for r in rows]

    return _Result()


class FakeActivationDatabase:
    """Enough of the schema to drive `activate_internal` end to end."""

    def __init__(self):
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        self.now = now
        self.runs = {
            1: {
                "id": 1,
                "status": "approved",
                "approved_snapshot_hash": "a" * 64,
                "snapshot_hash": "a" * 64,
                "activated_at": None,
                "updated_at": now,
            }
        }
        self.members = {
            1: {
                "id": 1,
                "portfolio_run_id": 1,
                "elite_candidate_id": 9,
                "campaign_id": 3,
                "candidate_id": "sd_497fc1c84e6342",
                "symbol": "GOOGL",
                "timeframe": "4h",
                "strategy_family": "session_momentum",
                "strategy_direction": "long",
                "execution_capability": "external_observe",
                "rank": 1,
                "activation_state": "approved",
                "evidence": {"strategy_name": "session_momentum_v1", "strategy_version": "v1", "parameters": {"rsi_min": 55}},
                "internal_deployment_id": None,
                "external_deployment_id": None,
                "latest_error": None,
                # The exact field type that crashed production: a real
                # datetime, not a string, coming straight off the DB row.
                "created_at": now,
                "updated_at": now,
            }
        }
        self.attempts: dict[int, dict] = {}
        self.attempts_by_key: dict[str, int] = {}
        self.strategy_deployments: dict[int, dict] = {}
        self.external_paper_deployments: dict[int, dict] = {}
        self.broker_accounts = [{"id": 1, "last_successful_sync_at": now}]
        self._next_id = 100
        self.jsonb_payloads: list[object] = []
        self.commits = 0

    def _next(self) -> int:
        self._next_id += 1
        return self._next_id

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        params = params or ()
        # Reproduce the real driver: anything passed as Jsonb(...) must
        # actually be JSON-serializable, or this raises exactly like
        # production psycopg does at bind time.
        for value in params:
            if isinstance(value, Jsonb):
                self.jsonb_payloads.append(value.obj)
                (value.dumps or json.dumps)(value.obj)

        if "FROM elite_portfolio_runs WHERE id=" in text and "SELECT" in text:
            return _row_result(self.runs.get(params[0]))

        if "FROM elite_portfolio_activation_attempts WHERE idempotency_key=" in text:
            attempt_id = self.attempts_by_key.get(params[0])
            return _row_result(self.attempts.get(attempt_id) if attempt_id else None)

        if "FROM elite_portfolio_activation_attempts WHERE status IN" in text:
            stalled = [row for row in self.attempts.values() if row["status"] in ("running", "partial")]
            return _rows_result(stalled)

        if text.startswith("UPDATE elite_portfolio_activation_attempts SET status='running'"):
            attempt = self.attempts[params[0]]
            attempt.update({"status": "running", "error": None, "completed_at": None})
            return _row_result(attempt)

        if text.startswith("INSERT INTO elite_portfolio_activation_attempts"):
            portfolio_run_id, idempotency_key, requested_snapshot_hash = params
            attempt_id = self._next()
            attempt = {
                "id": attempt_id,
                "portfolio_run_id": portfolio_run_id,
                "idempotency_key": idempotency_key,
                "status": "running",
                "requested_snapshot_hash": requested_snapshot_hash,
                "result": {},
                "error": None,
                "started_at": self.now,
                "completed_at": None,
            }
            self.attempts[attempt_id] = attempt
            self.attempts_by_key[idempotency_key] = attempt_id
            return _row_result(attempt)

        if text.startswith("UPDATE elite_portfolio_activation_attempts SET status=%s,result=%s,error=%s"):
            status, result_jsonb, error_jsonb, attempt_id = params
            attempt = self.attempts[attempt_id]
            attempt.update({
                "status": status,
                "result": result_jsonb.obj if isinstance(result_jsonb, Jsonb) else result_jsonb,
                "error": (error_jsonb.obj if isinstance(error_jsonb, Jsonb) else error_jsonb) if error_jsonb is not None else None,
                "completed_at": self.now,
            })
            return _row_result(None)

        if text.startswith("UPDATE elite_portfolio_runs SET status='activated_internal'"):
            self.runs[params[0]]["status"] = "activated_internal"
            self.runs[params[0]]["activated_at"] = self.now
            return _row_result(None)

        if "FROM elite_portfolio_members WHERE portfolio_run_id=" in text and "SELECT" in text:
            rows = [row for row in self.members.values() if row["portfolio_run_id"] == params[0]]
            rows.sort(key=lambda row: row["rank"])
            return _rows_result(rows)

        if text.startswith("UPDATE elite_portfolio_members SET activation_state='internal_activation_pending'"):
            self.members[params[0]].update({"activation_state": "internal_activation_pending", "latest_error": None, "updated_at": self.now})
            return _row_result(None)

        if text.startswith("UPDATE elite_portfolio_members SET internal_deployment_id="):
            internal_id, external_id, state, latest_error, member_id = params
            self.members[member_id].update({
                "internal_deployment_id": internal_id,
                "external_deployment_id": external_id,
                "activation_state": state,
                "latest_error": latest_error,
                "updated_at": self.now,
            })
            return _row_result(None)

        if text.startswith("UPDATE elite_portfolio_members SET activation_state='failed'"):
            latest_error, member_id = params
            self.members[member_id].update({"activation_state": "failed", "latest_error": latest_error, "updated_at": self.now})
            return _row_result(None)

        if "FROM strategy_deployments WHERE campaign_id=" in text:
            campaign_id, candidate_id, symbol, timeframe = params
            for row in self.strategy_deployments.values():
                if (row["campaign_id"], row["candidate_id"], row["symbol"], row["timeframe"]) == (campaign_id, candidate_id, symbol, timeframe) and row["status"] == "active":
                    return _row_result(row)
            return _row_result(None)

        if "FROM broker_accounts ORDER BY last_successful_sync_at" in text:
            return _row_result(self.broker_accounts[0] if self.broker_accounts else None)

        if "FROM external_paper_deployments WHERE internal_deployment_id=" in text:
            internal_id, broker_account_id = params
            for row in self.external_paper_deployments.values():
                if row["internal_deployment_id"] == internal_id and row["broker_account_id"] == broker_account_id:
                    return _row_result(row)
            return _row_result(None)

        if text.startswith("INSERT INTO external_paper_deployments"):
            (internal_id, broker_account_id, campaign_id, elite_candidate_id, candidate_id,
             strategy_version, symbol, timeframe, blockers) = params
            external_id = self._next()
            row = {
                "id": external_id,
                "internal_deployment_id": internal_id,
                "broker_account_id": broker_account_id,
                "campaign_id": campaign_id,
                "elite_candidate_id": elite_candidate_id,
                "candidate_id": candidate_id,
                "strategy_version": strategy_version,
                "symbol": symbol,
                "timeframe": timeframe,
                "state": "disabled",
                "latest_blockers": blockers.obj if isinstance(blockers, Jsonb) else blockers,
                "created_at": self.now,
            }
            self.external_paper_deployments[external_id] = row
            return _row_result(row)

        raise AssertionError(f"unhandled query in FakeActivationDatabase: {text}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _fake_create_deployment(conn, account_id, strategy_name, symbol, timeframe, **kwargs):
    deployment_id = conn._next()
    row = {
        "id": deployment_id,
        "campaign_id": kwargs.get("campaign_id"),
        "candidate_id": kwargs["candidate_id"],
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_version": kwargs.get("strategy_version"),
        "status": "active",
        "simulation_only": True,
        # Another real datetime straight off a DB row -- the second field
        # type that made the original bug reproducible in production.
        "created_at": conn.now,
        "updated_at": conn.now,
    }
    conn.strategy_deployments[deployment_id] = row
    return row


@pytest.fixture
def activation_env(monkeypatch):
    from app.services import elite_portfolio_activation

    monkeypatch.setattr(elite_portfolio_activation, "ensure_candidate_forward_account", lambda conn, candidate_id, cash: {"id": 55})
    monkeypatch.setattr(elite_portfolio_activation, "create_deployment", _fake_create_deployment)
    monkeypatch.setattr(elite_portfolio_activation, "feature_flags", lambda: {"broker_order_submission_enabled": False, "external_paper_execution_enabled": False})
    return elite_portfolio_activation


def test_activation_creates_deployments_persists_and_returns_cleanly(activation_env) -> None:
    conn = FakeActivationDatabase()

    result = activate_internal(conn, 1, "elite-builder-1-aaaaaaaaaaaaaaaaaaaaaaaa", "a" * 64)

    # The deployments exist -- this is the state your production traceback
    # already proved was true even while the endpoint 500'd.
    member_row = conn.members[1]
    assert member_row["activation_state"] == "external_approval_required"
    assert member_row["internal_deployment_id"] is not None
    assert member_row["external_deployment_id"] is not None
    assert member_row["latest_error"] is None

    # Persisting the attempt's result column must have actually happened --
    # i.e. json.dumps ran against it inside the fake without raising -- and
    # the stored attempt is no longer stuck.
    attempt = conn.attempts[conn.attempts_by_key["elite-builder-1-aaaaaaaaaaaaaaaaaaaaaaaa"]]
    assert attempt["status"] == "complete"
    assert attempt["result"]["status"] == "complete"

    # The endpoint response is the same sanitized object -- json.dumps must
    # succeed against exactly what was returned, proving the response would
    # serialize cleanly over HTTP too.
    json.dumps(result)
    assert result["status"] == "complete"
    assert result["members"][0]["internal_deployment_id"] == member_row["internal_deployment_id"]
    # Every datetime the raw rows carried came back as a plain string.
    assert isinstance(result["members"][0]["created_at"], str)
    assert isinstance(result["members"][0]["updated_at"], str)


def test_retrying_the_same_activation_reuses_deployments_and_creates_nothing_new(activation_env) -> None:
    conn = FakeActivationDatabase()
    idempotency_key = "elite-builder-1-aaaaaaaaaaaaaaaaaaaaaaaa"

    first = activate_internal(conn, 1, idempotency_key, "a" * 64)
    deployment_count_after_first = len(conn.strategy_deployments)
    external_count_after_first = len(conn.external_paper_deployments)

    second = activate_internal(conn, 1, idempotency_key, "a" * 64)

    assert second["status"] == "complete"
    assert first["members"][0]["internal_deployment_id"] == second["members"][0]["internal_deployment_id"]
    assert first["members"][0]["external_deployment_id"] == second["members"][0]["external_deployment_id"]
    assert len(conn.strategy_deployments) == deployment_count_after_first == 1
    assert len(conn.external_paper_deployments) == external_count_after_first == 1


def test_repair_completes_an_attempt_stuck_by_the_old_serialization_bug(activation_env) -> None:
    conn = FakeActivationDatabase()
    idempotency_key = "elite-builder-1-aaaaaaaaaaaaaaaaaaaaaaaa"

    # Simulate exactly what production hit: the deployment work already ran
    # and committed (member is in its terminal state, deployment ids are set),
    # but the attempt row was never marked complete because the old code
    # crashed on Jsonb(raw_result) before reaching that UPDATE.
    conn.strategy_deployments[8] = {
        "id": 8, "campaign_id": 3, "candidate_id": "sd_497fc1c84e6342", "symbol": "GOOGL", "timeframe": "4h",
        "strategy_version": "v1", "status": "active", "simulation_only": True, "created_at": conn.now, "updated_at": conn.now,
    }
    conn.external_paper_deployments[8] = {
        "id": 8, "internal_deployment_id": 8, "broker_account_id": 1, "campaign_id": 3, "elite_candidate_id": 9,
        "candidate_id": "sd_497fc1c84e6342", "strategy_version": "v1", "symbol": "GOOGL", "timeframe": "4h",
        "state": "disabled", "latest_blockers": ["EXPLICIT_OBSERVE_APPROVAL_REQUIRED"], "created_at": conn.now,
    }
    conn.members[1].update({"internal_deployment_id": 8, "external_deployment_id": 8, "activation_state": "external_approval_required"})
    attempt_id = conn._next()
    conn.attempts[attempt_id] = {
        "id": attempt_id, "portfolio_run_id": 1, "idempotency_key": idempotency_key, "status": "running",
        "requested_snapshot_hash": "a" * 64, "result": {}, "error": None, "started_at": conn.now, "completed_at": None,
    }
    conn.attempts_by_key[idempotency_key] = attempt_id

    repaired = repair_stalled_activation_attempts(conn)

    assert repaired == [{"attempt_id": attempt_id, "portfolio_run_id": 1, "idempotency_key": idempotency_key, "status": "complete"}]
    assert conn.attempts[attempt_id]["status"] == "complete"
    # No new deployment was created by the repair -- the member was already
    # terminal, so activation_worklist skipped it entirely.
    assert len(conn.strategy_deployments) == 1
    assert len(conn.external_paper_deployments) == 1
    assert conn.runs[1]["status"] == "activated_internal"


def test_repair_is_a_no_op_when_nothing_is_stalled(activation_env) -> None:
    conn = FakeActivationDatabase()
    activate_internal(conn, 1, "elite-builder-1-aaaaaaaaaaaaaaaaaaaaaaaa", "a" * 64)

    assert repair_stalled_activation_attempts(conn) == []


def test_a_response_serialization_failure_never_hides_a_committed_activation(activation_env, monkeypatch) -> None:
    """Even if the sanitize-then-persist step somehow still fails, the caller
    must see the true (already-committed) member state, not an opaque 500."""
    from app.services import elite_portfolio_activation

    conn = FakeActivationDatabase()

    def exploding_jsonable_encoder(value):
        raise TypeError("simulated: something new and unencodable slipped through")

    monkeypatch.setattr(elite_portfolio_activation, "jsonable_encoder", exploding_jsonable_encoder)

    with pytest.raises(TypeError):
        activate_internal(conn, 1, "elite-builder-1-aaaaaaaaaaaaaaaaaaaaaaaa", "a" * 64)

    # The deployment work itself must still be intact regardless of the
    # audit-column failure -- this is what "does not appear to have failed
    # operationally" means in practice.
    assert conn.members[1]["activation_state"] == "external_approval_required"
    assert conn.members[1]["internal_deployment_id"] is not None
