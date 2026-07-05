from decimal import Decimal
from statistics import mean, pstdev
from typing import Any

from app.services.strategy import trend_pullback_decision


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
) -> dict[str, Any]:
    rows = combine_candles_features(candles, features)
    train_rows, validation_rows = walk_forward_split(rows, float(params["walk_forward_train_ratio"]))
    execution_rows = validation_rows or rows

    equity = Decimal(str(params["initial_equity"]))
    initial_equity = equity
    fee_rate = Decimal(str(params["fee_rate"]))
    slippage_rate = Decimal(str(params["slippage_rate"]))
    risk_per_trade = Decimal(str(params["risk_per_trade"]))
    trades: list[dict[str, Any]] = []
    equity_curve = [equity]

    start_index = rows.index(execution_rows[0]) if execution_rows else 0
    i = max(start_index, 50)
    while i < len(rows) - 1:
        current = rows[i]
        candle = current["candle"]
        recent_candles = [row["candle"] for row in rows[: i + 1]]
        decision = trend_pullback_decision(candle, current["feature"], recent_candles, params)

        if decision.signal != "setup" or decision.stop_loss is None or decision.take_profit is None:
            i += 1
            continue

        signal_close = Decimal(candle["close"])
        entry_price = signal_close * (Decimal("1") + slippage_rate)
        risk_per_unit = entry_price - decision.stop_loss
        if risk_per_unit <= 0:
            i += 1
            continue

        max_risk = equity * risk_per_trade
        quantity = max_risk / risk_per_unit
        exit_price = None
        exit_reason = "end_of_data"
        exit_index = len(rows) - 1

        # Entry is evaluated on the next candle to avoid same-candle lookahead.
        for j in range(i + 1, len(rows)):
            future_candle = rows[j]["candle"]
            low = Decimal(future_candle["low"])
            high = Decimal(future_candle["high"])
            close = Decimal(future_candle["close"])
            if low <= decision.stop_loss:
                exit_price = decision.stop_loss * (Decimal("1") - slippage_rate)
                exit_reason = "stop_loss"
                exit_index = j
                break
            if high >= decision.take_profit:
                exit_price = decision.take_profit * (Decimal("1") - slippage_rate)
                exit_reason = "take_profit"
                exit_index = j
                break
            exit_price = close * (Decimal("1") - slippage_rate)

        if exit_price is None:
            i += 1
            continue

        gross_pnl = (exit_price - entry_price) * quantity
        fees = (entry_price * quantity * fee_rate) + (exit_price * quantity * fee_rate)
        pnl = gross_pnl - fees
        equity += pnl
        equity_curve.append(equity)
        trades.append(
            {
                "symbol": candle["symbol"],
                "side": "long",
                "entry_time": rows[i + 1]["candle"]["timestamp"],
                "exit_time": rows[exit_index]["candle"]["timestamp"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "pnl": pnl,
                "pnl_pct": pnl / initial_equity,
                "exit_reason": exit_reason,
            }
        )
        i = exit_index + 1

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

    return {"metrics": metrics, "trades": trades}


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
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
        "max_drawdown": float(max_drawdown),
        "sharpe_ratio": float(sharpe) if sharpe is not None else None,
        "number_of_trades": len(trades),
        "expectancy_per_trade": float(expectancy),
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
