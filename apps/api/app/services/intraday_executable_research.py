"""Backend-only 30m factor-to-elite research funnel.

The module deliberately has no route or UI dependency.  It converts only
pre-registered factor survivors into frozen Strategy Engine V2 candidates,
charges them observed stressed execution costs, requires a non-overlapping
factor and trading confirmation, and only then submits the exact frozen
candidate to the existing elite campaign gates.
"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from json import dumps
from typing import Any, Callable, Sequence

import psycopg
from psycopg.types.json import Jsonb

from app.services.intraday_factor_diagnostics import (
    evaluate_forward_confirmation,
    load_auction_imbalances,
    load_dataset_candles,
    load_microstructure,
    persist_factor_run,
)
from app.services.intraday_research_data import research_data_readiness
from app.services.intraday_research_integrity import (
    cost_model_readiness,
    exchange_session_date,
    rows_after_session,
)
from app.services.labs.intraday.campaign import _create_intraday_campaign
from app.services.labs.intraday.families.v2 import families as _v2_families  # noqa: F401
from app.services.labs.intraday.families.v2.base import (
    BASE_V2_PARAMETERS,
    V2_BLOCKS,
)
from app.services.research_campaigns import (
    candidate_consistency_summaries,
    candidate_from_payload,
    cross_validation_failures,
    jsonable,
    passes_cross_validation,
    passes_single_market_validation,
    run_campaign_job,
    run_research_campaign_batch,
)
from app.services.strategy_discovery import (
    DiscoveryCandidate,
    canonical_candidate_key,
)

EXECUTABLE_RESEARCH_VERSION = "intraday_executable_research_v2_directional_variants"
TIMEFRAME = "30m"
ProgressCallback = Callable[[dict[str, Any]], None]

FACTOR_ARCHITECTURES = {
    "first_to_last_half_hour_market_momentum":
        "first_to_last_half_hour_momentum_v1",
    "first_to_last_half_hour_market_reversal":
        "first_to_last_half_hour_momentum_v1",
    "overnight_gap_acceptance_absorption":
        "overnight_gap_acceptance_absorption_v1",
    "gap_up_acceptance_continuation":
        "overnight_gap_acceptance_absorption_v1",
    "gap_down_acceptance_continuation":
        "overnight_gap_acceptance_absorption_v1",
    "gap_up_absorption_reversal":
        "overnight_gap_acceptance_absorption_v1",
    "gap_down_absorption_reversal":
        "overnight_gap_acceptance_absorption_v1",
    "cross_sectional_same_slot_continuation":
        "same_slot_institutional_flow_v1",
    "cross_sectional_same_slot_reversal":
        "same_slot_institutional_flow_v1",
    "vwap_execution_pressure": "vwap_execution_pressure_v1",
    "vwap_execution_pressure_fade": "vwap_execution_pressure_v1",
    "liquidity_shock_reversal": "liquidity_shock_reversal_v1",
}

# These recipes are research protocol, not a tunable grid.  A factor earns
# exactly the pre-declared translation below; no threshold search occurs
# after its discovery result is known.
FACTOR_RECIPES: dict[str, tuple[dict[str, Any], ...]] = {
    "first_to_last_half_hour_market_momentum": (
        {
            "minimum_opening_return": 0.002,
            "signal_polarity": "continuation",
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "first_to_last_half_hour_market_reversal": (
        {
            "minimum_opening_return": 0.002,
            "signal_polarity": "reversal",
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "overnight_gap_acceptance_absorption": (
        {
            "flow_mode": "acceptance",
            "gap_side": "both",
            "minimum_gap_fraction": 0.003,
            "minimum_relative_volume": 1.5,
            "maximum_acceptance_fill_fraction": 0.25,
            "minimum_absorption_fill_fraction": 0.5,
            "direction": "both",
            "max_holding_bars": 1,
        },
        {
            "flow_mode": "absorption",
            "gap_side": "both",
            "minimum_gap_fraction": 0.003,
            "minimum_relative_volume": 1.5,
            "maximum_acceptance_fill_fraction": 0.25,
            "minimum_absorption_fill_fraction": 0.5,
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "gap_up_acceptance_continuation": (
        {
            "flow_mode": "acceptance",
            "gap_side": "up",
            "minimum_gap_fraction": 0.003,
            "minimum_relative_volume": 1.5,
            "maximum_acceptance_fill_fraction": 0.25,
            "minimum_absorption_fill_fraction": 0.5,
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "gap_down_acceptance_continuation": (
        {
            "flow_mode": "acceptance",
            "gap_side": "down",
            "minimum_gap_fraction": 0.003,
            "minimum_relative_volume": 1.5,
            "maximum_acceptance_fill_fraction": 0.25,
            "minimum_absorption_fill_fraction": 0.5,
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "gap_up_absorption_reversal": (
        {
            "flow_mode": "absorption",
            "gap_side": "up",
            "minimum_gap_fraction": 0.003,
            "minimum_relative_volume": 1.5,
            "maximum_acceptance_fill_fraction": 0.25,
            "minimum_absorption_fill_fraction": 0.5,
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "gap_down_absorption_reversal": (
        {
            "flow_mode": "absorption",
            "gap_side": "down",
            "minimum_gap_fraction": 0.003,
            "minimum_relative_volume": 1.5,
            "maximum_acceptance_fill_fraction": 0.25,
            "minimum_absorption_fill_fraction": 0.5,
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "cross_sectional_same_slot_continuation": (
        {
            "cross_sectional_lookback_bars": 20,
            "upper_percentile": 0.8,
            "lower_percentile": 0.2,
            "signal_polarity": "continuation",
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "cross_sectional_same_slot_reversal": (
        {
            "cross_sectional_lookback_bars": 20,
            "upper_percentile": 0.8,
            "lower_percentile": 0.2,
            "signal_polarity": "reversal",
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "vwap_execution_pressure": (
        {
            "minimum_vwap_displacement": 0.001,
            "minimum_relative_volume": 1.5,
            "signal_polarity": "continuation",
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "vwap_execution_pressure_fade": (
        {
            "minimum_vwap_displacement": 0.001,
            "minimum_relative_volume": 1.5,
            "signal_polarity": "reversal",
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
    "liquidity_shock_reversal": (
        {
            "minimum_relative_volume": 2.0,
            "minimum_range_expansion": 2.0,
            "direction": "both",
            "max_holding_bars": 1,
        },
    ),
}


def freeze_factor_survivors(
    conn: psycopg.Connection,
    *,
    source_factor_run_id: int,
) -> list[dict[str, Any]]:
    """Persist deterministic candidates for selected discovery factors."""
    source = _source_discovery(conn, source_factor_run_id)
    _validate_source(source)
    selected = list(
        dict(source["results"] or {}).get("selected_for_forward_confirmation")
        or []
    )
    tradable = [key for key in selected if key in FACTOR_ARCHITECTURES]
    cost_model = dict(source["cost_model"] or {})
    calibration_id = _require_sip_calibration(cost_model)
    output: list[dict[str, Any]] = []
    for factor_key in tradable:
        architecture = FACTOR_ARCHITECTURES[factor_key]
        for recipe in FACTOR_RECIPES[factor_key]:
            candidate = _candidate(
                source_factor_run_id=source_factor_run_id,
                factor_key=factor_key,
                architecture=architecture,
                recipe=recipe,
                cost_model=cost_model,
            )
            payload = jsonable(asdict(candidate))
            spec_hash = _hash(payload)
            row = conn.execute(
                """
                INSERT INTO intraday_executable_candidates(
                    source_factor_run_id, factor_key, architecture, timeframe,
                    discovery_dataset_id, cost_calibration_id, candidate_id,
                    candidate, frozen_spec_hash, protocol_version
                )
                VALUES (%s, %s, %s, '30m', %s, %s, %s, %s, %s, %s)
                ON CONFLICT(source_factor_run_id, candidate_id) DO NOTHING
                RETURNING *
                """,
                (
                    source_factor_run_id,
                    factor_key,
                    architecture,
                    int(source["dataset_id"]),
                    calibration_id,
                    candidate.candidate_id,
                    Jsonb(payload),
                    spec_hash,
                    EXECUTABLE_RESEARCH_VERSION,
                ),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT *
                    FROM intraday_executable_candidates
                    WHERE source_factor_run_id = %s AND candidate_id = %s
                    """,
                    (source_factor_run_id, candidate.candidate_id),
                ).fetchone()
            output.append(jsonable(dict(row)))
    conn.commit()
    return output


