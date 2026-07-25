"""Phase 13.5 + 13.9: strategy analytics and evidence-based explainability.

Everything here is computed from stored rows -- `research_campaign_jobs`,
`research_campaign_trades`, `strategy_dna`, `research_hypothesis_versions`.
Nothing is inferred, estimated, or invented, and no LLM is involved in
producing any number or conclusion: an LLM may summarize this output, but
the facts originate here.

**Evidence tiers.** Every aggregate carries an explicit tier so the UI can
distinguish a reliable result from a suggestive one:

  * `statistically_reliable` -- enough trades AND enough distinct symbols
  * `descriptive`            -- enough trades, but concentrated
  * `exploratory`            -- some evidence, below the reliability floor
  * `insufficient_sample`    -- too little to say anything

The tier is a property of sample size and spread, NOT of how good the
numbers look. A spectacular result on 4 trades is `insufficient_sample`.

**No causal feature-importance claims.** Correlations between a feature and
an outcome are reported as correlations with their sample size, explicitly
labeled non-causal. There is no ranked "feature importance" output, because
this data cannot support one.
"""

from __future__ import annotations

from math import sqrt
from statistics import fmean, pstdev
from typing import Any

import psycopg

ANALYTICS_VERSION = "strategy_analytics_v1"

MINIMUM_TRADES_FOR_EXPLORATORY = 10
MINIMUM_TRADES_FOR_DESCRIPTIVE = 30
MINIMUM_TRADES_FOR_RELIABLE = 60
MINIMUM_SYMBOLS_FOR_RELIABLE = 2

EVIDENCE_TIER_RULES = {
    "statistically_reliable": f">= {MINIMUM_TRADES_FOR_RELIABLE} trades across >= {MINIMUM_SYMBOLS_FOR_RELIABLE} symbols",
    "descriptive": f">= {MINIMUM_TRADES_FOR_DESCRIPTIVE} trades",
    "exploratory": f">= {MINIMUM_TRADES_FOR_EXPLORATORY} trades",
    "insufficient_sample": f"< {MINIMUM_TRADES_FOR_EXPLORATORY} trades",
    "note": (
        "Tier reflects sample size and spread only, never how favorable the numbers are. "
        "A strong result on a small sample is still insufficient_sample."
    ),
}


def evidence_tier(trade_count: int, symbol_count: int) -> str:
    if trade_count >= MINIMUM_TRADES_FOR_RELIABLE and symbol_count >= MINIMUM_SYMBOLS_FOR_RELIABLE:
        return "statistically_reliable"
    if trade_count >= MINIMUM_TRADES_FOR_DESCRIPTIVE:
        return "descriptive"
    if trade_count >= MINIMUM_TRADES_FOR_EXPLORATORY:
        return "exploratory"
    return "insufficient_sample"


def _mean_confidence_interval(values: list[float], *, z: float = 1.96) -> dict[str, Any]:
    """Normal-approximation CI for the mean. Reported with its own sample
    size so a reader can judge whether the approximation is reasonable;
    below 30 observations it is explicitly flagged as unreliable."""
    n = len(values)
    if n < 2:
        return {"mean": values[0] if values else None, "lower": None, "upper": None, "sample_size": n, "reliable": False}
    mean = fmean(values)
    standard_error = pstdev(values) / sqrt(n)
    return {
        "mean": round(mean, 6),
        "lower": round(mean - z * standard_error, 6),
        "upper": round(mean + z * standard_error, 6),
        "sample_size": n,
        "reliable": n >= MINIMUM_TRADES_FOR_DESCRIPTIVE,
    }


