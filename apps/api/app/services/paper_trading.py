from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb

from app.providers.alpaca import sync_alpaca_candles
from app.services.evidence_alerts import candle_is_stale, detect_paper_scan_alert
from app.services.features import load_candles, sync_features
from app.services.strategy import StrategyDecision, StrategyDefinition, get_strategy_definition
from app.services.strategy_diagnostics import enrich_decision, persist_strategy_evaluation
from app.settings import settings

FEE_RATE = Decimal("0.001")
SLIPPAGE_RATE = Decimal("0.0005")
MAX_ORDER_NOTIONAL_FRACTION = Decimal("0.25")
PAPER_SCAN_CANDLE_LIMIT = 5000


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
    stop_loss_price: Decimal | None = None,
    take_profit_price: Decimal | None = None,
    campaign_id: int | None = None,
    candidate_id: str | None = None,
    strategy_id: str | None = None,
    strategy_version: str | None = None,
    decision_id: str | None = None,
    signal_timestamp: Any | None = None,
) -> dict[str, Any]:
    ensure_forward_lineage_columns(conn)
    account = get_account(conn, account_id)
    deployment_block = deployment_block_reason(conn, account_id, deployment_id)
    deployment = get_deployment(conn, deployment_id) if deployment_id is not None else None
    campaign_id = campaign_id if campaign_id is not None else (deployment or {}).get("campaign_id")
    candidate_id = candidate_id or (deployment or {}).get("candidate_id")
    strategy_id = strategy_id or (deployment or {}).get("strategy_id")
    strategy_version = strategy_version or (deployment or {}).get("strategy_version")
    side = side.lower()
    order_type = order_type.lower()
    if side not in {"buy", "sell"}:
        raise PaperTradingError("paper trading supports buy/sell simulation only")
    if order_type not in {"market", "limit"}:
        raise PaperTradingError("paper trading supports market and limit simulation only")
    if quantity <= 0:
        raise PaperTradingError("quantity must be positive")
    direction = str((deployment or {}).get("strategy_direction") or "long")
    if side == "sell" and (stop_loss_price is not None or take_profit_price is not None) and direction != "short":
        raise PaperTradingError("protective exits on simulated sell entries require a short deployment")
    if stop_loss_price is not None and stop_loss_price <= 0:
        raise PaperTradingError("stop_loss_price must be positive")
    if take_profit_price is not None and take_profit_price <= 0:
        raise PaperTradingError("take_profit_price must be positive")
    latest = latest_candle(conn, symbol, timeframe)
    blocked = deployment_block or risk_block_reason(conn, account, symbol, side, quantity, latest["close"], limit_price, deployment_id=deployment_id)
    status = "rejected" if blocked else "pending"
    row = conn.execute(
        """
        INSERT INTO paper_orders(account_id, deployment_id, symbol, timeframe, side, order_type, quantity, limit_price, status, rejected_reason, stop_loss_price, take_profit_price, simulation_only, campaign_id, candidate_id, strategy_id, strategy_version, decision_id, signal_timestamp, evidence_origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            account_id,
            deployment_id,
            symbol.upper(),
            timeframe,
            side,
            order_type,
            quantity,
            limit_price,
            status,
            blocked,
            stop_loss_price,
            take_profit_price,
            campaign_id,
            candidate_id,
            strategy_id,
            strategy_version,
            decision_id,
            signal_timestamp,
            "candidate_forward_validation" if candidate_id and deployment_id else "manual_simulation",
        ),
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


def ensure_forward_lineage_columns(conn: psycopg.Connection) -> None:
    return None


def get_deployment(conn: psycopg.Connection, deployment_id: int | None) -> dict[str, Any] | None:
    if deployment_id is None:
        return None
    row = conn.execute("SELECT * FROM strategy_deployments WHERE id = %s", (deployment_id,)).fetchone()
    return dict(row) if row else None


def risk_block_reason(
    conn: psycopg.Connection,
    account: dict[str, Any],
    symbol: str,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    limit_price: Decimal | None,
    deployment_id: int | None = None,
) -> str | None:
    if account["status"] != "active":
        return "Paper account is not active."
    price = limit_price or reference_price
    notional = quantity * Decimal(price)
    cash = Decimal(account["cash_balance"])
    position = get_position(conn, int(account["id"]), symbol)
    current_qty = Decimal(position.get("quantity") or 0)
    deployment = get_deployment(conn, deployment_id) if deployment_id is not None else None
    direction = str((deployment or {}).get("strategy_direction") or "long")
    if side == "buy":
        is_short_cover = current_qty < 0
        if is_short_cover and quantity > abs(current_qty):
            return "Cannot cover more than the simulated short position."
        if not is_short_cover and notional * (Decimal("1") + FEE_RATE + SLIPPAGE_RATE) > cash:
            return "Insufficient paper cash; leverage is disabled."
        if not is_short_cover and notional > cash * MAX_ORDER_NOTIONAL_FRACTION:
            return "Order exceeds max simulation risk allocation."
    if side == "sell":
        is_short_entry = current_qty <= 0 and direction == "short"
        if current_qty > 0 and quantity > current_qty:
            return "Cannot sell more than simulated long position; shorting is disabled."
        if current_qty <= 0 and not is_short_entry:
            return "Opening a simulated short requires an internal-only short deployment."
        if is_short_entry and notional > cash * MAX_ORDER_NOTIONAL_FRACTION:
            return "Short order exceeds max simulation risk allocation."
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
    account = get_account(conn, order["account_id"])
    blocked = risk_block_reason(conn, account, order["symbol"], order["side"], Decimal(order["quantity"]), fill_price, fill_price, deployment_id=order.get("deployment_id"))
    if blocked:
        rejected = conn.execute(
            "UPDATE paper_orders SET status = 'rejected', rejected_reason = %s WHERE id = %s RETURNING *",
            (blocked, order_id),
        ).fetchone()
        log_event(conn, order["account_id"], order["deployment_id"], order_id, "paper_order_rejected", f"Pending order failed fill-time risk check: {blocked}", dict(rejected))
        return dict(rejected)
    fill = apply_fill(conn, dict(order), candle, fill_price)
    conn.execute("UPDATE paper_orders SET status = 'filled', filled_at = %s WHERE id = %s", (fill["filled_at"], order_id))
    log_event(conn, order["account_id"], order["deployment_id"], order_id, "paper_order_filled", "Paper order filled from historical candle data.", fill)
    if order.get("stop_loss_price") or order.get("take_profit_price"):
        create_protective_orders(conn, dict(order))
    if order.get("parent_order_id"):
        cancel_protective_sibling(conn, dict(order))
    record_equity_snapshot(conn, order["account_id"])
    return dict(conn.execute("SELECT * FROM paper_orders WHERE id = %s", (order_id,)).fetchone())


def simulated_fill_price(order: dict[str, Any], candle: dict[str, Any]) -> Decimal | None:
    close = Decimal(candle["close"])
    if order["order_type"] == "market":
        return close * (Decimal("1") + SLIPPAGE_RATE if order["side"] == "buy" else Decimal("1") - SLIPPAGE_RATE)
    if order["order_type"] == "stop_loss":
        trigger = Decimal(order["trigger_price"])
        touched = Decimal(candle["low"]) <= trigger if order["side"] == "sell" else Decimal(candle["high"]) >= trigger
        return trigger if touched else None
    if order["order_type"] == "take_profit":
        trigger = Decimal(order["trigger_price"])
        touched = Decimal(candle["high"]) >= trigger if order["side"] == "sell" else Decimal(candle["low"]) <= trigger
        return trigger if touched else None
    limit = Decimal(order["limit_price"])
    if order["side"] == "buy" and Decimal(candle["low"]) <= limit:
        return limit
    if order["side"] == "sell" and Decimal(candle["high"]) >= limit:
        return limit
    return None


def create_protective_orders(conn: psycopg.Connection, parent: dict[str, Any]) -> list[dict[str, Any]]:
    created = []
    exit_side = "sell" if parent["side"] == "buy" else "buy"
    for order_type, price_key in (("stop_loss", "stop_loss_price"), ("take_profit", "take_profit_price")):
        trigger = parent.get(price_key)
        if trigger is None:
            continue
        row = conn.execute(
            """
            INSERT INTO paper_orders(account_id, deployment_id, symbol, timeframe, side, order_type, quantity, trigger_price, parent_order_id, status, simulation_only, campaign_id, candidate_id, strategy_id, strategy_version, decision_id, signal_timestamp, evidence_origin)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', TRUE, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                parent["account_id"],
                parent.get("deployment_id"),
                parent["symbol"],
                parent["timeframe"],
                exit_side,
                order_type,
                parent["quantity"],
                trigger,
                parent["id"],
                parent.get("campaign_id"),
                parent.get("candidate_id"),
                parent.get("strategy_id"),
                parent.get("strategy_version"),
                parent.get("decision_id"),
                parent.get("signal_timestamp"),
                parent.get("evidence_origin") or "manual_simulation",
            ),
        ).fetchone()
        created.append(dict(row))
        log_event(conn, parent["account_id"], parent.get("deployment_id"), row["id"], "protective_order_created", f"Created {order_type.replace('_', ' ')} protective order.", dict(row))
    return created


