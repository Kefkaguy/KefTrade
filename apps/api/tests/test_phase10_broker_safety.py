from __future__ import annotations

import asyncio
import ast
from pathlib import Path

import httpx
import pytest

from app.brokers.alpaca_paper import AlpacaPaperBrokerAdapter
from app.brokers.base import BrokerMutationDisabled
from app.services.external_execution import assert_execution_disabled, bar_is_complete, feature_flags
from app.services.broker_sync import canonical_json, normalize_account, normalize_order, sanitize_value
from app.settings import settings


ROOT = Path(__file__).resolve().parents[1] / "app"


def configured_adapter(monkeypatch: pytest.MonkeyPatch, handler) -> AlpacaPaperBrokerAdapter:
    monkeypatch.setattr(settings, "broker_provider", "alpaca")
    monkeypatch.setattr(settings, "alpaca_paper_base_url", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(settings, "alpaca_paper_api_key", "paper-key")
    monkeypatch.setattr(settings, "alpaca_paper_secret_key", "paper-secret")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.alpaca_paper_base_url)
    return AlpacaPaperBrokerAdapter(client=client)


def test_adapter_only_uses_read_only_alpaca_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = [] if request.url.path.endswith(("orders", "positions", "FILL")) else {"id": "paper-account"}
        return httpx.Response(200, json=payload)

    adapter = configured_adapter(monkeypatch, handler)

    async def run() -> None:
        await adapter.get_account()
        await adapter.get_clock()
        await adapter.list_orders()
        await adapter.list_positions()
        await adapter.list_fill_activities()
        await adapter._provided_client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())
    assert requests
    assert {request.method for request in requests} == {"GET"}
    assert all(request.url.host == "paper-api.alpaca.markets" for request in requests)


def test_broker_mutations_are_unimplemented(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = configured_adapter(monkeypatch, lambda _: httpx.Response(500))

    async def run() -> None:
        with pytest.raises(BrokerMutationDisabled):
            await adapter.submit_order({"symbol": "AAPL", "qty": 1})
        with pytest.raises(BrokerMutationDisabled):
            await adapter.cancel_order("order-id")
        await adapter._provided_client.aclose()  # type: ignore[union-attr]

    asyncio.run(run())


def test_execution_flags_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "broker_order_submission_enabled", False)
    monkeypatch.setattr(settings, "external_paper_execution_enabled", False)
    assert_execution_disabled()
    assert feature_flags()["broker_order_submission_enabled"] is False
    assert feature_flags()["external_paper_execution_enabled"] is False

    monkeypatch.setattr(settings, "broker_order_submission_enabled", True)
    with pytest.raises(RuntimeError, match="disabled"):
        assert_execution_disabled()


def test_external_broker_http_surface_is_read_only() -> None:
    source = (ROOT / "routers" / "broker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    decorators = [ast.unparse(item) for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for item in node.decorator_list]
    assert decorators
    assert all("router.get" in decorator for decorator in decorators)


def test_phase10_modules_have_no_runtime_ddl() -> None:
    paths = [ROOT / "main.py", *(ROOT / name for name in ("routers", "services", "workers", "cli"))]
    files = [path for path in paths if path.is_file()]
    files.extend(child for path in paths if path.is_dir() for child in path.rglob("*.py"))
    forbidden = ("CREATE TABLE", "CREATE INDEX", "ALTER TABLE", "DROP TABLE", "DROP INDEX")
    violations = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bodies = [tree.body, *(node.body for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))]
        for body in bodies:
            for statement in body:
                if isinstance(statement, ast.Return):
                    break
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                for call in (node for node in ast.walk(statement) if isinstance(node, ast.Call)):
                    if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
                        continue
                    if any(token in call.args[0].value.upper() for token in forbidden):
                        violations.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert violations == []


def test_adapter_source_has_no_network_mutation_implementation() -> None:
    source = (ROOT / "brokers" / "alpaca_paper.py").read_text(encoding="utf-8")
    assert ".post(" not in source
    assert ".put(" not in source
    assert ".patch(" not in source
    assert ".delete(" not in source
    assert source.count("raise BrokerMutationDisabled") == 2


def test_completed_bar_gate_is_strict() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert bar_is_complete(now - timedelta(hours=2), "1h", now=now)
    assert not bar_is_complete(now - timedelta(minutes=30), "1h", now=now)


def test_raw_evidence_sanitization_rejects_nested_credentials() -> None:
    sanitized = sanitize_value({"Authorization": "Bearer secret", "nested": {"APCA-API-SECRET-KEY": "secret", "safe": "value"}, "url": "https://example.test/path"})
    assert "Authorization" not in sanitized
    assert "APCA-API-SECRET-KEY" not in sanitized["nested"]
    assert sanitized["nested"]["safe"] == "value"


def test_normalized_broker_metrics_are_json_serializable() -> None:
    account = normalize_account({"cash": "100000.00", "equity": "100000.00", "buying_power": "400000.00"})
    order = normalize_order({"id": "order-1", "symbol": "AAPL", "side": "buy", "qty": "1", "filled_qty": "0"})
    assert '"cash":"100000.00"' in canonical_json(account)
    assert '"requested_quantity":"1"' in canonical_json(order)
