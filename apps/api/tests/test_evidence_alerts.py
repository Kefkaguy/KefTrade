from decimal import Decimal

from app.services.evidence_alerts import DISCLAIMER, acknowledge_evidence_alert, create_evidence_alert, list_evidence_alerts


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class AlertConn:
    def __init__(self):
        self.alerts = {}
        self.orders = {}
        self.commits = 0

    def execute(self, query, params=None):
        if "SELECT *" in query and "FROM evidence_alerts" in query and "alert_type = %s" in query:
            rows = [
                row for row in self.alerts.values()
                if row["symbol"] == params[0]
                and row["timeframe"] == params[1]
                and row["strategy_id"] == params[2]
                and row["alert_type"] == params[3]
                and row["candle_timestamp"] == params[4]
            ]
            return Result(rows[:1])
        if "INSERT INTO evidence_alerts" in query:
            row = {
                "id": len(self.alerts) + 1,
                "symbol": params[0],
                "timeframe": params[1],
                "strategy_id": params[2],
                "alert_type": params[3],
                "severity": params[4],
                "verdict": params[5],
                "evidence_summary": params[6],
                "matched_rules": params[7],
                "failed_rules": params[8],
                "profit_factor": params[9],
                "expectancy": params[10],
                "trade_count": params[11],
                "max_drawdown": params[12],
                "regime": params[13],
                "candle_timestamp": params[14],
                "acknowledged_at": None,
                "simulation_only": True,
            }
            self.alerts[row["id"]] = row
            return Result([row])
        if "SELECT * FROM evidence_alerts ORDER BY created_at DESC" in query:
            return Result(list(reversed(self.alerts.values()))[: params[0]])
        if "SELECT * FROM evidence_alerts WHERE acknowledged_at IS NULL" in query:
            return Result([row for row in reversed(self.alerts.values()) if row["acknowledged_at"] is None][: params[0]])
        if "UPDATE evidence_alerts SET acknowledged_at" in query:
            row = self.alerts.get(params[0])
            if not row:
                return Result([])
            row["acknowledged_at"] = "2026-01-02T00:00:00Z"
            return Result([row])
        if "paper_orders" in query:
            raise AssertionError("Evidence alerts must not create or inspect paper orders.")
        raise AssertionError(query)

    def commit(self):
        self.commits += 1


def test_evidence_alert_is_simulation_only_and_research_disclaimer() -> None:
    conn = AlertConn()

    alert = create_evidence_alert(
        conn,
        symbol="TSLA",
        timeframe="1h",
        strategy_id="momentum_bull_v2",
        alert_type="entry_setup_review",
        severity="info",
        verdict="Research Opportunity",
        evidence_summary="Setup worth reviewing.",
        matched_rules=["Close above EMA50."],
        failed_rules=[],
        profit_factor=Decimal("1.5"),
        expectancy=Decimal("2.1"),
        trade_count=56,
        max_drawdown=Decimal("0.04"),
        regime="bull_trend",
        candle_timestamp="2026-01-02T15:00:00Z",
    )

    assert alert["simulation_only"] is True
    assert DISCLAIMER in alert["evidence_summary"]
    assert conn.orders == {}


def test_duplicate_alert_reuses_existing_record_without_order_side_effects() -> None:
    conn = AlertConn()
    payload = dict(
        symbol="TSLA",
        timeframe="1h",
        strategy_id="momentum_bull_v2",
        alert_type="avoid_condition",
        severity="warning",
        verdict="Avoid",
        evidence_summary="No setup.",
        candle_timestamp="2026-01-02T15:00:00Z",
    )

    first = create_evidence_alert(conn, **payload)
    second = create_evidence_alert(conn, **payload)

    assert first["id"] == second["id"]
    assert len(conn.alerts) == 1
    assert conn.orders == {}


def test_alerts_can_be_listed_and_acknowledged_without_orders() -> None:
    conn = AlertConn()
    alert = create_evidence_alert(
        conn,
        symbol="TSLA",
        timeframe="1h",
        strategy_id="momentum_bull_v2",
        alert_type="stale_data_warning",
        severity="warning",
        verdict="No Setup",
        evidence_summary="Data stale.",
    )

    rows = list_evidence_alerts(conn)
    acknowledged = acknowledge_evidence_alert(conn, alert["id"])

    assert rows[0]["id"] == alert["id"]
    assert acknowledged["acknowledged_at"] is not None
    assert acknowledged["simulation_only"] is True
    assert conn.orders == {}
