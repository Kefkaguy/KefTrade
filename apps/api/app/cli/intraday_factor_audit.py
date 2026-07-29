"""VPS CLI for continuous factor discovery and locked forward confirmation."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.db import connect
from app.services.intraday_factor_diagnostics import (
    DEFAULT_FACTOR_KEYS,
    FACTOR_SPECS,
    evaluate_factor_discovery,
    evaluate_forward_confirmation,
    frozen_spec_hash,
    load_auction_imbalances,
    load_cost_model,
    load_dataset_candles,
    load_microstructure,
    persist_factor_run,
)
from app.services.intraday_research_integrity import exchange_session_date, rows_after_session
from app.services.intraday_research_data import research_data_readiness


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


def discover(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeframe != "30m":
        raise ValueError("Executable intraday factor research is restricted to 30m.")
    keys = _factor_keys(args.factors)
    with connect() as conn:
        dataset_id = _dataset_id(conn, args.dataset_id)
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
        result = evaluate_factor_discovery(
            candles,
            timeframe=args.timeframe,
            factor_keys=keys,
            cost_model=cost_model,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
            institutional_data_readiness=institutional_readiness,
        )
        spec_hash = frozen_spec_hash(
            factor_keys=keys,
            timeframe=args.timeframe,
            cost_model=cost_model,
        )
        result["dataset_id"] = dataset_id
        result["symbols"] = sorted(candles)
        result["frozen_spec_hash"] = spec_hash
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
        result = evaluate_forward_confirmation(
            forward,
            timeframe=str(source["timeframe"]),
            factor_keys=keys,
            cost_model=cost_model,
            microstructure_by_symbol=microstructure or None,
            auction_by_symbol=auctions or None,
            institutional_data_readiness=institutional_readiness,
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
    discovery = commands.add_parser("discover")
    discovery.add_argument("--dataset-id", type=int)
    discovery.add_argument("--timeframe", choices=("30m",), default="30m")
    discovery.add_argument("--symbols")
    discovery.add_argument("--max-symbols", type=int, default=200)
    discovery.add_argument("--factors")
    discovery.add_argument(
        "--cost-calibration-id",
        help="Calibration integer id, or 'latest'. Omit to retain the conservative 30bps baseline.",
    )

    confirmation = commands.add_parser("confirm")
    confirmation.add_argument("--source-run-id", type=int, required=True)
    confirmation.add_argument("--dataset-id", type=int)
    confirmation.add_argument("--max-symbols", type=int, default=200)
    return root


def main() -> None:
    args = parser().parse_args()
    print(
        "Intraday factor research | backend only | confirmation locked from discovery",
        flush=True,
    )
    result = discover(args) if args.command == "discover" else confirm(args)
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
