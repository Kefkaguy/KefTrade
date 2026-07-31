"""VPS CLI for the institutional 30m dataset pipeline.

Drives the immediate implementation order: ingest a date range resumably,
construct point-in-time universe membership, build the immutable snapshot,
then run the quality and power checks that must clear before any factor is
calculated.

Research only. No campaign, broker, order-submission or UI action.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from typing import Any

from app.db import connect
from app.services.intraday_candle_ingest import (
    RESEARCH_FEEDS,
    feed_source,
    ingest_universe_range,
    reconcile_universe,
)
from app.services.intraday_dataset_quality import (
    dataset_quality_report,
    persist_quality_report,
)
from app.services.intraday_universe import UniverseRule, build_point_in_time_universe


def _symbols(value: str) -> list[str]:
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(items))


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def ingest(args: argparse.Namespace) -> dict[str, Any]:
    symbols = _symbols(args.symbols)

    def report(result: Any) -> None:
        print(
            f"  {result.symbol} {result.chunk_start} {result.status} "
            f"bars={result.bars_received}",
            flush=True,
        )

    with connect() as conn:
        summary = asyncio.run(
            ingest_universe_range(
                conn,
                symbols=symbols,
                timeframe=args.timeframe,
                start=args.start,
                end=args.end,
                feed=args.feed,
                resume=not args.no_resume,
                progress=report if args.verbose else None,
            )
        )
        summary["session_reconciliation"] = reconcile_universe(
            conn,
            symbols=symbols,
            timeframe=args.timeframe,
            source=feed_source(args.feed),
            start=args.start,
            end=args.end,
        )
    return summary


def universe(args: argparse.Namespace) -> dict[str, Any]:
    rule = UniverseRule(
        universe_key=args.universe_key,
        target_size=args.target_size,
        rebalance_months=args.rebalance_months,
        rank_lookback_sessions=args.rank_lookback_sessions,
        minimum_median_dollar_volume=args.minimum_dollar_volume,
        timeframe=args.timeframe,
        source=feed_source(args.feed),
    )
    with connect() as conn:
        return build_point_in_time_universe(
            conn,
            rule=rule,
            candidate_symbols=_symbols(args.symbols),
            start=args.start,
            end=args.end,
        )


def snapshot(args: argparse.Namespace) -> dict[str, Any]:
    from app.services.labs.intraday.dataset_snapshot import record_intraday_dataset_snapshot
    from app.services.labs.intraday.features import backfill_intraday_features

    symbols = _symbols(args.symbols)
    window_end = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
    )
    with connect() as conn:
        print("session-aware features: backfilling", flush=True)
        features = backfill_intraday_features(
            conn, symbols, (args.timeframe,), candle_limit=args.candle_limit
        )
        print("snapshot: materializing", flush=True)
        manifest = record_intraday_dataset_snapshot(
            conn,
            assets=symbols,
            timeframes=[args.timeframe],
            mode="reproducibility" if args.as_of else "rolling",
            name=args.name,
            universe_key=args.universe_key,
            window_end=window_end,
        )
    return {"features": features, "dataset": manifest}


def quality(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        report = dataset_quality_report(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            universe_key=args.universe_key,
        )
        report["quality_report_id"] = persist_quality_report(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            report=report,
        )
    return report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Institutional 30m dataset pipeline: resumable range ingestion, "
            "point-in-time universe, immutable snapshot, quality and power gate."
        )
    )
    commands = root.add_subparsers(dest="command", required=True)

    ingest_command = commands.add_parser(
        "ingest", help="Resumable date-range candle ingestion."
    )
    ingest_command.add_argument("--symbols", required=True)
    ingest_command.add_argument("--timeframe", default="30m", choices=("15m", "30m"))
    ingest_command.add_argument("--start", type=_date, required=True)
    ingest_command.add_argument("--end", type=_date, required=True)
    ingest_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)
    ingest_command.add_argument("--no-resume", action="store_true")
    ingest_command.add_argument("--verbose", action="store_true")

    universe_command = commands.add_parser(
        "universe", help="Build point-in-time liquid-universe membership."
    )
    universe_command.add_argument("--universe-key", required=True)
    universe_command.add_argument("--symbols", required=True)
    universe_command.add_argument("--start", type=_date, required=True)
    universe_command.add_argument("--end", type=_date, required=True)
    universe_command.add_argument("--target-size", type=int, default=100)
    universe_command.add_argument("--rebalance-months", type=int, default=3)
    universe_command.add_argument("--rank-lookback-sessions", type=int, default=60)
    universe_command.add_argument("--minimum-dollar-volume", type=float, default=20_000_000.0)
    universe_command.add_argument("--timeframe", default="30m", choices=("15m", "30m"))
    universe_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)

    snapshot_command = commands.add_parser(
        "snapshot", help="Backfill features and materialize an immutable snapshot."
    )
    snapshot_command.add_argument("--symbols", required=True)
    snapshot_command.add_argument("--timeframe", default="30m", choices=("15m", "30m"))
    snapshot_command.add_argument("--universe-key")
    snapshot_command.add_argument("--as-of")
    snapshot_command.add_argument("--name")
    snapshot_command.add_argument("--candle-limit", type=int, default=200_000)

    quality_command = commands.add_parser(
        "quality", help="Data-quality checks and the gap-experiment power gate."
    )
    quality_command.add_argument("--dataset-id", type=int, required=True)
    quality_command.add_argument("--timeframe", default="30m", choices=("15m", "30m"))
    quality_command.add_argument("--universe-key")

    return root


COMMANDS = {
    "ingest": ingest,
    "universe": universe,
    "snapshot": snapshot,
    "quality": quality,
}


def main() -> None:
    args = parser().parse_args()
    print("Intraday dataset pipeline | backend only | research use", flush=True)
    print(json.dumps(COMMANDS[args.command](args), default=str, indent=2))


if __name__ == "__main__":
    main()
