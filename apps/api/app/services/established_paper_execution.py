"""Governed Alpaca Paper execution for the three established strategies.

The caller must have completed broker synchronization and a clean
reconciliation in the same cycle.  This service never invents account state:
entries are attributed before submission, and every exit is bounded by both
the strategy ownership ledger and Alpaca's freshly re-read position.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, date, datetime, time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.brokers.alpaca_paper import AlpacaPaperPortfolioAdapter
from app.providers.alpaca import sync_alpaca_candles
from app.services.established_paper_signals import (
    CONNORS_STOP_ATR_MULTIPLE,
    CONNORS_STRATEGY,
    CONNORS_VERSION,
    MOM_STRATEGY,
    MOM_UNIVERSE_HASH,
    MOM_VERSION,
    RSI5_STRATEGY,
    RSI5_VERSION,
    DailyStrategyDecision,
    completed_daily_bars,
    evaluate_spy_connors,
    evaluate_spy_rsi5,
)
from app.services.features import load_candles
from app.services.fractional_execution import AssetFact
from app.services.governed_order_submission import GovernedOrderSubmitter
from app.services.portfolio_execution_bridge import (
    MOM_12_1_SHARE_POLICY,
    PROVENANCE_FORWARD,
    PortfolioSignal,
    build_rebalance_plan,
)
from app.services.position_reducing_sell import ConfirmedPosition
from app.services.strategy_ownership import (
    ReconciliationEvidence,
    ledger_from_rows,
    require_clean_reconciliation,
)
from app.settings import settings
from psycopg.types.json import Jsonb

STRATEGIES = {
    RSI5_STRATEGY: (RSI5_VERSION, "SPY"),
    CONNORS_STRATEGY: (CONNORS_VERSION, "SPY"),
    MOM_STRATEGY: (MOM_VERSION, None),
}
TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}
SENT_DECISION_STATUSES = {"submitted", "accepted", "filled"}
NEW_YORK = ZoneInfo("America/New_York")


def ensure_registry(conn: Any) -> None:
    for strategy, (version, symbol) in STRATEGIES.items():
        conn.execute(
            """
            INSERT INTO established_paper_strategies(strategy, strategy_version, symbol)
            VALUES (%s, %s, %s) ON CONFLICT(strategy) DO NOTHING
            """,
            (strategy, version, symbol),
        )
    conn.commit()


def set_execution_enabled(conn: Any, *, enabled: bool, operator: str, confirmation: str) -> dict[str, Any]:
    expected = "ENABLE ALPACA PAPER THREE STRATEGIES" if enabled else "DISABLE ALPACA PAPER THREE STRATEGIES"
    if confirmation != expected:
        raise ValueError(f"confirmation must exactly equal {expected!r}")
    if enabled and not (
        settings.established_paper_strategies_enabled
        and settings.broker_order_submission_enabled
        and settings.external_paper_execution_enabled
    ):
        raise ValueError(
            "activation requires ESTABLISHED_PAPER_STRATEGIES_ENABLED, "
            "BROKER_ORDER_SUBMISSION_ENABLED and EXTERNAL_PAPER_EXECUTION_ENABLED"
        )
    ensure_registry(conn)
    rows = conn.execute(
        """
        UPDATE established_paper_strategies
           SET enabled=%s,
               enabled_at=CASE WHEN %s THEN NOW() ELSE enabled_at END,
               enabled_by=CASE WHEN %s THEN %s ELSE enabled_by END,
               disabled_at=CASE WHEN %s THEN disabled_at ELSE NOW() END,
               disabled_by=CASE WHEN %s THEN disabled_by ELSE %s END,
               latest_status=%s, latest_error=NULL, updated_at=NOW()
         RETURNING strategy, strategy_version, symbol, enabled
        """,
        (enabled, enabled, enabled, operator, enabled, enabled, operator, "enabled" if enabled else "disabled"),
    ).fetchall()
    conn.commit()
    return {"paper_only": True, "enabled": enabled, "strategies": [dict(row) for row in rows]}


def _reconciliation_evidence(conn: Any, broker_account_id: int, run_id: int) -> ReconciliationEvidence:
    row = conn.execute(
        """SELECT id, status, completed_at, broker_account_id
             FROM broker_reconciliation_runs
            WHERE id=%s AND broker_account_id=%s""",
        (run_id, broker_account_id),
    ).fetchone()
    if not row:
        raise ValueError("the current broker cycle has no reconciliation evidence")
    evidence = ReconciliationEvidence(
        run_id=int(row["id"]),
        status=str(row["status"]),
        completed_at=row["completed_at"],
        broker_account_id=int(row["broker_account_id"]),
    )
    return require_clean_reconciliation(evidence)


def _ownership(conn: Any, broker_account_id: int, strategy: str):
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT symbol, quantity, as_of FROM strategy_owned_positions
                 WHERE broker_account_id=%s AND strategy=%s AND quantity>0""",
            (broker_account_id, strategy),
        ).fetchall()
    ]
    return ledger_from_rows(rows, strategy=strategy, source="strategy_owned_positions")