def cancel_protective_sibling(conn: psycopg.Connection, filled_order: dict[str, Any]) -> None:
    rows = conn.execute(
        """
        UPDATE paper_orders SET status = 'canceled'
        WHERE parent_order_id = %s AND id <> %s AND status = 'pending'
        RETURNING *
        """,
        (filled_order["parent_order_id"], filled_order["id"]),
    ).fetchall()
    for row in rows:
        log_event(conn, row["account_id"], row["deployment_id"], row["id"], "protective_order_canceled", "Canceled OCO sibling after protective fill.", dict(row))


def cancel_order(conn: psycopg.Connection, order_id: int) -> dict[str, Any]:
    row = conn.execute(
        "UPDATE paper_orders SET status = 'canceled' WHERE id = %s AND status = 'pending' RETURNING *",
        (order_id,),
    ).fetchone()
    if not row:
        existing = conn.execute("SELECT * FROM paper_orders WHERE id = %s", (order_id,)).fetchone()
        if not existing:
            raise PaperTradingError("paper order not found")
        raise PaperTradingError("only pending paper orders can be canceled")
    log_event(conn, row["account_id"], row["deployment_id"], row["id"], "paper_order_canceled", "Canceled pending simulation-only order.", dict(row))
    conn.commit()
    return dict(row)


