from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.brokers.base import BrokerMutationDisabled, BrokerResponse
from app.settings import settings


class AlpacaPaperBrokerAdapter:
    provider = "alpaca"
    environment = "paper"
    adapter_version = "1.0.0"
    adapter_contract_version = "1"
    provider_api_version = "trading-v2"
    normalization_version = "1"
    behavior_version = "1"
    change_class = "compatible_patch"
    compatible_from = "1.0.0"
    # Declared so a deployment can see what it approved. This release reads and
    # buys; it has never been able to sell.
    capabilities = ("read", "buy")

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        validate_paper_configuration()
        self._provided_client = client

    def _client(self) -> httpx.AsyncClient:
        if self._provided_client is not None:
            return self._provided_client
        return httpx.AsyncClient(
            base_url=settings.alpaca_paper_base_url,
            timeout=30,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_paper_api_key or "",
                "APCA-API-SECRET-KEY": settings.alpaca_paper_secret_key or "",
            },
        )

    async def _get(self, path: str, endpoint_class: str, params: dict[str, Any] | None = None) -> BrokerResponse:
        client = self._client()
        owns_client = self._provided_client is None
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return BrokerResponse(
                endpoint_class=endpoint_class,
                status_code=response.status_code,
                payload=response.json(),
                request_id=response.headers.get("X-Request-ID"),
            )
        finally:
            if owns_client:
                await client.aclose()

    async def _mutate(self, method: str, path: str, endpoint_class: str, payload: dict[str, Any] | None = None) -> BrokerResponse:
        client = self._client()
        owns_client = self._provided_client is None
        try:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            body = response.json() if response.content else {}
            return BrokerResponse(endpoint_class=endpoint_class, status_code=response.status_code, payload=body, request_id=response.headers.get("X-Request-ID"))
        finally:
            if owns_client:
                await client.aclose()

    async def get_account(self) -> BrokerResponse:
        return await self._get("/v2/account", "account")

    async def get_clock(self) -> BrokerResponse:
        return await self._get("/v2/clock", "clock")

    async def list_orders(self) -> BrokerResponse:
        return await self._get("/v2/orders", "orders", {"status": "all", "limit": 500, "direction": "asc", "nested": "false"})

    async def list_positions(self) -> BrokerResponse:
        return await self._get("/v2/positions", "positions")

    async def list_fill_activities(self) -> BrokerResponse:
        return await self._get("/v2/account/activities/FILL", "fill_activities", {"direction": "asc", "page_size": 100})

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerResponse:
        return await self._get("/v2/orders:by_client_order_id", "order_by_client_id", {"client_order_id": client_order_id})

    async def submit_order(self, payload: dict[str, Any]) -> BrokerResponse:
        """Buy-only, exactly as this adapter version was approved.

        Position-reducing sells are a *new mutation capability*, not a patch to
        this one. They live in ``AlpacaPaperPortfolioAdapter`` under its own
        release identity, so a deployment approved against 1.0.0 cannot inherit
        them by upgrading in place.
        """
        if str(payload.get("side") or "").lower() != "buy":
            raise BrokerMutationDisabled("KefTrade external paper supports buy orders only")
        if not settings.broker_order_submission_enabled or not settings.external_paper_execution_enabled:
            raise BrokerMutationDisabled("both broker execution flags must be enabled for Alpaca Paper mutation")
        # qty and notional are mutually exclusive at Alpaca, and fractional
        # orders are market/day only. Checking here means a malformed payload is
        # a local error rather than a remote reject whose outcome is ambiguous.
        # This is a tightening of an existing capability, not a new one.
        from app.services.fractional_execution import validate_order_payload

        validate_order_payload(payload)
        return await self._mutate("POST", "/v2/orders", "submit_order", payload)

    async def cancel_order(self, broker_order_id: str) -> BrokerResponse:
        if not settings.broker_order_submission_enabled or not settings.external_paper_execution_enabled:
            raise BrokerMutationDisabled("both broker execution flags must be enabled for Alpaca Paper mutation")
        return await self._mutate("DELETE", f"/v2/orders/{broker_order_id}", "cancel_order")