def _confirmed_positions(conn: Any, broker_account_id: int, reconciliation: ReconciliationEvidence):
    positions: dict[str, ConfirmedPosition] = {}
    for row in conn.execute(
        """SELECT symbol, quantity, market_value, updated_at FROM broker_positions
             WHERE broker_account_id=%s AND quantity>0""",
        (broker_account_id,),
    ).fetchall():
        positions[str(row["symbol"]).upper()] = ConfirmedPosition(
            symbol=str(row["symbol"]).upper(),
            quantity=Decimal(str(row["quantity"])),
            market_value=Decimal(str(row["market_value"])),
            observed_at=row["updated_at"],
            reconciliation_status=reconciliation.status,
        )
    return positions


def _signal_dict(decision: DailyStrategyDecision) -> dict[str, Any]:
    return {
        "strategy": decision.strategy,
        "version": decision.version,
        "symbol": decision.symbol,
        "session_date": decision.session_date.isoformat(),
        "action": decision.action,
        "close": decision.close,
        "indicators": decision.indicators,
        "reason": decision.reason,
        "next_open_execution": decision.action.endswith("next_open"),
    }


def _client_order_id(strategy: str, version: str, session: date, action: str, symbol: str) -> str:
    seed = f"{strategy}|{version}|{session.isoformat()}|{action}|{symbol}"
    prefix = strategy.lower().replace("spy_", "").replace("_", "-")[:16]
    return f"kt-{prefix}-{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


def _plan_decision(
    conn: Any,
    *,
    broker_account_id: int,
    strategy: str,
    version: str,
    session: date,
    action: str,
    signal: dict[str, Any],
    payload: dict[str, Any] | None,
    status: str | None = None,
) -> dict[str, Any] | None:
    key = f"{strategy}:{version}:{session.isoformat()}:{action}"
    row = conn.execute(
        """
        INSERT INTO established_paper_strategy_decisions
            (strategy,strategy_version,broker_account_id,session_date,action,
             decision_key,client_order_id,status,signal,order_payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(strategy,session_date,action) DO NOTHING
        RETURNING *
        """,
        (
            strategy, version, broker_account_id, session, action, key,
            payload.get("client_order_id") if payload else None,
            status or ("planned" if payload else "observed"),
            Jsonb(signal), Jsonb(payload) if payload else None,
        ),
    ).fetchone()
    conn.commit()
    if row:
        return dict(row)
    existing = conn.execute(
        """SELECT * FROM established_paper_strategy_decisions
             WHERE strategy=%s AND session_date=%s AND action=%s""",
        (strategy, session, action),
    ).fetchone()
    return dict(existing) if existing else None


def _decision_needs_submission(row: dict[str, Any] | None) -> bool:
    return bool(row) and str(row["status"]) not in SENT_DECISION_STATUSES


