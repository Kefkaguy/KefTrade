import pytest

from app.services.labs.intraday.specialist import (
    VALID_INVESTIGATION_TYPES,
    VALID_THREAD_STATUSES,
    create_specialist_thread,
    get_specialist_thread,
    list_specialist_investigations,
    record_specialist_investigation,
    update_specialist_thread_status,
)


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []


class FakeSpecialistConn:
    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.investigations: list[dict] = []
        self._next_thread_id = 1
        self._next_investigation_id = 1

    def execute(self, query, params=None):
        params = params or ()
        stripped = query.strip()
        if stripped.startswith("SELECT * FROM research_specialist_threads WHERE thread_key"):
            thread = self.threads.get(params[0])
            return FakeResult(dict(thread) if thread else None)
        if stripped.startswith("SELECT id FROM research_specialist_threads WHERE thread_key"):
            thread = self.threads.get(params[0])
            return FakeResult({"id": thread["id"]} if thread else None)
        if stripped.startswith("INSERT INTO research_specialist_threads"):
            thread_key, title, origin_campaign_id, origin_candidate_id, frozen_parameters, scope_symbols, scope_timeframe, scope_direction = params
            row = {
                "id": self._next_thread_id,
                "thread_key": thread_key,
                "title": title,
                "origin_campaign_id": origin_campaign_id,
                "origin_candidate_id": origin_candidate_id,
                "frozen_parameters": frozen_parameters.obj,
                "scope_symbols": scope_symbols.obj,
                "scope_timeframe": scope_timeframe,
                "scope_direction": scope_direction,
                "status": "active_research",
            }
            self._next_thread_id += 1
            self.threads[thread_key] = row
            return FakeResult(dict(row))
        if stripped.startswith("UPDATE research_specialist_threads SET status"):
            status, thread_key = params
            thread = self.threads.get(thread_key)
            if not thread:
                return FakeResult(None)
            thread["status"] = status
            return FakeResult(dict(thread))
        if stripped.startswith("INSERT INTO research_specialist_investigations"):
            thread_id, investigation_type, dataset_id, campaign_id, findings, conclusion = params
            row = {
                "id": self._next_investigation_id,
                "thread_id": thread_id,
                "investigation_type": investigation_type,
                "dataset_id": dataset_id,
                "campaign_id": campaign_id,
                "findings": findings.obj,
                "conclusion": conclusion,
            }
            self._next_investigation_id += 1
            self.investigations.append(row)
            return FakeResult(dict(row))
        if stripped.startswith("SELECT * FROM research_specialist_investigations WHERE thread_id"):
            thread_id = params[0]
            return FakeResult([dict(row) for row in self.investigations if row["thread_id"] == thread_id])
        raise AssertionError(f"unexpected query: {query}")

    def commit(self):
        pass


def test_create_specialist_thread_freezes_parameters():
    conn = FakeSpecialistConn()

    thread = create_specialist_thread(
        conn,
        thread_key="amd_30m_long_session_momentum",
        title="AMD 30m long Session Momentum",
        origin_candidate_id="sessmom_6d9e916151af38",
        frozen_parameters={"momentum_threshold": "0.004"},
        scope_timeframe="30m",
        scope_direction="long",
        origin_campaign_id=50,
        scope_symbols=["AMD"],
    )

    assert thread["status"] == "active_research"
    assert thread["frozen_parameters"] == {"momentum_threshold": "0.004"}
    assert thread["origin_campaign_id"] == 50


def test_create_specialist_thread_is_idempotent_by_thread_key():
    conn = FakeSpecialistConn()
    first = create_specialist_thread(
        conn, thread_key="t1", title="T1", origin_candidate_id="c1",
        frozen_parameters={"a": 1}, scope_timeframe="30m", scope_direction="long",
    )
    second = create_specialist_thread(
        conn, thread_key="t1", title="Different title, should be ignored", origin_candidate_id="c1",
        frozen_parameters={"a": 1}, scope_timeframe="30m", scope_direction="long",
    )

    assert first["id"] == second["id"]
    assert second["title"] == "T1"


def test_create_specialist_thread_rejects_invalid_direction():
    conn = FakeSpecialistConn()
    with pytest.raises(ValueError):
        create_specialist_thread(
            conn, thread_key="t1", title="T1", origin_candidate_id="c1",
            frozen_parameters={}, scope_timeframe="30m", scope_direction="sideways",
        )


def test_update_specialist_thread_status_transitions_and_rejects_unknown_status():
    conn = FakeSpecialistConn()
    create_specialist_thread(
        conn, thread_key="t1", title="T1", origin_candidate_id="c1",
        frozen_parameters={}, scope_timeframe="30m", scope_direction="long",
    )

    updated = update_specialist_thread_status(conn, thread_key="t1", status="confirmed_specialist")
    assert updated["status"] == "confirmed_specialist"

    with pytest.raises(ValueError):
        update_specialist_thread_status(conn, thread_key="t1", status="promoted")

    with pytest.raises(ValueError):
        update_specialist_thread_status(conn, thread_key="does_not_exist", status="retired")


def test_record_specialist_investigation_requires_an_existing_thread():
    conn = FakeSpecialistConn()
    with pytest.raises(ValueError):
        record_specialist_investigation(
            conn, thread_key="missing", investigation_type="unseen_holdout_performance", findings={},
        )


def test_record_specialist_investigation_rejects_unknown_type():
    conn = FakeSpecialistConn()
    create_specialist_thread(
        conn, thread_key="t1", title="T1", origin_candidate_id="c1",
        frozen_parameters={}, scope_timeframe="30m", scope_direction="long",
    )
    with pytest.raises(ValueError):
        record_specialist_investigation(conn, thread_key="t1", investigation_type="vibes_check", findings={})


def test_record_specialist_investigation_appends_and_lists_in_order():
    conn = FakeSpecialistConn()
    create_specialist_thread(
        conn, thread_key="t1", title="T1", origin_candidate_id="c1",
        frozen_parameters={}, scope_timeframe="30m", scope_direction="long",
    )

    record_specialist_investigation(
        conn, thread_key="t1", investigation_type="unseen_holdout_performance",
        findings={"net_profit_factor": 0.9}, conclusion="Does not hold up out of sample.",
    )
    record_specialist_investigation(
        conn, thread_key="t1", investigation_type="cost_robustness",
        findings={"net_profit_factor_at_2x_costs": 0.7},
    )

    investigations = list_specialist_investigations(conn, "t1")
    assert len(investigations) == 2
    assert investigations[0]["investigation_type"] == "unseen_holdout_performance"
    assert investigations[1]["investigation_type"] == "cost_robustness"


def test_get_specialist_thread_returns_none_when_missing():
    conn = FakeSpecialistConn()
    assert get_specialist_thread(conn, "does_not_exist") is None


def test_valid_status_and_investigation_type_constants_match_migration_049():
    assert set(VALID_THREAD_STATUSES) == {"active_research", "confirmed_specialist", "invalidated", "retired"}
    assert set(VALID_INVESTIGATION_TYPES) == {
        "unseen_holdout_performance",
        "forward_validation",
        "parameter_robustness",
        "cost_robustness",
        "stability_across_years",
        "similarity_to_declared_securities",
    }
