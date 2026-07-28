"""One resolved plan for a broad 15m/30m screen, shared by preview and launch.

The UI needs to show what a launch will actually do -- how many families, how
many assets, how many jobs -- before the user commits. The tempting shortcut
is to multiply those numbers in the frontend, which quietly creates a second
implementation of the launcher's arithmetic that drifts the moment either
side changes. (The displayed "12 variants per family" was already a hardcoded
string rather than a resolved value.)

So the plan is built here, from the same registry, the same candidate
generators, and the same deduplication `queue_campaign_jobs` applies. The
preview and the launch cannot disagree because they resolve the same way.

This module makes no research decisions. It resolves configuration, counts
what would be queued, reports the protocol versions in force, and surfaces
blockers from checks that already exist -- the Phase A simulator audit above
all, because a campaign run on a defective simulator produces confident
numbers that mean nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg

CAMPAIGN_PLAN_VERSION = "intraday_campaign_plan_v1"

DEFAULT_ASSET_LIMIT = 10
DEFAULT_VARIANTS_PER_FAMILY = 12


def active_family_definitions() -> list[dict[str, Any]]:
    """Every family the registry currently marks active, never the archived ones.

    Archived families keep their evidence and stay visible in the UI, but a
    broad screen must not spend compute on them -- Phase 12.4 already
    concluded they had no edge and the standing instruction is to stop tuning
    them.
    """
    from app.services.labs.intraday.families.registry import FAMILY_REGISTRY

    return [
        {
            "architecture": architecture,
            "name": definition.name,
            "status": definition.status,
            "supported_timeframes": list(definition.supported_timeframes),
        }
        for architecture, definition in sorted(FAMILY_REGISTRY.items())
        if definition.status == "active"
    ]


def _cost_model() -> dict[str, Any]:
    from app.services.labs.intraday.families.v2.base import BASE_V2_PARAMETERS

    fee = float(BASE_V2_PARAMETERS["fee_rate"])
    slippage = float(BASE_V2_PARAMETERS["slippage_rate"])
    return {
        # Derived from the live parameters rather than a stored constant, so a
        # changed cost assumption cannot be reported under a stale label.
        "version": f"fee_{fee:g}_slippage_{slippage:g}_per_leg",
        "fee_rate_per_leg": fee,
        "slippage_rate_per_leg": slippage,
        "round_trip_rate": round(2 * (fee + slippage), 6),
    }


def build_campaign_plan(
    conn: psycopg.Connection,
    *,
    timeframes: list[str] | None = None,
    asset_limit: int = DEFAULT_ASSET_LIMIT,
    variants_per_family: int = DEFAULT_VARIANTS_PER_FAMILY,
) -> dict[str, Any]:
    """Resolve a broad-screen launch without running it.

    `estimated_jobs` is computed from the deduplicated candidate set, matching
    what `queue_campaign_jobs` will insert, so the preview does not promise a
    number the launch then contradicts.
    """
    from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
    from app.services.research_campaigns import (
        ELITE_PROMOTION_RULE_VERSION,
        dedupe_candidates_by_execution_key,
        ensure_campaign_tables,
        get_universe,
        seed_default_universes,
    )
    from app.services.research_splits import SPLIT_VERSION

    families = active_family_definitions()
    supported = sorted({tf for family in families for tf in family["supported_timeframes"]})
    requested = [tf for tf in (timeframes if timeframes is not None else supported)]
    selected_timeframes = [tf for tf in supported if tf in requested]

    assets: list[str] = []
    try:
        ensure_campaign_tables(conn)
        seed_default_universes(conn)
        universe = get_universe(conn, "research_core_ten")
        assets = [str(asset).upper() for asset in (universe.get("assets") or [])][:asset_limit]
    except Exception:  # noqa: BLE001 - a preview must not fail because the universe is unreadable
        assets = []

    candidates: list[Any] = []
    for family in families:
        definition = FAMILY_REGISTRY[family["architecture"]]
        candidates.extend(definition.candidate_generator(max_candidates=variants_per_family))
    deduped = dedupe_candidates_by_execution_key(candidates, len(candidates))

    estimated_jobs = len(deduped) * len(assets) * len(selected_timeframes)

    duplicate_of = _existing_campaign_for_configuration(
        conn,
        family_ids=[family["architecture"] for family in families],
        assets=assets,
        timeframes=selected_timeframes,
        # The campaign key is built from the RAW generated count, before the
        # per-job dedupe -- matching `_create_intraday_campaign` exactly, so
        # this lookup finds the same row a launch would collide with.
        candidate_count=len(candidates),
    )

    audit = _simulator_audit_summary()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if audit["defects"]:
        blockers.append(
            {
                "code": "SIMULATOR_DEFECT",
                "detail": (
                    "The shared execution path failed its audit "
                    f"({', '.join(audit['defects'])}). Fix the simulator before judging any strategy."
                ),
            }
        )
    if not families:
        blockers.append({"code": "NO_ACTIVE_FAMILIES", "detail": "The registry has no active families to screen."})
    if not selected_timeframes:
        blockers.append({"code": "NO_TIMEFRAME_SELECTED", "detail": "Select at least one timeframe."})
    if not assets:
        blockers.append({"code": "NO_ASSETS_RESOLVED", "detail": "The research_core_ten universe resolved to no assets."})
    if not deduped:
        blockers.append({"code": "NO_CANDIDATES", "detail": "The active families generated no candidates."})

    signals = _signal_diagnostics_summary(conn, selected_timeframes)
    if signals["measured_families"] == 0:
        warnings.append(
            {
                "code": "SIGNAL_NOT_MEASURED",
                "detail": (
                    "No family's signal has been measured on this timeframe. A campaign is the most "
                    "expensive way to discover a signal predicts nothing -- run signal diagnostics first."
                ),
            }
        )
    elif not signals["predictive"]:
        warnings.append(
            {
                "code": "NO_PREDICTIVE_FAMILY",
                "detail": (
                    f"None of the {signals['measured_families']} measured families shows predictive content "
                    "that clears costs. This screen will spend its full job budget confirming that."
                ),
            }
        )

    if duplicate_of is not None:
        # Not a blocker. Re-running the same screen against a rolling dataset
        # that has since advanced is legitimate research; re-running it against
        # unchanged data is wasted compute that also inflates the
        # multiple-testing count. The caller has to say which it means, so this
        # is surfaced rather than silently resolved either way.
        warnings.append(
            {
                "code": "DUPLICATE_CONFIGURATION",
                "detail": (
                    f"This exact family/asset/timeframe/candidate configuration already ran as campaign "
                    f"{duplicate_of}. Launching again requires confirming a re-run, which records it as a "
                    "separate campaign."
                ),
            }
        )

    for finding in audit["economics_findings"]:
        warnings.append(
            {
                "code": "COST_MODEL_UNECONOMIC",
                "detail": (
                    f"Simulator audit reports '{finding}': at the configured cost rates, round-trip costs "
                    "consume a large share of the risk unit. The screen will run, but weak results may "
                    "reflect the cost model rather than the strategies."
                ),
            }
        )

    return {
        "plan_version": CAMPAIGN_PLAN_VERSION,
        "active_family_count": len(families),
        "active_families": families,
        "asset_count": len(assets),
        "assets": assets,
        "timeframes_supported": supported,
        "timeframes_selected": selected_timeframes,
        "variants_per_family": variants_per_family,
        "candidates_generated": len(candidates),
        "candidates_after_dedupe": len(deduped),
        "estimated_jobs": estimated_jobs,
        "duplicate_of_campaign_id": duplicate_of,
        "requires_rerun_confirmation": duplicate_of is not None,
        "signal_diagnostics": signals,
        "protocol": {
            "split_protocol_version": SPLIT_VERSION,
            "elite_gate_version": ELITE_PROMOTION_RULE_VERSION,
            "cost_model": _cost_model(),
            "simulator_audit": audit,
        },
        "blockers": blockers,
        "warnings": warnings,
        "can_launch": not blockers,
        "evidence_policy": (
            "Each family keeps its own candidates and is evaluated independently through the unmodified "
            "elite gate. Evidence is never merged across families."
        ),
    }


def _signal_diagnostics_summary(
    conn: psycopg.Connection, timeframes: list[str]
) -> dict[str, Any]:
    """Stored signal verdicts for the selected timeframes.

    Read-only and advisory: a never-measured family is reported as such rather
    than assumed fine, because "not checked" and "checked and passed" must not
    look the same on a launch screen.
    """
    from app.services.signal_diagnostics import list_signal_diagnostics

    rows: list[dict[str, Any]] = []
    for timeframe in timeframes:
        try:
            rows.extend(list_signal_diagnostics(conn, timeframe=timeframe))
        except Exception:  # noqa: BLE001 - a preview must not fail on an unreadable table
            continue

    verdicts: dict[str, int] = {}
    for row in rows:
        verdict = str(row.get("verdict"))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    return {
        "measured_families": len(rows),
        "verdict_counts": dict(sorted(verdicts.items())),
        "predictive": sorted({str(row["architecture"]) for row in rows if row.get("verdict") == "predictive"}),
        "signal_below_cost": sorted(
            {str(row["architecture"]) for row in rows if row.get("verdict") == "signal_below_cost"}
        ),
    }


def rerun_campaign_label() -> str:
    """A label that makes a re-run its own campaign rather than a collision.

    `research_campaign_key` hashes universe, assets, timeframes, candidate
    count, architecture and variant -- but NOT the dataset snapshot. So an
    identical screen re-run against a rolling dataset that has since advanced
    still produces the same key and collides with the earlier campaign. A
    distinct label is the mechanism the key already provides for "same
    configuration, different research question", and it is what the
    low-timeframe and focused-expansion launchers already use.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    return f"rerun_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}"


