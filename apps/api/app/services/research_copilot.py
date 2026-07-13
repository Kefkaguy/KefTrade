import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.services.research_intelligence import build_research_intelligence, collect_evidence, evidence_ref
from app.settings import settings

logger = logging.getLogger("keftrade.research_copilot")

INSUFFICIENT_EVIDENCE = "There is currently insufficient evidence to answer this confidently."
SAFETY_REFUSAL = "I cannot recommend buying, selling, trading, or generating signals. I can only explain stored KefTrade research evidence."

TRADING_ACTION_PATTERNS = (
    "should i buy",
    "should i sell",
    "buy ",
    "sell ",
    "go long",
    "go short",
    "trade now",
    "entry signal",
    "exit signal",
    "what should i trade",
)

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "what",
    "why",
    "how",
    "which",
    "does",
    "did",
    "after",
    "about",
    "into",
    "from",
    "this",
    "that",
    "were",
    "have",
    "has",
}

SYSTEM_PROMPT = """You are KefTrade's AI Research Copilot.
Use only the provided KefTrade research context.
Never invent research, metrics, citations, or conclusions.
Never recommend buying, selling, trade execution, or live signals.
If the context is insufficient, answer exactly: There is currently insufficient evidence to answer this confidently.
Return JSON only with keys: answer, evidence_refs, confidence."""


class LLMProvider(Protocol):
    model: str
    provider_name: str

    def answer(self, question: str, context: dict[str, Any]) -> dict[str, Any]:
        ...


ResearchLLM = LLMProvider


@dataclass(frozen=True)
class CopilotResult:
    answer: str
    evidence_refs: list[str]
    confidence: str
    model: str
    safety_flags: list[str]
    context_summary: dict[str, Any]


class OpenAIResearchLLM:
    provider_name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def answer(self, question: str, context: dict[str, Any]) -> dict[str, Any]:
        logger.warning("research_copilot.openai.request model=%s", self.model)
        response = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "input": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"question": question, "context": context}, default=str)},
                ],
                "text": {"format": {"type": "json_object"}},
            },
            timeout=30,
        )
        logger.warning("research_copilot.openai.response status_code=%s", response.status_code)
        response.raise_for_status()
        payload = response.json()
        text = extract_response_text(payload)
        return json.loads(text)


class OpenAIProvider(OpenAIResearchLLM):
    pass


class GroqProvider:
    provider_name = "groq"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def answer(self, question: str, context: dict[str, Any]) -> dict[str, Any]:
        logger.warning("research_copilot.groq.request model=%s", self.model)
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"question": question, "context": context}, default=str)},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        logger.warning("research_copilot.groq.response status_code=%s", response.status_code)
        response.raise_for_status()
        payload = response.json()
        return json.loads(extract_chat_completion_text(payload))


class ExtractiveResearchLLM:
    model = "extractive-research-copilot"
    provider_name = "deterministic"

    def answer(self, question: str, context: dict[str, Any]) -> dict[str, Any]:
        evidence_refs = context.get("allowed_evidence_refs", [])[:5]
        if not evidence_refs:
            return {"answer": INSUFFICIENT_EVIDENCE, "evidence_refs": [], "confidence": "low"}
        if context.get("recommendations"):
            if context.get("rankings") and asks_about_ranking(question):
                top = context["rankings"][0]
                return {
                    "answer": f"Stored Research Intelligence ranks {top['candidate_id']} highest with score {top['research_score']} ({top['classification']}). Reason: {top['ranking_reason']}",
                    "evidence_refs": top["source_evidence_refs"],
                    "confidence": "medium",
                }
            recommendation = context["recommendations"][0]
            return {
                "answer": f"Stored research suggests: {recommendation['finding']} Next research step: {recommendation['recommendation']}",
                "evidence_refs": recommendation["evidence_refs"],
                "confidence": recommendation.get("confidence", "low"),
            }
        if context.get("archive"):
            row = context["archive"][0]
            return {
                "answer": f"The strongest matching stored record is {row['candidate_id']} with recommendation {row['recommendation']}.",
                "evidence_refs": [row["evidence_ref"]],
                "confidence": "low",
            }
        return {"answer": INSUFFICIENT_EVIDENCE, "evidence_refs": [], "confidence": "low"}


def make_default_llm() -> ResearchLLM:
    logger.warning(
        "research_copilot.config provider=%s openai_model=%s groq_model=%s openai_key_present=%s groq_key_present=%s",
        settings.llm_provider,
        settings.openai_model,
        settings.groq_model,
        bool(settings.openai_api_key),
        bool(settings.groq_api_key),
    )
    return make_llm_from_config(
        llm_provider=settings.llm_provider,
        openai_api_key=settings.openai_api_key,
        openai_model=settings.openai_model,
        legacy_openai_model=settings.research_copilot_model,
        groq_api_key=settings.groq_api_key,
        groq_model=settings.groq_model,
    )


