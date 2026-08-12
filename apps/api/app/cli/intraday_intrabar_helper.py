"""Backend-only 1m microscope diagnostics for higher-timeframe setups."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_candle_ingest import feed_source
from app.services.intraday_intrabar_helper import (
    intrabar_data_coverage,
    run_intrabar_diagnostics,
)


DEFAULT_FACTORS = (
    "gap_down_absorption_reversal_2bar,"
    "gap_down_absorption_reversal,"
    "gap_up_absorption_reversal_2bar,"
    "first_to_last_half_hour_market_reversal,"
    "gap_up_acceptance_continuation"
)


def _symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(symbols))


def _factors(value: str) -> list[str]:
    factors = [item.strip() for item in value.split(",") if item.strip()]
    if not factors:
        raise argparse.ArgumentTypeError("at least one factor is required")
    return list(dict.fromkeys(factors))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def coverage(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return intrabar_data_coverage(
            conn,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
            timeframe=args.intrabar_timeframe,
            source=feed_source(args.feed),
            feed=args.feed,
        )


def diagnose(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return run_intrabar_diagnostics(
            conn,
            dataset_id=args.dataset_id,
            parent_timeframe=args.parent_timeframe,
            factor_keys=args.factors,
            intrabar_timeframe=args.intrabar_timeframe,
            source=feed_source(args.feed),
            feed=args.feed,
            start=args.start,
            end=args.end,
            parent_lookback_days=args.parent_lookback_days,
            max_events_per_factor=args.max_events_per_factor,
            persist=not args.no_persist,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "1m helper diagnostics for 15m/30m setups. Diagnostic only: no "
            "campaign, broker, confirmation, or UI action."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    coverage_command = commands.add_parser(
        "coverage",
        help="Check whether 1m candles and 1m trade-flow exist for a window.",
    )
    coverage_command.add_argument("--symbols", required=True, type=_symbols)
    coverage_command.add_argument("--start", required=True, type=_timestamp)
    coverage_command.add_argument("--end", required=True, type=_timestamp)
    coverage_command.add_argument("--intrabar-timeframe", default="1m", choices=("1m",))
    coverage_command.add_argument("--feed", default="sip", choices=("sip", "iex"))

    diagnose_command = commands.add_parser(
        "diagnose",
        help=(
            "Recompute higher-timeframe events and attach 1m intrabar "
            "confirmation diagnostics."
        ),
    )
    diagnose_command.add_argument("--dataset-id", type=int, required=True)
    diagnose_command.add_argument(
        "--parent-timeframe", default="30m", choices=("15m", "30m")
    )
    diagnose_command.add_argument("--intrabar-timeframe", default="1m", choices=("1m",))
    diagnose_command.add_argument("--feed", default="sip", choices=("sip", "iex"))
    diagnose_command.add_argument(
        "--start",
        type=_timestamp,
        help=(
            "Optional 1m diagnostic window start. If omitted, the command uses "
            "the overlapping available 1m candle/trade-flow window."
        ),
    )
    diagnose_command.add_argument(
        "--end",
        type=_timestamp,
        help=(
            "Optional 1m diagnostic window end. If omitted, the command uses "
            "the overlapping available 1m candle/trade-flow window."
        ),
    )
    diagnose_command.add_argument(
        "--parent-lookback-days",
        type=int,
        default=10,
        help=(
            "Extra higher-timeframe candle history to scan before the 1m window "
            "so gap/session-dependent factors have prior context."
        ),
    )
    diagnose_command.add_argument(
        "--factors",
        type=_factors,
        default=_factors(DEFAULT_FACTORS),
    )
    diagnose_command.add_argument(
        "--max-events-per-factor",
        type=int,
        default=500,
        help=(
            "Bound output size by scoring only the latest N events per factor. "
            "Use 0 for no event cap."
        ),
    )
    diagnose_command.add_argument("--no-persist", action="store_true")
    return root


COMMANDS = {
    "coverage": coverage,
    "diagnose": diagnose,
}


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "max_events_per_factor", None) == 0:
        args.max_events_per_factor = None
    run_command(
        COMMANDS[args.command],
        args,
        banner="Intraday intrabar helper | 1m microscope | backend only",
    )


if __name__ == "__main__":
    main()