def run_development_simulations(
    conn: psycopg.Connection,
    *,
    source_factor_run_id: int,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run complete cost-aware simulations without creating an elite."""
    source = _source_discovery(conn, source_factor_run_id)
    candidates = freeze_factor_survivors(
        conn,
        source_factor_run_id=source_factor_run_id,
    )
    if not candidates:
        return {
            "source_factor_run_id": source_factor_run_id,
            "status": "no_factor_survivors",
            "runs": [],
            "protocol_version": EXECUTABLE_RESEARCH_VERSION,
        }
    assets = _dataset_assets(conn, int(source["dataset_id"]))
    runs = []
    for row in candidates:
        _emit(
            progress_callback,
            stage="development_candidate",
            candidate_id=row["candidate_id"],
            factor_key=row["factor_key"],
            state="starting",
        )
        existing = _executable_run(
            conn,
            candidate_id=int(row["id"]),
            phase="development_simulation",
            dataset_id=int(source["dataset_id"]),
        )
        if existing:
            runs.append(jsonable(existing))
            continue
        symbols = _candidate_symbols(str(row["factor_key"]), assets)
        cost_readiness = _development_cost_readiness(
            dict(source["cost_model"] or {}),
            symbols,
        )
        simulation = _simulate_candidate(
            conn,
            candidate_payload=dict(row["candidate"]),
            dataset_id=int(source["dataset_id"]),
            symbols=symbols,
            progress_callback=progress_callback,
        )
        result = {
            **simulation,
            "cost_readiness": cost_readiness,
            "factor_key": row["factor_key"],
            "frozen_spec_hash": row["frozen_spec_hash"],
        }
        persisted = _persist_executable_run(
            conn,
            executable_candidate_id=int(row["id"]),
            phase="development_simulation",
            dataset_id=int(source["dataset_id"]),
            source_last_session_date=None,
            signal_confirmation_passed=True,
            simulation_passed=bool(simulation["simulation_passed"]),
            result=result,
        )
        runs.append(jsonable(persisted))
        _emit(
            progress_callback,
            stage="development_candidate",
            candidate_id=row["candidate_id"],
            factor_key=row["factor_key"],
            state="completed",
            passed=bool(simulation["simulation_passed"]),
        )
    conn.commit()
    return {
        "source_factor_run_id": source_factor_run_id,
        "dataset_id": int(source["dataset_id"]),
        "status": "completed",
        "runs": runs,
        "passing_candidate_ids": [
            row["executable_candidate_id"]
            for row in runs
            if row["simulation_passed"]
        ],
        "protocol_version": EXECUTABLE_RESEARCH_VERSION,
    }


def run_locked_confirmation_and_elite_competition(
    conn: psycopg.Connection,
    *,
    source_factor_run_id: int,
    confirmation_dataset_id: int,
    run_elite_campaigns: bool = True,
    campaign_batch_size: int = 50,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Confirm signal and trading evidence on later sessions, then compete."""
    source = _source_discovery(conn, source_factor_run_id)
    development = run_development_simulations(
        conn,
        source_factor_run_id=source_factor_run_id,
        progress_callback=progress_callback,
    )
    passing_ids = {
        int(value) for value in development.get("passing_candidate_ids") or []
    }
    if not passing_ids:
        return {
            "source_factor_run_id": source_factor_run_id,
            "status": "no_development_survivors",
            "confirmation_runs": [],
            "protocol_version": EXECUTABLE_RESEARCH_VERSION,
        }
    if confirmation_dataset_id == int(source["dataset_id"]):
        raise ValueError("Locked confirmation requires a different dataset snapshot.")

    candidate_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *
            FROM intraday_executable_candidates
            WHERE source_factor_run_id = %s AND id = ANY(%s)
            ORDER BY id
            """,
            (source_factor_run_id, list(passing_ids)),
        ).fetchall()
    ]
    factor_keys = list(dict.fromkeys(row["factor_key"] for row in candidate_rows))
    source_manifest = _dataset_manifest(conn, int(source["dataset_id"]))
    cutoff = source_manifest.get("window_end")
    if cutoff is None:
        raise ValueError("Discovery dataset has no window_end.")
    cutoff_session = exchange_session_date(cutoff)

    candles, confirmation_manifest = load_dataset_candles(
        conn,
        dataset_id=confirmation_dataset_id,
        timeframe=TIMEFRAME,
        symbols=list(source["symbols"] or []),
        max_symbols=200,
    )
    forward = {
        symbol: rows_after_session(
            rows,
            session_date_exclusive=cutoff_session,
        )
        for symbol, rows in candles.items()
    }
    forward = {symbol: rows for symbol, rows in forward.items() if rows}
    if not forward:
        raise ValueError(
            "Confirmation snapshot has no complete exchange session after the "
            "discovery window."
        )

    cost_model = dict(source["cost_model"] or {})
    microstructure = load_microstructure(
        conn,
        symbols=list(forward),
        timeframe=TIMEFRAME,
        start=cutoff,
        end=confirmation_manifest.get("window_end"),
        dataset_id=confirmation_dataset_id,
    )
    auctions = load_auction_imbalances(
        conn,
        symbols=list(forward),
        start=cutoff,
        end=confirmation_manifest.get("window_end"),
    )
    integrity = dict(confirmation_manifest.get("integrity") or {})
    institutional = research_data_readiness(
        conn,
        dataset_id=confirmation_dataset_id,
        timeframe=TIMEFRAME,
        universe_key=integrity.get("universe_key"),
    )
    prior_factor_confirmation = conn.execute(
        """
        SELECT *
        FROM intraday_factor_diagnostic_runs
        WHERE mode = 'confirmation' AND status = 'completed'
          AND source_run_id = %s AND dataset_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (source_factor_run_id, confirmation_dataset_id),
    ).fetchone()
    if prior_factor_confirmation:
        factor_confirmation = dict(prior_factor_confirmation["results"] or {})
        factor_confirmation_run_id = int(prior_factor_confirmation["id"])
    else:
        _emit(
            progress_callback,
            stage="factor_confirmation",
            state="starting",
            factors=factor_keys,
        )
        factor_confirmation = evaluate_forward_confirmation(
            forward,
            timeframe=TIMEFRAME,
            factor_keys=factor_keys,
            cost_model=cost_model,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
            institutional_data_readiness=institutional,
        )
        factor_confirmation.update(
            {
                "dataset_id": confirmation_dataset_id,
                "source_run_id": source_factor_run_id,
                "source_dataset_id": int(source["dataset_id"]),
                "forward_only_after": cutoff,
                "forward_only_after_session": cutoff_session,
                "symbols": sorted(forward),
                "frozen_spec_hash": source["frozen_spec_hash"],
            }
        )
        factor_confirmation_run_id = persist_factor_run(
            conn,
            mode="confirmation",
            dataset_id=confirmation_dataset_id,
            source_run_id=source_factor_run_id,
            timeframe=TIMEFRAME,
            factor_keys=factor_keys,
            symbols=sorted(forward),
            result=factor_confirmation,
            spec_hash=str(source["frozen_spec_hash"]),
        )
        _emit(
            progress_callback,
            stage="factor_confirmation",
            state="completed",
            passed=factor_confirmation.get("passed_locked_confirmation") or [],
        )
    passed_factors = set(
        factor_confirmation.get("passed_locked_confirmation") or []
    )

    confirmation_runs: list[dict[str, Any]] = []
    for candidate_row in candidate_rows:
        _emit(
            progress_callback,
            stage="locked_candidate",
            candidate_id=candidate_row["candidate_id"],
            factor_key=candidate_row["factor_key"],
            state="starting",
        )
        existing = _executable_run(
            conn,
            candidate_id=int(candidate_row["id"]),
            phase="locked_confirmation",
            dataset_id=confirmation_dataset_id,
        )
        if existing:
            confirmation_runs.append(jsonable(existing))
            continue
        factor_passed = candidate_row["factor_key"] in passed_factors
        symbols = _candidate_symbols(
            str(candidate_row["factor_key"]),
            list(candles),
        )
        production_costs = cost_model_readiness(cost_model, symbols=symbols)
        simulation: dict[str, Any] = {
            "simulation_passed": False,
            "summary": None,
            "cross_validation_failures": ["FACTOR_CONFIRMATION"],
            "markets": [],
        }
        if factor_passed and production_costs["production_cost_ready"]:
            payload = dict(candidate_row["candidate"])
            payload["parameters"] = {
                **dict(payload["parameters"]),
                "signal_start_session_date": str(cutoff_session),
            }
            simulation = _simulate_candidate(
                conn,
                candidate_payload=payload,
                dataset_id=confirmation_dataset_id,
                symbols=symbols,
                progress_callback=progress_callback,
            )
        result = {
            **simulation,
            "factor_confirmation_run_id": factor_confirmation_run_id,
            "factor_confirmation_passed": factor_passed,
            "production_cost_readiness": production_costs,
            "forward_only_after_session": str(cutoff_session),
            "frozen_spec_hash": candidate_row["frozen_spec_hash"],
        }
        persisted = _persist_executable_run(
            conn,
            executable_candidate_id=int(candidate_row["id"]),
            phase="locked_confirmation",
            dataset_id=confirmation_dataset_id,
            source_last_session_date=cutoff_session,
            signal_confirmation_passed=factor_passed,
            simulation_passed=bool(simulation["simulation_passed"]),
            result=result,
        )
        confirmation_runs.append(jsonable(persisted))
        _emit(
            progress_callback,
            stage="locked_candidate",
            candidate_id=candidate_row["candidate_id"],
            factor_key=candidate_row["factor_key"],
            state="completed",
            signal_passed=factor_passed,
            simulation_passed=bool(simulation["simulation_passed"]),
        )

    competitions = []
    if run_elite_campaigns:
        for run in confirmation_runs:
            if not (
                run["signal_confirmation_passed"]
                and run["simulation_passed"]
            ):
                continue
            candidate_row = next(
                row
                for row in candidate_rows
                if int(row["id"]) == int(run["executable_candidate_id"])
            )
            symbols = _candidate_symbols(
                str(candidate_row["factor_key"]),
                list(candles),
            )
            competitions.append(
                _compete_for_elite(
                    conn,
                    candidate_row=candidate_row,
                    confirmation_run=run,
                    confirmation_dataset_id=confirmation_dataset_id,
                    cutoff_session=cutoff_session,
                    symbols=symbols,
                    batch_size=campaign_batch_size,
                    progress_callback=progress_callback,
                )
            )
    conn.commit()
    return {
        "source_factor_run_id": source_factor_run_id,
        "factor_confirmation_run_id": factor_confirmation_run_id,
        "confirmation_dataset_id": confirmation_dataset_id,
        "forward_only_after_session": str(cutoff_session),
        "passed_factors": sorted(passed_factors),
        "confirmation_runs": confirmation_runs,
        "elite_competitions": competitions,
        "status": "completed",
        "protocol_version": EXECUTABLE_RESEARCH_VERSION,
    }


