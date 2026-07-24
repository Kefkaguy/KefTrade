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
        self.feature_inserts = 0
        self.committed = False

    def execute(self, query, params=None):
        params = params or ()
        stripped = query.strip()
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
            return FakeResult(None)
        if stripped.startswith("INSERT INTO research_dataset_intraday_features"):
            self.feature_inserts += 1
            return FakeResult(None)
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
    assert conn.committed is True


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