def process_pending_orders(conn: psycopg.Connection, account_id: int | None = None) -> dict[str, Any]:
    if account_id is None:
        rows = conn.execute("SELECT * FROM paper_orders WHERE status = 'pending' ORDER BY submitted_at, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM paper_orders WHERE status = 'pending' AND account_id = %s ORDER BY submitted_at, id", (account_id,)).fetchall()
    results = []
    for row in rows:
        results.append(simulate_order_fill(conn, row["id"]))
    conn.commit()
    filled = sum(1 for row in results if row["status"] == "filled")
    return {"processed": len(results), "filled": filled, "pending": len(results) - filled}


def apply_fill(conn: psycopg.Connection, order: dict[str, Any], candle: dict[str, Any], fill_price: Decimal) -> dict[str, Any]:
    quantity = Decimal(order["quantity"])
    gross = quantity * fill_price
    fee = gross * FEE_RATE
    side = order["side"]
    position = get_position(conn, order["account_id"], order["symbol"])
    current_qty = Decimal(position.get("quantity") or 0)
    avg_price = Decimal(position.get("average_price") or 0)
    realized = Decimal(position.get("realized_pnl") or 0)
    if side == "buy" and current_qty >= 0:
        new_qty = current_qty + quantity
        new_avg = ((current_qty * avg_price) + gross) / new_qty
        cash_delta = -(gross + fee)
        realized_delta = Decimal("0")
    elif side == "buy":
        if quantity > abs(current_qty):
            raise PaperTradingError("Cannot cover more than simulated short position.")
        new_qty = current_qty + quantity
        realized_delta = (avg_price - fill_price) * quantity - fee
        realized += realized_delta
        new_avg = Decimal("0") if new_qty == 0 else avg_price
        cash_delta = -(gross + fee)
    elif current_qty > 0:
        if quantity > current_qty:
            raise PaperTradingError("Cannot sell more than simulated long position.")
        new_qty = current_qty - quantity
        realized_delta = (fill_price - avg_price) * quantity - fee
        realized += realized_delta
        new_avg = Decimal("0") if new_qty == 0 else avg_price
        cash_delta = gross - fee
    else:
        new_qty = current_qty - quantity
        prior_abs = abs(current_qty)
        new_avg = ((prior_abs * avg_price) + gross) / abs(new_qty)
        cash_delta = gross - fee
        realized_delta = Decimal("0")
    fill = conn.execute(
        """
        INSERT INTO paper_fills(order_id, account_id, symbol, side, quantity, fill_price, gross_amount, fee, slippage, candle_timestamp, simulation_only, campaign_id, candidate_id, deployment_id, strategy_id, strategy_version, decision_id, signal_timestamp, evidence_origin)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            order["id"],
            order["account_id"],
            order["symbol"],
            side,
            quantity,
            fill_price,
            gross,
            fee,
            gross * SLIPPAGE_RATE,
            candle["timestamp"],
            order.get("campaign_id"),
            order.get("candidate_id"),
            order.get("deployment_id"),
            order.get("strategy_id"),
            order.get("strategy_version"),
            order.get("decision_id"),
            order.get("signal_timestamp"),
            order.get("evidence_origin") or "manual_simulation",
        ),
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
    campaign_id: int | None = None,
    candidate_id: str | None = None,
    strategy_id: str | None = None,
    forward_validation_started_at: Any | None = None,
    evidence_version: str | None = None,
    lifecycle_state: str = "manual_simulation",
    deployment_origin: str = "manual_simulation",
    strategy_direction: str = "long",
    execution_capability: str | None = None,
) -> dict[str, Any]:
    ensure_forward_lineage_columns(conn)
    get_account(conn, account_id)
    row = conn.execute(
        """
        INSERT INTO strategy_deployments(account_id, strategy_name, strategy_version, symbol, timeframe, parameters, status, simulation_only, campaign_id, candidate_id, strategy_id, forward_validation_started_at, evidence_version, lifecycle_state, deployment_origin, strategy_direction, execution_capability)
        VALUES (%s, %s, %s, %s, %s, %s, 'active', TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            account_id,
            strategy_name,
            strategy_version,
            symbol.upper(),
            timeframe,
            Jsonb(parameters or {}),
            campaign_id,
            candidate_id,
            strategy_id or f"{strategy_name}_{strategy_version}",
            forward_validation_started_at,
            evidence_version,
            lifecycle_state,
            deployment_origin,
            strategy_direction,
            execution_capability or ("internal_only" if strategy_direction == "short" else "external_observe"),
        ),
    ).fetchone()
    log_event(conn, account_id, row["id"], None, "paper_deployment_created", "Created simulation-only strategy deployment.", dict(row))
    conn.commit()
    return dict(row)


