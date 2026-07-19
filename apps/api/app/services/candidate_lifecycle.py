from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.promising_research import build_promising_research_candidates
from app.services.strategy_research import finite_metric


LIFECYCLE_STATES = (
    "Hypothesis",
    "Experimenting",
    "Promising",
    "Needs More Evidence",
    "Alpha Validation",
    "Validated",
    "Archived",
    "Rejected",
)

METRIC_DEFINITIONS = {
    "profit_factor": {
        "label": "Profit Factor",
        "measures": "Gross profit divided by gross loss.",
        "why_it_matters": "It shows whether winning trades are large enough to offset losing trades before deeper validation.",
        "calculation": "sum(winning PnL) / abs(sum(losing PnL)); no edge is claimed unless validation gates pass.",
    },
    "stability_score": {
        "label": "Stability",
        "measures": "Share of evaluated datasets or groups with positive expectancy and profit factor at or above 1.",
        "why_it_matters": "A candidate that only works in one pocket can disappear when market conditions change.",
        "calculation": "profitable groups / evaluated groups.",
    },
    "trade_count": {
        "label": "Trade Count",
        "measures": "Number of simulated historical trades.",
        "why_it_matters": "Low sample size makes profit factor and expectancy fragile.",
        "calculation": "Count of completed backtest trades after entry and exit rules are applied.",
    },
    "drawdown": {
        "label": "Drawdown",
        "measures": "Largest peak-to-trough equity decline.",
        "why_it_matters": "High drawdown can indicate unstable risk or poor stop/exit behavior.",
        "calculation": "max((equity peak - equity value) / equity peak).",
    },
    "research_score": {
        "label": "Research Score",
        "measures": "Composite research ranking score, not a validation result.",
        "why_it_matters": "It ranks ideas for further research using quality and robustness, not just profit factor.",
        "calculation": "Weighted PF, expectancy, stability, cross-asset consistency, OOS score, trade count, and drawdown penalty.",
    },
    "cross_asset_consistency": {
        "label": "Cross-Asset Consistency",
        "measures": "How often a candidate works across available assets.",
        "why_it_matters": "Single-asset behavior can be curve-fit or regime-specific.",
        "calculation": "assets with at least one profitable timeframe / evaluated assets.",
    },
    "out_of_sample_score": {
        "label": "Out-of-Sample Score",
        "measures": "How often test-period performance remains profitable.",
        "why_it_matters": "In-sample-only performance is not enough evidence.",
        "calculation": "profitable test windows / evaluated test windows.",
    },
}


def build_research_portfolio(conn: psycopg.Connection, max_candidates: int = 24) -> dict[str, Any]:
    ensure_lifecycle_tables(conn)
    evidence = build_promising_research_candidates(conn, max_candidates=max_candidates, max_runs_per_experiment=6, fold_count=2)
    candidates = []
    for candidate in evidence["candidates"]:
        status = infer_lifecycle_status(candidate)
        reason = transition_reason(candidate, status)
        record_lifecycle_state(conn, candidate, status, reason)
        events = load_candidate_events(conn, candidate["candidate_id"])
        candidates.append(
            {
                **candidate,
                "lifecycle_status": status,
                "lifecycle_events": events,
                "evidence_drift": detect_evidence_drift(events, candidate),
                "research_notebook": build_research_notebook(candidate, status, reason),
            }
        )
    conn.commit()
    return {
        "states": list(LIFECYCLE_STATES),
        "summary": summarize_portfolio(candidates),
        "metric_definitions": METRIC_DEFINITIONS,
        "timeline": build_evidence_timeline(candidates),
        "comparison": build_candidate_comparison(candidates),
        "clusters": strongest_evidence_clusters(candidates),
        "candidates": candidates,
    }


def ensure_lifecycle_tables(conn: psycopg.Connection) -> None:
    return None


def infer_lifecycle_status(candidate: dict[str, Any]) -> str:
    validation_status = candidate.get("validation_status", "")
    metrics = candidate["aggregate_metrics"]
    if validation_status == "Research candidate for alpha validation":
        return "Alpha Validation"
    if validation_status == "Needs more evidence":
        return "Needs More Evidence"
    if finite_metric(metrics.get("profit_factor")) >= 1.0 and finite_metric(metrics.get("number_of_trades")) > 0:
        return "Promising"
    if candidate.get("research_score", 0) > 0:
        return "Experimenting"
    return "Rejected"


