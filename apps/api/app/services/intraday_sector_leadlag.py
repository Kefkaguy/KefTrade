"""Governed 5-minute sector peer lead/lag research.

The frozen predictor is the leave-one-out equal-weight sector-peer 5m return
minus contemporaneous SPY 5m return, normalized by expanding same-slot prior
sessions. Forward +5/+10/+15m prices are read only after declaration.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import load_cost_model
from app.services.intraday_research_integrity import clustered_outcome_statistics
from app.services.intraday_sector_leadlag_outcomes import (
    _load_confirmation_cell_events,
    _load_outcome_events,
)
from app.services.intraday_sector_leadlag_predictor import (
    _assert_fingerprint,
    _build_predictor_states,
    _dataset_fingerprint,
    _predictor_supply,
)
from app.services.intraday_sector_leadlag_spec import (
    FRESH_TESTS,
    HORIZONS_MINUTES,
    MIN_HISTORY_SESSIONS,
    MIN_SECTOR_MEMBERS,
    SECTOR_LEADLAG_VERSION,
    STATE_DIRECTIONS,
    STATE_NEGATIVE_PEER_IMPULSE,
    STATE_POSITIVE_PEER_IMPULSE,
    TARGET_GROSS_LOWER_BOUND_BPS,
    Z_THRESHOLD,
    _jsonable,
    _stable_hash,
    classify_peer_impulse,
    selection_t_threshold,
)
from app.services.research_splits import get_dataset_splits, record_split_access


def preflight_sector_leadlag(conn: psycopg.Connection, *, dataset_id: int) -> dict[str, Any]:
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")
    fingerprint = _dataset_fingerprint(conn, dataset_id)
    _build_predictor_states(conn, dataset_id=dataset_id, include_confirmation=False)
    supply = _predictor_supply(conn)
    return {
        "protocol_version": SECTOR_LEADLAG_VERSION,
        "dataset_id": dataset_id,
        "forward_outcome_blind": True,
        "predictor_fields_accessed": [
            "completed_target_5m_return_for_leave_one_out_exclusion",
            "completed_peer_5m_returns",
            "completed_spy_5m_return",
            "historical_peer_excess_5m",
            "timestamps_for_max_horizon_grid",
        ],
        "forward_outcome_fields_accessed": [],
        "predictor_fingerprint": fingerprint,
        "states": list(STATE_DIRECTIONS),
        "horizons_minutes": list(HORIZONS_MINUTES),
        "fresh_tests": FRESH_TESTS,
        "z_threshold": Z_THRESHOLD,
        "minimum_history_sessions": MIN_HISTORY_SESSIONS,
        "minimum_sector_members": MIN_SECTOR_MEMBERS,
        "supply": supply,
        "confirmation_predictor_states_accessed": False,
    }


def declare_sector_leadlag(
    conn: psycopg.Connection,
    *,
    dataset_id: int,
    cost_calibration_id: int,
    prior_effective_trials: int,
    purpose: str,
) -> dict[str, Any]:
    if prior_effective_trials < 0:
        raise ValueError("prior_effective_trials cannot be negative")
    splits = get_dataset_splits(conn, dataset_id)
    if splits is None:
        raise ValueError(f"Dataset {dataset_id} has no frozen nested research split")

    preflight = preflight_sector_leadlag(conn, dataset_id=dataset_id)
    for phase in ("discovery", "validation"):
        for state in STATE_DIRECTIONS:
            cell = (preflight["supply"].get(phase) or {}).get(state)
            if not cell or int(cell["events"]) < 200:
                raise ValueError(f"Insufficient {phase} supply for {state}")
            if int(cell["symbols"]) < 20 or int(cell["sectors"]) < 3:
                raise ValueError(f"Insufficient diversification for {phase}/{state}")

    cost_model = load_cost_model(conn, cost_calibration_id)
    total_trials = prior_effective_trials + FRESH_TESTS
    specification = {
        "purpose": purpose,
        "dataset_id": dataset_id,
        "cost_calibration_id": cost_calibration_id,
        "cost_model_hash": _stable_hash(cost_model),
        "predictor": "leave_one_out_equal_weight_sector_peer_5m_return_minus_spy_5m_return",
        "normalization": "expanding_same_symbol_same_clock_slot_prior_sessions_only",
        "minimum_history_sessions": MIN_HISTORY_SESSIONS,
        "z_threshold": Z_THRESHOLD,
        "states": STATE_DIRECTIONS,
        "minimum_sector_members": MIN_SECTOR_MEMBERS,
        "eligible_target_requires_leave_one_out_peers": MIN_SECTOR_MEMBERS - 1,
        "sector_membership_source": "symbols.sector_current_mapping_frozen_by_declaration_fingerprint",
        "horizons_minutes": list(HORIZONS_MINUTES),
        "entry": "target_and_spy_open_at_decision_timestamp",
        "exit": "target_and_spy_close_at_final_1m_bar_of_horizon",
        "gross_return": "direction_times_target_forward_return_minus_spy_forward_return_equal_notional",
        "cost": "stressed_round_trip_target_plus_stressed_round_trip_spy",
        "target_gross_block_bootstrap_lower_bound_bps": TARGET_GROSS_LOWER_BOUND_BPS,
        "prior_effective_trials": prior_effective_trials,
        "fresh_tests": FRESH_TESTS,
        "total_effective_trials": total_trials,
        "bonferroni_two_sided_t_threshold": selection_t_threshold(total_trials),
        "splits": splits.as_dict(),
        "preflight": preflight,
        "confirmation_untouched": True,
        "strategy_construction_authorized": False,
    }
    specification_hash = _stable_hash(specification)
    row = conn.execute(
        """
        INSERT INTO intraday_sector_leadlag_declarations(
            dataset_id, cost_calibration_id, specification, specification_hash,
            predictor_fingerprint, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (specification_hash) DO NOTHING
        RETURNING *
        """,
        (
            dataset_id,
            cost_calibration_id,
            Jsonb(_jsonable(specification)),
            specification_hash,
            Jsonb(_jsonable(preflight["predictor_fingerprint"])),
            SECTOR_LEADLAG_VERSION,
        ),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM intraday_sector_leadlag_declarations WHERE specification_hash = %s",
            (specification_hash,),
        ).fetchone()
    conn.commit()
    return {
        "declaration_id": int(row["id"]),
        "protocol_version": SECTOR_LEADLAG_VERSION,
        "specification_hash": specification_hash,
        "total_effective_trials": total_trials,
        "fresh_tests": FRESH_TESTS,
        "selection_t_threshold": specification["bonferroni_two_sided_t_threshold"],
        "preflight": preflight,
        "next_command": f"discover --declaration-id {int(row['id'])}",
    }


def _phase_cell_report(
    events: Sequence[dict[str, Any]],
    *,
    state: str,
    horizon: int,
    total_trials: int,
) -> dict[str, Any]:
    selected = [row for row in events if row["state"] == state]
    gross_rows = [
        {
            "value": row["outcomes"][horizon]["gross_return"],
            "session_date": row["session_date"],
            "symbol": row["symbol"],
            "timestamp": row["decision_at"],
        }
        for row in selected
    ]
    net_rows = [
        {
            "value": row["outcomes"][horizon]["net_return"],
            "session_date": row["session_date"],
            "symbol": row["symbol"],
            "timestamp": row["decision_at"],
        }
        for row in selected
    ]
    gross = clustered_outcome_statistics(
        gross_rows,
        effective_trials=total_trials,
        require_symbol_diversification=True,
    )
    net = clustered_outcome_statistics(
        net_rows,
        effective_trials=total_trials,
        require_symbol_diversification=True,
    )
    sector_counts = Counter(str(row["sector"]) for row in selected)
    return {
        "state": state,
        "direction": (
            "long_target_short_spy"
            if STATE_DIRECTIONS[state] > 0
            else "short_target_long_spy"
        ),
        "horizon_minutes": horizon,
        "events": len(selected),
        "gross": gross,
        "net": net,
        "mean_peer_excess_bps": (
            sum(float(row["peer_excess_bps"]) for row in selected) / len(selected)
            if selected
            else None
        ),
        "mean_peer_impulse_z": (
            sum(float(row["peer_impulse_z"]) for row in selected) / len(selected)
            if selected
            else None
        ),
        "mean_total_cost_bps": (
            sum(float(row["total_cost_bps"]) for row in selected) / len(selected)
            if selected
            else None
        ),
        "sector_counts": dict(sector_counts),
    }


def cell_passes_promotion(
    discovery: dict[str, Any],
    validation: dict[str, Any],
    *,
    t_threshold: float,
) -> bool:
    def lower(report: dict[str, Any], side: str) -> float | None:
        ci = (report.get(side) or {}).get("block_bootstrap", {}).get("confidence_interval_95")
        return float(ci[0]) if ci else None

    d_gross = lower(discovery, "gross")
    d_net = lower(discovery, "net")
    v_gross = lower(validation, "gross")
    v_net = lower(validation, "net")
    v_t = (validation.get("net") or {}).get("day_clustered_t_statistic")
    return bool(
        d_gross is not None
        and d_gross >= TARGET_GROSS_LOWER_BOUND_BPS
        and d_net is not None
        and d_net > 0
        and v_gross is not None
        and v_gross >= TARGET_GROSS_LOWER_BOUND_BPS
        and v_net is not None
        and v_net > 0
        and v_t is not None
        and float(v_t) >= t_threshold
        and bool((discovery.get("gross") or {}).get("independent_evidence_ready"))
        and bool((validation.get("gross") or {}).get("independent_evidence_ready"))
    )


def run_sector_leadlag_discovery(
    conn: psycopg.Connection,
    *,
    declaration_id: int,
) -> dict[str, Any]:
    declaration_row = conn.execute(
        "SELECT * FROM intraday_sector_leadlag_declarations WHERE id = %s",
        (declaration_id,),
    ).fetchone()
    if not declaration_row:
        raise ValueError(f"Unknown sector-leadlag declaration {declaration_id}")
    declaration = dict(declaration_row)
    if str(declaration["protocol_version"]) != SECTOR_LEADLAG_VERSION:
        raise ValueError("Declaration protocol does not match current sector lead/lag version")
    if conn.execute(
        "SELECT 1 FROM intraday_sector_leadlag_runs WHERE declaration_id = %s",
        (declaration_id,),
    ).fetchone():
        raise ValueError("This declaration already has a discovery run; do not re-run spent evidence")

    _assert_fingerprint(conn, declaration)
    spec = dict(declaration["specification"])
    total_trials = int(spec["total_effective_trials"])
    t_threshold = float(spec["bonferroni_two_sided_t_threshold"])
    events = _load_outcome_events(
        conn,
        declaration,
        allowed_phases={"discovery", "validation"},
    )
    discovery_events = [row for row in events if row["phase"] == "discovery"]
    validation_events = [row for row in events if row["phase"] == "validation"]

    cells: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for state in STATE_DIRECTIONS:
        for horizon in HORIZONS_MINUTES:
            discovery = _phase_cell_report(
                discovery_events,
                state=state,
                horizon=horizon,
                total_trials=total_trials,
            )
            validation = _phase_cell_report(
                validation_events,
                state=state,
                horizon=horizon,
                total_trials=total_trials,
            )
            passed = cell_passes_promotion(
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
        "protocol_version": SECTOR_LEADLAG_VERSION,
        "declaration_id": declaration_id,
        "dataset_id": int(declaration["dataset_id"]),
        "effective_trials": total_trials,
        "fresh_tests": FRESH_TESTS,
        "selection_t_threshold": t_threshold,
        "target_gross_lower_bound_bps": TARGET_GROSS_LOWER_BOUND_BPS,
        "development_events": len(events),
        "discovery_events": len(discovery_events),
        "validation_events": len(validation_events),
        "cells": cells,
        "candidate_cells": candidates,
        "strategy_construction_authorized": False,
        "confirmation_accessed": False,
    }
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="discovery",
        decision_type="sector_leadlag_5m_discovery",
        detail={"declaration_id": declaration_id, "fresh_tests": FRESH_TESTS},
    )
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="validation",
        decision_type="sector_leadlag_5m_validation",
        detail={"declaration_id": declaration_id, "fresh_tests": FRESH_TESTS},
    )
    run = conn.execute(
        """
        INSERT INTO intraday_sector_leadlag_runs(
            declaration_id, dataset_id, results, effective_trials,
            candidate_cells, protocol_version
        ) VALUES (%s,%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            declaration_id,
            int(declaration["dataset_id"]),
            Jsonb(_jsonable(results)),
            total_trials,
            Jsonb(candidates),
            SECTOR_LEADLAG_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {
        "run_id": int(run["id"]),
        **results,
        "next_command": f"confirm --run-id {int(run['id'])}" if candidates else None,
    }


def confirm_sector_leadlag(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    run_row = conn.execute(
        "SELECT * FROM intraday_sector_leadlag_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not run_row:
        raise ValueError(f"Unknown sector-leadlag run {run_id}")
    if str(run_row["protocol_version"]) != SECTOR_LEADLAG_VERSION:
        raise ValueError("Only the current sector lead/lag protocol may use this confirmation path")
    if conn.execute(
        "SELECT 1 FROM intraday_sector_leadlag_confirmation_runs WHERE discovery_run_id = %s",
        (run_id,),
    ).fetchone():
        raise ValueError("Confirmation already spent for this run")

    candidates = list(run_row["candidate_cells"] or [])
    if not candidates:
        raise ValueError("Discovery produced no candidate cells; confirmation is not justified")
    if any(str(row.get("state")) not in STATE_DIRECTIONS for row in candidates):
        raise ValueError("Run contains a candidate outside the frozen state set")

    declaration_row = conn.execute(
        "SELECT * FROM intraday_sector_leadlag_declarations WHERE id = %s",
        (int(run_row["declaration_id"]),),
    ).fetchone()
    declaration = dict(declaration_row)
    _assert_fingerprint(conn, declaration)
    spec = dict(declaration["specification"])
    total_trials = int(spec["total_effective_trials"])
    t_threshold = float(spec["bonferroni_two_sided_t_threshold"])
    _build_predictor_states(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        include_confirmation=True,
    )

    reports: list[dict[str, Any]] = []
    all_passed = True
    confirmation_events_read = 0
    for candidate in candidates:
        candidate_state = str(candidate["state"])
        candidate_horizon = int(candidate["horizon_minutes"])
        events = _load_confirmation_cell_events(
            conn,
            declaration,
            state=candidate_state,
            horizon=candidate_horizon,
        )
        confirmation_events_read += len(events)
        report = _phase_cell_report(
            events,
            state=candidate_state,
            horizon=candidate_horizon,
            total_trials=total_trials,
        )
        gross_ci = report["gross"]["block_bootstrap"]["confidence_interval_95"]
        net_ci = report["net"]["block_bootstrap"]["confidence_interval_95"]
        t_value = report["net"]["day_clustered_t_statistic"]
        passed = bool(
            gross_ci
            and float(gross_ci[0]) >= TARGET_GROSS_LOWER_BOUND_BPS
            and net_ci
            and float(net_ci[0]) > 0
            and t_value is not None
            and float(t_value) >= t_threshold
            and report["gross"]["independent_evidence_ready"]
        )
        reports.append({**report, "confirmation_passed": passed})
        all_passed = all_passed and passed

    results = {
        "protocol_version": SECTOR_LEADLAG_VERSION,
        "discovery_run_id": run_id,
        "confirmation_events_read_across_candidate_cells": confirmation_events_read,
        "candidate_reports": reports,
        "passed": all_passed,
        "strategy_construction_authorized": all_passed,
    }
    record_split_access(
        conn,
        dataset_id=int(declaration["dataset_id"]),
        phase="confirmation",
        decision_type="sector_leadlag_5m_confirmation",
        detail={"run_id": run_id, "candidate_cells": candidates},
    )
    row = conn.execute(
        """
        INSERT INTO intraday_sector_leadlag_confirmation_runs(
            discovery_run_id, declaration_id, results, passed, protocol_version
        ) VALUES (%s,%s,%s,%s,%s)
        RETURNING id, created_at
        """,
        (
            run_id,
            int(run_row["declaration_id"]),
            Jsonb(_jsonable(results)),
            all_passed,
            SECTOR_LEADLAG_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return {"confirmation_run_id": int(row["id"]), **results}


def sector_leadlag_report(conn: psycopg.Connection, *, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*, d.specification, d.specification_hash, d.predictor_fingerprint
        FROM intraday_sector_leadlag_runs r
        JOIN intraday_sector_leadlag_declarations d ON d.id = r.declaration_id
        WHERE r.id = %s
        """,
        (run_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown sector-leadlag run {run_id}")
    confirmation = conn.execute(
        """
        SELECT id, results, passed, created_at
        FROM intraday_sector_leadlag_confirmation_runs
        WHERE discovery_run_id = %s
        """,
        (run_id,),
    ).fetchone()
    return {
        "run_id": run_id,
        "declaration_id": int(row["declaration_id"]),
        "dataset_id": int(row["dataset_id"]),
        "effective_trials": int(row["effective_trials"]),
        "specification_hash": str(row["specification_hash"]),
        "specification": row["specification"],
        "predictor_fingerprint": row["predictor_fingerprint"],
        "results": row["results"],
        "confirmation": dict(confirmation) if confirmation else None,
    }
