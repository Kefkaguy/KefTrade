"""Versioned, session-bound RUG research. Never certifies deployable strategies.

V1 evidence remains replayable. V2 runs development folds and cost ablations;
its reserved tail is NOT called untouched without an independent exposure audit.
"""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from statistics import mean
from typing import Any

import pandas as pd

from app.services.strategy import BASE_PARAMETERS, ExecutionConstraints, StrategyDecision
from app.services.strategy_discovery import DiscoveryCandidate, avoid, moving_average, rsi_value

VERSION = "rug_v2_intraday"
TIMEFRAMES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}
WINDOWS = ((0, 390), (0, 90), (30, 180), (90, 300), (240, 390))


def is_rug_v2(payload: dict[str, Any]) -> bool:
    return (payload.get("parameters") or {}).get("strategy_architecture") == VERSION


def generate_candidates(*, max_candidates: int, seed: int, batch_index: int = 0,
                        evidence: list[dict[str, Any]] | None = None,
                        cost_model: dict[str, Any] | None = None,
                        excluded_candidate_ids: set[str] | None = None) -> tuple[list[DiscoveryCandidate], dict[str, Any]]:
    if max_candidates < 1 or batch_index < 0:
        raise ValueError("RUG requires a positive batch size and non-negative batch index")
    from app.services.rug import FAST_PERIODS, SLOW_PERIODS, RSI_PERIODS
    rng = random.Random(f"{VERSION}|{seed}|{batch_index}")
    pool: dict[str, DiscoveryCandidate] = {}
    for _ in range(max_candidates * 30):
        fast = rng.choice(FAST_PERIODS)
        start, end = rng.choice(WINDOWS)
        entry = rng.choice(("breakout", "opening_range", "rsi_reversal", "ema_continuation"))
        p = {**BASE_PARAMETERS, "strategy_architecture": VERSION, "generator_version": VERSION,
             "trend_fast": fast, "trend_slow": rng.choice([s for s in SLOW_PERIODS if s > fast]),
             "trend_filter": rng.choice((True, False)), "direction": rng.choice(("long", "short")),
             "rsi_period": rng.choice(RSI_PERIODS), "rsi_threshold": rng.choice((25, 30, 35, 40)),
             "entry": entry, "breakout_lookback": rng.choice((4, 8, 12, 20)),
             "opening_range_minutes": rng.choice((30, 60)), "atr_period": rng.choice((7, 14, 20)),
             "atr_multiplier": rng.choice((0.75, 1, 1.5, 2, 3)),
             "atr_pct_min": rng.choice((0, 0.0005, 0.001)),
             "relative_volume_min": rng.choice((0, 0.75, 1, 1.5)),
             "entry_start_minutes_from_open": start, "entry_end_minutes_from_open": end,
             "risk_reward": rng.choice((1, 1.5, 2, 3)), "max_holding_bars": rng.choice((2, 4, 8, 12)),
             "recent_candle_window_bars": 240, "max_notional_to_equity": 1.0,
             "gap_aware_stops": True, "frequency_screen_min_opportunities": 0,
             "holding_bar_convention": "entry_bar_is_one",
             "exit_timestamp_convention": "bar_close_for_forced_exits",
             "indicator_convention": "causal_full_history_ema_v1",
             "cost_model_status": "uncalibrated_conservative_assumption",
             "minimum_trades_per_year": 50, "development_only": True}
        if entry == "ema_continuation":
            p["trend_filter"] = True  # the EMA entry always enforces this rule
        if cost_model is not None:
            p.update(execution_cost_model=cost_model, execution_cost_calibration_id=cost_model["id"],
                     cost_model_status="persisted_quote_and_fill_calibration")
        # No nuisance dimensions in semantic identities. Unused labels must
        # not create fake diversity or consume repeated tests.
        if entry != "rsi_reversal":
            p.pop("rsi_period"); p.pop("rsi_threshold")
        if entry != "breakout":
            p.pop("breakout_lookback")
        if entry != "opening_range":
            p.pop("opening_range_minutes")
        if not p["trend_filter"] and entry != "ema_continuation":
            p.pop("trend_fast"); p.pop("trend_slow")
        key = repr(sorted(p.items()))
        digest = sha256(key.encode()).hexdigest()[:20]
        if f"rug2_{digest}" in (excluded_candidate_ids or set()):
            continue
        blocks = {"trend": "ema" if p["trend_filter"] else "none", "momentum": entry,
                  "volatility": "true_range_sma_atr", "volume": "same_slot_relative_volume",
                  "entry": entry, "exit": "atr_stop_rr_target_or_session_time_exit"}
        pool.setdefault(key, DiscoveryCandidate(f"rug2_{digest}", f"rug2_{entry}", None,
                                               batch_index + 1, blocks, p, 4, key))
        if len(pool) >= max_candidates * 3:
            break
    rows = list(pool.values())
    if len(rows) < max_candidates:
        raise ValueError("Unique RUG search budget exhausted; do not silently repeat candidates")
    rng.shuffle(rows)
    # Select a fixed random control BEFORE adaptive ranking; its membership
    # is independent of the evidence. Adaptation uses comparable completed
    # v2 development experiments only (loader pins dataset and timeframe).
    evidence = [e for e in (evidence or []) if e.get("valid_economic_test") is True]
    evidence_by_id = {str(e["candidate_id"]): e for e in evidence}
    evidence = list(evidence_by_id.values())
    adaptive_count = int(max_candidates * min(0.5, len(evidence) / 400)) if len(evidence) >= 40 else 0
    control_count = max_candidates - adaptive_count
    selected = [(c, "random_control") for c in rows[:control_count]]
    rates: dict[str, list[float]] = defaultdict(list)
    for e in evidence:
        rates[str(e["entry"])].append(float(bool(e.get("screen_passed"))))
    def score(c: DiscoveryCandidate) -> float:
        outcomes = rates.get(c.parameters["entry"], [])
        return (sum(outcomes) + 1) / (len(outcomes) + 2)  # shrink small samples, normalize attempts
    ranked = sorted(rows[control_count:], key=score, reverse=True)
    selected += [(c, "evidence_exploitation") for c in ranked[:adaptive_count]]
    candidates = [replace(c, parameters={**c.parameters, "rug_seed": seed, "rug_batch_index": batch_index,
                                         "rug_channel": channel}) for c, channel in selected]
    return candidates, {"generator_version": VERSION, "mode": "rug", "development_only": True,
                        "promotion_authority": False, "channels": {"random_control": control_count,
                        "evidence_exploitation": adaptive_count}, "valid_learning_candidates": len(evidence),
                        "learning_effectiveness": "unproven_requires_control_comparison"}