def _existing_campaign_for_configuration(
    conn: psycopg.Connection,
    *,
    family_ids: list[str],
    assets: list[str],
    timeframes: list[str],
    candidate_count: int,
) -> int | None:
    """The campaign an unlabeled launch of this configuration would collide with.

    Recomputes the same `research_campaign_key` the launcher builds so the
    preview can warn before the click, instead of the user discovering the
    collision from a 422.
    """
    if not family_ids or not assets or not timeframes or not candidate_count:
        return None
    from app.services.research_campaigns import research_campaign_key

    architecture = f"multi_family:{','.join(sorted(family_ids))}"
    campaign_key = research_campaign_key(
        "research_core_ten",
        assets,
        timeframes,
        candidate_count,
        search_mode=architecture,
        variant=architecture,
    )
    try:
        row = conn.execute(
            "SELECT id FROM research_campaigns WHERE campaign_key = %s", (campaign_key,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - a preview must not fail on an unreadable table
        return None
    return int(row["id"]) if row else None


def _simulator_audit_summary() -> dict[str, Any]:
    """Audit headline only -- the full report has its own endpoint."""
    from app.services.simulator_audit import AUDIT_VERSION, run_simulator_audit

    try:
        audit = run_simulator_audit()
    except Exception as error:  # noqa: BLE001 - an unavailable audit is a blocker, not a crash
        return {
            "audit_version": AUDIT_VERSION,
            "simulator_sound": False,
            "defects": ["audit_did_not_run"],
            "economics_findings": [],
            "detail": str(error),
        }
    return {
        "audit_version": audit["audit_version"],
        "simulator_sound": audit["simulator_sound"],
        "defects": audit["defects"],
        "economics_findings": audit["economics_findings"],
    }
