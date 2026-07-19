from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from app.brokers import AlpacaPaperBrokerAdapter, BrokerAdapter, BrokerResponse
from app.settings import settings

SYNC_SCHEMA_VERSION = "broker-sync-v1"
REQUIRED_COMPONENTS = ("account", "clock", "orders", "positions", "fill_activities")
SECRET_KEYS = {"authorization", "apca-api-key-id", "apca-api-secret-key", "api_key", "secret", "token"}


async def synchronize_broker(conn: psycopg.Connection, adapter: BrokerAdapter | None = None) -> dict[str, Any]:
    if not settings.broker_sync_enabled:
        return {"status": "disabled", "feature": "BROKER_SYNC_ENABLED", "paper_only": True}
    adapter = adapter or AlpacaPaperBrokerAdapter()
    assert_read_only_flags()
    trace_id = uuid4()
    release_id = persist_adapter_release(conn, adapter)
    run = conn.execute(
        """
        INSERT INTO broker_sync_runs(trace_id, status, provider_api_version, adapter_version, normalization_version, schema_version, required_components)
        VALUES (%s, 'running', %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (trace_id, adapter.provider_api_version, adapter.adapter_version, adapter.normalization_version, SYNC_SCHEMA_VERSION, Jsonb(list(REQUIRED_COMPONENTS))),
    ).fetchone()
    conn.commit()
    responses: dict[str, tuple[BrokerResponse, int]] = {}
    try:
        for fetch in (adapter.get_account, adapter.get_clock, adapter.list_orders, adapter.list_positions, adapter.list_fill_activities):
            response = await fetch()
            raw_event_id = persist_raw_response(conn, int(run["id"]), trace_id, adapter, response)
            responses[response.endpoint_class] = (response, raw_event_id)
        missing = [name for name in REQUIRED_COMPONENTS if name not in responses]
        if missing:
            return mark_sync_incomplete(conn, int(run["id"]), responses, missing)
        compatibility = adapter_compatibility(conn, release_id)
        if not compatibility["latest_state_promotion_allowed"]:
            blocked = conn.execute(
                "UPDATE broker_sync_runs SET status='incompatible', completed_components=%s, completeness=%s, completed_at=NOW() WHERE id=%s RETURNING *",
                (Jsonb(sorted(responses)), Jsonb(compatibility), run["id"]),
            ).fetchone()
            conn.commit()
            return {"status": "incompatible", "sync_run": dict(blocked), "compatibility": compatibility, "trace_id": str(trace_id), "raw_evidence_preserved": True, "paper_only": True}
        account_payload = dict(responses["account"][0].payload)
        broker_account = upsert_broker_account(conn, account_payload)
        persist_normalized_state(conn, int(run["id"]), trace_id, broker_account, responses)
        completed = conn.execute(
            """
            UPDATE broker_sync_runs
            SET broker_account_id = %s, status = 'complete', completed_components = %s,
                completeness = %s, completed_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (
                broker_account["id"],
                Jsonb(list(REQUIRED_COMPONENTS)),
                Jsonb({"complete": True, "component_count": len(REQUIRED_COMPONENTS), "adapter_release_id": release_id}),
                run["id"],
            ),
        ).fetchone()
        conn.execute("UPDATE broker_accounts SET last_successful_sync_at = NOW(), latest_error = NULL, updated_at = NOW() WHERE id = %s", (broker_account["id"],))
        conn.commit()
        return {"status": "complete", "sync_run": dict(completed), "broker_account": broker_account, "trace_id": str(trace_id), "paper_only": True}
    except Exception as error:
        safe_rollback(conn)
        conn.execute(
            "UPDATE broker_sync_runs SET status = 'failed', completed_components = %s, error = %s, completed_at = NOW() WHERE id = %s",
            (Jsonb(sorted(responses)), Jsonb(sanitize_value({"class": error.__class__.__name__, "message": str(error)})), run["id"]),
        )
        conn.commit()
        raise