async def _submit(
    conn: Any,
    *,
    broker_account_id: int,
    decision_row: dict[str, Any],
    payload: dict[str, Any],
    strategy: str,
    version: str,
    positions: dict[str, ConfirmedPosition],
    ownership: Any,
    reconciliation: ReconciliationEvidence,
) -> dict[str, Any]:
    adapter = AlpacaPaperPortfolioAdapter()
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=broker_account_id)
    conn.execute(
        """UPDATE established_paper_strategy_decisions
              SET status='planned', error=NULL, updated_at=NOW() WHERE id=%s""",
        (decision_row["id"],),
    )
    conn.commit()
    try:
        response = await submitter.submit(
            payload,
            strategy=strategy,
            strategy_version=version,
            confirmed_positions=positions,
            ownership_ledger=ownership,
            reconciliation=reconciliation,
        )
        body = dict(response.payload or {})
        status = "accepted" if str(body.get("status") or "").lower() in {"accepted", "new", "pending_new"} else "submitted"
        updated = conn.execute(
            """UPDATE established_paper_strategy_decisions
                  SET status=%s, broker_order_id=%s, response_payload=%s, updated_at=NOW()
                WHERE id=%s RETURNING *""",
            (status, str(body.get("id") or "") or None, Jsonb(body), decision_row["id"]),
        ).fetchone()
        conn.commit()
        return dict(updated)
    except Exception as error:  # noqa: BLE001 - every transport failure can be an accepted order
        # Resolve the classic ambiguous-submit case by deterministic client id.
        try:
            existing = await adapter.get_order_by_client_id(str(payload["client_order_id"]))
            body = dict(existing.payload or {})
            updated = conn.execute(
                """UPDATE established_paper_strategy_decisions
                      SET status='accepted', broker_order_id=%s, response_payload=%s,
                          error=%s, updated_at=NOW() WHERE id=%s RETURNING *""",
                (
                    str(body.get("id") or "") or None, Jsonb(body),
                    Jsonb({"submit_error": error.__class__.__name__, "resolved_by_client_order_id": True}),
                    decision_row["id"],
                ),
            ).fetchone()
            conn.commit()
            return dict(updated)
        except Exception as lookup_error:
            conn.execute(
                """UPDATE established_paper_strategy_decisions SET status='failed', error=%s,
                          updated_at=NOW() WHERE id=%s""",
                (Jsonb({"submit_error": str(error), "lookup_error": lookup_error.__class__.__name__}), decision_row["id"]),
            )
            conn.commit()
            raise


