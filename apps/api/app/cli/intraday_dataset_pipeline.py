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
from datetime import date, datetime
from typing import Any

from app.cli._refusal import run_command
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
from app.services.intraday_trade_flow_ingest import MAX_SESSIONS_PER_RUN
from app.services.intraday_universe import UniverseRule, build_point_in_time_universe

INGEST_TIMEFRAMES = ("1m", "15m", "30m")
RESEARCH_TIMEFRAMES = ("15m", "30m")


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


def features(args: argparse.Namespace) -> dict[str, Any]:
    """Compute features only, so a large universe can be split across workers."""
    from app.services.labs.intraday.features import backfill_intraday_features

    source = feed_source(args.feed)
    with connect() as conn:
        symbols = _universe_members(conn, args) if args.from_universe else _symbols(args.symbols)
        return backfill_intraday_features(
            conn,
            symbols,
            (args.timeframe,),
            candle_limit=args.candle_limit,
            source=source,
        )


def _universe_members(conn: Any, args: argparse.Namespace) -> list[str]:
    if not args.universe_key:
        raise ValueError("--from-universe requires --universe-key")
    rows = conn.execute(
        """
        SELECT DISTINCT symbol
        FROM research_point_in_time_universe_membership
        WHERE universe_key = %s
        ORDER BY symbol
        """,
        (args.universe_key,),
    ).fetchall()
    symbols = [str(row["symbol"]) for row in rows]
    if not symbols:
        raise ValueError(f"Universe {args.universe_key!r} has no members.")
    return symbols


