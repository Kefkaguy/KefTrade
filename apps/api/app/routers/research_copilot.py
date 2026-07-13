from dataclasses import asdict
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
import psycopg
from psycopg.types.json import Jsonb

from app.db import get_connection
from app.routers.research_intelligence import load_research_history
from app.services.research_copilot import ResearchCopilot
from app.settings import settings

router = APIRouter(tags=["research-copilot"])
logger = logging.getLogger("keftrade.research_copilot")


class CopilotQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@router.post("/research/copilot")
def ask_research_copilot(payload: CopilotQuestion, conn: psycopg.Connection = Depends(get_connection)) -> dict[str, Any]:
    copilot = ResearchCopilot()
    history = load_research_history(conn)
    logger.warning(
        "research_copilot.runtime provider=%s openai_model=%s groq_model=%s openai_key_present=%s groq_key_present=%s database=%s history_counts=%s",
        settings.llm_provider,
        settings.openai_model,
        settings.groq_model,
        bool(settings.openai_api_key),
        bool(settings.groq_api_key),
        sanitized_database_target(settings.database_url),
        {
            "hypotheses": len(history[0]),
            "experiments": len(history[1]),
            "journal_entries": len(history[2]),
            "validation_runs": len(history[3]),
        },
    )
    result = copilot.ask(payload.question, *history, load_strategy_discovery_context(conn))
    interaction_id = log_copilot_interaction(conn, payload.question, result)
    return {"id": interaction_id, **asdict(result)}


@router.get("/research/copilot/interactions")
def list_copilot_interactions(conn: psycopg.Connection = Depends(get_connection)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, question, response, evidence_refs, model, safety_flags, context_summary, created_at
        FROM research_copilot_interactions
        ORDER BY created_at DESC
        LIMIT 100
        """
    ).fetchall()
    return list(rows)


def log_copilot_interaction(conn: psycopg.Connection, question: str, result: Any) -> int:
    row = conn.execute(
        """
        INSERT INTO research_copilot_interactions(question, response, evidence_refs, model, safety_flags, context_summary)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            question,
            result.answer,
            Jsonb(result.evidence_refs),
            result.model,
            Jsonb(result.safety_flags),
            Jsonb(result.context_summary),
        ),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def load_strategy_discovery_context(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT candidate_id, family_id, parent_candidate_id, blocks, metrics, research_score, status, failure_reasons, explanation, created_at
        FROM strategy_discovery_strategies
        ORDER BY research_score DESC, created_at DESC
        LIMIT 50
        """
    ).fetchall()
    return list(rows)


def sanitized_database_target(database_url: str) -> str:
    parsed = urlparse(database_url)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or ''}{parsed.path}"