def transition_reason(candidate: dict[str, Any], status: str) -> str:
    metrics = candidate["aggregate_metrics"]
    if status == "Alpha Validation":
        return "Candidate has enough composite research evidence to queue for formal alpha validation."
    if status == "Needs More Evidence":
        return "Candidate has positive research evidence but insufficient sample size or robustness for validation."
    if status == "Promising":
        return "Candidate has a profitable research pocket but still needs broader evidence."
    if status == "Experimenting":
        return "Candidate remains under active research with incomplete evidence."
    return (
        f"Rejected for now: PF={finite_metric(metrics.get('profit_factor')):.2f}, "
        f"trades={int(finite_metric(metrics.get('number_of_trades')))}, score={candidate.get('research_score')}."
    )


def record_lifecycle_state(conn: psycopg.Connection, candidate: dict[str, Any], status: str, reason: str) -> None:
    candidate_id = candidate["candidate_id"]
    previous = conn.execute(
        """
        SELECT to_state
        FROM candidate_lifecycle_events
        WHERE candidate_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    previous_state = previous["to_state"] if previous else None
    if previous_state == status:
        return
    conn.execute(
        """
        INSERT INTO candidate_lifecycle_events(candidate_id, from_state, to_state, reason, metrics)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (candidate_id, previous_state, status, reason, Jsonb(snapshot_metrics(candidate))),
    )


def snapshot_metrics(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = candidate["aggregate_metrics"]
    return {
        "research_score": candidate.get("research_score"),
        "profit_factor": metrics.get("profit_factor"),
        "expectancy_per_trade": metrics.get("expectancy_per_trade"),
        "number_of_trades": metrics.get("number_of_trades"),
        "max_drawdown": metrics.get("max_drawdown"),
        "stability_score": candidate.get("stability_score"),
        "cross_asset_consistency": candidate.get("cross_asset_consistency"),
        "out_of_sample_score": candidate.get("out_of_sample_score"),
    }


def load_candidate_events(conn: psycopg.Connection, candidate_id: str) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT id, candidate_id, from_state, to_state, reason, metrics, created_at
            FROM candidate_lifecycle_events
            WHERE candidate_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (candidate_id,),
        ).fetchall()
    )


def detect_evidence_drift(events: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any]:
    if not events:
        return {"status": "No baseline", "score_delta": 0, "robustness_delta": 0, "message": "No prior evidence snapshot exists."}
    baseline = events[0]["metrics"]
    current = snapshot_metrics(candidate)
    score_delta = finite_metric(current.get("research_score")) - finite_metric(baseline.get("research_score"))
    robustness_delta = finite_metric(current.get("out_of_sample_score")) - finite_metric(baseline.get("out_of_sample_score"))
    drifted = score_delta < -10 or robustness_delta < -0.25
    return {
        "status": "Drifting" if drifted else "Stable",
        "score_delta": score_delta,
        "robustness_delta": robustness_delta,
        "message": "Evidence quality has weakened versus the first snapshot." if drifted else "No meaningful evidence decay detected.",
    }


def build_research_notebook(candidate: dict[str, Any], status: str, reason: str) -> str:
    metrics = candidate["aggregate_metrics"]
    return "\n".join(
        [
            f"# {candidate['candidate_id']} Research Notebook",
            "",
            "## What Was Tested",
            f"{candidate['title']} using parameters: {candidate['parameters']}",
            "",
            "## What Changed",
            candidate.get("research_report", "Experiment parameters were evaluated across available assets and timeframes."),
            "",
            "## What Improved",
            f"Research score {candidate['research_score']}; PF {metrics.get('profit_factor')}; OOS score {candidate.get('out_of_sample_score')}.",
            "",
            "## What Failed",
            f"Failed datasets: {', '.join(candidate.get('assets_failed', [])) or 'None recorded'}.",
            "",
            "## Lifecycle Decision",
            f"{status}: {reason}",
            "",
            "## Next Research Recommendation",
            candidate.get("recommended_next_experiment", "Collect more evidence before validation."),
        ]
    )


