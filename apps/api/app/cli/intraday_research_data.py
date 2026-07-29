"""Build a frozen intraday research dataset directly on the VPS."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.db import connect
from app.providers.alpaca import sync_alpaca_candles


def _csv(value: str, *, upper: bool = False) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("at least one value is required")
    return list(dict.fromkeys(item.upper() if upper else item for item in items))


async def prepare(args: argparse.Namespace) -> dict:
    from app.services.labs.intraday.dataset_snapshot import record_intraday_dataset_snapshot
    from app.services.labs.intraday.features import backfill_intraday_features

    symbols = _csv(args.symbols, upper=True)
    timeframes = _csv(args.timeframes)
    synced: list[dict] = []
    for symbol in symbols:
        for timeframe in timeframes:
            print(f"candles {symbol} {timeframe}: syncing", flush=True)
            with connect() as conn:
                result = await sync_alpaca_candles(
                    conn,
                    symbol=symbol,
                    timeframe=timeframe,
                    limit=args.candle_limit,
                )
            synced.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "received": result.received,
                    "upserted": result.upserted,
                }
            )

    print("session-aware features: backfilling", flush=True)
    with connect() as conn:
        feature_result = backfill_intraday_features(
            conn,
            symbols,
            tuple(timeframes),
            candle_limit=args.candle_limit,
        )
        snapshot = record_intraday_dataset_snapshot(
            conn,
            assets=symbols,
            timeframes=timeframes,
            mode="rolling",
            name=args.name,
            universe_key=args.universe_key,
        )
    return {
        "candles": synced,
        "features": feature_result,
        "dataset": snapshot,
        "research_only": True,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description=(
            "Sync Alpaca candles, backfill session-aware features, and create an "
            "immutable intraday snapshot. Research only; no campaign or broker action."
        )
    )
    root.add_argument("--symbols", required=True, help="Comma-separated research universe")
    root.add_argument("--timeframes", default="30m", help="Comma-separated 15m/30m values")
    root.add_argument("--candle-limit", type=int, default=5000)
    root.add_argument("--name")
    root.add_argument(
        "--universe-key",
        help=(
            "Optional historical universe key. When supplied, snapshot rows are "
            "materialized only while each symbol was an active member."
        ),
    )
    return root


def main() -> None:
    print("Intraday research dataset builder | backend only", flush=True)
    result = asyncio.run(prepare(parser().parse_args()))
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