def _open_stop_orders(conn: Any, broker_account_id: int, strategy: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT o.* FROM broker_orders o
            JOIN strategy_order_attributions a
              ON a.broker_account_id=o.broker_account_id
             AND a.client_order_id=o.client_order_id
            WHERE o.broker_account_id=%s AND a.strategy=%s AND o.side='sell'
              AND o.order_type IN ('stop','stop_limit')
              AND NOT (o.status = ANY(%s))
            ORDER BY o.submitted_at, o.id
            """,
            (broker_account_id, strategy, sorted(TERMINAL_ORDER_STATUSES)),
        ).fetchall()
    ]


async def _cancel_stops(conn: Any, adapter: AlpacaPaperPortfolioAdapter, stops: list[dict[str, Any]]) -> None:
    for stop in stops:
        await adapter.cancel_order(str(stop["broker_order_id"]))
        conn.execute(
            """INSERT INTO broker_audit_events(trace_id,event_type,operator,phase,details)
               VALUES (%s,'established_strategy_stop_cancel','broker_worker','automatic',%s)""",
            (uuid4(), Jsonb({"broker_order_id": stop["broker_order_id"], "client_order_id": stop["client_order_id"]})),
        )
    conn.commit()


async def _run_spy_strategy(
    conn: Any,
    *,
    registry: dict[str, Any],
    broker_account_id: int,
    reconciliation: ReconciliationEvidence,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    strategy = str(registry["strategy"])
    version = str(registry["strategy_version"])
    ownership = _ownership(conn, broker_account_id, strategy)
    owned = ownership.owned_quantity("SPY")
    decision = (
        evaluate_spy_rsi5(bars, is_long=owned > 0)
        if strategy == RSI5_STRATEGY
        else evaluate_spy_connors(bars, is_long=owned > 0)
    )
    signal = _signal_dict(decision)
    positions = _confirmed_positions(conn, broker_account_id, reconciliation)
    stops = _open_stop_orders(conn, broker_account_id, strategy) if strategy == CONNORS_STRATEGY else []

    if decision.action == "hold":
        broker_mutation = False
        _plan_decision(
            conn, broker_account_id=broker_account_id, strategy=strategy, version=version,
            session=decision.session_date, action="hold", signal=signal, payload=None,
        )
        # A filled Connors entry gets its 3xATR stop on the first clean cycle
        # that can prove ownership. The signal ATR is immutable in the entry row.
        if strategy == CONNORS_STRATEGY and owned > 0 and not stops:
            entry = conn.execute(
                """SELECT * FROM established_paper_strategy_decisions
                    WHERE strategy=%s AND action='enter_next_open'
                      AND status IN ('submitted','accepted','filled')
                    ORDER BY session_date DESC,id DESC LIMIT 1""",
                (strategy,),
            ).fetchone()
            if entry:
                entry_signal = dict(entry["signal"] or {})
                atr = Decimal(str((entry_signal.get("indicators") or {}).get("atr14")))
                held = ownership.positions["SPY"]
                if held.quantity % 1:
                    raise ValueError("Connors protective stops require a whole-share filled position")
                state = conn.execute(
                    "SELECT average_entry_price FROM strategy_owned_positions WHERE strategy=%s AND broker_account_id=%s AND symbol='SPY'",
                    (strategy, broker_account_id),
                ).fetchone()
                if not state or state["average_entry_price"] is None:
                    raise ValueError("Connors fill has no attributed average entry price")
                stop_price = Decimal(str(state["average_entry_price"])) - Decimal(str(CONNORS_STOP_ATR_MULTIPLE)) * atr
                payload = {
                    "symbol": "SPY", "side": "sell", "qty": format(owned, "f"),
                    "type": "stop", "time_in_force": "gtc", "stop_price": format(stop_price.quantize(Decimal("0.01")), "f"),
                    "client_order_id": _client_order_id(strategy, version, entry["session_date"], "protective-stop", "SPY"),
                }
                row = _plan_decision(
                    conn, broker_account_id=broker_account_id, strategy=strategy, version=version,
                    session=entry["session_date"], action="protective_stop", signal=entry_signal, payload=payload,
                )
                if _decision_needs_submission(row):
                    await _submit(
                        conn, broker_account_id=broker_account_id, decision_row=row, payload=payload,
                        strategy=strategy, version=version, positions=positions, ownership=ownership,
                        reconciliation=reconciliation,
                    )
                    broker_mutation = True
        return {"strategy": strategy, "decision": signal, "broker_mutation": broker_mutation}

    payload: dict[str, Any]
    if decision.action == "enter_next_open":
        client_id = _client_order_id(strategy, version, decision.session_date, "entry", "SPY")
        if strategy == RSI5_STRATEGY:
            notional = Decimal(str(settings.established_rsi5_notional)).quantize(Decimal("0.01"), ROUND_DOWN)
            payload = {"symbol": "SPY", "side": "buy", "notional": format(notional, "f"), "type": "market", "time_in_force": "day", "client_order_id": client_id}
        else:
            atr = Decimal(str(decision.indicators["atr14"]))
            risk_budget = Decimal(str(settings.broker_allocated_capital)) * Decimal(str(settings.established_connors_risk_pct))
            risk_qty = (risk_budget / (Decimal(str(CONNORS_STOP_ATR_MULTIPLE)) * atr)).to_integral_value(rounding=ROUND_DOWN)
            capital_qty = (Decimal(str(settings.broker_allocated_capital)) / Decimal(str(decision.close))).to_integral_value(rounding=ROUND_DOWN)
            qty = min(risk_qty, capital_qty)
            if qty < 1:
                raise ValueError("Connors risk budget cannot fund one whole SPY share")
            payload = {"symbol": "SPY", "side": "buy", "qty": format(qty, "f"), "type": "market", "time_in_force": "day", "client_order_id": client_id}
    else:
        if owned <= 0:
            return {"strategy": strategy, "decision": signal, "broker_mutation": False, "reason": "no strategy-owned position"}
        payload = {
            "symbol": "SPY", "side": "sell", "qty": format(owned, "f"),
            "type": "market", "time_in_force": "day",
            "client_order_id": _client_order_id(strategy, version, decision.session_date, "exit", "SPY"),
        }

    row = _plan_decision(
        conn, broker_account_id=broker_account_id, strategy=strategy, version=version,
        session=decision.session_date, action=decision.action, signal=signal, payload=payload,
    )
    if not _decision_needs_submission(row):
        return {"strategy": strategy, "decision": signal, "broker_mutation": False, "reason": "decision already processed"}
    # Persist the resumable exit decision before removing the protective stop.
    # A crash can therefore retry the same deterministic client order id.
    if decision.action == "exit_next_open" and stops:
        await _cancel_stops(conn, AlpacaPaperPortfolioAdapter(), stops)
    submitted = await _submit(
        conn, broker_account_id=broker_account_id, decision_row=row, payload=payload,
        strategy=strategy, version=version, positions=positions, ownership=ownership,
        reconciliation=reconciliation,
    )
    return {"strategy": strategy, "decision": signal, "submission": submitted, "broker_mutation": True}


def load_mom_shadow_signal(path: Path) -> PortfolioSignal:
    state_path = path.parent.parent / "state.json"
    if not state_path.is_file():
        raise ValueError(f"MOM_12_1 state is missing at {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("version") != MOM_VERSION or state.get("universe_hash") != MOM_UNIVERSE_HASH:
        raise ValueError("MOM_12_1 state does not match the frozen version/universe")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("MOM_12_1 signal is empty")
    formation = {row["formation_date"] for row in rows}
    entries = {row["intended_entry_date"] for row in rows}
    versions = {row["strategy_version"] for row in rows}
    if len(formation) != 1 or len(entries) != 1 or versions != {MOM_VERSION}:
        raise ValueError("MOM_12_1 signal mixes dates or versions")
    symbols = tuple(str(row["symbol"]).upper() for row in rows)
    if len(set(symbols)) != len(symbols):
        raise ValueError("MOM_12_1 signal contains duplicate symbols")
    return PortfolioSignal(
        strategy=MOM_STRATEGY,
        strategy_version=MOM_VERSION,
        universe_hash=MOM_UNIVERSE_HASH,
        signal_date=date.fromisoformat(formation.pop()),
        intended_execution_date=date.fromisoformat(entries.pop()),
        symbols=symbols,
        provenance=PROVENANCE_FORWARD,
        source_path=str(path),
        source_sha256=digest,
    )


def _mom_signal_is_missed(intended_execution_date: date, *, now: datetime | None = None) -> bool:
    instant = (now or datetime.now(UTC)).astimezone(NEW_YORK)
    return intended_execution_date < instant.date() or (
        intended_execution_date == instant.date()
        and instant.time().replace(tzinfo=None) >= time(9, 30)
    )


async def _run_mom(
    conn: Any,
    *,
    registry: dict[str, Any],
    broker_account_id: int,
    reconciliation: ReconciliationEvidence,
) -> dict[str, Any]:
    signal_dir = Path(settings.mom_12_1_signal_dir)
    files = sorted(signal_dir.glob("*.csv")) if signal_dir.is_dir() else []
    if not files:
        return {"strategy": MOM_STRATEGY, "status": "waiting_for_first_forward_month_end", "broker_mutation": False}
    signal = load_mom_shadow_signal(files[-1])
    if _mom_signal_is_missed(signal.intended_execution_date):
        return {"strategy": MOM_STRATEGY, "status": "missed_signal_refused_no_backfill", "signal_date": signal.signal_date.isoformat(), "broker_mutation": False}
    facts = {
        str(row["symbol"]): AssetFact(
            symbol=str(row["symbol"]), tradable=bool(row["is_active"]), fractionable=row["fractionable"]
        )
        for row in conn.execute(
            "SELECT symbol,is_active,fractionable FROM symbols WHERE symbol=ANY(%s)",
            (list(signal.symbols),),
        ).fetchall()
    }
    prices = {
        str(row["symbol"]): Decimal(str(row["close"]))
        for row in conn.execute(
            """SELECT DISTINCT ON(symbol) symbol,close FROM candles
                WHERE symbol=ANY(%s) AND timeframe='1d' ORDER BY symbol,timestamp DESC""",
            (list(signal.symbols),),
        ).fetchall()
    }
    ownership = _ownership(conn, broker_account_id, MOM_STRATEGY)
    positions = _confirmed_positions(conn, broker_account_id, reconciliation)
    plan = build_rebalance_plan(
        signal=signal,
        allocated_capital=Decimal(str(settings.broker_allocated_capital)),
        reference_prices=prices,
        asset_facts=facts,
        positions=positions,
        ownership=ownership,
        reconciliation=reconciliation,
        share_policy=MOM_12_1_SHARE_POLICY,
    )
    if plan.blocked:
        return {"strategy": MOM_STRATEGY, "status": "blocked", "blockers": list(plan.blockers), "broker_mutation": False}
    decision = _plan_decision(
        conn, broker_account_id=broker_account_id, strategy=MOM_STRATEGY, version=MOM_VERSION,
        session=signal.signal_date, action="mom_rebalance", signal=signal.as_dict(), payload=None,
        status="planned",
    )
    if not _decision_needs_submission(decision):
        return {"strategy": MOM_STRATEGY, "status": "already_processed", "broker_mutation": False}
    adapter = AlpacaPaperPortfolioAdapter()
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=broker_account_id)
    progress = dict(decision.get("response_payload") or {})
    submitted_ids = set(progress.get("submitted_client_order_ids") or [])
    submitted_now = 0
    for symbol_plan in (*plan.symbol_plans, *plan.exits):
        payload = symbol_plan.order_payload
        if not payload or payload["client_order_id"] in submitted_ids:
            continue
        try:
            response = await submitter.submit(
                payload,
                strategy=signal.strategy,
                strategy_version=signal.strategy_version,
                confirmed_positions=positions,
                ownership_ledger=ownership,
                reconciliation=reconciliation,
            )
        except Exception as error:  # noqa: BLE001 - resolve an ambiguous POST before retrying
            try:
                response = await adapter.get_order_by_client_id(payload["client_order_id"])
            except Exception as lookup_error:  # noqa: BLE001
                conn.execute(
                    """UPDATE established_paper_strategy_decisions
                          SET status='failed',response_payload=%s,error=%s,updated_at=NOW()
                        WHERE id=%s""",
                    (
                        Jsonb({"submitted_client_order_ids": sorted(submitted_ids)}),
                        Jsonb({"submit_error": str(error), "lookup_error": lookup_error.__class__.__name__}),
                        decision["id"],
                    ),
                )
                conn.commit()
                return {
                    "strategy": MOM_STRATEGY,
                    "status": "partial_failed",
                    "submitted_orders": len(submitted_ids),
                    "broker_mutation": bool(submitted_ids),
                    "error": error.__class__.__name__,
                }
        submitted_ids.add(payload["client_order_id"])
        submitted_now += 1
        progress = {
            "submitted_client_order_ids": sorted(submitted_ids),
            "last_broker_order_id": str((response.payload or {}).get("id") or "") or None,
        }
        conn.execute(
            """UPDATE established_paper_strategy_decisions
                  SET status='planned',response_payload=%s,error=NULL,updated_at=NOW()
                WHERE id=%s""",
            (Jsonb(progress), decision["id"]),
        )
        conn.commit()
    conn.execute(
        "UPDATE established_paper_strategy_decisions SET status='submitted',response_payload=%s,updated_at=NOW() WHERE id=%s",
        (Jsonb({**progress, "submitted_orders": len(submitted_ids)}), decision["id"]),
    )
    conn.commit()
    return {
        "strategy": MOM_STRATEGY,
        "status": "submitted",
        "submitted_orders": len(submitted_ids),
        "submitted_now": submitted_now,
        "broker_mutation": submitted_now > 0,
    }


async def run_established_paper_cycle(conn: Any, *, sync_run_id: int, reconciliation_run_id: int) -> dict[str, Any]:
    ensure_registry(conn)
    if not settings.established_paper_strategies_enabled:
        return {"status": "disabled", "broker_mutation": False}
    if not (settings.broker_order_submission_enabled and settings.external_paper_execution_enabled):
        raise ValueError("established paper strategies require both broker execution flags")
    account = conn.execute(
        """SELECT a.* FROM broker_sync_runs r
             JOIN broker_accounts a ON a.id=r.broker_account_id
            WHERE r.id=%s AND r.status='complete'""",
        (sync_run_id,),
    ).fetchone()
    if not account:
        raise ValueError("broker synchronization has not registered an Alpaca Paper account")
    broker_account_id = int(account["id"])
    reconciliation = _reconciliation_evidence(conn, broker_account_id, reconciliation_run_id)
    enabled = [dict(row) for row in conn.execute(
        "SELECT * FROM established_paper_strategies WHERE enabled=TRUE ORDER BY strategy"
    ).fetchall()]
    if not enabled:
        return {"status": "armed_no_enabled_strategies", "broker_mutation": False}

    results: list[dict[str, Any]] = []
    spy_rows = [row for row in enabled if row["strategy"] in {RSI5_STRATEGY, CONNORS_STRATEGY}]
    bars: list[dict[str, Any]] = []
    if spy_rows:
        await sync_alpaca_candles(conn, symbol="SPY", timeframe="1d", limit=450)
        bars = completed_daily_bars(
            load_candles(conn, "SPY", "1d", limit=450, source="alpaca_iex")
        )
    for registry in enabled:
        try:
            if registry["strategy"] == MOM_STRATEGY:
                result = await _run_mom(
                    conn,
                    registry=registry,
                    broker_account_id=broker_account_id,
                    reconciliation=reconciliation,
                )
            else:
                result = await _run_spy_strategy(
                    conn,
                    registry=registry,
                    broker_account_id=broker_account_id,
                    reconciliation=reconciliation,
                    bars=bars,
                )
            results.append(result)
        except Exception as error:  # noqa: BLE001 - stop the cycle on unknown mutation state
            conn.rollback()
            results.append(
                {
                    "strategy": registry["strategy"],
                    "status": "failed",
                    "error_class": error.__class__.__name__,
                    "error": str(error),
                    # A transport/database exception may occur after Alpaca
                    # accepted a POST. Treat it as mutation until the next
                    # clean synchronization proves otherwise, and do not run
                    # another strategy against stale reconciliation evidence.
                    "broker_mutation": True,
                    "mutation_state": "unknown_after_failure",
                }
            )
            break
    return {
        "status": "complete",
        "sync_run_id": sync_run_id,
        "reconciliation_run_id": reconciliation_run_id,
        "results": results,
        "broker_mutation": any(bool(row.get("broker_mutation")) for row in results),
    }
