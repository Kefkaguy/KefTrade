"""Stage 0 feasibility probe -- CLI.

Bounded, database-free, and deliberately incapable of testing a hypothesis.
It measures whether Alpaca's NBBO feed can support order-book research at all,
against thresholds declared before it was first run.

Nothing here declares a trial, writes to a research table, or touches the
locked confirmation window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.cli._refusal import run_command
from app.providers.alpaca import (
    fetch_stock_quotes,
    iter_stock_quote_pages,
    iter_stock_trade_pages,
    normalize_stock_quote,
    normalize_stock_trade,
)
from app.services.intraday_hypotheses import (
    DECLARED_OBSERVATION_DISPERSION_BPS,
    MINIMUM_TRADEABLE_NET_BPS,
    REQUIRED_T_STATISTIC,
)
from app.services.intraday_trade_flow import classifier_agreement_report
from app.services.microstructure_probe import (
    PREDECLARED_PROBE_SESSIONS,
    PREDECLARED_PROBE_SYMBOLS,
    STAGE0_VERSION,
    QuoteStreamProbe,
    aggregate_probe_reports,
    rotation_verdict,
    session_window,
    stage0_power_report,
)
from app.settings import settings

DEFAULT_OUTPUT_DIR = Path("reports/stage0_microstructure_probe")
POWER_Z = 0.841621

# Alpaca's public quoted spread for these names is one cent most of the time.
# Declared here as the cost input to the power comparison so the number is
# visible and arguable rather than buried.
DECLARED_ROUND_TRIP_COST_BPS = 3.0
DECLARED_COST_SAFETY_MULTIPLE = 2.0


def _session(value: str) -> date:
    return date.fromisoformat(value)


def _symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(symbols))


def _sessions(value: str) -> list[date]:
    return [_session(item.strip()) for item in value.split(",") if item.strip()]


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Power
# ---------------------------------------------------------------------------


def power(args: argparse.Namespace) -> dict[str, Any]:
    report = stage0_power_report(
        minimum_tradeable_net_bps=MINIMUM_TRADEABLE_NET_BPS,
        declared_dispersion_bps=DECLARED_OBSERVATION_DISPERSION_BPS,
        hurdle_t=REQUIRED_T_STATISTIC,
        power_z=POWER_Z,
        round_trip_cost_bps=args.round_trip_cost_bps,
        cost_safety_multiple=args.cost_safety_multiple,
    )
    _write(Path(args.output_dir) / "01_power.json", report)
    return report


# ---------------------------------------------------------------------------
# 2-5. Quote stream probe
# ---------------------------------------------------------------------------


async def _probe_symbol_session(
    symbol: str, session: date, *, feed: str, max_pages: int
) -> dict[str, Any]:
    start, end = session_window(session)
    probe = QuoteStreamProbe(symbol=symbol, session_date=session, feed=feed)
    async for rows, meta in iter_stock_quote_pages(
        symbol, start=start, end=end, feed=feed, max_pages=max_pages
    ):
        probe.add_page(rows, meta)
    return probe.report()


def probe(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    per_session_dir = output_dir / "sessions"
    reports: list[dict[str, Any]] = []
    skipped: list[str] = []
    for symbol in args.symbols:
        for session in args.sessions:
            checkpoint = per_session_dir / f"{symbol}_{session.isoformat()}.json"
            if checkpoint.exists() and not args.force:
                reports.append(json.loads(checkpoint.read_text(encoding="utf-8")))
                skipped.append(f"{symbol}/{session.isoformat()}")
                continue
            report = asyncio.run(
                _probe_symbol_session(
                    symbol, session, feed=args.feed, max_pages=args.max_pages
                )
            )
            _write(checkpoint, report)
            reports.append(report)
    pooled = aggregate_probe_reports(reports)
    payload = {
        "stage0_version": STAGE0_VERSION,
        "feed": args.feed,
        "symbols": args.symbols,
        "sessions": [item.isoformat() for item in args.sessions],
        "resumed_from_checkpoint": skipped,
        "pooled": pooled,
        "rotation_kill_rule": rotation_verdict(pooled.get("rotation_share_of_gross_abs_e")),
        "per_symbol_session": reports,
    }
    _write(output_dir / "02_quote_stream_probe.json", payload)
    return payload


# ---------------------------------------------------------------------------
# 6. Halt / status coverage
# ---------------------------------------------------------------------------

CANDIDATE_STATUS_ENDPOINTS = (
    "/v2/stocks/{symbol}/auctions",
    "/v2/stocks/{symbol}/status",
    "/v2/stocks/{symbol}/statuses",
    "/v2/stocks/{symbol}/halts",
    "/v2/stocks/{symbol}/luld",
    "/v2/stocks/{symbol}/imbalances",
    "/v1beta1/stocks/{symbol}/status",
)


async def _halt_coverage(symbol: str, session: date, *, feed: str) -> dict[str, Any]:
    start, end = session_window(session)
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key or "",
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret or "",
    }
    params = {
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "feed": feed,
        "limit": 10,
    }
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=settings.alpaca_data_base_url, timeout=30, headers=headers
    ) as client:
        for template in CANDIDATE_STATUS_ENDPOINTS:
            path = template.format(symbol=symbol)
            try:
                response = await client.get(path, params=params)
                body = response.text[:400]
                results.append(
                    {
                        "endpoint": template,
                        "status_code": response.status_code,
                        "available": response.status_code == 200,
                        "body_preview": body,
                    }
                )
            except httpx.HTTPError as error:  # transport failure, not a 4xx
                results.append(
                    {
                        "endpoint": template,
                        "status_code": None,
                        "available": False,
                        "error": str(error),
                    }
                )
    return {"symbol": symbol, "session": session.isoformat(), "endpoints": results}


def halt_coverage(args: argparse.Namespace) -> dict[str, Any]:
    probes = [
        asyncio.run(_halt_coverage(symbol, args.sessions[0], feed=args.feed))
        for symbol in args.symbols[:1]
    ]
    available = sorted(
        {
            row["endpoint"]
            for probe_result in probes
            for row in probe_result["endpoints"]
            if row.get("available")
        }
    )
    payload = {
        "stage0_version": STAGE0_VERSION,
        "question": (
            "Can trading halts, LULD bands or auction imbalances be reconstructed "
            "historically from the data we can retrieve?"
        ),
        "probes": probes,
        "available_endpoints": available,
        "halt_status_history_available": any(
            "status" in endpoint or "halt" in endpoint or "luld" in endpoint
            for endpoint in available
        ),
    }
    _write(Path(args.output_dir) / "03_halt_coverage.json", payload)
    return payload


# ---------------------------------------------------------------------------
# 7. Trade classifier agreement
# ---------------------------------------------------------------------------


async def _classifier_agreement(
    symbol: str,
    session: date,
    *,
    feed: str,
    window_minutes: int,
    quote_limit: int,
) -> dict[str, Any]:
    start, _ = session_window(session)
    start = start + timedelta(minutes=30)
    end = start + timedelta(minutes=window_minutes)

    trades: list[dict[str, Any]] = []
    truncated = False
    async for page, meta in iter_stock_trade_pages(
        symbol, start=start, end=end, feed=feed, max_pages=40
    ):
        trades.extend(
            row
            for row in (normalize_stock_trade(symbol, item, feed=feed) for item in page)
            if row
        )
        if meta.get("exhausted"):
            break
    else:
        truncated = True

    _status, raw_quotes, _log, _request_id = await fetch_stock_quotes(
        symbol, start=start, end=end, limit=quote_limit, feed=feed
    )
    quotes = [
        row
        for row in (normalize_stock_quote(symbol, item, feed=feed) for item in raw_quotes)
        if row
    ]
    report = classifier_agreement_report(trades, quotes)
    report.update(
        {
            "symbol": symbol,
            "session": session.isoformat(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "trade_stream_truncated": truncated,
            "quote_stream_possibly_truncated": len(raw_quotes) >= quote_limit,
        }
    )
    return report


def classifier_agreement(args: argparse.Namespace) -> dict[str, Any]:
    reports = [
        asyncio.run(
            _classifier_agreement(
                symbol,
                args.sessions[0],
                feed=args.feed,
                window_minutes=args.window_minutes,
                quote_limit=args.quote_limit,
            )
        )
        for symbol in args.symbols
    ]
    payload = {
        "stage0_version": STAGE0_VERSION,
        "reports": reports,
    }
    _write(Path(args.output_dir) / "04_classifier_agreement.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def verdict(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    pieces: dict[str, Any] = {}
    for key, filename in (
        ("power", "01_power.json"),
        ("quote_stream", "02_quote_stream_probe.json"),
        ("halt_coverage", "03_halt_coverage.json"),
        ("classifier_agreement", "04_classifier_agreement.json"),
    ):
        path = output_dir / filename
        if not path.exists():
            raise ValueError(f"missing Stage 0 artefact: {path} -- run the earlier commands first")
        pieces[key] = json.loads(path.read_text(encoding="utf-8"))

    rotation = pieces["quote_stream"]["rotation_kill_rule"]
    payload = {
        "stage0_version": STAGE0_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rotation_kill_rule": rotation,
        "pooled_stream": pieces["quote_stream"]["pooled"],
        "power": pieces["power"],
        "halt_status_history_available": pieces["halt_coverage"][
            "halt_status_history_available"
        ],
        "classifier_agreement_rates": [
            {
                "symbol": row["symbol"],
                "agreement_rate": row["agreement_rate"],
                "comparable_trades": row["comparable_trades"],
            }
            for row in pieces["classifier_agreement"]["reports"]
        ],
    }
    _write(output_dir / "05_verdict.json", payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keftrade-microstructure-probe",
        description="Stage 0 order-book data feasibility probe (no hypothesis testing).",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--feed", default="sip", choices=("sip", "iex"))
    parser.add_argument(
        "--symbols", type=_symbols, default=list(PREDECLARED_PROBE_SYMBOLS)
    )
    parser.add_argument(
        "--sessions", type=_sessions, default=list(PREDECLARED_PROBE_SESSIONS)
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    power_parser = subparsers.add_parser("power", help="Predeclared power and materiality.")
    power_parser.add_argument(
        "--round-trip-cost-bps", type=float, default=DECLARED_ROUND_TRIP_COST_BPS
    )
    power_parser.add_argument(
        "--cost-safety-multiple", type=float, default=DECLARED_COST_SAFETY_MULTIPLE
    )
    power_parser.set_defaults(handler=power)

    probe_parser = subparsers.add_parser(
        "probe", help="Stream quotes and measure rotation, collapse and rates."
    )
    probe_parser.add_argument("--max-pages", type=int, default=4000)
    probe_parser.add_argument("--force", action="store_true")
    probe_parser.set_defaults(handler=probe)

    halt_parser = subparsers.add_parser(
        "halt-coverage", help="Which status/halt/auction endpoints actually exist."
    )
    halt_parser.set_defaults(handler=halt_coverage)

    agreement_parser = subparsers.add_parser(
        "classifier-agreement", help="Lee-Ready versus tick rule on a bounded window."
    )
    agreement_parser.add_argument("--window-minutes", type=int, default=15)
    agreement_parser.add_argument("--quote-limit", type=int, default=200_000)
    agreement_parser.set_defaults(handler=classifier_agreement)

    verdict_parser = subparsers.add_parser("verdict", help="Assemble the Stage 0 result.")
    verdict_parser.set_defaults(handler=verdict)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_command(args.handler, args, banner=f"{STAGE0_VERSION} :: {args.command}")


if __name__ == "__main__":
    main()