def assert_read_only_flags() -> None:
    if settings.broker_order_submission_enabled or settings.external_paper_execution_enabled:
        raise RuntimeError("Phase 10 read-only foundation requires all broker execution flags to remain disabled")


def persist_adapter_release(conn: psycopg.Connection, adapter: BrokerAdapter) -> int:
    if adapter.change_class not in {"compatible_patch", "normalization_change", "behavioral_change", "contract_incompatible", "provider_api_incompatible", "schema_incompatible"}:
        raise RuntimeError("unknown adapter compatibility change class")
    manifest = {
        "adapter_contract_version": adapter.adapter_contract_version,
        "provider_api_version": adapter.provider_api_version,
        "normalization_version": adapter.normalization_version,
        "behavior_version": adapter.behavior_version,
        "read_only": True,
        "order_submission_implemented": False,
        "change_class": adapter.change_class,
        "compatible_from": adapter.compatible_from,
    }
    conn.execute(
        """
        INSERT INTO broker_adapter_releases(provider, adapter_version, adapter_contract_version, provider_api_version, normalization_version, behavior_version, change_class, compatible_from, manifest)
        VALUES ('alpaca', %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(provider, adapter_version) DO NOTHING
        """,
        (adapter.adapter_version, adapter.adapter_contract_version, adapter.provider_api_version, adapter.normalization_version, adapter.behavior_version, adapter.change_class, adapter.compatible_from, Jsonb(manifest)),
    )
    row = conn.execute("SELECT * FROM broker_adapter_releases WHERE provider = 'alpaca' AND adapter_version = %s", (adapter.adapter_version,)).fetchone()
    expected = (adapter.adapter_contract_version, adapter.provider_api_version, adapter.normalization_version, adapter.behavior_version, adapter.change_class, adapter.compatible_from)
    stored = (row["adapter_contract_version"], row["provider_api_version"], row["normalization_version"], row["behavior_version"], row["change_class"], row["compatible_from"])
    if stored != expected:
        raise RuntimeError("adapter version collision with different compatibility metadata")
    conn.commit()
    return int(row["id"])


def adapter_compatibility(conn: psycopg.Connection, release_id: int) -> dict[str, Any]:
    current = conn.execute("SELECT * FROM broker_adapter_releases WHERE id=%s", (release_id,)).fetchone()
    if not current:
        return {"latest_state_promotion_allowed": False, "reason": "missing_adapter_release"}
    previous = conn.execute("SELECT * FROM broker_adapter_releases WHERE provider='alpaca' AND id<>%s ORDER BY created_at DESC LIMIT 1", (release_id,)).fetchone()
    if current["change_class"] != "compatible_patch":
        return {"latest_state_promotion_allowed": False, "reason": current["change_class"], "adapter_version": current["adapter_version"]}
    if previous and any(current[key] != previous[key] for key in ("adapter_contract_version", "provider_api_version", "normalization_version", "behavior_version")):
        return {"latest_state_promotion_allowed": False, "reason": "compatible_patch_manifest_mismatch", "adapter_version": current["adapter_version"]}
    return {"latest_state_promotion_allowed": True, "reason": "compatible_patch", "adapter_version": current["adapter_version"]}