class AlpacaPaperPortfolioAdapter(AlpacaPaperBrokerAdapter):
    """Alpaca Paper with position-reducing sells, under a distinct release.

    Adding sells to the 1.0.0 adapter would have shipped a new mutation
    capability under a frozen identity, and ``change_class="compatible_patch"``
    would have carried it through the approval gate that exists to catch exactly
    that. So the capability is a separate release instead:

    * ``adapter_version`` 2.0.0-portfolio, ``behavior_version`` 2;
    * ``change_class`` **behavioral_change**, which
      ``external_execution.enable_observe_only`` refuses -- it admits only
      ``compatible_patch`` -- so an existing approved deployment cannot inherit
      sell capability by upgrading. Approving this adapter is a deliberate,
      separate act.

    Everything read-only is inherited unchanged, so there is one book-reading
    implementation rather than two that can disagree.
    """

    adapter_version = "2.0.0-portfolio"
    behavior_version = "2"
    change_class = "behavioral_change"
    compatible_from = "2.0.0-portfolio"
    capabilities = ("read", "buy", "position_reducing_sell")

    async def submit_order(
        self,
        payload: dict[str, Any],
        *,
        confirmed_positions: dict[str, Any] | None = None,
        ownership_ledger: Any = None,
        reconciliation: Any = None,
    ) -> BrokerResponse:
        """A buy, or a sell that provably reduces a position this strategy owns.

        A sell must satisfy both bounds, and neither substitutes for the other:
        it may not exceed what the strategy *owns*, and it may not exceed what
        the broker *confirms exists*. The first stops a rebalance liquidating
        another strategy's holding; the second stops it opening a short.
        """
        side = str(payload.get("side") or "").lower()
        if side == "buy":
            return await super().submit_order(payload)
        if side != "sell":
            raise BrokerMutationDisabled(
                "KefTrade external paper supports buy orders and "
                "position-reducing sells only"
            )

        from app.services.position_reducing_sell import (
            assert_sell_is_position_reducing,
        )
        from app.services.strategy_ownership import (
            assert_within_strategy_ownership,
            require_clean_reconciliation,
            require_ownership_ledger,
        )

        if confirmed_positions is None:
            raise BrokerMutationDisabled(
                "a sell requires the confirmed long positions it reduces; "
                "KefTrade never sells against a remembered position"
            )
        # Evidence, not a default. Omission is a refusal.
        evidence = require_clean_reconciliation(reconciliation)
        ledger = require_ownership_ledger(
            ownership_ledger, strategy=str(payload.get("strategy") or "")
        )
        from decimal import Decimal

        assert_within_strategy_ownership(
            strategy=ledger.strategy,
            symbol=str(payload.get("symbol") or ""),
            requested_qty=Decimal(str(payload.get("qty"))),
            ledger=ledger,
        )
        assert_sell_is_position_reducing(payload, confirmed_positions)

        if not settings.broker_order_submission_enabled or not settings.external_paper_execution_enabled:
            raise BrokerMutationDisabled("both broker execution flags must be enabled for Alpaca Paper mutation")

        from app.services.fractional_execution import validate_order_payload

        wire = {k: v for k, v in payload.items() if k != "strategy"}
        validate_order_payload(wire)

        # The last thing before the order leaves the process. Everything above
        # ran against a snapshot; this runs against the broker.
        await self._revalidate_reduction_now(wire, confirmed_positions, evidence, ledger)
        return await self._mutate("POST", "/v2/orders", "submit_order", wire)

    async def _revalidate_reduction_now(
        self,
        payload: dict[str, Any],
        stored_positions: dict[str, Any],
        evidence: Any,
        ledger: Any,
    ) -> None:
        """Re-read Alpaca's positions and refuse unless they still support the sell.

        Staleness bounds how old a stored snapshot may be; it cannot bound what
        happened since. A fill, a corporate action, or another process can move a
        position between planning and submission, and the only way to know is to
        ask the venue at the moment of the mutation.

        Any failure refuses -- timeout, malformed body, vanished position,
        smaller position, or disagreement with the plan. Nothing is clamped: an
        oversized sell means the plan rests on state that no longer exists, and
        the answer is to recompute it.
        """
        from decimal import Decimal

        from app.services.position_reducing_sell import (
            BLOCKER_FRESH_READ_FAILED,
            StalePositionAtSubmit,
            parse_broker_positions,
            revalidate_reduction_against_fresh_positions,
        )
        from app.services.strategy_ownership import assert_within_strategy_ownership

        # Environment and flags are re-asserted here rather than trusted from
        # construction time: this is the instant that matters.
        validate_paper_configuration()
        if not settings.broker_order_submission_enabled or not settings.external_paper_execution_enabled:
            raise BrokerMutationDisabled(
                "both broker execution flags must be enabled at the mutation boundary"
            )

        try:
            fresh = await self.list_positions()
        except Exception as error:
            raise StalePositionAtSubmit(
                "could not re-read Alpaca positions immediately before "
                f"submitting a reduction ({error.__class__.__name__}); the sell "
                f"is refused rather than sent blind; {BLOCKER_FRESH_READ_FAILED}"
            ) from error

        symbol = str(payload.get("symbol") or "")
        requested = Decimal(str(payload.get("qty")))
        positions = parse_broker_positions(
            fresh.payload,
            observed_at=datetime.now(UTC),
            reconciliation_status=evidence.status,
        )
        revalidate_reduction_against_fresh_positions(
            symbol=symbol,
            requested_qty=requested,
            fresh_positions=positions,
            stored=stored_positions.get(symbol),
            reconciliation=evidence,
        )
        # Ownership is re-asserted against the fresh read too: the availability
        # bound moving does not widen what this strategy is entitled to sell.
        assert_within_strategy_ownership(
            strategy=ledger.strategy,
            symbol=symbol,
            requested_qty=requested,
            ledger=ledger,
        )

def validate_paper_configuration() -> None:
    if settings.broker_provider != "alpaca":
        raise RuntimeError("Phase 10 supports BROKER_PROVIDER=alpaca only")
    if settings.alpaca_paper_base_url.rstrip("/") != "https://paper-api.alpaca.markets":
        raise RuntimeError("ALPACA_PAPER_BASE_URL must be the Alpaca paper endpoint")
    if not settings.alpaca_paper_api_key or not settings.alpaca_paper_secret_key:
        raise RuntimeError("ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY are required")