def find_active_deployment(conn: psycopg.Connection, account_id: int, strategy_name: str, strategy_version: str, symbol: str, timeframe: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM strategy_deployments
        WHERE account_id = %s
          AND strategy_name = %s
          AND strategy_version = %s
          AND symbol = %s
          AND timeframe = %s
          AND status = 'active'
          AND simulation_only = TRUE
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (account_id, strategy_name, strategy_version, symbol.upper(), timeframe),
    ).fetchone()
    return dict(row) if row else None


def ensure_tsla_momentum_bull_deployment(conn: psycopg.Connection, account_id: int) -> dict[str, Any]:
    strategy = get_strategy_definition("momentum", "bull_v2")
    existing = find_active_deployment(conn, account_id, strategy.name, strategy.version, "TSLA", "1h")
    if existing:
        log_event(conn, account_id, existing["id"], None, "paper_deployment_exists", "TSLA 1h momentum_bull_v2 deployment already active.", existing)
        conn.commit()
        return existing
    return create_deployment(
        conn,
        account_id=account_id,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        symbol="TSLA",
        timeframe="1h",
        parameters=strategy.parameters,
    )


async def run_deployment_scan(
    conn: psycopg.Connection,
    deployment_id: int,
    *,
    synchronize_market_data: bool = True,
) -> dict[str, Any]:
    deployment = get_deployment(conn, deployment_id)
    if deployment["status"] != "active":
        raise PaperTradingError("only active simulation deployments can be scanned")
    if not deployment["simulation_only"]:
        raise PaperTradingError("deployment is not simulation-only")

    account_id = int(deployment["account_id"])
    symbol = deployment["symbol"]
    timeframe = deployment["timeframe"]
    sync_result = await sync_latest_deployment_candles(conn, symbol, timeframe) if synchronize_market_data else None
    if synchronize_market_data and is_candidate_linked_deployment(deployment):
        await sync_candidate_context_candles(conn, deployment)
    feature_result = (
        sync_features(conn, symbol=symbol, timeframe=timeframe)
        if synchronize_market_data
        else {"symbol": symbol, "timeframe": timeframe, "shared_scheduler_snapshot": True, "upserted": 0}
    )
    deployment = get_deployment(conn, deployment_id)
    candle = latest_candle(conn, symbol, timeframe)
    sync_payload = {
        "provider": sync_result.provider if sync_result else "scheduler_shared_sync",
        "received": sync_result.received if sync_result else 0,
        "upserted": sync_result.upserted if sync_result else 0,
        "first_timestamp": sync_result.first_timestamp if sync_result else None,
        "last_timestamp": sync_result.last_timestamp if sync_result else candle.get("timestamp"),
    }
    if candle_is_stale(candle):
        candle_timestamp = candle.get("timestamp")
        age_hours = None
        if candle_timestamp:
            parsed_timestamp = candle_timestamp
            if isinstance(parsed_timestamp, str):
                parsed_timestamp = datetime.fromisoformat(parsed_timestamp.replace("Z", "+00:00"))
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.replace(tzinfo=UTC)
            age_hours = round((datetime.now(UTC) - parsed_timestamp).total_seconds() / 3600, 2)
        message = "Latest stored candle is stale; skipped strategy evaluation, simulated order creation, and stale candle fills."
        payload = {
            "action": "stale_data_warning",
            "candle_timestamp": candle_timestamp,
            "candle_age_hours": age_hours,
            "max_candle_age_hours": settings.paper_scan_max_candle_age_hours,
            "sync": sync_payload,
            "features": feature_result,
            "processed_pending": {"processed": 0, "filled": 0, "canceled": 0, "skipped": True, "reason": "stale_data_gate"},
            "simulation_only": True,
        }
        log_event(conn, account_id, deployment_id, None, "paper_scan_stale_data_skipped", message, payload)
        safely_detect_paper_scan_alert(conn, deployment=deployment, decision={"signal": "skipped"}, candle=candle, action="stale_data_warning", message=message)
        updated = update_deployment_scan_state(conn, deployment_id, "stale_data_warning", message, payload)
        reconcile = reconcile_account(conn, account_id, repair=False)
        refresh_signal_review(conn, deployment)
        conn.commit()
        return {
            "deployment": updated,
            "action": "stale_data_warning",
            "message": message,
            "decision": {"signal": "skipped", "explanation": [message]},
            "sync": sync_payload,
            "features": feature_result,
            "processed_pending": payload["processed_pending"],
            "order": None,
            "position": get_position(conn, account_id, symbol),
            "reconciliation": reconcile,
            "simulation_only": True,
        }

    if not candle_is_forward_eligible_for_deployment(deployment, candle):
        message = "Waiting for the first completed candle after forward validation started."
        payload = {
            "action": "awaiting_forward_candle",
            "candle_timestamp": candle.get("timestamp"),
            "forward_validation_started_at": deployment.get("forward_validation_started_at"),
            "sync": sync_payload,
            "features": feature_result,
            "processed_pending": {"processed": 0, "filled": 0, "canceled": 0, "skipped": True, "reason": "pre_forward_candle_gate"},
            "simulation_only": True,
        }
        log_event(conn, account_id, deployment_id, None, "paper_scan_awaiting_forward_candle", message, payload)
        updated = update_deployment_scan_state(conn, deployment_id, "awaiting_forward_candle", message, payload)
        conn.commit()
        return {
            "deployment": updated,
            "action": "awaiting_forward_candle",
            "message": message,
            "decision": {"signal": "skipped", "explanation": [message]},
            "sync": sync_payload,
            "features": feature_result,
            "processed_pending": payload["processed_pending"],
            "order": None,
            "position": get_position(conn, account_id, symbol),
            "reconciliation": reconcile_account(conn, account_id, repair=False),
            "simulation_only": True,
        }

    processed = process_pending_orders(conn, account_id)
    claimed = claim_deployment_candle_scan(conn, deployment_id, candle["timestamp"])
    if not claimed:
        message = "Deployment already scanned this candle; skipped duplicate paper decision."
        payload = {
            "action": "skipped_duplicate_candle",
            "candle_timestamp": candle["timestamp"],
            "sync": sync_payload,
            "features": feature_result,
            "processed_pending": processed,
            "simulation_only": True,
        }
        log_event(conn, account_id, deployment_id, None, "paper_scan_duplicate_candle_skipped", message, payload)
        safely_detect_paper_scan_alert(conn, deployment=deployment, decision={"signal": "skipped"}, candle=candle, action="skipped_duplicate_candle", message=message, duplicate_candle=True)
        updated = update_deployment_scan_state(conn, deployment_id, "skipped", message, payload)
        reconcile = reconcile_account(conn, account_id, repair=False)
        refresh_signal_review(conn, deployment)
        conn.commit()
        return {
            "deployment": updated,
            "action": "skipped_duplicate_candle",
            "message": message,
            "decision": {"signal": "skipped", "explanation": [message]},
            "sync": payload["sync"],
            "features": feature_result,
            "processed_pending": processed,
            "order": None,
            "position": get_position(conn, account_id, symbol),
            "reconciliation": reconcile,
            "simulation_only": True,
        }
    decision = evaluate_deployment_decision(conn, deployment)
    strategy_evaluation = persist_strategy_evaluation(
        conn,
        internal_deployment_id=int(deployment["id"]),
        decision=decision,
        candle=candle,
        trace_id=uuid4(),
        configuration_fingerprint=str(deployment.get("candidate_fingerprint") or "") or None,
    )
    open_position = get_position(conn, account_id, symbol)
    pending_orders = pending_deployment_orders(conn, deployment_id)
    order = None
    action = "skipped"
    message = ""

    if decision.signal != "setup":
        message = "Strategy rules did not produce a setup on the latest stored candle."
    elif Decimal(open_position.get("quantity") or 0) > 0:
        message = "Existing simulated long position is open; skipped duplicate entry."
    elif pending_orders:
        message = "Pending simulated deployment orders already exist; skipped duplicate entry."
    else:
        order = create_deployment_order_from_decision(conn, deployment, decision)
        action = "order_created" if order["status"] != "rejected" else "order_rejected"
        message = order.get("rejected_reason") or f"Created simulated {order['status']} order from deployment scan."

    record_equity_snapshot(conn, account_id)
    payload = {
        "action": action,
        "decision": decision_payload(decision),
        "strategy_evaluation_id": strategy_evaluation["id"],
        "sync": sync_payload,
        "features": feature_result,
        "processed_pending": processed,
        "order": order,
        "position": get_position(conn, account_id, symbol),
    }
    log_event(conn, account_id, deployment_id, order.get("id") if order else None, "paper_scan_completed", message, payload)
    safely_detect_paper_scan_alert(conn, deployment=deployment, decision=decision, candle=candle, action=action, message=message)
    updated = update_deployment_scan_state(conn, deployment_id, decision.signal, message, payload)
    reconcile = reconcile_account(conn, account_id, repair=False)
    refresh_signal_review(conn, deployment)
    conn.commit()
    return {
        "deployment": updated,
        "action": action,
        "message": message,
        "decision": decision_payload(decision),
        "sync": payload["sync"],
        "features": feature_result,
        "processed_pending": processed,
        "order": order,
        "position": payload["position"],
        "reconciliation": reconcile,
        "simulation_only": True,
    }


