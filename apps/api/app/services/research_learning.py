from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.strategy_research import finite_metric

LEARNING_VERSION = "research_learning_v1"
SAFETY_STATEMENT = "Deterministic simulation-only research learning. No live trading, broker routing, or opaque ML decisioning."


def ensure_research_learning_tables(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_knowledge_versions (
            id BIGSERIAL PRIMARY KEY,
            knowledge_key TEXT NOT NULL,
            knowledge_type TEXT NOT NULL,
            campaign_id BIGINT,
            source_ref TEXT NOT NULL,
            version INTEGER NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            confidence_score NUMERIC NOT NULL DEFAULT 0,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_failure_patterns (
            id BIGSERIAL PRIMARY KEY,
            pattern_key TEXT NOT NULL,
            campaign_id BIGINT,
            pattern_type TEXT NOT NULL,
            description TEXT NOT NULL,
            frequency INTEGER NOT NULL DEFAULT 0,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            supporting_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            recommendation TEXT,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_success_patterns (
            id BIGSERIAL PRIMARY KEY,
            pattern_key TEXT NOT NULL,
            campaign_id BIGINT,
            pattern_type TEXT NOT NULL,
            description TEXT NOT NULL,
            frequency INTEGER NOT NULL DEFAULT 0,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            supporting_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            recommendation TEXT,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_recommendations (
            id BIGSERIAL PRIMARY KEY,
            recommendation_key TEXT NOT NULL,
            campaign_id BIGINT,
            title TEXT NOT NULL,
            finding TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'open',
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            expected_improvement TEXT NOT NULL,
            confidence_score NUMERIC NOT NULL DEFAULT 0,
            validation JSONB NOT NULL DEFAULT '{}'::jsonb,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_confidence_history (
            id BIGSERIAL PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            campaign_id BIGINT,
            confidence_score NUMERIC NOT NULL,
            components JSONB NOT NULL DEFAULT '{}'::jsonb,
            explanation TEXT NOT NULL,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_evolution_history (
            id BIGSERIAL PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            parent_candidate_id TEXT,
            campaign_id BIGINT,
            mutation JSONB NOT NULL DEFAULT '{}'::jsonb,
            reason TEXT NOT NULL,
            supporting_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            expected_improvement TEXT NOT NULL,
            confidence_score NUMERIC NOT NULL DEFAULT 0,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_timeline_events (
            id BIGSERIAL PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            campaign_id BIGINT,
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            event_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_campaign_plans (
            id BIGSERIAL PRIMARY KEY,
            plan_key TEXT NOT NULL,
            campaign_id BIGINT,
            priorities JSONB NOT NULL DEFAULT '[]'::jsonb,
            exploration_targets JSONB NOT NULL DEFAULT '[]'::jsonb,
            confirmation_targets JSONB NOT NULL DEFAULT '[]'::jsonb,
            rationale JSONB NOT NULL DEFAULT '{}'::jsonb,
            calculation_version TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            simulation_only BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )


def learn_from_completed_campaign(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    ensure_research_learning_tables(conn)
    campaign = conn.execute("SELECT * FROM research_campaigns WHERE id = %s AND simulation_only = TRUE", (campaign_id,)).fetchone()
    if not campaign:
        return {"campaign_id": campaign_id, "learned": False, "reason": "campaign not found", "simulation_only": True}
    jobs = conn.execute(
        """
        SELECT *
        FROM research_campaign_jobs
        WHERE campaign_id = %s
        ORDER BY id ASC
        """,
        (campaign_id,),
    ).fetchall()
    learning = build_campaign_learning(dict(campaign), [dict(row) for row in jobs])
    persist_campaign_learning(conn, learning)
    return learning


def build_campaign_learning(campaign: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [normalize_job(row) for row in jobs]
    failure_patterns = detect_failure_patterns(normalized)
    success_patterns = detect_success_patterns(normalized)
    knowledge = build_knowledge_versions(campaign, normalized, failure_patterns, success_patterns)
    recommendations = generate_research_recommendations(failure_patterns, success_patterns)
    confidence_scores = calculate_campaign_confidence(normalized)
    evolution_history = generate_evidence_based_mutations(normalized, failure_patterns, success_patterns)
    campaign_plan = build_adaptive_campaign_plan(normalized, failure_patterns, success_patterns, recommendations)
    timeline = build_strategy_timeline(campaign, normalized, confidence_scores, evolution_history)
    elite_rankings = rank_elite_candidates(normalized, confidence_scores)
    return {
        "campaign_id": campaign.get("id"),
        "calculation_version": LEARNING_VERSION,
        "knowledge": knowledge,
        "failure_patterns": failure_patterns,
        "success_patterns": success_patterns,
        "recommendations": recommendations,
        "confidence_scores": confidence_scores,
        "evolution_history": evolution_history,
        "campaign_plan": campaign_plan,
        "timeline": timeline,
        "elite_rankings": elite_rankings,
        "summary": {
            "jobs_analyzed": len(normalized),
            "failure_patterns": len(failure_patterns),
            "success_patterns": len(success_patterns),
            "recommendations": len(recommendations),
            "evolved_variants": len(evolution_history),
            "confidence_scores": len(confidence_scores),
        },
        "safety": {"simulation_only": True, "statement": SAFETY_STATEMENT},
        "simulation_only": True,
    }


def normalize_job(job: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(job.get("candidate") or job.get("payload") or {})
    result = dict(job.get("result") or {})
    metrics = dict(result.get("metrics") or job.get("metrics") or {})
    blocks = dict(candidate.get("blocks") or job.get("blocks") or {})
    parameters = dict(candidate.get("parameters") or job.get("parameters") or {})
    candidate_id = str(job.get("candidate_id") or candidate.get("candidate_id") or job.get("id"))
    status = str(job.get("status") or "unknown")
    failure_reasons = list(job.get("failure_reasons") or result.get("failure_reasons") or [])
    if job.get("failure_classification"):
        failure_reasons.append(str(job["failure_classification"]))
    if status in {"rejected", "failed", "blocked_data"} and not failure_reasons:
        failure_reasons.extend(infer_metric_failure_reasons(metrics))
    return {
        "job_id": job.get("id"),
        "candidate_id": candidate_id,
        "family_id": str(job.get("family_id") or candidate.get("family_id") or "unknown"),
        "parent_candidate_id": candidate.get("parent_candidate_id") or job.get("parent_candidate_id"),
        "strategy_family": str(job.get("strategy_family") or strategy_family(blocks)),
        "symbol": str(job.get("symbol") or result.get("symbol") or "unknown"),
        "timeframe": str(job.get("timeframe") or result.get("timeframe") or "unknown"),
        "status": status,
        "promoted": status == "promoted",
        "rejected": status in {"rejected", "failed", "blocked_data"},
        "blocks": blocks,
        "parameters": parameters,
        "metrics": metrics,
        "result": result,
        "failure_reasons": sorted(set(failure_reasons)),
        "validation_score": finite_metric(job.get("validation_score") or result.get("validation_score")),
        "consistency_score": finite_metric(job.get("consistency_score") or result.get("consistency_score")),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "evidence_ref": f"campaign_job:{job.get('id') or candidate_id}",
    }


def infer_metric_failure_reasons(metrics: dict[str, Any]) -> list[str]:
    reasons = []
    if finite_metric(metrics.get("number_of_trades")) < 30:
        reasons.append("sample_size_too_small")
    if finite_metric(metrics.get("profit_factor")) < 1.1:
        reasons.append("profit_factor_below_threshold")
    if finite_metric(metrics.get("expectancy_per_trade")) <= 0:
        reasons.append("expectancy_non_positive")
    if finite_metric(metrics.get("max_drawdown")) > 0.15:
        reasons.append("drawdown_excessive")
    return reasons or ["validation_rules_failed"]


def detect_failure_patterns(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rejected = [row for row in jobs if row["rejected"]]
    counters: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rejected:
        for reason in row["failure_reasons"]:
            counters[("failure_reason", reason)].append(row)
        for key, value in row["blocks"].items():
            counters[(f"{key}_block", str(value))].append(row)
        for key, value in parameter_buckets(row["parameters"]).items():
            counters[(f"{key}_range", value)].append(row)
        for regime in failing_regimes(row):
            counters[("market_regime", regime)].append(row)
    return ranked_patterns(counters, success=False)


def detect_success_patterns(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    promoted = [row for row in jobs if row["promoted"]]
    counters: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in promoted:
        for key, value in row["blocks"].items():
            counters[(f"{key}_block", str(value))].append(row)
        for key, value in parameter_buckets(row["parameters"]).items():
            counters[(f"{key}_range", value)].append(row)
        for regime in successful_regimes(row):
            counters[("market_regime", regime)].append(row)
    return ranked_patterns(counters, success=True)


def ranked_patterns(counters: dict[tuple[str, str], list[dict[str, Any]]], *, success: bool) -> list[dict[str, Any]]:
    rows = []
    for (pattern_type, value), evidence in counters.items():
        if len(evidence) < 1:
            continue
        refs = [row["evidence_ref"] for row in evidence]
        avg_pf = average(row["metrics"].get("profit_factor") for row in evidence)
        avg_expectancy = average(row["metrics"].get("expectancy_per_trade") for row in evidence)
        key = stable_key("success" if success else "failure", pattern_type, value)
        rows.append(
            {
                "pattern_key": key,
                "pattern_type": pattern_type,
                "value": value,
                "description": pattern_description(pattern_type, value, success),
                "frequency": len(evidence),
                "evidence_refs": refs,
                "supporting_metrics": {"average_profit_factor": avg_pf, "average_expectancy": avg_expectancy},
                "recommendation": pattern_recommendation(pattern_type, value, success),
                "calculation": {
                    "version": LEARNING_VERSION,
                    "formula": "group campaign jobs by deterministic block, parameter, regime, and explicit failure reason; rank by frequency then evidence refs",
                },
            }
        )
    return sorted(rows, key=lambda row: (row["frequency"], row["pattern_type"], row["value"]), reverse=True)


def pattern_description(pattern_type: str, value: str, success: bool) -> str:
    outcome = "succeeded" if success else "failed"
    return f"{pattern_type.replace('_', ' ')}={value} repeatedly {outcome} in stored simulation research."


def pattern_recommendation(pattern_type: str, value: str, success: bool) -> str:
    if success:
        return f"Favor nearby deterministic variants of {value} while continuing out-of-sample confirmation."
    if value == "drawdown_excessive":
        return "Tighten stop placement or reduce reward assumptions before expanding this family."
    if value == "sample_size_too_small":
        return "Relax over-restrictive entry or volume filters before retesting."
    if "ema_20_50" in value:
        return "Test faster EMA pairs such as EMA10/EMA30 before more EMA20/EMA50 variants."
    if "sideways" in value:
        return "Add a stronger trend-strength filter before entering sideways regimes."
    return f"Reduce exposure to {value} until a targeted falsification run improves the evidence."


def build_knowledge_versions(campaign: dict[str, Any], jobs: list[dict[str, Any]], failures: list[dict[str, Any]], successes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    grouped = {
        "successful_indicators": successes,
        "unsuccessful_indicators": failures,
        "strategy_family_statistics": family_statistics(jobs),
        "regime_statistics": regime_statistics(jobs),
    }
    for knowledge_type, evidence in grouped.items():
        if isinstance(evidence, list):
            confidence = min(1.0, sum(int(row.get("frequency") or row.get("tested") or 1) for row in evidence) / max(10, len(jobs)))
            refs = [ref for row in evidence for ref in row.get("evidence_refs", [])][:50]
            summary = {"items": evidence[:20], "campaign_name": campaign.get("name")}
        else:
            confidence = 0.0
            refs = []
            summary = {"items": []}
        key = stable_key(knowledge_type, str(campaign.get("id")), LEARNING_VERSION)
        rows.append(
            {
                "knowledge_key": key,
                "knowledge_type": knowledge_type,
                "campaign_id": campaign.get("id"),
                "source_ref": f"campaign:{campaign.get('id')}",
                "version": int(campaign.get("id") or 0) or 1,
                "evidence": {"evidence_refs": refs},
                "summary": summary,
                "confidence_score": round(confidence, 4),
                "calculation_version": LEARNING_VERSION,
            }
        )
    return rows


def family_statistics(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        grouped[row["strategy_family"]].append(row)
    rows = []
    for family, items in grouped.items():
        promoted = sum(1 for row in items if row["promoted"])
        rejected = sum(1 for row in items if row["rejected"])
        rows.append({"family": family, "tested": len(items), "promoted": promoted, "rejected": rejected, "promotion_rate": round(promoted / len(items), 4)})
    return sorted(rows, key=lambda row: (row["promotion_rate"], row["tested"], row["family"]), reverse=True)


def regime_statistics(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    wins: Counter[str] = Counter()
    for row in jobs:
        for regime in successful_regimes(row) + failing_regimes(row):
            counts[regime] += 1
            if row["promoted"]:
                wins[regime] += 1
    return [{"regime": key, "tested": counts[key], "successes": wins[key], "success_rate": round(wins[key] / counts[key], 4)} for key in sorted(counts)]


def generate_research_recommendations(failures: list[dict[str, Any]], successes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for pattern in failures[:5]:
        rows.append(
            recommendation_row(
                "Resolve recurring failure",
                pattern["description"],
                pattern["recommendation"],
                "high" if pattern["frequency"] >= 3 else "medium",
                pattern["evidence_refs"],
                "Higher validation pass rate by avoiding repeated deterministic failure modes.",
            )
        )
    for pattern in successes[:5]:
        rows.append(
            recommendation_row(
                "Confirm recurring success",
                pattern["description"],
                pattern["recommendation"],
                "medium",
                pattern["evidence_refs"],
                "Better confirmation density around patterns that already passed simulation gates.",
            )
        )
    return dedupe_by_key(rows)


def recommendation_row(title: str, finding: str, recommendation: str, priority: str, refs: list[str], expected: str) -> dict[str, Any]:
    score = min(1.0, len(refs) / 10)
    return {
        "recommendation_key": stable_key(title, finding, recommendation),
        "title": title,
        "finding": finding,
        "recommendation": recommendation,
        "priority": priority,
        "status": "open",
        "evidence_refs": refs,
        "expected_improvement": expected,
        "confidence_score": round(score, 4),
        "explainability": {
            "why": "Generated from deterministic frequency analysis over stored campaign results.",
            "calculation": "confidence_score=min(1, supporting_evidence_refs/10)",
        },
        "calculation_version": LEARNING_VERSION,
    }


def calculate_campaign_confidence(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in jobs:
        if not row["promoted"]:
            continue
        metrics = row["metrics"]
        sample = min(1.0, finite_metric(metrics.get("number_of_trades")) / 100)
        validation = min(1.0, max(0.0, row["validation_score"] / 100))
        consistency = min(1.0, max(0.0, row["consistency_score"]))
        performance = min(1.0, max(0.0, (finite_metric(metrics.get("profit_factor")) - 1) / 1.5))
        drawdown = max(0.0, 1 - finite_metric(metrics.get("max_drawdown")) / 0.25)
        drift = 1.0 - min(1.0, finite_metric((row["result"].get("evidence_drift") or {}).get("drift_score")))
        forward = min(1.0, finite_metric((row["result"].get("forward_validation") or {}).get("pass_rate")))
        paper = min(1.0, max(0.0, finite_metric((row["result"].get("paper_performance") or {}).get("profit_factor")) / 2))
        components = {
            "historical_validation": round(validation, 4),
            "forward_validation": round(forward, 4),
            "paper_performance": round(paper, 4),
            "evidence_drift": round(drift, 4),
            "stability": round(consistency, 4),
            "sample_size": round(sample, 4),
            "performance": round(performance, 4),
            "drawdown": round(drawdown, 4),
            "deployment_age": 0.0,
        }
        score = round(
            (
                components["historical_validation"] * 0.22
                + components["forward_validation"] * 0.14
                + components["paper_performance"] * 0.12
                + components["evidence_drift"] * 0.12
                + components["stability"] * 0.14
                + components["sample_size"] * 0.12
                + components["performance"] * 0.08
                + components["drawdown"] * 0.06
            )
            * 100,
            4,
        )
        rows.append(
            {
                "candidate_id": row["candidate_id"],
                "campaign_id": None,
                "confidence_score": score,
                "components": components,
                "explanation": "Deterministic evidence-confidence score; it measures evidence quality, not future profit.",
                "calculation": {
                    "version": LEARNING_VERSION,
                    "formula": "weighted sum of validation, forward, paper, drift, stability, sample, performance, drawdown, and deployment-age components",
                },
                "evidence_refs": [row["evidence_ref"]],
                "simulation_only": True,
            }
        )
    return sorted(rows, key=lambda item: (item["confidence_score"], item["candidate_id"]), reverse=True)


def generate_evidence_based_mutations(jobs: list[dict[str, Any]], failures: list[dict[str, Any]], successes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    promoted = [row for row in jobs if row["promoted"]]
    if not promoted:
        promoted = sorted(jobs, key=lambda row: (row["validation_score"], row["consistency_score"]), reverse=True)[:3]
    failure_values = {row["value"] for row in failures}
    success_values = {row["value"] for row in successes}
    variants = []
    for parent in promoted[:5]:
        mutations = mutation_options(parent, failure_values, success_values)
        for mutation in mutations[:2]:
            material = {"parent": parent["candidate_id"], "mutation": mutation["changes"]}
            child_id = f"sd_{sha256(repr(material).encode()).hexdigest()[:14]}"
            variants.append(
                {
                    "candidate_id": child_id,
                    "parent_candidate_id": parent["candidate_id"],
                    "campaign_id": None,
                    "mutation": mutation["changes"],
                    "reason": mutation["reason"],
                    "supporting_evidence": mutation["supporting_evidence"],
                    "expected_improvement": mutation["expected_improvement"],
                    "confidence_score": mutation["confidence_score"],
                    "explainability": {
                        "why_generated": mutation["reason"],
                        "calculation": "mutation selected from recurring success/failure patterns; child id is hash(parent, mutation)",
                    },
                    "calculation_version": LEARNING_VERSION,
                }
            )
    return sorted(variants, key=lambda row: (row["confidence_score"], row["candidate_id"]), reverse=True)


def mutation_options(parent: dict[str, Any], failure_values: set[str], success_values: set[str]) -> list[dict[str, Any]]:
    params = parent["parameters"]
    refs = [parent["evidence_ref"]]
    options = []
    if "ema_20_50" in failure_values or (params.get("trend_fast"), params.get("trend_slow")) == (20, 50):
        options.append(mutation("Use faster EMA confirmation after EMA20/EMA50 weakness.", {"trend_fast": 10, "trend_slow": 30}, refs, "Faster trend response may reduce lag-driven rejections.", 0.72))
    if any("atr_multiplier" in value and "3" in value for value in success_values):
        atr = finite_metric(params.get("atr_multiplier") or 3.0)
        options.append(mutation("Stay near repeatedly successful ATR stop range.", {"atr_multiplier": round(max(1.0, min(4.0, atr)), 2)}, refs, "Keeps risk logic near the successful ATR evidence cluster.", 0.68))
    if "drawdown_excessive" in failure_values:
        current = finite_metric(params.get("atr_multiplier") or 2.0)
        options.append(mutation("Reduce excessive drawdown with tighter ATR risk.", {"atr_multiplier": round(max(1.0, current - 0.25), 2)}, refs, "Lower stop distance targets smaller loss tails.", 0.64))
    if "sample_size_too_small" in failure_values:
        rsi = int(params.get("rsi_min") or 55)
        options.append(mutation("Relax restrictive momentum threshold to increase sample size.", {"rsi_min": max(45, rsi - 5)}, refs, "More eligible setups should improve evidence sample size.", 0.6))
    if not options:
        rr = round(finite_metric(params.get("risk_reward") or 2.0) + 0.1, 2)
        options.append(mutation("Local deterministic confirmation around promoted parent.", {"risk_reward": rr}, refs, "Small controlled change preserves explainability while testing nearby reward geometry.", 0.5))
    return options


def mutation(reason: str, changes: dict[str, Any], refs: list[str], expected: str, confidence: float) -> dict[str, Any]:
    return {"reason": reason, "changes": changes, "supporting_evidence": refs, "expected_improvement": expected, "confidence_score": confidence}


def build_adaptive_campaign_plan(jobs: list[dict[str, Any]], failures: list[dict[str, Any]], successes: list[dict[str, Any]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    asset_counts = Counter(row["symbol"] for row in jobs)
    timeframe_counts = Counter(row["timeframe"] for row in jobs)
    family_counts = Counter(row["strategy_family"] for row in jobs)
    under_tested_assets = [key for key, count in sorted(asset_counts.items(), key=lambda item: (item[1], item[0])) if count <= max(1, len(jobs) // max(1, len(asset_counts) * 2))]
    under_tested_timeframes = [key for key, count in sorted(timeframe_counts.items(), key=lambda item: (item[1], item[0])) if count <= max(1, len(jobs) // max(1, len(timeframe_counts) * 2))]
    promising = [row["value"] for row in successes if row["pattern_type"] == "strategy_family_range"][:5]
    if not promising:
        promising = [family for family, _count in family_counts.most_common(5)]
    priorities = []
    priorities.extend({"type": "recommendation", "target": row["recommendation"], "reason": row["finding"]} for row in recommendations[:5])
    priorities.extend({"type": "exploration", "target": asset, "reason": "under-tested asset"} for asset in under_tested_assets[:5])
    priorities.extend({"type": "confirmation", "target": family, "reason": "promising strategy family or most-tested family"} for family in promising[:5])
    return {
        "plan_key": stable_key("adaptive_plan", *(str(row["candidate_id"]) for row in jobs[:10])),
        "priorities": priorities,
        "exploration_targets": [{"asset": asset} for asset in under_tested_assets] + [{"timeframe": timeframe} for timeframe in under_tested_timeframes],
        "confirmation_targets": [{"strategy_family": family} for family in promising],
        "rationale": {
            "exploration": "prioritize under-tested assets, timeframes, parameter ranges, and unresolved recommendations",
            "confirmation": "reserve capacity for recurring success patterns and promising strategy families",
            "duplication_control": "targets are derived from counts so over-tested areas move down the queue",
        },
        "calculation_version": LEARNING_VERSION,
    }


def build_strategy_timeline(campaign: dict[str, Any], jobs: list[dict[str, Any]], confidence: list[dict[str, Any]], evolution: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confidence_by_id = {row["candidate_id"]: row for row in confidence}
    events = []
    for row in jobs:
        events.append(timeline_event(row["candidate_id"], campaign.get("id"), "creation", f"Strategy candidate {row['candidate_id']} entered campaign {campaign.get('id')}.", [row["evidence_ref"]], {"family_id": row["family_id"]}, row.get("created_at")))
        events.append(timeline_event(row["candidate_id"], campaign.get("id"), "validation", f"Validation completed with status {row['status']}.", [row["evidence_ref"]], {"metrics": row["metrics"], "failure_reasons": row["failure_reasons"]}, row.get("completed_at")))
        if row["promoted"]:
            events.append(timeline_event(row["candidate_id"], campaign.get("id"), "promotion", "Promoted because deterministic validation gates passed.", [row["evidence_ref"]], {"confidence": confidence_by_id.get(row["candidate_id"])}, row.get("completed_at")))
        if row["rejected"]:
            events.append(timeline_event(row["candidate_id"], campaign.get("id"), "rejection", f"Rejected because {', '.join(row['failure_reasons']) or 'validation gates failed'}.", [row["evidence_ref"]], {"failure_reasons": row["failure_reasons"]}, row.get("completed_at")))
    for row in evolution:
        events.append(timeline_event(row["candidate_id"], campaign.get("id"), "mutation", row["reason"], row["supporting_evidence"], {"mutation": row["mutation"], "parent_candidate_id": row["parent_candidate_id"]}, None))
    return sorted(events, key=lambda item: (str(item["event_timestamp"]), item["strategy_id"], item["event_type"]))


def timeline_event(strategy_id: str, campaign_id: Any, event_type: str, summary: str, refs: list[str], details: dict[str, Any], timestamp: Any) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "campaign_id": campaign_id,
        "event_type": event_type,
        "summary": summary,
        "evidence_refs": refs,
        "details": details,
        "event_timestamp": timestamp or datetime.now(UTC),
        "calculation_version": LEARNING_VERSION,
        "simulation_only": True,
    }


def rank_elite_candidates(jobs: list[dict[str, Any]], confidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confidence_by_id = {row["candidate_id"]: row["confidence_score"] for row in confidence}
    rows = []
    for row in jobs:
        if not row["promoted"]:
            continue
        metrics = row["metrics"]
        evidence_score = round(confidence_by_id.get(row["candidate_id"], 0) + row["validation_score"] * 0.2 + row["consistency_score"] * 10, 4)
        rows.append(
            {
                "candidate_id": row["candidate_id"],
                "strategy_family": row["strategy_family"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "confidence_score": confidence_by_id.get(row["candidate_id"], 0),
                "ranking_score": evidence_score,
                "components": {
                    "profit_factor": finite_metric(metrics.get("profit_factor")),
                    "expectancy": finite_metric(metrics.get("expectancy_per_trade")),
                    "validation_score": row["validation_score"],
                    "consistency_score": row["consistency_score"],
                },
                "explanation": "Ranked deterministically by evidence confidence, validation score, and consistency.",
            }
        )
    return sorted(rows, key=lambda item: (item["ranking_score"], item["candidate_id"]), reverse=True)


def persist_campaign_learning(conn: psycopg.Connection, learning: dict[str, Any]) -> None:
    campaign_id = learning["campaign_id"]
    for row in learning["knowledge"]:
        conn.execute(
            """
            INSERT INTO research_knowledge_versions(knowledge_key, knowledge_type, campaign_id, source_ref, version, evidence, summary, confidence_score, calculation_version, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (row["knowledge_key"], row["knowledge_type"], campaign_id, row["source_ref"], row["version"], Jsonb(jsonable(row["evidence"])), Jsonb(jsonable(row["summary"])), row["confidence_score"], LEARNING_VERSION),
        )
    for table, rows in (("research_failure_patterns", learning["failure_patterns"]), ("research_success_patterns", learning["success_patterns"])):
        for row in rows:
            conn.execute(
                f"""
                INSERT INTO {table}(pattern_key, campaign_id, pattern_type, description, frequency, evidence_refs, supporting_metrics, recommendation, calculation_version, simulation_only)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (row["pattern_key"], campaign_id, row["pattern_type"], row["description"], row["frequency"], Jsonb(jsonable(row["evidence_refs"])), Jsonb(jsonable(row["supporting_metrics"])), row["recommendation"], LEARNING_VERSION),
            )
    for row in learning["recommendations"]:
        conn.execute(
            """
            INSERT INTO research_recommendations(recommendation_key, campaign_id, title, finding, recommendation, priority, status, evidence_refs, expected_improvement, confidence_score, validation, calculation_version, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (row["recommendation_key"], campaign_id, row["title"], row["finding"], row["recommendation"], row["priority"], row["status"], Jsonb(jsonable(row["evidence_refs"])), row["expected_improvement"], row["confidence_score"], Jsonb({}), LEARNING_VERSION),
        )
    for row in learning["confidence_scores"]:
        conn.execute(
            """
            INSERT INTO research_confidence_history(candidate_id, campaign_id, confidence_score, components, explanation, calculation_version, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            """,
            (row["candidate_id"], campaign_id, row["confidence_score"], Jsonb(jsonable(row["components"])), row["explanation"], LEARNING_VERSION),
        )
    for row in learning["evolution_history"]:
        conn.execute(
            """
            INSERT INTO research_evolution_history(candidate_id, parent_candidate_id, campaign_id, mutation, reason, supporting_evidence, expected_improvement, confidence_score, calculation_version, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (row["candidate_id"], row["parent_candidate_id"], campaign_id, Jsonb(jsonable(row["mutation"])), row["reason"], Jsonb(jsonable(row["supporting_evidence"])), row["expected_improvement"], row["confidence_score"], LEARNING_VERSION),
        )
    for row in learning["timeline"]:
        conn.execute(
            """
            INSERT INTO research_timeline_events(strategy_id, campaign_id, event_type, summary, evidence_refs, details, event_timestamp, simulation_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
            """,
            (row["strategy_id"], campaign_id, row["event_type"], row["summary"], Jsonb(jsonable(row["evidence_refs"])), Jsonb(jsonable(row["details"])), row["event_timestamp"]),
        )
    plan = learning["campaign_plan"]
    conn.execute(
        """
        INSERT INTO research_campaign_plans(plan_key, campaign_id, priorities, exploration_targets, confirmation_targets, rationale, calculation_version, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
        """,
        (plan["plan_key"], campaign_id, Jsonb(jsonable(plan["priorities"])), Jsonb(jsonable(plan["exploration_targets"])), Jsonb(jsonable(plan["confirmation_targets"])), Jsonb(jsonable(plan["rationale"])), LEARNING_VERSION),
    )


def research_learning_summary(conn: psycopg.Connection, limit: int = 5) -> dict[str, Any]:
    ensure_research_learning_tables(conn)
    failures = fetch_rows(conn, "research_failure_patterns", limit)
    successes = fetch_rows(conn, "research_success_patterns", limit)
    recommendations = fetch_rows(conn, "research_recommendations", limit, "status = 'open' AND simulation_only = TRUE", "created_at DESC")
    confidence = conn.execute(
        """
        SELECT confidence_score
        FROM research_confidence_history
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 100
        """
    ).fetchall()
    scores = [finite_metric(row.get("confidence_score")) for row in confidence]
    distribution = {
        "high": sum(1 for score in scores if score >= 75),
        "medium": sum(1 for score in scores if 50 <= score < 75),
        "low": sum(1 for score in scores if score < 50),
    }
    return {
        "current_priorities": [row.get("recommendation") for row in recommendations],
        "strongest_emerging_ideas": [row.get("description") for row in successes],
        "recurring_failures": [row.get("description") for row in failures],
        "recurring_successes": [row.get("description") for row in successes],
        "recommendation_queue": [jsonable(row) for row in recommendations],
        "evolving_strategy_families": [row.get("value") for row in successes if row.get("pattern_type") == "strategy_family_range"],
        "confidence_distribution": distribution,
        "safety": {"simulation_only": True, "statement": SAFETY_STATEMENT},
    }


def fetch_rows(conn: psycopg.Connection, table: str, limit: int, where: str = "simulation_only = TRUE", order_by: str = "frequency DESC, created_at DESC") -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE {where}
            ORDER BY {order_by}
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    ]


def get_learning_table(conn: psycopg.Connection, table: str, limit: int = 100) -> dict[str, Any]:
    ensure_research_learning_tables(conn)
    rows = conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE simulation_only = TRUE
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return {"rows": [jsonable(dict(row)) for row in rows], "calculation_version": LEARNING_VERSION, "simulation_only": True}


def get_strategy_timeline(conn: psycopg.Connection, strategy_id: str, limit: int = 100) -> dict[str, Any]:
    ensure_research_learning_tables(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM research_timeline_events
        WHERE simulation_only = TRUE AND strategy_id = %s
        ORDER BY event_timestamp ASC, id ASC
        LIMIT %s
        """,
        (strategy_id, limit),
    ).fetchall()
    return {"strategy_id": strategy_id, "timeline": [jsonable(dict(row)) for row in rows], "simulation_only": True}


def parameter_buckets(parameters: dict[str, Any]) -> dict[str, str]:
    buckets = {}
    for key in ("trend_fast", "trend_slow", "rsi_min", "rsi_max", "risk_reward", "atr_multiplier", "max_holding_bars", "volume_change_min"):
        if key in parameters and parameters[key] is not None:
            buckets[key] = bucket_value(key, parameters[key])
    return buckets


def bucket_value(key: str, value: Any) -> str:
    numeric = finite_metric(value)
    if key in {"trend_fast", "trend_slow", "max_holding_bars"}:
        return f"{key}:{int(numeric)}"
    if key in {"risk_reward", "atr_multiplier"}:
        return f"{key}:{round(numeric * 2) / 2:.1f}"
    if key.startswith("rsi"):
        return f"{key}:{int(round(numeric / 5) * 5)}"
    return f"{key}:{round(numeric, 2)}"


def failing_regimes(row: dict[str, Any]) -> list[str]:
    return regime_rows(row, positive=False)


def successful_regimes(row: dict[str, Any]) -> list[str]:
    return regime_rows(row, positive=True)


def regime_rows(row: dict[str, Any], *, positive: bool) -> list[str]:
    regimes = []
    analysis = row["result"].get("regime_analysis") or {}
    for bucket in ("by_market_regime", "by_volatility_regime"):
        for item in analysis.get(bucket) or []:
            metrics = item.get("metrics") or {}
            expectancy = finite_metric(metrics.get("expectancy_per_trade"))
            pf = finite_metric(metrics.get("profit_factor"))
            if positive and (expectancy > 0 or pf >= 1.2):
                regimes.append(str(item.get("regime") or item.get("condition") or "unknown"))
            if not positive and (expectancy < 0 or (pf and pf < 1.0)):
                regimes.append(str(item.get("regime") or item.get("condition") or "unknown"))
    return sorted(set(regimes))


def strategy_family(blocks: dict[str, Any]) -> str:
    if not blocks:
        return "unknown"
    return "+".join(str(blocks.get(key, "")) for key in ("trend", "momentum", "entry", "exit") if blocks.get(key))


def average(values: Any) -> float:
    nums = [finite_metric(value) for value in values if value is not None]
    return round(sum(nums) / len(nums), 4) if nums else 0.0


def stable_key(*parts: str) -> str:
    return sha256("|".join(parts).encode()).hexdigest()[:24]


def dedupe_by_key(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for row in rows:
        key = row["recommendation_key"]
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
