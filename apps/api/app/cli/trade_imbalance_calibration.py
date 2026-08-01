"""VPS CLI for return-blind signed-trade-imbalance calibration.

The calibration commands read trade-flow predictor features only.  They cannot
run discovery, simulation, paper trading, qualification, or broker actions.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_factor_diagnostics import FACTOR_DIAGNOSTICS_VERSION
from app.services.intraday_hypotheses import (
    TRADE_IMBALANCE_V2_EXPERIMENT_KEY,
    order_flow_required_event_count,
    persist_hypotheses,
    trade_imbalance_v2_hypotheses,
)
from app.services.intraday_trade_imbalance_calibration import (
    DEFAULT_SPEC,
    calibrate_predictor_distribution,
    load_calibration,
    load_source_rows,
    persist_calibration,
)
from app.services.intraday_trial_ledger import declare_trials


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _symbols(value: str) -> list[str]:
    symbols = list(dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return symbols


def _measure(args: argparse.Namespace, *, persist: bool) -> dict[str, Any]:
    with connect() as conn:
        rows = load_source_rows(
            conn,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
            timeframe=args.timeframe,
            feed=args.feed,
        )
        report, eligible = calibrate_predictor_distribution(rows, spec=DEFAULT_SPEC)
        if persist and eligible:
            report["calibration_id"] = persist_calibration(
                conn,
                report=report,
                rows=eligible,
                timeframe=args.timeframe,
                feed=args.feed,
            )
        return report


def status(args: argparse.Namespace) -> dict[str, Any]:
    """Readiness plus distribution calibration; never persists or reads returns."""
    return _measure(args, persist=False)


def calibrate(args: argparse.Namespace) -> dict[str, Any]:
    return _measure(args, persist=True)


def show(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        row = load_calibration(conn, args.calibration_id, require_ready=False)
        return {
            "id": int(row["id"]),
            "ready_for_declaration": bool(row["ready_for_declaration"]),
            "timeframe": row["timeframe"],
            "feed": row["feed"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "dataset_hash": row["dataset_hash"],
            "specification_hash": row["specification_hash"],
            "report": row["report"],
        }


def declare_v2(args: argparse.Namespace) -> dict[str, Any]:
    """Create a new immutable hypothesis version after calibration passes."""
    with connect() as conn:
        calibration = load_calibration(conn, args.calibration_id, require_ready=True)
        manifest = conn.execute(
            "SELECT id, window_end FROM research_dataset_manifests WHERE id = %s",
            (args.dataset_id,),
        ).fetchone()
        if not manifest:
            raise ValueError(f"No immutable discovery dataset id={args.dataset_id}.")
        if manifest["window_end"] <= calibration["window_end"]:
            raise ValueError(
                "The discovery snapshot contains no observations after the "
                "calibration window. Collect later trade flow and create a later snapshot."
            )
        required = args.required_event_count or order_flow_required_event_count()
        hypotheses = trade_imbalance_v2_hypotheses(
            calibration=calibration,
            required_event_count=required,
        )
        stored = persist_hypotheses(
            conn,
            hypotheses,
            experiment_key=TRADE_IMBALANCE_V2_EXPERIMENT_KEY,
            timeframe="30m",
            dataset_id=args.dataset_id,
        )
        keys = [item.factor_key for item in hypotheses]
        declaration = declare_trials(
            conn,
            purpose=(
                f"{TRADE_IMBALANCE_V2_EXPERIMENT_KEY}: signed-flow only; "
                f"return-blind calibration {args.calibration_id}"
            ),
            timeframe="30m",
            factor_keys=keys,
            dataset_id=args.dataset_id,
            hypothesis=(
                "Sustained extreme signed trade imbalance from a same-day parent "
                "order continues over predeclared one- and two-bar horizons. "
                "Extreme is fixed exclusively from the predictor distribution."
            ),
            protocol_version=FACTOR_DIAGNOSTICS_VERSION,
        )
        return {
            "experiment_key": TRADE_IMBALANCE_V2_EXPERIMENT_KEY,
            "calibration_id": args.calibration_id,
            "dataset_id": args.dataset_id,
            "declaration_id": int(declaration["id"]),
            "already_declared": bool(declaration["already_declared"]),
            "factor_keys": keys,
            "required_event_count_per_horizon": required,
            "hypotheses": stored,
            "next_allowed_phase": "governed_discovery_only",
        }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Return-blind signed-trade-imbalance calibration; backend only."
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("status", "calibrate"):
        command = commands.add_parser(name)
        command.add_argument("--symbols", required=True, type=_symbols)
        command.add_argument("--start", required=True, type=_date)
        command.add_argument("--end", required=True, type=_date)
        command.add_argument("--timeframe", choices=("30m",), default="30m")
        command.add_argument("--feed", choices=("sip",), default="sip")

    show_command = commands.add_parser("show")
    show_command.add_argument("--calibration-id", type=int, required=True)

    declaration = commands.add_parser("declare-v2")
    declaration.add_argument("--calibration-id", type=int, required=True)
    declaration.add_argument("--dataset-id", type=int, required=True)
    declaration.add_argument("--required-event-count", type=int)
    return root


COMMANDS = {
    "status": status,
    "calibrate": calibrate,
    "show": show,
    "declare-v2": declare_v2,
}


def main() -> None:
    args = parser().parse_args()
    run_command(
        COMMANDS[args.command],
        args,
        banner="Signed trade-imbalance calibration | return-blind | backend only",
    )


if __name__ == "__main__":
    main()