def get_deployment(conn: psycopg.Connection, deployment_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM strategy_deployments WHERE id = %s", (deployment_id,)).fetchone()
    if not row:
        raise PaperTradingError("strategy deployment not found")
    return dict(row)


def refresh_signal_review(conn: psycopg.Connection, deployment: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from app.services.signal_reviews import generate_signal_review

        return generate_signal_review(conn, deployment)
    except Exception as error:
        log_event(
            conn,
            int(deployment["account_id"]) if deployment.get("account_id") else None,
            int(deployment["id"]) if deployment.get("id") else None,
            None,
            "signal_review_refresh_skipped",
            f"Signal review refresh skipped: {error}",
            {"simulation_only": True},
        )
        return None


def safely_detect_paper_scan_alert(conn: psycopg.Connection, **kwargs: Any) -> dict[str, Any] | None:
    try:
        return detect_paper_scan_alert(conn, **kwargs)
    except Exception as error:
        deployment = dict(kwargs.get("deployment") or {})
        log_event(
            conn,
            int(deployment["account_id"]) if deployment.get("account_id") else None,
            int(deployment["id"]) if deployment.get("id") else None,
            None,
            "paper_scan_alert_refresh_skipped",
            f"Paper scan alert refresh skipped: {error}",
            {"simulation_only": True},
        )
        return None


def claim_deployment_candle_scan(conn: psycopg.Connection, deployment_id: int, candle_timestamp: Any) -> dict[str, Any] | None:
    row = conn.execute(
        """
        UPDATE strategy_deployments
        SET last_scanned_candle_timestamp = %s,
            updated_at = NOW()
        WHERE id = %s
          AND status = 'active'
          AND simulation_only = TRUE
          AND (
              last_scanned_candle_timestamp IS NULL
              OR last_scanned_candle_timestamp <> %s
          )
        RETURNING *
        """,
        (candle_timestamp, deployment_id, candle_timestamp),
    ).fetchone()
    return dict(row) if row else None


async def sync_latest_deployment_candles(conn: psycopg.Connection, symbol: str, timeframe: str):
    try:
        return await sync_alpaca_candles(conn, symbol=symbol, timeframe=timeframe, limit=PAPER_SCAN_CANDLE_LIMIT)
    except (RuntimeError, ValueError) as error:
        raise PaperTradingError(str(error)) from error


async def sync_candidate_context_candles(conn: psycopg.Connection, deployment: dict[str, Any]) -> None:
    candidate = candidate_payload_for_deployment(conn, deployment)
    parameters = dict(candidate.get("parameters") or {})
    if parameters.get("strategy_architecture") != "relative_strength_continuation_v2":
        return
    for symbol in ("SPY", "QQQ"):
        await sync_latest_deployment_candles(conn, symbol, deployment["timeframe"])
        sync_features(conn, symbol=symbol, timeframe=deployment["timeframe"])


def evaluate_deployment_decision(conn: psycopg.Connection, deployment: dict[str, Any]) -> StrategyDecision:
    strategy = strategy_definition_for_deployment(conn, deployment)
    candles = load_candles(conn, symbol=deployment["symbol"], timeframe=deployment["timeframe"])
    if not candles:
        raise PaperTradingError("No candle data available for deployment scan.")
    features = conn.execute(
        """
        SELECT *
        FROM features
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp ASC
        """,
        (deployment["symbol"], deployment["timeframe"]),
    ).fetchall()
    if not features:
        raise PaperTradingError("No feature data available for deployment scan.")
    if is_candidate_linked_deployment(deployment):
        from app.services.research_campaigns import enrich_phase_9_11_context

        features = enrich_phase_9_11_context(conn, deployment["symbol"], deployment["timeframe"], [dict(row) for row in features])
    feature_by_time = {row["timestamp"]: row for row in features}
    latest_candle = candles[-1]
    feature = feature_by_time.get(latest_candle["timestamp"])
    if feature is None:
        raise PaperTradingError("No feature row matches the latest deployment candle.")
    params = {**strategy.parameters, **dict(deployment.get("parameters") or {})}
    return enrich_decision(strategy.decide(latest_candle, feature, candles, params), latest_candle, feature, candles, params)


def is_candidate_linked_deployment(deployment: dict[str, Any]) -> bool:
    return bool(deployment.get("campaign_id") and deployment.get("candidate_id"))


def candidate_payload_for_deployment(conn: psycopg.Connection, deployment: dict[str, Any]) -> dict[str, Any]:
    if not is_candidate_linked_deployment(deployment):
        raise PaperTradingError("deployment is not linked to a research candidate")
    row = conn.execute(
        """
        SELECT candidate
        FROM research_campaign_jobs
        WHERE campaign_id = %s
          AND candidate_id = %s
          AND simulation_only = TRUE
        ORDER BY id
        LIMIT 1
        """,
        (deployment["campaign_id"], deployment["candidate_id"]),
    ).fetchone()
    if not row or not row.get("candidate"):
        raise PaperTradingError("authoritative candidate payload was not found for deployment")
    candidate = dict(row["candidate"])
    if candidate.get("candidate_id") != deployment.get("candidate_id"):
        raise PaperTradingError("candidate payload lineage does not match deployment")
    return candidate


def strategy_definition_for_deployment(conn: psycopg.Connection, deployment: dict[str, Any]) -> StrategyDefinition:
    if deployment.get("strategy_name") != "autonomous_strategy_discovery":
        return get_strategy_definition(deployment["strategy_name"], deployment["strategy_version"])
    from app.services.research_campaigns import candidate_from_payload
    from app.services.strategy_discovery import make_strategy_definition

    candidate = candidate_from_payload(candidate_payload_for_deployment(conn, deployment))
    if candidate.candidate_id != deployment.get("strategy_version"):
        raise PaperTradingError("candidate strategy version does not match deployment lineage")
    return make_strategy_definition(candidate)


def candle_is_forward_eligible_for_deployment(deployment: dict[str, Any], candle: dict[str, Any]) -> bool:
    if not is_candidate_linked_deployment(deployment):
        return True
    started_at = deployment.get("forward_validation_started_at")
    candle_timestamp = candle.get("timestamp")
    if not started_at or not candle_timestamp:
        return False
    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    if isinstance(candle_timestamp, str):
        candle_timestamp = datetime.fromisoformat(candle_timestamp.replace("Z", "+00:00"))
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if candle_timestamp.tzinfo is None:
        candle_timestamp = candle_timestamp.replace(tzinfo=UTC)
    return candle_timestamp > started_at


def pending_deployment_orders(conn: psycopg.Connection, deployment_id: int) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT * FROM paper_orders WHERE deployment_id = %s AND status = 'pending' ORDER BY submitted_at",
            (deployment_id,),
        ).fetchall()
    )