class IntradayRugStrategy:
    execution_constraints = ExecutionConstraints(flat_by_session_close=True)

    def __call__(self, candle, feature, recent, params):
        p = params
        elapsed = feature.get("minutes_from_open")
        if elapsed is None or not p["entry_start_minutes_from_open"] <= elapsed < p["entry_end_minutes_from_open"]:
            return avoid("Outside exchange-session entry window")
        if feature["minutes_to_close"] <= 2 * TIMEFRAMES[candle["timeframe"]]:
            return avoid("Insufficient session time for next-bar entry")
        close = Decimal(str(candle["close"]))
        side = p["direction"]
        sign = Decimal(1 if side == "long" else -1)
        n = int(p["atr_period"])
        if len(recent) <= n:
            return avoid("ATR warmup")
        ranges = [max(Decimal(str(b["high"])) - Decimal(str(b["low"])),
                      abs(Decimal(str(b["high"])) - Decimal(str(a["close"]))),
                      abs(Decimal(str(b["low"])) - Decimal(str(a["close"]))))
                  for a, b in zip(recent[-n-1:-1], recent[-n:])]
        atr = sum(ranges) / n
        if atr <= 0 or atr / close < Decimal(str(p["atr_pct_min"])):
            return avoid("Actual ATR filter")
        if p["relative_volume_min"] > 0 and (feature.get("slot_relative_volume") is None or
                feature["slot_relative_volume"] < p["relative_volume_min"]):
            return avoid("Same-time-of-day volume filter")
        entry = p["entry"]
        if p["trend_filter"] or entry == "ema_continuation":
            if p.get("indicator_convention") == "causal_full_history_ema_v1":
                fast = feature.get(f"rug_ema_{p['trend_fast']}")
                slow = feature.get(f"rug_ema_{p['trend_slow']}")
            else:
                fast = moving_average(recent, p["trend_fast"], "ema")
                slow = moving_average(recent, p["trend_slow"], "ema")
            if fast is None or slow is None or sign * (fast - slow) <= 0 or sign * (close - slow) <= 0:
                return avoid("EMA direction filter")
        if entry == "rsi_reversal":
            rsi = rsi_value(recent, p["rsi_period"])
            if rsi is None or (rsi > p["rsi_threshold"] if side == "long" else rsi < 100 - p["rsi_threshold"]):
                return avoid("RSI reversal filter")
        elif entry in {"breakout", "opening_range"}:
            if entry == "opening_range":
                duration = p["opening_range_minutes"]
                if elapsed < duration:
                    return avoid("Opening range not complete")
                prior = feature.get(f"opening_range_{duration}")
                if prior is None:
                    return avoid("Opening range unavailable")
                low, high = prior
            else:
                n = p["breakout_lookback"]
                if len(recent) <= n:
                    return avoid("Breakout warmup")
                prior = recent[-n-1:-1]
                low, high = min(Decimal(str(b["low"])) for b in prior), max(Decimal(str(b["high"])) for b in prior)
            if (close <= high if side == "long" else close >= low):
                return avoid("Breakout threshold")
        distance = atr * Decimal(str(p["atr_multiplier"]))
        stop = close - sign * distance
        rr = Decimal(str(p["risk_reward"]))
        if stop <= 0:
            return avoid("Invalid positive-price stop")
        return StrategyDecision("setup", (Decimal(str(candle["low"])), Decimal(str(candle["high"]))),
                                stop, close + sign * distance * rr, rr, ["RUG v2 session-bound setup"], direction=side)


