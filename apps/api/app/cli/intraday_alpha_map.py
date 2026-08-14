"""Backend-only CLI for pre-strategy alpha cartography.

Nothing here constructs a strategy, opens a campaign, or touches a broker. The
commands declare a measurement grid, measure it once, and print what survived.
"""

from __future__ import annotations

import argparse
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_alpha_map import (
    DEFAULT_COST_SAFETY_MULTIPLE,
    DEFAULT_HORIZONS_SECONDS,
    PANEL_FEATURES,
    SUB_MINUTE_HORIZONS_SECONDS,
    TRANSFORMS,
    alpha_map_report,
    declare_alpha_map,
    feature_horizon_profile,
    run_alpha_map,
)


def _csv(value: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return list(dict.fromkeys(values))


def _symbols(value: str) -> list[str]:
    return [item.upper() for item in _csv(value)]


def _seconds(value: str) -> list[int]:
    try:
        values = [int(item) for item in _csv(value)]
    except ValueError as error:
        raise argparse.ArgumentTypeError("horizons must be whole seconds") from error
    if any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("horizons must be positive")
    return sorted(set(values))


def declare(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return declare_alpha_map(
            conn,
            dataset_id=args.dataset_id,
            signal_timeframe=args.signal_timeframe,
            grid_timeframe=args.grid_timeframe,
            symbols=args.symbols,
            features=args.features or PANEL_FEATURES,
            horizons_seconds=args.horizons_seconds or DEFAULT_HORIZONS_SECONDS,
            transforms=args.transforms or TRANSFORMS,
            slices=args.slices or ("all",),
            cost_calibration_id=args.cost_calibration_id,
            cost_safety_multiple=args.cost_safety_multiple,
            feed=args.feed,
            source=args.source,
        )


def measure(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        result = run_alpha_map(conn, declaration_id=args.declaration_id, phase=args.phase)
    if args.full:
        return result
    # The full payload carries every bucket table for every cell, which is the
    # right thing to persist and the wrong thing to print. The summary keeps
    # the part a person acts on.
    return {
        "run_id": result["run_id"],
        "phase": result["phase"],
        "observations": result["observations"],
        "effective_trials": result["effective_trials"],
        "verdict_counts": result["verdict_counts"],
        "horizons": result["horizons"],
        "horizon_cost_feasibility": result["horizon_cost_feasibility"],
        "probability_of_backtest_overfitting": result["probability_of_backtest_overfitting"],
        "cross_sectional_dependence": result["cross_sectional_dependence"],
        "kill_summary": result["kill_summary"],
        "survivors": result["survivors"],
        "strategy_construction_authorized": result["strategy_construction_authorized"],
        "panel": result["panel"],
    }


def report(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return alpha_map_report(conn, run_id=args.run_id)


def profile(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return feature_horizon_profile(conn, feature=args.feature, transform=args.transform)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Alpha cartography: measure where a feature's information lives before "
            "any strategy is built from it. Produces forward-return profiles across a "
            "horizon ladder, per-symbol/time-of-day normalizations, market/sector "
            "residuals, cost hurdles and selection-adjusted verdicts. Creates no "
            "campaign, candidate, order or position."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    declare_command = commands.add_parser(
        "declare",
        help="Freeze the feature x transform x horizon x slice grid before measuring it.",
    )
    declare_command.add_argument("--dataset-id", type=int, required=True)
    declare_command.add_argument("--signal-timeframe", choices=("15m", "30m"), default="30m")
    declare_command.add_argument(
        "--grid-timeframe",
        choices=("1m", "5m"),
        default="1m",
        help="Forward-return measurement grid. Must be finer than the signal timeframe.",
    )
    declare_command.add_argument("--symbols", type=_symbols, required=True)
    declare_command.add_argument(
        "--features",
        type=_csv,
        help=f"Defaults to: {','.join(PANEL_FEATURES)}",
    )
    declare_command.add_argument(
        "--horizons-seconds",
        type=_seconds,
        help=(
            "Defaults to "
            f"{','.join(str(item) for item in DEFAULT_HORIZONS_SECONDS)}. "
            f"Sub-minute rungs ({','.join(str(item) for item in SUB_MINUTE_HORIZONS_SECONDS)}) "
            "are accepted and will be reported as unavailable until a sub-minute bar "
            "grid is ingested."
        ),
    )
    declare_command.add_argument(
        "--transforms",
        type=_csv,
        help=f"Defaults to: {','.join(TRANSFORMS)}",
    )
    declare_command.add_argument(
        "--slices",
        type=_csv,
        help="all, symbol, time_of_day, session_half. Defaults to all. Every slice is a trial.",
    )
    declare_command.add_argument("--cost-calibration-id", type=int)
    declare_command.add_argument(
        "--cost-safety-multiple",
        type=float,
        default=DEFAULT_COST_SAFETY_MULTIPLE,
        help=(
            "Required gross edge is round-trip cost times this. Break-even is not a "
            "hurdle: the cost model has more estimation error than that."
        ),
    )
    declare_command.add_argument("--feed", choices=("sip", "iex"), default="sip")
    declare_command.add_argument("--source", default="alpaca")
    declare_command.set_defaults(handler=declare)

    measure_command = commands.add_parser(
        "measure",
        help="Measure a declared grid exactly once against one split phase.",
    )
    measure_command.add_argument("--declaration-id", type=int, required=True)
    measure_command.add_argument(
        "--phase",
        choices=("discovery", "validation", "confirmation"),
        default="discovery",
    )
    measure_command.add_argument(
        "--full",
        action="store_true",
        help="Print every cell's bucket table rather than the acted-on summary.",
    )
    measure_command.set_defaults(handler=measure)

    report_command = commands.add_parser("report", help="Print a persisted run and its cells.")
    report_command.add_argument("--run-id", type=int, required=True)
    report_command.set_defaults(handler=report)

    profile_command = commands.add_parser(
        "profile",
        help="Every horizon one feature has ever been measured at, across all runs.",
    )
    profile_command.add_argument("--feature", required=True)
    profile_command.add_argument("--transform", choices=TRANSFORMS)
    profile_command.set_defaults(handler=profile)
    return root


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "transforms", None):
        unknown = sorted(set(args.transforms) - set(TRANSFORMS))
        if unknown:
            parser().error(f"unknown transforms: {unknown}")
    run_command(
        args.handler,
        args,
        banner="Intraday alpha cartography | pre-strategy measurement | backend only",
    )


if __name__ == "__main__":
    main()
