"""Backend-only Alpaca options ingestion and point-in-time feature tooling."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.intraday_options import (
    ingest_option_chains,
    materialize_option_features_for_dataset,
    option_coverage,
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


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def chain_ingest(args: argparse.Namespace) -> dict[str, Any]:
    def progress(row: dict[str, Any]) -> None:
        print(
            "options chain ingest: "
            f"symbol={row['symbol']} page={row['page']} received={row['received']} "
            f"seen={row['contracts_seen']} upserted={row['contracts_upserted']}",
            flush=True,
        )

    with connect() as conn:
        return asyncio.run(
            ingest_option_chains(
                conn,
                symbols=args.symbols,
                feed=args.feed,
                observed_at=args.observed_at,
                max_pages=args.max_pages,
                request_pause_seconds=args.request_pause_seconds,
                progress=progress if args.verbose else None,
                expiration_date_gte=args.expiration_date_gte,
                expiration_date_lte=args.expiration_date_lte,
                strike_price_gte=args.strike_price_gte,
                strike_price_lte=args.strike_price_lte,
                option_type=args.type,
            )
        )


def coverage(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return option_coverage(
            conn,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
            feed=args.feed,
        )


def features(args: argparse.Namespace) -> dict[str, Any]:
    with connect() as conn:
        return materialize_option_features_for_dataset(
            conn,
            dataset_id=args.dataset_id,
            timeframe=args.timeframe,
            symbols=args.symbols,
            feed=args.feed,
            limit=args.limit,
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Alpaca Options | point-in-time research side channel | backend only"
    )
    commands = root.add_subparsers(dest="command", required=True)

    ingest_command = commands.add_parser("chain-ingest", help="Fetch latest Alpaca option-chain snapshots.")
    ingest_command.add_argument("--symbols", type=_symbols, required=True)
    ingest_command.add_argument("--feed", choices=("opra", "indicative"), default="opra")
    ingest_command.add_argument("--observed-at", type=_timestamp)
    ingest_command.add_argument("--max-pages", type=int, default=100)
    ingest_command.add_argument("--request-pause-seconds", type=float, default=0.0)
    ingest_command.add_argument("--expiration-date-gte", type=_date)
    ingest_command.add_argument("--expiration-date-lte", type=_date)
    ingest_command.add_argument("--strike-price-gte", type=float)
    ingest_command.add_argument("--strike-price-lte", type=float)
    ingest_command.add_argument("--type", choices=("call", "put"))
    ingest_command.add_argument("--verbose", action="store_true")
    ingest_command.set_defaults(handler=chain_ingest)

    coverage_command = commands.add_parser("coverage", help="Summarize ingested option-chain coverage.")
    coverage_command.add_argument("--symbols", type=_symbols, required=True)
    coverage_command.add_argument("--start", type=_timestamp, required=True)
    coverage_command.add_argument("--end", type=_timestamp, required=True)
    coverage_command.add_argument("--feed", choices=("opra", "indicative"), default="opra")
    coverage_command.set_defaults(handler=coverage)

    features_command = commands.add_parser(
        "features",
        help="Materialize point-in-time option feature snapshots for a dataset/timeframe.",
    )
    features_command.add_argument("--dataset-id", type=int, required=True)
    features_command.add_argument("--timeframe", choices=("1m", "15m", "30m"), required=True)
    features_command.add_argument("--symbols", type=_symbols)
    features_command.add_argument("--feed", choices=("opra", "indicative"), default="opra")
    features_command.add_argument("--limit", type=int)
    features_command.set_defaults(handler=features)
    return root


def main() -> None:
    args = parser().parse_args()
    run_command(
        args.handler,
        args,
        banner="Alpaca Options | point-in-time research | backend only",
    )


if __name__ == "__main__":
    main()
