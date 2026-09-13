"""Read-only FIFO journal of actual broker fills, excluding research simulations.

An analytical reconstruction, not an audited ledger: fees, missing initial
inventory and corporate-action adjustments cannot be inferred from fills.
"""
from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


def match_fills(fills):
    books = defaultdict(deque)
    closed = []
    for fill in fills:
        qty = Decimal(str(fill["quantity"]))
        price = Decimal(str(fill["price"]))
        direction = 1 if fill["side"] == "buy" else -1
        book = books[fill["symbol"]]
        while qty > 0 and book and book[0]["direction"] != direction:
            entry = book[0]
            matched = min(qty, entry["quantity"])
            pnl = (price - entry["price"]) * matched * entry["direction"]
            timestamp = fill["transaction_at"]
            closed.append({
                "id": f'{entry["id"]}-{fill["id"]}',
                "symbol": fill["symbol"],
                "strategy": ("Shared / unattributed" if fill.get("strategy")
                             and fill["strategy"] != entry["strategy"] else entry["strategy"]),
                "side": "long" if entry["direction"] == 1 else "short",
                "quantity": matched, "entry_price": entry["price"],
                "exit_price": price, "opened_at": entry["timestamp"],
                "closed_at": timestamp,
                "day": timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
                "gross_pnl": pnl, "net_pnl": None,
            })
            qty -= matched
            entry["quantity"] -= matched
            if not entry["quantity"]:
                book.popleft()
        if qty > 0:
            book.append({"id": fill["id"], "direction": direction,
                         "quantity": qty, "price": price,
                         "strategy": fill.get("strategy") or "Unattributed",
                         "timestamp": fill["transaction_at"]})
    return closed


def pnl_journal(conn, month):
    account = conn.execute("""SELECT id, account_number_masked, last_successful_sync_at
        FROM broker_accounts WHERE environment='paper'
        ORDER BY last_successful_sync_at DESC NULLS LAST, id DESC LIMIT 1""").fetchone()
    base = {"month": month, "generated_at": datetime.now(UTC), "account": None,
            "positions": [], "strategies": [], "trades": [], "reconciliation": None,
            "fees_available": False, "coverage_start": None}
    if not account:
        return base
    aid = account["id"]
    state = conn.execute("""SELECT equity, cash, buying_power, updated_at
        FROM broker_account_state WHERE broker_account_id=%s""", (aid,)).fetchone()
    base["account"] = {**account, **(state or {})}
    base["reconciliation"] = conn.execute("""SELECT status, completed_at
        FROM broker_reconciliation_runs WHERE broker_account_id=%s
        ORDER BY id DESC LIMIT 1""", (aid,)).fetchone()
    base["positions"] = conn.execute("""SELECT p.symbol, p.quantity, p.average_entry_price,
        p.market_value, p.unrealized_pl,
        (SELECT string_agg(s.strategy, ', ' ORDER BY s.strategy)
         FROM strategy_owned_positions s WHERE s.broker_account_id=p.broker_account_id
         AND s.symbol=p.symbol AND s.quantity>0) AS strategy
        FROM broker_positions p WHERE p.broker_account_id=%s AND p.quantity<>0
        ORDER BY p.symbol""", (aid,)).fetchall()
    base["strategies"] = conn.execute("""SELECT candidate_id AS name, symbol, state
        FROM external_paper_deployments WHERE broker_account_id=%s
        UNION ALL
        SELECT strategy AS name, symbol, CASE WHEN enabled THEN 'enabled' ELSE 'disabled' END
        FROM established_paper_strategies
        UNION ALL
        SELECT name, NULL AS symbol, status AS state FROM intraday_paper_lab_experiments
        UNION ALL
        SELECT DISTINCT strategy AS name, symbol, 'position recorded' AS state
        FROM strategy_owned_positions WHERE broker_account_id=%s AND quantity>0
        ORDER BY name, symbol""", (aid, aid)).fetchall()
    fills = conn.execute("""SELECT f.id, f.symbol, f.side, f.quantity, f.price, f.transaction_at,
        COALESCE(x.candidate_id, d.strategy, lab.name,
          CASE WHEN o.client_order_id LIKE 'kefka%%' THEN 'kefka-auto' END) AS strategy
        FROM broker_fills f
        LEFT JOIN broker_orders o ON o.broker_account_id=f.broker_account_id
          AND o.broker_order_id=f.broker_order_id
        LEFT JOIN broker_execution_attempts a ON a.client_order_id=o.client_order_id
        LEFT JOIN external_paper_deployments x ON x.id=a.external_deployment_id
        LEFT JOIN LATERAL (SELECT strategy FROM established_paper_strategy_decisions
          WHERE broker_account_id=f.broker_account_id AND client_order_id=o.client_order_id
          ORDER BY id DESC LIMIT 1) d ON TRUE
        LEFT JOIN LATERAL (SELECT e.name FROM intraday_paper_lab_positions t
          JOIN intraday_paper_lab_experiments e ON e.id=t.experiment_id
          WHERE t.entry_client_order_id=o.client_order_id LIMIT 1) lab ON TRUE
        WHERE f.broker_account_id=%s AND NOT f.reconstructed
          AND f.transaction_at < ((%s::date + INTERVAL '1 month') AT TIME ZONE 'America/New_York')
        ORDER BY f.transaction_at, f.id""", (aid, month + "-01")).fetchall()
    base["coverage_start"] = fills[0]["transaction_at"] if fills else None
    base["trades"] = [t for t in match_fills(fills) if t["day"].startswith(month)]
    return base
