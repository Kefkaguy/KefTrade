from app.services.research_architecture import append_hypothesis_version


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeHypothesisConn:
    def __init__(self):
        self.inserted_params = None

    def execute(self, query, params=None):
        stripped = query.strip()
        if stripped.startswith("SELECT COALESCE(MAX(version)"):
            return FakeResult({"next_version": 1})
        if stripped.startswith("INSERT INTO research_hypothesis_versions"):
            self.inserted_params = params
            return FakeResult({"id": 1, "version": 1})
        raise AssertionError(f"unexpected query: {query}")


def base_hypothesis():
    return {
        "hypothesis_key": "amd_30m_session_momentum",
        "scope_type": "asset",
        "scope_ref": "AMD",
        "strategy_family": "Session Momentum",
        "title": "AMD 30m long momentum continuation",
        "observation": "AMD showed a repeatable positive edge in Campaign 50.",
        "hypothesis": "Momentum continuation exists for AMD specifically, not universally.",
        "expected_behavior": "Positive net profit factor on AMD 30m long only.",
        "confidence_score": 0.4,
        "evidence_window": {"campaign_id": 50},
        "creation_source": "phase_12_5_specialist_thread",
    }


def test_append_hypothesis_version_persists_required_and_invalidation_conditions_and_success_criteria():
    conn = FakeHypothesisConn()

    append_hypothesis_version(
        conn,
        base_hypothesis(),
        status="testing",
        test_summary={},
        required_conditions="Positive net profit factor must hold across at least 2 distinct calendar years.",
        invalidation_conditions="Net profit factor falls below 1.0 in any single held-out year.",
        success_criteria={"min_net_profit_factor": 1.0, "min_years_tested": 2},
    )

    params = conn.inserted_params
    assert params[-3] == "Positive net profit factor must hold across at least 2 distinct calendar years."
    assert params[-2] == "Net profit factor falls below 1.0 in any single held-out year."
    assert params[-1].obj == {"min_net_profit_factor": 1.0, "min_years_tested": 2}


def test_append_hypothesis_version_leaves_new_fields_null_when_omitted():
    conn = FakeHypothesisConn()

    append_hypothesis_version(conn, base_hypothesis(), status="proposed", test_summary={})

    params = conn.inserted_params
    assert params[-3] is None
    assert params[-2] is None
    assert params[-1] is None


def test_append_hypothesis_version_rejects_unsupported_status():
    conn = FakeHypothesisConn()
    try:
        append_hypothesis_version(conn, base_hypothesis(), status="approved", test_summary={})
        assert False, "expected ValueError"
    except ValueError:
        pass
