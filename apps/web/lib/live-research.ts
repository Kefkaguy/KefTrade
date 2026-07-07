import {
  getResearchArchive,
  getResearchHypotheses,
  getResearchIntelligence,
  getResearchJournal,
  getResearchTimeline,
  getSymbols,
  getValidationRuns,
  type ResearchArchiveRow,
  type ResearchHypothesis,
  type ResearchIntelligence,
  type ResearchJournalEntry,
  type ResearchTimelineEvent,
  type SymbolRow,
  type ValidationRun
} from "@/lib/api";

export type LiveResearchSnapshot = {
  symbols: SymbolRow[];
  hypotheses: ResearchHypothesis[];
  journal: ResearchJournalEntry[];
  timeline: ResearchTimelineEvent[];
  archive: ResearchArchiveRow[];
  validationRuns: ValidationRun[];
  intelligence: ResearchIntelligence | null;
};

export type TimelineItem = {
  date: string;
  title: string;
  body: string;
  status?: string;
};

export async function getLiveResearchSnapshot(): Promise<LiveResearchSnapshot> {
  const [symbols, hypotheses, journal, timeline, archive, validationRuns, intelligence] = await Promise.all([
    safe(getSymbols(), []),
    safe(getResearchHypotheses(), []),
    safe(getResearchJournal(), []),
    safe(getResearchTimeline(), []),
    safe(getResearchArchive(), []),
    safe(getValidationRuns(), []),
    safe(getResearchIntelligence(), null)
  ]);

  return { symbols, hypotheses, journal, timeline, archive, validationRuns, intelligence };
}

async function safe<T>(promise: Promise<T>, fallback: T): Promise<T> {
  try {
    return await promise;
  } catch {
    return fallback;
  }
}

export function countBy<T>(rows: T[], getKey: (row: T) => string | null | undefined): Record<string, number> {
  return rows.reduce<Record<string, number>>((acc, row) => {
    const key = getKey(row) || "Unknown";
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
}

export function barRows(counts: Record<string, number>, emptyLabel = "No data") {
  const rows = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({ label, value, meta: String(value) }));
  return rows.length ? rows : [{ label: emptyLabel, value: 0, meta: "0" }];
}

export function numericSeries(values: Array<number | string | null | undefined>, fallback = [0]): number[] {
  const parsed = values.map((value) => Number(value)).filter((value) => Number.isFinite(value));
  return parsed.length ? parsed : fallback;
}

export function validationSeries(runs: ValidationRun[]): number[] {
  return numericSeries(
    runs.map((run) => {
      const summary = run.summary || {};
      const bestScore = summary.best_validation_score ?? summary.best_alpha_score ?? summary.validated_count ?? summary.recommendations;
      if (typeof bestScore === "object") return null;
      return bestScore as number | string | null | undefined;
    }),
    runs.length ? runs.map((_run, index) => index + 1) : [0]
  );
}

export function timelineItems(snapshot: LiveResearchSnapshot, limit = 8): TimelineItem[] {
  const fromTimeline = snapshot.timeline.map((item) => ({
    date: formatDate(item.timestamp),
    title: titleFromEvent(item.event_type),
    body: item.summary,
    status: item.evidence_refs?.[0]
  }));
  const fromJournal = snapshot.journal.map((item) => ({
    date: formatDate(item.created_at),
    title: titleFromEvent(item.entry_type),
    body: item.conclusion,
    status: item.experiment_id ? `experiment:${item.experiment_id}` : `journal:${item.id}`
  }));
  return [...fromTimeline, ...fromJournal]
    .sort((a, b) => b.date.localeCompare(a.date))
    .slice(0, limit);
}

export function latestExperimentRows(archive: ResearchArchiveRow[], limit = 8) {
  return archive.slice(0, limit).map((row) => ({
    strategy: row.strategy,
    candidate: row.candidate_id,
    recommendation: row.recommendation,
    trades: metricValue(row.metrics, "number_of_trades"),
    failure: row.failure_reasons?.[0] || "No failure reason recorded."
  }));
}

export function metricValue(metrics: Record<string, unknown> | undefined, key: string): string {
  const value = metrics?.[key];
  if (value === null || value === undefined || value === "") return "N/A";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  return String(value);
}

export function recommendationTone(value: string): "success" | "warning" | "error" | "neutral" {
  if (value === "Validated Alpha" || value === "Candidate for Paper Trading") return "success";
  if (value === "Research More" || value === "Needs More Research") return "warning";
  if (value === "Reject" || value === "rejected") return "error";
  return "neutral";
}

export function statusClass(value: string): string {
  const tone = recommendationTone(value);
  if (tone === "success") return "setup";
  if (tone === "warning") return "watchlist";
  if (tone === "error") return "avoid";
  return "";
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "No date";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toISOString().slice(0, 10);
}

export function titleFromEvent(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function displayAssetClass(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function displayProviderFromModel(model: string | undefined): string {
  const lowered = (model || "").toLowerCase();
  if (lowered.includes("llama") || lowered.includes("groq")) return "Groq";
  if (lowered.includes("gpt") || lowered.includes("openai")) return "OpenAI";
  if (lowered.includes("extractive")) return "Deterministic";
  return "Unknown";
}
