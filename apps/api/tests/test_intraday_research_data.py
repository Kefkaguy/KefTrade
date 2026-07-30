from datetime import UTC, datetime

from app.services.intraday_research_data import research_data_readiness


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class ReadinessConnection:
    def __init__(self):
        self.queries: list[str] = []

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "FROM research_dataset_manifests" in normalized:
            return FakeResult(
                {
                    "assets": ["AAPL"],
                    "window_start": datetime(2025, 1, 2, tzinfo=UTC),
                    "window_end": datetime(2026, 1, 2, tzinfo=UTC),
                    "integrity": {},
                }
            )
        if (
            "FROM research_dataset_intraday_features" in normalized
            and "JOIN" not in normalized
            and "quote_count > 0" not in normalized
        ):
            assert "midpoint_at_message" not in normalized
            assert "auction_price" not in normalized
            return FakeResult({"rows": 100, "symbols": 1, "sessions": 10})
        if "FROM research_dataset_intraday_features snapshot" in normalized:
            assert "FROM intraday_microstructure_features live" in normalized
            assert "LOWER(live.feed) = 'sip'" in normalized
            assert "live.symbol = snapshot.symbol" in normalized
            assert "live.timeframe = snapshot.timeframe" in normalized
            assert "live.timestamp = snapshot.timestamp" in normalized
            return FakeResult(
                {
                    "frozen_rows": 0,
                    "frozen_symbols": 0,
                    "live_sip_rows": 25,
                    "live_sip_symbols": 1,
                    "available_rows": 25,
                    "available_symbols": 1,
                }
            )
        if "FROM intraday_auction_imbalances" in normalized:
            assert "midpoint_at_message" in normalized
            assert "auction_price" in normalized
            return FakeResult(
                {
                    "rows": 0,
                    "symbols": 0,
                    "sessions": 0,
                    "executable_rows": 0,
                }
            )
        raise AssertionError(f"unexpected query: {query}")


def test_readiness_checks_auction_prices_only_on_the_auction_table():
    conn = ReadinessConnection()

    result = research_data_readiness(conn, dataset_id=76, timeframe="30m")

    assert result["candle_features"]["rows"] == 100
    assert result["microstructure"] == {
        "rows": 25,
        "symbols": 1,
        "coverage": 0.25,
        "live_sip_rows": 25,
        "live_sip_symbols": 1,
        "frozen_rows": 0,
        "frozen_symbols": 0,
        "frozen_coverage": 0.0,
        "snapshot_refresh_required": True,
        "matching_rule": (
            "exact symbol/timeframe/timestamp match; consolidated SIP feed only"
        ),
    }
    assert result["gates"]["microstructure_80pct_coverage"] is False
    assert result["gates"]["frozen_microstructure_80pct_coverage"] is False
    assert result["institutional_execution_ready"] is False
    assert result["auction_imbalances"]["executable_rows"] == 0
