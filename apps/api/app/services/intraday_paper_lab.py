"""Backend-only Alpaca Paper lab for signed-imbalance curiosity tests.

This module is intentionally not wired into the elite deployment system.  It
submits fake-money Alpaca Paper orders only when the CLI passes explicit
``--submit --confirm-paper`` flags, and records every decision in separate lab
tables so the experiment can be inspected or deleted without confusing elite
research evidence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.providers.alpaca import iter_stock_trade_pages, normalize_stock_trade
from app.services.intraday_trade_flow import TradeFlowAccumulator
from app.services.intraday_trade_imbalance_calibration import load_calibration
from app.settings import settings

EXCHANGE = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
LAST_ENTRY_TIME = time(15, 30)
TIMEFRAME_MINUTES = {"30m": 30}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _et(moment: datetime) -> datetime:
    return moment.astimezone(EXCHANGE)


def _utc(session_date: date, wall_time: time) -> datetime:
    return datetime.combine(session_date, wall_time, tzinfo=EXCHANGE).astimezone(UTC)


def completed_signal_bar(now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """The most recent completed regular-session 30m bar eligible for entry."""
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    local = _et(now)
    session = local.date()
    open_utc = _utc(session, REGULAR_OPEN)
    close_utc = _utc(session, REGULAR_CLOSE)
    last_entry_utc = _utc(session, LAST_ENTRY_TIME)
    if now < open_utc + timedelta(minutes=30) or now > close_utc + timedelta(minutes=5):
        return None
    elapsed = int((now - open_utc).total_seconds() // 1800) * 1800
    bar_end = open_utc + timedelta(seconds=elapsed)
    if bar_end > last_entry_utc:
        bar_end = last_entry_utc
    bar_start = bar_end - timedelta(minutes=30)
    if bar_start < open_utc:
        return None
    return bar_start, bar_end


def load_lab_experiment(conn: psycopg.Connection, experiment_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM intraday_paper_lab_experiments WHERE id = %s",
        (experiment_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"No intraday paper lab experiment id={experiment_id}.")
    return dict(row)


def create_experiment(
    conn: psycopg.Connection,
    *,
    name: str,
    trading_date: date,
    symbols: Sequence[str],
    calibration_id: int,
    max_orders_per_day: int = 200,
    max_open_positions: int = 25,
    quantity: int = 1,
    allow_shorts: bool = True,
) -> dict[str, Any]:
    calibration = load_calibration(conn, calibration_id, require_ready=True)
    threshold = float(dict(calibration["report"]).get("threshold", {})["global_rounded_up"])
    config = {
        "environment": "alpaca_paper",
        "factor_key": "signed_trade_imbalance_continuation_v2_1bar",
        "timeframe": "30m",
        "quantity": quantity,
        "max_orders_per_day": max_orders_per_day,
        "max_open_positions": max_open_positions,
        "allow_shorts": allow_shorts,
        "short_only_if_alpaca_shortable": True,
        "no_pyramiding": "max one open position per symbol per direction",
        "flatten_before_close": True,
        "market_hours_only": True,
        "calibration_id": calibration_id,
        "threshold": threshold,
    }
    row = conn.execute(
        """
        INSERT INTO intraday_paper_lab_experiments(
            name, trading_date, factor_key, timeframe, calibration_id,
            threshold, symbols, config
        )
        VALUES (%s,%s,%s,'30m',%s,%s,%s,%s)
        RETURNING *
        """,
        (
            name,
            trading_date,
            config["factor_key"],
            calibration_id,
            threshold,
            Jsonb(sorted({symbol.upper() for symbol in symbols})),
            Jsonb(config),
        ),
    ).fetchone()
    conn.commit()
    return dict(row)


class AlpacaPaperLabClient:
    """Small direct client because this lab intentionally supports shorts."""

    def __init__(self) -> None:
        if settings.alpaca_paper_base_url.rstrip("/") != "https://paper-api.alpaca.markets":
            raise RuntimeError("Paper lab refuses non-paper Alpaca base URL.")
        if not settings.alpaca_paper_api_key or not settings.alpaca_paper_secret_key:
            raise RuntimeError("ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY are required.")
        self.headers = {
            "APCA-API-KEY-ID": settings.alpaca_paper_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_paper_secret_key,
        }

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(
            base_url=settings.alpaca_paper_base_url, timeout=30, headers=self.headers
        ) as client:
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=settings.alpaca_paper_base_url, timeout=30, headers=self.headers
        ) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            return {
                "status_code": response.status_code,
                "request_id": response.headers.get("X-Request-ID"),
                "payload": response.json() if response.content else {},
            }

    async def get_clock(self) -> dict[str, Any]:
        return await self._get("/v2/clock")

    async def get_asset(self, symbol: str) -> dict[str, Any]:
        return await self._get(f"/v2/assets/{symbol.upper()}")

    async def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        client_order_id: str,
    ) -> dict[str, Any]:
        return await self._post(
            "/v2/orders",
            {
                "symbol": symbol.upper(),
                "side": side,
                "type": "market",
                "qty": str(quantity),
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )


async def _bar_trade_flow(
    symbol: str,
    *,
    bar_start: datetime,
    bar_end: datetime,
    feed: str,
) -> dict[str, Any] | None:
    accumulator = TradeFlowAccumulator(symbol=symbol, timeframe="30m", feed=feed)
    async for page, _meta in iter_stock_trade_pages(
        symbol,
        start=bar_start,
        end=bar_end,
        feed=feed,
        rate_limit_retries=8,
        rate_limit_base_sleep=20,
        request_pause_seconds=0,
        max_pages=80,
    ):
        normalized = [
            row
            for row in (normalize_stock_trade(symbol, item, feed=feed) for item in page)
            if row is not None
        ]
        accumulator.add(normalized)
    rows = [row for row in accumulator.bars() if row["timestamp"] == bar_start]
    return rows[0] if rows else None


def _open_position_exists(
    conn: psycopg.Connection,
    *,
    experiment_id: int,
    symbol: str,
    side: str,
) -> bool:
    row = conn.execute(
        """
        SELECT id FROM intraday_paper_lab_positions
        WHERE experiment_id = %s AND symbol = %s AND side = %s
          AND status IN ('open', 'closing')
        LIMIT 1
        """,
        (experiment_id, symbol.upper(), side),
    ).fetchone()
    return bool(row)


def _orders_today(conn: psycopg.Connection, experiment_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM intraday_paper_lab_decisions
        WHERE experiment_id = %s
          AND client_order_id IS NOT NULL
        """,
        (experiment_id,),
    ).fetchone()
    return int((row or {}).get("count") or 0)


