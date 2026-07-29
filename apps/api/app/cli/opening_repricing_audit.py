from __future__ import annotations

import argparse
import json
from typing import Any

from app.db import connect
from app.services.signal_diagnostics import run_signal_diagnostics

ARCHITECTURE = "opening_repricing_flow_v1"


def _bounded_count(value: str, *, name: str, maximum: int) -> int:
    parsed = int(value)
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be between 1 and {maximum}")
    return parsed


def _symbols(value: str | None) -> list[str] | None:
    if value is None:
        return None
    symbols = [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
    if not symbols:
        raise ValueError("--symbols must contain at least one symbol")
    return symbols


def _family_progress(progress: dict[str, Any]) -> None:
    current = progress.get("current") or "complete"
    print(
        f"family {progress['completed']}/{progress['total']} | {current}",
        flush=True,
    )


def _work_progress(progress: dict[str, Any]) -> None:
    print(
        (
            f"work {progress['completed']}/{progress['total']} | "
            f"variant {progress['variant']}/{progress['variants']} | "
            f"symbol {progress['symbol_index']}/{progress['symbols']} "
            f"{progress['symbol']}"
        ),
        flush=True,
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return run_signal_diagnostics(
            conn,
            timeframe="30m",
            dataset_id=args.dataset_id,
            symbols=_symbols(args.symbols),
            architectures=(ARCHITECTURE,),
            max_variants=args.max_variants,
            max_symbols=args.max_symbols,
            persist=not args.no_persist,
            progress_callback=_family_progress,
            work_progress_callback=_work_progress,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Run the 30m Opening Repricing Flow raw-signal audit directly on "
            "the VPS. Research only; no campaign or broker action is reachable."
        )
    )
    root.add_argument("--dataset-id", type=int, default=None, help="Default: latest intraday dataset snapshot")
    root.add_argument("--symbols", default=None, help="Optional comma-separated symbol override")
    root.add_argument(
        "--max-symbols",
        type=lambda value: _bounded_count(value, name="max symbols", maximum=100),
        default=10,
    )
    root.add_argument(
        "--max-variants",
        type=lambda value: _bounded_count(value, name="max variants", maximum=8),
        default=8,
    )
    root.add_argument("--no-persist", action="store_true", help="Measure without storing the verdict")
    return root


def main() -> None:
    print(
        "Opening Repricing Flow v1 | 30m raw-signal audit | "
        "research only, no campaign and no broker action",
        flush=True,
    )
    result = execute(parser().parse_args())
    print(
        json.dumps(
            {
                "timeframe": result["timeframe"],
                "dataset_id": result["dataset_id"],
                "symbols": result["symbols"],
                "verdict_counts": result["verdict_counts"],
                "predictive_families": result["predictive_families"],
                "signal_below_cost_families": result["signal_below_cost_families"],
                "round_trip_cost_bps": result["round_trip_cost_bps"],
                "recommendation": result["recommendation"],
                "families": result["families"],
            },
            default=str,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
