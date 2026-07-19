from __future__ import annotations

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

    async def submit_order(self, payload: dict[str, Any]) -> BrokerResponse:
        raise BrokerMutationDisabled("broker order submission is not implemented in the Phase 10 read-only foundation")

    async def cancel_order(self, broker_order_id: str) -> BrokerResponse:
        raise BrokerMutationDisabled("broker order cancellation is not implemented in the Phase 10 read-only foundation")


def validate_paper_configuration() -> None:
    if settings.broker_provider != "alpaca":
        raise RuntimeError("Phase 10 supports BROKER_PROVIDER=alpaca only")
    if settings.alpaca_paper_base_url.rstrip("/") != "https://paper-api.alpaca.markets":
        raise RuntimeError("ALPACA_PAPER_BASE_URL must be the Alpaca paper endpoint")
    if not settings.alpaca_paper_api_key or not settings.alpaca_paper_secret_key:
        raise RuntimeError("ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY are required")
