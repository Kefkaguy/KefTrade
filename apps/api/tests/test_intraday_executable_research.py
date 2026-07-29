import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cli.intraday_executable_research import parser
from app.cli import intraday_costs
from app.services.intraday_executable_research import (
    FACTOR_ARCHITECTURES,
    FACTOR_RECIPES,
    _candidate,
    _candidate_symbols,
    _require_sip_calibration,
)


def cost_model(feed="sip"):
    return {
        "calibration_id": 44,
        "feed": feed,
        "observed_round_trip_bps": 2.5,
        "stressed_round_trip_bps": 7.5,
        "conservative_round_trip_bps": 30,
        "by_symbol": {},
        "by_time_slot": {},
    }


def test_every_tradable_factor_has_a_frozen_candidate_recipe():
    assert set(FACTOR_ARCHITECTURES) == set(FACTOR_RECIPES)
    assert "auction_imbalance_pressure" not in FACTOR_ARCHITECTURES


def test_candidate_translation_is_deterministic_and_cost_aware():
    kwargs = {
        "source_factor_run_id": 7,
        "factor_key": "vwap_execution_pressure",
        "architecture": "vwap_execution_pressure_v1",
        "recipe": FACTOR_RECIPES["vwap_execution_pressure"][0],
        "cost_model": cost_model(),
    }
    first = _candidate(**kwargs)
    second = _candidate(**kwargs)

    assert first == second
    assert first.parameters["timeframe"] == "30m"
    assert first.parameters["direction"] == "both"
    assert first.parameters["execution_cost_scenario"] == "stressed"
    assert first.parameters["execution_cost_model"]["calibration_id"] == 44
    assert first.parameters["fee_rate"] == 0
    assert first.parameters["slippage_rate"] == 0


@pytest.mark.parametrize("feed", ["iex", "unknown", ""])
def test_partial_quote_feeds_cannot_freeze_executable_candidates(feed):
    with pytest.raises(ValueError, match="Full-feed"):
        _require_sip_calibration(cost_model(feed))


def test_first_to_last_candidate_requires_both_benchmark_etfs():
    assert _candidate_symbols(
        "first_to_last_half_hour_market_momentum",
        ["AAPL", "SPY", "QQQ"],
    ) == ["SPY", "QQQ"]
    with pytest.raises(ValueError, match="both SPY and QQQ"):
        _candidate_symbols(
            "first_to_last_half_hour_market_momentum",
            ["SPY", "AAPL"],
        )


def test_backend_cli_exposes_no_15m_option():
    args = parser().parse_args(
        ["simulate", "--source-factor-run-id", "9"]
    )
    assert args.command == "simulate"
    assert not hasattr(args, "timeframe")


def test_migration_persists_immutable_funnel_evidence():
    root = Path(__file__).resolve().parents[3]
    sql = (
        root / "database" / "migrations" /
        "064_intraday_executable_research_funnel.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "intraday_quote_ingestion_checkpoints",
        "intraday_executable_candidates",
        "intraday_executable_runs",
        "intraday_family_activations",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CHECK (timeframe = '30m')" in sql
    assert "normalized_order_flow_imbalance" in sql
    assert "prevent_intraday_research_evidence_mutation()" in sql


def test_sip_session_fetch_splits_only_when_pagination_is_incomplete(monkeypatch):
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    calls = []

    async def fake_fetch(symbol, *, start, end, limit, feed):
        calls.append((start, end))
        if len(calls) == 1:
            return 200, [{"t": "whole"}], [
                {"next_page_token_present": True}
            ], None
        return 200, [{"t": str(len(calls))}], [
            {"next_page_token_present": False}
        ], None

    monkeypatch.setattr(intraday_costs, "fetch_stock_quotes", fake_fetch)
    monkeypatch.setattr(
        intraday_costs,
        "_regular_session_windows",
        lambda *_args, **_kwargs: [
            (start, start + timedelta(minutes=30)),
            (start + timedelta(minutes=30), start + timedelta(minutes=60)),
        ],
    )

    rows = asyncio.run(
        intraday_costs._fetch_complete_session_quotes(
            symbol="SPY",
            window_start=start,
            window_end=start + timedelta(hours=6, minutes=30),
            limit=1_000_000,
            feed="sip",
        )
    )

    assert len(calls) == 3
    assert rows == [{"t": "2"}, {"t": "3"}]
