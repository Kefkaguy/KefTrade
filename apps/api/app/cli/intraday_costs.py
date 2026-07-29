"""VPS CLI for quote capture and observed execution-cost calibration."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx

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


def _regular_sessions(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """One provider request window per exchange session.

    The former implementation made one HTTP request per intraday bar. A
    390-session, 25-symbol backfill therefore required well over 100,000
    requests. Session chunks retain calendar correctness while reducing that
    to roughly one paginated request chain per symbol/session.
    """
    from app.services.labs.intraday.session import trading_schedule

    schedule = trading_schedule(start.date(), end.date(), padding_days=0)
    sessions: list[tuple[datetime, datetime]] = []
    for _, session in schedule.iterrows():
        market_open = session["market_open"].to_pydatetime().astimezone(UTC)
        market_close = session["market_close"].to_pydatetime().astimezone(UTC)
        clipped_start = max(market_open, start)
        clipped_end = min(market_close, end)
        if clipped_start < clipped_end:
            sessions.append((clipped_start, clipped_end))
    return sessions


def _checkpoint_completed(
    conn,
    *,
    symbol: str,
    feed: str,
    window_start: datetime,
) -> bool:
    row = conn.execute(
        """
        SELECT status
        FROM intraday_quote_ingestion_checkpoints
        WHERE provider = 'alpaca'
          AND feed = %s
          AND symbol = %s
          AND session_date = %s
        """,
        (feed, symbol, window_start.date()),
    ).fetchone()
    return bool(row and row["status"] == "completed")


def _checkpoint_running(
    conn,
    *,
    symbol: str,
    feed: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO intraday_quote_ingestion_checkpoints(
            provider, feed, symbol, session_date, window_start, window_end,
            status, attempts, started_at, updated_at
        )
        VALUES ('alpaca', %s, %s, %s, %s, %s, 'running', 1, NOW(), NOW())
        ON CONFLICT(provider, feed, symbol, session_date)
        DO UPDATE SET
            window_start = EXCLUDED.window_start,
            window_end = EXCLUDED.window_end,
            status = 'running',
            attempts = intraday_quote_ingestion_checkpoints.attempts + 1,
            error = NULL,
            started_at = NOW(),
            updated_at = NOW()
        """,
        (feed, symbol, window_start.date(), window_start, window_end),
    )
    conn.commit()


