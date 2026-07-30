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
from app.services.intraday_research_controls import certify_measurement_instrument
from app.services.intraday_research_integrity import exchange_session_date, rows_after_session
from app.services.intraday_research_data import research_data_readiness
from app.services.intraday_research_leakage import audit_factor_leakage
from app.services.intraday_session_calendar import extended_hours_audit
from app.services.intraday_trial_ledger import (
    assert_declared,
    declare_trials,
    effective_trials_for_run,
    load_declaration,
    record_declaration_use,
    trial_ledger_summary,
)


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


def certify(args: argparse.Namespace) -> dict[str, Any]:
    """Prove the instrument before it is pointed at a real hypothesis."""
    keys = _factor_keys(args.factors)
    with connect() as conn:
        dataset_id = _dataset_id(conn, args.dataset_id)
        candles, _manifest = load_dataset_candles(
            conn,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            symbols=_symbols(args.symbols),
            max_symbols=args.max_symbols,
        )
        if not candles:
            raise ValueError("The selected frozen dataset contains no candles.")
        calendar = extended_hours_audit(candles, timeframe=args.timeframe)
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
        candles, manifest = load_dataset_candles(
            conn,
            dataset_id=dataset_id,
            timeframe=args.timeframe,
            symbols=_symbols(args.symbols),
            max_symbols=args.max_symbols,
        )
        if not candles:
            raise ValueError("The selected frozen dataset contains no candles for this request.")
        cost_model = load_cost_model(conn, _cost_calibration_id(conn, args.cost_calibration_id))
        microstructure = load_microstructure(
            conn,
            symbols=list(candles),
            timeframe=args.timeframe,
            start=manifest.get("window_start"),
            end=manifest.get("window_end"),
            dataset_id=dataset_id,
        )
        auctions = load_auction_imbalances(
            conn,
            symbols=list(candles),
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
        trial_ledger = effective_trials_for_run(
            conn,
            timeframe=args.timeframe,
            factor_keys=keys,
            spec_hash=spec_hash,
        )
        result = evaluate_factor_discovery(
            candles,
            timeframe=args.timeframe,
            factor_keys=keys,
            cost_model=cost_model,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
            institutional_data_readiness=institutional_readiness,
            effective_trials=trial_ledger["effective_trials"],
            trial_ledger=trial_ledger,
            sector_by_symbol=sector_map(conn, list(candles)),
            certification=certification,
        )
        result["dataset_id"] = dataset_id
        result["symbols"] = sorted(candles)
        result["frozen_spec_hash"] = spec_hash
        result["trial_declaration"] = declaration_check
        result["run_id"] = persist_factor_run(
            conn,
            mode="discovery",
            dataset_id=dataset_id,
            source_run_id=None,
            timeframe=args.timeframe,
            factor_keys=keys,
            symbols=sorted(candles),
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
    discovery.add_argument("--max-symbols", type=int, default=200)
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
        "--cost-calibration-id",
        help="Calibration integer id, or 'latest'. Omit to retain the conservative 30bps baseline.",
    )

    confirmation = commands.add_parser("confirm")
    confirmation.add_argument("--source-run-id", type=int, required=True)
    confirmation.add_argument("--dataset-id", type=int)
    confirmation.add_argument("--max-symbols", type=int, default=200)
    confirmation.add_argument("--certification-id", type=int)
    return root


COMMANDS = {
    "certify": certify,
    "declare": declare,
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