def create_deployment_order_from_decision(conn: psycopg.Connection, deployment: dict[str, Any], decision: StrategyDecision) -> dict[str, Any]:
    if decision.stop_loss is None or decision.take_profit is None:
        raise PaperTradingError("setup decision is missing protective exits")
    account = account_balances(conn, int(deployment["account_id"]))
    latest = latest_candle(conn, deployment["symbol"], deployment["timeframe"])
    reference_price = Decimal(latest["close"])
    max_notional = Decimal(account["equity"]) * Decimal("0.10")
    quantity = max_notional / reference_price
    return create_order(
        conn,
        account_id=int(deployment["account_id"]),
        deployment_id=int(deployment["id"]),
        symbol=deployment["symbol"],
        timeframe=deployment["timeframe"],
        side="sell" if decision.direction == "short" else "buy",
        order_type="market",
        quantity=quantity,
        stop_loss_price=decision.stop_loss,
        take_profit_price=decision.take_profit,
    )


def decision_payload(decision: StrategyDecision) -> dict[str, Any]:
    return {
        "signal": decision.signal,
        "entry_zone": [str(value) for value in decision.entry_zone] if decision.entry_zone else None,
        "stop_loss": str(decision.stop_loss) if decision.stop_loss is not None else None,
        "take_profit": str(decision.take_profit) if decision.take_profit is not None else None,
        "risk_reward": str(decision.risk_reward) if decision.risk_reward is not None else None,
        "explanation": decision.explanation,
        "decision_version": decision.decision_version,
        "gates": decision.gates,
        "regime": decision.regime,
        "strategy_direction": decision.direction,
    }


