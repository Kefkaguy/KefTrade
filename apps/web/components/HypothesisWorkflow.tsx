"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { ActionNote, Toast } from "@/components/ResearchUI";
import { createResearchHypothesis, runHypothesisExperiment, type ResearchHypothesis } from "@/lib/api";

type ToastState = {
  tone: "success" | "error" | "info";
  message: string;
};

export function HypothesisComposer() {
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [hypothesis, setHypothesis] = useState("");
  const [tags, setTags] = useState("");
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<ToastState>({ tone: "info", message: "" });

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setToast({ tone: "info", message: "" });
    try {
      const created = await createResearchHypothesis({
        title: title.trim(),
        hypothesis: hypothesis.trim(),
        tags: tags.split(",").map((tag) => tag.trim()).filter(Boolean)
      });
      setTitle("");
      setHypothesis("");
      setTags("");
      setToast({ tone: "success", message: `Hypothesis ${created.id} created. You can run an experiment from the backlog below.` });
      router.refresh();
    } catch {
      setToast({ tone: "error", message: "Could not create the hypothesis. Check the API connection and required fields." });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="workflowStack">
      <ActionNote
        title="What this does"
        body="Creates an auditable research hypothesis and journal entry. It does not run a strategy or produce trading advice."
      />
      <form className="formGrid" onSubmit={onSubmit}>
        <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Short title" required maxLength={200} />
        <textarea value={hypothesis} onChange={(event) => setHypothesis(event.target.value)} placeholder="Research hypothesis to test" required />
        <input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="Tags, comma separated" />
        <button className="button" type="submit" disabled={loading || !title.trim() || !hypothesis.trim()}>
          {loading ? "Creating hypothesis..." : "Create hypothesis"}
        </button>
      </form>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}

export function HypothesisExperimentAction({ hypothesis }: { hypothesis: ResearchHypothesis }) {
  const router = useRouter();
  const [maxCandidates, setMaxCandidates] = useState(5);
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<ToastState>({ tone: "info", message: "" });

  async function runExperiment() {
    setLoading(true);
    setToast({ tone: "info", message: "" });
    try {
      const result = await runHypothesisExperiment(hypothesis.id, { maxCandidates });
      const recommendation = String((result.summary as Record<string, unknown> | undefined)?.best_recommendation ?? "complete");
      setToast({ tone: "success", message: `Experiment complete for hypothesis ${hypothesis.id}: ${recommendation}.` });
      router.refresh();
    } catch {
      setToast({ tone: "error", message: `Could not run experiment for hypothesis ${hypothesis.id}. Sync data and try again.` });
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="workflowStack">
      <ActionNote
        title="Experiment action"
        body="Runs deterministic generated candidates against currently loaded validation datasets, persists the experiment, updates hypothesis status, and writes journal evidence."
      />
      <label className="field">
        <span className="muted">Candidate limit</span>
        <input type="number" min={1} max={500} value={maxCandidates} onChange={(event) => setMaxCandidates(Number(event.target.value))} />
      </label>
      <button className="button secondary" type="button" onClick={runExperiment} disabled={loading}>
        {loading ? "Running experiment..." : "Run experiment"}
      </button>
      <Toast tone={toast.tone} message={toast.message} />
    </div>
  );
}
