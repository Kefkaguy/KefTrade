import pytest

from app.services.research_copilot import (
    INSUFFICIENT_EVIDENCE,
    SAFETY_REFUSAL,
    ExtractiveResearchLLM,
    GroqProvider,
    OpenAIProvider,
    ResearchCopilot,
    make_llm_from_config,
)
from tests.test_research_intelligence import research_history


class FakeLLM:
    model = "fake-llm"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def answer(self, question, context):
        self.calls.append({"question": question, "context": context})
        return self.payload


class FailingLLM:
    model = "failing-llm"

    def answer(self, question, context):
        raise RuntimeError("provider unavailable")


class FakeResponse:
    def __init__(self, payload, status_error=False):
        self.payload = payload
        self.status_error = status_error
        self.status_code = 500 if status_error else 200

    def raise_for_status(self):
        if self.status_error:
            raise RuntimeError("provider failed")

    def json(self):
        return self.payload


def test_copilot_returns_grounded_response_with_citations() -> None:
    llm = FakeLLM(
        {
            "answer": "generated_alpha_v1 was rejected because stored validation evidence shows failed stability.",
            "evidence_refs": ["validation_run:20"],
            "confidence": "low",
        }
    )

    result = ResearchCopilot(llm).ask("Why did generated_alpha fail stability?", *research_history())

    assert result.answer.startswith("generated_alpha_v1 was rejected")
    assert result.evidence_refs == ["validation_run:20"]
    assert result.model == "fake-llm"
    assert llm.calls[0]["context"]["allowed_evidence_refs"]


def test_copilot_reports_insufficient_evidence_when_no_records_match() -> None:
    llm = FakeLLM({"answer": "Unsupported answer", "evidence_refs": ["validation_run:20"], "confidence": "low"})

    result = ResearchCopilot(llm).ask("What happened after earnings for AAPL?", *research_history())

    assert result.answer == INSUFFICIENT_EVIDENCE
    assert result.evidence_refs == []
    assert "no_relevant_evidence" in result.safety_flags


def test_copilot_blocks_trading_action_requests() -> None:
    llm = FakeLLM({"answer": "unused", "evidence_refs": ["validation_run:20"], "confidence": "low"})

    result = ResearchCopilot(llm).ask("Should I buy QQQ today?", *research_history())

    assert result.answer == SAFETY_REFUSAL
    assert result.evidence_refs == []
    assert result.safety_flags == ["trading_action_request"]
    assert llm.calls == []


def test_copilot_rejects_hallucinated_citations() -> None:
    llm = FakeLLM(
        {
            "answer": "This answer cites a made-up source.",
            "evidence_refs": ["experiment:9999"],
            "confidence": "high",
        }
    )

    result = ResearchCopilot(llm).ask("Compare generated_alpha evidence.", *research_history())

    assert result.answer == INSUFFICIENT_EVIDENCE
    assert result.evidence_refs == []
    assert "missing_supported_citations" in result.safety_flags


def test_copilot_rejects_unsafe_model_output() -> None:
    llm = FakeLLM(
        {
            "answer": "Based on the research, you should buy QQQ.",
            "evidence_refs": ["validation_run:20"],
            "confidence": "low",
        }
    )

    result = ResearchCopilot(llm).ask("Summarize generated_alpha evidence.", *research_history())

    assert result.answer == SAFETY_REFUSAL
    assert result.evidence_refs == []
    assert result.safety_flags == ["unsafe_model_output"]


def test_copilot_falls_back_when_llm_provider_fails() -> None:
    result = ResearchCopilot(FailingLLM()).ask("What should we research next?", *research_history())

    assert result.model == "extractive-research-copilot"
    assert result.evidence_refs
    assert "llm_provider_error" in result.safety_flags


def test_openai_provider_success(monkeypatch) -> None:
    def fake_post(url, headers, json, timeout):
        assert url == "https://api.openai.com/v1/responses"
        assert headers["Authorization"] == "Bearer test-key"
        return FakeResponse(
            {
                "output_text": (
                    '{"answer":"OpenAI grounded answer.",'
                    '"evidence_refs":["validation_run:20"],"confidence":"medium"}'
                )
            }
        )

    monkeypatch.setattr("app.services.research_copilot.httpx.post", fake_post)

    result = OpenAIProvider("test-key", "gpt-5-mini").answer("question", {"allowed_evidence_refs": ["validation_run:20"]})

    assert result["answer"] == "OpenAI grounded answer."
    assert result["evidence_refs"] == ["validation_run:20"]


def test_openai_provider_failure_falls_back(monkeypatch) -> None:
    def fake_post(url, headers, json, timeout):
        return FakeResponse({}, status_error=True)

    monkeypatch.setattr("app.services.research_copilot.httpx.post", fake_post)

    result = ResearchCopilot(OpenAIProvider("test-key", "gpt-5-mini")).ask("What should we research next?", *research_history())

    assert result.model == "extractive-research-copilot"
    assert "llm_provider_error" in result.safety_flags


def test_groq_provider_success(monkeypatch) -> None:
    def fake_post(url, headers, json, timeout):
        assert url == "https://api.groq.com/openai/v1/chat/completions"
        assert headers["Authorization"] == "Bearer groq-key"
        assert json["response_format"] == {"type": "json_object"}
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"answer":"Groq grounded answer.",'
                                '"evidence_refs":["validation_run:20"],"confidence":"medium"}'
                            )
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.research_copilot.httpx.post", fake_post)

    result = GroqProvider("groq-key", "llama-3.3-70b-versatile").answer(
        "question", {"allowed_evidence_refs": ["validation_run:20"]}
    )

    assert result["answer"] == "Groq grounded answer."
    assert result["evidence_refs"] == ["validation_run:20"]


def test_groq_provider_failure_falls_back(monkeypatch) -> None:
    def fake_post(url, headers, json, timeout):
        return FakeResponse({}, status_error=True)

    monkeypatch.setattr("app.services.research_copilot.httpx.post", fake_post)

    result = ResearchCopilot(GroqProvider("groq-key", "llama-3.3-70b-versatile")).ask(
        "What should we research next?", *research_history()
    )

    assert result.model == "extractive-research-copilot"
    assert "llm_provider_error" in result.safety_flags


@pytest.mark.parametrize(
    ("provider", "expected_type", "expected_model"),
    [
        ("openai", OpenAIProvider, "gpt-5-mini"),
        ("groq", GroqProvider, "llama-3.3-70b-versatile"),
        ("unknown", ExtractiveResearchLLM, "extractive-research-copilot"),
    ],
)
def test_provider_selection(provider, expected_type, expected_model) -> None:
    llm = make_llm_from_config(
        llm_provider=provider,
        openai_api_key="openai-key",
        openai_model="gpt-5-mini",
        groq_api_key="groq-key",
        groq_model="llama-3.3-70b-versatile",
    )

    assert isinstance(llm, expected_type)
    assert llm.model == expected_model


def test_provider_selection_falls_back_when_credentials_are_missing() -> None:
    openai_llm = make_llm_from_config(llm_provider="openai", openai_api_key=None)
    groq_llm = make_llm_from_config(llm_provider="groq", groq_api_key=None)

    assert isinstance(openai_llm, ExtractiveResearchLLM)
    assert isinstance(groq_llm, ExtractiveResearchLLM)
