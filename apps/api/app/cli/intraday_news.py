"""Backend-only Alpaca news ingestion and point-in-time feature tooling."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_news import (
    ingest_alpaca_news,
    materialize_news_features_for_dataset,
    news_coverage,
)


def _symbols(value: str) -> list[str]:
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(items))


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed


def ingest(args: argparse.Namespace) -> dict[str, Any]:
    def progress(row: dict[str, Any]) -> None:
        print(
            "news ingest: "
            f"page={row['page']} received={row['received']} "
            f"seen={row['articles_seen']} upserted={row['symbol_versions_upserted']}",
            flush=True,
        )

    with connect() as conn:
        return asyncio.run(
            ingest_alpaca_news(
                conn,
                symbols=args.symbols,
                start=args.start,
                end=args.end,
                include_content=args.include_content,
                max_pages=args.max_pages,
                request_pause_seconds=args.request_pause_seconds,
                progress=progress if args.verbose else None,
            )
        )


def coverage(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return news_coverage(
            conn,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
        )


def features(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return materialize_news_features_for_dataset(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            symbols=args.symbols,
            limit=args.limit,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Alpaca News | point-in-time research side channel | backend only"
    )
    commands = root.add_subparsers(dest="command", required=True)

    ingest_command = commands.add_parser("ingest", help="Fetch historical Alpaca news.")
    ingest_command.add_argument("--symbols", type=_symbols, required=True)
    ingest_command.add_argument("--start", type=_timestamp, required=True)
    ingest_command.add_argument("--end", type=_timestamp, required=True)
    ingest_command.add_argument("--include-content", action="store_true")
    ingest_command.add_argument("--max-pages", type=int, default=10_000)
    ingest_command.add_argument("--request-pause-seconds", type=float, default=0.0)
    ingest_command.add_argument("--verbose", action="store_true")
    ingest_command.set_defaults(handler=ingest)

    coverage_command = commands.add_parser("coverage", help="Summarize ingested news coverage.")
    coverage_command.add_argument("--symbols", type=_symbols, required=True)
    coverage_command.add_argument("--start", type=_timestamp, required=True)
    coverage_command.add_argument("--end", type=_timestamp, required=True)
    coverage_command.set_defaults(handler=coverage)

    features_command = commands.add_parser(
        "features",
        help="Materialize point-in-time news feature snapshots for a dataset/timeframe.",
    )
    features_command.add_argument("--dataset-id", type=int, required=True)
    features_command.add_argument("--timeframe", choices=("1m", "15m", "30m"), required=True)
    features_command.add_argument("--symbols", type=_symbols)
    features_command.add_argument("--limit", type=int)
    features_command.set_defaults(handler=features)
    return root


def main() -> None:
    args = parser().parse_args()
    run_command(
        args.handler,
        args,
        banner="Alpaca News | point-in-time research | backend only",
    )


if __name__ == "__main__":
    main()