def make_llm_from_config(
    llm_provider: str,
    openai_api_key: str | None = None,
    openai_model: str = "gpt-5-mini",
    groq_api_key: str | None = None,
    groq_model: str = "llama-3.3-70b-versatile",
    legacy_openai_model: str | None = None,
) -> ResearchLLM:
    provider = llm_provider.strip().lower()
    if provider == "groq":
        if groq_api_key:
            logger.warning("research_copilot.provider_initialized provider=groq model=%s", groq_model)
            return GroqProvider(groq_api_key, groq_model)
        logger.warning("research_copilot.provider_fallback provider=groq reason=missing_groq_api_key")
        return ExtractiveResearchLLM()
    if provider == "openai":
        if openai_api_key:
            model = legacy_openai_model or openai_model
            logger.warning("research_copilot.provider_initialized provider=openai model=%s", model)
            return OpenAIProvider(openai_api_key, model)
        logger.warning("research_copilot.provider_fallback provider=openai reason=missing_openai_api_key")
        return ExtractiveResearchLLM()
    logger.warning("research_copilot.provider_fallback provider=%s reason=unsupported_provider", provider)
    return ExtractiveResearchLLM()


class ResearchCopilot:
    def __init__(self, llm: ResearchLLM | None = None) -> None:
        self.llm = llm or make_default_llm()

    def ask(
        self,
        question: str,
        hypotheses: list[dict[str, Any]],
        experiments: list[dict[str, Any]],
        journal_entries: list[dict[str, Any]],
        validation_runs: list[dict[str, Any]],
    ) -> CopilotResult:
        safety_flags = safety_flags_for_question(question)
        if safety_flags:
            return CopilotResult(
                answer=SAFETY_REFUSAL,
                evidence_refs=[],
                confidence="none",
                model=self.llm.model,
                safety_flags=safety_flags,
                context_summary={"blocked": True, "reason": "trading_action_request"},
            )

        context = build_copilot_context(question, hypotheses, experiments, journal_entries, validation_runs)
        logger.warning(
            "research_copilot.context provider=%s model=%s hypotheses=%s experiments=%s journal_entries=%s validation_runs=%s allowed_refs=%s",
            getattr(self.llm, "provider_name", "unknown"),
            self.llm.model,
            len(hypotheses),
            len(experiments),
            len(journal_entries),
            len(validation_runs),
            len(context["allowed_evidence_refs"]),
        )
        allowed_refs = set(context["allowed_evidence_refs"])
        if not allowed_refs:
            logger.warning("research_copilot.fallback reason=no_relevant_evidence provider=%s model=%s", getattr(self.llm, "provider_name", "unknown"), self.llm.model)
            return insufficient_result(self.llm.model, context, ["no_relevant_evidence"])

        model = self.llm.model
        provider_flags: list[str] = []
        try:
            raw = self.llm.answer(question, context)
        except Exception as exc:
            logger.exception(
                "research_copilot.fallback reason=llm_provider_error provider=%s model=%s exception=%s",
                getattr(self.llm, "provider_name", "unknown"),
                self.llm.model,
                exc,
            )
            fallback = ExtractiveResearchLLM()
            raw = fallback.answer(question, context)
            model = fallback.model
            provider_flags.append("llm_provider_error")
        answer = str(raw.get("answer") or "").strip()
        refs = [str(ref) for ref in raw.get("evidence_refs", []) if str(ref) in allowed_refs]
        if answer == INSUFFICIENT_EVIDENCE:
            return insufficient_result(model, context, provider_flags + ["llm_reported_insufficient_evidence"])
        if not answer or not refs:
            return insufficient_result(model, context, provider_flags + ["missing_supported_citations"])
        if contains_trading_action_advice(answer):
            return CopilotResult(
                answer=SAFETY_REFUSAL,
                evidence_refs=[],
                confidence="none",
                model=model,
                safety_flags=provider_flags + ["unsafe_model_output"],
                context_summary=summarize_context(context),
            )
        return CopilotResult(
            answer=answer,
            evidence_refs=refs,
            confidence=str(raw.get("confidence") or "low"),
            model=model,
            safety_flags=provider_flags,
            context_summary=summarize_context(context),
        )


