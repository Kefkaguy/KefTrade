from __future__ import annotations

from app.services.elite_portfolio_activation import _activate_member, activation_worklist, authorization_instruction


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

    instruction = authorization_instruction(member(1), snapshot_hash)

    assert instruction is not None
    assert instruction["portfolio_snapshot_hash"] == snapshot_hash
    assert "--confirm-deployment-id 101" in instruction["command"]
    assert instruction["expected_effect"].endswith("this command does not enable order submission.")
    assert authorization_instruction(member(2, direction="short"), snapshot_hash) is None


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