def campaign_family_analytics(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    """Promotion rate, trade frequency, and profitability per family, plus
    the failure-rate breakdown by validation rule."""

    rows = conn.execute(
        """
        SELECT candidate->'parameters'->>'strategy_architecture' AS architecture,
               count(*) AS jobs,
               count(*) FILTER (WHERE status = 'promoted') AS promoted,
               count(DISTINCT symbol) AS symbols,
               coalesce(sum((result->'metrics'->>'number_of_trades')::int), 0) AS trades,
               avg((result->'metrics'->>'profit_factor')::float) AS avg_profit_factor,
               avg((result->'metrics'->>'expectancy_per_trade')::float) AS avg_expectancy,
               avg((result->'metrics'->>'max_drawdown')::float) AS avg_max_drawdown,
               avg((result->'metrics'->>'total_return')::float) AS avg_total_return,
               avg((result->'metrics'->>'average_holding_time_hours')::float) AS avg_holding_hours
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND status <> 'queued'
        GROUP BY 1
        ORDER BY 1
        """,
        (campaign_id,),
    ).fetchall()

    failure_rows = conn.execute(
        """
        SELECT architecture, reason, count(*) AS occurrences
        FROM (
            SELECT candidate->'parameters'->>'strategy_architecture' AS architecture,
                   jsonb_array_elements_text(failure_reasons) AS reason
            FROM research_campaign_jobs
            WHERE campaign_id = %s AND status <> 'queued'
        ) exploded
        GROUP BY 1, 2
        ORDER BY 1, 3 DESC
        """,
        (campaign_id,),
    ).fetchall()
    failures_by_family: dict[str, list[dict[str, Any]]] = {}
    for row in failure_rows:
        failures_by_family.setdefault(row["architecture"], []).append(
            {"validation_rule": row["reason"], "occurrences": int(row["occurrences"])}
        )

    analytics = []
    for row in rows:
        jobs = int(row["jobs"] or 0)
        trades = int(row["trades"] or 0)
        symbols = int(row["symbols"] or 0)
        analytics.append(
            {
                "architecture": row["architecture"],
                "jobs": jobs,
                "promoted_jobs": int(row["promoted"] or 0),
                "promotion_rate": round(int(row["promoted"] or 0) / jobs, 4) if jobs else 0.0,
                "symbols": symbols,
                "trades": trades,
                "trades_per_job": round(trades / jobs, 2) if jobs else 0.0,
                "avg_profit_factor": _round(row["avg_profit_factor"]),
                "avg_expectancy": _round(row["avg_expectancy"]),
                "avg_max_drawdown": _round(row["avg_max_drawdown"]),
                "avg_total_return": _round(row["avg_total_return"]),
                "avg_holding_hours": _round(row["avg_holding_hours"]),
                "evidence_tier": evidence_tier(trades, symbols),
                "failure_by_validation_rule": failures_by_family.get(row["architecture"], []),
            }
        )
    return analytics


def _round(value: Any, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None else None


def trade_level_breakdowns(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """Per-dimension performance from real trade rows. Returns empty
    breakdowns (not zeros) when a campaign has no trade-level evidence --
    campaigns predating Phase 12.4 legitimately have none."""

    def grouped(expression: str, label: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT {expression} AS bucket,
                   strategy_architecture,
                   count(*) AS trades,
                   count(DISTINCT symbol) AS symbols,
                   sum(net_pnl) AS net_pnl,
                   avg(net_pnl) AS avg_net_pnl,
                   sum(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)::float / count(*) AS win_rate,
                   avg(holding_period_hours) AS avg_holding_hours
            FROM research_campaign_trades
            WHERE campaign_id = %s
            GROUP BY 1, 2
            ORDER BY 2, 1
            """,
            (campaign_id,),
        ).fetchall()
        return [
            {
                label: row["bucket"],
                "architecture": row["strategy_architecture"],
                "trades": int(row["trades"]),
                "symbols": int(row["symbols"]),
                "net_pnl": _round(row["net_pnl"], 2),
                "avg_net_pnl": _round(row["avg_net_pnl"], 4),
                "win_rate": _round(row["win_rate"]),
                "avg_holding_hours": _round(row["avg_holding_hours"]),
                "evidence_tier": evidence_tier(int(row["trades"]), int(row["symbols"])),
            }
            for row in rows
        ]

    return {
        "by_symbol": grouped("symbol", "symbol"),
        "by_timeframe": grouped("timeframe", "timeframe"),
        "by_direction": grouped("direction", "direction"),
        "by_exit_reason": grouped("exit_reason", "exit_reason"),
        "by_month": grouped("month_key", "month"),
        "by_market_regime": grouped("coalesce(market_regime, 'unknown')", "market_regime"),
        "by_time_of_day": grouped(
            "CASE WHEN entry_minutes_from_open < 60 THEN 'opening_hour' "
            "WHEN entry_minutes_from_open < 120 THEN '60-120m' "
            "WHEN entry_minutes_to_close <= 60 THEN 'power_hour' "
            "ELSE 'midday' END",
            "time_of_day",
        ),
        "by_day_of_week": grouped("to_char(entry_time, 'Dy')", "day_of_week"),
        "by_dataset_split": grouped("coalesce(dataset_split, 'unlabeled')", "dataset_split"),
    }


def holding_period_distribution(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT strategy_architecture,
               count(*) AS trades,
               min(holding_period_hours) AS min_hours,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY holding_period_hours) AS p25,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY holding_period_hours) AS median,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY holding_period_hours) AS p75,
               max(holding_period_hours) AS max_hours
        FROM research_campaign_trades
        WHERE campaign_id = %s
        GROUP BY 1
        ORDER BY 1
        """,
        (campaign_id,),
    ).fetchall()
    return [
        {
            "architecture": row["strategy_architecture"],
            "trades": int(row["trades"]),
            "min_hours": _round(row["min_hours"]),
            "p25_hours": _round(row["p25"]),
            "median_hours": _round(row["median"]),
            "p75_hours": _round(row["p75"]),
            "max_hours": _round(row["max_hours"]),
        }
        for row in rows
    ]


def candidate_buckets(conn: psycopg.Connection, campaign_id: int) -> dict[str, list[dict[str, Any]]]:
    """The three diagnostic buckets that matter for allocating next steps:
    profitable but under-evidenced, frequent but unprofitable, and near-pass."""

    rows = conn.execute(
        """
        SELECT candidate_id,
               candidate->'parameters'->>'strategy_architecture' AS architecture,
               symbol, timeframe, status,
               (result->'metrics'->>'profit_factor')::float AS profit_factor,
               (result->'metrics'->>'expectancy_per_trade')::float AS expectancy,
               (result->'metrics'->>'number_of_trades')::int AS trades,
               (result->'metrics'->>'max_drawdown')::float AS max_drawdown,
               failure_reasons
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND status <> 'queued' AND result IS NOT NULL
        """,
        (campaign_id,),
    ).fetchall()

    profitable_under_evidenced, frequent_unprofitable, near_pass = [], [], []
    for row in rows:
        record = {
            "candidate_id": row["candidate_id"],
            "architecture": row["architecture"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "profit_factor": _round(row["profit_factor"]),
            "expectancy": _round(row["expectancy"]),
            "trades": int(row["trades"] or 0),
            "max_drawdown": _round(row["max_drawdown"]),
            "failure_reasons": list(row["failure_reasons"] or []),
        }
        pf = row["profit_factor"]
        trades = int(row["trades"] or 0)
        expectancy = row["expectancy"]

        if pf is not None and pf >= 1.2 and expectancy is not None and expectancy > 0 and trades < 30:
            record["why"] = f"Profit factor {pf:.2f} with positive expectancy, but only {trades} trades (gate needs 30)."
            profitable_under_evidenced.append(record)
        if trades >= 60 and pf is not None and pf < 1.0:
            record["why"] = f"{trades} trades but profit factor {pf:.2f} -- frequent and losing."
            frequent_unprofitable.append(record)
        if row["status"] != "promoted" and pf is not None and 1.0 <= pf < 1.2 and trades >= 30:
            record["why"] = f"Profit factor {pf:.2f} sits just below the unchanged 1.2 gate on {trades} trades."
            near_pass.append(record)

    key = lambda item: (-(item["profit_factor"] or 0), item["candidate_id"])
    return {
        "profitable_but_under_evidenced": sorted(profitable_under_evidenced, key=key)[:50],
        "frequent_but_unprofitable": sorted(frequent_unprofitable, key=lambda i: -i["trades"])[:50],
        "near_pass": sorted(near_pass, key=key)[:50],
    }


def family_confidence_intervals(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    """Per-family CI on mean net P&L per trade, from real trade rows."""
    rows = conn.execute(
        """
        SELECT strategy_architecture, net_pnl, symbol
        FROM research_campaign_trades
        WHERE campaign_id = %s
        """,
        (campaign_id,),
    ).fetchall()

    grouped: dict[str, list[float]] = {}
    symbols: dict[str, set] = {}
    for row in rows:
        grouped.setdefault(row["strategy_architecture"], []).append(float(row["net_pnl"]))
        symbols.setdefault(row["strategy_architecture"], set()).add(row["symbol"])

    return [
        {
            "architecture": architecture,
            "mean_net_pnl_per_trade": _mean_confidence_interval(values),
            "evidence_tier": evidence_tier(len(values), len(symbols.get(architecture, ()))),
        }
        for architecture, values in sorted(grouped.items())
    ]


def dna_diversity(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """Behavioral diversity of the families actually tested in this campaign."""
    from app.services.strategy_dna import behavioral_similarity, get_strategy_dna

    rows = conn.execute(
        """
        SELECT DISTINCT candidate->'parameters'->>'strategy_architecture' AS architecture
        FROM research_campaign_jobs
        WHERE campaign_id = %s AND candidate->'parameters'->>'strategy_architecture' IS NOT NULL
        ORDER BY 1
        """,
        (campaign_id,),
    ).fetchall()
    architectures = [row["architecture"] for row in rows]

    records = []
    for architecture in architectures:
        record = get_strategy_dna(conn, architecture)
        if record:
            records.append(record)

    pairs = []
    for index, first in enumerate(records):
        for second in records[index + 1 :]:
            pairs.append(
                {
                    "a": first["family_architecture"],
                    "b": second["family_architecture"],
                    "behavioral_similarity": behavioral_similarity(first["dna"], second["dna"]),
                }
            )
    pairs.sort(key=lambda item: -item["behavioral_similarity"])
    similarities = [pair["behavioral_similarity"] for pair in pairs]

    return {
        "families_tested": architectures,
        "families_with_dna": [record["family_architecture"] for record in records],
        "mean_pairwise_similarity": round(fmean(similarities), 4) if similarities else None,
        "most_similar_pairs": pairs[:10],
        "behaviorally_distinct_pairs": [pair for pair in pairs if pair["behavioral_similarity"] < 0.5][:10],
        "note": (
            "Behavioral similarity is computed from Strategy DNA vocabulary fields only, "
            "and is deliberately independent of parameter similarity."
        ),
    }


def campaign_analytics(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """The full Phase 13.5 analytics payload for one campaign."""
    families = campaign_family_analytics(conn, campaign_id)
    return {
        "analytics_version": ANALYTICS_VERSION,
        "campaign_id": campaign_id,
        "evidence_tier_rules": EVIDENCE_TIER_RULES,
        "families": families,
        "holding_period_distribution": holding_period_distribution(conn, campaign_id),
        "breakdowns": trade_level_breakdowns(conn, campaign_id),
        "candidate_buckets": candidate_buckets(conn, campaign_id),
        "family_confidence_intervals": family_confidence_intervals(conn, campaign_id),
        "dna_diversity": dna_diversity(conn, campaign_id),
        "causal_claims_disclaimer": (
            "All associations reported here are descriptive. No causal feature importance is "
            "claimed or computed; sample sizes are reported alongside every aggregate so a "
            "reader can judge reliability directly."
        ),
    }


# ---------------------------------------------------------------------------
# Phase 13.9: evidence report for a single candidate
# ---------------------------------------------------------------------------

def candidate_evidence_report(conn: psycopg.Connection, campaign_id: int, candidate_id: str) -> dict[str, Any]:
    """A complete, stored-evidence-only account of one candidate.

    Every field traces to a database row or to the family's declared
    hypothesis. Nothing is generated, estimated, or paraphrased by a model.
    """

    from app.services.labs.intraday.families.v2.base import V2_HYPOTHESES
    from app.services.strategy_dna import get_strategy_dna

    jobs = conn.execute(
        """
        SELECT j.id, j.symbol, j.timeframe, j.status, j.failure_reasons, j.dataset_id,
               j.candidate->'parameters' AS parameters,
               j.candidate->'blocks' AS blocks,
               j.result->'metrics' AS metrics,
               j.result->'execution_semantics' AS execution_semantics,
               c.name AS campaign_name, c.hypothesis_version_id
        FROM research_campaign_jobs j
        JOIN research_campaigns c ON c.id = j.campaign_id
        WHERE j.campaign_id = %s AND j.candidate_id = %s AND j.status <> 'queued'
        ORDER BY j.symbol, j.timeframe
        """,
        (campaign_id, candidate_id),
    ).fetchall()
    if not jobs:
        return {"error": f"No completed jobs for candidate {candidate_id!r} in campaign {campaign_id}."}

    first = jobs[0]
    parameters = dict(first["parameters"] or {})
    architecture = parameters.get("strategy_architecture")

    trades = conn.execute(
        """
        SELECT symbol, direction, exit_reason, net_pnl, holding_period_hours, month_key, dataset_split
        FROM research_campaign_trades
        WHERE campaign_id = %s AND candidate_id = %s
        """,
        (campaign_id, candidate_id),
    ).fetchall()

    per_symbol: dict[str, list[float]] = {}
    per_month: dict[str, list[float]] = {}
    for trade in trades:
        per_symbol.setdefault(trade["symbol"], []).append(float(trade["net_pnl"]))
        per_month.setdefault(trade["month_key"], []).append(float(trade["net_pnl"]))

    total_net = sum(float(trade["net_pnl"]) for trade in trades)
    symbol_concentration = None
    if per_symbol and total_net:
        magnitudes = {symbol: abs(sum(values)) for symbol, values in per_symbol.items()}
        total_magnitude = sum(magnitudes.values())
        if total_magnitude:
            dominant = max(magnitudes, key=magnitudes.get)
            symbol_concentration = {
                "dominant_symbol": dominant,
                "share_of_absolute_net_pnl": round(magnitudes[dominant] / total_magnitude, 4),
            }

    failures: dict[str, int] = {}
    for job in jobs:
        for reason in (job["failure_reasons"] or []):
            failures[reason] = failures.get(reason, 0) + 1

    profit_factors = [float(job["metrics"]["profit_factor"]) for job in jobs if (job["metrics"] or {}).get("profit_factor") is not None]
    trade_counts = [int((job["metrics"] or {}).get("number_of_trades") or 0) for job in jobs]
    hypothesis = V2_HYPOTHESES.get(architecture)
    dna = get_strategy_dna(conn, architecture) if architecture else None

    total_trades = sum(trade_counts)
    tier = evidence_tier(total_trades, len(per_symbol) or len({job["symbol"] for job in jobs}))

    strongest = weakest = None
    if profit_factors:
        best_job = max(jobs, key=lambda job: (job["metrics"] or {}).get("profit_factor") or -1)
        worst_job = min(jobs, key=lambda job: (job["metrics"] or {}).get("profit_factor") or 1e9)
        strongest = f"{best_job['symbol']} {best_job['timeframe']}: profit factor {(best_job['metrics'] or {}).get('profit_factor')}"
        weakest = f"{worst_job['symbol']} {worst_job['timeframe']}: profit factor {(worst_job['metrics'] or {}).get('profit_factor')}"

    return {
        "candidate_id": candidate_id,
        "campaign_id": campaign_id,
        "campaign_name": first["campaign_name"],
        "architecture": architecture,
        "strategy_version": (dna or {}).get("strategy_version"),
        "strategy_engine_version": parameters.get("strategy_engine_version"),
        "feature_engine_version": parameters.get("feature_engine_version"),
        "generator_version": parameters.get("generator_version"),
        "generation_channel": parameters.get("generation_channel"),
        "generation_reason": parameters.get("generation_reason"),
        "dataset_snapshot_id": first["dataset_id"],
        "hypothesis_version_id": first["hypothesis_version_id"],
        "execution_semantics": first["execution_semantics"],
        "hypothesis": hypothesis.as_dict() if hypothesis else None,
        "entry_logic": (first["blocks"] or {}).get("entry"),
        "exit_logic": (first["blocks"] or {}).get("exit"),
        "expected_holding_period": (dna or {}).get("dna", {}).get("holding_horizon_class"),
        "expected_trade_frequency": (dna or {}).get("dna", {}).get("expected_frequency_class"),
        "strategy_dna": (dna or {}).get("dna"),
        "external_execution_eligibility": (dna or {}).get("dna", {}).get("execution_capability", "simulation_only"),
        "evidence": {
            "jobs": len(jobs),
            "symbols_tested": sorted({job["symbol"] for job in jobs}),
            "total_trades": total_trades,
            "median_profit_factor": round(sorted(profit_factors)[len(profit_factors) // 2], 4) if profit_factors else None,
            "strongest_evidence": strongest,
            "weakest_evidence": weakest,
            "evidence_tier": tier,
            "symbol_concentration": symbol_concentration,
            "months_covered": sorted(per_month),
            "promoted_jobs": sum(1 for job in jobs if job["status"] == "promoted"),
        },
        "validation_failures": [
            {"validation_rule": reason, "jobs_affected": count}
            for reason, count in sorted(failures.items(), key=lambda item: -item[1])
        ],
        "provenance_note": (
            "Every field above is read from stored database rows or the family's declared "
            "hypothesis. No value here was generated, estimated, or paraphrased by a model."
        ),
    }
