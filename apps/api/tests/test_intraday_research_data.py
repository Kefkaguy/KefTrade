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
        if "FROM research_dataset_intraday_features" in normalized and "JOIN" not in normalized:
            assert "midpoint_at_message" not in normalized
            assert "auction_price" not in normalized
            return FakeResult({"rows": 100, "symbols": 1, "sessions": 10})
        if "FROM intraday_microstructure_features" in normalized:
            return FakeResult({"rows": 0, "symbols": 0})
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
    assert result["auction_imbalances"]["executable_rows"] == 0
