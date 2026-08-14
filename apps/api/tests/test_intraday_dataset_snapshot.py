from datetime import UTC, datetime

import pytest

from app.services.labs.intraday.dataset_snapshot import record_intraday_dataset_snapshot


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.row or []


class FakeSnapshotConn:
    def __init__(self, *, candle_count=500, feature_count=500):
        self.candle_count = candle_count
        self.feature_count = feature_count
        self.manifests: dict[str, dict] = {}
        self._next_id = 1
        self.candle_inserts = 0
        self.candle_insert_timeframes: list[str] = []
        self.feature_inserts = 0
        self.trade_flow_inserts = 0
        self.split_timestamp_queries = 0
        self.committed = False
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, query, params=None):
        params = params or ()
        self.executed.append((query, params))
        # Collapsed to single spaces so a reformatted query -- one that gained a
        # WHERE clause and therefore a line break -- still routes to the same
        # branch. Matching raw text meant an indentation change looked like an
        # unknown query.
        stripped = " ".join(query.split())
        if "FROM candles" in stripped and "COUNT(*)" in stripped:
            return FakeResult(
                {
                    "candle_count": self.candle_count,
                    "window_start": datetime(2026, 1, 2, tzinfo=UTC),
                    "window_end": datetime(2026, 3, 1, tzinfo=UTC),
                    "candle_hash": "candlehash",
                    "sources": ["alpaca_iex"],
                }
            )
        if "FROM intraday_features" in stripped and "COUNT(*)" in stripped:
            return FakeResult({"feature_count": self.feature_count, "feature_hash": "featurehash"})
        if "FROM intraday_trade_flow_features" in stripped and "COUNT(*)" in stripped:
            return FakeResult({"trade_flow_count": 100, "trade_flow_hash": "flowhash"})
        if stripped.startswith("INSERT INTO research_dataset_manifests"):
            dataset_key = params[0]
            if dataset_key in self.manifests:
                return FakeResult(None)  # ON CONFLICT DO NOTHING
            row = {
                "id": self._next_id,
                "dataset_key": dataset_key,
                "name": params[1],
                "mode": params[2],
                "dataset_kind": "intraday",
                "content_hash": params[11],
            }
            self._next_id += 1
            self.manifests[dataset_key] = row
            return FakeResult(dict(row))
        if stripped.startswith("SELECT * FROM research_dataset_manifests WHERE dataset_key"):
            return FakeResult(self.manifests.get(params[0]))
        if stripped.startswith("INSERT INTO research_dataset_candles"):
            self.candle_inserts += 1
            # params[2] is the timeframe: the outcome grid is materialized by
            # the same statement as the signal layer, so the timeframe is the
            # only thing that distinguishes the two passes.
            self.candle_insert_timeframes.append(params[2])
            return FakeResult(None)
        if stripped.startswith("INSERT INTO research_dataset_intraday_features"):
            self.feature_inserts += 1
            return FakeResult(None)
        if stripped.startswith("INSERT INTO research_dataset_trade_flow_features"):
            self.trade_flow_inserts += 1
            return FakeResult(None)
        # Phase E: splits are fixed at snapshot time, before any research has
        # run against the data. This fake returns too few timestamps to split,
        # which exercises the "cannot split" path without a split table.
        if stripped.startswith("SELECT DISTINCT timestamp, session_date"):
            self.split_timestamp_queries += 1
            return FakeResult([])
        if stripped.startswith("SELECT DISTINCT timestamp FROM research_dataset_candles"):
            self.split_timestamp_queries += 1
            return FakeResult([])
        raise AssertionError(f"unexpected query: {query}")

    def commit(self):
        self.committed = True


def test_record_intraday_dataset_snapshot_creates_a_manifest_tagged_intraday():
    conn = FakeSnapshotConn()

    dataset = record_intraday_dataset_snapshot(conn, assets=["amd", "spy"], timeframes=["30m"])

    assert dataset["dataset_kind"] == "intraday"
    assert dataset["mode"] == "rolling"
    assert dataset["dataset_key"].startswith("intraday_dataset_")
    assert conn.candle_inserts == 2  # one per (symbol, timeframe) pair
    assert conn.feature_inserts == 2
    assert conn.trade_flow_inserts == 2
    assert conn.committed is True


def test_snapshot_queries_cast_nullable_filter_parameters():
    conn = FakeSnapshotConn()

    record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])
    queries = " ".join(query.strip() for query, _ in conn.executed)

    assert "%s::timestamptz IS NULL" in queries
    assert "%s::text IS NULL" in queries
    assert "membership.universe_key = %s::text" in queries


def test_record_intraday_dataset_snapshot_is_idempotent_by_content_hash():
    conn = FakeSnapshotConn()

    first = record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])
    second = record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])

    assert first["id"] == second["id"]
    assert first["dataset_key"] == second["dataset_key"]
    assert len(conn.manifests) == 1


def test_record_intraday_dataset_snapshot_raises_when_no_candles():
    conn = FakeSnapshotConn(candle_count=0)

    with pytest.raises(ValueError, match="cannot snapshot missing candle dataset"):
        record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])


def test_record_intraday_dataset_snapshot_raises_when_no_intraday_features():
    conn = FakeSnapshotConn(feature_count=0)

    with pytest.raises(ValueError, match="no intraday_features rows"):
        record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])