def update_deployment_scan_state(conn: psycopg.Connection, deployment_id: int, signal: str, check_result: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """
        UPDATE strategy_deployments
        SET last_scan_at = NOW(),
            last_signal = %s,
            last_check_result = %s,
            last_scan_payload = %s,
            updated_at = NOW()
        WHERE id = %s
        RETURNING *
        """,
        (signal, check_result, Jsonb(jsonable_encoder(payload)), deployment_id),
    ).fetchone()
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


def list_execution_logs(conn: psycopg.Connection, account_id: int, limit: int = 200) -> list[dict[str, Any]]:
    return list(conn.execute(
        "SELECT * FROM execution_logs WHERE account_id = %s ORDER BY created_at DESC LIMIT %s",
        (account_id, limit),
    ).fetchall())


def reconcile_account(conn: psycopg.Connection, account_id: int, repair: bool = False) -> dict[str, Any]:
    account = get_account(conn, account_id)
    fills = list_fills(conn, account_id)
    expected_cash = Decimal(account["starting_cash"])
    ledgers: dict[str, dict[str, Decimal]] = {}
    for fill in reversed(fills):
        symbol = fill["symbol"]
        ledger = ledgers.setdefault(symbol, {"quantity": Decimal("0"), "average_price": Decimal("0"), "realized_pnl": Decimal("0")})
        quantity = Decimal(fill["quantity"])
        price = Decimal(fill["fill_price"])
        fee = Decimal(fill["fee"])
        gross = Decimal(fill["gross_amount"])
        if fill["side"] == "buy":
            new_quantity = ledger["quantity"] + quantity
            ledger["average_price"] = ((ledger["quantity"] * ledger["average_price"]) + gross) / new_quantity
            ledger["quantity"] = new_quantity
            expected_cash -= gross + fee
        else:
            ledger["realized_pnl"] += (price - ledger["average_price"]) * quantity - fee
            ledger["quantity"] -= quantity
            if ledger["quantity"] == 0:
                ledger["average_price"] = Decimal("0")
            expected_cash += gross - fee

    current_positions = {row["symbol"]: row for row in conn.execute("SELECT * FROM paper_positions WHERE account_id = %s", (account_id,)).fetchall()}
    issues = []
    cash_delta = expected_cash - Decimal(account["cash_balance"])
    if cash_delta != 0:
        issues.append({"type": "cash", "expected": str(expected_cash), "actual": str(account["cash_balance"]), "delta": str(cash_delta)})
    for symbol in sorted(set(ledgers) | set(current_positions)):
        expected = ledgers.get(symbol, {"quantity": Decimal("0"), "average_price": Decimal("0"), "realized_pnl": Decimal("0")})
        actual = current_positions.get(symbol, {})
        if any(Decimal(actual.get(key) or 0) != expected[key] for key in ("quantity", "average_price", "realized_pnl")):
            issues.append({"type": "position", "symbol": symbol, "expected": {key: str(value) for key, value in expected.items()}, "actual": {key: str(actual.get(key) or 0) for key in expected}})

    if repair and issues:
        conn.execute("UPDATE paper_accounts SET cash_balance = %s, realized_pnl = %s, updated_at = NOW() WHERE id = %s", (expected_cash, sum(row["realized_pnl"] for row in ledgers.values()), account_id))
        for symbol, ledger in ledgers.items():
            conn.execute(
                """INSERT INTO paper_positions(account_id, symbol, quantity, average_price, realized_pnl, simulation_only)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT(account_id, symbol) DO UPDATE SET quantity = EXCLUDED.quantity, average_price = EXCLUDED.average_price, realized_pnl = EXCLUDED.realized_pnl, updated_at = NOW()""",
                (account_id, symbol, ledger["quantity"], ledger["average_price"], ledger["realized_pnl"]),
            )
        record_equity_snapshot(conn, account_id)
        log_event(conn, account_id, None, None, "paper_account_reconciled", "Repaired paper ledger from immutable fills.", {"issue_count": len(issues)})
        conn.commit()
    return {"account_id": account_id, "healthy": not issues, "repaired": bool(repair and issues), "issue_count": len(issues), "issues": issues, "expected_cash": expected_cash}


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
    if allow_any_timeframe:
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
        if row:
            return dict(row)
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
    if not row:
        raise PaperTradingError("No candle data available for paper fill simulation.")
    return dict(row)


def log_event(conn: psycopg.Connection, account_id: int | None, deployment_id: int | None, order_id: int | None, event_type: str, message: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO execution_logs(account_id, deployment_id, order_id, event_type, message, payload, simulation_only)
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        """,
        (account_id, deployment_id, order_id, event_type, message, Jsonb(jsonable_encoder(payload))),
    )
