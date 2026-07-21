from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BrokerResponse:
    endpoint_class: str
    status_code: int
    payload: Any
    request_id: str | None = None


class BrokerMutationDisabled(RuntimeError):
    pass


class BrokerAdapter(Protocol):
    provider: str
    environment: str
    adapter_version: str
    adapter_contract_version: str
    provider_api_version: str
    normalization_version: str
    behavior_version: str
    change_class: str
    compatible_from: str

    async def get_account(self) -> BrokerResponse: ...
    async def get_clock(self) -> BrokerResponse: ...
    async def list_orders(self) -> BrokerResponse: ...
    async def list_positions(self) -> BrokerResponse: ...
    async def list_fill_activities(self) -> BrokerResponse: ...
    async def get_order_by_client_id(self, client_order_id: str) -> BrokerResponse: ...

    async def submit_order(self, payload: dict[str, Any]) -> BrokerResponse: ...
    async def cancel_order(self, broker_order_id: str) -> BrokerResponse: ...
