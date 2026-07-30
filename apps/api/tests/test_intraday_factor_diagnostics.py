from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json

import app.services.intraday_factor_diagnostics as diagnostics
from app.services.intraday_factor_diagnostics import (
    FACTOR_SPECS,
    evaluate_forward_confirmation,
    evaluate_factor_discovery,
    factor_research_readiness,
    first_to_last_half_hour_observations,
    overnight_gap_acceptance_absorption_observations,
    persist_factor_run,
    vwap_execution_pressure_observations,
)
from psycopg.types.json import Jsonb


def market_candles(symbol: str, sessions: int = 120):
    rows = []
    previous_close = 100.0
    start = date(2025, 1, 2)
    for session_index in range(sessions):
        day = start + timedelta(days=session_index)
        direction = 1 if session_index % 2 == 0 else -1
        opening_return = direction * (0.006 + (session_index % 3) * 0.0002)
        first_close = previous_close * (1 + opening_return)
        for bar_index in range(13):
            timestamp = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC) + timedelta(minutes=30 * bar_index)
            open_price = previous_close if bar_index == 0 else first_close
            target = direction * (0.005 + (session_index % 5) * 0.0001) if bar_index == 12 else 0
            close = open_price * (1 + target) if bar_index == 12 else first_close
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": "30m",
                    "timestamp": timestamp,
                    "open": Decimal(str(open_price)),
                    "high": Decimal(str(max(open_price, close) * 1.001)),
                    "low": Decimal(str(min(open_price, close) * 0.999)),
                    "close": Decimal(str(close)),
                    "volume": Decimal("100000"),
                }
            )
        previous_close = float(rows[-1]["close"])
    return rows


def test_first_to_last_observation_uses_opening_signal_and_last_bar_target():
    candles = {"SPY": market_candles("SPY", sessions=12)}

    observations = first_to_last_half_hour_observations(candles, timeframe="30m")

    assert len(observations) == 11
    assert observations[0]["score"] < 0
    assert observations[0]["target_return"] < 0


def test_discovery_never_calculates_withheld_confirmation_metrics():
    candles = {
        "SPY": market_candles("SPY"),
        "QQQ": market_candles("QQQ"),
    }
    cost_model = {
        "observed_round_trip_bps": 5,
        "stressed_round_trip_bps": 20,
        "conservative_round_trip_bps": 30,
    }

    result = evaluate_factor_discovery(
        candles,
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum", "liquidity_shock_reversal"],
        cost_model=cost_model,
    )

    market = result["factors"]["first_to_last_half_hour_market_momentum"]
    assert result["confirmation_data_accessed"] is False
    assert market["confirmation"]["status"] == "locked"
    assert "confirmation" not in market["confirmation"] or "rank_ic" not in market["confirmation"]
    assert market["validation"]["observations"] >= 50
    assert market["cost_clearance"]["clears_stressed"] is True
    assert result["factors"]["liquidity_shock_reversal"]["status"] == "blocked_missing_quote_data"


def test_candle_factor_readiness_does_not_require_quote_or_auction_gates():
    readiness = factor_research_readiness(
        FACTOR_SPECS["first_to_last_half_hour_market_momentum"],
        data_readiness={"candle_research_ready": True, "execution_research_ready": False},
        institutional_readiness={
            "institutional_candle_ready": True,
            "gates": {"frozen_microstructure_80pct_coverage": False},
            "auction_imbalances": {"ready": False},
        },
    )

    assert readiness["ready"] is True
    assert "institutional_frozen_sip_coverage" not in readiness["gates"]
    assert "auction_imbalance_ready" not in readiness["gates"]


def test_quote_factor_readiness_requires_frozen_sip_coverage():
    readiness = factor_research_readiness(
        FACTOR_SPECS["liquidity_shock_reversal"],
        data_readiness={"candle_research_ready": True, "execution_research_ready": False},
        institutional_readiness={
            "institutional_candle_ready": True,
            "gates": {"frozen_microstructure_80pct_coverage": False},
            "auction_imbalances": {"ready": False},
        },
    )

    assert readiness["ready"] is False
    assert readiness["limitations"] == [
        "snapshot_frozen_quote_coverage",
        "institutional_frozen_sip_coverage",
    ]