def _open_positions(conn: psycopg.Connection, experiment_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM intraday_paper_lab_positions
        WHERE experiment_id = %s AND status IN ('open', 'closing')
        """,
        (experiment_id,),
    ).fetchone()
    return int((row or {}).get("count") or 0)


def _existing_bar_decision(
    conn: psycopg.Connection,
    *,
    experiment_id: int,
    symbol: str,
    signal_start: datetime,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM intraday_paper_lab_decisions
        WHERE experiment_id = %s AND symbol = %s AND signal_bar_start = %s
          AND action IN ('enter', 'skip', 'error')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (experiment_id, symbol.upper(), signal_start),
    ).fetchone()
    return dict(row) if row else None


async def run_cycle(
    conn: psycopg.Connection,
    *,
    experiment_id: int,
    submit: bool,
    confirm_paper: bool,
    now: datetime | None = None,
    bar_start: datetime | None = None,
    feed: str = "sip",
) -> dict[str, Any]:
    experiment = load_lab_experiment(conn, experiment_id)
    config = dict(experiment["config"] or {})
    if submit and not confirm_paper:
        raise ValueError("Submitting requires --confirm-paper.")
    if submit and settings.alpaca_paper_base_url.rstrip("/") != "https://paper-api.alpaca.markets":
        raise ValueError("Refusing to submit: Alpaca base URL is not paper.")

    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    selected = (
        (bar_start, bar_start + timedelta(minutes=30))
        if bar_start is not None
        else completed_signal_bar(now)
    )
    client = AlpacaPaperLabClient() if submit else None

    exits = await flatten_due_positions(
        conn, experiment=experiment, client=client, submit=submit, now=now
    )
    if selected is None:
        return {"experiment_id": experiment_id, "status": "outside_market_hours", "exits": exits}
    signal_start, signal_end = selected
    if _et(signal_end).date() != experiment["trading_date"]:
        return {
            "experiment_id": experiment_id,
            "status": "wrong_trading_date",
            "bar": {"start": signal_start, "end": signal_end},
            "exits": exits,
        }
    local_start = _et(signal_start).time()
    local_end = _et(signal_end).time()
    if local_start < REGULAR_OPEN or local_end > LAST_ENTRY_TIME:
        return {
            "experiment_id": experiment_id,
            "status": "bar_outside_entry_window",
            "bar": {"start": signal_start, "end": signal_end},
            "exits": exits,
        }

    threshold = float(experiment["threshold"])
    quantity = int(config.get("quantity") or 1)
    max_orders = int(config.get("max_orders_per_day") or 200)
    max_open = int(config.get("max_open_positions") or 25)
    allow_shorts = bool(config.get("allow_shorts", True))
    decisions: list[dict[str, Any]] = []

    for symbol in list(experiment["symbols"]):
        if _orders_today(conn, experiment_id) >= max_orders:
            break
        existing = _existing_bar_decision(
            conn,
            experiment_id=experiment_id,
            symbol=symbol,
            signal_start=signal_start,
        )
        if existing:
            decisions.append(existing)
            continue
        try:
            flow = await _bar_trade_flow(
                symbol, bar_start=signal_start, bar_end=signal_end, feed=feed
            )
        except Exception as error:  # noqa: BLE001 - lab keeps processing other symbols
            decisions.append(
                _record_decision(
                    conn,
                    experiment_id=experiment_id,
                    symbol=symbol,
                    signal_start=signal_start,
                    signal_end=signal_end,
                    action="error",
                    reason=f"{error.__class__.__name__}: {str(error)[:300]}",
                )
            )
            continue
        if not flow:
            decisions.append(
                _record_decision(
                    conn,
                    experiment_id=experiment_id,
                    symbol=symbol,
                    signal_start=signal_start,
                    signal_end=signal_end,
                    action="skip",
                    reason="no_trade_flow",
                )
            )
            continue
        try:
            decision = await _maybe_enter(
                conn,
                experiment=experiment,
                config=config,
                client=client,
                submit=submit,
                symbol=symbol,
                flow=flow,
                threshold=threshold,
                quantity=quantity,
                max_open=max_open,
                allow_shorts=allow_shorts,
                signal_start=signal_start,
                signal_end=signal_end,
            )
        except Exception as error:  # noqa: BLE001 - record order/provider errors
            decision = _record_decision(
                conn,
                experiment_id=experiment_id,
                symbol=symbol,
                signal_start=signal_start,
                signal_end=signal_end,
                action="error",
                reason=f"{error.__class__.__name__}: {str(error)[:300]}",
                flow=flow,
            )
        decisions.append(decision)
    conn.execute(
        "UPDATE intraday_paper_lab_experiments SET status='running', updated_at=NOW() WHERE id=%s",
        (experiment_id,),
    )
    conn.commit()
    return {
        "experiment_id": experiment_id,
        "bar": {"start": signal_start, "end": signal_end},
        "submit": submit,
        "entries_or_skips": len(decisions),
        "submitted_entries": sum(1 for row in decisions if row.get("client_order_id")),
        "exits": exits,
    }


def _record_decision(
    conn: psycopg.Connection,
    *,
    experiment_id: int,
    symbol: str,
    signal_start: datetime,
    signal_end: datetime,
    action: str,
    reason: str | None = None,
    side: str | None = None,
    flow: dict[str, Any] | None = None,
    client_order_id: str | None = None,
    request_payload: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        """
        INSERT INTO intraday_paper_lab_decisions(
            experiment_id, symbol, signal_bar_start, signal_bar_end, side,
            signed_trade_imbalance, trade_count, unclassified_share,
            effective_trade_count, action, reason, client_order_id,
            broker_order_id, broker_status, request_payload, response_payload
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (experiment_id, symbol, signal_bar_start, action)
        DO NOTHING
        RETURNING *
        """,
        (
            experiment_id,
            symbol.upper(),
            signal_start,
            signal_end,
            side,
            (flow or {}).get("signed_trade_imbalance"),
            (flow or {}).get("trade_count"),
            (flow or {}).get("unclassified_share"),
            (flow or {}).get("effective_trade_count"),
            action,
            reason,
            client_order_id,
            ((response or {}).get("payload") or {}).get("id"),
            ((response or {}).get("payload") or {}).get("status"),
            Jsonb(_json_safe(request_payload or {})),
            Jsonb(_json_safe(response or {})),
        ),
    ).fetchone()
    if row:
        conn.commit()
        return dict(row)
    existing = conn.execute(
        """
        SELECT * FROM intraday_paper_lab_decisions
        WHERE experiment_id=%s AND symbol=%s AND signal_bar_start=%s AND action=%s
        """,
        (experiment_id, symbol.upper(), signal_start, action),
    ).fetchone()
    return dict(existing or {})


async def _maybe_enter(
    conn: psycopg.Connection,
    *,
    experiment: dict[str, Any],
    config: dict[str, Any],
    client: AlpacaPaperLabClient | None,
    submit: bool,
    symbol: str,
    flow: dict[str, Any],
    threshold: float,
    quantity: int,
    max_open: int,
    allow_shorts: bool,
    signal_start: datetime,
    signal_end: datetime,
) -> dict[str, Any]:
    imbalance = flow.get("signed_trade_imbalance")
    if imbalance is None or abs(float(imbalance)) < threshold:
        return _record_decision(
            conn,
            experiment_id=int(experiment["id"]),
            symbol=symbol,
            signal_start=signal_start,
            signal_end=signal_end,
            action="skip",
            reason="below_threshold",
            flow=flow,
        )
    if int(flow.get("trade_count") or 0) < 200:
        reason = "trade_count_below_200"
    elif float(flow.get("unclassified_share") or 0) > 0.25:
        reason = "unclassified_share_above_25pct"
    elif float(flow.get("effective_trade_count") or 0) < 50.0:
        reason = "effective_trade_count_below_50"
    else:
        reason = None
    if reason:
        return _record_decision(
            conn,
            experiment_id=int(experiment["id"]),
            symbol=symbol,
            signal_start=signal_start,
            signal_end=signal_end,
            action="skip",
            reason=reason,
            flow=flow,
        )

    position_side = "long" if float(imbalance) > 0 else "short"
    order_side = "buy" if position_side == "long" else "sell"
    if position_side == "short" and not allow_shorts:
        return _record_decision(
            conn,
            experiment_id=int(experiment["id"]),
            symbol=symbol,
            signal_start=signal_start,
            signal_end=signal_end,
            action="skip",
            reason="shorts_disabled",
            side=order_side,
            flow=flow,
        )
    if _open_position_exists(
        conn, experiment_id=int(experiment["id"]), symbol=symbol, side=position_side
    ):
        return _record_decision(
            conn,
            experiment_id=int(experiment["id"]),
            symbol=symbol,
            signal_start=signal_start,
            signal_end=signal_end,
            action="skip",
            reason="no_pyramiding_existing_position",
            side=order_side,
            flow=flow,
        )
    if _open_positions(conn, int(experiment["id"])) >= max_open:
        return _record_decision(
            conn,
            experiment_id=int(experiment["id"]),
            symbol=symbol,
            signal_start=signal_start,
            signal_end=signal_end,
            action="skip",
            reason="max_open_positions_reached",
            side=order_side,
            flow=flow,
        )
    if position_side == "short" and client is not None:
        asset = await client.get_asset(symbol)
        if not bool(asset.get("shortable")):
            return _record_decision(
                conn,
                experiment_id=int(experiment["id"]),
                symbol=symbol,
                signal_start=signal_start,
                signal_end=signal_end,
                action="skip",
                reason="alpaca_asset_not_shortable",
                side=order_side,
                flow=flow,
            )

    client_order_id = f"kef-lab-{experiment['id']}-{symbol}-{int(signal_start.timestamp())}-{uuid4().hex[:8]}"
    payload = {
        "symbol": symbol.upper(),
        "side": order_side,
        "type": "market",
        "qty": str(quantity),
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }
    response = (
        await client.submit_market_order(
            symbol=symbol, side=order_side, quantity=quantity, client_order_id=client_order_id
        )
        if submit and client is not None
        else {}
    )
    decision = _record_decision(
        conn,
        experiment_id=int(experiment["id"]),
        symbol=symbol,
        signal_start=signal_start,
        signal_end=signal_end,
        action="enter",
        reason=None if submit else "dry_run",
        side=order_side,
        flow=flow,
        client_order_id=client_order_id if submit else None,
        request_payload=payload,
        response=response,
    )
    if submit and decision:
        broker_order_id = ((response or {}).get("payload") or {}).get("id")
        conn.execute(
            """
            INSERT INTO intraday_paper_lab_positions(
                experiment_id, symbol, side, quantity, entry_decision_id,
                entry_client_order_id, entry_broker_order_id,
                signal_bar_start, exit_due_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                int(experiment["id"]),
                symbol.upper(),
                position_side,
                quantity,
                decision["id"],
                client_order_id,
                broker_order_id,
                signal_start,
                signal_end + timedelta(minutes=30),
            ),
        )
        conn.commit()
    return decision


async def flatten_due_positions(
    conn: psycopg.Connection,
    *,
    experiment: dict[str, Any],
    client: AlpacaPaperLabClient | None,
    submit: bool,
    now: datetime,
    force: bool = False,
) -> list[dict[str, Any]]:
    local = _et(now)
    flatten_time = local.time() >= time(15, 55)
    rows = conn.execute(
        """
        SELECT * FROM intraday_paper_lab_positions
        WHERE experiment_id = %s AND status = 'open'
          AND (%s OR exit_due_at <= %s OR %s)
        ORDER BY opened_at
        """,
        (experiment["id"], force, now, flatten_time),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        position = dict(row)
        exit_side = "sell" if position["side"] == "long" else "buy"
        client_order_id = (
            f"kef-lab-exit-{experiment['id']}-{position['symbol']}-"
            f"{int(now.timestamp())}-{uuid4().hex[:8]}"
        )
        payload = {
            "symbol": position["symbol"],
            "side": exit_side,
            "type": "market",
            "qty": str(int(position["quantity"])),
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        response = (
            await client.submit_market_order(
                symbol=position["symbol"],
                side=exit_side,
                quantity=int(position["quantity"]),
                client_order_id=client_order_id,
            )
            if submit and client is not None
            else {}
        )
        decision = _record_decision(
            conn,
            experiment_id=int(experiment["id"]),
            symbol=position["symbol"],
            signal_start=position["signal_bar_start"],
            signal_end=position["exit_due_at"],
            action="exit" if not force else "flatten",
            reason="due" if not force else "forced",
            side=exit_side,
            client_order_id=client_order_id if submit else None,
            request_payload=payload,
            response=response,
        )
        if submit:
            conn.execute(
                """
                UPDATE intraday_paper_lab_positions
                SET status='closed', exit_decision_id=%s, exit_client_order_id=%s,
                    exit_broker_order_id=%s, closed_at=NOW()
                WHERE id=%s
                """,
                (
                    decision.get("id"),
                    client_order_id,
                    ((response or {}).get("payload") or {}).get("id"),
                    position["id"],
                ),
            )
            conn.commit()
        results.append(decision)
    return results


async def run_loop(
    conn: psycopg.Connection,
    *,
    experiment_id: int,
    submit: bool,
    confirm_paper: bool,
    poll_seconds: int = 300,
) -> dict[str, Any]:
    experiment = load_lab_experiment(conn, experiment_id)
    session_close = _utc(experiment["trading_date"], REGULAR_CLOSE) + timedelta(minutes=10)
    cycles = []
    seen_bars: set[datetime] = set()
    while datetime.now(tz=UTC) < session_close:
        selected = completed_signal_bar()
        if selected and selected[0] not in seen_bars:
            cycles.append(
                await run_cycle(
                    conn,
                    experiment_id=experiment_id,
                    submit=submit,
                    confirm_paper=confirm_paper,
                    bar_start=selected[0],
                )
            )
            seen_bars.add(selected[0])
        else:
            await flatten_due_positions(
                conn,
                experiment=experiment,
                client=AlpacaPaperLabClient() if submit else None,
                submit=submit,
                now=datetime.now(tz=UTC),
            )
        await asyncio.sleep(max(30, poll_seconds))
    await flatten_due_positions(
        conn,
        experiment=experiment,
        client=AlpacaPaperLabClient() if submit else None,
        submit=submit,
        now=datetime.now(tz=UTC),
        force=True,
    )
    conn.execute(
        "UPDATE intraday_paper_lab_experiments SET status='completed', updated_at=NOW() WHERE id=%s",
        (experiment_id,),
    )
    conn.commit()
    return {"experiment_id": experiment_id, "cycles": len(cycles), "status": "completed"}


def monitor(conn: psycopg.Connection, *, experiment_id: int) -> dict[str, Any]:
    experiment = load_lab_experiment(conn, experiment_id)
    summary = conn.execute(
        """
        SELECT
            COUNT(*) AS decisions,
            COUNT(*) FILTER (WHERE action='enter' AND client_order_id IS NOT NULL) AS entries_submitted,
            COUNT(*) FILTER (WHERE action IN ('exit','flatten') AND client_order_id IS NOT NULL) AS exits_submitted,
            COUNT(*) FILTER (WHERE action='skip') AS skips,
            COUNT(*) FILTER (WHERE action='error') AS errors,
            MAX(created_at) AS last_decision_at
        FROM intraday_paper_lab_decisions
        WHERE experiment_id = %s
        """,
        (experiment_id,),
    ).fetchone()
    positions = conn.execute(
        """
        SELECT status, side, COUNT(*) AS count
        FROM intraday_paper_lab_positions
        WHERE experiment_id = %s
        GROUP BY status, side
        ORDER BY status, side
        """,
        (experiment_id,),
    ).fetchall()
    recent = conn.execute(
        """
        SELECT id, created_at, symbol, action, side, signed_trade_imbalance,
               trade_count, reason, broker_status, client_order_id
        FROM intraday_paper_lab_decisions
        WHERE experiment_id = %s
        ORDER BY created_at DESC
        LIMIT 25
        """,
        (experiment_id,),
    ).fetchall()
    trade_rows = conn.execute(
        """
        WITH lab_positions AS (
            SELECT
                position.id,
                position.symbol,
                position.side,
                position.quantity,
                position.status,
                position.signal_bar_start,
                position.exit_due_at,
                position.opened_at,
                position.closed_at,
                position.entry_client_order_id,
                position.exit_client_order_id
            FROM intraday_paper_lab_positions position
            WHERE position.experiment_id = %s
        )
        SELECT
            lab_positions.*,
            entry_order.status AS entry_status,
            entry_order.side AS entry_order_side,
            entry_order.filled_quantity AS entry_filled_quantity,
            entry_order.filled_average_price AS entry_price,
            entry_order.submitted_at AS entry_submitted_at,
            entry_order.filled_at AS entry_filled_at,
            exit_order.status AS exit_status,
            exit_order.side AS exit_order_side,
            exit_order.filled_quantity AS exit_filled_quantity,
            exit_order.filled_average_price AS exit_price,
            exit_order.submitted_at AS exit_submitted_at,
            exit_order.filled_at AS exit_filled_at
        FROM lab_positions
        LEFT JOIN broker_orders entry_order
          ON entry_order.client_order_id = lab_positions.entry_client_order_id
        LEFT JOIN broker_orders exit_order
          ON exit_order.client_order_id = lab_positions.exit_client_order_id
        ORDER BY lab_positions.opened_at DESC, lab_positions.id DESC
        """,
        (experiment_id,),
    ).fetchall()
    trades: list[dict[str, Any]] = []
    realized_pnl = Decimal("0")
    realized_count = 0
    open_count = 0
    awaiting_sync = 0
    for row in trade_rows:
        trade = dict(row)
        quantity = Decimal(str(trade.get("quantity") or "0"))
        entry_price = trade.get("entry_price")
        exit_price = trade.get("exit_price")
        pnl: Decimal | None = None
        if entry_price is not None and exit_price is not None:
            entry = Decimal(str(entry_price))
            exit_ = Decimal(str(exit_price))
            pnl = (exit_ - entry) * quantity if trade.get("side") == "long" else (entry - exit_) * quantity
            realized_pnl += pnl
            realized_count += 1
        elif trade.get("status") in {"open", "closing"}:
            open_count += 1
        if trade.get("entry_client_order_id") and trade.get("entry_status") is None:
            awaiting_sync += 1
        if trade.get("exit_client_order_id") and trade.get("exit_status") is None:
            awaiting_sync += 1
        trade["realized_pnl"] = pnl
        trades.append(trade)
    broker_orders = conn.execute(
        """
        SELECT symbol, side, order_type, requested_quantity, filled_quantity,
               filled_average_price, status, submitted_at, filled_at,
               canceled_at, expired_at, client_order_id, updated_at
        FROM broker_orders
        WHERE client_order_id LIKE %s
           OR client_order_id LIKE %s
        ORDER BY submitted_at DESC NULLS LAST, updated_at DESC
        LIMIT 100
        """,
        (f"kef-lab-{experiment_id}-%", f"kef-lab-exit-{experiment_id}-%"),
    ).fetchall()
    latest_sync = conn.execute(
        """
        SELECT id, status, started_at, completed_at, required_components,
               completed_components, completeness, error
        FROM broker_sync_runs
        ORDER BY started_at DESC
        LIMIT 1
        """,
    ).fetchone()
    return {
        "experiment": experiment,
        "summary": dict(summary or {}),
        "positions": [dict(row) for row in positions],
        "recent_decisions": [dict(row) for row in recent],
        "trades": trades,
        "orders": [dict(row) for row in broker_orders],
        "pnl": {
            "realized_pnl": realized_pnl,
            "realized_trades": realized_count,
            "open_trades": open_count,
            "awaiting_broker_sync_items": awaiting_sync,
        },
        "broker_sync": dict(latest_sync or {}),
    }