def build_dataset(candles: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    """Fail closed on mixed feeds, duplicate bars, and incomplete RTH sessions.

    No interpolation or source preference is silently applied. The final 20%
    of sessions is withheld from feature calculation and all search results.
    """
    from app.services.features import calculate_features
    from app.services.labs.intraday.session import trading_schedule, assign_sessions
    from app.services.labs.intraday.dataset import build_session_end_index
    if timeframe not in TIMEFRAMES or not candles:
        raise ValueError("RUG v2 needs nonempty 1m/3m/5m/15m/30m candles")
    minutes = TIMEFRAMES[timeframe]
    rows = sorted((dict(c) for c in candles), key=lambda c: c["timestamp"])
    timestamps = [r["timestamp"] for r in rows]
    if any(t.tzinfo is None or t.utcoffset() is None for t in timestamps):
        raise ValueError("RUG dataset requires timezone-aware timestamps")
    if len(set(timestamps)) != len(rows):
        raise ValueError("RUG dataset has duplicate timestamps; pin one consistent feed")
    if not rows[0].get("source") or len({r.get("source") for r in rows}) != 1:
        raise ValueError("RUG dataset mixes providers; create a single-feed snapshot")
    for row in rows:
        o, h, l, c, v = [Decimal(str(row[k])) for k in ("open", "high", "low", "close", "volume")]
        if not all(x.is_finite() for x in (o, h, l, c, v)) or min(o, l, c) <= 0 or v < 0 or h < max(o, c, l) or l > min(o, c):
            raise ValueError("RUG dataset has invalid OHLCV")
    schedule = trading_schedule(timestamps[0].date(), timestamps[-1].date())
    metadata = assign_sessions(pd.Series(timestamps), schedule).to_dict("records")
    sessions: dict[Any, list[tuple[dict, dict]]] = defaultdict(list)
    for c, f in zip(rows, metadata):
        if f["session_date"] is not None:
            sessions[f["session_date"]].append((c, f))
    for day, bars in sessions.items():
        expected = pd.date_range(bars[0][1]["market_open"], bars[0][1]["market_close"], freq=f"{minutes}min", inclusive="left")
        if list(expected) != [b[0]["timestamp"] for b in bars]:
            raise ValueError(f"RUG incomplete/misaligned RTH session {day}; repair data before learning")
    days = sorted(sessions)
    if len(days) < 60:
        raise ValueError("RUG v2 requires at least 60 complete sessions")
    expected_days = [d.date() for d in schedule.index if days[0] <= d.date() <= days[-1]]
    if days != expected_days:
        raise ValueError("RUG dataset is missing entire exchange sessions")
    cutoff = int(len(days) * .8)
    development = [(c, f) for day in days[:cutoff] for c, f in sessions[day]]
    dev_candles = [c for c, _ in development]
    features = calculate_features(dev_candles)
    from app.services.rug import FAST_PERIODS, SLOW_PERIODS
    # SMA-seeded recursive EMAs, computed once per dataset from past closes.
    # The recent ATR/breakout window must not reset the EMA's history.
    closes = [Decimal(str(c["close"])) for c in dev_candles]
    for period in sorted(set(FAST_PERIODS) | set(SLOW_PERIODS)):
        ema = None
        alpha = Decimal(2) / (period + 1)
        for index, (close, feature) in enumerate(zip(closes, features)):
            if index == period - 1:
                ema = sum(closes[:period]) / period
            elif ema is not None:
                ema += (close - ema) * alpha
            feature[f"rug_ema_{period}"] = ema
    slot_history: dict[float, list[float]] = defaultdict(list)
    opening: dict[Any, list[dict]] = defaultdict(list)
    for (c, meta), f in zip(development, features):
        f.update(meta)
        slot = meta["minutes_from_open"]
        history = slot_history[slot][-20:]
        f["slot_relative_volume"] = float(c["volume"]) / mean(history) if len(history) >= 5 and mean(history) > 0 else None
        slot_history[slot].append(float(c["volume"]))
        day_rows = opening[meta["session_date"]]
        for duration in (30, 60):
            prior = [b for b in day_rows if b["minutes_from_open"] < duration]
            if slot >= duration and prior:
                f[f"opening_range_{duration}"] = (min(Decimal(str(b["low"])) for b in prior), max(Decimal(str(b["high"])) for b in prior))
        day_rows.append({**c, "minutes_from_open": slot})
    combined = [{"candle": c, "feature": f} for c, f in zip(dev_candles, features)]
    return {"candles": dev_candles, "features": features, "session_end_index": build_session_end_index(combined),
            "development_days": days[:cutoff], "reserved_start": str(days[cutoff]),
            "reserved_tail_status": "excluded_from_v2_search_prior_exposure_unverified",
            "coverage": {"complete_sessions": len(days), "development_sessions": cutoff, "sources": sorted(str(r.get("source")) for r in rows[:1])}}


def evaluate(candidate: DiscoveryCandidate, dataset: dict[str, Any]) -> dict[str, Any]:
    from app.services.backtester import run_backtest
    from app.services.strategy_research import score_metrics
    days = dataset["development_days"]
    calibrated = isinstance(candidate.parameters.get("execution_cost_model"), dict)
    # Three expanding-history development folds, disjoint evaluation sessions.
    boundaries = [int(len(days) * x) for x in (.4, .6, .8, 1)]
    folds = []
    all_trades = []
    for start, end in zip(boundaries, boundaries[1:]):
        indices = [i for i, f in enumerate(dataset["features"]) if f["session_date"] < days[end-1] or f["session_date"] == days[end-1]]
        stop = indices[-1] + 1
        first = next(i for i, f in enumerate(dataset["features"]) if f["session_date"] == days[start])
        p = {**candidate.parameters, "walk_forward_train_ratio": first / stop}
        scenarios = {}
        for scenario, multiplier in (("frictionless_diagnostic", 0), ("baseline_costs", 1), ("cost_stress", 2)):
            params = {**p, "fee_rate": p["fee_rate"] * multiplier, "slippage_rate": p["slippage_rate"] * multiplier,
                      "research_execution_start_index": first}
            if multiplier == 0:
                params.pop("execution_cost_model", None)
            elif calibrated:
                params["execution_cost_scenario"] = "observed" if multiplier == 1 else "stressed"
            result = run_backtest(dataset["candles"][:stop], dataset["features"][:stop], params,
                                  IntradayRugStrategy(), session_end_index=dataset["session_end_index"][:stop])
            scenarios[scenario] = result["metrics"]
            if scenario == "baseline_costs":
                for trade in result["trades"]:
                    trade["development_fold"] = len(folds) + 1
                all_trades.extend(result["trades"])
        folds.append({"start": str(days[start]), "end": str(days[end-1]), "scenarios": scenarios})
    from app.services.backtester import calculate_metrics
    initial = Decimal(str(candidate.parameters["initial_equity"]))
    # Fold-reset capital, not a purported compounded portfolio return.
    pnl = sum((t["pnl"] for t in all_trades), Decimal(0))
    metrics = calculate_metrics(initial, initial + pnl, all_trades, [initial, initial + pnl])
    metrics["max_drawdown"] = max(f["scenarios"]["baseline_costs"]["max_drawdown"] for f in folds)
    metrics["aggregation"] = "pooled_trade_statistics_fold_reset_capital_not_portfolio_return"
    metrics["walk_forward"] = {"enabled": True, "kind": "three_development_folds_fixed_rules",
                                "validation_start": folds[0]["start"], "validation_end": folds[-1]["end"],
                                "untouched_out_of_sample": False}
    elapsed_years = (days[-1] - days[boundaries[0]]).days / 365.25
    frequency = len(all_trades) / elapsed_years if elapsed_years > 0 else 0
    reasons = []
    if len(all_trades) < 60: reasons.append("insufficient_trades")
    if frequency < candidate.parameters["minimum_trades_per_year"]: reasons.append("insufficient_opportunity")
    if metrics["max_drawdown"] > .12: reasons.append("high_drawdown")
    for f in folds:
        for scenario in ("baseline_costs", "cost_stress"):
            m = f["scenarios"][scenario]
            if m["number_of_trades"] < 20 or m["expectancy_per_trade"] <= 0 or m["max_drawdown"] > .12 or not (m.get("profit_factor_is_infinite") or (m.get("profit_factor") or 0) >= 1.2):
                reasons.append(f"{scenario}_development_fold_failed")
    scenario_pnl = {name: sum(f["scenarios"][name]["final_equity"] - f["scenarios"][name]["initial_equity"] for f in folds)
                    for name in ("frictionless_diagnostic", "baseline_costs", "cost_stress")}
    if scenario_pnl["frictionless_diagnostic"] <= 0:
        diagnosis = "unprofitable_even_without_costs"
    elif scenario_pnl["baseline_costs"] <= 0:
        diagnosis = "frictionless_profit_does_not_survive_baseline_costs"
    elif scenario_pnl["cost_stress"] <= 0:
        diagnosis = "baseline_profit_does_not_survive_cost_stress"
    else:
        diagnosis = "positive_pooled_scenario_pnl_still_requires_all_validation_gates"
    return {"candidate_id": candidate.candidate_id, "family_id": candidate.family_id,
            "parameters": candidate.parameters, "blocks": candidate.blocks, "metrics": metrics,
            "research_score": score_metrics(metrics), "trades": all_trades, "status": "completed",
            "development_only": True, "valid_economic_test": True, "screen_passed": not reasons,
            "failure_reasons": sorted(set(reasons)), "paper_readiness": {"paper_ready": False},
            "folds": folds, "trades_per_year": frequency,
            "economic_diagnosis": {"classification": diagnosis, "scenario_pnl": scenario_pnl,
                                   "causal_claim": False, "note": "Scenario changes can also change sizing and trade paths."},
            "certification": {"passed": False, "missing": ([] if calibrated else ["measured_execution_costs"]) + ["untouched_final_evaluation",
                "multiple_testing_adjustment", "parameter_neighborhood_validation", "portfolio_correlation", "prospective_paper_evidence",
                "execution_cost_representativeness", "historical_feed_and_corporate_action_audit", "liquidity_capacity_validation"] +
                (["historical_short_borrow_availability"] if candidate.parameters["direction"] == "short" else [])},
            "execution_semantics": {"version": VERSION, "flat_by_session_close": True,
                "holding_bar_convention": candidate.parameters.get("holding_bar_convention", "legacy"),
                "exit_timestamp_convention": candidate.parameters.get("exit_timestamp_convention", "legacy"),
                "indicator_convention": candidate.parameters.get("indicator_convention", "rolling_history"),
                "calibrated_execution_costs": calibrated, "gap_aware_stops": True, "max_notional_to_equity": 1},
            "data_quality": dataset["coverage"], "reserved_start": dataset["reserved_start"],
            "reserved_tail_status": dataset["reserved_tail_status"], "simulation_only": True}


def load_cost_model(conn, calibration_id: int, assets: list[str]) -> dict[str, Any]:
    """Only existing, sufficiently observed quote/fill calibrations are accepted.

    No caller-supplied cheap scalar can be passed off as measured execution.
    Copy into immutable candidate parameters instead of looking up latest at run time.
    """
    from app.services.intraday_research_integrity import cost_model_readiness
    from app.services.strategy_discovery import jsonable
    row = conn.execute("SELECT * FROM intraday_execution_cost_calibrations WHERE id = %s", (calibration_id,)).fetchone()
    if not row:
        raise ValueError("Execution-cost calibration not found")
    model = jsonable(dict(row))
    readiness = cost_model_readiness(model, symbols=assets)
    if not readiness["production_cost_ready"] or not set(assets) <= set(model.get("by_symbol") or {}):
        raise ValueError(f"Cost calibration lacks complete quote/fill evidence: {readiness['limitations']}")
    methodology = model.get("methodology") or {}
    if methodology.get("cost_basis_version") != "all_in_symbol_costs_v2" or methodology.get("feeds") != ["sip"]:
        raise ValueError("RUG cost calibration requires versioned, single SIP feed regular-session evidence")
    missing_symbols = [symbol for symbol in assets if
                       int(model["by_symbol"][symbol].get("regular_session_bars") or 0) < 100 or
                       int(model["by_symbol"][symbol].get("matched_fill_observations") or 0) < 30]
    if missing_symbols:
        raise ValueError(f"Cost calibration needs at least 100 quote bars and 30 matched fills per symbol: {missing_symbols}")
    regulatory = Decimal(str(model.get("regulatory_bps") or 0))
    if not regulatory.is_finite() or regulatory < 0:
        raise ValueError("Nonfinite or negative regulatory cost")
    symbol_models = list((model.get("by_symbol") or {}).values())
    for item, observed_key, stressed_key in [(item, "observed_round_trip_bps", "stressed_round_trip_bps") for item in [model, *symbol_models]] + [
        (item, "median_bar_spread_bps", "p90_bar_spread_bps")
        for item in [*(model.get("by_symbol") or {}).values(), *(model.get("by_time_slot") or {}).values()]]:
        observed, stressed = item.get(observed_key), item.get(stressed_key)
        for value in (observed, stressed):
            if value is not None and (not Decimal(str(value)).is_finite() or Decimal(str(value)) < 0):
                raise ValueError("Nonfinite or negative calibrated cost")
        if observed is not None and stressed is not None and Decimal(str(stressed)) < Decimal(str(observed)):
            raise ValueError("Cost stress cannot be weaker than observed costs")
    return model