def executable_research_status(
    conn: psycopg.Connection,
    *,
    source_factor_run_id: int | None = None,
) -> dict[str, Any]:
    where = "WHERE source_factor_run_id = %s" if source_factor_run_id else ""
    params: tuple[Any, ...] = (
        (source_factor_run_id,) if source_factor_run_id else ()
    )
    candidates = [
        jsonable(dict(row))
        for row in conn.execute(
            f"""
            SELECT *
            FROM intraday_executable_candidates
            {where}
            ORDER BY created_at, id
            """,
            params,
        ).fetchall()
    ]
    candidate_ids = [int(row["id"]) for row in candidates]
    runs = []
    activations = []
    if candidate_ids:
        runs = [
            jsonable(dict(row))
            for row in conn.execute(
                """
                SELECT *
                FROM intraday_executable_runs
                WHERE executable_candidate_id = ANY(%s)
                ORDER BY created_at, id
                """,
                (candidate_ids,),
            ).fetchall()
        ]
        activations = [
            jsonable(dict(row))
            for row in conn.execute(
                """
                SELECT *
                FROM intraday_family_activations
                WHERE executable_candidate_id = ANY(%s)
                ORDER BY created_at, id
                """,
                (candidate_ids,),
            ).fetchall()
        ]
    return {
        "source_factor_run_id": source_factor_run_id,
        "candidates": candidates,
        "runs": runs,
        "activations": activations,
        "protocol_version": EXECUTABLE_RESEARCH_VERSION,
    }


