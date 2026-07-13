from datetime import UTC, datetime
from decimal import Decimal

from app.services import strategy_discovery
from app.services.strategy_discovery import (
    discovery_dashboard,
    discovered_strategy_decision,
    evolve_discovered_strategies,
    generate_discovery_candidates,
    run_strategy_discovery,
)


class Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class DiscoveryConn:
    def __init__(self):
        self.runs = []
        self.strategies = []
        self.events = []
        self.features = [{"timestamp": datetime(2026, 1, 1, tzinfo=UTC)}]
        self.commits = 0

    def execute(self, query, params=None):
        if "SELECT *" in query and "FROM features" in query:
            return Result(self.features)
        if "INSERT INTO strategy_discovery_runs" in query:
            row = {"id": len(self.runs) + 1, "symbol": params[0], "timeframe": params[1], "simulation_only": True}
            self.runs.append(row)
            return Result([row])
        if "INSERT INTO strategy_discovery_strategies" in query:
            self.strategies.append(
                {
                    "candidate_id": params[0],
                    "family_id": params[1],
                    "parent_candidate_id": params[2],
                    "discovery_run_id": params[3],
                    "symbol": params[4],
                    "timeframe": params[5],
                    "generation": params[6],
                    "blocks": jsonb(params[7]),
                    "parameters": jsonb(params[8]),
                    "complexity": params[9],
                    "metrics": jsonb(params[10]),
                    "validation_metrics": jsonb(params[11]),
                    "walk_forward_metrics": jsonb(params[12]),
                    "out_of_sample_metrics": jsonb(params[13]),
                    "regime_analysis": jsonb(params[14]),
                    "feature_correlations": jsonb(params[15]),
                    "paper_readiness": jsonb(params[16]),
                    "research_score": params[17],
                    "status": params[18],
                    "failure_reasons": jsonb(params[19]),
                    "explanation": params[20],
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "simulation_only": True,
                }
            )
            return Result([])
        if "SELECT status, COUNT(*) AS count" in query:
            counts = {}
            for row in self.strategies:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            return Result([{"status": key, "count": value} for key, value in counts.items()])
        if "FROM strategy_discovery_strategies" in query and "WHERE status = 'promoted'" in query:
            promoted = [row for row in self.strategies if row["status"] == "promoted"]
            return Result(promoted[: params[0]])
        if "FROM strategy_discovery_strategies" in query and "ORDER BY research_score" in query:
            return Result(sorted(self.strategies, key=lambda row: row["research_score"], reverse=True)[: params[0]])
        if "FROM strategy_discovery_strategies" in query and "ORDER BY created_at" in query:
            return Result(self.strategies[-params[0] :])
        if "INSERT INTO strategy_discovery_events" in query:
            self.events.append({"candidate_id": params[0], "parent_candidate_id": params[1], "event_type": "variant_generated", "details": jsonb(params[2]), "created_at": datetime(2026, 1, 1, tzinfo=UTC)})
            return Result([])
        if "FROM strategy_discovery_events" in query:
            return Result(self.events[-params[0] :])
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def jsonb(value):
    return getattr(value, "obj", value)


def test_generate_discovery_candidates_is_deterministic_and_filtered() -> None:
    first = generate_discovery_candidates(max_candidates=12)
    second = generate_discovery_candidates(max_candidates=12)

    assert [row.candidate_id for row in first] == [row.candidate_id for row in second]
    assert len({row.canonical_key for row in first}) == len(first)
    assert all(row.complexity <= 10 for row in first)


def test_discovered_strategy_decision_uses_rule_blocks() -> None:
    candles = []
    for index in range(220):
        close = Decimal(100 + index)
        candles.append({"open": close, "high": close + 2, "low": close - 2, "close": close, "volume": Decimal("1000")})
    candidate = generate_discovery_candidates(max_candidates=1)[0]
    params = dict(candidate.parameters)
    params.update({"entry": "trend_continuation", "momentum": "roc", "returns_5_min": 0.01, "volume_change_min": -0.5})

    decision = discovered_strategy_decision(
        candles[-1],
        {"returns_5": Decimal("0.03"), "volume_change": Decimal("0.1"), "volatility_20": Decimal("0.02"), "rsi_14": Decimal("60")},
        candles,
        params,
    )

    assert decision.signal == "setup"
    assert decision.stop_loss is not None
    assert "deterministic rule blocks" in decision.explanation[0]


def test_run_strategy_discovery_persists_research_only_rows(monkeypatch) -> None:
    conn = DiscoveryConn()
    monkeypatch.setattr(strategy_discovery, "sync_market_regimes", lambda *args, **kwargs: None)
    monkeypatch.setattr(strategy_discovery, "load_candles", lambda *args, **kwargs: [{"timestamp": datetime(2026, 1, 1, tzinfo=UTC)} for _ in range(120)])
    monkeypatch.setattr(strategy_discovery, "load_regimes", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        strategy_discovery,
        "run_backtest",
        lambda *args, **kwargs: {
            "metrics": {
                "profit_factor": 1.2,
                "expectancy_per_trade": 5,
                "max_drawdown": 0.04,
                "number_of_trades": 25,
                "sharpe_ratio": 0.4,
                "win_rate": 0.58,
                "longest_losing_streak": 2,
                "walk_forward": {"enabled": True},
            },
            "trades": [],
        },
    )

    result = run_strategy_discovery(conn, "BTCUSDT", "4h", max_candidates=3)

    assert result["evaluated"] == 3
    assert conn.strategies[0]["simulation_only"] is True
    assert conn.strategies[0]["status"] == "promoted"
    assert conn.commits == 1


def test_dashboard_and_evolution_use_stored_evidence_only() -> None:
    conn = DiscoveryConn()
    parent = generate_discovery_candidates(max_candidates=1)[0]
    conn.strategies.append(
        {
            "candidate_id": parent.candidate_id,
            "family_id": parent.family_id,
            "parent_candidate_id": None,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "generation": 1,
            "blocks": parent.blocks,
            "parameters": parent.parameters,
            "metrics": {"profit_factor": 1.2, "number_of_trades": 30},
            "research_score": 3.4,
            "status": "promoted",
            "failure_reasons": [],
            "explanation": "Stored validation metrics met promotion gates.",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )

    evolved = evolve_discovered_strategies(conn, limit=1)
    dashboard = discovery_dashboard(conn, limit=5)

    assert evolved["variants_generated"] == 3
    assert dashboard["summary"]["promoted"] == 1
    assert dashboard["successful_rule_combinations"][0]["count"] == 1
