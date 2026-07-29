"""VPS CLI for quote capture and observed execution-cost calibration."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

from app.db import connect
from app.providers.alpaca import fetch_stock_quotes, normalize_stock_quote
from app.services.intraday_execution_costs import (
    aggregate_microstructure_bars,
    calibrate_execution_costs,
    load_execution_evidence,
    persist_cost_calibration,
    persist_microstructure_bars,
    persist_quote_snapshots,
)


def _symbols(value: str) -> list[str]:
    symbols = sorted({item.strip().upper() for item in value.split(",") if item.strip()})
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return symbols


def _time(value: str | None, *, fallback: datetime) -> datetime:
    if not value:
        return fallback
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


async def sync_quotes(args: argparse.Namespace) -> dict:
    end = _time(args.end, fallback=datetime.now(tz=UTC))
    start = _time(args.start, fallback=end - timedelta(days=args.days))
    imported = 0
    microstructure = 0
    for symbol in args.symbols:
        print(f"quotes {symbol}: fetching {start.isoformat()} to {end.isoformat()}", flush=True)
        _, raw, _, _ = await fetch_stock_quotes(
            symbol,
            start=start,
            end=end,
            limit=args.max_quotes,
            feed=args.feed,
        )
        normalized = [
            quote
            for row in raw
            if (quote := normalize_stock_quote(symbol, row, feed=args.feed)) is not None
        ]
        with connect() as conn:
            imported += persist_quote_snapshots(conn, normalized)
            for timeframe in args.timeframes:
                microstructure += persist_microstructure_bars(
                    conn,
                    aggregate_microstructure_bars(normalized, timeframe=timeframe),
                )
        print(f"quotes {symbol}: stored {len(normalized)}", flush=True)
    return {
        "symbols": args.symbols,
        "window_start": start,
        "window_end": end,
        "quote_rows_stored": imported,
        "microstructure_rows_stored": microstructure,
        "feed": args.feed,
    }


def calibrate(args: argparse.Namespace) -> dict:
    end = _time(args.end, fallback=datetime.now(tz=UTC))
    start = _time(args.start, fallback=end - timedelta(days=args.days))
    with connect() as conn:
        quotes, fills = load_execution_evidence(
            conn,
            symbols=args.symbols,
            start=start,
            end=end,
        )
        result = calibrate_execution_costs(
            quotes,
            fills,
            regulatory_bps=args.regulatory_bps,
        )
        result["calibration_id"] = persist_cost_calibration(conn, result)
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Backend-only quote/fill transaction-cost research. Never submits orders."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync-quotes")
    sync.add_argument("--symbols", type=_symbols, required=True)
    sync.add_argument("--start")
    sync.add_argument("--end")
    sync.add_argument("--days", type=int, default=5)
    sync.add_argument("--max-quotes", type=int, default=100_000)
    sync.add_argument("--feed", default="iex")
    sync.add_argument("--timeframes", nargs="+", choices=("15m", "30m"), default=["15m", "30m"])

    cost = subparsers.add_parser("calibrate")
    cost.add_argument("--symbols", type=_symbols, required=True)
    cost.add_argument("--start")
    cost.add_argument("--end")
    cost.add_argument("--days", type=int, default=30)
    cost.add_argument("--regulatory-bps", type=float, default=0.1)
    return root


def main() -> None:
    args = parser().parse_args()
    print("Intraday execution-cost research | backend only | no broker action", flush=True)
    result = asyncio.run(sync_quotes(args)) if args.command == "sync-quotes" else calibrate(args)
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
