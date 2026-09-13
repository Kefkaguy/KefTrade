from datetime import date, datetime, UTC
from decimal import Decimal
from dataclasses import replace

import pandas as pd
import pytest

from app.services.rug_intraday import generate_candidates, build_dataset, evaluate, IntradayRugStrategy, VERSION
from app.services.labs.intraday.session import trading_schedule
from app.services.research_campaigns import passes_single_market_validation, trades_per_year_for_metrics, run_campaign_job
from app.services.research_learning import normalize_job, build_global_duplicate_intelligence


@pytest.fixture(scope="module")
def candles():
    schedule = trading_schedule(date(2025, 2, 3), date(2025, 6, 30), padding_days=0)
    result = []
    for _, row in schedule.iterrows():
        for ts in pd.date_range(row.market_open, row.market_close, freq="15min", inclusive="left"):
            close = Decimal(100) + Decimal(len(result)) / 100
            result.append({"timestamp": ts.to_pydatetime(), "symbol": "SPY", "timeframe": "15m", "source": "fixture",
                           "open": close - Decimal(".005"), "high": close + Decimal(".001"),
                           "low": close - Decimal(".01"), "close": close, "volume": Decimal(1000)})
    return result


def test_v2_deterministic_semantics_and_valid_domains():
    rows, meta = generate_candidates(max_candidates=100, seed=44)
    repeat, _ = generate_candidates(max_candidates=100, seed=44)
    assert [r.candidate_id for r in rows] == [r.candidate_id for r in repeat]
    assert len({r.canonical_key for r in rows}) == 100
    assert meta["channels"] == {"random_control": 100, "evidence_exploitation": 0}
    assert {r.parameters["direction"] for r in rows} == {"long", "short"}
    assert all(0 <= r.parameters["entry_start_minutes_from_open"] < r.parameters["entry_end_minutes_from_open"] <= 390 for r in rows)
    assert all("proxy" not in repr(r.blocks) for r in rows)


def test_random_control_is_independent_of_learning():
    evidence = [{"candidate_id": str(i), "valid_economic_test": True, "entry": "breakout", "screen_passed": True} for i in range(200)]
    plain, _ = generate_candidates(max_candidates=100, seed=44)
    adapted, meta = generate_candidates(max_candidates=100, seed=44, evidence=evidence)
    assert meta["channels"]["random_control"] == 50
    assert [r.candidate_id for r in plain[:50]] == [r.candidate_id for r in adapted[:50]]
    invalid, meta = generate_candidates(max_candidates=100, seed=44, evidence=[{**e, "valid_economic_test": False} for e in evidence])
    assert [r.candidate_id for r in invalid] == [r.candidate_id for r in plain]


def test_dataset_enforces_dedup_feed_and_complete_sessions(candles):
    with pytest.raises(ValueError, match="duplicate"):
        build_dataset(candles + [candles[0]], "15m")
    with pytest.raises(ValueError, match="mixes providers"):
        build_dataset([{**candles[0], "source": "other"}] + candles[1:], "15m")
    with pytest.raises(ValueError, match="incomplete"):
        build_dataset(candles[:10] + candles[11:], "15m")
    with pytest.raises(ValueError, match="entire exchange sessions"):
        build_dataset([c for c in candles if c["timestamp"].date() != date(2025, 3, 12)], "15m")


def test_dst_and_reserved_tail_not_used_for_features(candles):
    dataset = build_dataset(candles, "15m")
    openings = {f["session_date"]: f["timestamp"] for f in dataset["features"] if f["minutes_from_open"] == 0}
    assert openings[date(2025, 3, 7)].hour == 14
    assert openings[date(2025, 3, 10)].hour == 13
    assert all(str(f["session_date"]) < dataset["reserved_start"] for f in dataset["features"])
    changed_tail = [{**c, "volume": Decimal("999999999")} if str(c["timestamp"].date()) >= dataset["reserved_start"] else c for c in candles]
    assert build_dataset(changed_tail, "15m")["features"] == dataset["features"]


def test_rug_ema_uses_full_causal_history(candles):
    from app.services.strategy_discovery import moving_average
    dataset = build_dataset(candles, "15m")
    index = 1000
    assert dataset["features"][index]["rug_ema_200"] == moving_average(dataset["candles"][:index + 1], 200)
    # Changing later prices must not alter an earlier indicator.
    changed = [dict(c) if i <= index else {**c, **{key: c[key] * 2 for key in ("open", "high", "low", "close")}}
               for i, c in enumerate(candles)]
    assert build_dataset(changed, "15m")["features"][index]["rug_ema_200"] == dataset["features"][index]["rug_ema_200"]


