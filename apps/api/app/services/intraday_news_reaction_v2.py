"""Power-pruned v2 governance for the 5m news-reaction study.

V1 preflight was return-blind.  A second predictor-side audit then counted the
four reaction states using only information knowable at the 5-minute decision
(timestamp, news text, and the completed 5m stock/SPY reaction).  No +5/+10/
+15/+30m target return was read.

That audit showed adequate development supply for both positive-news states and
insufficient validation supply for both negative-news states.  V2 therefore
freezes only the two positive-news states x four horizons = eight fresh tests.
The negative states are excluded for a pre-outcome power reason, not because of
observed performance.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import load_cost_model
from app.services.intraday_news import NEGATIVE_TERMS, POSITIVE_TERMS
from app.services import intraday_news_reaction as measurement
from app.services import intraday_news_reaction_governed as governed
from app.services.research_splits import get_dataset_splits, record_split_access

NEWS_REACTION_VERSION = "intraday_news_reaction_5m_v2_positive_only_power_pruned"
FRESH_TESTS = 8
ACTIVE_STATE_DIRECTIONS: dict[str, int] = {
    measurement.STATE_POSITIVE_CONTINUATION: 1,
    measurement.STATE_POSITIVE_FAILURE: -1,
}

# Frozen before any forward outcome was inspected.  These are predictor-side
# event-supply counts from the development window only.
POWER_PRUNING_EVIDENCE = {
    "basis": "predictor_side_state_supply_only_no_forward_outcomes",
    "discovery": {
        "negative_continuation": {"events": 580, "sessions": 195},
        "negative_failure": {"events": 547, "sessions": 196},
        "positive_continuation": {"events": 3450, "sessions": 251},
        "positive_failure": {"events": 3418, "sessions": 251},
    },
    "validation": {
        "negative_continuation": {"events": 212, "sessions": 99},
        "negative_failure": {"events": 220, "sessions": 101},
        "positive_continuation": {"events": 2082, "sessions": 150},
        "positive_failure": {"events": 2039, "sessions": 150},
    },
    "decision": {
        "retained": [
            measurement.STATE_POSITIVE_CONTINUATION,
            measurement.STATE_POSITIVE_FAILURE,
        ],
        "excluded_underpowered": [
            measurement.STATE_NEGATIVE_CONTINUATION,
            measurement.STATE_NEGATIVE_FAILURE,
        ],
        "negative_states_may_be_revisited_only_with_new_data_or_a_new_information_set": True,
    },
}


def preflight_news_reaction(conn: psycopg.Connection, *, dataset_id: int) -> dict[str, Any]:
    """Return the v2 return-blind supply report.

    The underlying preflight still selects no forward OHLC.  V2 only changes
    which already-predeclared states are eligible to consume outcome trials.
    """
    result = governed.preflight_news_reaction(conn, dataset_id=dataset_id)
    return {
        **result,
        "protocol_version": NEWS_REACTION_VERSION,
        "fresh_tests": FRESH_TESTS,
        "active_states": list(ACTIVE_STATE_DIRECTIONS),
        "power_pruning": POWER_PRUNING_EVIDENCE,
    }


def declare_news_reaction(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    cost_calibration_id: int,
    prior_effective_trials: int,
    purpose: str,
) -> dict[str, Any]:
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")
    if prior_effective_trials < 0:
        raise ValueError("prior_effective_trials cannot be negative")

    preflight = preflight_news_reaction(conn, dataset_id=dataset_id)
    if preflight["phases"]["discovery"]["events"] < 200:
        raise ValueError("Insufficient discovery event supply")
    if preflight["phases"]["validation"]["events"] < 200:
        raise ValueError("Insufficient validation event supply")

    cost_model = load_cost_model(conn, cost_calibration_id)
    total_trials = prior_effective_trials + FRESH_TESTS
    specification = {
        "purpose": purpose,
        "dataset_id": dataset_id,
        "cost_calibration_id": cost_calibration_id,
        "cost_model_hash": measurement._stable_hash(cost_model),
        "reaction_minutes": measurement.REACTION_MINUTES,
        "horizons_minutes": list(measurement.HORIZONS_MINUTES),
        "quiet_period_minutes": measurement.QUIET_PERIOD_MINUTES,
        "excluded_news_targets": list(measurement.EXCLUDED_NEWS_TARGETS),
        "states": ACTIVE_STATE_DIRECTIONS,
        "polarity_model": {
            "positive_terms": list(POSITIVE_TERMS),
            "negative_terms": list(NEGATIVE_TERMS),
            "ties": "excluded_as_neutral",
            "negative_polarity": "classified_but_not_tested_in_v2_due_pre_outcome_power_gate",
        },
        "reaction_measure": "stock_5m_return_minus_spy_5m_return",
        "entry": "open_of_first_1m_bar_after_completed_5m_reaction",
        "target_gross_block_bootstrap_lower_bound_bps": measurement.TARGET_GROSS_LOWER_BOUND_BPS,
        "prior_effective_trials": prior_effective_trials,
        "fresh_tests": FRESH_TESTS,
        "total_effective_trials": total_trials,
        "bonferroni_two_sided_t_threshold": measurement.selection_t_threshold(total_trials),
        "splits": splits.as_dict(),
        "confirmation_untouched": True,
        "price_source": "frozen_research_dataset_candles_1m",
        "trade_flow_used": False,
        "news_categories_are_labels_not_tests": True,
        "preflight_return_blind": True,
        "preflight_outcome_fields_accessed": [],
        "power_pruning": POWER_PRUNING_EVIDENCE,
    }
    specification_hash = measurement._stable_hash(specification)
    fingerprint = preflight["news_fingerprint"]
    row = conn.execute(
        """
        INSERT INTO intraday_news_reaction_declarations(
            dataset_id, cost_calibration_id, specification, specification_hash,
            news_fingerprint, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (specification_hash) DO NOTHING
        RETURNING *
        """,
        (
            dataset_id,
            cost_calibration_id,
            Jsonb(measurement._jsonable(specification)),
            specification_hash,
            Jsonb(measurement._jsonable(fingerprint)),
            NEWS_REACTION_VERSION,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM intraday_news_reaction_declarations WHERE specification_hash = %s",
            (specification_hash,),
        ).fetchone()
    conn.commit()
    return {
        "declaration_id": int(row["id"]),
        "protocol_version": NEWS_REACTION_VERSION,
        "specification_hash": specification_hash,
        "total_effective_trials": total_trials,
        "fresh_tests": FRESH_TESTS,
        "selection_t_threshold": specification["bonferroni_two_sided_t_threshold"],
        "preflight": preflight,
        "next_command": f"discover --declaration-id {int(row['id'])}",
    }


def _require_v2_declaration(declaration: dict[str, Any]) -> None:
    if str(declaration.get("protocol_version")) != NEWS_REACTION_VERSION:
        raise ValueError(
            "This CLI now runs the power-pruned v2 protocol. Create/use a v2 declaration; "
            "do not run a v1 four-state declaration after the pre-outcome pruning decision."
        )
    states = dict((declaration.get("specification") or {}).get("states") or {})
    if set(states) != set(ACTIVE_STATE_DIRECTIONS):
        raise ValueError("Declaration state set does not match the frozen v2 positive-only design")


def run_news_reaction_discovery(conn: psycopg.Connection, *, declaration_id: int) -> dict[str, Any]:
    declaration_row = conn.execute(
        "SELECT * FROM intraday_news_reaction_declarations WHERE id = %s",
        (declaration_id,),
    ).fetchone()
    if not declaration_row:
        raise ValueError(f"Unknown news-reaction declaration {declaration_id}")
    declaration = dict(declaration_row)
    _require_v2_declaration(declaration)
    if conn.execute(
        "SELECT 1 FROM intraday_news_reaction_runs WHERE declaration_id = %s",
        (declaration_id,),
    ).fetchone():
        raise ValueError("This declaration already has a discovery run; do not re-run spent evidence")

    measurement._assert_news_fingerprint(conn, declaration)
    spec = dict(declaration["specification"])
    total_trials = int(spec["total_effective_trials"])
    t_threshold = float(spec["bonferroni_two_sided_t_threshold"])

    # The legacy measurement loader may classify negative-news events, but v2
    # drops them before any cell is formed, reported, selected, or confirmed.
    all_events = measurement._prepare_events(
        conn,
        declaration,
        allowed_phases={"discovery", "validation"},
    )
    events = [row for row in all_events if row["state"] in ACTIVE_STATE_DIRECTIONS]
    discovery_events = [row for row in events if row["phase"] == "discovery"]
    validation_events = [row for row in events if row["phase"] == "validation"]

    cells: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for state in ACTIVE_STATE_DIRECTIONS:
        for horizon in measurement.HORIZONS_MINUTES:
            discovery = measurement._phase_cell_report(
                discovery_events,
                state=state,
                horizon=horizon,
                total_trials=total_trials,
            )
            validation = measurement._phase_cell_report(
                validation_events,
                state=state,
                horizon=horizon,
                total_trials=total_trials,
            )
            passed = measurement.cell_passes_promotion(
                discovery,
                validation,
                t_threshold=t_threshold,
            )
            cell = {
                "state": state,
                "horizon_minutes": horizon,
                "discovery": discovery,
                "validation": validation,
                "promotion_passed": passed,
            }
            cells.append(cell)
            if passed:
                candidates.append({"state": state, "horizon_minutes": horizon})

    results = {
        "protocol_version": NEWS_REACTION_VERSION,
        "declaration_id": declaration_id,
        "dataset_id": int(declaration["dataset_id"]),
        "effective_trials": total_trials,
        "fresh_tests": FRESH_TESTS,
        "selection_t_threshold": t_threshold,
        "target_gross_lower_bound_bps": measurement.TARGET_GROSS_LOWER_BOUND_BPS,
        "active_states": list(ACTIVE_STATE_DIRECTIONS),
        "development_events_in_active_states": len(events),
        "discovery_events_in_active_states": len(discovery_events),
        "validation_events_in_active_states": len(validation_events),
        "cells": cells,
        "candidate_cells": candidates,
        "power_pruning": POWER_PRUNING_EVIDENCE,
        "strategy_construction_authorized": False,
        "confirmation_accessed": False,
    }

    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="discovery",
        decision_type="news_reaction_5m_v2_positive_only_discovery",
        detail={"declaration_id": declaration_id, "fresh_tests": FRESH_TESTS},
    )
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="validation",
        decision_type="news_reaction_5m_v2_positive_only_validation",
        detail={"declaration_id": declaration_id, "fresh_tests": FRESH_TESTS},
    )
    run = conn.execute(
        """
        INSERT INTO intraday_news_reaction_runs(
            declaration_id, dataset_id, results, effective_trials,
            candidate_cells, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            declaration_id,
            int(declaration["dataset_id"]),
            Jsonb(measurement._jsonable(results)),
            total_trials,
            Jsonb(candidates),
            NEWS_REACTION_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {
        "run_id": int(run["id"]),
        **results,
        "next_command": f"confirm --run-id {int(run['id'])}" if candidates else None,
    }


def confirm_news_reaction(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    run_row = conn.execute(
        "SELECT * FROM intraday_news_reaction_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not run_row:
        raise ValueError(f"Unknown news-reaction run {run_id}")
    if str(run_row["protocol_version"]) != NEWS_REACTION_VERSION:
        raise ValueError("Only a v2 positive-only discovery run may use the v2 confirmation path")
    if conn.execute(
        "SELECT 1 FROM intraday_news_reaction_confirmation_runs WHERE discovery_run_id = %s",
        (run_id,),
    ).fetchone():
        raise ValueError("Confirmation already spent for this run")

    candidates = list(run_row["candidate_cells"] or [])
    if not candidates:
        raise ValueError("Discovery produced no candidate cells; confirmation is not justified")
    if any(str(row.get("state")) not in ACTIVE_STATE_DIRECTIONS for row in candidates):
        raise ValueError("Run contains a candidate outside the frozen v2 positive-only state set")

    declaration_row = conn.execute(
        "SELECT * FROM intraday_news_reaction_declarations WHERE id = %s",
        (int(run_row["declaration_id"]),),
    ).fetchone()
    declaration = dict(declaration_row)
    _require_v2_declaration(declaration)
    measurement._assert_news_fingerprint(conn, declaration)
    spec = dict(declaration["specification"])
    total_trials = int(spec["total_effective_trials"])
    t_threshold = float(spec["bonferroni_two_sided_t_threshold"])

    all_events = measurement._prepare_events(conn, declaration, allowed_phases={"confirmation"})
    events = [row for row in all_events if row["state"] in ACTIVE_STATE_DIRECTIONS]
    reports: list[dict[str, Any]] = []
    all_passed = True
    for candidate in candidates:
        report = measurement._phase_cell_report(
            events,
            state=str(candidate["state"]),
            horizon=int(candidate["horizon_minutes"]),
            total_trials=total_trials,
        )
        gross_ci = report["gross"]["block_bootstrap"]["confidence_interval_95"]
        net_ci = report["net"]["block_bootstrap"]["confidence_interval_95"]
        t_value = report["net"]["day_clustered_t_statistic"]
        passed = bool(
            gross_ci
            and float(gross_ci[0]) >= measurement.TARGET_GROSS_LOWER_BOUND_BPS
            and net_ci
            and float(net_ci[0]) > 0
            and t_value is not None
            and float(t_value) >= t_threshold
            and report["gross"]["independent_evidence_ready"]
        )
        reports.append({**report, "confirmation_passed": passed})
        all_passed = all_passed and passed

    results = {
        "protocol_version": NEWS_REACTION_VERSION,
        "discovery_run_id": run_id,
        "confirmation_events_in_active_states": len(events),
        "candidate_reports": reports,
        "passed": all_passed,
        "strategy_construction_authorized": all_passed,
    }
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="confirmation",
        decision_type="news_reaction_5m_v2_positive_only_confirmation",
        detail={"run_id": run_id, "candidate_cells": candidates},
    )
    row = conn.execute(
        """
        INSERT INTO intraday_news_reaction_confirmation_runs(
            discovery_run_id, declaration_id, results, passed, protocol_version
        ) VALUES (%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            run_id,
            int(run_row["declaration_id"]),
            Jsonb(measurement._jsonable(results)),
            all_passed,
            NEWS_REACTION_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {"confirmation_run_id": int(row["id"]), **results}


def news_reaction_report(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    return measurement.news_reaction_report(conn, run_id=run_id)