def _candidate(
    *,
    source_factor_run_id: int,
    factor_key: str,
    architecture: str,
    recipe: dict[str, Any],
    cost_model: dict[str, Any],
) -> DiscoveryCandidate:
    parameters = {
        **BASE_V2_PARAMETERS,
        **recipe,
        "strategy_architecture": architecture,
        "timeframe": TIMEFRAME,
        "factor_key": factor_key,
        "source_factor_run_id": source_factor_run_id,
        "execution_cost_model": cost_model,
        "execution_cost_scenario": "stressed",
        "fee_rate": 0,
        "slippage_rate": 0,
    }
    blocks = dict(V2_BLOCKS[architecture])
    canonical = canonical_candidate_key(blocks, parameters)
    suffix = sha256(canonical.encode()).hexdigest()[:14]
    return DiscoveryCandidate(
        candidate_id=f"exec30_{architecture[:18]}_{suffix}",
        family_id=architecture,
        parent_candidate_id=None,
        generation=1,
        blocks=blocks,
        parameters=parameters,
        complexity=len(recipe),
        canonical_key=canonical,
    )


def _simulate_candidate(
    conn: psycopg.Connection,
    *,
    candidate_payload: dict[str, Any],
    dataset_id: int,
    symbols: Sequence[str],
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    for index, symbol in enumerate(symbols, start=1):
        _emit(
            progress_callback,
            stage="market_simulation",
            candidate_id=candidate_payload["candidate_id"],
            symbol=symbol,
            completed=index - 1,
            total=len(symbols),
        )
        result = run_campaign_job(
            conn,
            {
                "candidate": candidate_payload,
                "candidate_id": candidate_payload["candidate_id"],
                "family_id": candidate_payload["family_id"],
                "symbol": symbol,
                "timeframe": TIMEFRAME,
                "dataset_id": dataset_id,
                "_dataset_cache": cache,
            },
        )
        result = dict(result)
        result.pop("trades", None)
        status = (
            "promoted"
            if passes_single_market_validation(result)
            else "rejected"
        )
        jobs.append(
            {
                "candidate_id": candidate_payload["candidate_id"],
                "family_id": candidate_payload["family_id"],
                "symbol": symbol,
                "timeframe": TIMEFRAME,
                "status": status,
                "validation_score": result.get("research_score"),
                "failure_reasons": result.get("failure_reasons") or [],
                "result": result,
            }
        )
        _emit(
            progress_callback,
            stage="market_simulation",
            candidate_id=candidate_payload["candidate_id"],
            symbol=symbol,
            completed=index,
            total=len(symbols),
            status=status,
        )
    summary = candidate_consistency_summaries(jobs)[0] if jobs else None
    passed = bool(summary and passes_cross_validation(summary))
    return {
        "simulation_passed": passed,
        "summary": jsonable(summary) if summary else None,
        "cross_validation_failures": (
            cross_validation_failures(summary) if summary else ["NO_MARKETS"]
        ),
        "markets": jsonable(jobs),
    }


def _compete_for_elite(
    conn: psycopg.Connection,
    *,
    candidate_row: dict[str, Any],
    confirmation_run: dict[str, Any],
    confirmation_dataset_id: int,
    cutoff_session: Any,
    symbols: list[str],
    batch_size: int,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    payload = dict(candidate_row["candidate"])
    payload["parameters"] = {
        **dict(payload["parameters"]),
        "signal_start_session_date": str(cutoff_session),
    }
    candidate = candidate_from_payload(payload)
    prior_activation = conn.execute(
        """
        SELECT *
        FROM intraday_family_activations
        WHERE executable_candidate_id = %s
          AND confirmation_run_id = %s
          AND activation_state = 'campaign_eligible'
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(candidate_row["id"]), int(confirmation_run["id"])),
    ).fetchone()
    if prior_activation:
        campaign_id = int(prior_activation["campaign_id"])
    else:
        campaign = _create_intraday_campaign(
            conn,
            name=(
                f"Locked executable 30m confirmation: "
                f"{candidate_row['architecture']}"
            ),
            architecture=str(candidate_row["architecture"]),
            strategy_family_label=str(candidate_row["architecture"]),
            candidates=[candidate],
            supported_timeframes=(TIMEFRAME,),
            timeframes=[TIMEFRAME],
            asset_limit=len(symbols),
            campaign_label=(
                f"locked-source-{candidate_row['source_factor_run_id']}"
                f"-run-{confirmation_run['id']}"
            ),
            assets_override=symbols,
            dataset_id_override=confirmation_dataset_id,
        )
        campaign_id = int(campaign["campaign_id"])
    _persist_activation(
        conn,
        candidate_row=candidate_row,
        confirmation_run_id=int(confirmation_run["id"]),
        campaign_id=campaign_id,
        activation_state="campaign_eligible",
        evidence={
            "locked_factor_confirmation": True,
            "locked_trading_confirmation": True,
            "dataset_id": confirmation_dataset_id,
        },
    )
    last: dict[str, Any] = {}
    for _ in range(10_000):
        _emit(
            progress_callback,
            stage="elite_campaign",
            campaign_id=campaign_id,
            candidate_id=candidate.candidate_id,
            state="running",
        )
        last = run_research_campaign_batch(
            conn,
            campaign_id=campaign_id,
            batch_size=batch_size,
            coordinate_campaign=True,
        )
        if int(last.get("remaining") or 0) == 0:
            break
        if int(last.get("processed") or 0) == 0:
            break
    elite = conn.execute(
        """
        SELECT *
        FROM elite_research_candidates
        WHERE campaign_id = %s
          AND candidate_id = %s
          AND simulation_only = TRUE
          AND promotion_state = 'elite'
        """,
        (campaign_id, candidate.candidate_id),
    ).fetchone()
    if elite:
        _persist_activation(
            conn,
            candidate_row=candidate_row,
            confirmation_run_id=int(confirmation_run["id"]),
            campaign_id=campaign_id,
            activation_state="elite_eligible",
            evidence={
                "elite_candidate_id": int(elite["id"]),
                "promotion_state": elite["promotion_state"],
                "dataset_id": confirmation_dataset_id,
            },
        )
    conn.commit()
    _emit(
        progress_callback,
        stage="elite_campaign",
        campaign_id=campaign_id,
        candidate_id=candidate.candidate_id,
        state="completed" if int(last.get("remaining") or 0) == 0 else "paused",
        remaining=int(last.get("remaining") or 0),
        elite=bool(elite),
    )
    return {
        "candidate_id": candidate.candidate_id,
        "campaign_id": campaign_id,
        "campaign_result": jsonable(last),
        "elite_candidate_id": int(elite["id"]) if elite else None,
        "elite": bool(elite),
    }


def _persist_activation(
    conn: psycopg.Connection,
    *,
    candidate_row: dict[str, Any],
    confirmation_run_id: int,
    campaign_id: int,
    activation_state: str,
    evidence: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO intraday_family_activations(
            architecture, factor_key, executable_candidate_id,
            confirmation_run_id, campaign_id, activation_state,
            activation_evidence, protocol_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(
            executable_candidate_id, confirmation_run_id, activation_state
        ) DO NOTHING
        """,
        (
            candidate_row["architecture"],
            candidate_row["factor_key"],
            int(candidate_row["id"]),
            confirmation_run_id,
            campaign_id,
            activation_state,
            Jsonb(jsonable(evidence)),
            EXECUTABLE_RESEARCH_VERSION,
        ),
    )


def _persist_executable_run(
    conn: psycopg.Connection,
    *,
    executable_candidate_id: int,
    phase: str,
    dataset_id: int,
    source_last_session_date: Any,
    signal_confirmation_passed: bool,
    simulation_passed: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO intraday_executable_runs(
            executable_candidate_id, phase, dataset_id,
            source_last_session_date, signal_confirmation_passed,
            simulation_passed, result, protocol_version
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(executable_candidate_id, phase, dataset_id) DO NOTHING
        RETURNING *
        """,
        (
            executable_candidate_id,
            phase,
            dataset_id,
            source_last_session_date,
            signal_confirmation_passed,
            simulation_passed,
            Jsonb(jsonable(result)),
            EXECUTABLE_RESEARCH_VERSION,
        ),
    ).fetchone()
    if not row:
        row = _executable_run(
            conn,
            candidate_id=executable_candidate_id,
            phase=phase,
            dataset_id=dataset_id,
        )
    return dict(row)


def _executable_run(
    conn: psycopg.Connection,
    *,
    candidate_id: int,
    phase: str,
    dataset_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM intraday_executable_runs
        WHERE executable_candidate_id = %s
          AND phase = %s
          AND dataset_id = %s
        """,
        (candidate_id, phase, dataset_id),
    ).fetchone()
    return dict(row) if row else None


def _source_discovery(
    conn: psycopg.Connection,
    source_factor_run_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM intraday_factor_diagnostic_runs
        WHERE id = %s AND mode = 'discovery' AND status = 'completed'
        """,
        (source_factor_run_id,),
    ).fetchone()
    if not row:
        raise ValueError("Source must be a completed factor discovery run.")
    return dict(row)


def _validate_source(source: dict[str, Any]) -> None:
    if str(source["timeframe"]) != TIMEFRAME:
        raise ValueError("Executable intraday research is restricted to 30m.")
    if not dict(source["results"] or {}).get("confirmation_data_accessed") is False:
        raise ValueError(
            "Source discovery run did not preserve a locked confirmation set."
        )


def _require_sip_calibration(cost_model: dict[str, Any]) -> int:
    calibration_id = cost_model.get("calibration_id")
    if calibration_id is None:
        raise ValueError(
            "A persisted observed execution-cost calibration is required."
        )
    feed = str(cost_model.get("feed") or "").lower()
    if feed not in {"sip", "consolidated", "cta", "utp"}:
        raise ValueError(
            f"Full-feed execution evidence is required; received feed={feed!r}."
        )
    return int(calibration_id)


def _development_cost_readiness(
    cost_model: dict[str, Any],
    symbols: Sequence[str],
) -> dict[str, Any]:
    readiness = cost_model_readiness(cost_model, symbols=symbols)
    required = (
        "observed_cost_available",
        "stressed_cost_available",
        "minimum_quote_observations",
        "minimum_symbol_coverage",
        "consolidated_or_full_feed",
    )
    missing = [key for key in required if not readiness["gates"][key]]
    if missing:
        raise ValueError(
            "Development simulation requires observed SIP cost coverage: "
            + ", ".join(missing)
        )
    return readiness


def _dataset_manifest(
    conn: psycopg.Connection,
    dataset_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM research_dataset_manifests
        WHERE id = %s AND dataset_kind = 'intraday' AND immutable = TRUE
        """,
        (dataset_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No immutable intraday dataset id={dataset_id}.")
    return dict(row)


def _dataset_assets(conn: psycopg.Connection, dataset_id: int) -> list[str]:
    return [
        str(value).upper()
        for value in (_dataset_manifest(conn, dataset_id)["assets"] or [])
    ]


def _candidate_symbols(factor_key: str, assets: Sequence[str]) -> list[str]:
    available = list(dict.fromkeys(str(value).upper() for value in assets))
    if factor_key in {
        "first_to_last_half_hour_market_momentum",
        "first_to_last_half_hour_market_reversal",
    }:
        selected = [symbol for symbol in ("SPY", "QQQ") if symbol in available]
        if len(selected) < 2:
            raise ValueError("First-to-last research requires both SPY and QQQ.")
        return selected
    return available


def _hash(value: Any) -> str:
    return sha256(
        dumps(jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _emit(
    callback: ProgressCallback | None,
    **payload: Any,
) -> None:
    if callback is not None:
        callback(payload)