def test_true_atr_not_fixed_percentage_and_volume_uses_prior_sessions():
    candidate = generate_candidates(max_candidates=1, seed=9)[0][0]
    p = {**candidate.parameters, "entry": "breakout", "breakout_lookback": 4, "trend_filter": False,
         "direction": "long", "atr_period": 7, "atr_multiplier": 2, "atr_pct_min": 0,
         "relative_volume_min": 0, "entry_start_minutes_from_open": 0, "entry_end_minutes_from_open": 390}
    recent = [{"open": Decimal(100), "close": Decimal(100), "high": Decimal("100.1"), "low": Decimal("99.9")} for _ in range(8)]
    recent[-1] = {"open": Decimal(100), "close": Decimal("100.2"), "high": Decimal("100.2"), "low": Decimal(100), "timeframe": "15m"}
    feature = {"minutes_from_open": 90, "minutes_to_close": 300}
    decision = IntradayRugStrategy()(recent[-1], feature, recent, p)
    assert decision.signal == "setup"
    assert decision.stop_loss == Decimal("99.8")  # .2 ATR * 2, not 2% of price
    assert IntradayRugStrategy()(recent[-1], {**feature, "minutes_to_close": 15}, recent, p).signal == "avoid"


def test_development_folds_cannot_promote_and_never_hold_overnight(candles):
    dataset = build_dataset(candles, "15m")
    c = generate_candidates(max_candidates=1, seed=9)[0][0]
    c = replace(c, parameters={**c.parameters, "entry": "breakout", "breakout_lookback": 4,
        "direction": "long", "trend_filter": False, "relative_volume_min": 0, "atr_pct_min": 0,
        "entry_start_minutes_from_open": 0, "entry_end_minutes_from_open": 390})
    result = evaluate(c, dataset)
    assert len(result["folds"]) == 3
    assert result["development_only"] is True
    assert not passes_single_market_validation(result)
    assert not result["certification"]["passed"]
    assert result["trades"]
    assert all(t["entry_time"].date() == t["exit_time"].date() for t in result["trades"])
    assert all(t["entry_price"] * t["quantity"] <= Decimal(10000) * 2 for t in result["trades"])
    assert {t["development_fold"] for t in result["trades"]} == {1, 2, 3}
    assert len({t["entry_time"] for t in result["trades"]}) == len(result["trades"])


def test_technical_failures_are_not_economic_rejections():
    for status in ("failed", "blocked_data"):
        job = normalize_job({"id": 1, "status": status, "candidate": {"parameters": {"trend_fast": 20}}})
        assert job["rejected"] is False
        assert job["technical_failure"] is True
        assert "poor_expectancy" not in job["failure_reasons"]


def test_learning_uses_denominators_not_raw_popularity():
    jobs = []
    for value, count, failures in ((20, 100, 40), (50, 25, 24)):
        for i in range(count):
            jobs.append({"status": "rejected" if i < failures else "promoted", "rejected": i < failures,
                         "parameters": {"trend_fast": value}})
    result = build_global_duplicate_intelligence([], jobs)
    first = result["repeated_failed_parameter_regions"][0]
    assert first["region"] == "trend_fast:50"
    assert first["trials"] == 25 and first["failures"] == 24


def test_frequency_uses_only_evaluated_window():
    metrics = {"number_of_trades": 100, "walk_forward": {"train_start": "2020-01-01T00:00:00+00:00",
               "validation_start": "2024-01-01T00:00:00+00:00", "validation_end": "2025-01-01T00:00:00+00:00"}}
    assert 99 < trades_per_year_for_metrics(metrics) < 101


def test_v2_dispatch_fails_closed_without_snapshot():
    candidate = generate_candidates(max_candidates=1, seed=9)[0][0]
    with pytest.raises(ValueError, match="non-frozen"):
        run_campaign_job(None, {"symbol": "SPY", "timeframe": "15m", "candidate": {"parameters": candidate.parameters}})


def test_prior_candidate_ids_are_not_retested():
    first, _ = generate_candidates(max_candidates=25, seed=4)
    ids = {c.candidate_id for c in first}
    replacement, _ = generate_candidates(max_candidates=25, seed=4, excluded_candidate_ids=ids)
    assert ids.isdisjoint({c.candidate_id for c in replacement})


