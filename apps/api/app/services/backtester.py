from datetime import datetime
from decimal import Decimal
from statistics import mean, pstdev
from typing import Any

import numpy as np

from app.services.strategy import StrategyFn, trend_pullback_decision
from app.services.strategy_diagnostics import enrich_decision

SAME_CANDLE_EXIT_POLICY = "stop_first"


def combine_candles_features(candles: list[dict[str, Any]], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feature_by_time = {row["timestamp"]: row for row in features}
    combined = []
    for candle in candles:
        feature = feature_by_time.get(candle["timestamp"])
        if feature:
            combined.append({"candle": candle, "feature": feature})
    return combined


def walk_forward_split(rows: list[dict[str, Any]], train_ratio: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(rows) < 80:
        return rows, []
    split_index = max(1, min(len(rows) - 1, int(len(rows) * train_ratio)))
    return rows[:split_index], rows[split_index:]


def run_backtest(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    params: dict[str, Any],
    strategy_decide: StrategyFn = trend_pullback_decision,
    market_arrays: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    rows = combine_candles_features(candles, features)
    candle_rows = [row["candle"] for row in rows]
    arrays = market_arrays if market_arrays is not None and len(market_arrays["low"]) == len(rows) else build_market_arrays(rows)
    train_rows, validation_rows = walk_forward_split(rows, float(params["walk_forward_train_ratio"]))
    execution_rows = validation_rows or rows

    equity = Decimal(str(params["initial_equity"]))
    initial_equity = equity
    fee_rate = Decimal(str(params["fee_rate"]))
    slippage_rate = Decimal(str(params["slippage_rate"]))
    risk_per_trade = Decimal(str(params["risk_per_trade"]))
    trades: list[dict[str, Any]] = []
    equity_curve = [equity]
    entry_delay_bars = max(0, int(params.get("entry_delay_bars") or 0))
    entry_offset = 1 + entry_delay_bars
    entry_cooldown_bars = max(0, int(params.get("entry_cooldown_bars") or 0))

    start_index = len(train_rows) if validation_rows else 0
    realized_equity_points = [{"timestamp": rows[start_index]["candle"]["timestamp"], "equity": equity}] if rows else []
    i = max(start_index, 50)
    while i < len(rows) - entry_offset:
        current = rows[i]
        candle = current["candle"]
        feature = current["feature"]
        recent_window_bars = max(0, int(params.get("recent_candle_window_bars") or 0))
        recent_start = max(0, i + 1 - recent_window_bars) if recent_window_bars else 0
        recent_candles = candle_rows[recent_start : i + 1]
        decision = enrich_decision(strategy_decide(candle, feature, recent_candles, params), candle, feature, recent_candles, params)

        if decision.signal != "setup" or decision.stop_loss is None or decision.take_profit is None:
            i += 1
            continue

        entry_candle = rows[i + entry_offset]["candle"]
        entry_price = Decimal(entry_candle["open"]) * (Decimal("1") + slippage_rate)
        risk_per_unit = entry_price - decision.stop_loss
        if risk_per_unit <= 0:
            i += 1
            continue
        effective_risk_reward = decision.risk_reward if decision.risk_reward is not None else Decimal(str(params["risk_reward"]))
        effective_take_profit = entry_price + (risk_per_unit * effective_risk_reward)

        max_risk = equity * risk_per_trade
        quantity = max_risk / risk_per_unit
        exit_index, exit_reason = find_exit_index(
            rows,
            arrays,
            start_index=i + entry_offset,
            stop_loss=decision.stop_loss,
            take_profit=effective_take_profit,
            max_holding_bars=int(params.get("max_holding_bars") or 0),
        )
        exit_candle = rows[exit_index]["candle"]
        if exit_reason.startswith("stop_loss"):
            exit_price = decision.stop_loss * (Decimal("1") - slippage_rate)
        elif exit_reason == "take_profit":
            exit_price = effective_take_profit * (Decimal("1") - slippage_rate)
        else:
            exit_price = Decimal(exit_candle["close"]) * (Decimal("1") - slippage_rate)

        for mark_index in range(i + entry_offset, exit_index + 1):
            mark_price = exit_price if mark_index == exit_index else Decimal(rows[mark_index]["candle"]["close"])
            equity_curve.append(mark_to_market_equity(equity, entry_price, mark_price, quantity))

        if exit_price is None:
            i += 1
            continue

        gross_pnl = (exit_price - entry_price) * quantity
        fees = (entry_price * quantity * fee_rate) + (exit_price * quantity * fee_rate)
        pnl = gross_pnl - fees
        equity += pnl
        equity_curve.append(equity)
        realized_equity_points.append({"timestamp": rows[exit_index]["candle"]["timestamp"], "equity": equity})
        trades.append(
            {
                "symbol": candle["symbol"],
                "side": "long",
                "entry_time": entry_candle["timestamp"],
                "exit_time": rows[exit_index]["candle"]["timestamp"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "stop_loss": decision.stop_loss,
                "take_profit": effective_take_profit,
                "pnl": pnl,
                "pnl_pct": pnl / initial_equity,
                "exit_reason": exit_reason,
                "holding_period_hours": (rows[exit_index]["candle"]["timestamp"] - entry_candle["timestamp"]).total_seconds() / 3600,
                "entry_reason": decision.explanation,
                "entry_candle": candle_snapshot(entry_candle),
                "exit_candle": candle_snapshot(rows[exit_index]["candle"]),
                "indicators": indicator_snapshot(feature),
            }
        )
        i = max(exit_index + 1, i + entry_cooldown_bars + 1)

    metrics = calculate_metrics(initial_equity, equity, trades, equity_curve)
    if train_rows and validation_rows:
        metrics["walk_forward"] = {
            "enabled": True,
            "train_start": train_rows[0]["candle"]["timestamp"].isoformat(),
            "train_end": train_rows[-1]["candle"]["timestamp"].isoformat(),
            "validation_start": validation_rows[0]["candle"]["timestamp"].isoformat(),
            "validation_end": validation_rows[-1]["candle"]["timestamp"].isoformat(),
        }
    else:
        metrics["walk_forward"] = {"enabled": False, "reason": "At least 80 candle/feature rows are required."}

    return {
        "metrics": metrics,
        "trades": trades,
        "equity_curve": build_equity_curve(realized_equity_points),
        "drawdown_curve": build_drawdown_curve(realized_equity_points),
        "equity_curve_summary": summarize_equity_curve(equity_curve),
    }


def count_setup_opportunities(
    candles: list[dict[str, Any]],
    features: list[dict[str, Any]],
    params: dict[str, Any],
    strategy_decide: StrategyFn,
) -> dict[str, Any]:
    rows = combine_candles_features(candles, features)
    candle_rows = [row["candle"] for row in rows]
    train_rows, validation_rows = walk_forward_split(rows, float(params["walk_forward_train_ratio"]))
    execution_rows = validation_rows or rows
    start_index = len(train_rows) if validation_rows else 0
    count = 0
    for index in range(max(start_index, 50), max(0, len(rows) - 1)):
        current = rows[index]
        recent_window_bars = max(0, int(params.get("recent_candle_window_bars") or 0))
        recent_start = max(0, index + 1 - recent_window_bars) if recent_window_bars else 0
        recent_candles = candle_rows[recent_start : index + 1]
        decision = enrich_decision(strategy_decide(current["candle"], current["feature"], recent_candles, params), current["candle"], current["feature"], recent_candles, params)
        if decision.signal == "setup" and decision.stop_loss is not None and decision.take_profit is not None:
            count += 1
    return {
        "opportunities": count,
        "execution_rows": len(execution_rows),
        "walk_forward_enabled": bool(validation_rows),
    }


def build_market_arrays(rows: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    return {
        "low": np.fromiter((float(row["candle"]["low"]) for row in rows), dtype=np.float64, count=len(rows)),
        "high": np.fromiter((float(row["candle"]["high"]) for row in rows), dtype=np.float64, count=len(rows)),
    }


def find_exit_index(
    rows: list[dict[str, Any]],
    arrays: dict[str, np.ndarray],
    *,
    start_index: int,
    stop_loss: Decimal,
    take_profit: Decimal,
    max_holding_bars: int,
) -> tuple[int, str]:
    final_index = len(rows) - 1
    search_end = min(final_index, start_index + max_holding_bars) if max_holding_bars > 0 else final_index
    lows = arrays["low"][start_index : search_end + 1]
    highs = arrays["high"][start_index : search_end + 1]
    hit_offsets = np.flatnonzero((lows <= float(stop_loss)) | (highs >= float(take_profit)))
    for offset in hit_offsets:
        index = start_index + int(offset)
        candle = rows[index]["candle"]
        stop_touched = Decimal(candle["low"]) <= stop_loss
        target_touched = Decimal(candle["high"]) >= take_profit
        if stop_touched:
            return index, "stop_loss" if not target_touched else f"stop_loss_{SAME_CANDLE_EXIT_POLICY}"
        if target_touched:
            return index, "take_profit"
    if max_holding_bars > 0 and search_end < final_index:
        return search_end, "time_exit"
    return final_index, "end_of_data"


def mark_to_market_equity(equity_before_trade: Decimal, entry_price: Decimal, mark_price: Decimal, quantity: Decimal) -> Decimal:
    return equity_before_trade + ((mark_price - entry_price) * quantity)


def calculate_metrics(initial_equity: Decimal, final_equity: Decimal, trades: list[dict[str, Any]], equity_curve: list[Decimal]) -> dict[str, Any]:
    wins = [trade["pnl"] for trade in trades if trade["pnl"] > 0]
    losses = [trade["pnl"] for trade in trades if trade["pnl"] <= 0]
    returns = [trade["pnl_pct"] for trade in trades]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    win_rate = Decimal(len(wins)) / Decimal(len(trades)) if trades else Decimal("0")
    avg_win = gross_profit / Decimal(len(wins)) if wins else Decimal("0")
    avg_loss = gross_loss / Decimal(len(losses)) if losses else Decimal("0")
    loss_rate = Decimal("1") - win_rate if trades else Decimal("0")
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    profit_factor_is_infinite = gross_loss == 0 and gross_profit > 0
    max_drawdown = calculate_max_drawdown(equity_curve)
    sharpe = None
    if len(returns) > 1:
        return_values = [float(value) for value in returns]
        stdev = pstdev(return_values)
        sharpe = mean(return_values) / stdev if stdev else None

    return {
        "initial_equity": float(initial_equity),
        "final_equity": float(final_equity),
        "total_return": float((final_equity - initial_equity) / initial_equity),
        "win_rate": float(win_rate),
        "average_win": float(avg_win),
        "average_loss": float(avg_loss),
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
        "profit_factor_is_infinite": profit_factor_is_infinite,
        "max_drawdown": float(max_drawdown),
        "sharpe_ratio": float(sharpe) if sharpe is not None else None,
        "number_of_trades": len(trades),
        "expectancy_per_trade": float(expectancy),
        "longest_losing_streak": longest_losing_streak(trades),
        "average_holding_time_hours": average_holding_time_hours(trades),
    }


def calculate_max_drawdown(equity_curve: list[Decimal]) -> Decimal:
    peak = equity_curve[0] if equity_curve else Decimal("0")
    max_drawdown = Decimal("0")
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (peak - value) / peak
            max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def longest_losing_streak(trades: list[dict[str, Any]]) -> int:
    longest = 0
    current = 0
    for trade in trades:
        if trade["pnl"] <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def average_holding_time_hours(trades: list[dict[str, Any]]) -> float:
    durations = [float(trade.get("holding_period_hours", 0)) for trade in trades]
    return mean(durations) if durations else 0.0


def summarize_equity_curve(equity_curve: list[Decimal]) -> dict[str, Any]:
    if not equity_curve:
        return {"points": 0, "start": None, "end": None, "high": None, "low": None}
    return {
        "points": len(equity_curve),
        "start": float(equity_curve[0]),
        "end": float(equity_curve[-1]),
        "high": float(max(equity_curve)),
        "low": float(min(equity_curve)),
    }


def candle_snapshot(candle: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": candle["timestamp"],
        "open": candle["open"],
        "high": candle["high"],
        "low": candle["low"],
        "close": candle["close"],
        "volume": candle["volume"],
    }


def indicator_snapshot(feature: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "returns_1",
        "returns_5",
        "ema_20",
        "ema_50",
        "rsi_14",
        "macd",
        "macd_signal",
        "volume_change",
        "volatility_20",
        "distance_from_ema_20",
        "distance_from_ema_50",
    ]
    return {key: feature.get(key) for key in keys}


def build_equity_curve(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"timestamp": point["timestamp"], "equity": point["equity"]} for point in points]


def build_drawdown_curve(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    peak: Decimal | None = None
    curve = []
    for point in points:
        equity = Decimal(point["equity"])
        peak = equity if peak is None else max(peak, equity)
        drawdown = Decimal("0") if peak == 0 else (peak - equity) / peak
        curve.append({"timestamp": point["timestamp"], "drawdown": drawdown})
    return curve


def month_key(value: datetime) -> str:
    return f"{value.year:04d}-{value.month:02d}"
