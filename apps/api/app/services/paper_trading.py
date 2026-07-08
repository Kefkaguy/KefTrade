from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

FEE_RATE = Decimal("0.001")
SLIPPAGE_RATE = Decimal("0.0005")
MAX_ORDER_NOTIONAL_FRACTION = Decimal("0.25")


class PaperTradingError(ValueError):
    pass


def create_paper_account(conn: psycopg.Connection, name: str, starting_cash: Decimal, base_currency: str = "USD") -> dict[str, Any]:
    if starting_cash <= 0:
        raise PaperTradingError("starting_cash must be positive")
    row = conn.execute(
        """
        INSERT INTO paper_accounts(name, base_currency, starting_cash, cash_balance, simulation_only)
        VALUES (%s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (name, base_currency, starting_cash, starting_cash),
    ).fetchone()
    log_event(conn, row["id"], None, None, "paper_account_created", "Created simulation-only paper account.", {"starting_cash": str(starting_cash)})
    record_equity_snapshot(conn, row["id"])
    conn.commit()
    return dict(row)


def list_accounts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_accounts ORDER BY created_at DESC").fetchall())


def get_account(conn: psycopg.Connection, account_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM paper_accounts WHERE id = %s", (account_id,)).fetchone()
    if not row:
        raise PaperTradingError("paper account not found")
    return dict(row)


def create_order(
    conn: psycopg.Connection,
    account_id: int,
    symbol: str,
    quantity: Decimal,
    side: str = "buy",
    order_type: str = "market",
    timeframe: str = "1d",
    limit_price: Decimal | None = None,
    deployment_id: int | None = None,
) -> dict[str, Any]:
    account = get_account(conn, account_id)
    deployment_block = deployment_block_reason(conn, account_id, deployment_id)
    side = side.lower()
    order_type = order_type.lower()
    if side not in {"buy", "sell"}:
        raise PaperTradingError("paper trading supports buy/sell simulation only")
    if order_type not in {"market", "limit"}:
        raise PaperTradingError("paper trading supports market and limit simulation only")
    if quantity <= 0:
        raise PaperTradingError("quantity must be positive")
    latest = latest_candle(conn, symbol, timeframe)
    blocked = deployment_block or risk_block_reason(conn, account, symbol, side, quantity, latest["close"], limit_price)
    status = "rejected" if blocked else "pending"
    row = conn.execute(
        """
        INSERT INTO paper_orders(account_id, deployment_id, symbol, timeframe, side, order_type, quantity, limit_price, status, rejected_reason, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (account_id, deployment_id, symbol.upper(), timeframe, side, order_type, quantity, limit_price, status, blocked),
    ).fetchone()
    log_event(conn, account_id, deployment_id, row["id"], "paper_order_submitted", "Submitted simulation-only paper order.", dict(row))
    if blocked:
        log_event(conn, account_id, deployment_id, row["id"], "paper_order_rejected", blocked, dict(row))
        conn.commit()
        return dict(row)
    filled = simulate_order_fill(conn, row["id"])
    conn.commit()
    return filled


def deployment_block_reason(conn: psycopg.Connection, account_id: int, deployment_id: int | None) -> str | None:
    if deployment_id is None:
        return None
    row = conn.execute(
        """
        SELECT id, account_id, status, simulation_only
        FROM strategy_deployments
        WHERE id = %s
        """,
        (deployment_id,),
    ).fetchone()
    if not row:
        return "Strategy deployment not found."
    if int(row["account_id"]) != int(account_id):
        return "Strategy deployment does not belong to this paper account."
    if row["status"] != "active":
        return "Strategy deployment is not active; paused deployments cannot create paper orders."
    if not row["simulation_only"]:
        return "Strategy deployment is not simulation-only."
    return None


def risk_block_reason(
    conn: psycopg.Connection,
    account: dict[str, Any],
    symbol: str,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    limit_price: Decimal | None,
) -> str | None:
    if account["status"] != "active":
        return "Paper account is not active."
    price = limit_price or reference_price
    notional = quantity * Decimal(price)
    cash = Decimal(account["cash_balance"])
    if side == "buy":
        if notional * (Decimal("1") + FEE_RATE + SLIPPAGE_RATE) > cash:
            return "Insufficient paper cash; leverage is disabled."
        if notional > cash * MAX_ORDER_NOTIONAL_FRACTION:
            return "Order exceeds max simulation risk allocation."
    if side == "sell":
        position = get_position(conn, int(account["id"]), symbol)
        if quantity > Decimal(position.get("quantity") or 0):
            return "Cannot sell more than simulated long position; shorting is disabled."
    return None


def simulate_order_fill(conn: psycopg.Connection, order_id: int) -> dict[str, Any]:
    order = conn.execute("SELECT * FROM paper_orders WHERE id = %s", (order_id,)).fetchone()
    if not order:
        raise PaperTradingError("paper order not found")
    if order["status"] != "pending":
        return dict(order)
    candle = latest_candle(conn, order["symbol"], order["timeframe"])
    fill_price = simulated_fill_price(order, candle)
    if fill_price is None:
        return dict(order)
    fill = apply_fill(conn, dict(order), candle, fill_price)
    conn.execute("UPDATE paper_orders SET status = 'filled', filled_at = %s WHERE id = %s", (fill["filled_at"], order_id))
    log_event(conn, order["account_id"], order["deployment_id"], order_id, "paper_order_filled", "Paper order filled from historical candle data.", fill)
    record_equity_snapshot(conn, order["account_id"])
    return dict(conn.execute("SELECT * FROM paper_orders WHERE id = %s", (order_id,)).fetchone())


def simulated_fill_price(order: dict[str, Any], candle: dict[str, Any]) -> Decimal | None:
    close = Decimal(candle["close"])
    if order["order_type"] == "market":
        return close * (Decimal("1") + SLIPPAGE_RATE if order["side"] == "buy" else Decimal("1") - SLIPPAGE_RATE)
    limit = Decimal(order["limit_price"])
    if order["side"] == "buy" and Decimal(candle["low"]) <= limit:
        return limit
    if order["side"] == "sell" and Decimal(candle["high"]) >= limit:
        return limit
    return None


def apply_fill(conn: psycopg.Connection, order: dict[str, Any], candle: dict[str, Any], fill_price: Decimal) -> dict[str, Any]:
    quantity = Decimal(order["quantity"])
    gross = quantity * fill_price
    fee = gross * FEE_RATE
    side = order["side"]
    position = get_position(conn, order["account_id"], order["symbol"])
    current_qty = Decimal(position.get("quantity") or 0)
    avg_price = Decimal(position.get("average_price") or 0)
    realized = Decimal(position.get("realized_pnl") or 0)
    if side == "buy":
        new_qty = current_qty + quantity
        new_avg = ((current_qty * avg_price) + gross) / new_qty
        cash_delta = -(gross + fee)
        realized_delta = Decimal("0")
    else:
        if quantity > current_qty:
            raise PaperTradingError("Cannot sell more than simulated long position.")
        new_qty = current_qty - quantity
        realized_delta = (fill_price - avg_price) * quantity - fee
        realized += realized_delta
        new_avg = Decimal("0") if new_qty == 0 else avg_price
        cash_delta = gross - fee
    fill = conn.execute(
        """
        INSERT INTO paper_fills(order_id, account_id, symbol, side, quantity, fill_price, gross_amount, fee, slippage, candle_timestamp, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (order["id"], order["account_id"], order["symbol"], side, quantity, fill_price, gross, fee, gross * SLIPPAGE_RATE, candle["timestamp"]),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO paper_positions(account_id, symbol, quantity, average_price, realized_pnl, simulation_only)
        VALUES (%s, %s, %s, %s, %s, TRUE)
        ON CONFLICT(account_id, symbol) DO UPDATE
        SET quantity = EXCLUDED.quantity,
            average_price = EXCLUDED.average_price,
            realized_pnl = EXCLUDED.realized_pnl,
            updated_at = NOW()
        """,
        (order["account_id"], order["symbol"], new_qty, new_avg, realized),
    )
    conn.execute(
        """
        UPDATE paper_accounts
        SET cash_balance = cash_balance + %s,
            realized_pnl = realized_pnl + %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (cash_delta, realized_delta, order["account_id"]),
    )
    return dict(fill)


def create_deployment(
    conn: psycopg.Connection,
    account_id: int,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    strategy_version: str = "v1",
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    get_account(conn, account_id)
    row = conn.execute(
        """
        INSERT INTO strategy_deployments(account_id, strategy_name, strategy_version, symbol, timeframe, parameters, status, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, 'active', TRUE)
        RETURNING *
        """,
        (account_id, strategy_name, strategy_version, symbol.upper(), timeframe, Jsonb(parameters or {})),
    ).fetchone()
    log_event(conn, account_id, row["id"], None, "paper_deployment_created", "Created simulation-only strategy deployment.", dict(row))
    conn.commit()
    return dict(row)


def pause_deployment(conn: psycopg.Connection, deployment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        UPDATE strategy_deployments
        SET status = 'paused', paused_at = NOW(), updated_at = NOW()
        WHERE id = %s
        RETURNING *
        """,
        (deployment_id,),
    ).fetchone()
    if not row:
        raise PaperTradingError("strategy deployment not found")
    log_event(conn, row["account_id"], row["id"], None, "paper_deployment_paused", "Paused simulation-only strategy deployment.", dict(row))
    conn.commit()
    return dict(row)


def list_deployments(conn: psycopg.Connection, account_id: int | None = None) -> list[dict[str, Any]]:
    if account_id:
        return list(conn.execute("SELECT * FROM strategy_deployments WHERE account_id = %s ORDER BY created_at DESC", (account_id,)).fetchall())
    return list(conn.execute("SELECT * FROM strategy_deployments ORDER BY created_at DESC").fetchall())


def get_position(conn: psycopg.Connection, account_id: int, symbol: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM paper_positions WHERE account_id = %s AND symbol = %s", (account_id, symbol.upper())).fetchone()
    return dict(row) if row else {"account_id": account_id, "symbol": symbol.upper(), "quantity": Decimal("0"), "average_price": Decimal("0"), "realized_pnl": Decimal("0")}


def list_positions(conn: psycopg.Connection, account_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM paper_positions WHERE account_id = %s ORDER BY symbol", (account_id,)).fetchall()
    return [with_market_value(conn, dict(row)) for row in rows]


def with_market_value(conn: psycopg.Connection, position: dict[str, Any]) -> dict[str, Any]:
    if Decimal(position.get("quantity") or 0) == 0:
        return {**position, "last_price": Decimal("0"), "market_value": Decimal("0"), "unrealized_pnl": Decimal("0")}
    candle = latest_candle(conn, position["symbol"], "1d", allow_any_timeframe=True)
    last = Decimal(candle["close"])
    market_value = Decimal(position["quantity"]) * last
    unrealized = (last - Decimal(position["average_price"])) * Decimal(position["quantity"])
    return {**position, "last_price": last, "market_value": market_value, "unrealized_pnl": unrealized}


def list_orders(conn: psycopg.Connection, account_id: int) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_orders WHERE account_id = %s ORDER BY submitted_at DESC", (account_id,)).fetchall())


def list_fills(conn: psycopg.Connection, account_id: int) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_fills WHERE account_id = %s ORDER BY filled_at DESC", (account_id,)).fetchall())


def list_equity_curve(conn: psycopg.Connection, account_id: int) -> list[dict[str, Any]]:
    return list(conn.execute("SELECT * FROM paper_equity_curve WHERE account_id = %s ORDER BY timestamp ASC", (account_id,)).fetchall())


def account_balances(conn: psycopg.Connection, account_id: int) -> dict[str, Any]:
    account = get_account(conn, account_id)
    positions = list_positions(conn, account_id)
    market_value = sum(Decimal(row["market_value"]) for row in positions)
    unrealized = sum(Decimal(row["unrealized_pnl"]) for row in positions)
    equity = Decimal(account["cash_balance"]) + market_value
    return {**account, "market_value": market_value, "unrealized_pnl": unrealized, "equity": equity}


def record_equity_snapshot(conn: psycopg.Connection, account_id: int) -> dict[str, Any]:
    balances = account_balances(conn, account_id)
    row = conn.execute(
        """
        INSERT INTO paper_equity_curve(account_id, cash_balance, equity, unrealized_pnl, realized_pnl, simulation_only)
        VALUES (%s, %s, %s, %s, %s, TRUE)
        RETURNING *
        """,
        (account_id, balances["cash_balance"], balances["equity"], balances["unrealized_pnl"], balances["realized_pnl"]),
    ).fetchone()
    return dict(row)


def latest_candle(conn: psycopg.Connection, symbol: str, timeframe: str, allow_any_timeframe: bool = False) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT symbol, timeframe, timestamp, open, high, low, close, volume
        FROM candles
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (symbol.upper(), timeframe),
    ).fetchone()
    if not row and allow_any_timeframe:
        row = conn.execute(
            """
            SELECT symbol, timeframe, timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
    if not row:
        raise PaperTradingError("No candle data available for paper fill simulation.")
    return dict(row)


def log_event(conn: psycopg.Connection, account_id: int | None, deployment_id: int | None, order_id: int | None, event_type: str, message: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO execution_logs(account_id, deployment_id, order_id, event_type, message, payload, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """,
        (account_id, deployment_id, order_id, event_type, message, Jsonb(payload)),
    )
