from __future__ import annotations

import json
import math
from typing import Any
from uuid import UUID

import httpx
import psycopg
from psycopg.types.json import Jsonb

from app.settings import settings


PROMPT_VERSION = "bounded-risk-v1"
SYSTEM_PROMPT = """You are the shadow risk challenger for an Alpaca Paper research system.
Return JSON only. You may choose enter, wait, or reject. Use only supplied evidence.
Never request more than 1% risk. Never change a stop, target, symbol, or strategy.
Required keys: action, confidence, requested_risk_pct, thesis, invalidation, holding_horizon."""


async def evaluate_model_risk(
    conn: psycopg.Connection,
    *,
    strategy_evaluation: dict[str, Any],
    external_deployment_id: int,
    trace_id: UUID,
) -> dict[str, Any]:
    authority = normalized_authority(settings.model_risk_authority)
    raw, provider, model = await model_answer(strategy_evaluation) if settings.model_risk_enabled else (fallback("reject", "Model risk is disabled."), "deterministic", "disabled")
    action = str(raw.get("action") or "reject").lower()
    if action not in {"enter", "wait", "reject"}:
        action = "reject"
    confidence = clamp(float_value(raw.get("confidence")), 0.0, 1.0)
    requested = clamp(float_value(raw.get("requested_risk_pct")), 0.0, 0.01)
    configured_max = clamp(float(settings.model_risk_max_risk_pct), 0.0, 0.01)
    bounded = min(requested, configured_max)
    deterministic_setup = str(strategy_evaluation.get("signal_type")) == "setup"
    checks = [
        check("MODEL_RISK_ENABLED", settings.model_risk_enabled),
        check("VALID_ACTION", action in {"enter", "wait", "reject"}),
        check("CONFIDENCE_MIN", confidence >= float(settings.model_risk_min_confidence)),
        check("RISK_WITHIN_BOUND", requested <= configured_max),
        check("DETERMINISTIC_SETUP_FOR_PAPER", deterministic_setup or authority != "bounded_paper"),
        check("PAPER_AUTHORITY_REQUIRES_FLAGS", authority != "bounded_paper" or (settings.broker_order_submission_enabled and settings.external_paper_execution_enabled)),
    ]
    approved = action == "enter" and all(item["passed"] for item in checks)
    row = conn.execute(
        """
        INSERT INTO model_risk_decisions(
          strategy_evaluation_id, external_deployment_id, trace_id, provider, model,
          prompt_version, authority_level, requested_action, requested_risk_pct,
          bounded_risk_pct, confidence, thesis, invalidation, holding_horizon,
          raw_response, safety_checks, approved
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(strategy_evaluation_id, provider, model, prompt_version) DO UPDATE
          SET raw_response=EXCLUDED.raw_response, safety_checks=EXCLUDED.safety_checks,
              approved=EXCLUDED.approved, created_at=NOW()
        RETURNING *
        """,
        (
            strategy_evaluation["id"], external_deployment_id, trace_id, provider, model,
            PROMPT_VERSION, authority, action, requested, bounded, confidence,
            str(raw.get("thesis") or "No supported thesis."),
            str(raw.get("invalidation") or "No supported invalidation."),
            str(raw.get("holding_horizon") or "unspecified"), Jsonb(raw), Jsonb(checks), approved,
        ),
    ).fetchone()
    return dict(row)


async def model_answer(strategy_evaluation: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    context = {
        "signal": strategy_evaluation.get("signal_type"),
        "symbol": strategy_evaluation.get("symbol"),
        "timeframe": strategy_evaluation.get("timeframe"),
        "regime": strategy_evaluation.get("regime") or {},
        "gates": strategy_evaluation.get("gates") or [],
        "decision": strategy_evaluation.get("decision") or {},
        "maximum_risk_pct": settings.model_risk_max_risk_pct,
    }
    try:
        if settings.llm_provider.lower() == "openai" and settings.openai_api_key:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                    json={"model": settings.openai_model, "input": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(context, default=str)}], "text": {"format": {"type": "json_object"}}},
                )
            response.raise_for_status()
            payload = response.json()
            text = str(payload.get("output_text") or "")
            if not text:
                for item in payload.get("output", []):
                    for content in item.get("content", []):
                        if content.get("type") in {"output_text", "text"}:
                            text = str(content.get("text") or "")
            return json.loads(text or "{}"), "openai", settings.openai_model
        if settings.llm_provider.lower() == "groq" and settings.groq_api_key:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
                    json={"model": settings.groq_model, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(context, default=str)}], "response_format": {"type": "json_object"}},
                )
            response.raise_for_status()
            return json.loads(response.json()["choices"][0]["message"]["content"]), "groq", settings.groq_model
    except Exception as error:
        return fallback("wait", f"Model provider failed: {error.__class__.__name__}."), "deterministic", "provider-fallback"
    return fallback("wait", "No configured model provider credentials; shadow recommendation withheld."), "deterministic", "no-provider"


def fallback(action: str, thesis: str) -> dict[str, Any]:
    return {"action": action, "confidence": 0, "requested_risk_pct": 0, "thesis": thesis, "invalidation": "Model decision unavailable.", "holding_horizon": "unspecified"}


def normalized_authority(value: str) -> str:
    return value if value in {"observer", "shadow", "bounded_paper"} else "shadow"


def float_value(value: Any) -> float:
    try:
        result = float(value or 0)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def check(code: str, passed: bool) -> dict[str, Any]:
    return {"code": code, "passed": bool(passed)}