@pytest.mark.parametrize("side", ["long", "short"])
def test_cash_cap_and_gap_through_stop(side):
    from datetime import timedelta
    from app.services.backtester import run_backtest
    from app.services.strategy import BASE_PARAMETERS, StrategyDecision
    rows = [{"symbol": "SPY", "timeframe": "15m", "timestamp": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=15*i),
             "open": Decimal(100), "high": Decimal(100), "low": Decimal(100), "close": Decimal(100), "volume": Decimal(1000)} for i in range(100)]
    opening = Decimal(90 if side == "long" else 110)
    rows[72] = {**rows[72], "open": opening, "low": opening - 1, "high": opening + 1, "close": opening}
    features = [{"timestamp": r["timestamp"]} for r in rows]
    def decision(c, f, recent, p):
        if c["timestamp"] != rows[70]["timestamp"]:
            from app.services.strategy_discovery import avoid
            return avoid("not signal")
        stop = Decimal("99.99") if side == "long" else Decimal("100.01")
        target = Decimal(102 if side == "long" else 98)
        return StrategyDecision("setup", (Decimal(100), Decimal(100)), stop, target, Decimal(200), ["fixture"], direction=side)
    result = run_backtest(rows, features, {**BASE_PARAMETERS, "fee_rate": 0, "slippage_rate": 0,
                         "max_notional_to_equity": 1, "gap_aware_stops": True}, decision)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_price"] * trade["quantity"] == Decimal(10000)
    assert trade["exit_price"] == opening
    assert trade["pnl"] == Decimal(-1000)


def test_opening_range_resets_by_session(candles):
    dataset = build_dataset(candles, "15m")
    for f in dataset["features"]:
        if f["minutes_from_open"] < 30:
            assert f.get("opening_range_30") is None
        else:
            assert f["opening_range_30"] is not None
    assert dataset["features"][0]["slot_relative_volume"] is None
    assert next(f for f in dataset["features"] if f["session_date"] == dataset["development_days"][5])["slot_relative_volume"] == 1


def test_cost_calibration_requires_measured_complete_evidence():
    from app.services.rug_intraday import load_cost_model
    class Conn:
        def execute(self, *_args): return self
        def fetchone(self): return {"id": 1, "feed": "iex", "by_symbol": {"SPY": {}}, "observed_round_trip_bps": 0}
    with pytest.raises(ValueError, match="lacks complete"):
        load_cost_model(Conn(), 1, ["SPY"])


def test_old_run_continuation_keeps_v1(monkeypatch):
    from app.services import research_campaigns as rc
    captured = {}
    monkeypatch.setattr(rc, "create_research_campaign", lambda _c, **kwargs: (captured.update(kwargs) or {"campaign": {"id": 2}}))
    rc.continue_rug_run_after_learning(None, {"universe_key": "research_core_ten", "dataset_id": 88,
        "controls": {"timeframes": ["15m"], "rug": {"enabled": True, "auto_continue": True,
        "batch_size": 100, "batch_candidates": 100, "target_candidates": 1000}}})
    assert captured["rug_version"] == "rug_v1"


def test_repeat_v2_launch_does_not_regenerate_or_queue(monkeypatch):
    from app.services import research_campaigns as rc, research_architecture as ra
    monkeypatch.setattr(rc, "seed_default_universes", lambda _c: None)
    monkeypatch.setattr(rc, "get_universe", lambda *_a: {"name": "Fixture", "assets": ["SPY"]})
    monkeypatch.setattr(ra, "verify_dataset_snapshot", lambda *_a: {"passed": True})
    class Conn:
        def __init__(self): self.queries = []
        def execute(self, sql, args):
            self.queries.append(sql)
            self.last = sql
            return self
        def fetchone(self):
            if "research_dataset_manifests" in self.last:
                return {"id": 88, "mode": "reproducibility"}
            if "research_campaigns WHERE campaign_key" in self.last:
                return {"id": 150, "controls": {"rug": {"version": VERSION}}}
            return None
        def commit(self): pass
    conn = Conn()
    result = rc.create_research_campaign(conn, universe_key="fixture", generator_mode="rug", dataset_id=88,
                                        timeframes=["15m"], max_candidates=25)
    assert result["already_exists"] is True
    assert result["jobs_created"] == 0
    assert not any("INSERT" in q for q in conn.queries)


def test_development_summary_cannot_pass_even_with_profitable_metrics():
    from app.services.research_campaigns import passes_cross_validation
    summary = {"development_only": True, "research_score": 100, "profit_factor": 10, "expectancy": 100,
               "max_drawdown": 0, "trade_count": 1000, "stability": 1, "assets_passed": 5, "timeframes_passed": 1,
               "median_profit_factor": 10, "median_expectancy": 100, "median_max_drawdown": 0,
               "median_variant_trade_count": 200}
    assert not passes_cross_validation(summary)
