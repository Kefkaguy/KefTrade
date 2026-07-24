"""Phase 12.4: Intraday Portfolio-Wide Failure Analysis.

Explains why each Phase 12.3 family failed, using trade-level evidence from
`research_campaign_trades` (populated only for a campaign launched after that
table existed -- see database/migrations/046_intraday_trade_evidence.sql and
research_campaigns.persist_intraday_job_trades). Campaign 47's own rows are
never read or written by anything in this module in a way that could alter
them; this module is read-only everywhere.

Everything here is computed from real stored rows. Where the requested
analysis needs a field that was never persisted (pre-entry price movement,
per-trade market/volatility regime beyond "unknown"), the corresponding
section is explicitly marked "insufficient_evidence" with the missing field
named -- never fabricated. See DATA_AVAILABILITY below and the Phase 12.4
data-availability appendix in docs/.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median as statistics_median
from typing import Any

import psycopg

# ---------------------------------------------------------------------------
# Explicit, documented rules -- exposed in the API response so the UI (and
# this docstring's readers) see the exact thresholds a subgroup must clear
# before it's ever called "stable", not just narrative judgment.
# ---------------------------------------------------------------------------
MINIMUM_EVIDENCE_RULES: dict[str, Any] = {
    "min_trades_for_subgroup_evidence": 20,
    "min_symbols_for_stability": 2,
    "min_months_for_stability": 2,
    "max_single_symbol_share_of_net_pnl": 0.6,
    "max_single_month_share_of_net_pnl": 0.6,
    "min_net_profit_factor_for_positive_subgroup": 1.0,
    "min_net_expectancy_for_positive_subgroup": 0.0,
}

ENTRY_TIME_BUCKETS: list[tuple[str, int, int | None]] = [
    ("0-30m", 0, 30),
    ("30-60m", 30, 60),
    ("60-120m", 60, 120),
    ("120-240m", 120, 240),
    ("240m+", 240, None),
]

RELATIVE_VOLUME_BUCKETS: list[tuple[str, float | None, float | None]] = [
    ("below_average (<1.0x)", None, 1.0),
    ("average (1.0x-1.5x)", 1.0, 1.5),
    ("elevated (>1.5x)", 1.5, None),
]

# "Moved against the position immediately after entry": the adverse extreme
# was reached on the entry bar itself (bars_to_mae == 0). A trade whose worst
# point comes later isn't "immediate" by this definition.
IMMEDIATE_ADVERSE_MOVE_BAR_THRESHOLD = 0

DATA_AVAILABILITY: dict[str, str] = {
    "pre_entry_price_movement": (
        "insufficient_evidence: no pre-entry candle/price history was persisted per trade "
        "(only the entry bar's own session feature snapshot -- minutes_from_open, "
        "minutes_to_close, session_relative_volume, gap_percent). 'Movement before entry' "
        "and 'time to MFE/MAE measured from a pre-entry reference' cannot be computed."
    ),
    "market_regime": (
        "insufficient_evidence: intraday campaign jobs pass an empty context_by_time "
        "(see research_campaigns.run_intraday_campaign_job), because market/volatility "
        "regime classification depends on swing `features` columns (ema_50, returns_5, "
        "volatility_20) that have never been computed at 15m/30m granularity. Every trade's "
        "market_regime/volatility_regime column reads 'unknown'."
    ),
    "training_vs_validation_split_metrics": (
        "insufficient_evidence: research_campaign_jobs.result.walk_forward_metrics only "
        "stores the train/validation date boundaries (train_start/end, validation_start/end), "
        "not separate performance numbers for each side -- walk_forward_metrics and "
        "out_of_sample_metrics are both aliases of the same dict "
        "(strategy_discovery.evaluate_candidate), never independently computed."
    ),
    "monthly_and_quarterly_breakdown_for_campaign_47": (
        "insufficient_evidence for Campaign 47 specifically: only annual (by_year) rollups "
        "were persisted for pre-Phase-12.4 jobs. Monthly stability in this report is computed "
        "from the Phase 12.4 re-run campaign's trade-level month_key column, not from "
        "Campaign 47's own stored evidence."
    ),
    "planned_reward_to_risk_for_archived_families": (
        "available: take_profit and stop_loss are both persisted per trade, so planned R:R "
        "(target_distance / stop_distance) is computable for every trade in the Phase 12.4 "
        "re-run campaign."
    ),
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def fetch_family_trades(conn: psycopg.Connection, campaign_id: int, architecture: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, timeframe, direction, candidate_id, entry_time, exit_time,
               entry_price, exit_price, quantity, stop_loss, take_profit, risk_per_unit,
               gross_pnl, fees, slippage_cost, net_pnl, pnl_pct, exit_reason,
               holding_period_hours, mfe_amount, mae_amount, mfe_r, mae_r,
               bars_to_mfe, bars_to_mae, entry_minutes_from_open, entry_minutes_to_close,
               entry_session_relative_volume, entry_gap_percent, market_regime,
               volatility_regime, month_key
        FROM research_campaign_trades
        WHERE campaign_id = %s AND strategy_architecture = %s
        """,
        (campaign_id, architecture),
    ).fetchall()
    trades = []
    for row in rows:
        trade = dict(row)
        for key in (
            "entry_price", "exit_price", "quantity", "stop_loss", "take_profit", "risk_per_unit",
            "gross_pnl", "fees", "slippage_cost", "net_pnl", "pnl_pct", "holding_period_hours",
            "mfe_amount", "mae_amount", "mfe_r", "mae_r", "entry_session_relative_volume", "entry_gap_percent",
        ):
            trade[key] = _to_float(trade.get(key))
        trades.append(trade)
    return trades


