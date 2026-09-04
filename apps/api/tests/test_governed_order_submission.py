"""Attribution is a hard prerequisite to submission, not a convention.

Every test here asserts on the *sequence* of things that happened -- database
statements and HTTP requests interleaved on one recorder -- because the property
under test is an ordering, and a test that only checked the end state would pass
just as happily if attribution were written after the POST.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.brokers.alpaca_paper import AlpacaPaperPortfolioAdapter
from app.services.governed_order_submission import (
    ATTRIBUTION_CONFLICT,
    ATTRIBUTION_MISSING,
    AttributionConflict,
    GovernedOrderSubmitter,
    OrderAttributionError,
    assert_attribution_matches,
    intent_from_payload,
    persist_and_verify_attribution,
)
from app.services.position_reducing_sell import ConfirmedPosition
from app.services.strategy_ownership import (
    ReconciliationEvidence,
    StrategyOwnedPosition,
    StrategyOwnershipLedger,
)
from app.settings import settings

MOM = "MOM_12_1"
VERSION = "1.0.0"
ACCOUNT = 1
NOW = datetime(2026, 9, 1, 14, 30, tzinfo=UTC)
COID = "kt-mom_12_1-9f2c1a4b7e8d3c5a1b0e6f42"

SCHEMA = """
CREATE TABLE strategy_order_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_account_id INTEGER NOT NULL,
    client_order_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    intended_side TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE (broker_account_id, client_order_id)
);
"""


class Result:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchall(self):
        return [dict(row) for row in self._cursor.fetchall()]

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row else None


class Conn:
    """SQLite behind psycopg's surface, recording onto a shared timeline.

    The timeline is the point: it holds database statements and HTTP requests in
    the order they actually occurred, so "attribution before POST" is something
    a test can read off rather than infer.
    """

    def __init__(self, timeline: list[str], *, fail_on: str | None = None):
        self._db = sqlite3.connect(":memory:", isolation_level="DEFERRED")
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()
        self.timeline = timeline
        self._fail_on = fail_on

    def execute(self, query, params=()):
        label = "db:insert-attribution" if "INSERT INTO strategy_order_attributions" in query else (
            "db:read-attribution" if "FROM strategy_order_attributions" in query else "db:other"
        )
        if self._fail_on and self._fail_on in label:
            self.timeline.append(f"{label}:FAILED")
            raise sqlite3.OperationalError("database is locked")
        self.timeline.append(label)
        adapted = tuple(
            str(p) if isinstance(p, (Decimal, datetime)) else p for p in params
        )
        return Result(self._db.execute(query.replace("%s", "?"), adapted))

    def commit(self):
        if self._fail_on == "commit":
            self.timeline.append("db:commit:FAILED")
            raise sqlite3.OperationalError("could not commit")
        self.timeline.append("db:commit")
        self._db.commit()

    def rollback(self):
        self._db.rollback()

    def rows(self):
        return [
            dict(r)
            for r in self._db.execute("SELECT * FROM strategy_order_attributions")
        ]

    def seed(self, **overrides):
        row = {
            "broker_account_id": ACCOUNT, "client_order_id": COID, "strategy": MOM,
            "strategy_version": VERSION, "symbol": "AAPL", "intended_side": "buy",
        }
        row.update(overrides)
        self._db.execute(
            "INSERT INTO strategy_order_attributions(broker_account_id, client_order_id, "
            "strategy, strategy_version, symbol, intended_side, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (row["broker_account_id"], row["client_order_id"], row["strategy"],
             row["strategy_version"], row["symbol"], row["intended_side"], str(NOW)),
        )
        self._db.commit()


def make_adapter(timeline, monkeypatch, *, positions=None, order_id="paper-order-1",
                 duplicate_client_ids=None):
    """A portfolio adapter on a mock transport, both flags on, recording onto
    the same timeline as the database."""
    monkeypatch.setattr(settings, "broker_provider", "alpaca")
    monkeypatch.setattr(settings, "alpaca_paper_base_url", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(settings, "alpaca_paper_api_key", "paper-key")
    monkeypatch.setattr(settings, "alpaca_paper_secret_key", "paper-secret")
    monkeypatch.setattr(settings, "broker_order_submission_enabled", True)
    monkeypatch.setattr(settings, "external_paper_execution_enabled", True)

    seen_client_ids = duplicate_client_ids if duplicate_client_ids is not None else set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            timeline.append("http:GET /v2/positions")
            return httpx.Response(200, json=positions or [])
        timeline.append(f"http:{request.method} {request.url.path}")
        import json as _json

        body = _json.loads(request.content)
        client_order_id = body.get("client_order_id")
        if client_order_id in seen_client_ids:
            # Alpaca rejects a client order id it has already accepted.
            return httpx.Response(422, json={"message": "client_order_id must be unique"})
        seen_client_ids.add(client_order_id)
        return httpx.Response(200, json={"id": order_id, "client_order_id": client_order_id})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.alpaca_paper_base_url
    )
    return AlpacaPaperPortfolioAdapter(client=client), client


def buy_payload(client_order_id=COID, symbol="AAPL"):
    return {
        "symbol": symbol, "side": "buy", "type": "market", "time_in_force": "day",
        "notional": "1000.00", "client_order_id": client_order_id,
    }


def sell_payload(qty="4", client_order_id=COID, symbol="AAPL"):
    return {
        "symbol": symbol, "side": "sell", "qty": qty, "type": "market",
        "time_in_force": "day", "client_order_id": client_order_id,
    }


def ledger(owned):
    return StrategyOwnershipLedger(
        strategy=MOM,
        positions={
            symbol: StrategyOwnedPosition(
                strategy=MOM, symbol=symbol, quantity=Decimal(str(qty)),
                as_of=datetime.now(UTC) - timedelta(minutes=1),
            )
            for symbol, qty in owned.items()
        },
        available=True,
        source="test",
    )


def evidence(status="clean", age=timedelta(minutes=1)):
    return ReconciliationEvidence(
        run_id=42, status=status, completed_at=datetime.now(UTC) - age,
        broker_account_id=ACCOUNT,
    )


def position(symbol, qty, price=100):
    return ConfirmedPosition(
        symbol=symbol, quantity=Decimal(str(qty)),
        market_value=Decimal(str(qty)) * Decimal(str(price)),
        observed_at=datetime.now(UTC) - timedelta(seconds=30),
    )


def position_body(symbol, qty, price=100):
    return [{"symbol": symbol, "qty": str(qty), "market_value": str(qty * price)}]


def posts(timeline):
    return [entry for entry in timeline if entry.startswith("http:POST")]


# ---------------------------------------------------------------------------
# The ordering
# ---------------------------------------------------------------------------


def test_a_buy_attributes_verifies_then_posts(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        response = await submitter.submit(
            buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW
        )
        assert response.payload["id"] == "paper-order-1"
        await client.aclose()

    asyncio.run(run())

    assert timeline == [
        "db:insert-attribution",   # 1. persist
        "db:commit",               #    durably
        "db:read-attribution",     # 2. verify
        "http:POST /v2/orders",    # 4. post
    ]
    assert len(conn.rows()) == 1


def test_a_sell_attributes_verifies_then_re_reads_positions_then_posts(monkeypatch):
    """The TOCTOU re-read survives: it still happens, and still immediately
    before the POST rather than being displaced by the attribution gate."""
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch, positions=position_body("AAPL", 10))
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        await submitter.submit(
            sell_payload(qty="4"), strategy=MOM, strategy_version=VERSION,
            confirmed_positions={"AAPL": position("AAPL", 10)},
            ownership_ledger=ledger({"AAPL": 10}),
            reconciliation=evidence(),
            now=NOW,
        )
        await client.aclose()

    asyncio.run(run())

    assert timeline == [
        "db:insert-attribution",
        "db:commit",
        "db:read-attribution",
        "http:GET /v2/positions",   # 3. fresh broker safety check
        "http:POST /v2/orders",     # 4. post
    ]


def test_attribution_is_committed_before_any_http_happens(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch, positions=position_body("AAPL", 10))
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        await submitter.submit(
            sell_payload(), strategy=MOM, strategy_version=VERSION,
            confirmed_positions={"AAPL": position("AAPL", 10)},
            ownership_ledger=ledger({"AAPL": 10}), reconciliation=evidence(), now=NOW,
        )
        await client.aclose()

    asyncio.run(run())
    first_http = next(i for i, e in enumerate(timeline) if e.startswith("http:"))
    assert timeline.index("db:commit") < first_http


# ---------------------------------------------------------------------------
# Failure cases -- every one asserts zero POSTs
# ---------------------------------------------------------------------------


def test_a_failed_attribution_insert_sends_nothing(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline, fail_on="db:insert-attribution")
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(OrderAttributionError, match=ATTRIBUTION_MISSING):
            await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION)
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []
    assert not any(e.startswith("http:") for e in timeline)


def test_a_failed_commit_sends_nothing(monkeypatch):
    """Written but not durable is not written."""
    timeline: list[str] = []
    conn = Conn(timeline, fail_on="commit")
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(OrderAttributionError, match=ATTRIBUTION_MISSING):
            await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION)
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []


def test_a_failed_verification_read_sends_nothing(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline, fail_on="db:read-attribution")
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(OrderAttributionError, match="could not be verified"):
            await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION)
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []


@pytest.mark.parametrize(
    "conflict",
    [
        {"strategy": "MEANREV_5"},
        {"strategy_version": "2.0.0"},
        {"symbol": "TSLA"},
        {"intended_side": "sell"},
    ],
    ids=["strategy", "version", "symbol", "side"],
)
def test_a_conflicting_stored_attribution_sends_nothing(monkeypatch, conflict):
    """The id is deterministic, so a row that disagrees means it was reused for
    something else. Accepting it would credit this order's fills to whatever the
    old row named."""
    timeline: list[str] = []
    conn = Conn(timeline)
    conn.seed(**conflict)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(AttributionConflict):
            await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION)
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []


def test_the_same_id_on_another_account_is_a_different_order(monkeypatch):
    """Attribution is keyed on (account, client order id), so another account's
    row neither blocks this order nor is reused for it -- our account gets its
    own row, and the other one is left alone."""
    timeline: list[str] = []
    conn = Conn(timeline)
    conn.seed(broker_account_id=99, symbol="TSLA", strategy="MEANREV_5")
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW)
        await client.aclose()

    asyncio.run(run())
    rows = {row["broker_account_id"]: row for row in conn.rows()}
    assert rows[ACCOUNT]["strategy"] == MOM and rows[ACCOUNT]["symbol"] == "AAPL"
    assert rows[99]["strategy"] == "MEANREV_5"  # untouched
    assert len(posts(timeline)) == 1


def test_a_conflicting_row_is_never_silently_accepted(monkeypatch):
    """The insert is a silent no-op when a row already exists, so only the
    read-back notices the disagreement."""
    timeline: list[str] = []
    conn = Conn(timeline)
    conn.seed(symbol="TSLA")
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(AttributionConflict, match=ATTRIBUTION_CONFLICT):
            await submitter.submit(buy_payload(symbol="AAPL"), strategy=MOM,
                                   strategy_version=VERSION)
        await client.aclose()

    asyncio.run(run())
    assert "db:insert-attribution" in timeline   # the insert ran
    assert "db:read-attribution" in timeline     # and the read caught it
    assert posts(timeline) == []
    # The pre-existing row is left exactly as it was, not repaired into agreement.
    assert conn.rows()[0]["symbol"] == "TSLA"


def test_an_order_without_a_client_order_id_is_refused(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)
    payload = buy_payload()
    del payload["client_order_id"]

    async def run():
        with pytest.raises(OrderAttributionError, match="deterministic client order id"):
            await submitter.submit(payload, strategy=MOM, strategy_version=VERSION)
        await client.aclose()

    asyncio.run(run())
    assert timeline == []  # nothing written, nothing sent


def test_an_order_without_a_strategy_version_is_refused(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(OrderAttributionError, match="strategy and its version"):
            await submitter.submit(buy_payload(), strategy=MOM, strategy_version="")
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []


def test_the_adapters_own_refusals_still_stop_an_attributed_order(monkeypatch):
    """Attribution is a prerequisite, not a permission. A sell that would open a
    short is refused after attribution, and still posts nothing."""
    from app.services.position_reducing_sell import StalePositionAtSubmit

    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch, positions=position_body("AAPL", 6))
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        # Stored snapshot says 10, broker now says 6, the plan sells 8.
        with pytest.raises(StalePositionAtSubmit):
            await submitter.submit(
                sell_payload(qty="8"), strategy=MOM, strategy_version=VERSION,
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}), reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []
    assert "http:GET /v2/positions" in timeline  # the fresh re-read did run
    assert len(conn.rows()) == 1  # attribution stands; the order was not sent


def test_a_stale_reconciliation_still_blocks_after_attribution(monkeypatch):
    from app.services.strategy_ownership import ReconciliationEvidenceMissing

    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch, positions=position_body("AAPL", 10))
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        with pytest.raises(ReconciliationEvidenceMissing):
            await submitter.submit(
                sell_payload(), strategy=MOM, strategy_version=VERSION,
                confirmed_positions={"AAPL": position("AAPL", 10)},
                ownership_ledger=ledger({"AAPL": 10}),
                reconciliation=evidence(age=timedelta(days=14)),  # clean but ancient
            )
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []


# ---------------------------------------------------------------------------
# Idempotency and the crash race
# ---------------------------------------------------------------------------


def test_repeated_submission_writes_one_attribution(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW)
        # The venue rejects the duplicate client order id; attribution does not
        # grow a second row either way.
        with pytest.raises(httpx.HTTPStatusError):
            await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW)
        await client.aclose()

    asyncio.run(run())
    assert len(conn.rows()) == 1
    assert submitter.attributions[0]["newly_written"] is True
    assert submitter.attributions[1]["newly_written"] is False
    assert submitter.attributions[1]["verified"] is True


def test_a_crash_between_attribution_and_post_leaves_one_order(monkeypatch):
    """The dangerous race, run end to end.

    Attribution commits, the process dies before the POST, and the rerun uses the
    same deterministic client order id. The attribution stays a single row, and
    the venue's own client-order-id uniqueness decides what happens to the order
    -- so at most one exists.
    """
    timeline: list[str] = []
    conn = Conn(timeline)
    accepted_ids: set[str] = set()

    # --- first attempt: attribution commits, then the process dies ----------
    #
    # No adapter is constructed, because the crash happens before the submitter
    # would reach one -- which is itself the shape of the race being tested.
    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    persist_and_verify_attribution(conn, intent, now=NOW)

    assert len(conn.rows()) == 1
    assert posts(timeline) == []          # the crash happened before any POST
    assert accepted_ids == set()          # so the venue has no order

    # --- restart: same plan, same deterministic id --------------------------
    adapter2, client2 = make_adapter(timeline, monkeypatch, duplicate_client_ids=accepted_ids)
    submitter2 = GovernedOrderSubmitter(conn=conn, adapter=adapter2, broker_account_id=ACCOUNT)

    async def rerun():
        response = await submitter2.submit(
            buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW
        )
        await client2.aclose()
        return response

    response = asyncio.run(rerun())

    assert response.payload["client_order_id"] == COID
    assert len(conn.rows()) == 1                 # attribution still idempotent
    assert submitter2.attributions[0]["newly_written"] is False
    assert len(posts(timeline)) == 1             # exactly one order created
    assert accepted_ids == {COID}


def test_a_third_attempt_after_the_order_exists_creates_no_second_order(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    accepted_ids: set[str] = set()
    adapter, client = make_adapter(timeline, monkeypatch, duplicate_client_ids=accepted_ids)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW)
        for _ in range(2):
            with pytest.raises(httpx.HTTPStatusError):
                await submitter.submit(
                    buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW
                )
        await client.aclose()

    asyncio.run(run())
    assert accepted_ids == {COID}   # the venue holds exactly one
    assert len(conn.rows()) == 1


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_attribution_still_carries_no_quantity(monkeypatch):
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(conn=conn, adapter=adapter, broker_account_id=ACCOUNT)

    async def run():
        await submitter.submit(buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW)
        await client.aclose()

    asyncio.run(run())
    row = conn.rows()[0]
    assert not any("quantity" in key or "notional" in key for key in row)
    assert submitter.attributions[0]["quantity_persisted"] is False


def test_the_intent_is_read_off_the_payload_not_supplied_beside_it():
    """Otherwise the attribution could describe a different order than the one
    on the wire."""
    intent = intent_from_payload(
        buy_payload(symbol="tsla"), broker_account_id=ACCOUNT,
        strategy=MOM, strategy_version=VERSION,
    )
    assert intent.symbol == "TSLA"
    assert intent.intended_side == "buy"
    assert not hasattr(intent, "quantity")


def test_every_attributed_field_is_compared():
    from app.services.governed_order_submission import ATTRIBUTED_FIELDS

    assert set(ATTRIBUTED_FIELDS) == {
        "broker_account_id", "client_order_id", "strategy",
        "strategy_version", "symbol", "intended_side",
    }
    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    for field in ATTRIBUTED_FIELDS:
        stored = intent.as_dict() | {field: "something-else"}
        with pytest.raises(AttributionConflict):
            assert_attribution_matches(stored, intent)


def test_a_missing_row_is_refused_not_treated_as_agreement():
    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    with pytest.raises(OrderAttributionError, match=ATTRIBUTION_MISSING):
        assert_attribution_matches(None, intent)


def test_an_account_id_stored_as_text_still_matches():
    """Some drivers hand an integer column back as a string; that is the only
    difference this comparison forgives."""
    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    assert_attribution_matches(intent.as_dict() | {"broker_account_id": "1"}, intent)


def test_client_order_ids_are_compared_case_sensitively():
    """They are case-sensitive at the venue, so folding case here would let two
    genuinely different orders compare equal."""
    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    with pytest.raises(AttributionConflict):
        assert_attribution_matches(
            intent.as_dict() | {"client_order_id": COID.upper()}, intent
        )


def test_the_adapter_has_no_database_access():
    """The gate sits above the adapter precisely so the frozen buy-only contract
    does not grow a database dependency by inheritance."""
    import inspect

    from app.brokers import alpaca_paper

    source = inspect.getsource(alpaca_paper)
    for banned in ("psycopg", "app.db", "strategy_order_attributions", "conn."):
        assert banned not in source


def test_the_submitter_is_the_only_thing_holding_both():
    import inspect

    from app.services import governed_order_submission as gate

    source = inspect.getsource(gate)
    assert "record_order_attribution" in source
    assert "submit_order" in source
    # And it reaches the broker only through an injected adapter, never by
    # constructing one with ambient credentials.
    assert "AlpacaPaperBrokerAdapter(" not in source
    assert "AlpacaPaperPortfolioAdapter(" not in source


def test_execution_flags_remain_false_by_default():
    from app.settings import Settings

    fresh = Settings(_env_file=None)
    assert fresh.broker_order_submission_enabled is False
    assert fresh.external_paper_execution_enabled is False


# ---------------------------------------------------------------------------
# The bypass is closed
# ---------------------------------------------------------------------------
#
# The submitter enforces "attribution before POST" by doing them in that order.
# That is only a guarantee if the second step cannot be reached without the
# first -- and it could be, by constructing the adapter and calling it.


def test_a_directly_constructed_adapter_cannot_submit(monkeypatch):
    from app.brokers.submission_capability import SubmissionCapabilityError

    timeline: list[str] = []
    adapter, client = make_adapter(timeline, monkeypatch)

    async def run():
        with pytest.raises(
            SubmissionCapabilityError, match="does not submit orders directly"
        ):
            await adapter.submit_order(buy_payload())
        await client.aclose()

    asyncio.run(run())
    assert posts(timeline) == []
    assert timeline == []  # not even a position read


def test_a_direct_sell_on_the_adapter_cannot_submit(monkeypatch):
    from app.brokers.submission_capability import SubmissionCapabilityError

    timeline: list[str] = []
    adapter, client = make_adapter(
        timeline, monkeypatch, positions=position_body("AAPL", 100)
    )

    async def run():
        with pytest.raises(SubmissionCapabilityError):
            await adapter.submit_order(sell_payload())
        await client.aclose()

    asyncio.run(run())
    assert timeline == []


def test_the_mutating_entry_point_refuses_without_a_capability(monkeypatch):
    """Even reaching past the public name gets nowhere: the private entry point
    is gated on the capability, not on being called politely."""
    from app.brokers.submission_capability import SubmissionCapabilityError

    timeline: list[str] = []
    adapter, client = make_adapter(
        timeline, monkeypatch, positions=position_body("AAPL", 100)
    )

    async def run():
        with pytest.raises(
            SubmissionCapabilityError, match="requires a submission capability"
        ):
            await adapter._submit_governed_order(
                sell_payload(),
                confirmed_positions={"AAPL": position("AAPL", 100)},
                ownership_ledger=ledger({"AAPL": 100}),
                reconciliation=evidence(),
            )
        await client.aclose()

    asyncio.run(run())
    assert timeline == []  # refused before the flags, before the position read


def test_a_capability_cannot_be_constructed_directly():
    from app.brokers.submission_capability import (
        SubmissionCapability,
        SubmissionCapabilityError,
    )

    with pytest.raises(
        SubmissionCapabilityError, match="cannot be constructed directly"
    ):
        SubmissionCapability(
            object(),
            broker_account_id=ACCOUNT, client_order_id=COID, strategy=MOM,
            strategy_version=VERSION, symbol="AAPL", intended_side="buy",
        )


def test_an_unverified_attribution_mints_nothing():
    from app.brokers.submission_capability import (
        SubmissionCapability,
        SubmissionCapabilityError,
    )

    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    with pytest.raises(SubmissionCapabilityError, match="not verified"):
        SubmissionCapability.after_verified_attribution(
            attribution=intent.as_dict(), verified=False
        )


def test_an_incomplete_attribution_mints_nothing():
    from app.brokers.submission_capability import (
        SubmissionCapability,
        SubmissionCapabilityError,
    )

    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    with pytest.raises(SubmissionCapabilityError, match="missing"):
        SubmissionCapability.after_verified_attribution(
            attribution=intent.as_dict() | {"strategy_version": ""}, verified=True
        )


def test_a_capability_cannot_be_replayed_onto_another_order(monkeypatch):
    """Bound to one order, so a bug that reused one fails loudly instead of
    sending the wrong thing."""
    from app.brokers.submission_capability import (
        SubmissionCapability,
        SubmissionCapabilityError,
    )

    timeline: list[str] = []
    adapter, client = make_adapter(timeline, monkeypatch)
    intent = intent_from_payload(
        buy_payload(), broker_account_id=ACCOUNT, strategy=MOM, strategy_version=VERSION
    )
    capability = SubmissionCapability.after_verified_attribution(
        attribution=intent.as_dict(), verified=True
    )

    async def run():
        for other in (
            buy_payload(client_order_id="kt-mom_12_1-different"),
            buy_payload(symbol="TSLA"),
            sell_payload(),
        ):
            with pytest.raises(SubmissionCapabilityError, match="cannot be replayed"):
                await adapter._submit_governed_order(other, capability=capability)
        await client.aclose()

    asyncio.run(run())
    assert timeline == []


def test_something_that_is_not_a_capability_is_refused(monkeypatch):
    """A duck-typed lookalike does not get through: the check is on the type,
    because the type is what only the sanctioned path can produce."""
    from app.brokers.submission_capability import SubmissionCapabilityError

    timeline: list[str] = []
    adapter, client = make_adapter(timeline, monkeypatch)

    class Lookalike:
        client_order_id = COID
        symbol = "AAPL"
        intended_side = "buy"

        def authorises(self, payload):
            return True

        def assert_authorises(self, payload):
            return None

    async def run():
        with pytest.raises(
            SubmissionCapabilityError, match="not a submission capability"
        ):
            await adapter._submit_governed_order(buy_payload(), capability=Lookalike())
        await client.aclose()

    asyncio.run(run())
    assert timeline == []


def test_the_governed_submitter_still_reaches_the_venue(monkeypatch):
    """The gate closes the bypass without closing the door."""
    timeline: list[str] = []
    conn = Conn(timeline)
    adapter, client = make_adapter(timeline, monkeypatch)
    submitter = GovernedOrderSubmitter(
        conn=conn, adapter=adapter, broker_account_id=ACCOUNT
    )

    async def run():
        response = await submitter.submit(
            buy_payload(), strategy=MOM, strategy_version=VERSION, now=NOW
        )
        await client.aclose()
        return response

    response = asyncio.run(run())
    assert response.payload["id"] == "paper-order-1"
    assert len(posts(timeline)) == 1
    assert timeline == [
        "db:insert-attribution", "db:commit", "db:read-attribution",
        "http:POST /v2/orders",
    ]


def test_the_frozen_adapter_keeps_its_buy_only_contract(monkeypatch):
    """The gate is on the portfolio release only. The frozen 1.0.0 adapter is
    unchanged: it still buys, still refuses sells, and needs no capability."""
    from app.brokers.alpaca_paper import AlpacaPaperBrokerAdapter
    from app.brokers.base import BrokerMutationDisabled

    timeline: list[str] = []
    make_adapter(timeline, monkeypatch)  # for the paper settings only
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "frozen-order"})
        ),
        base_url=settings.alpaca_paper_base_url,
    )
    frozen = AlpacaPaperBrokerAdapter(client=client)

    async def run():
        response = await frozen.submit_order(
            {"symbol": "AAPL", "side": "buy", "qty": "1",
             "type": "market", "time_in_force": "day"}
        )
        assert response.payload["id"] == "frozen-order"
        with pytest.raises(BrokerMutationDisabled, match="buy orders only"):
            await frozen.submit_order({"symbol": "AAPL", "side": "sell", "qty": "1"})
        await client.aclose()

    asyncio.run(run())
    assert frozen.capabilities == ("read", "buy")


def test_the_capability_module_imports_nothing():
    """It is named by both the adapter and the service, so it must depend on
    neither -- and must not drag a database import into the broker layer."""
    import ast
    import inspect

    from app.brokers import submission_capability

    tree = ast.parse(inspect.getsource(submission_capability))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {"__future__", "typing"}


def test_no_database_dependency_entered_the_adapter_module():
    import ast
    import inspect

    from app.brokers import alpaca_paper

    source = inspect.getsource(alpaca_paper)
    for banned in ("psycopg", "app.db", "sqlite", "strategy_order_attributions"):
        assert banned not in source

    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("repository" in name or "governed" in name for name in imported)


def test_the_capability_carries_no_quantity():
    from app.brokers.submission_capability import SubmissionCapability

    assert not any(
        "quantity" in slot or "notional" in slot
        for slot in SubmissionCapability.__slots__
    )
