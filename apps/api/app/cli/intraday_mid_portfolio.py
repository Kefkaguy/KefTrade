"""Backend-only mid-portfolio research screen.

This command deliberately does not weaken elite gates, create campaigns, submit
orders, or promote strategies.  It answers a separate question: which existing
intraday lab candidates are modest positive edges worth putting on a mid-tier
portfolio watchlist?
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Any

from app.cli._refusal import run_command
from app.db import connect
from app.services.labs.intraday.dataset import IntradayDatasetError, load_intraday_backtest_dataset
from app.services.labs.intraday.families.registry import FAMILY_REGISTRY
from app.services.labs.intraday.feature_engine_v2 import DEFAULT_CONFIG
from app.services.research_architecture import jsonable
from app.services.strategy_discovery import evaluate_candidate

MID_PORTFOLIO_VERSION = "intraday_mid_portfolio_screen_v1"


def _symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("at least one symbol is required")
    return list(dict.fromkeys(symbols))


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _profit_factor(gross_profit: float, gross_loss: float) -> float | None:
    if gross_loss > 0:
        return gross_profit / gross_loss
    if gross_profit > 0:
        return None
    return 0.0


def _candidate_tier(
    *,
    trades: int,
    symbols_traded: int,
    profit_factor: float | None,
    expectancy: float,
    positive_symbol_share: float | None,
    min_trades: int,
    min_symbols: int,
) -> str:
    pf = profit_factor or 0.0
    if (
        trades >= max(min_trades * 2, min_trades)
        and symbols_traded >= min_symbols
        and pf >= 1.2
        and expectancy > 0
        and (positive_symbol_share or 0.0) >= 0.55
    ):
        return "strong_mid_portfolio_candidate"
    if (
        trades >= min_trades
        and symbols_traded >= min_symbols
        and pf >= 1.05
        and expectancy > 0
        and (positive_symbol_share or 0.0) >= 0.45
    ):
        return "mid_portfolio_candidate"
    if trades >= max(20, min_trades // 2) and pf >= 1.0 and expectancy > 0:
        return "weak_positive_watchlist"
    return "failed_or_negative"


def _active_family_ids(timeframe: str, *, include_archived: bool) -> list[str]:
    blocked = {"blocked_data"}
    allowed = {"active", "research_only", "confirmation_only"}
    if include_archived:
        allowed.add("archived")
    return [
        key
        for key, definition in sorted(FAMILY_REGISTRY.items())
        if timeframe in definition.supported_timeframes
        and definition.status in allowed
        and definition.status not in blocked
    ]


def _net_trade_pnl(trade: dict[str, Any], *, round_trip_cost_bps: float) -> float:
    pnl = _float(trade.get("pnl"))
    if round_trip_cost_bps <= 0:
        return pnl
    entry_price = _float(trade.get("entry_price"))
    quantity = abs(_float(trade.get("quantity")))
    notional = entry_price * quantity
    return pnl - (notional * round_trip_cost_bps / 10_000)


def screen(args: argparse.Namespace) -> dict[str, Any]:
    family_ids = _csv(args.families) or _active_family_ids(
        args.timeframe,
        include_archived=args.include_archived,
    )
    unknown = [item for item in family_ids if item not in FAMILY_REGISTRY]
    if unknown:
        raise ValueError(f"unknown family id(s): {unknown}")
    unsupported = [
        item for item in family_ids
        if args.timeframe not in FAMILY_REGISTRY[item].supported_timeframes
    ]
    if unsupported:
        raise ValueError(f"family id(s) not supported on {args.timeframe}: {unsupported}")

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    dataset_cache: dict[tuple[int, str, str], dict[str, Any]] = {}

    with connect() as conn:
        for family_id in family_ids:
            definition = FAMILY_REGISTRY[family_id]
            candidates = definition.candidate_generator(
                max_candidates=args.max_candidates_per_family
            )
            for candidate in candidates:
                candidate = replace(
                    candidate,
                    parameters={
                        **candidate.parameters,
                        "timeframe": args.timeframe,
                        "recent_candle_window_bars": int(
                            candidate.parameters.get("recent_candle_window_bars")
                            or DEFAULT_CONFIG.lookback_bars
                        ),
                    },
                )
                trades: list[dict[str, Any]] = []
                symbol_pnls: dict[str, float] = {}
                symbols_loaded = 0
                for symbol in args.symbols:
                    cache_key = (args.dataset_id, symbol, args.timeframe)
                    try:
                        dataset = dataset_cache.get(cache_key)
                        if dataset is None:
                            dataset = load_intraday_backtest_dataset(
                                conn,
                                symbol,
                                args.timeframe,
                                dataset_id=args.dataset_id,
                            )
                            dataset_cache[cache_key] = dataset
                        symbols_loaded += 1
                        result = evaluate_candidate(
                            candidate,
                            dataset["candles"],
                            dataset["features"],
                            {},
                            market_arrays=dataset["market_arrays"],
                            session_end_index=dataset["session_end_index"],
                            persist_bar_series=False,
                        )
                    except IntradayDatasetError as exc:
                        failures.append(
                            {
                                "family": family_id,
                                "candidate_id": candidate.candidate_id,
                                "symbol": symbol,
                                "error": str(exc),
                            }
                        )
                        continue
                    symbol_trade_pnl = 0.0
                    for trade in result.get("trades") or []:
                        net_pnl = _net_trade_pnl(
                            trade,
                            round_trip_cost_bps=args.round_trip_cost_bps,
                        )
                        trade = {**trade, "mid_portfolio_net_pnl": net_pnl}
                        trades.append(trade)
                        symbol_trade_pnl += net_pnl
                    if result.get("trades"):
                        symbol_pnls[symbol] = symbol_trade_pnl

                gross_profit = sum(
                    max(0.0, _float(trade.get("mid_portfolio_net_pnl")))
                    for trade in trades
                )
                gross_loss = abs(
                    sum(
                        min(0.0, _float(trade.get("mid_portfolio_net_pnl")))
                        for trade in trades
                    )
                )
                trade_count = len(trades)
                expectancy = _safe_div(gross_profit - gross_loss, trade_count) or 0.0
                symbols_traded = len(symbol_pnls)
                positive_symbols = sum(1 for pnl in symbol_pnls.values() if pnl > 0)
                positive_symbol_share = _safe_div(positive_symbols, symbols_traded)
                pf = _profit_factor(gross_profit, gross_loss)
                rows.append(
                    {
                        "family": family_id,
                        "family_status": definition.status,
                        "family_name": definition.name,
                        "candidate_id": candidate.candidate_id,
                        "timeframe": args.timeframe,
                        "symbols_loaded": symbols_loaded,
                        "symbols_traded": symbols_traded,
                        "trades": trade_count,
                        "gross_profit": round(gross_profit, 2),
                        "gross_loss": round(gross_loss, 2),
                        "net_pnl": round(gross_profit - gross_loss, 2),
                        "profit_factor": round(pf, 6) if pf is not None else None,
                        "expectancy_per_trade": round(expectancy, 6),
                        "positive_symbols": positive_symbols,
                        "positive_symbol_share": (
                            round(positive_symbol_share, 6)
                            if positive_symbol_share is not None
                            else None
                        ),
                        "portfolio_tier": _candidate_tier(
                            trades=trade_count,
                            symbols_traded=symbols_traded,
                            profit_factor=pf,
                            expectancy=expectancy,
                            positive_symbol_share=positive_symbol_share,
                            min_trades=args.min_trades,
                            min_symbols=args.min_symbols,
                        ),
                        "parameters": jsonable(candidate.parameters),
                    }
                )

    ranked = sorted(
        rows,
        key=lambda row: (
            {
                "strong_mid_portfolio_candidate": 0,
                "mid_portfolio_candidate": 1,
                "weak_positive_watchlist": 2,
                "failed_or_negative": 3,
            }.get(str(row["portfolio_tier"]), 9),
            -_float(row["net_pnl"]),
            -_float(row["profit_factor"]),
        ),
    )
    return {
        "calculation_version": MID_PORTFOLIO_VERSION,
        "dataset_id": args.dataset_id,
        "timeframe": args.timeframe,
        "round_trip_cost_bps": args.round_trip_cost_bps,
        "families_tested": family_ids,
        "candidates_tested": len(rows),
        "symbols": args.symbols,
        "thresholds": {
            "min_trades": args.min_trades,
            "min_symbols": args.min_symbols,
            "mid_profit_factor": 1.05,
            "strong_mid_profit_factor": 1.2,
        },
        "tier_counts": {
            tier: sum(1 for row in ranked if row["portfolio_tier"] == tier)
            for tier in (
                "strong_mid_portfolio_candidate",
                "mid_portfolio_candidate",
                "weak_positive_watchlist",
                "failed_or_negative",
            )
        },
        "top_candidates": ranked[: args.limit],
        "failures": failures[:50],
        "protocol_note": (
            "Mid-tier research screen only. Elite gates are unchanged. Results "
            "are not locked confirmation, not campaign promotion, and not "
            "broker authorization."
        ),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Backend-only mid-portfolio screen for existing intraday datasets."
    )
    commands = root.add_subparsers(dest="command", required=True)
    screen_command = commands.add_parser("screen")
    screen_command.add_argument("--dataset-id", type=int, required=True)
    screen_command.add_argument("--timeframe", choices=("15m", "30m"), required=True)
    screen_command.add_argument("--symbols", type=_symbols, required=True)
    screen_command.add_argument(
        "--families",
        help=(
            "Comma-separated family ids. Default: all non-blocked families "
            "supported on the timeframe, including archived when "
            "--include-archived is set."
        ),
    )
    screen_command.add_argument("--include-archived", action="store_true")
    screen_command.add_argument("--max-candidates-per-family", type=int, default=4)
    screen_command.add_argument("--round-trip-cost-bps", type=float, default=0.0)
    screen_command.add_argument("--min-trades", type=int, default=100)
    screen_command.add_argument("--min-symbols", type=int, default=8)
    screen_command.add_argument("--limit", type=int, default=25)
    return root


COMMANDS = {"screen": screen}


def main() -> None:
    args = parser().parse_args()
    run_command(
        COMMANDS[args.command],
        args,
        banner="Intraday mid-portfolio screen | backend only | no broker action",
    )


if __name__ == "__main__":
    main()