def build_copilot_context(
    question: str,
    hypotheses: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    journal_entries: list[dict[str, Any]],
    validation_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    intelligence = build_research_intelligence(hypotheses, experiments, journal_entries, validation_runs)
    evidence = collect_evidence(experiments, validation_runs)
    tokens = tokenize(question)
    archive = rank_archive_matches(intelligence["archive"], tokens)[:8]
    rankings = rank_intelligence_matches(intelligence.get("rankings", []), tokens)[:8]
    recommendations = rank_recommendation_matches(intelligence["recommendations"], tokens)[:5]
    meta = {
        "common_failure_reasons": intelligence["meta_analysis"]["most_common_failure_reasons"][:8],
        "common_rejection_rules": intelligence["meta_analysis"]["most_common_rejection_rules"][:8],
        "strategy_family_performance": intelligence["meta_analysis"]["strategy_family_performance"][:8],
        "regime_specific_performance": intelligence["meta_analysis"]["regime_specific_performance"][:8],
        "strongest_indicator_combinations": intelligence["meta_analysis"]["strongest_indicator_combinations"][:8],
        "weakest_indicator_combinations": intelligence["meta_analysis"]["weakest_indicator_combinations"][:8],
    }
    evidence_refs = sorted(
        {
            row["evidence_ref"]
            for row in archive
        }
        | {
            ref
            for row in recommendations
            for ref in row.get("evidence_refs", [])
        }
        | {
            ref
            for row in rankings
            for ref in row.get("source_evidence_refs", [])
        }
        | {
            evidence_ref(row)
            for row in evidence
            if evidence_matches_tokens(row, tokens)
        }
        | meta_evidence_refs(meta, tokens)
    )
    return {
        "question": question,
        "summary": intelligence["summary"],
        "archive": archive,
        "rankings": rankings,
        "recommendations": recommendations,
        "meta_analysis": meta,
        "allowed_evidence_refs": evidence_refs,
    }


def rank_archive_matches(archive: list[dict[str, Any]], tokens: set[str]) -> list[dict[str, Any]]:
    scored = []
    for row in archive:
        haystack = " ".join(
            [
                row["candidate_id"],
                row["strategy"],
                row["recommendation"],
                " ".join(row["indicators"]),
                " ".join(row["assets"]),
                " ".join(row["timeframes"]),
                " ".join(row["market_regimes"]),
                " ".join(row["failure_reasons"]),
            ]
        ).lower()
        haystack_tokens = tokenize(haystack)
        score = len(tokens & haystack_tokens)
        if score:
            scored.append((score, row))
    return [row for _score, row in sorted(scored, key=lambda item: item[0], reverse=True)]


def rank_recommendation_matches(recommendations: list[dict[str, Any]], tokens: set[str]) -> list[dict[str, Any]]:
    scored = []
    for row in recommendations:
        haystack = f"{row['title']} {row['finding']} {row['recommendation']}".lower()
        haystack_tokens = tokenize(haystack)
        score = len(tokens & haystack_tokens)
        if score:
            scored.append((score, row))
    return [row for _score, row in sorted(scored, key=lambda item: item[0], reverse=True)]


def rank_intelligence_matches(rankings: list[dict[str, Any]], tokens: set[str]) -> list[dict[str, Any]]:
    if not tokens:
        return rankings[:5]
    scored = []
    for row in rankings:
        haystack = json.dumps(
            {
                "candidate_id": row.get("candidate_id"),
                "symbol": row.get("symbol"),
                "strategy": row.get("strategy"),
                "classification": row.get("classification"),
                "review_priority": row.get("review_priority"),
                "blocking_issues": row.get("blocking_issues"),
            },
            default=str,
        ).lower()
        score = len(tokens & tokenize(haystack))
        if score:
            scored.append((score, row))
    return [row for _score, row in sorted(scored, key=lambda item: item[0], reverse=True)]


def asks_about_ranking(question: str) -> bool:
    lowered = question.lower()
    return any(term in lowered for term in ("strongest", "ranked", "ranking", "best evidence", "review first", "blocking", "concentrated"))


def meta_evidence_refs(meta: dict[str, Any], tokens: set[str]) -> set[str]:
    refs: set[str] = set()
    for section in meta.values():
        if not isinstance(section, list):
            continue
        for row in section:
            haystack_tokens = tokenize(json.dumps(row, default=str))
            if tokens & haystack_tokens:
                refs.update(str(ref) for ref in row.get("evidence_refs", []))
    return refs


def evidence_matches_tokens(row: dict[str, Any], tokens: set[str]) -> bool:
    haystack = json.dumps(
        {
            "candidate_id": row["candidate_id"],
            "strategy": f"{row['strategy_name']}_{row['strategy_version']}",
            "blocks": row["blocks"],
            "parameters": row["parameters"],
            "recommendation": row["recommendation"],
        },
        default=str,
    ).lower()
    return bool(tokens & tokenize(haystack))


def safety_flags_for_question(question: str) -> list[str]:
    lowered = question.lower()
    return ["trading_action_request"] if any(pattern in lowered for pattern in TRADING_ACTION_PATTERNS) else []


def contains_trading_action_advice(answer: str) -> bool:
    lowered = answer.lower()
    unsafe_patterns = ("you should buy", "you should sell", "enter a trade", "go long", "go short")
    return any(pattern in lowered for pattern in unsafe_patterns)


def insufficient_result(model: str, context: dict[str, Any], safety_flags: list[str]) -> CopilotResult:
    return CopilotResult(
        answer=INSUFFICIENT_EVIDENCE,
        evidence_refs=[],
        confidence="low",
        model=model,
        safety_flags=safety_flags,
        context_summary=summarize_context(context),
    )


def summarize_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "archive_records": len(context.get("archive", [])),
        "recommendations": len(context.get("recommendations", [])),
        "evidence_refs": len(context.get("allowed_evidence_refs", [])),
        "research_summary": context.get("summary", {}),
    }


def tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9_]+", text.lower())
        if len(token) > 2 and token not in STOPWORDS
    }


def extract_response_text(payload: dict[str, Any]) -> str:
    if "output_text" in payload:
        return str(payload["output_text"])
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                return str(content.get("text", ""))
    return "{}"


def extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return "{}"
    message = choices[0].get("message", {})
    return str(message.get("content", "{}"))
