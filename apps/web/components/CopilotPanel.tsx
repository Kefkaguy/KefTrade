"use client";

import { FormEvent, useState } from "react";
import { askCopilot, type CopilotResponse } from "@/lib/api";

const suggestedQuestions = [
  "Why did validation_dac45e27ee fail?",
  "Which evidence rules fail most often?",
  "What should we research next?",
  "Compare SPY and QQQ."
];

export function CopilotPanel() {
  const [question, setQuestion] = useState("What should we research next?");
  const [response, setResponse] = useState<CopilotResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      setResponse(await askCopilot(question));
    } catch {
      setError("Copilot evidence is unavailable. Check the API connection.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <aside className="copilotPanel" aria-label="AI Research Copilot">
      <div className="panelHeader">
        <div>
          <span className="sectionLabel">AI Copilot</span>
          <h2>Ask the research record</h2>
        </div>
        <span className="status setup">Read-only</span>
      </div>

      <form onSubmit={onSubmit} className="copilotForm">
        <textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={4} />
        <button className="button" type="submit" disabled={loading}>
          {loading ? "Reading evidence..." : "Ask"}
        </button>
      </form>

      <div className="suggestionList">
        {suggestedQuestions.map((item) => (
          <button key={item} type="button" onClick={() => setQuestion(item)}>
            {item}
          </button>
        ))}
      </div>

      <section className="answerBox">
        {loading ? (
          <div className="skeletonStack">
            <span />
            <span />
            <span />
          </div>
        ) : response ? (
          <>
            <p>{response.answer}</p>
            <div className="evidenceList">
              {response.evidence_refs?.length ? (
                response.evidence_refs.map((ref) => (
                  <span key={ref}>{ref}</span>
                ))
              ) : (
                <span>No evidence references returned</span>
              )}
            </div>
            <small>
              {response.model} · {response.confidence}
            </small>
          </>
        ) : error ? (
          <p className="errorText">{error}</p>
        ) : (
          <p className="muted">Ask about failed strategies, validation runs, evidence rules, regimes, or next hypotheses.</p>
        )}
      </section>
    </aside>
  );
}
