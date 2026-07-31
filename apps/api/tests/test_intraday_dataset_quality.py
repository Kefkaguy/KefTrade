from app.services.intraday_dataset_quality import (
    GAP_EXPERIMENT_OBSERVATION_TARGET,
    GAP_EXPERIMENT_SESSION_TARGET,
    duplicate_rows,
    gap_event_power,
    session_shape_report,
)


class Result:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class QueryConn:
    """Returns a canned result per query fragment, in call order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.queries: list[str] = []

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))
        return self.responses.pop(0)

    def commit(self):
        pass


def flow_row(state, *, total_obs, total_sessions, validation_obs, validation_sessions):
    return {
        "flow_state": state,
        "total_observations": total_obs,
        "total_sessions": total_sessions,
        "validation_observations": validation_obs,
        "validation_sessions": validation_sessions,
    }


def power(rows):
    return gap_event_power(QueryConn([Result(rows=rows)]), dataset_id=1, timeframe="30m")


def test_power_is_measured_on_the_validation_split_not_the_whole_dataset():
    report = power(
        [
            flow_row(
                "acceptance",
                total_obs=5000,
                total_sessions=2000,
                validation_obs=900,
                validation_sessions=500,
            ),
            flow_row(
                "absorption",
                total_obs=4000,
                total_sessions=1800,
                validation_obs=880,
                validation_sessions=480,
            ),
        ]
    )

    assert report["measured_on"] == "validation_split_50_to_80_percent"
    # The huge dataset totals are not what the gate reads.
    assert report["by_flow_state"]["acceptance"]["validation_observations"] == 900
    assert report["passed"] is True


def test_a_large_pool_does_not_rescue_an_underpowered_flow_state():
    report = power(
        [
            flow_row(
                "acceptance",
                total_obs=9000,
                total_sessions=3000,
                validation_obs=2400,
                validation_sessions=900,
            ),
            flow_row(
                "absorption",
                total_obs=800,
                total_sessions=400,
                validation_obs=120,
                validation_sessions=90,
            ),
        ]
    )

    # Acceptance is comfortably powered; the experiment still is not, because
    # absorption is a separate hypothesis and it is not.
    assert report["by_flow_state"]["acceptance"]["passed"] is True
    assert report["by_flow_state"]["absorption"]["passed"] is False
    assert report["limiting_flow_state"] == "absorption"
    assert report["passed"] is False


def test_a_flow_state_with_no_events_is_reported_as_zero_not_omitted():
    report = power(
        [
            flow_row(
                "acceptance",
                total_obs=3000,
                total_sessions=1500,
                validation_obs=900,
                validation_sessions=500,
            )
        ]
    )

    absorption = report["by_flow_state"]["absorption"]
    assert absorption["validation_observations"] == 0
    assert absorption["passed"] is False
    assert absorption["observations_short_by"] == GAP_EXPERIMENT_OBSERVATION_TARGET
    assert absorption["sessions_short_by"] == GAP_EXPERIMENT_SESSION_TARGET
    assert report["passed"] is False


def test_the_shortfall_is_reported_for_both_sessions_and_observations():
    report = power(
        [
            flow_row(
                "acceptance",
                total_obs=1000,
                total_sessions=600,
                validation_obs=300,
                validation_sessions=200,
            ),
            flow_row(
                "absorption",
                total_obs=400,
                total_sessions=250,
                validation_obs=100,
                validation_sessions=75,
            ),
        ]
    )

    acceptance = report["by_flow_state"]["acceptance"]
    assert acceptance["observations_short_by"] == GAP_EXPERIMENT_OBSERVATION_TARGET - 300
    assert acceptance["sessions_short_by"] == GAP_EXPERIMENT_SESSION_TARGET - 200


def test_enough_observations_but_too_few_sessions_still_fails():
    # Many events crammed into few sessions are not independent evidence.
    report = power(
        [
            flow_row(
                "acceptance",
                total_obs=9000,
                total_sessions=300,
                validation_obs=3000,
                validation_sessions=100,
            ),
            flow_row(
                "absorption",
                total_obs=9000,
                total_sessions=300,
                validation_obs=3000,
                validation_sessions=100,
            ),
        ]
    )

    assert report["passed"] is False
    assert report["by_flow_state"]["acceptance"]["sessions_short_by"] > 0


def test_duplicate_detection_fails_a_snapshot_holding_two_feeds():
    conn = QueryConn(
        [
            Result(rows=[]),
            Result(
                rows=[
                    {"source": "alpaca_iex", "rows": 100},
                    {"source": "alpaca_sip", "rows": 100},
                ]
            ),
        ]
    )

    report = duplicate_rows(conn, dataset_id=1, timeframe="30m")

    assert report["duplicate_symbol_timestamp_rows"] == 0
    assert report["single_source"] is False
    assert report["passed"] is False


def test_duplicate_detection_passes_a_single_feed_snapshot():
    conn = QueryConn([Result(rows=[]), Result(rows=[{"source": "alpaca_sip", "rows": 100}])])

    assert duplicate_rows(conn, dataset_id=1, timeframe="30m")["passed"] is True


def test_session_shapes_classify_full_early_close_and_incomplete():
    conn = QueryConn(
        [
            Result(
                rows=[
                    {"symbol": "AAPL", "session_date": "d1", "regular_bars": 13, "extended_bars": 4},
                    {"symbol": "AAPL", "session_date": "d2", "regular_bars": 7, "extended_bars": 0},
                    {"symbol": "AAPL", "session_date": "d3", "regular_bars": 5, "extended_bars": 0},
                ]
            )
        ]
    )

    report = session_shape_report(conn, dataset_id=1, timeframe="30m")

    assert report["expected_full_session_bars"] == 13
    assert report["session_shapes"] == {"full": 1, "early_close": 1, "incomplete": 1}
    assert report["extended_hours_rows"] == 4
    # Two of three complete is below the 95% bar.
    assert report["passed"] is False