def test_record_intraday_dataset_snapshot_rejects_bad_mode():
    conn = FakeSnapshotConn()

    with pytest.raises(ValueError, match="mode must be"):
        record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"], mode="bogus")


def test_record_intraday_dataset_snapshot_rejects_empty_assets_or_timeframes():
    conn = FakeSnapshotConn()

    with pytest.raises(ValueError, match="requires at least one asset and timeframe"):
        record_intraday_dataset_snapshot(conn, assets=[], timeframes=["30m"])
    with pytest.raises(ValueError, match="requires at least one asset and timeframe"):
        record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=[])


def test_predeclared_as_of_cutoff_changes_the_immutable_dataset_identity():
    conn = FakeSnapshotConn()

    first = record_intraday_dataset_snapshot(
        conn,
        assets=["AMD"],
        timeframes=["30m"],
        window_end=datetime(2026, 1, 31, tzinfo=UTC),
    )
    second = record_intraday_dataset_snapshot(
        conn,
        assets=["AMD"],
        timeframes=["30m"],
        window_end=datetime(2026, 2, 28, tzinfo=UTC),
    )

    assert first["dataset_key"] != second["dataset_key"]


def test_window_start_changes_the_immutable_dataset_identity():
    # Without the lower bound in the hash, a snapshot deliberately bounded to a
    # recent window would collide with an earlier unbounded one and silently
    # reuse a manifest describing years of history it does not contain.
    conn = FakeSnapshotConn()

    unbounded = record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])
    bounded = record_intraday_dataset_snapshot(
        conn,
        assets=["AMD"],
        timeframes=["30m"],
        window_start=datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
    )

    assert unbounded["dataset_key"] != bounded["dataset_key"]
    assert len(conn.manifests) == 2


def test_window_start_bounds_the_snapshot_queries():
    conn = FakeSnapshotConn()

    record_intraday_dataset_snapshot(
        conn,
        assets=["AMD"],
        timeframes=["30m"],
        window_start=datetime(2025, 1, 6, 14, 30, tzinfo=UTC),
    )
    summaries = [
        (" ".join(query.split()), params)
        for query, params in conn.executed
        if "COUNT(*)" in query
    ]

    assert summaries, "expected the summary queries to run"
    for query, params in summaries[:2]:
        assert "timestamp >= %s::timestamptz" in query
        assert datetime(2025, 1, 6, 14, 30, tzinfo=UTC) in params


def test_window_start_must_precede_window_end():
    with pytest.raises(ValueError, match="earlier than window_end"):
        record_intraday_dataset_snapshot(
            FakeSnapshotConn(),
            assets=["AMD"],
            timeframes=["30m"],
            window_start=datetime(2026, 3, 1, tzinfo=UTC),
            window_end=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_window_start_must_be_timezone_aware():
    with pytest.raises(ValueError, match="window_start must be timezone-aware"):
        record_intraday_dataset_snapshot(
            FakeSnapshotConn(),
            assets=["AMD"],
            timeframes=["30m"],
            window_start=datetime(2025, 1, 6),
        )


def test_outcome_timeframes_freeze_a_finer_candle_only_grid():
    conn = FakeSnapshotConn()

    dataset = record_intraday_dataset_snapshot(
        conn,
        assets=["AMD"],
        timeframes=["30m"],
        outcome_timeframes=["1m"],
    )

    # The outcome grid is candles only: `intraday_features` is CHECK-constrained
    # to 15m/30m, and a forward-return grid needs prices, not signals.
    assert conn.candle_insert_timeframes == ["30m", "1m"]
    assert conn.feature_inserts == 1
    assert conn.trade_flow_inserts == 1
    assert dataset["dataset_kind"] == "intraday"


def test_outcome_timeframes_change_the_dataset_identity():
    conn = FakeSnapshotConn()

    without = record_intraday_dataset_snapshot(conn, assets=["AMD"], timeframes=["30m"])
    with_grid = record_intraday_dataset_snapshot(
        conn, assets=["AMD"], timeframes=["30m"], outcome_timeframes=["1m"]
    )

    assert without["dataset_key"] != with_grid["dataset_key"]


def test_a_timeframe_cannot_be_both_signal_and_outcome():
    with pytest.raises(ValueError, match="cannot be both a signal timeframe"):
        record_intraday_dataset_snapshot(
            FakeSnapshotConn(),
            assets=["AMD"],
            timeframes=["30m"],
            outcome_timeframes=["30m"],
        )


def test_splits_are_computed_from_the_signal_layer_only():
    # A phase says which decisions a researcher was allowed to see, and
    # decisions happen on signal bars. Letting a 1m outcome grid into the
    # calculation would put the boundaries wherever that grid is dense.
    conn = FakeSnapshotConn()

    record_intraday_dataset_snapshot(
        conn, assets=["AMD"], timeframes=["30m"], outcome_timeframes=["1m"]
    )
    split_queries = [
        params for query, params in conn.executed if "SELECT DISTINCT timestamp" in query
    ]

    assert split_queries
    for params in split_queries:
        assert ["30m"] in params


def test_as_of_cutoff_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        record_intraday_dataset_snapshot(
            FakeSnapshotConn(),
            assets=["AMD"],
            timeframes=["30m"],
            window_end=datetime(2026, 1, 31),
        )
