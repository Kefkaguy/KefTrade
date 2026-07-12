from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.services import signal_reviews


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self):
        self.reviews = {}
        self.logs = []
        self.order_queries = []
        self.commits = 0
        self.next_review_id = 1
        self.evidence_metric_rows = []

    def execute(self, query, params=None):
        if "FROM evidence_alerts" in query:
            return Result(self.evidence_metric_rows)
        if "SELECT quantity FROM paper_positions" in query:
            return Result([])
        if "INSERT INTO signal_reviews" in query:
            row = {
                "id": self.next_review_id,
                "account_id": params[0],
                "deployment_id": params[1],
                "symbol": params[2],
                "timeframe": params[3],
                "strategy_id": params[4],
                "status": params[5],
                "verdict": params[6],
                "regime": params[7],
                "evidence_score": params[8],
                "matched_rules": jsonb_value(params[9]),
                "failed_rules": jsonb_value(params[10]),
                "profit_factor": params[11],
                "expectancy": params[12],
                "trade_count": params[13],
                "max_drawdown": params[14],
                "latest_candle_timestamp": params[15],
                "data_freshness": params[16],
                "possible_entry_price": params[17],
                "invalidation_level": params[18],
                "risk_target": params[19],
                "exit_zone": params[20],
                "risk_per_share": params[21],
                "reward_per_share": params[22],
                "risk_reward_ratio": params[23],
                "max_holding_bars": params[24],
                "note": None,
                "reviewed_at": None,
                "ignored_at": None,
                "sent_to_paper_simulation_at": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "simulation_only": True,
            }
            self.reviews[row["id"]] = row
            self.next_review_id += 1
            return Result([row])
        if "UPDATE signal_reviews" in query:
            row = self.reviews.get(params[-1])
            if not row:
                return Result([])
            if "reviewed_at = NOW()" in query:
                row["reviewed_at"] = datetime.now(UTC)
            if "ignored_at = NOW()" in query:
                row["ignored_at"] = datetime.now(UTC)
            if "sent_to_paper_simulation_at = NOW()" in query:
                row["sent_to_paper_simulation_at"] = datetime.now(UTC)
            if "note = %s" in query:
                row["note"] = params[0]
            return Result([row])
        if "INSERT INTO execution_logs" in query:
            self.logs.append({"event_type": params[2], "payload": params[4]})
            return Result([])
        if "paper_orders" in query or "broker" in query.lower():
            self.order_queries.append(query)
        return Result([])

    def commit(self):
        self.commits += 1


def jsonb_value(value):
    return getattr(value, "obj", value)


def deployment():
    return {
        "id": 7,
        "account_id": 3,
        "symbol": "TSLA",
        "timeframe": "1h",
        "strategy_name": "momentum",
        "strategy_version": "bull_v2",
        "parameters": {"returns_5_min": 0.008, "risk_reward": 1.6, "swing_lookback": 5, "max_holding_bars": 12},
        "simulation_only": True,
    }


def test_signal_review_blocks_stale_data_before_features_or_levels(monkeypatch) -> None:
    stale_candle = {
        "symbol": "TSLA",
        "timeframe": "1h",
        "timestamp": datetime.now(UTC) - timedelta(days=10),
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "volume": Decimal("1000"),
    }
    conn = FakeConn()
    monkeypatch.setattr(signal_reviews, "load_candles", lambda *args, **kwargs: [stale_candle])
    monkeypatch.setattr(signal_reviews, "latest_feature", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale data must not load features")))

    review = signal_reviews.generate_signal_review(conn, deployment())

    assert review["status"] == "Stale Data Blocked"
    assert review["possible_entry_price"] is None
    assert review["invalidation_level"] is None
    assert review["risk_target"] is None
    assert review["simulation_only"] is True
    assert conn.order_queries == []


def test_signal_review_manual_actions_do_not_create_orders_or_call_brokers() -> None:
    conn = FakeConn()
    review = {
        "id": 1,
        "account_id": 3,
        "deployment_id": 7,
        "symbol": "TSLA",
        "timeframe": "1h",
        "strategy_id": "momentum_bull_v2_007",
        "status": "Setup Worth Reviewing",
        "verdict": "Setup Worth Reviewing",
        "simulation_only": True,
    }
    conn.reviews[1] = review

    updated = signal_reviews.mark_signal_review(conn, 1, "sent_to_paper_simulation")

    assert updated["sent_to_paper_simulation_at"] is not None
    assert updated["simulation_only"] is True
    assert conn.order_queries == []
    assert conn.logs[0]["event_type"] == "signal_review_sent_to_paper_simulation"


def test_signal_review_prefers_selected_candidate_metrics_over_recomputed_failures(monkeypatch) -> None:
    fresh_candle = {
        "symbol": "TSLA",
        "timeframe": "1h",
        "timestamp": datetime.now(UTC) - timedelta(hours=2),
        "open": Decimal("108"),
        "high": Decimal("112"),
        "low": Decimal("107"),
        "close": Decimal("110"),
        "volume": Decimal("1000"),
    }
    feature = {
        "timestamp": fresh_candle["timestamp"],
        "ema_50": Decimal("100"),
        "returns_5": Decimal("0.001"),
        "macd": Decimal("2"),
        "macd_signal": Decimal("1"),
    }
    conn = FakeConn()
    conn.evidence_metric_rows = [
        {
            "profit_factor": Decimal("0.78"),
            "expectancy": Decimal("-8.93"),
            "trade_count": 91,
            "max_drawdown": Decimal("0.139"),
            "created_at": datetime.now(UTC),
        },
        {
            "profit_factor": Decimal("1.521992854765452"),
            "expectancy": Decimal("17.200148212785386"),
            "trade_count": 56,
            "max_drawdown": Decimal("0.03670592788406391"),
            "created_at": datetime.now(UTC) - timedelta(hours=1),
        },
    ]
    monkeypatch.setattr(signal_reviews, "load_candles", lambda *args, **kwargs: [fresh_candle])
    monkeypatch.setattr(signal_reviews, "latest_feature", lambda *args, **kwargs: feature)
    monkeypatch.setattr(signal_reviews, "research_metrics_for_deployment", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("selected candidate metrics should avoid recomputation")))

    review = signal_reviews.generate_signal_review(conn, deployment())

    assert review["profit_factor"] == Decimal("1.521992854765452")
    assert review["expectancy"] == Decimal("17.200148212785386")
    assert review["trade_count"] == 56
    assert review["max_drawdown"] == Decimal("0.03670592788406391")
    assert "Profit factor is below paper threshold 1.25." not in review["failed_rules"]
    assert "Expectancy is not positive." not in review["failed_rules"]
    assert review["simulation_only"] is True


def test_signal_review_service_has_no_live_execution_calls() -> None:
    source = Path(signal_reviews.__file__).read_text()

    forbidden = ["create_order(", "sync_alpaca_candles", "broker", "order submission", "live trading"]

    assert all(term not in source for term in forbidden)
