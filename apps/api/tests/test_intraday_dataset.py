"""Phase 12, Step 2A: intraday backtest dataset loader.

Proves the loader never silently backtests a partially joined dataset: every
honesty check (unsupported timeframe, missing data, low join coverage,
missing session metadata, too few sessions, insufficient opening-range
coverage) raises `IntradayDatasetError` with the real counts, and a
well-formed dataset produces a correct `session_end_index`.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.labs.intraday.dataset import (
    IntradayDatasetError,
    build_intraday_backtest_dataset,
    build_session_end_index,
    entry_is_within_session_cutoff,
    load_intraday_backtest_dataset,
    minimum_entry_lookahead_minutes,
)
from app.settings import settings


def make_dataset(
    *,
    symbol: str = "TEST",
    timeframe: str = "30m",
    sessions: int = 20,
    bars_per_session: int = 13,
    missing_opening_range_first_bar: bool = False,
) -> tuple[list[dict], list[dict]]:
    candles: list[dict] = []
    features: list[dict] = []
    cursor = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    for session_index in range(sessions):
        session_date = date(2026, 1, 2) + timedelta(days=session_index)
        for bar_index in range(bars_per_session):
            timestamp = cursor
            cursor += timedelta(minutes=30)
            candles.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "open": Decimal("100"),
                    "high": Decimal("100.5"),
                    "low": Decimal("99.5"),
                    "close": Decimal("100"),
                    "volume": Decimal("1000"),
                }
            )
            opening_range_missing = missing_opening_range_first_bar and bar_index == 0
            features.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "session_date": session_date,
                    "minutes_from_open": bar_index * 30,
                    "minutes_to_close": (bars_per_session - 1 - bar_index) * 30,
                    "opening_range_high": None if opening_range_missing else Decimal("101"),
                    "opening_range_low": None if opening_range_missing else Decimal("99"),
                }
            )
    return candles, features


def test_rejects_unsupported_timeframe() -> None:
    candles, features = make_dataset(timeframe="1h")
    with pytest.raises(IntradayDatasetError, match="Unsupported"):
        build_intraday_backtest_dataset(candles, features, symbol="TEST", timeframe="1h")


def test_rejects_empty_candles() -> None:
    with pytest.raises(IntradayDatasetError, match="No candles"):
        build_intraday_backtest_dataset([], [{"timestamp": datetime.now(UTC)}], symbol="TEST", timeframe="30m")


def test_rejects_empty_features() -> None:
    candles, _ = make_dataset(sessions=1, bars_per_session=1)
    with pytest.raises(IntradayDatasetError, match="No intraday_features"):
        build_intraday_backtest_dataset(candles, [], symbol="TEST", timeframe="30m")


def test_rejects_zero_overlap_join() -> None:
    candles, features = make_dataset(sessions=1, bars_per_session=3)
    for row in features:
        row["timestamp"] = row["timestamp"] + timedelta(days=365)
    with pytest.raises(IntradayDatasetError, match="zero rows"):
        build_intraday_backtest_dataset(candles, features, symbol="TEST", timeframe="30m")


def test_rejects_low_join_coverage() -> None:
    candles, features = make_dataset(sessions=20, bars_per_session=13)
    kept = max(1, len(features) // 10)
    with pytest.raises(IntradayDatasetError, match="Refusing to silently backtest"):
        build_intraday_backtest_dataset(candles, features[:kept], symbol="TEST", timeframe="30m")


def test_rejects_missing_session_date() -> None:
    candles, features = make_dataset(sessions=20, bars_per_session=13)
    features[5]["session_date"] = None
    with pytest.raises(IntradayDatasetError, match="session_date"):
        build_intraday_backtest_dataset(candles, features, symbol="TEST", timeframe="30m")


def test_rejects_too_few_distinct_sessions() -> None:
    assert settings.intraday_minimum_distinct_sessions >= 6
    candles, features = make_dataset(sessions=5, bars_per_session=13)
    with pytest.raises(IntradayDatasetError, match="distinct sessions"):
        build_intraday_backtest_dataset(candles, features, symbol="TEST", timeframe="30m")


def test_rejects_insufficient_opening_range_coverage() -> None:
    candles, features = make_dataset(sessions=20, bars_per_session=13, missing_opening_range_first_bar=True)
    with pytest.raises(IntradayDatasetError, match="Opening-range coverage"):
        build_intraday_backtest_dataset(candles, features, symbol="TEST", timeframe="30m")


def test_well_formed_dataset_builds_correct_session_end_index_and_coverage(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intraday_minimum_distinct_sessions", 3)
    candles, features = make_dataset(sessions=3, bars_per_session=5)

    dataset = build_intraday_backtest_dataset(candles, features, symbol="TEST", timeframe="30m")

    assert len(dataset["rows"]) == 15
    assert dataset["coverage"]["candle_join_ratio"] == 1.0
    assert dataset["coverage"]["distinct_sessions"] == 3
    assert dataset["coverage"]["opening_range_coverage"] == 1.0
    # Sessions of 5 bars each: [0..4], [5..9], [10..14].
    session_end_index = dataset["session_end_index"]
    assert session_end_index[0] == 4
    assert session_end_index[4] == 4
    assert session_end_index[5] == 9
    assert session_end_index[14] == 14


def test_build_session_end_index_on_empty_rows() -> None:
    assert build_session_end_index([]) == []


def test_minimum_entry_lookahead_minutes_scales_with_bar_duration() -> None:
    assert minimum_entry_lookahead_minutes("15m", entry_offset_bars=1, minimum_holding_bars=1) == 30
    assert minimum_entry_lookahead_minutes("30m", entry_offset_bars=1, minimum_holding_bars=1) == 60


def test_minimum_entry_lookahead_minutes_rejects_unsupported_timeframe() -> None:
    with pytest.raises(IntradayDatasetError, match="Unsupported"):
        minimum_entry_lookahead_minutes("1h")


def test_entry_cutoff_true_when_enough_session_time_remains() -> None:
    feature = {"minutes_to_close": 90}
    assert entry_is_within_session_cutoff(feature, timeframe="30m", entry_offset_bars=1, minimum_holding_bars=1) is True


def test_entry_cutoff_false_when_too_close_to_session_end() -> None:
    feature = {"minutes_to_close": 30}
    assert entry_is_within_session_cutoff(feature, timeframe="30m", entry_offset_bars=1, minimum_holding_bars=1) is False


def test_entry_cutoff_false_when_minutes_to_close_missing() -> None:
    assert entry_is_within_session_cutoff({}, timeframe="30m") is False


class FakeFrozenDatasetConn:
    """Phase 12.5: records which table load_intraday_backtest_dataset reads
    from when a dataset_id is supplied -- must be the frozen snapshot tables,
    never the live candles/intraday_features tables."""

    def __init__(self, candles, features):
        self._candles = candles
        self._features = features
        self.queried_tables: list[str] = []

    def execute(self, query, params=None):
        if "research_dataset_candles" in query:
            self.queried_tables.append("research_dataset_candles")
            return _Result(self._candles)
        if "research_dataset_intraday_features" in query:
            self.queried_tables.append("research_dataset_intraday_features")
            return _Result(self._features)
        if " FROM candles" in query:
            self.queried_tables.append("candles")
            return _Result(self._candles)
        if " FROM intraday_features" in query:
            self.queried_tables.append("intraday_features")
            return _Result(self._features)
        raise AssertionError(f"unexpected query: {query}")


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def test_load_intraday_backtest_dataset_reads_frozen_tables_when_dataset_id_given() -> None:
    candles, features = make_dataset(sessions=20, bars_per_session=13)
    conn = FakeFrozenDatasetConn(candles, features)

    result = load_intraday_backtest_dataset(conn, "TEST", "30m", dataset_id=7)

    assert conn.queried_tables == ["research_dataset_candles", "research_dataset_intraday_features"]
    assert result["symbol"] == "TEST"
    assert len(result["rows"]) == len(candles)


def test_load_intraday_backtest_dataset_reads_live_tables_when_no_dataset_id() -> None:
    candles, features = make_dataset(sessions=20, bars_per_session=13)
    conn = FakeFrozenDatasetConn(candles, features)

    load_intraday_backtest_dataset(conn, "TEST", "30m")

    assert conn.queried_tables == ["candles", "intraday_features"]
