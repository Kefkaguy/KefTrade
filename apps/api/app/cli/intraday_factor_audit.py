"""VPS CLI for continuous factor discovery and locked forward confirmation."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.db import connect
from app.services.intraday_factor_diagnostics import (
    DEFAULT_FACTOR_KEYS,
    FACTOR_DIAGNOSTICS_VERSION,
    FACTOR_SPECS,
    evaluate_factor_discovery,
    evaluate_forward_confirmation,
    frozen_spec_hash,
    load_auction_imbalances,
    load_certification,
    load_cost_model,
    load_dataset_candles,
    load_microstructure,
    persist_certification,
    persist_factor_run,
    sector_map,
)
from app.services.intraday_hypotheses import (
    GAP_EXPERIMENT_KEY,
    assert_not_retired,
    gap_experiment_hypotheses,
    persist_hypotheses,
    retire_factor_version,
    retired_factor_versions,
)
from app.services.intraday_research_controls import certify_measurement_instrument
from app.services.intraday_research_integrity import exchange_session_date, rows_after_session
from app.services.intraday_research_data import research_data_readiness
from app.services.intraday_research_leakage import audit_factor_leakage
from app.services.intraday_dataset_quality import duplicate_rows, session_shape_report
from app.services.intraday_session_calendar import timeframe_minutes
from app.services.intraday_trial_ledger import (
    assert_declared,
    declare_trials,
    effective_trials_for_run,
    load_declaration,
    record_declaration_use,
    trial_ledger_summary,
)


def _latest_quality_report(conn: Any, *, dataset_id: int, timeframe: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, ready_for_discovery, quality_passed, power_passed, report
        FROM intraday_dataset_quality_reports
        WHERE dataset_id = %s AND timeframe = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (dataset_id, timeframe),
    ).fetchone()
    if not row:
        raise ValueError(
            f"No dataset quality report exists for dataset {dataset_id}. Run "
            "`intraday_dataset_pipeline quality` before discovery: a factor "
            "result from an unmeasured dataset cannot be interpreted."
        )
    return dict(row)


def _factor_keys(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_FACTOR_KEYS)
    keys = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(keys) - set(FACTOR_SPECS))
    if unknown:
        raise ValueError(f"Unknown factor keys: {unknown}")
    return list(dict.fromkeys(keys))


def _symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))


def _dataset_id(conn: Any, requested: int | None) -> int:
    if requested is not None:
        return requested
    row = conn.execute(
        """
        SELECT id
        FROM research_dataset_manifests
        WHERE dataset_kind = 'intraday'
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        raise ValueError("No intraday dataset snapshot exists.")
    return int(row["id"])