def snapshot(args: argparse.Namespace) -> dict[str, Any]:
    from app.services.labs.intraday.dataset_snapshot import record_intraday_dataset_snapshot
    from app.services.labs.intraday.features import backfill_intraday_features

    window_end = (
        datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
    )
    source = feed_source(args.feed)
    with connect() as conn:
        # The snapshot's assets are the universe's members, not the candidate
        # pool it was chosen from. A pool symbol that never qualified has no
        # rows under the membership filter, and asking for it would abort the
        # snapshot over a symbol that was correctly excluded.
        if args.from_universe:
            symbols = _universe_members(conn, args)
            print(f"universe {args.universe_key}: {len(symbols)} members", flush=True)
        else:
            symbols = _symbols(args.symbols)
        if args.skip_features:
            # Features were computed by a separate parallel pass. The snapshot
            # still refuses to materialize a symbol with no feature rows, so a
            # missed symbol fails loudly rather than being snapshotted empty.
            print("session-aware features: skipped (computed separately)", flush=True)
            features = {"skipped": True}
        else:
            print(f"session-aware features: backfilling from {source}", flush=True)
            features = backfill_intraday_features(
                conn,
                symbols,
                (args.timeframe,),
                candle_limit=args.candle_limit,
                source=source,
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
            source=source,
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


def premarket(args: argparse.Namespace) -> dict[str, Any]:
    """Premarket price discovery from bars already ingested."""
    from app.services.intraday_premarket import build_premarket_dataset

    with connect() as conn:
        symbols = _universe_members(conn, args) if args.from_universe else _symbols(args.symbols)
        print(f"premarket: {len(symbols)} symbols", flush=True)
        return build_premarket_dataset(
            conn,
            symbols=symbols,
            timeframe=args.timeframe,
            source=feed_source(args.feed),
        )


def trade_flow(args: argparse.Namespace) -> dict[str, Any]:
    """Bounded, checkpointed signed-trade-flow ingestion."""
    from app.services.intraday_trade_flow_ingest import ingest_trade_flow

    with connect() as conn:
        symbols = _universe_members(conn, args) if args.from_universe else _symbols(args.symbols)
        print(
            f"trade flow: {len(symbols)} symbols, {args.start} to {args.end}",
            flush=True,
        )
        return asyncio.run(
            ingest_trade_flow(
                conn,
                symbols=symbols,
                start=args.start,
                end=args.end,
                timeframe=args.timeframe,
                feed=args.feed,
                max_sessions=args.max_sessions,
                rate_limit_retries=args.rate_limit_retries,
                rate_limit_base_sleep=args.rate_limit_base_sleep,
                request_pause_seconds=args.request_pause_seconds,
            )
        )


def auto_trade_flow(args: argparse.Namespace) -> dict[str, Any]:
    """Run checkpointed trade-flow ingestion in bounded batches."""
    from app.services.intraday_trade_flow_ingest import ingest_trade_flow_auto

    def report(batch: dict[str, Any]) -> None:
        status = batch.get("status")
        status_text = f" {status}" if status else ""
        worker = batch.get("worker")
        worker_text = f" worker={worker}" if worker else ""
        error = batch.get("error")
        error_text = f" error={error}" if error else ""
        print(
            "auto trade flow: "
            f"{batch['completed_symbol_sessions']}/{batch['target_completed']} "
            f"batch {batch['batch']}/{batch['planned_batches']}{status_text}{worker_text} "
            f"{batch['session']} symbols={len(batch['symbols'])}{error_text}",
            flush=True,
        )

    with connect() as conn:
        symbols = _universe_members(conn, args) if args.from_universe else _symbols(args.symbols)
        print(
            f"auto trade flow: {len(symbols)} symbols, {args.start} to {args.end}, "
            f"target={args.target_completed or 'all'}, workers={args.parallel_workers}",
            flush=True,
        )
        return asyncio.run(
            ingest_trade_flow_auto(
                conn,
                symbols=symbols,
                start=args.start,
                end=args.end,
                timeframe=args.timeframe,
                feed=args.feed,
                target_completed=args.target_completed,
                max_batches=args.max_batches,
                max_sessions=args.max_sessions,
                parallel_workers=args.parallel_workers,
                rate_limit_retries=args.rate_limit_retries,
                rate_limit_base_sleep=args.rate_limit_base_sleep,
                request_pause_seconds=args.request_pause_seconds,
                progress=report,
            )
        )


def flow_agreement(args: argparse.Namespace) -> dict[str, Any]:
    """Measure the cheap classifier against Lee-Ready before trusting it."""
    from app.services.intraday_trade_flow_ingest import measure_classifier_agreement

    return asyncio.run(
        measure_classifier_agreement(
            symbol=args.symbol,
            session=args.session,
            feed=args.feed,
            window_minutes=args.window_minutes,
        )
    )


def sector_flow(args: argparse.Namespace) -> dict[str, Any]:
    """Report how much of a snapshot has a usable sector peer group.

    Answered in the database. Loading a universe-scale snapshot into Python to
    count sectors costs gigabytes and answers a question SQL answers exactly --
    the same lesson the calendar audit already paid for.
    """
    from app.services.intraday_sector_flow import dataset_sector_coverage

    with connect() as conn:
        return dataset_sector_coverage(
            conn, dataset_id=args.dataset_id, timeframe=args.timeframe
        )


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
    ingest_command.add_argument("--timeframe", default="30m", choices=INGEST_TIMEFRAMES)
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
    universe_command.add_argument("--timeframe", default="30m", choices=RESEARCH_TIMEFRAMES)
    universe_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)

    snapshot_command = commands.add_parser(
        "snapshot", help="Backfill features and materialize an immutable snapshot."
    )
    snapshot_command.add_argument("--symbols")
    snapshot_command.add_argument(
        "--from-universe",
        action="store_true",
        help="Take the asset list from the universe's members instead of --symbols.",
    )
    snapshot_command.add_argument("--timeframe", default="30m", choices=RESEARCH_TIMEFRAMES)
    snapshot_command.add_argument("--universe-key")
    snapshot_command.add_argument("--as-of")
    snapshot_command.add_argument("--name")
    snapshot_command.add_argument("--candle-limit", type=int, default=200_000)
    snapshot_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)
    snapshot_command.add_argument(
        "--skip-features",
        action="store_true",
        help="Assume features were already computed by a separate parallel pass.",
    )

    quality_command = commands.add_parser(
        "quality", help="Data-quality checks and the gap-experiment power gate."
    )
    quality_command.add_argument("--dataset-id", type=int, required=True)
    quality_command.add_argument("--timeframe", default="30m", choices=RESEARCH_TIMEFRAMES)
    quality_command.add_argument("--universe-key")

    features_command = commands.add_parser(
        "features", help="Compute session-aware features for a symbol list."
    )
    features_command.add_argument("--symbols")
    features_command.add_argument("--from-universe", action="store_true")
    features_command.add_argument("--universe-key")
    features_command.add_argument("--timeframe", default="30m", choices=RESEARCH_TIMEFRAMES)
    features_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)
    features_command.add_argument("--candle-limit", type=int, default=200_000)

    premarket_command = commands.add_parser(
        "premarket",
        help="Premarket price discovery from the extended-hours bars already held.",
    )
    premarket_command.add_argument("--symbols")
    premarket_command.add_argument("--from-universe", action="store_true")
    premarket_command.add_argument("--universe-key")
    premarket_command.add_argument("--timeframe", default="30m", choices=RESEARCH_TIMEFRAMES)
    premarket_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)

    trade_flow_command = commands.add_parser(
        "trade-flow", help="Bounded, checkpointed signed-trade-flow ingestion."
    )
    trade_flow_command.add_argument("--symbols")
    trade_flow_command.add_argument("--from-universe", action="store_true")
    trade_flow_command.add_argument("--universe-key")
    trade_flow_command.add_argument("--start", type=_date, required=True)
    trade_flow_command.add_argument("--end", type=_date, required=True)
    trade_flow_command.add_argument("--timeframe", default="30m", choices=INGEST_TIMEFRAMES)
    trade_flow_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)
    trade_flow_command.add_argument(
        "--max-sessions",
        type=int,
        default=MAX_SESSIONS_PER_RUN,
        help="Refuse to start beyond this many pending symbol-sessions.",
    )
    trade_flow_command.add_argument("--rate-limit-retries", type=int, default=20)
    trade_flow_command.add_argument("--rate-limit-base-sleep", type=float, default=60.0)
    trade_flow_command.add_argument("--request-pause-seconds", type=float, default=2.0)

    auto_trade_flow_command = commands.add_parser(
        "auto-trade-flow",
        help="Automatically run bounded trade-flow batches until a target is reached.",
    )
    auto_trade_flow_command.add_argument("--symbols")
    auto_trade_flow_command.add_argument("--from-universe", action="store_true")
    auto_trade_flow_command.add_argument("--universe-key")
    auto_trade_flow_command.add_argument("--start", type=_date, required=True)
    auto_trade_flow_command.add_argument("--end", type=_date, required=True)
    auto_trade_flow_command.add_argument("--timeframe", default="30m", choices=INGEST_TIMEFRAMES)
    auto_trade_flow_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)
    auto_trade_flow_command.add_argument(
        "--target-completed",
        type=int,
        help="Stop once this many completed symbol-sessions exist in the window.",
    )
    auto_trade_flow_command.add_argument(
        "--max-batches",
        type=int,
        help="Optional safety stop after this many bounded batches.",
    )
    auto_trade_flow_command.add_argument(
        "--max-sessions",
        type=int,
        default=MAX_SESSIONS_PER_RUN,
        help="Maximum pending symbol-sessions per inner ingestion batch.",
    )
    auto_trade_flow_command.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help=(
            "Run this many bounded ingestion batches at the same time. "
            "Use cautiously because each worker can process a high-volume symbol-session."
        ),
    )
    auto_trade_flow_command.add_argument("--rate-limit-retries", type=int, default=20)
    auto_trade_flow_command.add_argument("--rate-limit-base-sleep", type=float, default=60.0)
    auto_trade_flow_command.add_argument("--request-pause-seconds", type=float, default=2.0)

    agreement_command = commands.add_parser(
        "flow-agreement",
        help="Measure the tick rule against Lee-Ready on one bounded window.",
    )
    agreement_command.add_argument("--symbol", required=True)
    agreement_command.add_argument("--session", type=_date, required=True)
    agreement_command.add_argument("--feed", default="sip", choices=RESEARCH_FEEDS)
    agreement_command.add_argument("--window-minutes", type=int, default=30)

    sector_flow_command = commands.add_parser(
        "sector-flow", help="Sector peer-group coverage for a snapshotted dataset."
    )
    sector_flow_command.add_argument("--dataset-id", type=int, required=True)
    sector_flow_command.add_argument("--timeframe", default="30m", choices=RESEARCH_TIMEFRAMES)

    return root


COMMANDS = {
    "ingest": ingest,
    "features": features,
    "universe": universe,
    "snapshot": snapshot,
    "quality": quality,
    "premarket": premarket,
    "trade-flow": trade_flow,
    "auto-trade-flow": auto_trade_flow,
    "flow-agreement": flow_agreement,
    "sector-flow": sector_flow,
}


def main() -> None:
    args = parser().parse_args()
    run_command(
        COMMANDS[args.command],
        args,
        banner="Intraday dataset pipeline | backend only | research use",
    )


if __name__ == "__main__":
    main()
