"""Phase F: the research-quality control panel.

One view answering the question the campaign dashboards never could: is this
research process trustworthy right now? It deliberately reports process
health rather than strategy performance, because a system that cannot tell a
broken simulator from a weak hypothesis will keep producing confident numbers
either way.

Every panel is sourced from the phase that owns it -- the simulator audit
(A), null models (B), response surfaces (C), splits and multiple testing (E),
diagnostics and the graveyard (F) -- rather than recomputed here, so the
dashboard cannot drift away from the checks it claims to summarize.

Panels degrade to an explicit "unavailable" instead of a zero. A dashboard
that shows 0 when it means "not measured" is worse than one that shows
nothing, because it reads as a clean bill of health.
"""

from __future__ import annotations

from typing import Any

import psycopg

DASHBOARD_VERSION = "research_quality_dashboard_v1"


def _panel(value: Any, *, unavailable_reason: str | None = None) -> dict[str, Any]:
    if unavailable_reason:
        return {"available": False, "reason": unavailable_reason}
    return {"available": True, "value": value}


def _simulator_panel() -> dict[str, Any]:
    from app.services.simulator_audit import run_simulator_audit

    audit = run_simulator_audit()
    return {
        "simulator_sound": audit["simulator_sound"],
        "defects": audit["defects"],
        "known_biases": audit["optimistic_or_pessimistic_biases"],
        "economics_findings": audit["economics_findings"],
        "verdict": audit["verdict"],
        "blocks_all_conclusions": not audit["simulator_sound"],
    }


def _leakage_panel(conn: psycopg.Connection, dataset_id: int | None) -> dict[str, Any]:
    """Data-leakage checks: are the three windows real, and how much of the
    validation budget has already been spent?"""
    from app.services.research_splits import get_dataset_splits, split_usage_summary

    if dataset_id is None:
        return _panel(None, unavailable_reason="campaign has no dataset snapshot")
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        return _panel(
            None,
            unavailable_reason=(
                "no nested splits recorded for this dataset; it predates Phase E and cannot support "
                "a locked confirmation"
            ),
        )
    usage = split_usage_summary(conn, dataset_id)
    return _panel(
        {
            "splits": splits.as_dict(),
            "uses_by_phase": usage["uses_by_phase"],
            "validation_is_effectively_training": usage["validation_is_effectively_training"],
            "confirmation_is_spent": usage["confirmation_is_spent"],
        }
    )


def _multiple_testing_panel(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    from app.services.research_splits import multiple_testing_ledger

    ledger = multiple_testing_ledger(conn, campaign_id)
    return _panel(
        {
            "strategies_tested": ledger["variants_tested"],
            "families_tested": ledger["families_tested"],
            "symbols_tested": ledger["symbols_tested"],
            "effective_independent_hypotheses": ledger["effective_trials"],
            "lineage": ledger["lineage"],
            "burden_note": (
                "effective_independent_hypotheses is the trial count a deflated Sharpe must be judged "
                "against; the variant count alone understates the search."
            ),
        }
    )


def _response_surface_panel(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    from app.services.labs.intraday.response_surface import family_response_surface_report

    report = family_response_surface_report(conn, campaign_id)
    families = report["families"]
    if not families:
        return _panel(None, unavailable_reason="no family has trade-level evidence in this campaign")

    stable = [
        row["architecture"]
        for row in families
        if row["scenarios"]["as_simulated"]["stable_region"]["size"] >= 3
    ]
    return _panel(
        {
            "families_analyzed": report["families_analyzed"],
            "families_with_stable_positive_regions": stable,
            "promising_as_simulated": report["promising_as_simulated"],
            "promising_at_realistic_costs": report["promising_at_realistic_costs"],
            "cost_sensitive_families": report["cost_sensitive_families"],
        }
    )


def _diagnostics_panel(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    from app.services.research_diagnostics import campaign_diagnostics_report

    report = campaign_diagnostics_report(conn, campaign_id)
    if not report["families"]:
        return _panel(None, unavailable_reason="no family has trade-level evidence in this campaign")

    counts = report["failure_reason_counts"]
    by_reason: dict[str, list[str]] = {}
    concentration: list[dict[str, Any]] = []
    for family in report["families"]:
        reason = str(family["diagnosis"].get("failure_reason"))
        by_reason.setdefault(reason, []).append(family["architecture"])
        share = family["decomposition"].get("largest_symbol_profit_share")
        if share is not None:
            concentration.append({"architecture": family["architecture"], "largest_symbol_profit_share": share})
    return _panel(
        {
            "failure_reason_counts": counts,
            "rejected_for_no_raw_edge": by_reason.get("NO_RAW_SIGNAL", []),
            "rejected_because_of_costs": by_reason.get("COST_DESTROYED_SIGNAL", []),
            "rejected_for_instability": (
                by_reason.get("ONE_SYMBOL_DEPENDENCE", []) + by_reason.get("POOR_REGIME_TARGETING", [])
            ),
            "evidence_concentration": sorted(
                concentration, key=lambda row: row["largest_symbol_profit_share"], reverse=True
            ),
            "next_experiments": [
                {
                    "architecture": family["architecture"],
                    "recommendation": family["next_experiment"]["recommendation"],
                    "changes": family["next_experiment"].get("changes"),
                }
                for family in report["families"]
            ],
        }
    )


def _confirmation_panel(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    from app.services.research_splits import confirmation_status

    status = confirmation_status(conn, campaign_id=campaign_id)
    return _panel(
        {
            "confirmations_run": status["confirmations_run"],
            "confirmations_passed": status["confirmations_passed"],
            "candidates_passing_final_confirmation": [
                row["candidate_id"] for row in status["runs"] if row["passed"]
            ],
            "candidates_failing_final_confirmation": [
                row["candidate_id"] for row in status["runs"] if not row["passed"]
            ],
        }
    )


def research_quality_dashboard(conn: psycopg.Connection, campaign_id: int) -> dict[str, Any]:
    """Process health for one campaign, assembled from every phase's own checks."""
    campaign = conn.execute(
        "SELECT id, dataset_id, status FROM research_campaigns WHERE id = %s", (campaign_id,)
    ).fetchone()
    if not campaign:
        raise ValueError(f"campaign {campaign_id} not found")

    simulator = _simulator_panel()
    dataset_id = campaign["dataset_id"]
    panels = {
        "simulator_audit": simulator,
        "data_leakage_checks": _leakage_panel(conn, int(dataset_id) if dataset_id is not None else None),
        "multiple_testing": _multiple_testing_panel(conn, campaign_id),
        "response_surface": _response_surface_panel(conn, campaign_id),
        "loss_diagnostics": _diagnostics_panel(conn, campaign_id),
        "locked_confirmation": _confirmation_panel(conn, campaign_id),
    }

    blockers: list[str] = []
    if not simulator["simulator_sound"]:
        blockers.append("simulator has defects; no strategy conclusion from this campaign is trustworthy")
    leakage = panels["data_leakage_checks"]
    if leakage["available"] and leakage["value"]["validation_is_effectively_training"]:
        blockers.append("validation window has been consulted enough times to count as training")
    if not leakage["available"]:
        blockers.append(f"data-leakage checks unavailable: {leakage['reason']}")

    return {
        "campaign_id": campaign_id,
        "dashboard_version": DASHBOARD_VERSION,
        "campaign_status": campaign["status"],
        "trustworthy": not blockers,
        "blockers": blockers,
        "panels": panels,
        "reading_note": (
            "Panels report process health, not strategy performance. An unavailable panel is not a passing "
            "one -- it means the check could not be made."
        ),
    }