def _cost_calibration_id(conn: Any, requested: str | None) -> int | None:
    if requested is None:
        return None
    if requested.lower() == "latest":
        row = conn.execute(
            "SELECT id FROM intraday_execution_cost_calibrations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise ValueError("No execution-cost calibration exists.")
        return int(row["id"])
    try:
        return int(requested)
    except ValueError as error:
        raise ValueError("--cost-calibration-id must be an integer or 'latest'.") from error


def _dataset_symbols(conn: Any, *, dataset_id: int, timeframe: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT symbol FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = %s ORDER BY symbol
        """,
        (dataset_id, timeframe),
    ).fetchall()
    return [str(row["symbol"]) for row in rows]


def _calendar_audit_sql(conn: Any, *, dataset_id: int, timeframe: str) -> dict[str, Any]:
    """Calendar integrity over the whole dataset, computed in the database.

    Loading every candle to count bar slots costs gigabytes on a universe-scale
    snapshot and answers a question SQL answers exactly.
    """
    shapes = session_shape_report(conn, dataset_id=dataset_id, timeframe=timeframe)
    duplicates = duplicate_rows(conn, dataset_id=dataset_id, timeframe=timeframe)
    row = conn.execute(
        """
        SELECT COUNT(*) FILTER (
                   WHERE EXTRACT(second FROM timestamp) <> 0
                      OR MOD(EXTRACT(minute FROM timestamp)::int, %s) <> 0
               ) AS misaligned,
               COUNT(*) AS rows
        FROM research_dataset_candles
        WHERE dataset_id = %s AND timeframe = %s
        """,
        (timeframe_minutes(timeframe), dataset_id, timeframe),
    ).fetchone()
    misaligned = int((row or {}).get("misaligned") or 0)
    return {
        "scope": "full_dataset",
        "candle_rows": int((row or {}).get("rows") or 0),
        "extended_hours_rows": shapes["extended_hours_rows"],
        "session_shapes": shapes["session_shapes"],
        "complete_session_share": shapes["complete_session_share"],
        "expected_full_session_bars": shapes["expected_full_session_bars"],
        "duplicate_symbol_timestamp_rows": duplicates["duplicate_symbol_timestamp_rows"],
        "sources": duplicates["sources"],
        "misaligned_bar_starts": misaligned,
        "naive_timestamps": 0,
        "timestamps_normalized": misaligned == 0,
    }


def _stream_observations(
    conn: Any,
    *,
    dataset_id: int,
    timeframe: str,
    universe: list[str],
    factor_keys: list[str],
    batch_size: int,
    microstructure_by_symbol: Any = None,
    auction_by_symbol: Any = None,
) -> dict[str, Any]:
    """Build every factor's observations without holding the universe in memory.

    A ten-year, 237-symbol snapshot is roughly seven million candle rows, which
    as Python dicts exceeds the machine.  Every factor streamed here is
    per-symbol, so its observations are identical whether the symbols are
    processed together or a batch at a time, and only the observations -- far
    smaller than the bars -- are retained.
    """
    from app.services.intraday_research_power import benchmark_session_context
    from app.services.intraday_session_calendar import extended_hours_audit

    observations: dict[str, list[dict[str, Any]]] = {key: [] for key in factor_keys}
    session_dates: set[Any] = set()
    symbols_with_rows: set[str] = set()
    candle_rows = 0
    calendar: dict[str, Any] = {"session_shapes": {}}

    step = max(1, batch_size)
    for start in range(0, len(universe), step):
        batch = universe[start : start + step]
        candles, _ = load_dataset_candles(
            conn,
            dataset_id=dataset_id,
            timeframe=timeframe,
            symbols=batch,
            max_symbols=len(batch),
            include_benchmarks=False,
        )
        if not candles:
            continue
        print(f"  observations: {min(start + step, len(universe))}/{len(universe)} symbols", flush=True)
        for symbol, rows in candles.items():
            symbols_with_rows.add(symbol)
            candle_rows += len(rows)
            for row in rows:
                session_dates.add(exchange_session_date(row["timestamp"]))
        audit = extended_hours_audit(candles, timeframe=timeframe)
        for field in (
            "extended_hours_rows",
            "duplicate_symbol_timestamp_rows",
            "naive_timestamps",
            "misaligned_bar_starts",
        ):
            calendar[field] = calendar.get(field, 0) + audit[field]
        for shape, count in (audit.get("session_shapes") or {}).items():
            calendar["session_shapes"][shape] = calendar["session_shapes"].get(shape, 0) + count

        for key in factor_keys:
            spec = FACTOR_SPECS[key]
            if timeframe not in spec.supported_timeframes:
                continue
            observations[key].extend(
                spec.builder(
                    candles,
                    timeframe=timeframe,
                    microstructure_by_symbol=microstructure_by_symbol,
                    auction_by_symbol=auction_by_symbol,
                )
            )
        del candles

    # The market-direction and volatility regimes need the benchmark, which a
    # batch may not contain, so it is loaded once on its own.
    benchmark_candles, _ = load_dataset_candles(
        conn,
        dataset_id=dataset_id,
        timeframe=timeframe,
        symbols=["SPY"],
        max_symbols=1,
        include_benchmarks=False,
    )
    benchmark = benchmark_session_context(benchmark_candles, timeframe=timeframe)

    shapes = calendar["session_shapes"]
    total_sessions = sum(shapes.values()) or 1
    calendar.update(
        {
            "timeframe": timeframe,
            "candle_rows": candle_rows,
            "timestamps_normalized": (
                calendar.get("naive_timestamps", 0) == 0
                and calendar.get("misaligned_bar_starts", 0) == 0
            ),
            "complete_session_share": round(
                (shapes.get("full", 0) + shapes.get("early_close", 0)) / total_sessions, 6
            ),
        }
    )
    quote_bars = sum(len(rows) for rows in (microstructure_by_symbol or {}).values())
    coverage = quote_bars / candle_rows if candle_rows else 0.0
    gates = {
        "minimum_symbols": len(symbols_with_rows) >= 20,
        "minimum_sessions": len(session_dates) >= 252,
        "candle_data_present": candle_rows > 0,
        "quote_coverage_for_execution": coverage >= 0.80,
    }
    return {
        "observations_by_factor": observations,
        "session_dates": sorted(session_dates),
        "calendar_audit": calendar,
        "benchmark_context": benchmark,
        "data_readiness": {
            "symbols": len(symbols_with_rows),
            "distinct_sessions": len(session_dates),
            "candle_rows": candle_rows,
            "microstructure_bars": quote_bars,
            "microstructure_coverage": round(coverage, 6),
            "gates": gates,
            "candle_research_ready": all(
                gates[key]
                for key in ("minimum_symbols", "minimum_sessions", "candle_data_present")
            ),
            "execution_research_ready": all(gates.values()),
            "limitations": [key for key, ok in gates.items() if not ok],
        },
    }


def certify(args: argparse.Namespace) -> dict[str, Any]:
    """Prove the instrument before it is pointed at a real hypothesis."""
    keys = _factor_keys(args.factors)
    with connect() as conn:
        dataset_id = _dataset_id(conn, args.dataset_id)
        # Calendar integrity is a property of every row, so it is measured over
        # the whole dataset in SQL.  The leakage experiment is a mechanical
        # invariance check on the builders, so it runs on a bounded, recorded
        # subsample: loading a universe-scale snapshot into memory and then
        # copying it once per cut point needs tens of gigabytes and proves
        # nothing the subsample does not.
        calendar = _calendar_audit_sql(
            conn, dataset_id=dataset_id, timeframe=args.timeframe
        )
        available = _dataset_symbols(conn, dataset_id=dataset_id, timeframe=args.timeframe)
        requested = _symbols(args.symbols)
        if requested:
            audit_symbols = [item for item in requested if item in set(available)]
        else:
            # Deterministic spread across the alphabetically ordered universe,
            # with the benchmarks always present.
            step = max(1, len(available) // max(1, args.audit_symbols))
            audit_symbols = available[::step][: args.audit_symbols]
            audit_symbols = list(
                dict.fromkeys(
                    [*audit_symbols, *[s for s in ("SPY", "QQQ") if s in available]]
                )
            )
        candles, _manifest = load_dataset_candles(
            conn,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            symbols=audit_symbols,
            max_symbols=len(audit_symbols),
        )
        if not candles:
            raise ValueError("The selected frozen dataset contains no candles.")
        controls = certify_measurement_instrument(
            FACTOR_SPECS,
            timeframe=args.timeframe,
            candles_by_symbol=candles,
            sessions=args.control_sessions,
        )
        # Quote- and auction-dependent factors cannot be exercised without
        # their data, and a factor that never ran is not a factor that passed.
        auditable = [
            key
            for key in keys
            if not FACTOR_SPECS[key].requires_quotes
            and not FACTOR_SPECS[key].requires_auction_data
        ]
        leakage = audit_factor_leakage(
            FACTOR_SPECS,
            candles,
            timeframe=args.timeframe,
            factor_keys=auditable,
        )
        stored = persist_certification(
            conn,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            factor_keys=auditable,
            controls=controls,
            leakage=leakage,
            calendar=calendar,
        )
        return {
            **stored,
            "dataset_id": dataset_id,
            "calendar_scope": "full_dataset",
            "leakage_audit_scope": {
                "symbols_audited": len(candles),
                "symbols_in_dataset": len(available),
                "detail": (
                    "Leakage is a mechanical invariance property of the builders, "
                    "so it is proven on a bounded deterministic subsample; "
                    "calendar integrity is measured over every row."
                ),
            },
            "symbols": sorted(candles),
            "session_calendar_audit": calendar,
            "controls": controls,
            "leakage": leakage,
            "factors_excluded_from_leakage_audit": sorted(set(keys) - set(auditable)),
        }


def declare(args: argparse.Namespace) -> dict[str, Any]:
    """Predeclare a test list before any of its results exist."""
    keys = _factor_keys(args.factors)
    with connect() as conn:
        dataset_id = args.dataset_id if args.dataset_id else None
        declaration = declare_trials(
            conn,
            purpose=args.purpose,
            timeframe=args.timeframe,
            factor_keys=keys,
            dataset_id=dataset_id,
            hypothesis=args.hypothesis,
            protocol_version=FACTOR_DIAGNOSTICS_VERSION,
        )
        return {
            "declaration_id": int(declaration["id"]),
            "already_declared": declaration["already_declared"],
            "purpose": declaration["purpose"],
            "timeframe": declaration["timeframe"],
            "declared_factor_keys": list(declaration["declared_factor_keys"]),
            "declared_test_count": int(declaration["declared_test_count"]),
            "created_at": declaration["created_at"],
        }


def ledger(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return trial_ledger_summary(conn, timeframe=args.timeframe)


def discover(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeframe != "30m":
        raise ValueError("Executable intraday factor research is restricted to 30m.")
    keys = _factor_keys(args.factors)
    with connect() as conn:
        dataset_id = _dataset_id(conn, args.dataset_id)
        # Both gates come before any result is computed: an uncertified
        # instrument cannot separate a null from a defect, and an undeclared
        # test cannot be charged honestly to the multiple-testing correction.
        certification = load_certification(
            conn, certification_id=args.certification_id
        )
        if not certification["certified"] and not args.allow_uncertified:
            raise ValueError(
                f"Certification {args.certification_id} did not pass "
                f"(controls={certification['controls_passed']}, "
                f"leakage={certification['leakage_passed']}, "
                f"calendar={certification['calendar_passed']}). "
                "Fix the instrument, or pass --allow-uncertified to record an "
                "explicitly non-certified exploratory run."
            )
        declaration = load_declaration(conn, args.declaration_id)
        declaration_check = assert_declared(
            declaration, timeframe=args.timeframe, factor_keys=keys
        )
        # A dataset that cannot resolve the effect produces a reading, not a
        # result, so the power gate is checked before anything is calculated.
        quality = _latest_quality_report(
            conn, dataset_id=dataset_id, timeframe=args.timeframe
        )
        if not quality["ready_for_discovery"] and not args.allow_underpowered:
            raise ValueError(
                f"Dataset {dataset_id} did not clear the quality and power gate "
                f"(quality={quality['quality_passed']}, power={quality['power_passed']}). "
                "Expand the dataset, or pass --allow-underpowered to record an "
                "explicitly non-interpretable exploratory run."
            )
        manifest = dict(
            conn.execute(
                "SELECT * FROM research_dataset_manifests WHERE id = %s", (dataset_id,)
            ).fetchone()
            or {}
        )
        universe = (
            _symbols(args.symbols)
            or _dataset_symbols(conn, dataset_id=dataset_id, timeframe=args.timeframe)
        )[: args.max_symbols]
        if not universe:
            raise ValueError("The selected frozen dataset contains no candles for this request.")
        cost_model = load_cost_model(conn, _cost_calibration_id(conn, args.cost_calibration_id))
        microstructure = load_microstructure(
            conn,
            symbols=universe,
            timeframe=args.timeframe,
            start=manifest.get("window_start"),
            end=manifest.get("window_end"),
            dataset_id=dataset_id,
        )
        auctions = load_auction_imbalances(
            conn,
            symbols=universe,
            start=manifest.get("window_start"),
            end=manifest.get("window_end"),
        )
        integrity = dict(manifest.get("integrity") or {})
        institutional_readiness = research_data_readiness(
            conn,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            universe_key=integrity.get("universe_key"),
        )
        spec_hash = frozen_spec_hash(
            factor_keys=keys,
            timeframe=args.timeframe,
            cost_model=cost_model,
        )
        assert_not_retired(
            conn, timeframe=args.timeframe, factor_keys=keys, spec_hash=spec_hash
        )
        trial_ledger = effective_trials_for_run(
            conn,
            timeframe=args.timeframe,
            factor_keys=keys,
            spec_hash=spec_hash,
        )
        streamed = _stream_observations(
            conn,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            universe=universe,
            factor_keys=keys,
            batch_size=args.symbol_batch,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
        )
        result = evaluate_factor_discovery(
            {},
            observations_by_factor=streamed["observations_by_factor"],
            session_dates=streamed["session_dates"],
            symbols=universe,
            data_readiness=streamed["data_readiness"],
            calendar_audit=streamed["calendar_audit"],
            benchmark_context=streamed["benchmark_context"],
            timeframe=args.timeframe,
            factor_keys=keys,
            cost_model=cost_model,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
            institutional_data_readiness=institutional_readiness,
            effective_trials=trial_ledger["effective_trials"],
            trial_ledger=trial_ledger,
            sector_by_symbol=sector_map(conn, universe),
            certification=certification,
        )
        result["dataset_id"] = dataset_id
        result["symbols"] = sorted(universe)
        result["frozen_spec_hash"] = spec_hash
        result["trial_declaration"] = declaration_check
        result["dataset_quality_report"] = {
            "quality_report_id": int(quality["id"]),
            "ready_for_discovery": bool(quality["ready_for_discovery"]),
            "quality_passed": bool(quality["quality_passed"]),
            "power_passed": bool(quality["power_passed"]),
            "gap_experiment_power": (quality["report"] or {}).get("gap_experiment_power"),
        }
        result["run_id"] = persist_factor_run(
            conn,
            mode="discovery",
            dataset_id=dataset_id,
            source_run_id=None,
            timeframe=args.timeframe,
            factor_keys=keys,
            symbols=sorted(universe),
            result=result,
            spec_hash=spec_hash,
            certification_id=certification["certification_id"],
            declaration_id=int(declaration["id"]),
        )
        record_declaration_use(
            conn,
            declaration_id=int(declaration["id"]),
            run_id=result["run_id"],
            factor_keys=keys,
        )
        return result


def declare_experiment(args: argparse.Namespace) -> dict[str, Any]:
    """Predeclare the bounded gap experiment: six hypotheses, six trials."""
    hypotheses = gap_experiment_hypotheses(required_event_count=args.required_event_count)
    with connect() as conn:
        stored = persist_hypotheses(
            conn,
            hypotheses,
            experiment_key=GAP_EXPERIMENT_KEY,
            timeframe=args.timeframe,
            dataset_id=args.dataset_id,
        )
        declaration = declare_trials(
            conn,
            purpose=f"{GAP_EXPERIMENT_KEY}: bounded six-test gap-down experiment",
            timeframe=args.timeframe,
            factor_keys=[item.factor_key for item in hypotheses],
            dataset_id=args.dataset_id,
            hypothesis=(
                "Gap-down acceptance continues and gap-down absorption reverses, "
                "measured over 1, 2 and 4 bar holds at fixed predeclared thresholds."
            ),
            protocol_version=FACTOR_DIAGNOSTICS_VERSION,
        )
        return {
            "experiment_key": GAP_EXPERIMENT_KEY,
            "declaration_id": int(declaration["id"]),
            "already_declared": declaration["already_declared"],
            "tests": len(hypotheses),
            "hypotheses": stored,
            "factor_keys": [item.factor_key for item in hypotheses],
            "fixed_parameters": dict(hypotheses[0].parameters),
        }


def retire(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        if args.list:
            return {
                "timeframe": args.timeframe,
                "retired": retired_factor_versions(conn, timeframe=args.timeframe),
            }
        if not (args.factor_key and args.spec_hash and args.reason):
            raise ValueError("--factor-key, --spec-hash and --reason are required to retire.")
        return {
            "factor_key": args.factor_key,
            **retire_factor_version(
                conn,
                factor_key=args.factor_key,
                timeframe=args.timeframe,
                spec_hash=args.spec_hash,
                reason=args.reason,
                evidence={"retired_by": "intraday_factor_audit", "run_id": args.run_id},
            ),
        }


def confirm(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        source = conn.execute(
            """
            SELECT run.*, source_manifest.window_end AS source_window_end
            FROM intraday_factor_diagnostic_runs run
            JOIN research_dataset_manifests source_manifest ON source_manifest.id = run.dataset_id
            WHERE run.id = %s AND run.mode = 'discovery' AND run.status = 'completed'
            """,
            (args.source_run_id,),
        ).fetchone()
        if not source:
            raise ValueError("The source must be a completed discovery run.")
        if str(source["timeframe"]) != "30m":
            raise ValueError("Executable intraday confirmation is restricted to 30m.")
        source_result = source["results"]
        keys = list(source_result.get("selected_for_forward_confirmation") or [])
        if not keys:
            raise ValueError("The discovery run selected no factor for forward confirmation.")

        dataset_id = _dataset_id(conn, args.dataset_id)
        if dataset_id == int(source["dataset_id"]):
            raise ValueError("Confirmation requires a different immutable dataset snapshot.")
        candles, manifest = load_dataset_candles(
            conn,
            dataset_id=dataset_id,
            timeframe=str(source["timeframe"]),
            symbols=list(source["symbols"] or []),
            max_symbols=args.max_symbols,
        )
        cutoff = source["source_window_end"]
        if cutoff is None:
            raise ValueError("The discovery dataset has no window_end; forward non-overlap cannot be proven.")
        cutoff_session_date = exchange_session_date(cutoff)
        forward = {
            symbol: rows_after_session(
                rows,
                session_date_exclusive=cutoff_session_date,
            )
            for symbol, rows in candles.items()
        }
        forward = {symbol: rows for symbol, rows in forward.items() if rows}
        if not forward:
            raise ValueError(
                "The confirmation dataset contains no candles after the discovery window. "
                "Create a later snapshot after new sessions arrive."
            )
        # Locked confirmation runs exactly once against exactly the frozen
        # specification. A second attempt, or a changed specification, would
        # turn the untouched sample into another validation set.
        previous = conn.execute(
            """
            SELECT id FROM intraday_factor_diagnostic_runs
            WHERE mode = 'confirmation' AND source_run_id = %s AND status = 'completed'
            """,
            (args.source_run_id,),
        ).fetchall()
        if previous and not args.allow_repeat:
            raise ValueError(
                f"Discovery run {args.source_run_id} has already been confirmed by run(s) "
                f"{[int(row['id']) for row in previous]}. The confirmation sample is "
                "consumed; it cannot be used to diagnose, tune or retry."
            )
        expected_hash = frozen_spec_hash(
            factor_keys=keys,
            timeframe=str(source["timeframe"]),
            cost_model=dict(source["cost_model"]),
        )
        if expected_hash != str(source["frozen_spec_hash"]):
            raise ValueError(
                "The frozen specification does not reproduce the discovery hash "
                f"({expected_hash[:12]} vs {str(source['frozen_spec_hash'])[:12]}). "
                "Confirmation requires the exact declared specification."
            )
        assert_not_retired(
            conn,
            timeframe=str(source["timeframe"]),
            factor_keys=keys,
            spec_hash=str(source["frozen_spec_hash"]),
        )
        cost_model = dict(source["cost_model"])
        microstructure = load_microstructure(
            conn,
            symbols=list(forward),
            timeframe=str(source["timeframe"]),
            start=cutoff,
            end=manifest.get("window_end"),
            dataset_id=dataset_id,
        )
        auctions = load_auction_imbalances(
            conn,
            symbols=list(forward),
            start=cutoff,
            end=manifest.get("window_end"),
        )
        integrity = dict(manifest.get("integrity") or {})
        institutional_readiness = research_data_readiness(
            conn,
            dataset_id=dataset_id,
            timeframe=str(source["timeframe"]),
            universe_key=integrity.get("universe_key"),
        )
        certification = (
            load_certification(conn, certification_id=args.certification_id)
            if args.certification_id
            else (
                load_certification(
                    conn, certification_id=int(source["certification_id"])
                )
                if source["certification_id"]
                else None
            )
        )
        trial_ledger = effective_trials_for_run(
            conn,
            timeframe=str(source["timeframe"]),
            factor_keys=keys,
            spec_hash=str(source["frozen_spec_hash"]),
        )
        result = evaluate_forward_confirmation(
            forward,
            timeframe=str(source["timeframe"]),
            factor_keys=keys,
            cost_model=cost_model,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
            institutional_data_readiness=institutional_readiness,
            effective_trials=trial_ledger["effective_trials"],
            trial_ledger=trial_ledger,
            sector_by_symbol=sector_map(conn, list(forward)),
            certification=certification,
        )
        result.update(
            {
                "dataset_id": dataset_id,
                "source_run_id": args.source_run_id,
                "source_dataset_id": int(source["dataset_id"]),
                "forward_only_after": cutoff,
                "forward_only_after_session": cutoff_session_date,
                "symbols": sorted(forward),
                "frozen_spec_hash": source["frozen_spec_hash"],
            }
        )
        result["run_id"] = persist_factor_run(
            conn,
            mode="confirmation",
            dataset_id=dataset_id,
            source_run_id=args.source_run_id,
            timeframe=str(source["timeframe"]),
            factor_keys=keys,
            symbols=sorted(forward),
            result=result,
            spec_hash=str(source["frozen_spec_hash"]),
            certification_id=(certification or {}).get("certification_id"),
            declaration_id=source["declaration_id"],
        )
        # Failing locked confirmation is terminal for that factor version.
        retired: list[dict[str, Any]] = []
        for key in result.get("failed_locked_confirmation") or []:
            retired.append(
                {
                    "factor_key": key,
                    **retire_factor_version(
                        conn,
                        factor_key=key,
                        timeframe=str(source["timeframe"]),
                        spec_hash=str(source["frozen_spec_hash"]),
                        reason="failed_locked_confirmation",
                        evidence={
                            "confirmation_run_id": result["run_id"],
                            "source_run_id": args.source_run_id,
                            "failed_gates": (
                                (result["factors"].get(key) or {}).get("evidence_gate") or {}
                            ).get("failed"),
                        },
                    ),
                }
            )
        result["retired_factor_versions"] = retired
        return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Backend-only factor research. Discovery withholds confirmation; "
            "confirmation requires later, non-overlapping frozen candles."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    certification = commands.add_parser(
        "certify",
        help=(
            "Prove the measurement path recovers a planted factor, stays quiet "
            "on placebos, and reads no future data."
        ),
    )
    certification.add_argument("--dataset-id", type=int)
    certification.add_argument("--timeframe", choices=("15m", "30m"), default="30m")
    certification.add_argument("--symbols")
    certification.add_argument("--max-symbols", type=int, default=200)
    certification.add_argument("--factors")
    certification.add_argument("--control-sessions", type=int, default=260)
    certification.add_argument(
        "--audit-symbols",
        type=int,
        default=24,
        help=(
            "Symbols loaded for the leakage experiment. Calendar integrity is "
            "always measured over the whole dataset in SQL."
        ),
    )

    declaration = commands.add_parser(
        "declare",
        help="Predeclare a test list before its results exist.",
    )
    declaration.add_argument("--purpose", required=True)
    declaration.add_argument("--timeframe", choices=("15m", "30m"), default="30m")
    declaration.add_argument("--factors", required=True)
    declaration.add_argument("--dataset-id", type=int)
    declaration.add_argument("--hypothesis")

    ledger_command = commands.add_parser(
        "ledger",
        help="Report every trial ever recorded at this timeframe.",
    )
    ledger_command.add_argument("--timeframe", choices=("15m", "30m"), default="30m")

    discovery = commands.add_parser("discover")
    discovery.add_argument("--dataset-id", type=int)
    discovery.add_argument("--timeframe", choices=("30m",), default="30m")
    discovery.add_argument("--symbols")
    discovery.add_argument("--max-symbols", type=int, default=400)
    discovery.add_argument(
        "--symbol-batch",
        type=int,
        default=20,
        help="Symbols loaded at once while building observations.",
    )
    discovery.add_argument("--factors")
    discovery.add_argument("--certification-id", type=int, required=True)
    discovery.add_argument("--declaration-id", type=int, required=True)
    discovery.add_argument(
        "--allow-uncertified",
        action="store_true",
        help=(
            "Record a run against an instrument that failed certification. "
            "The run is stored and marked uncertified; its results are not "
            "evidence about the market."
        ),
    )
    discovery.add_argument(
        "--allow-underpowered",
        action="store_true",
        help=(
            "Record a run on a dataset that failed the power gate. The result "
            "cannot distinguish absence of alpha from an unresolvable sample."
        ),
    )
    discovery.add_argument(
        "--cost-calibration-id",
        help="Calibration integer id, or 'latest'. Omit to retain the conservative 30bps baseline.",
    )

    experiment = commands.add_parser(
        "declare-experiment",
        help=(
            "Predeclare the bounded six-test gap-down experiment: six "
            "hypotheses and one trial declaration."
        ),
    )
    experiment.add_argument("--timeframe", choices=("15m", "30m"), default="30m")
    experiment.add_argument("--dataset-id", type=int)
    experiment.add_argument("--required-event-count", type=int, default=850)

    retirement = commands.add_parser(
        "retire", help="Permanently retire a factor version, or list retirements."
    )
    retirement.add_argument("--timeframe", choices=("15m", "30m"), default="30m")
    retirement.add_argument("--list", action="store_true")
    retirement.add_argument("--factor-key")
    retirement.add_argument("--spec-hash")
    retirement.add_argument("--reason")
    retirement.add_argument("--run-id", type=int)

    confirmation = commands.add_parser("confirm")
    confirmation.add_argument("--source-run-id", type=int, required=True)
    confirmation.add_argument("--dataset-id", type=int)
    confirmation.add_argument("--max-symbols", type=int, default=200)
    confirmation.add_argument("--certification-id", type=int)
    confirmation.add_argument(
        "--allow-repeat",
        action="store_true",
        help="Re-run a confirmation that already executed. The sample is no longer untouched.",
    )
    return root


COMMANDS = {
    "certify": certify,
    "declare": declare,
    "declare-experiment": declare_experiment,
    "retire": retire,
    "ledger": ledger,
    "discover": discover,
    "confirm": confirm,
}


def main() -> None:
    args = parser().parse_args()
    print(
        "Intraday factor research | backend only | confirmation locked from discovery",
        flush=True,
    )
    result = COMMANDS[args.command](args)
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
