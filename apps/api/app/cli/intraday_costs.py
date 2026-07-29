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
    calibrate_regular_session_bar_costs,
    load_execution_evidence,
    load_regular_session_cost_bars,
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


def _regular_session_windows(
    start: datetime,
    end: datetime,
    *,
    timeframe: str,
) -> list[tuple[datetime, datetime]]:
    """Calendar-correct regular-session windows, including early closes."""
    from app.services.labs.intraday.session import trading_schedule

    minutes = int(timeframe[:-1])
    schedule = trading_schedule(start.date(), end.date(), padding_days=0)
    windows: list[tuple[datetime, datetime]] = []
    for _, session in schedule.iterrows():
        cursor = session["market_open"].to_pydatetime().astimezone(UTC)
        close = session["market_close"].to_pydatetime().astimezone(UTC)
        while cursor < close:
            window_end = min(cursor + timedelta(minutes=minutes), close)
            clipped_start = max(cursor, start)
            clipped_end = min(window_end, end)
            if clipped_start < clipped_end:
                windows.append((clipped_start, clipped_end))
            cursor = window_end
    return windows


async def sync_quotes(args: argparse.Namespace) -> dict:
    end = _time(args.end, fallback=datetime.now(tz=UTC))
    start = _time(args.start, fallback=end - timedelta(days=args.days))
    windows = _regular_session_windows(start, end, timeframe=args.quote_window)
    if not windows:
        raise ValueError("The requested period contains no completed regular-session windows.")
    quotes_per_window = max(1000, args.max_quotes // len(windows))
    imported = 0
    microstructure = 0
    for symbol in args.symbols:
        print(
            f"quotes {symbol}: fetching {len(windows)} regular-session "
            f"{args.quote_window} windows",
            flush=True,
        )
        normalized_by_timestamp = {}
        for window_start, window_end in windows:
            _, raw, _, _ = await fetch_stock_quotes(
                symbol,
                start=window_start,
                end=window_end,
                limit=quotes_per_window,
                feed=args.feed,
            )
            for row in raw:
                quote = normalize_stock_quote(symbol, row, feed=args.feed)
                if quote is not None:
                    normalized_by_timestamp[quote["timestamp"]] = quote
        normalized = list(normalized_by_timestamp.values())
        normalized.sort(key=lambda row: row["timestamp"])
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
        "regular_session_windows": len(windows),
        "quotes_per_window_cap": quotes_per_window,
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
        bars = load_regular_session_cost_bars(
            conn,
            symbols=args.symbols,
            timeframe=args.timeframe,
            start=start,
            end=end,
        )
        result = calibrate_regular_session_bar_costs(
            bars,
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
    sync.add_argument(
        "--quote-window",
        choices=("15m", "30m"),
        default="30m",
        help="Calendar-aligned regular-session sampling window.",
    )
    sync.add_argument("--timeframes", nargs="+", choices=("15m", "30m"), default=["15m", "30m"])

    cost = subparsers.add_parser("calibrate")
    cost.add_argument("--symbols", type=_symbols, required=True)
    cost.add_argument("--start")
    cost.add_argument("--end")
    cost.add_argument("--days", type=int, default=30)
    cost.add_argument("--timeframe", choices=("15m", "30m"), default="30m")
    cost.add_argument("--regulatory-bps", type=float, default=0.1)
    return root


def main() -> None:
    args = parser().parse_args()
    print("Intraday execution-cost research | backend only | no broker action", flush=True)
    result = asyncio.run(sync_quotes(args)) if args.command == "sync-quotes" else calibrate(args)
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
