from __future__ import annotations

import argparse
import json

from app.db import connect
from app.services.labs.intraday.features import backfill_intraday_features, sync_intraday_features


def execute(args: argparse.Namespace) -> dict:
    with connect() as conn:
        if args.command == "backfill":
            symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
            timeframes = tuple(tf.strip() for tf in args.timeframes.split(",") if tf.strip())
            return backfill_intraday_features(conn, symbols, timeframes, candle_limit=args.candle_limit)
        if args.command == "sync":
            return sync_intraday_features(
                conn,
                args.symbol.upper(),
                args.timeframe,
                candle_limit=args.candle_limit,
                opening_range_minutes=args.opening_range_minutes,
            )
        raise ValueError("unsupported intraday-features command")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KefTrade Intraday Lab: session-aware feature computation (Phase 12, Step 1).")
    commands = root.add_subparsers(dest="command", required=True)

    backfill = commands.add_parser("backfill", help="Compute intraday_features for every (symbol, timeframe) pair given.")
    backfill.add_argument("--symbols", required=True, help="Comma-separated symbols, e.g. TSLA,NVDA,AAPL")
    backfill.add_argument("--timeframes", default="15m,30m", help="Comma-separated timeframes (default: 15m,30m)")
    backfill.add_argument("--candle-limit", type=int, default=None, help="Optional cap on candles loaded per symbol/timeframe")

    sync = commands.add_parser("sync", help="Recompute intraday_features for one (symbol, timeframe) pair.")
    sync.add_argument("symbol")
    sync.add_argument("timeframe", choices=["15m", "30m"])
    sync.add_argument("--candle-limit", type=int, default=None)
    sync.add_argument("--opening-range-minutes", type=int, default=None, help="Override settings.intraday_opening_range_minutes for this run")

    return root


def main() -> None:
    print("Intraday Lab | Step 1: session-aware features only | Simulation/research data, no execution impact")
    print(json.dumps(execute(parser().parse_args()), default=str, indent=2))


if __name__ == "__main__":
    main()
