import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
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


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_completed"),
    [
        (None, "completed", True),
        ("provider failure", "failed", False),
    ],
)
def test_quote_checkpoint_completion_uses_typed_boolean(
    error,
    expected_status,
    expected_completed,
):
    class RecordingConnection:
        def __init__(self):
            self.query = None
            self.params = None
            self.committed = False

        def execute(self, query, params):
            self.query = query
            self.params = params

        def commit(self):
            self.committed = True

    conn = RecordingConnection()
    intraday_costs._checkpoint_finished(
        conn,
        symbol="AAPL",
        feed="sip",
        session_date=datetime(2026, 7, 28, tzinfo=UTC).date(),
        quote_rows=100,
        microstructure_rows=13,
        error=error,
    )

    assert "%s::boolean" in conn.query
    assert conn.params[0] == expected_status
    assert conn.params[4] is expected_completed
    assert conn.committed


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


def test_sip_session_fetch_splits_dense_windows_recursively(monkeypatch):
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    calls = []

    async def fake_fetch(symbol, *, start, end, limit, feed):
        calls.append((start, end))
        if (end - start) == timedelta(minutes=30):
            return 200, [{"t": "dense"}], [{"next_page_token_present": True}], None
        return 200, [{"t": f"{start.isoformat()}-{end.isoformat()}"}], [
            {"next_page_token_present": False}
        ], None

    monkeypatch.setattr(intraday_costs, "fetch_stock_quotes", fake_fetch)
    monkeypatch.setattr(
        intraday_costs,
        "_regular_session_windows",
        lambda *_args, **_kwargs: [
            (start, start + timedelta(minutes=30)),
        ],
    )

    rows = asyncio.run(
        intraday_costs._fetch_complete_session_quotes(
            symbol="SPY",
            window_start=start,
            window_end=start + timedelta(hours=6, minutes=30),
            limit=1_000_000,
            feed="sip",
            min_split_seconds=60,
        )
    )

    assert calls == [
        (start, start + timedelta(minutes=30)),
        (start, start + timedelta(minutes=15)),
        (start + timedelta(minutes=15), start + timedelta(minutes=30)),
    ]
    assert rows == [
        {"t": f"{start.isoformat()}-{(start + timedelta(minutes=15)).isoformat()}"},
        {
            "t": (
                f"{(start + timedelta(minutes=15)).isoformat()}-"
                f"{(start + timedelta(minutes=30)).isoformat()}"
            )
        },
    ]


def test_quote_fetch_retries_rate_limits(monkeypatch):
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    calls = 0
    sleeps = []

    async def fake_fetch(symbol, *, start, end, limit, feed):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/AAPL/quotes")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("Too Many Requests", request=request, response=response)
        return 200, [{"t": "ok"}], [{"next_page_token_present": False}], "req_2"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(intraday_costs, "fetch_stock_quotes", fake_fetch)
    monkeypatch.setattr(intraday_costs.asyncio, "sleep", fake_sleep)

    result = asyncio.run(
        intraday_costs._fetch_stock_quotes_with_rate_limit_retry(
            "AAPL",
            start=start,
            end=start + timedelta(minutes=30),
            limit=100,
            feed="sip",
            retries=2,
            base_sleep=3,
        )
    )

    assert calls == 2
    assert sleeps == [3]
    assert result[1] == [{"t": "ok"}]


def test_quote_fetch_uses_retry_after_header(monkeypatch):
    request = httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/AAPL/quotes")
    response = httpx.Response(429, headers={"Retry-After": "11"}, request=request)
    error = httpx.HTTPStatusError("Too Many Requests", request=request, response=response)

    assert intraday_costs._retry_after_seconds(error) == 11


def test_sync_quotes_cli_exposes_rate_limit_controls():
    args = intraday_costs.parser().parse_args(
        [
            "sync-quotes",
            "--symbols",
            "AAPL",
            "--rate-limit-retries",
            "4",
            "--rate-limit-base-sleep",
            "7.5",
            "--request-pause-seconds",
            "2",
        ]
    )

    assert args.rate_limit_retries == 4
    assert args.rate_limit_base_sleep == 7.5
    assert args.request_pause_seconds == 2