def test_forward_confirmation_can_pass_candle_factor_before_production_tca(monkeypatch):
    def passing_metrics(*args, **kwargs):
        return {
            "observations": 75,
            "distinct_sessions": 25,
            "distinct_symbols": 2,
            "rank_ic": 0.21,
            "mean_cross_sectional_rank_ic": None,
            "rank_ic_periods": 0,
            "gross_directional_edge_bps": 12.0,
            "net_stressed_edge_bps": 8.0,
            "day_clustered_t_statistic": 3.5,
            "two_sided_normal_p_value": 0.01,
            "measurable": True,
            "evidence_quality": {"selection_adjusted_signal": True},
            "net_evidence_quality": {"selection_adjusted_signal": True},
        }

    monkeypatch.setattr(
        diagnostics,
        "dataset_research_readiness",
        lambda *args, **kwargs: {
            "candle_research_ready": True,
            "execution_research_ready": False,
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "cost_model_readiness",
        lambda *args, **kwargs: {
            "research_cost_available": True,
            "production_cost_ready": False,
        },
    )
    monkeypatch.setattr(diagnostics, "factor_metrics", passing_metrics)

    result = evaluate_forward_confirmation(
        {"SPY": market_candles("SPY", sessions=3)},
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum"],
        cost_model={"stressed_round_trip_bps": 4.0},
        institutional_data_readiness={
            "institutional_candle_ready": True,
            "gates": {"frozen_microstructure_80pct_coverage": False},
            "auction_imbalances": {"ready": False},
        },
    )

    assert result["passed_locked_confirmation"] == [
        "first_to_last_half_hour_market_momentum"
    ]
    assert (
        result["factors"]["first_to_last_half_hour_market_momentum"][
            "factor_research_readiness"
        ]["ready"]
        is True
    )


class PersistResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class JsonDumpingPersistConn:
    def __init__(self):
        self.committed = False

    def execute(self, query, params=None):
        for value in params or ():
            if isinstance(value, Jsonb):
                json.dumps(value.obj)
        if query.strip().startswith("INSERT INTO intraday_factor_diagnostic_runs"):
            return PersistResult({"id": 42})
        return PersistResult(None)

    def commit(self):
        self.committed = True


def test_persist_factor_run_sanitizes_dates_before_jsonb():
    result = {
        "effective_trials": 1,
        "split_boundaries": {
            "discovery_start": date(2025, 1, 2),
            "discovery_end": date(2025, 2, 3),
        },
        "cost_model": {
            "window_start": datetime(2026, 7, 22, tzinfo=UTC),
            "stressed_round_trip_bps": Decimal("1.5"),
        },
        "factors": {
            "first_to_last_half_hour_market_momentum": {
                "status": "measured",
                "confirmation": {
                    "timestamp": datetime(2026, 7, 30, 20, 30, tzinfo=UTC),
                    "session_date": date(2026, 7, 30),
                },
            }
        },
    }

    run_id = persist_factor_run(
        JsonDumpingPersistConn(),
        mode="discovery",
        dataset_id=77,
        source_run_id=None,
        timeframe="30m",
        factor_keys=["first_to_last_half_hour_market_momentum"],
        symbols=["SPY"],
        result=result,
        spec_hash="abc123",
    )

    assert run_id == 42


def test_gap_acceptance_requires_elevated_opening_participation():
    first = market_candles("AAPL", sessions=3)
    by_session = {}
    for row in first:
        by_session.setdefault(row["timestamp"].date(), []).append(row)
    sessions = [sorted(rows, key=lambda row: row["timestamp"]) for _, rows in sorted(by_session.items())]
    for row in sessions[1]:
        row["session_relative_volume"] = Decimal("2")
    previous_close = float(sessions[0][-1]["close"])
    sessions[1][0]["open"] = Decimal(str(previous_close * 1.01))
    sessions[1][0]["close"] = sessions[1][0]["open"]
    sessions[1][1]["open"] = sessions[1][0]["open"]
    sessions[1][1]["close"] = Decimal(str(float(sessions[1][0]["open"]) * 1.002))
    sessions[1][2]["open"] = sessions[1][1]["close"]
    sessions[1][2]["close"] = Decimal(str(float(sessions[1][2]["open"]) * 1.002))

    observations = overnight_gap_acceptance_absorption_observations(
        {"AAPL": first},
        timeframe="30m",
    )

    assert any(row["flow_state"] == "acceptance" for row in observations)


def test_vwap_pressure_uses_volume_curve_and_same_session_next_bar():
    rows = market_candles("AAPL", sessions=2)
    for row in rows:
        row["session_vwap"] = Decimal(str(float(row["close"]) * 0.995))
        row["session_relative_volume"] = Decimal("2")

    observations = vwap_execution_pressure_observations(
        {"AAPL": rows},
        timeframe="30m",
    )

    assert observations
    assert all(row["score"] > 0 for row in observations)