def build_evidence_timeline(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for candidate in candidates:
        base_timestamp = candidate["lifecycle_events"][0]["created_at"] if candidate["lifecycle_events"] else datetime.now(UTC)
        events.extend(
            [
                {
                    "timestamp": base_timestamp,
                    "candidate_id": candidate["candidate_id"],
                    "event_type": "experiment_created",
                    "summary": f"{candidate['title']} was generated from {candidate['experiment_id']}.",
                    "reason": "Candidate entered the research portfolio for evidence tracking.",
                },
                {
                    "timestamp": base_timestamp,
                    "candidate_id": candidate["candidate_id"],
                    "event_type": "parameter_changes",
                    "summary": ", ".join(f"{key}={value}" for key, value in candidate.get("parameters", {}).items()) or "No parameter overrides recorded.",
                    "reason": "Tracked parameter set used for the current research candidate.",
                },
                {
                    "timestamp": datetime.now(UTC),
                    "candidate_id": candidate["candidate_id"],
                    "event_type": "validation_run",
                    "summary": candidate["validation_status"],
                    "reason": "Validation standards remain unchanged; this records research readiness only.",
                },
            ]
        )
        for event in candidate["lifecycle_events"]:
            events.append(
                {
                    "timestamp": event["created_at"],
                    "candidate_id": candidate["candidate_id"],
                    "event_type": "promotion_rejection_decision",
                    "summary": f"{event.get('from_state') or 'New'} -> {event['to_state']}",
                    "reason": event["reason"],
                }
            )
        events.extend(
            [
                {
                    "timestamp": datetime.now(UTC),
                    "candidate_id": candidate["candidate_id"],
                    "event_type": "cross_asset_results",
                    "summary": candidate["evidence_summary"],
                    "reason": candidate["recommended_next_experiment"],
                },
                {
                    "timestamp": datetime.now(UTC),
                    "candidate_id": candidate["candidate_id"],
                    "event_type": "research_notes",
                    "summary": "Research notebook generated.",
                    "reason": candidate.get("recommended_next_experiment", "Collect more evidence before validation."),
                },
            ]
        )
    return sorted(events, key=lambda row: str(row["timestamp"]), reverse=True)[:100]


def build_candidate_comparison(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for candidate in candidates:
        metrics = candidate["aggregate_metrics"]
        rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "strategy": candidate["strategy_name"],
                "profit_factor": metrics.get("profit_factor"),
                "stability": candidate["stability_score"],
                "trade_count": metrics.get("number_of_trades"),
                "drawdown": metrics.get("max_drawdown"),
                "research_score": candidate["research_score"],
                "assets": sorted({item.split()[0] for item in candidate.get("assets_worked", []) + candidate.get("assets_failed", [])}),
                "timeframes": sorted({item.split()[1] for item in candidate.get("assets_worked", []) + candidate.get("assets_failed", []) if len(item.split()) > 1}),
                "validation_status": candidate["validation_status"],
                "lifecycle_status": candidate["lifecycle_status"],
            }
        )
    return rows


def strongest_evidence_clusters(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        for worked in candidate.get("assets_worked", []):
            clusters[worked].append(candidate)
    rows = []
    for cluster, members in clusters.items():
        rows.append(
            {
                "cluster": cluster,
                "candidate_count": len(members),
                "avg_score": sum(float(row["research_score"]) for row in members) / len(members),
                "top_candidate": sorted(members, key=lambda row: row["research_score"], reverse=True)[0]["candidate_id"],
            }
        )
    return sorted(rows, key=lambda row: row["avg_score"], reverse=True)[:12]


def summarize_portfolio(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate["lifecycle_status"]] = counts.get(candidate["lifecycle_status"], 0) + 1
    return {
        "total_candidates": len(candidates),
        "state_counts": counts,
        "active_candidates": counts.get("Experimenting", 0) + counts.get("Promising", 0) + counts.get("Needs More Evidence", 0),
        "validation_queue": counts.get("Alpha Validation", 0),
        "rejected": counts.get("Rejected", 0),
        "archived": counts.get("Archived", 0),
    }