def _checkpoint_finished(
    conn,
    *,
    symbol: str,
    feed: str,
    session_date,
    quote_rows: int,
    microstructure_rows: int,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE intraday_quote_ingestion_checkpoints
        SET status = %s,
            quote_rows = %s,
            microstructure_rows = %s,
            error = %s,
            completed_at = CASE WHEN %s::boolean THEN NOW() ELSE NULL END,
            updated_at = NOW()
        WHERE provider = 'alpaca'
          AND feed = %s
          AND symbol = %s
          AND session_date = %s
        """,
        (
            "completed" if error is None else "failed",
            quote_rows,
            microstructure_rows,
            error,
            error is None,
            feed,
            symbol,
            session_date,
        ),
    )
    conn.commit()


async def _fetch_complete_session_quotes(
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    limit: int,
    feed: str,
    rate_limit_retries: int = 5,
    rate_limit_base_sleep: float = 60.0,
) -> list[dict]:
    """Fetch a session completely, splitting only when pagination exhausts."""
    _, raw, request_log, _ = await _fetch_stock_quotes_with_rate_limit_retry(
        symbol,
        start=window_start,
        end=window_end,
        limit=limit,
        feed=feed,
        retries=rate_limit_retries,
        base_sleep=rate_limit_base_sleep,
    )
    if not request_log or not request_log[-1].get("next_page_token_present"):
        return raw

    complete: list[dict] = []
    for sub_start, sub_end in _regular_session_windows(
        window_start,
        window_end,
        timeframe="30m",
    ):
        _, sub_rows, sub_log, _ = await _fetch_stock_quotes_with_rate_limit_retry(
            symbol,
            start=sub_start,
            end=sub_end,
            limit=limit,
            feed=feed,
            retries=rate_limit_retries,
            base_sleep=rate_limit_base_sleep,
        )
        if sub_log and sub_log[-1].get("next_page_token_present"):
            raise RuntimeError(
                f"Provider pagination remained incomplete for {symbol} "
                f"{sub_start.isoformat()} to {sub_end.isoformat()}; the session "
                "is not being persisted."
            )
        complete.extend(sub_rows)
    return complete


def _retry_after_seconds(error: httpx.HTTPStatusError) -> float | None:
    header = error.response.headers.get("Retry-After")
    if not header:
        return None
    try:
        value = float(header)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


async def _fetch_stock_quotes_with_rate_limit_retry(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    feed: str,
    retries: int,
    base_sleep: float,
) -> tuple[int, list[dict], list[dict], str | None]:
    attempts = max(0, retries) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await fetch_stock_quotes(
                symbol,
                start=start,
                end=end,
                limit=limit,
                feed=feed,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 429 or attempt == attempts:
                raise
            retry_after = _retry_after_seconds(error)
            sleep_seconds = retry_after if retry_after is not None else base_sleep * attempt
            print(
                f"quotes {symbol}: provider rate-limited; retry "
                f"{attempt}/{attempts - 1} after {sleep_seconds:.1f}s",
                flush=True,
            )
            await asyncio.sleep(sleep_seconds)
    raise RuntimeError("unreachable quote retry state")


async def sync_quotes(args: argparse.Namespace) -> dict:
    end = _time(args.end, fallback=datetime.now(tz=UTC))
    start = _time(args.start, fallback=end - timedelta(days=args.days))
    sessions = _regular_sessions(start, end)
    if not sessions:
        raise ValueError("The requested period contains no completed regular sessions.")
    if args.feed.lower() != "sip" and not args.allow_partial_feed:
        raise ValueError(
            "Executable 30m research requires consolidated SIP quotes. "
            "Pass --allow-partial-feed only for diagnostics that cannot promote an elite."
        )
    imported = 0
    processed_quotes = 0
    microstructure = 0
    completed_sessions = 0
    skipped_sessions = 0
    failed_sessions = 0
    for symbol in args.symbols:
        print(f"quotes {symbol}: {len(sessions)} exchange sessions", flush=True)
        for session_number, (window_start, window_end) in enumerate(sessions, start=1):
            with connect() as conn:
                if args.resume and _checkpoint_completed(
                    conn,
                    symbol=symbol,
                    feed=args.feed,
                    window_start=window_start,
                ):
                    skipped_sessions += 1
                    continue
                _checkpoint_running(
                    conn,
                    symbol=symbol,
                    feed=args.feed,
                    window_start=window_start,
                    window_end=window_end,
                )
            try:
                raw = await _fetch_complete_session_quotes(
                    symbol=symbol,
                    window_start=window_start,
                    window_end=window_end,
                    limit=args.max_quotes_per_session,
                    feed=args.feed,
                    rate_limit_retries=args.rate_limit_retries,
                    rate_limit_base_sleep=args.rate_limit_base_sleep,
                )
                normalized_by_timestamp = {}
                for row in raw:
                    quote = normalize_stock_quote(symbol, row, feed=args.feed)
                    if quote is not None:
                        normalized_by_timestamp[quote["timestamp"]] = quote
                normalized = sorted(
                    normalized_by_timestamp.values(),
                    key=lambda row: row["timestamp"],
                )
                processed_quotes += len(normalized)
                session_microstructure = 0
                with connect() as conn:
                    if window_end >= end - timedelta(days=args.retain_raw_days):
                        imported += persist_quote_snapshots(conn, normalized)
                    for timeframe in args.timeframes:
                        rows = aggregate_microstructure_bars(normalized, timeframe=timeframe)
                        stored = persist_microstructure_bars(conn, rows)
                        microstructure += stored
                        session_microstructure += stored
                    _checkpoint_finished(
                        conn,
                        symbol=symbol,
                        feed=args.feed,
                        session_date=window_start.date(),
                        quote_rows=len(normalized),
                        microstructure_rows=session_microstructure,
                    )
                completed_sessions += 1
                if session_number == 1 or session_number % 20 == 0 or session_number == len(sessions):
                    print(
                        f"quotes {symbol}: {session_number}/{len(sessions)} sessions",
                        flush=True,
                    )
                if args.request_pause_seconds > 0:
                    await asyncio.sleep(args.request_pause_seconds)
            except Exception as error:
                failed_sessions += 1
                with connect() as conn:
                    _checkpoint_finished(
                        conn,
                        symbol=symbol,
                        feed=args.feed,
                        session_date=window_start.date(),
                        quote_rows=0,
                        microstructure_rows=0,
                        error=f"{type(error).__name__}: {error}",
                    )
                if not args.continue_on_error:
                    raise
    return {
        "symbols": args.symbols,
        "window_start": start,
        "window_end": end,
        "quote_rows_stored": imported,
        "quote_rows_processed": processed_quotes,
        "microstructure_rows_stored": microstructure,
        "feed": args.feed,
        "regular_sessions": len(sessions),
        "completed_symbol_sessions": completed_sessions,
        "skipped_completed_symbol_sessions": skipped_sessions,
        "failed_symbol_sessions": failed_sessions,
        "quotes_per_session_cap": args.max_quotes_per_session,
        "retain_raw_days": args.retain_raw_days,
        "resumable": True,
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
            feed=args.feed,
        )
        bars = load_regular_session_cost_bars(
            conn,
            symbols=args.symbols,
            timeframe=args.timeframe,
            start=start,
            end=end,
            feed=args.feed,
        )
        result = calibrate_regular_session_bar_costs(
            bars,
            quotes,
            fills,
            regulatory_bps=args.regulatory_bps,
        )
        result["calibration_id"] = persist_cost_calibration(conn, result)
    return result


def status(args: argparse.Namespace) -> dict:
    end = _time(args.end, fallback=datetime.now(tz=UTC))
    start = _time(args.start, fallback=end - timedelta(days=args.days))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS sessions,
                   SUM(quote_rows) AS quote_rows,
                   SUM(microstructure_rows) AS microstructure_rows,
                   MIN(session_date) AS first_session,
                   MAX(session_date) AS last_session
            FROM intraday_quote_ingestion_checkpoints
            WHERE provider = 'alpaca' AND feed = %s
              AND symbol = ANY(%s)
              AND session_date BETWEEN %s AND %s
            GROUP BY status
            ORDER BY status
            """,
            (
                args.feed,
                args.symbols,
                start.date(),
                end.date(),
            ),
        ).fetchall()
    by_status = {
        str(row["status"]): {
            "sessions": int(row["sessions"] or 0),
            "quote_rows": int(row["quote_rows"] or 0),
            "microstructure_rows": int(row["microstructure_rows"] or 0),
            "first_session": row["first_session"],
            "last_session": row["last_session"],
        }
        for row in rows
    }
    expected = len(_regular_sessions(start, end)) * len(args.symbols)
    completed = int((by_status.get("completed") or {}).get("sessions") or 0)
    return {
        "symbols": args.symbols,
        "feed": args.feed,
        "window_start": start,
        "window_end": end,
        "expected_symbol_sessions": expected,
        "completed_symbol_sessions": completed,
        "completion_fraction": round(completed / expected, 6) if expected else 0,
        "by_status": by_status,
    }


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
    sync.add_argument("--max-quotes-per-session", type=int, default=1_000_000)
    sync.add_argument(
        "--retain-raw-days",
        type=int,
        default=45,
        help=(
            "Keep raw quote messages only for this recent window; older sessions "
            "still persist complete 30m microstructure aggregates."
        ),
    )
    sync.add_argument("--feed", default="sip")
    sync.add_argument("--timeframes", nargs="+", choices=("30m",), default=["30m"])
    sync.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    sync.add_argument("--continue-on-error", action="store_true")
    sync.add_argument(
        "--rate-limit-retries",
        type=int,
        default=8,
        help="Retry HTTP 429 responses before marking a symbol/session failed.",
    )
    sync.add_argument(
        "--rate-limit-base-sleep",
        type=float,
        default=60.0,
        help="Seconds to wait per retry attempt when the provider does not send Retry-After.",
    )
    sync.add_argument(
        "--request-pause-seconds",
        type=float,
        default=0.0,
        help="Optional pause after each completed symbol/session to stay below provider limits.",
    )
    sync.add_argument(
        "--allow-partial-feed",
        action="store_true",
        help="Permit IEX/non-SIP diagnostics; these observations cannot make an elite executable.",
    )

    cost = subparsers.add_parser("calibrate")
    cost.add_argument("--symbols", type=_symbols, required=True)
    cost.add_argument("--start")
    cost.add_argument("--end")
    cost.add_argument("--days", type=int, default=30)
    cost.add_argument("--timeframe", choices=("30m",), default="30m")
    cost.add_argument("--feed", default="sip")
    cost.add_argument("--regulatory-bps", type=float, default=0.1)

    progress = subparsers.add_parser("status")
    progress.add_argument("--symbols", type=_symbols, required=True)
    progress.add_argument("--start")
    progress.add_argument("--end")
    progress.add_argument("--days", type=int, default=30)
    progress.add_argument("--feed", default="sip")
    return root


def main() -> None:
    args = parser().parse_args()
    print("Intraday execution-cost research | backend only | no broker action", flush=True)
    if args.command == "sync-quotes":
        result = asyncio.run(sync_quotes(args))
    elif args.command == "calibrate":
        result = calibrate(args)
    else:
        result = status(args)
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