def fetch_family_jobs(conn: psycopg.Connection, campaign_id: int, architecture: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, candidate_id, symbol, timeframe, status,
               candidate->'parameters'->>'direction' AS direction,
               result->'metrics' AS metrics,
               result->'walk_forward_metrics' AS walk_forward_metrics,
               result->'regime_analysis' AS regime_analysis
        FROM research_campaign_jobs
        WHERE campaign_id = %s
          AND candidate->'parameters'->>'strategy_architecture' = %s
          AND status <> 'queued'
        """,
        (campaign_id, architecture),
    ).fetchall()
    return [dict(row) for row in rows]


def _win_loss_split(values: list[float]) -> tuple[list[float], list[float]]:
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    return wins, losses


def compute_group_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """The core aggregate calculation reused by every breakdown in this
    module: gross vs net profit factor/expectancy, win rate, payoff ratio,
    median trade return. `gross_pnl` and `net_pnl` are both real persisted
    trade columns -- this is a genuine before/after-costs comparison, not
    costs added back into an aggregate after the fact."""

    count = len(trades)
    if count == 0:
        return {
            "trade_count": 0,
            "gross_profit": 0.0, "gross_loss": 0.0, "gross_pnl": 0.0,
            "fees": 0.0, "slippage_cost": 0.0, "total_transaction_costs": 0.0,
            "net_pnl": 0.0,
            "gross_profit_factor": None, "net_profit_factor": None,
            "gross_expectancy": 0.0, "net_expectancy": 0.0,
            "win_rate": 0.0, "average_win": 0.0, "average_loss": 0.0, "payoff_ratio": None,
            "median_trade_return_pct": 0.0,
        }

    gross_values = [trade["gross_pnl"] for trade in trades]
    net_values = [trade["net_pnl"] for trade in trades]
    fees_total = sum(trade["fees"] for trade in trades)
    slippage_total = sum(trade["slippage_cost"] for trade in trades)

    gross_wins, gross_losses = _win_loss_split(gross_values)
    net_wins, net_losses = _win_loss_split(net_values)

    gross_profit = sum(gross_wins)
    gross_loss = abs(sum(gross_losses))
    net_profit = sum(net_wins)
    net_loss = abs(sum(net_losses))

    gross_profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    net_profit_factor = round(net_profit / net_loss, 4) if net_loss > 0 else (float("inf") if net_profit > 0 else None)

    win_rate = len(net_wins) / count
    average_win = (net_profit / len(net_wins)) if net_wins else 0.0
    average_loss = (net_loss / len(net_losses)) if net_losses else 0.0
    payoff_ratio = round(average_win / average_loss, 4) if average_loss > 0 else None

    returns = sorted(trade["pnl_pct"] for trade in trades)

    return {
        "trade_count": count,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "gross_pnl": round(sum(gross_values), 2),
        "fees": round(fees_total, 2),
        "slippage_cost": round(slippage_total, 2),
        "total_transaction_costs": round(fees_total + slippage_total, 2),
        "net_pnl": round(sum(net_values), 2),
        "gross_profit_factor": gross_profit_factor if gross_profit_factor in (None, float("inf")) else round(gross_profit_factor, 4),
        "net_profit_factor": net_profit_factor if net_profit_factor in (None, float("inf")) else round(net_profit_factor, 4),
        "gross_expectancy": round(sum(gross_values) / count, 4),
        "net_expectancy": round(sum(net_values) / count, 4),
        "win_rate": round(win_rate, 4),
        "average_win": round(average_win, 2),
        "average_loss": round(average_loss, 2),
        "payoff_ratio": payoff_ratio,
        "median_trade_return_pct": round(statistics_median(returns), 6) if returns else 0.0,
    }


def performance_decomposition(trades: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    group_metrics = compute_group_metrics(trades)
    job_profit_factors = [float(job["metrics"]["profit_factor"]) for job in jobs if job.get("metrics", {}).get("profit_factor") is not None]
    job_expectancies = [float(job["metrics"]["expectancy_per_trade"]) for job in jobs if job.get("metrics", {}).get("expectancy_per_trade") is not None]

    gross_pf = group_metrics["gross_profit_factor"]
    net_pf = group_metrics["net_profit_factor"]
    gross_is_finite_number = isinstance(gross_pf, (int, float)) and gross_pf != float("inf")
    net_is_finite_number = isinstance(net_pf, (int, float)) and net_pf != float("inf")

    if gross_is_finite_number and gross_pf < 1.0:
        cost_verdict = "already_unprofitable_before_costs"
    elif gross_pf is None:
        cost_verdict = "insufficient_evidence: no losing trades to compute a finite gross profit factor"
    elif (gross_pf == float("inf") or gross_pf >= 1.0) and net_is_finite_number and net_pf < 1.0:
        cost_verdict = "weak_gross_edge_destroyed_by_costs"
    elif net_is_finite_number and net_pf >= 1.0:
        cost_verdict = "gross_and_net_both_profitable_at_aggregate_level"
    else:
        cost_verdict = "failed_primarily_on_loss_magnitude_or_trade_structure"

    return {
        "total_jobs": len(jobs),
        **group_metrics,
        "median_job_profit_factor": round(statistics_median(job_profit_factors), 4) if job_profit_factors else None,
        "median_job_expectancy": round(statistics_median(job_expectancies), 4) if job_expectancies else None,
        "cost_impact_pct_of_gross_expectancy": (
            round((1 - (group_metrics["net_expectancy"] / group_metrics["gross_expectancy"])) * 100, 2)
            if group_metrics["gross_expectancy"] not in (0, 0.0)
            else None
        ),
        "verdict": cost_verdict,
    }


def exit_reason_breakdown(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_reason[trade["exit_reason"]].append(trade)

    total = len(trades)
    rows = []
    for reason, group in sorted(by_reason.items(), key=lambda item: -len(item[1])):
        metrics = compute_group_metrics(group)
        holding = sorted(trade["holding_period_hours"] for trade in group)
        mfe = sorted(trade["mfe_amount"] for trade in group if trade["mfe_amount"] is not None)
        mae = sorted(trade["mae_amount"] for trade in group if trade["mae_amount"] is not None)
        remaining_session = [trade["entry_minutes_to_close"] for trade in group if trade["entry_minutes_to_close"] is not None]
        rows.append(
            {
                "exit_reason": reason,
                "trade_count": len(group),
                "pct_of_trades": round(len(group) / total, 4) if total else 0.0,
                "gross_pnl": metrics["gross_pnl"],
                "net_pnl": metrics["net_pnl"],
                "gross_expectancy": metrics["gross_expectancy"],
                "net_expectancy": metrics["net_expectancy"],
                "win_rate": metrics["win_rate"],
                "average_holding_period_hours": round(sum(holding) / len(holding), 3) if holding else 0.0,
                "median_holding_period_hours": round(statistics_median(holding), 3) if holding else 0.0,
                "average_mfe": round(sum(mfe) / len(mfe), 2) if mfe else None,
                "median_mfe": round(statistics_median(mfe), 2) if mfe else None,
                "average_mae": round(sum(mae) / len(mae), 2) if mae else None,
                "median_mae": round(statistics_median(mae), 2) if mae else None,
                "average_remaining_session_minutes_at_entry": round(sum(remaining_session) / len(remaining_session), 1) if remaining_session else None,
            }
        )
    return rows


def _bucket_entry_time(minutes_from_open: int | None) -> str:
    if minutes_from_open is None:
        return "unknown"
    for label, lower, upper in ENTRY_TIME_BUCKETS:
        if minutes_from_open >= lower and (upper is None or minutes_from_open < upper):
            return label
    return "unknown"


def entry_quality_analysis(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(trades)
    if total == 0:
        return {"trade_count": 0, "by_entry_time_bucket": [], "insufficient_evidence": ["pre_entry_price_movement"]}

    moved_favorably_before_losing = sum(
        1 for trade in trades if (trade["mfe_amount"] or 0) > 0 and trade["net_pnl"] <= 0
    )
    mfe_exceeded_costs = sum(
        1 for trade in trades if trade["mfe_amount"] is not None and trade["mfe_amount"] > (trade["fees"] + trade["slippage_cost"])
    )
    target_distance_reached = 0
    immediate_adverse_moves = 0
    for trade in trades:
        if trade["mfe_amount"] is not None and trade["quantity"]:
            target_distance = abs(trade["take_profit"] - trade["entry_price"])
            mfe_per_unit = trade["mfe_amount"] / trade["quantity"]
            if mfe_per_unit >= target_distance:
                target_distance_reached += 1
        if trade.get("bars_to_mae") is not None and trade["bars_to_mae"] <= IMMEDIATE_ADVERSE_MOVE_BAR_THRESHOLD:
            immediate_adverse_moves += 1

    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_bucket[_bucket_entry_time(trade.get("entry_minutes_from_open"))].append(trade)

    bucket_rows = []
    for label, _, _ in ENTRY_TIME_BUCKETS:
        group = by_bucket.get(label, [])
        metrics = compute_group_metrics(group)
        bucket_rows.append({"entry_time_bucket": label, **metrics})
    unknown_group = by_bucket.get("unknown", [])
    if unknown_group:
        bucket_rows.append({"entry_time_bucket": "unknown", **compute_group_metrics(unknown_group)})

    return {
        "trade_count": total,
        "pct_moved_favorably_before_losing": round(moved_favorably_before_losing / total, 4),
        "pct_mfe_exceeded_transaction_costs": round(mfe_exceeded_costs / total, 4),
        "pct_mfe_reached_intended_target_distance": round(target_distance_reached / total, 4),
        "pct_moved_against_position_immediately": round(immediate_adverse_moves / total, 4),
        "immediate_adverse_move_definition": f"adverse extreme (MAE) reached within {IMMEDIATE_ADVERSE_MOVE_BAR_THRESHOLD} bar(s) of entry",
        "by_entry_time_bucket": bucket_rows,
        "insufficient_evidence": ["pre_entry_price_movement"],
    }


def cost_and_sizing_analysis(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(trades)
    if total == 0:
        return {"trade_count": 0}

    fees = [trade["fees"] for trade in trades]
    slippage = [trade["slippage_cost"] for trade in trades]
    total_costs = [trade["fees"] + trade["slippage_cost"] for trade in trades]
    notionals = [trade["entry_price"] * trade["quantity"] for trade in trades]
    stop_distances = [abs(trade["entry_price"] - trade["stop_loss"]) for trade in trades]
    target_distances = [abs(trade["take_profit"] - trade["entry_price"]) for trade in trades]
    quantities = [trade["quantity"] for trade in trades]

    cost_vs_mfe = [
        (trade["fees"] + trade["slippage_cost"]) / trade["mfe_amount"]
        for trade in trades
        if trade["mfe_amount"] and trade["mfe_amount"] > 0
    ]
    average_win = compute_group_metrics(trades)["average_win"]
    cost_pct_of_avg_winner = round((sum(total_costs) / total) / average_win, 4) if average_win > 0 else None

    realized_r = [
        trade["net_pnl"] / (trade["risk_per_unit"] * trade["quantity"])
        for trade in trades
        if trade["risk_per_unit"] and trade["quantity"]
    ]
    planned_rr = [
        target_distances[index] / stop_distances[index]
        for index in range(total)
        if stop_distances[index] > 0
    ]

    stop_bucket_edges = sorted(stop_distances)
    tercile_size = max(1, len(stop_bucket_edges) // 3)
    low_cut = stop_bucket_edges[tercile_size - 1] if stop_bucket_edges else 0
    high_cut = stop_bucket_edges[2 * tercile_size - 1] if len(stop_bucket_edges) >= 2 * tercile_size else (stop_bucket_edges[-1] if stop_bucket_edges else 0)
    buckets: dict[str, list[dict[str, Any]]] = {"narrow_stop": [], "medium_stop": [], "wide_stop": []}
    for trade in trades:
        distance = abs(trade["entry_price"] - trade["stop_loss"])
        if distance <= low_cut:
            buckets["narrow_stop"].append(trade)
        elif distance <= high_cut:
            buckets["medium_stop"].append(trade)
        else:
            buckets["wide_stop"].append(trade)
    loss_by_stop_bucket = []
    for label, group in buckets.items():
        losers = [trade["net_pnl"] for trade in group if trade["net_pnl"] <= 0]
        loss_by_stop_bucket.append(
            {
                "bucket": label,
                "trade_count": len(group),
                "average_loss": round(sum(losers) / len(losers), 2) if losers else None,
            }
        )

    return {
        "trade_count": total,
        "average_fees_per_trade": round(sum(fees) / total, 4),
        "average_slippage_per_trade": round(sum(slippage) / total, 4),
        "average_total_cost_per_trade": round(sum(total_costs) / total, 4),
        "costs_pct_of_gross_favorable_movement": round(sum(cost_vs_mfe) / len(cost_vs_mfe), 4) if cost_vs_mfe else None,
        "costs_pct_of_average_winner": cost_pct_of_avg_winner,
        "costs_pct_of_notional": round(sum(total_costs) / sum(notionals), 6) if sum(notionals) else None,
        "average_position_notional": round(sum(notionals) / total, 2),
        "median_position_notional": round(statistics_median(notionals), 2),
        "average_quantity": round(sum(quantities) / total, 4),
        "median_quantity": round(statistics_median(quantities), 4),
        "average_stop_distance": round(sum(stop_distances) / total, 4),
        "median_stop_distance": round(statistics_median(stop_distances), 4),
        "average_target_distance": round(sum(target_distances) / total, 4),
        "median_target_distance": round(statistics_median(target_distances), 4),
        "average_realized_r_multiple": round(sum(realized_r) / len(realized_r), 4) if realized_r else None,
        "average_planned_reward_to_risk": round(sum(planned_rr) / len(planned_rr), 4) if planned_rr else None,
        "loss_by_stop_distance_bucket": loss_by_stop_bucket,
    }


def _bucket_relative_volume(value: float | None) -> str:
    if value is None:
        return "unknown"
    for label, lower, upper in RELATIVE_VOLUME_BUCKETS:
        if (lower is None or value >= lower) and (upper is None or value < upper):
            return label
    return "unknown"


def _dominance_share(values_by_key: dict[str, float], total_abs: float) -> tuple[str | None, float]:
    if total_abs == 0:
        return None, 0.0
    dominant_key = max(values_by_key, key=lambda key: abs(values_by_key[key]))
    share = abs(values_by_key[dominant_key]) / total_abs
    return dominant_key, round(share, 4)


def _subgroup_rows(trades: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[key_fn(trade)].append(trade)
    rows = []
    for key, group in sorted(grouped.items()):
        metrics = compute_group_metrics(group)
        rows.append({"key": key, **metrics})
    return rows


def _annotate_stability(rows: list[dict[str, Any]], *, dimension: str, min_trades: int = None) -> list[dict[str, Any]]:
    min_trades = min_trades if min_trades is not None else MINIMUM_EVIDENCE_RULES["min_trades_for_subgroup_evidence"]
    for row in rows:
        reasons = []
        if row["trade_count"] < min_trades:
            reasons.append(f"trade_count {row['trade_count']} below minimum {min_trades}")
        net_pf = row["net_profit_factor"]
        net_pf_ok = isinstance(net_pf, (int, float)) and (net_pf == float("inf") or net_pf >= MINIMUM_EVIDENCE_RULES["min_net_profit_factor_for_positive_subgroup"])
        if not net_pf_ok:
            reasons.append("net_profit_factor below minimum")
        if row["net_expectancy"] < MINIMUM_EVIDENCE_RULES["min_net_expectancy_for_positive_subgroup"]:
            reasons.append("net_expectancy not positive")
        row["meets_minimum_evidence"] = not reasons
        row["stability_notes"] = reasons
    return rows


def stability_subgroups(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total_net_pnl_abs = sum(abs(trade["net_pnl"]) for trade in trades) or 0.0
    by_symbol_net: dict[str, float] = defaultdict(float)
    by_month_net: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_symbol_net[trade["symbol"]] += trade["net_pnl"]
        by_month_net[trade["month_key"]] += trade["net_pnl"]

    dominant_symbol, symbol_share = _dominance_share(by_symbol_net, total_net_pnl_abs)
    dominant_month, month_share = _dominance_share(by_month_net, total_net_pnl_abs)

    distinct_symbols = len({trade["symbol"] for trade in trades})
    distinct_months = len({trade["month_key"] for trade in trades})

    campaign_level_dominance = {
        "distinct_symbols": distinct_symbols,
        "distinct_months": distinct_months,
        "dominant_symbol": dominant_symbol,
        "dominant_symbol_share_of_net_pnl": symbol_share,
        "single_symbol_dominates": symbol_share > MINIMUM_EVIDENCE_RULES["max_single_symbol_share_of_net_pnl"],
        "dominant_month": dominant_month,
        "dominant_month_share_of_net_pnl": month_share,
        "single_month_dominates": month_share > MINIMUM_EVIDENCE_RULES["max_single_month_share_of_net_pnl"],
        "meets_symbol_diversity_requirement": distinct_symbols >= MINIMUM_EVIDENCE_RULES["min_symbols_for_stability"],
        "meets_month_diversity_requirement": distinct_months >= MINIMUM_EVIDENCE_RULES["min_months_for_stability"],
    }

    return {
        "campaign_level_dominance": campaign_level_dominance,
        "by_symbol": _annotate_stability(_subgroup_rows(trades, lambda t: t["symbol"]), dimension="symbol"),
        "by_direction": _annotate_stability(_subgroup_rows(trades, lambda t: t["direction"]), dimension="direction"),
        "by_timeframe": _annotate_stability(_subgroup_rows(trades, lambda t: t["timeframe"]), dimension="timeframe"),
        "by_month": _annotate_stability(_subgroup_rows(trades, lambda t: t["month_key"]), dimension="month"),
        "by_exit_reason": _annotate_stability(_subgroup_rows(trades, lambda t: t["exit_reason"]), dimension="exit_reason"),
        "by_candidate_parameter_set": _annotate_stability(_subgroup_rows(trades, lambda t: t["candidate_id"]), dimension="candidate_id"),
        "by_relative_volume_bucket": _annotate_stability(
            _subgroup_rows(trades, lambda t: _bucket_relative_volume(t.get("entry_session_relative_volume"))), dimension="relative_volume_bucket"
        ),
        "by_market_regime": {
            "rows": _subgroup_rows(trades, lambda t: t.get("market_regime") or "unknown"),
            "insufficient_evidence": DATA_AVAILABILITY["market_regime"],
        },
    }


def classify_family(
    *,
    performance: dict[str, Any],
    exits: list[dict[str, Any]],
    entry_quality: dict[str, Any],
    cost_sizing: dict[str, Any],
    stability: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every classification below is backed by the specific metric that
    triggered it -- never assigned from narrative judgment alone."""

    classifications: list[dict[str, Any]] = []

    def add(label: str, evidence: dict[str, Any]) -> None:
        classifications.append({"classification": label, "evidence": evidence})

    gross_pf = performance["gross_profit_factor"]
    gross_pf_weak = isinstance(gross_pf, (int, float)) and gross_pf != float("inf") and gross_pf < 1.05
    if gross_pf_weak and performance["win_rate"] < 0.5 and performance["gross_expectancy"] <= 0:
        add("no_directional_edge", {"gross_profit_factor": gross_pf, "gross_expectancy": performance["gross_expectancy"], "win_rate": performance["win_rate"]})

    if performance["verdict"] == "weak_gross_edge_destroyed_by_costs":
        add(
            "transaction_cost_failure",
            {"gross_profit_factor": gross_pf, "net_profit_factor": performance["net_profit_factor"], "cost_impact_pct_of_gross_expectancy": performance["cost_impact_pct_of_gross_expectancy"]},
        )

    stop_exit_rows = [row for row in exits if row["exit_reason"].startswith("stop_loss")]
    stop_exit_share = sum(row["pct_of_trades"] for row in stop_exit_rows)
    if stop_exit_share >= 0.5:
        add("stop_sizing_failure", {"stop_loss_exit_share": round(stop_exit_share, 4), "stop_exit_rows": stop_exit_rows})

    target_exit_row = next((row for row in exits if row["exit_reason"] == "take_profit"), None)
    if target_exit_row and target_exit_row["pct_of_trades"] < 0.15 and entry_quality.get("pct_mfe_reached_intended_target_distance", 1) < 0.3:
        add(
            "target_sizing_failure",
            {"take_profit_exit_share": target_exit_row["pct_of_trades"], "pct_mfe_reached_intended_target_distance": entry_quality.get("pct_mfe_reached_intended_target_distance")},
        )

    session_close_row = next((row for row in exits if row["exit_reason"] == "session_close"), None)
    if session_close_row and session_close_row["pct_of_trades"] >= 0.3 and session_close_row["net_expectancy"] < 0:
        add("forced_session_close_failure", {"session_close_exit_share": session_close_row["pct_of_trades"], "session_close_net_expectancy": session_close_row["net_expectancy"]})

    late_entry_rows = [row for row in entry_quality.get("by_entry_time_bucket", []) if row["entry_time_bucket"] in ("120-240m", "240m+")]
    late_entry_trade_share = sum(row["trade_count"] for row in late_entry_rows) / entry_quality["trade_count"] if entry_quality.get("trade_count") else 0
    late_entry_negative = any(row["net_expectancy"] < 0 for row in late_entry_rows)
    if late_entry_trade_share >= 0.3 and late_entry_negative:
        add("late_entry_failure", {"late_entry_trade_share": round(late_entry_trade_share, 4), "late_entry_buckets": late_entry_rows})

    if cost_sizing.get("costs_pct_of_average_winner") is not None and cost_sizing["costs_pct_of_average_winner"] >= 0.5:
        add("position_sizing_failure", {"costs_pct_of_average_winner": cost_sizing["costs_pct_of_average_winner"]})

    dominance = stability["campaign_level_dominance"]
    if dominance["single_symbol_dominates"]:
        add("symbol_concentration", {"dominant_symbol": dominance["dominant_symbol"], "share_of_net_pnl": dominance["dominant_symbol_share_of_net_pnl"]})
    if dominance["single_month_dominates"]:
        add("out_of_sample_failure", {"dominant_month": dominance["dominant_month"], "share_of_net_pnl": dominance["dominant_month_share_of_net_pnl"]})

    timeframe_rows = stability["by_timeframe"]
    if len(timeframe_rows) >= 2:
        pfs = [row["net_profit_factor"] for row in timeframe_rows if isinstance(row["net_profit_factor"], (int, float))]
        if len(pfs) >= 2 and (max(pfs) - min(pfs)) >= 0.5:
            add("timeframe_dependence", {"by_timeframe": timeframe_rows})

    direction_rows = stability["by_direction"]
    if len(direction_rows) >= 2:
        pfs = [row["net_profit_factor"] for row in direction_rows if isinstance(row["net_profit_factor"], (int, float))]
        if len(pfs) >= 2 and (max(pfs) - min(pfs)) >= 0.5:
            add("direction_dependence", {"by_direction": direction_rows})

    parameter_rows = stability["by_candidate_parameter_set"]
    if len(parameter_rows) >= 2:
        pfs = [row["net_profit_factor"] for row in parameter_rows if isinstance(row["net_profit_factor"], (int, float))]
        if len(pfs) >= 2 and (max(pfs) - min(pfs)) >= 0.75:
            add("unstable_parameters", {"by_candidate_parameter_set": parameter_rows})

    if performance["trade_count"] < 60:
        add("insufficient_evidence", {"trade_count": performance["trade_count"], "minimum_expected": 60})

    if not classifications:
        add("weak_gross_edge", {"gross_profit_factor": gross_pf, "net_profit_factor": performance["net_profit_factor"]})

    return classifications


def research_allocation(
    *,
    family_name: str,
    performance: dict[str, Any],
    classifications: list[dict[str, Any]],
    stability: dict[str, Any],
) -> dict[str, Any]:
    labels = {entry["classification"] for entry in classifications}
    net_pf = performance["net_profit_factor"]
    net_pf_positive = isinstance(net_pf, (int, float)) and (net_pf == float("inf") or net_pf >= 1.0)
    dominance = stability["campaign_level_dominance"]
    evidence_stable = (
        net_pf_positive
        and dominance["meets_symbol_diversity_requirement"]
        and dominance["meets_month_diversity_requirement"]
        and not dominance["single_symbol_dominates"]
        and not dominance["single_month_dominates"]
    )

    symbol_rows = sorted(stability["by_symbol"], key=lambda row: row["net_pnl"], reverse=True)
    strongest_subgroup = symbol_rows[0]["key"] if symbol_rows else None
    weakest_subgroup = symbol_rows[-1]["key"] if symbol_rows else None

    if "insufficient_evidence" in labels:
        decision = "gather_more_evidence"
    elif evidence_stable and net_pf_positive and "no_directional_edge" not in labels and "transaction_cost_failure" not in labels:
        decision = "retain_for_focused_investigation"
    elif "transaction_cost_failure" in labels and net_pf is not None and (net_pf == float("inf") or (isinstance(net_pf, (int, float)) and net_pf >= 0.8)):
        decision = "redesign_as_separately_versioned_hypothesis"
    elif "no_directional_edge" in labels:
        decision = "archive"
    else:
        decision = "archive"

    return {
        "family": family_name,
        "decision": decision,
        "primary_evidence": {"net_profit_factor": net_pf, "net_expectancy": performance["net_expectancy"], "trade_count": performance["trade_count"]},
        "principal_failure_mechanism": sorted(labels),
        "strongest_subgroup": strongest_subgroup,
        "weakest_subgroup": weakest_subgroup,
        "evidence_stability": "stable" if evidence_stable else "not_stable",
        "recommended_research_budget": (
            "0 jobs -- archive, no further compute" if decision == "archive"
            else "small (<=40 jobs): targeted follow-up on the strongest subgroup only" if decision == "redesign_as_separately_versioned_hypothesis"
            else "medium (60-120 jobs): broaden symbol/timeframe coverage before any promotion decision" if decision == "gather_more_evidence"
            else "focused (40-80 jobs): deepen evidence on the already-promising subgroup, no blind grid expansion"
        ),
        "permitted_next_action": (
            "Design a new, separately-versioned candidate generation for this hypothesis and evaluate it through the unmodified elite gate."
            if decision == "redesign_as_separately_versioned_hypothesis"
            else "Run additional symbols/periods for this family under the existing registry, still through the unmodified elite gate."
            if decision == "gather_more_evidence"
            else "Continue investigating this family's strongest subgroup with the existing candidate generator; still through the unmodified elite gate."
            if decision == "retain_for_focused_investigation"
            else "None -- do not spend further research compute on this family."
        ),
        "prohibited_next_action": "Promoting any candidate from this family without passing the unmodified elite gate; relaxing any threshold; blind parameter-grid expansion.",
    }


def amd_session_momentum_investigation(conn: psycopg.Connection, campaign_id: int, *, architecture: str = "session_momentum_v1") -> dict[str, Any]:
    trades = fetch_family_trades(conn, campaign_id, architecture)
    amd_30m_long = [trade for trade in trades if trade["symbol"] == "AMD" and trade["timeframe"] == "30m" and trade["direction"] == "long"]

    if not amd_30m_long:
        return {"insufficient_evidence": "No AMD 30m long Session Momentum trades found in this campaign's trade-level evidence."}

    candidate_ids = sorted({trade["candidate_id"] for trade in amd_30m_long})
    overall = compute_group_metrics(amd_30m_long)

    by_month = _subgroup_rows(amd_30m_long, lambda t: t["month_key"])
    strongest_month = max(by_month, key=lambda row: row["net_pnl"]) if by_month else None
    without_strongest_month = [trade for trade in amd_30m_long if strongest_month is None or trade["month_key"] != strongest_month["key"]]
    survives_without_best_month = compute_group_metrics(without_strongest_month)["net_pnl"] > 0 if without_strongest_month else False

    sorted_by_pnl = sorted(amd_30m_long, key=lambda trade: trade["net_pnl"], reverse=True)
    best_trade_count = max(1, round(len(sorted_by_pnl) * 0.1))
    without_best_trades = sorted_by_pnl[best_trade_count:]
    survives_without_best_trades = compute_group_metrics(without_best_trades)["net_pnl"] > 0 if without_best_trades else False

    exit_distribution = exit_reason_breakdown(amd_30m_long)
    entry_time_distribution = [
        {"bucket": label, "trade_count": len(group)}
        for label, group in ((label, [t for t in amd_30m_long if _bucket_entry_time(t.get("entry_minutes_from_open")) == label]) for label, _, _ in ENTRY_TIME_BUCKETS)
        if group
    ]

    def transfer_check(*, symbol: str | None = None, timeframe: str | None = None, direction: str | None = None) -> dict[str, Any]:
        subset = [
            trade
            for trade in trades
            if (symbol is None or trade["symbol"] == symbol)
            and (timeframe is None or trade["timeframe"] == timeframe)
            and (direction is None or trade["direction"] == direction)
        ]
        if not subset:
            return {"trade_count": 0, "transfers": None, "note": "no comparable trades in this campaign"}
        metrics = compute_group_metrics(subset)
        return {"trade_count": metrics["trade_count"], "net_profit_factor": metrics["net_profit_factor"], "net_expectancy": metrics["net_expectancy"], "transfers": metrics["net_profit_factor"] is not None and (metrics["net_profit_factor"] == float("inf") or metrics["net_profit_factor"] >= 1.0)}

    comparison_symbols = {
        "other_semiconductors": ["NVDA"],
        "other_high_beta": ["TSLA", "META"],
        "SPY": ["SPY"],
        "QQQ": ["QQQ"],
    }
    comparisons = {
        group_label: {symbol: transfer_check(symbol=symbol, timeframe="30m", direction="long") for symbol in symbols}
        for group_label, symbols in comparison_symbols.items()
    }

    return {
        "candidate_ids": candidate_ids,
        "trade_count": overall["trade_count"],
        "gross_and_net_results": overall,
        "exit_distribution": exit_distribution,
        "cost_impact": {
            "total_transaction_costs": overall["total_transaction_costs"],
            "cost_impact_pct_of_gross_expectancy": (
                round((1 - (overall["net_expectancy"] / overall["gross_expectancy"])) * 100, 2) if overall["gross_expectancy"] else None
            ),
        },
        "monthly_results": by_month,
        "quarterly_results": {"insufficient_evidence": "quarter_key was not persisted; only month_key. Quarter can be derived from month_key by the caller if needed."},
        "entry_time_distribution": entry_time_distribution,
        "regime_distribution": {"insufficient_evidence": DATA_AVAILABILITY["market_regime"]},
        "strongest_month": strongest_month,
        "survives_removal_of_best_month": survives_without_best_month,
        "survives_removal_of_best_trades": {"trades_removed": best_trade_count, "survives": survives_without_best_trades},
        "transfer_to_other_configurations": {
            "15m_same_symbol_direction": transfer_check(symbol="AMD", timeframe="15m", direction="long"),
            "30m_short_direction": transfer_check(symbol="AMD", timeframe="30m", direction="short"),
        },
        "comparison_to_other_symbols": comparisons,
        "training_vs_validation_split": {"insufficient_evidence": DATA_AVAILABILITY["training_vs_validation_split_metrics"]},
        "note_on_campaign_47_vs_this_reevaluation": (
            "Campaign 47's own 2 promoted-at-job-level AMD 30m long Session Momentum jobs are preserved "
            "unchanged; this section's trade-level detail comes from the Phase 12.4 re-run campaign "
            "(identical strategy code and parameters, same candidate_id, re-executed against the current "
            "candle/feature history to capture trade-level evidence that Campaign 47 never persisted). "
            "Aggregate metrics may differ slightly from Campaign 47's stored numbers if the underlying "
            "candle history has been extended by resyncs since Campaign 47 ran; see the data-availability "
            "appendix for the exact comparison."
        ),
    }


def family_report(conn: psycopg.Connection, campaign_id: int, architecture: str, family_name: str) -> dict[str, Any]:
    trades = fetch_family_trades(conn, campaign_id, architecture)
    jobs = fetch_family_jobs(conn, campaign_id, architecture)
    performance = performance_decomposition(trades, jobs)
    exits = exit_reason_breakdown(trades)
    entry_quality = entry_quality_analysis(trades)
    cost_sizing = cost_and_sizing_analysis(trades)
    stability = stability_subgroups(trades)
    classifications = classify_family(performance=performance, exits=exits, entry_quality=entry_quality, cost_sizing=cost_sizing, stability=stability)
    allocation = research_allocation(family_name=family_name, performance=performance, classifications=classifications, stability=stability)

    return {
        "architecture": architecture,
        "family_name": family_name,
        "performance_decomposition": performance,
        "exit_reason_breakdown": exits,
        "entry_quality_analysis": entry_quality,
        "cost_and_position_sizing_analysis": cost_sizing,
        "stability_analysis": stability,
        "failure_classifications": classifications,
        "research_allocation": allocation,
    }


PHASE_12_4_FAMILIES: dict[str, str] = {
    "gap_fill_v1": "Gap Fill",
    "session_momentum_v1": "Session Momentum",
    "intraday_trend_pullback_v1": "Intraday Trend Pullback",
    "ema_trend_continuation_v1": "EMA Trend Continuation",
    "opening_fade_v1": "Opening Fade",
    "vwap_trend_continuation_v1": "VWAP Trend Continuation",
}


def phase_12_4_report(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    families = [family_report(conn, campaign_id, architecture, name) for architecture, name in PHASE_12_4_FAMILIES.items()]
    return {
        "campaign_id": campaign_id,
        "minimum_evidence_rules": MINIMUM_EVIDENCE_RULES,
        "entry_time_buckets": [label for label, _, _ in ENTRY_TIME_BUCKETS],
        "relative_volume_buckets": [label for label, _, _ in RELATIVE_VOLUME_BUCKETS],
        "data_availability": DATA_AVAILABILITY,
        "families": families,
        "amd_30m_session_momentum_investigation": amd_session_momentum_investigation(conn, campaign_id),
    }