def persist_raw_response(conn: psycopg.Connection, sync_run_id: int, trace_id: UUID, adapter: BrokerAdapter, response: BrokerResponse) -> int:
    payload = sanitize_value(response.payload)
    encoded = canonical_json(payload)
    payload_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        INSERT INTO broker_raw_ingest_events(sync_run_id, trace_id, endpoint_class, request_metadata, response_status, payload, payload_hash, provider_api_version, adapter_version, normalization_version, schema_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            sync_run_id,
            trace_id,
            response.endpoint_class,
            Jsonb(sanitize_value({"request_id": response.request_id})),
            response.status_code,
            Jsonb(payload),
            payload_hash,
            adapter.provider_api_version,
            adapter.adapter_version,
            adapter.normalization_version,
            SYNC_SCHEMA_VERSION,
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def upsert_broker_account(conn: psycopg.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    external_id = str(payload.get("id") or "").strip()
    account_number = str(payload.get("account_number") or external_id)
    if not external_id:
        raise ValueError("Alpaca account response is missing id")
    row = conn.execute(
        """
        INSERT INTO broker_accounts(provider, environment, external_account_id, account_number_masked, status)
        VALUES ('alpaca', 'paper', %s, %s, %s)
        ON CONFLICT(provider, environment, external_account_id) DO UPDATE
        SET account_number_masked = EXCLUDED.account_number_masked, status = EXCLUDED.status, updated_at = NOW()
        RETURNING *
        """,
        (external_id, mask_account(account_number), str(payload.get("status") or "unknown")),
    ).fetchone()
    return dict(row)


def persist_normalized_state(
    conn: psycopg.Connection,
    sync_run_id: int,
    trace_id: UUID,
    broker_account: dict[str, Any],
    responses: dict[str, tuple[BrokerResponse, int]],
) -> None:
    account_id = int(broker_account["id"])
    account_raw, account_raw_id = responses["account"]
    clock_raw, clock_raw_id = responses["clock"]
    account = normalize_account(dict(account_raw.payload))
    clock = normalize_clock(dict(clock_raw.payload))
    conn.execute(
        """
        INSERT INTO broker_account_state(broker_account_id, sync_run_id, raw_event_id, status, currency, cash, equity, buying_power, trading_blocked, account_blocked, trade_suspended_by_user, normalized)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(broker_account_id) DO UPDATE SET sync_run_id=EXCLUDED.sync_run_id, raw_event_id=EXCLUDED.raw_event_id, status=EXCLUDED.status, currency=EXCLUDED.currency, cash=EXCLUDED.cash, equity=EXCLUDED.equity, buying_power=EXCLUDED.buying_power, trading_blocked=EXCLUDED.trading_blocked, account_blocked=EXCLUDED.account_blocked, trade_suspended_by_user=EXCLUDED.trade_suspended_by_user, normalized=EXCLUDED.normalized, updated_at=NOW()
        """,
        (account_id, sync_run_id, account_raw_id, account["status"], account["currency"], account["cash"], account["equity"], account["buying_power"], account["trading_blocked"], account["account_blocked"], account["trade_suspended_by_user"], Jsonb(account)),
    )
    conn.execute("INSERT INTO broker_account_snapshots(broker_account_id, sync_run_id, trace_id, raw_event_id, state) VALUES (%s,%s,%s,%s,%s)", (account_id, sync_run_id, trace_id, account_raw_id, Jsonb(account)))
    conn.execute(
        """
        INSERT INTO broker_clock_state(broker_account_id, sync_run_id, raw_event_id, timestamp, is_open, next_open, next_close)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(broker_account_id) DO UPDATE SET sync_run_id=EXCLUDED.sync_run_id, raw_event_id=EXCLUDED.raw_event_id, timestamp=EXCLUDED.timestamp, is_open=EXCLUDED.is_open, next_open=EXCLUDED.next_open, next_close=EXCLUDED.next_close, updated_at=NOW()
        """,
        (account_id, sync_run_id, clock_raw_id, clock["timestamp"], clock["is_open"], clock["next_open"], clock["next_close"]),
    )
    conn.execute("INSERT INTO broker_clock_snapshots(broker_account_id, sync_run_id, trace_id, raw_event_id, state) VALUES (%s,%s,%s,%s,%s)", (account_id, sync_run_id, trace_id, clock_raw_id, Jsonb(clock)))
    persist_orders(conn, account_id, sync_run_id, responses["orders"])
    persist_fills(conn, account_id, sync_run_id, responses["fill_activities"])
    persist_positions(conn, account_id, sync_run_id, trace_id, responses["positions"])


def persist_orders(conn: psycopg.Connection, account_id: int, sync_run_id: int, source: tuple[BrokerResponse, int]) -> None:
    response, raw_event_id = source
    for item in list(response.payload or []):
        row = normalize_order(dict(item))
        conn.execute(
            """
            INSERT INTO broker_orders(broker_account_id, broker_order_id, client_order_id, sync_run_id, raw_event_id, symbol, side, order_type, time_in_force, requested_quantity, filled_quantity, filled_average_price, status, submitted_at, filled_at, canceled_at, expired_at, normalized)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(broker_account_id, broker_order_id) DO UPDATE SET sync_run_id=EXCLUDED.sync_run_id, raw_event_id=EXCLUDED.raw_event_id, filled_quantity=EXCLUDED.filled_quantity, filled_average_price=EXCLUDED.filled_average_price, status=EXCLUDED.status, filled_at=EXCLUDED.filled_at, canceled_at=EXCLUDED.canceled_at, expired_at=EXCLUDED.expired_at, normalized=EXCLUDED.normalized, updated_at=NOW()
            """,
            (account_id, row["broker_order_id"], row["client_order_id"], sync_run_id, raw_event_id, row["symbol"], row["side"], row["order_type"], row["time_in_force"], row["requested_quantity"], row["filled_quantity"], row["filled_average_price"], row["status"], row["submitted_at"], row["filled_at"], row["canceled_at"], row["expired_at"], Jsonb(row)),
        )


def persist_fills(conn: psycopg.Connection, account_id: int, sync_run_id: int, source: tuple[BrokerResponse, int]) -> None:
    response, raw_event_id = source
    for item in list(response.payload or []):
        row = normalize_fill(dict(item))
        conn.execute(
            """
            INSERT INTO broker_fills(broker_account_id, broker_order_id, broker_activity_id, sync_run_id, raw_event_id, symbol, side, quantity, price, cumulative_quantity, leaves_quantity, source, reconstructed, transaction_at, normalized)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'alpaca_account_activity',FALSE,%s,%s)
            ON CONFLICT(broker_account_id, broker_activity_id) DO NOTHING
            """,
            (account_id, row["broker_order_id"], row["broker_activity_id"], sync_run_id, raw_event_id, row["symbol"], row["side"], row["quantity"], row["price"], row["cumulative_quantity"], row["leaves_quantity"], row["transaction_at"], Jsonb(row)),
        )


def persist_positions(conn: psycopg.Connection, account_id: int, sync_run_id: int, trace_id: UUID, source: tuple[BrokerResponse, int]) -> None:
    response, raw_event_id = source
    symbols: list[str] = []
    for item in list(response.payload or []):
        row = normalize_position(dict(item))
        symbols.append(row["symbol"])
        conn.execute(
            """
            INSERT INTO broker_positions(broker_account_id, symbol, sync_run_id, raw_event_id, quantity, average_entry_price, market_value, unrealized_pl, normalized)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(broker_account_id, symbol) DO UPDATE SET sync_run_id=EXCLUDED.sync_run_id, raw_event_id=EXCLUDED.raw_event_id, quantity=EXCLUDED.quantity, average_entry_price=EXCLUDED.average_entry_price, market_value=EXCLUDED.market_value, unrealized_pl=EXCLUDED.unrealized_pl, normalized=EXCLUDED.normalized, updated_at=NOW()
            """,
            (account_id, row["symbol"], sync_run_id, raw_event_id, row["quantity"], row["average_entry_price"], row["market_value"], row["unrealized_pl"], Jsonb(row)),
        )
        conn.execute("INSERT INTO broker_position_snapshots(broker_account_id, sync_run_id, trace_id, raw_event_id, symbol, state) VALUES (%s,%s,%s,%s,%s,%s)", (account_id, sync_run_id, trace_id, raw_event_id, row["symbol"], Jsonb(row)))
    if symbols:
        conn.execute("DELETE FROM broker_positions WHERE broker_account_id = %s AND NOT (symbol = ANY(%s))", (account_id, symbols))
    else:
        conn.execute("DELETE FROM broker_positions WHERE broker_account_id = %s", (account_id,))


def normalize_account(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(payload.get("status") or "unknown"),
        "currency": str(payload.get("currency") or "USD"),
        "cash": decimal(payload.get("cash")),
        "equity": decimal(payload.get("equity")),
        "buying_power": decimal(payload.get("buying_power")),
        "trading_blocked": bool(payload.get("trading_blocked")),
        "account_blocked": bool(payload.get("account_blocked")),
        "trade_suspended_by_user": bool(payload.get("trade_suspended_by_user")),
    }


def normalize_clock(payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": timestamp(payload.get("timestamp")), "is_open": bool(payload.get("is_open")), "next_open": timestamp(payload.get("next_open")), "next_close": timestamp(payload.get("next_close"))}


def normalize_order(payload: dict[str, Any]) -> dict[str, Any]:
    broker_order_id = str(payload.get("id") or "")
    if not broker_order_id:
        raise ValueError("broker order is missing id")
    return {
        "broker_order_id": broker_order_id,
        "client_order_id": str(payload.get("client_order_id") or broker_order_id),
        "symbol": str(payload.get("symbol") or "").upper(),
        "side": str(payload.get("side") or "").lower(),
        "order_type": str(payload.get("order_type") or payload.get("type") or "unknown"),
        "time_in_force": str(payload.get("time_in_force") or "day"),
        "requested_quantity": decimal(payload.get("qty")),
        "filled_quantity": decimal(payload.get("filled_qty")),
        "filled_average_price": decimal(payload.get("filled_avg_price")) if payload.get("filled_avg_price") is not None else None,
        "status": str(payload.get("status") or "unknown"),
        "submitted_at": optional_timestamp(payload.get("submitted_at")),
        "filled_at": optional_timestamp(payload.get("filled_at")),
        "canceled_at": optional_timestamp(payload.get("canceled_at")),
        "expired_at": optional_timestamp(payload.get("expired_at")),
    }


def normalize_fill(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "broker_order_id": str(payload.get("order_id") or ""),
        "broker_activity_id": str(payload.get("id") or ""),
        "symbol": str(payload.get("symbol") or "").upper(),
        "side": str(payload.get("side") or "").lower(),
        "quantity": decimal(payload.get("qty")),
        "price": decimal(payload.get("price")),
        "cumulative_quantity": decimal(payload.get("cum_qty")) if payload.get("cum_qty") is not None else None,
        "leaves_quantity": decimal(payload.get("leaves_qty")) if payload.get("leaves_qty") is not None else None,
        "transaction_at": timestamp(payload.get("transaction_time")),
    }


def normalize_position(payload: dict[str, Any]) -> dict[str, Any]:
    return {"symbol": str(payload.get("symbol") or "").upper(), "quantity": decimal(payload.get("qty")), "average_entry_price": decimal(payload.get("avg_entry_price")), "market_value": abs(decimal(payload.get("market_value"))), "unrealized_pl": decimal(payload.get("unrealized_pl"))}


def mark_sync_incomplete(conn: psycopg.Connection, run_id: int, responses: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    row = conn.execute("UPDATE broker_sync_runs SET status='incomplete', completed_components=%s, completeness=%s, completed_at=NOW() WHERE id=%s RETURNING *", (Jsonb(sorted(responses)), Jsonb({"complete": False, "missing": missing}), run_id)).fetchone()
    conn.commit()
    return {"status": "incomplete", "sync_run": dict(row), "paper_only": True}


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "-")
            if normalized in SECRET_KEYS or any(token in normalized for token in ("secret", "password", "authorization")):
                continue
            clean[str(key)] = sanitize_value(item)
        return clean
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, (datetime, Decimal, UUID)):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(sanitize_value(value), sort_keys=True, separators=(",", ":"), default=str)


def decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def timestamp(value: Any) -> datetime:
    parsed = optional_timestamp(value)
    if parsed is None:
        raise ValueError("broker timestamp is required")
    return parsed


def optional_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def mask_account(value: str) -> str:
    return f"***{value[-4:]}" if len(value) > 4 else "***"


def safe_rollback(conn: psycopg.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        pass
